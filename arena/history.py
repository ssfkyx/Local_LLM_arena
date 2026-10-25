"""Local Arena — 历史归档与跨模型对比。

模块名: arena.history
职责: 在 ResultStore 之上做只读查询：某模型的记录、最新记分板、跨模型的 Comparison 行；
      把"只有 official 档才参与对比"落成过滤（ADR-0018），并对"历史里混有旧评估协议记录"
      给出可显示的告警（ADR-0021）。
依赖: 标准库 datetime；arena.domain、arena.store、arena.vocabulary。
被依赖: arena.session、arena.ui.controller。
相关 ADR: docs/decisions/ADR-0005-jsonl-archive.md、
          docs/decisions/ADR-0010-comparison-across-models.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md
"""

from __future__ import annotations

from datetime import datetime, timezone

from arena.domain import (
    BenchRecord,
    ComparisonRow,
    ExamRecord,
    Model,
    PerplexityRecord,
    Scoreboard,
    TrialRecord,
)
from arena.store import ResultStore
from arena.vocabulary import OFFICIAL_PROFILE, PROTOCOL_NAME


def official_records(records: list[TrialRecord]) -> list[TrialRecord]:
    """筛出 official 档（真实档）的记录。

    前置条件: records 为任意记录的列表（可为空）。
    后置条件: 返回 profile 等于 OFFICIAL_PROFILE 的子集，顺序不变。
    禁止: 用"负载参数看起来像不像 quick"来推断档位——档位是落盘的事实，不是猜测（ADR-0018）。
    异常: 无。
    """
    return [record for record in records if record.profile == OFFICIAL_PROFILE]


def protocol_warning(records: list[TrialRecord], since: str | None) -> str | None:
    """生成"历史里混有旧评估协议记录"的告警文案。

    前置条件: since 为当前协议的生效时刻（ProtocolMarker 给出，UTC ISO 秒级），可为 None。
    后置条件: since 非 None 且存在 started_at 早于它的记录时，返回单行告警（含条数与生效时刻）；
               否则返回 None。
    禁止: 在 since 为 None 时声称"历史全是当前协议"——没有分界线时应当沉默，而不是替读者下结论；
          也禁止对解析不了的时间戳做猜测比较（跳过该条，不据此报警）。
    异常: 无。
    """
    if not since:
        return None
    boundary = _parse_stamp(since)
    if boundary is None:
        return None
    stale = 0
    for record in records:
        stamp = _parse_stamp(record.started_at)
        if stamp is not None and stamp < boundary:
            stale += 1
    if not stale:
        return None
    return (
        f"注意: 历史里有 {stale} 条记录早于当前评估协议（{PROTOCOL_NAME}，生效于 {since}）——"
        "它们按旧口径测的，不要与本表数字混读（ADR-0021）。"
    )


def _parse_stamp(text: str) -> "datetime | None":
    """把 UTC ISO 时间戳解析成带时区的 datetime；解析不了时返回 None。

    前置条件: text 为任意字符串。
    后置条件: 可解析时返回带时区的 datetime（无时区信息的一律按 UTC 处理），否则 None。
    禁止: 用字符串前缀比较代替时间比较——不同偏移量的时间戳按字典序排会给出错误结论。
    异常: 无（坏时间戳由返回值表达，不由异常表达——它只影响一条告警）。
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class ScoreboardAssembler:
    """把一组 TrialRecord 折叠成一块 Scoreboard（纯函数，无 I/O）。"""

    @staticmethod
    def build(model: Model, records: list[TrialRecord], run_id: str) -> Scoreboard:
        """组装记分板。

        前置条件: records 属于同一次 Run 的同一个 Model（允许为空）。
        后置条件: 缺失的指标以 None 表达；失败项汇总进 failures（"bench: Program terminated" 形式）；
                   全部指标齐全且无失败时 complete 为真；profile 取自记录本身
                   （记录为空时为空串，表示"这板子没有来源可标"）。
        禁止: 用上一次 Run 的旧值填充本次缺失的指标——那会让 Scoreboard 说谎。
        异常: 无。
        """
        bench_pp: float | None = None
        bench_tg: float | None = None
        perplexity: float | None = None
        perplexity_stderr: float | None = None
        answers: list[str] = []
        failures: list[str] = []

        for record in records:
            if not record.ok:
                failures.append(f"{record.kind.value.lower()}: {record.error or '未知错误'}")
                # 失败记录里可能仍带着**部分成功**的答卷（EXAM 逐卷落盘，ADR-0021）：
                # 那些文件确实在磁盘上，必须照实计数，否则界面会把"跑成的那几卷"藏起来。
                if isinstance(record, ExamRecord):
                    answers.extend(record.answer_paths)
                continue
            if isinstance(record, BenchRecord):
                bench_pp = record.throughput(n_prompt=True)
                bench_tg = record.throughput(n_prompt=False)
            elif isinstance(record, PerplexityRecord) and record.result is not None:
                perplexity = record.result.ppl
                perplexity_stderr = record.result.ppl_stderr
            elif isinstance(record, ExamRecord):
                answers.extend(record.answer_paths)

        return Scoreboard(
            model=model,
            run_id=run_id,
            bench_pp_ts=bench_pp,
            bench_tg_ts=bench_tg,
            perplexity=perplexity,
            perplexity_stderr=perplexity_stderr,
            answer_paths=tuple(answers),
            failures=tuple(failures),
            wall_seconds=sum(float(record.wall_seconds) for record in records),
            profile=records[0].profile if records else "",
        )


class Archive:
    """历史库的查询视图。"""

    def __init__(self, store: ResultStore) -> None:
        self._store = store

    def all_records(self) -> list[TrialRecord]:
        """返回全部历史记录（写入顺序）。

        前置条件: 无。
        后置条件: 每次调用都重新读盘——历史可能被其他 Run 追加。
        禁止: 缓存结果并长期复用（会看不到新记录）。
        异常: OSError —— 历史文件不可读。
        """
        return self._store.load_all()

    def records_for(self, model_path: str) -> list[TrialRecord]:
        """返回某一模型的全部历史记录。

        前置条件: model_path 为 Model.path 的取值（历史里以它为准）。
        后置条件: 返回写入顺序的记录子集；无记录时为空列表。
        禁止: 按显示名匹配——显示名会变，路径才是身份。
        异常: OSError —— 历史文件不可读。
        """
        return [record for record in self.all_records() if record.model_path == model_path]

    def models(self) -> list[str]:
        """返回历史中出现过的全部模型路径，按首次出现顺序。

        前置条件: 无。
        后置条件: 每个 model_path 只出现一次，顺序为历史中的首次出现顺序。
        禁止: 去重时排序——首次出现顺序承载"先测了谁"的信息。
        异常: OSError —— 历史文件不可读。
        """
        seen: list[str] = []
        for record in self.all_records():
            if record.model_path not in seen:
                seen.append(record.model_path)
        return seen

    def latest_scoreboard(self, model_path: str) -> Scoreboard:
        """由历史记录重建**最近一次有成绩的 Run** 的记分板。

        前置条件: 无；该模型无历史时返回一个"无历史"占位记分板（模型名为 "(无历史)"）。
        后置条件: 返回的 Scoreboard 只由某一轮 run_id 的记录组成——优先取最新一轮**含成功记录**的 Run
                   （与对比表"取最新有效值"同源，见 ADR-0010）；该模型全轮失败时退化为最新一轮，
                   于是屏上是"三个 `—` + 失败原因"，读作"试过但没成"。
                   档位选择：只要该模型有 official 档记录，就只看 official 档——
                   否则一次 quick 自检就会把已有成绩从屏上顶掉（ADR-0018）；
                   只有 quick 记录时照实显示 quick 的成绩，并由 profile 字段标明来源。
        禁止: 跨 run_id 拼凑不同轮次的指标——记分板的定义就是"一轮 Run 的汇总"，
              混轮次会让读者看到一份从不存在的成绩单。
        异常: OSError —— 历史文件不可读。
        """
        records = self.records_for(model_path)
        if not records:
            return ScoreboardAssembler.build(self._empty_model(), [], "")
        pool = official_records(records) or records
        chosen = self._newest_run(pool, require_success=True) or self._newest_run(pool, require_success=False)
        run_id = chosen[0].run_id
        return ScoreboardAssembler.build(self._model_of(chosen), chosen, run_id)

    @staticmethod
    def _newest_run(records: list[TrialRecord], *, require_success: bool) -> list[TrialRecord]:
        """取最新一轮 Run 的记录；require_success 为真时只考虑含成功记录的那些轮。

        前置条件: records 非空且同属一个 model_path。
        后置条件: 返回同一 run_id 的全部记录（顺序保持传入顺序）；无符合条件的轮次时返回空列表。
        禁止: 在同一轮内部再筛记录——一轮要么整轮展示，要么整轮不看。
        异常: 无。
        """
        by_run: dict[str, list[TrialRecord]] = {}
        for record in records:
            by_run.setdefault(record.run_id, []).append(record)
        runs = [group for group in by_run.values() if not require_success or any(item.ok for item in group)]
        if not runs:
            return []
        return max(runs, key=lambda group: max(item.started_at for item in group))

    @staticmethod
    def _empty_model() -> Model:
        """返回"无历史"占位模型描述符。"""
        return Model(path="", display_name="(无历史)", quantization="", size_bytes=0)

    @staticmethod
    def _model_of(records: list[TrialRecord]) -> Model:
        """用记录里的元数据重建一个"仅用于展示"的 Model 描述符。

        前置条件: records 非空且同属一个 model_path。
        后置条件: 返回 quantization/size_bytes 为空的展示用 Model（历史里没存这两个字段）。
        禁止: 无。
        异常: IndexError —— records 为空（调用方需先判空）。
        """
        record = records[0]
        return Model(path=record.model_path, display_name=record.model_name, quantization="", size_bytes=0)


class ComparisonBuilder:
    """把历史记录折叠成跨模型对比表。"""

    @staticmethod
    def build(records: list[TrialRecord]) -> list[ComparisonRow]:
        """构建 Comparison 行。

        前置条件: records 可为任意模型、任意档的混合历史。
        后置条件: **先丢掉非 official 档**（quick 自检记录一律不进对比，ADR-0018），
                   再每个 model_path 一行，取该模型各类 Trial 的最新有效值；
                   按 bench pp 吞吐降序排列，无 pp 值的排在最后（名次不猜）；
                   last_run_at 取"产生这些数字的那次记录"的时间，只有失败记录时退化为最后一次尝试时间。
        禁止: 把不同模型的记录做平均——对比是并列，不是聚合（ADR-0010）；
              也禁止用失败记录的时间戳给旧数字"续期"——那会让读者以为数字是刚测的；
              更禁止把 quick 档数字放进来——那会让"同负载才可比"这个前提当场失效。
        异常: 无。
        """
        grouped: dict[str, list[TrialRecord]] = {}
        for record in official_records(records):
            grouped.setdefault(record.model_path, []).append(record)

        rows: list[ComparisonRow] = []
        for model_path, group in grouped.items():
            bench_pp: float | None = None
            bench_tg: float | None = None
            perplexity: float | None = None
            # 数字与时间戳必须来自同一次记录：失败记录只进历史视图，不给对比行的数字续期。
            measured_at = ""
            for record in sorted(group, key=lambda item: item.started_at):
                if isinstance(record, BenchRecord) and record.ok:
                    bench_pp = record.throughput(n_prompt=True)
                    bench_tg = record.throughput(n_prompt=False)
                    measured_at = record.started_at
                elif isinstance(record, PerplexityRecord) and record.ok and record.result is not None:
                    perplexity = record.result.ppl
                    measured_at = record.started_at
            rows.append(
                ComparisonRow(
                    model_name=group[-1].model_name,
                    model_path=model_path,
                    bench_pp_ts=bench_pp,
                    bench_tg_ts=bench_tg,
                    perplexity=perplexity,
                    last_run_at=measured_at or max(record.started_at for record in group),
                )
            )

        rows.sort(key=lambda row: (row.bench_pp_ts is None, -(row.bench_pp_ts or 0.0)))
        return rows
