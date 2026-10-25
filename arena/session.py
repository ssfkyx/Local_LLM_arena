"""Local Arena — 用例编排（Run 的唯一实现）。

模块名: arena.session
职责: 定义"一轮 Run"：按规范顺序对同一 Model 施加 TestPlan 里的各类 Trial、
      逐条落盘历史、汇总出 Scoreboard；并对外暴露取消与历史查询。
依赖: arena.config、arena.domain、arena.export、arena.history、arena.process、arena.runners、arena.sink、arena.store。
被依赖: arena.ui.controller、run_arena.py。
相关 ADR: docs/decisions/ADR-0003-runner-polymorphism-and-module-boundaries.md、
          docs/decisions/ADR-0009-no-automatic-grading.md、
          docs/decisions/ADR-0012-vram-not-a-constraint.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0020-completion-condition-instead-of-token-cap.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md、
          docs/decisions/ADR-0023-run-answer-text-export.md
"""

from __future__ import annotations

import warnings
from datetime import datetime
from arena.config import AppConfig
from arena.domain import (
    BenchCase,
    ComparisonRow,
    Corpus,
    ExamRecord,
    Model,
    Paper,
    Scoreboard,
    TestPlan,
    TrialRecord,
)
from arena.export import render
from arena.history import (
    Archive,
    ComparisonBuilder,
    ScoreboardAssembler,
    official_records,
    protocol_warning,
)
from arena.process import CancellationToken
from arena.runners import TrialRunner
from arena.sink import ProgressSink, TrialContext
from arena.store import ProtocolMarker, ResultStore, RunArtifacts
from arena.vocabulary import ANSWERS_TEXT_FILENAME, RUN_ORDER, TestKind


def make_run_id(model: Model) -> str:
    """生成一轮 Run 的标识 `<ISO 时间戳去冒号>_<模型 slug>`。

    前置条件: model 非空。
    后置条件: 返回值是合法的单层目录名，且按字典序即按时间序。
    禁止: 使用模型路径拼接——路径里的斜杠会破坏目录层级。
    异常: 无。
    """
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return f"{stamp}_{model.slug()}"


class ArenaSession:
    """一轮 Run 的编排者，也是 UI 与核心逻辑之间的唯一界面。"""

    def __init__(
        self,
        config: AppConfig,
        runners: dict[TestKind, TrialRunner],
        store: ResultStore,
        archive: Archive | None = None,
    ) -> None:
        self._config = config
        self._runners = runners
        self._store = store
        self._archive = archive or Archive(store)
        # 当前轮次的取消信号：cancel() 从 UI 线程置位，Runner 在 worker 线程轮询。
        self._token: CancellationToken | None = None

    def default_plan(self, model: Model) -> TestPlan:
        """按配置默认值构造该模型的测试计划。

        前置条件: model 来自本机 ModelCatalog（路径可读）。
        后置条件: 计划包含全部三类 Trial 与该目录下的全部试卷；试卷文本已读入内存；
                   三类负载参数取自 config，因此计划本身就是"这一轮用什么负载"的定义，
                   其档名（config.profile）会随记录一起落盘（ADR-0018）。
        禁止: 在试卷目录为空时静默产出"无试卷"计划——那会让 EXAM 静默变成空跑；
              也禁止在此处接受"临时参数"——参数只能来自配置，不存在第三种负载。
        异常: FileNotFoundError —— 语料缺失；ValueError —— 试卷目录为空。
        """
        papers = tuple(Paper.from_path(path) for path in self._config.paper_paths())
        if not papers:
            raise ValueError(f"试卷目录中没有 paper_* 文件: {self._config.papers_dir}")
        corpus = Corpus.from_path(self._config.corpus_path())
        return TestPlan(
            model=model,
            kinds=frozenset(RUN_ORDER),
            bench_case=BenchCase(
                n_prompt=self._config.bench.n_prompt,
                n_gen=self._config.bench.n_gen,
                n_depth=self._config.bench.n_depth,
            ),
            bench_reps=self._config.bench.reps,
            corpus=corpus,
            ppl_n_ctx=self._config.perplexity.n_ctx,
            ppl_chunks=self._config.perplexity.chunks,
            papers=papers,
            exam_n_predict=self._config.exam.n_predict,
            exam_n_ctx=self._config.exam.n_ctx,
            exam_temperature=self._config.exam.temperature,
            exam_seed=self._config.exam.seed,
            exam_prompt_suffix=self._config.exam.prompt_suffix,
        )

    def run(self, plan: TestPlan, sink: ProgressSink) -> Scoreboard:
        """执行一轮 Run。

        前置条件: 同类 Runner 齐全（plan.kinds 里的每种都有对应 Runner）。
        后置条件: 每个 Trial 的记录（成功或失败）都已追加进历史；
                   本轮有答卷时，另在 run 目录落一份汇总文本 answers.txt（ADR-0023）；
                   返回的 Scoreboard 只含本轮结果；取消后已完成的记录仍然保留。
        禁止: 并发调用——同一 Session 同时只允许一轮 Run（UI 侧靠禁用 START 保证）。
        异常: KeyError —— 计划里出现了没有 Runner 的 Trial 种类。
        """
        run_id = make_run_id(plan.model)
        token = CancellationToken()
        self._token = token
        records: list[TrialRecord] = []

        for kind in RUN_ORDER:
            if not plan.wants(kind):
                continue
            if token.is_cancelled():
                break
            runner = self._runners[kind]
            record = runner.run(self._context(run_id, plan, sink, token))
            records.append(record)
            # 先落盘再继续：即使下一刻崩溃/被取消，已完成的结果也不会丢。
            self._store.append(record)

        # 收尾的附加步骤：历史已经写完了，这里只是把答卷再切一份纯文本出来。
        # 它不影响任何数字，失败也不该让本轮结果作废（见 _export_answers 的禁止条）。
        self._export_answers(run_id, records)
        return ScoreboardAssembler.build(plan.model, records, run_id)

    def _export_answers(self, run_id: str, records: list[TrialRecord]) -> str | None:
        """把本轮答卷汇总成一份纯文本落到 run 目录（ADR-0023）。

        前置条件: records 为本轮已落盘的记录（可能为空，也可能没有 EXAM）。
        后置条件: 本轮存在带答卷的 EXAM 记录时，写出 runs/<run_id>/answers.txt 并返回其绝对路径；
                  没有答卷（未跑 EXAM，或每一卷都没跑成）时什么都不做并返回 None。
        禁止: 因导出失败而让 run 抛错或丢掉已有记录——导出是附加产物，失败只告警；
              也禁止在无答卷时写出空文件，那会把"没跑"伪装成"跑出来是空的"。
        异常: 无（OSError/ValueError 转成告警，不向上抛）。
        """
        record = next(
            (item for item in records if isinstance(item, ExamRecord) and item.sheets),
            None,
        )
        if record is None:
            return None
        try:
            text = render(record, self._config.papers_dir)
            artifacts = RunArtifacts(self._config.runs_dir)
            return artifacts.save(run_id, ANSWERS_TEXT_FILENAME, text)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            warnings.warn(f"答卷汇总文本导出失败（历史记录不受影响）: {exc}", stacklevel=2)
            return None

    def cancel(self) -> None:
        """请求取消当前 Run。

        前置条件: 无（无进行中的 Run 时是空操作）。
        后置条件: 正在跑的子进程会在一个轮询周期内被终止；未开始的 Trial 不再启动。
        禁止: 在 Run 结束后复用同一个 token 开始新一轮——每轮新建（见 run()）。
        异常: 无。
        """
        if self._token is not None:
            self._token.cancel()

    @property
    def cancelled(self) -> bool:
        """当前轮次是否已被请求取消。

        前置条件: 无（未开始过任何 Run 时 _token 为 None）。
        后置条件: 已有取消请求时为 True；否则 False。
        禁止: 另建一份取消状态——这是取消信号的**唯一事实来源**，UI 与 Runner 都必须读它。
        异常: 无。
        """
        return self._token is not None and self._token.is_cancelled()

    def history(self) -> list[TrialRecord]:
        """返回全部历史记录（写入顺序）。

        前置条件: 无（历史文件不存在时视为空）。
        后置条件: 每次调用都重新读盘，含其他进程刚追加的记录。
        禁止: 缓存结果——GUI 的 HISTORY 视图必须反映磁盘当前事实（ADR-0005）。
        异常: OSError —— 历史文件存在但不可读。
        """
        return self._archive.all_records()

    def compare(self) -> list[ComparisonRow]:
        """返回跨模型的对比表。

        前置条件: 无。
        后置条件: 每个 model_path 一行，取各类 Trial 的最新有效值（ADR-0010）。
        禁止: 在此处做聚合平均或补零排名——对比是并列。
        异常: OSError —— 历史文件不可读。
        """
        return ComparisonBuilder.build(self._archive.all_records())

    def register_protocol(self) -> str:
        """登记当前评估协议并返回其生效时刻。

        前置条件: 无。
        后置条件: runs 目录下存在 PROTOCOL 标记；协议名未变时返回既有生效时刻（不刷新）。
        禁止: 在这里归档或删除历史——重置历史是人的动作，本方法只登记分界线（ADR-0021）。
        异常: OSError —— 产物目录不可写。
        """
        return ProtocolMarker(self._config.runs_dir).ensure()

    def protocol_warning(self) -> str | None:
        """返回"对比表里可能混入旧协议数字"的告警文案。

        前置条件: 无。
        后置条件: 存在 official 档记录早于当前协议生效时刻时，返回单行告警；否则 None。
        禁止: 把 quick 档记录算进告警——对比表本来就不收它们，拿它们报警只会制造噪音。
        异常: OSError —— 标记文件或历史文件不可读。
        """
        since = ProtocolMarker(self._config.runs_dir).read_since()
        return protocol_warning(official_records(self._archive.all_records()), since)

    def latest_scoreboard(self, model: Model) -> Scoreboard | None:
        """返回某模型的历史记分板。

        前置条件: 无。
        后置条件: 该模型在历史里完全没有记录时返回 None（区别于"跑过但全失败"）。
        禁止: 无。
        异常: 无。
        """
        if not self._archive.records_for(model.path):
            return None
        return self._archive.latest_scoreboard(model.path)

    @staticmethod
    def _context(run_id: str, plan: TestPlan, sink: ProgressSink, token: CancellationToken) -> TrialContext:
        """构造单个 Trial 的上下文（Runner 需要的一切都在这里）。"""
        return TrialContext(run_id=run_id, model=plan.model, plan=plan, sink=sink, token=token)
