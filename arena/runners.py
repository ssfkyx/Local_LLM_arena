"""Local Arena — Trial 执行器（每个 llama 工具一个 Runner）。

模块名: arena.runners
职责: 构造 llama.cpp 工具的命令行、执行、解析、产出 TrialRecord；是"工具差异"的唯一归属地，
      也是原始输出流落盘的执行点。
依赖: arena.config、arena.domain、arena.parsers、arena.paper、arena.process、arena.server、arena.sink、arena.store、arena.vocabulary。
被依赖: arena.session、run_arena.py。
相关 ADR: docs/decisions/ADR-0002-subprocess-over-server.md、
          docs/decisions/ADR-0003-runner-polymorphism-and-module-boundaries.md、
          docs/decisions/ADR-0004-parsers-follow-stream-semantics.md、
          docs/decisions/ADR-0011-answer-sheet-reports-throughput.md、
          docs/decisions/ADR-0012-vram-not-a-constraint.md、
          docs/decisions/ADR-0017-environmental-cuda-oom-is-recorded-not-retried.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md、
          docs/decisions/ADR-0020-completion-condition-instead-of-token-cap.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md、
          docs/decisions/ADR-0022-answer-coverage-check.md、
          docs/decisions/ADR-0025-per-question-exam.md、
          docs/decisions/ADR-0026-in-question-progress-from-server-log.md
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from arena.config import AppConfig
from arena.coverage import Coverage
from arena.domain import (
    AnswerSheet,
    BenchRecord,
    ExamLoad,
    ExamRecord,
    PaperFailure,
    PerplexityRecord,
    PerplexityResult,
    TrialRecord,
)
from arena.parsers import (
    BenchJsonParser,
    ParseError,
    PerplexityLogParser,
    PerplexityParseError,
    ServerTiming,
    ServerTimingParser,
)
from arena.paper import Question, split_paper
from arena.process import ProcessOutcome, ProcessStartError, SubprocessRunner
from arena.server import (
    ChatCompleter,
    Completion,
    CompletionCancelled,
    CompletionError,
    CompletionTimeout,
    LlamaServer,
    ServerLogTail,
    ServerStartError,
)
from arena.sink import TrialContext
from arena.store import AnswerSheetWriter, RunArtifacts
from arena.vocabulary import Stage, TestKind


@dataclass(frozen=True)
class TrialExecution:
    """一次 Trial 的原始执行产物（一个或多个子进程的 Outcome，按执行顺序）。

    EXAM 会在执行过程中**逐卷落盘**，因此这里额外携带已落盘的答卷与逐卷失败原因；
    其余 Trial 这三项恒为空（它们只有一个子进程、一份产物）。
    """

    outcomes: tuple[ProcessOutcome, ...]
    sheets: tuple[AnswerSheet, ...] = ()
    answer_paths: tuple[str, ...] = ()
    paper_failures: tuple[PaperFailure, ...] = ()

    @property
    def cancelled(self) -> bool:
        """本 Trial 的任一子进程是否被取消。

        前置条件: 无。
        后置条件: 任一 Outcome 的 cancelled 为真时为 True（Exam 可能启动多个子进程）。
        禁止: 只看第一个 Outcome——取消发生在第 N 份试卷时同样要判定为取消。
        异常: 无。
        """
        return any(outcome.cancelled for outcome in self.outcomes)

    @property
    def timed_out(self) -> bool:
        """本 Trial 是否触发了超时。

        前置条件: 无。
        后置条件: 任一 Outcome 的 timed_out 为真时为 True。
        禁止: 与 cancelled 合并成一个"没跑完"标志——两者对用户的含义不同（一个是自己点的）。
        异常: 无。
        """
        return any(outcome.timed_out for outcome in self.outcomes)

    def first_failure(self) -> ProcessOutcome | None:
        """返回第一个非零退出码的 Outcome，全成功时返回 None。

        前置条件: 无。
        后置条件: 按执行顺序返回首个失败项；无失败时返回 None。
        禁止: 返回最后一个失败项——首个失败才是根因（后续失败往往是它的连锁反应）。
        异常: 无。
        """
        for outcome in self.outcomes:
            if outcome.returncode != 0:
                return outcome
        return None


class TrialRunner(ABC):
    """Trial 的公共生命周期：落阶段 → 执行 → 判定 → 解析 → 产出记录。

    子类只实现"命令怎么拼"与"输出怎么变成记录"两件事（ADR-0003）。
    """

    def __init__(
        self,
        config: AppConfig,
        process: SubprocessRunner,
        artifacts: RunArtifacts | None = None,
    ) -> None:
        self._config = config
        self._process = process
        self._artifacts = artifacts

    @property
    @abstractmethod
    def kind(self) -> TestKind:
        """本 Runner 负责的 Trial 种类。

        前置条件: 无。
        后置条件: 返回固定的 TestKind（同一实例恒定）。
        禁止: 让实现依赖运行期状态——它同时是落盘记录的 kind 与进度事件的分组键。
        异常: 无。
        """

    def _build_command(self, context: TrialContext) -> Sequence[str]:
        """构造单个子进程的命令行（谁需要谁实现）。

        前置条件: context 为本 Trial 的上下文。
        后置条件: 子进程型 Runner（BENCH / PERPLEXITY）返回完整命令行；
                  服务型 Runner（EXAM 走常驻 llama-server）不重写本方法——它的进程由 arena.server 管。
        禁止: 在服务型 Runner 里返回假的命令行充数——那会让"命令从哪来"出现两个来源（ADR-0025）。
        异常: NotImplementedError —— 子进程型 Runner 忘了重写。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 _build_command")

    @abstractmethod
    def _execute(self, context: TrialContext) -> TrialExecution:
        """执行 Trial 所需的全部子进程，并落盘原始输出。"""

    @abstractmethod
    def _build_record(
        self,
        context: TrialContext,
        execution: TrialExecution,
        started_at: str,
        wall_seconds: float,
    ) -> TrialRecord:
        """把执行产物解析成记录；解析失败应抛 ParseError 而不是返回半个对象。"""

    def run(self, context: TrialContext) -> TrialRecord:
        """执行一次 Trial 并返回记录（失败也返回记录，不抛异常）。

        前置条件: context.plan.model 与 context.model 一致；同一 Runner 实例不被并发调用。
        后置条件: 返回值必然是 TrialRecord；ok=False 时 error 字段说明原因（取消/超时/退出码/解析/逐卷失败）；
                   原始 stdout/stderr 已尽力落盘；进度已通过 context.sink 汇报到终态；
                   **部分成功**（EXAM 已落盘的那几卷）会出现在失败记录的 payload 里（ADR-0021）。
        禁止: 在失败时抛异常给上层——失败是记分板的一等公民（否则一次失败会抹掉整轮历史）；
              也禁止在失败后自动重试或偷偷降参数——环境性 CUDA OOM 只如实记录（ADR-0017）。
        异常: 无（所有内部异常都被转成 ok=False 的记录）。
        """
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.perf_counter()
        context.sink.on_stage(self.kind, Stage.LOADING)

        try:
            execution = self._execute(context)
        except ProcessStartError as exc:
            return self._failure(
                context, None, started_at, time.perf_counter() - started, str(exc), Stage.FAILED
            )
        except OSError as exc:
            return self._failure(
                context, None, started_at, time.perf_counter() - started, f"落盘失败: {exc}", Stage.FAILED
            )

        wall_seconds = time.perf_counter() - started

        if execution.cancelled:
            return self._failure(context, execution, started_at, wall_seconds, "用户取消", Stage.CANCELLED)
        if execution.timed_out:
            return self._failure(context, execution, started_at, wall_seconds, "执行超时", Stage.FAILED)
        failure = execution.first_failure()
        if failure is not None:
            reason = self._exit_error(failure)
            return self._failure(context, execution, started_at, wall_seconds, reason, Stage.FAILED)
        if execution.paper_failures:
            # 子进程退出码正常，但某一份试卷的产物不可用（例如抽取失败）：
            # 这同样是"这一轮没跑全"，必须落失败记录而不是让它以成功的样子进历史。
            reason = "；".join(f"{item.paper_name}: {item.reason}" for item in execution.paper_failures)
            return self._failure(context, execution, started_at, wall_seconds, reason, Stage.FAILED)

        context.sink.on_stage(self.kind, Stage.PARSING)
        try:
            record = self._build_record(context, execution, started_at, wall_seconds)
        except ParseError as exc:
            return self._failure(context, execution, started_at, wall_seconds, str(exc), Stage.FAILED)

        context.sink.on_stage(self.kind, Stage.DONE)
        return record

    # -- 供子类复用的执行助手 -------------------------------------------------

    def _run_process(
        self,
        context: TrialContext,
        command: Sequence[str],
        *,
        artifact_prefix: str,
        timeout_s: float,
        on_stdout_line: Callable[[str], None] | None = None,
        on_stderr_line: Callable[[str], None] | None = None,
    ) -> ProcessOutcome:
        """跑一个子进程并把原始 stdout/stderr 留档。

        前置条件: artifact_prefix 为不含扩展名的简单名字。
        后置条件: 返回 Outcome；原始输出已写入 runs/<run_id>/<prefix>.out|.err（若配置了产物目录）；
                   失败（含取消/超时/异常）也留档，保证"数字可回溯到原始文本"；
                   两个可选回调都只在**行到达时**被调用，且不改变留档内容——回调用途是实时进度，
                   不是取数的唯一来源（ppl 的总 chunk 数就用它提前拿到，见 ADR-0020）。
        禁止: 在无产物目录时静默成功——此时必须自行检查 artifacts 是否为空。
        异常: ProcessStartError / OSError —— 向上抛给 run() 统一转成失败记录。
        """
        context.sink.on_stage(self.kind, Stage.RUNNING)
        outcome = self._process.run(
            command,
            timeout_s=timeout_s,
            cancel_check=context.cancel_check,
            on_stdout_line=on_stdout_line,
            on_stderr_line=on_stderr_line,
        )
        if self._artifacts is not None:
            self._artifacts.save(context.run_id, f"{artifact_prefix}.out", outcome.stdout)
            self._artifacts.save(context.run_id, f"{artifact_prefix}.err", outcome.stderr)
        return outcome

    def _failure(
        self,
        context: TrialContext,
        execution: TrialExecution | None,
        started_at: str,
        wall_seconds: float,
        error: str,
        stage: Stage,
    ) -> TrialRecord:
        """构造一条失败记录并汇报终态。

        前置条件: execution 为本次执行产物；子进程根本没起来时为 None。
        后置条件: 返回 ok=False 的记录；EXAM 的 execution 里**已落盘的那几卷**会被带进记录
                   （部分成功），不是整轮清空。
        禁止: 因为有失败就丢弃 execution 里已完成的部分——那会把"少了一卷"写成"整轮没跑"（ADR-0021）。
        异常: 无。
        """
        context.sink.on_progress(self.kind, None, error)
        context.sink.on_stage(self.kind, stage)
        return self._new_record(context, execution, started_at, wall_seconds, ok=False, error=error)

    @abstractmethod
    def _new_record(
        self,
        context: TrialContext,
        execution: TrialExecution | None,
        started_at: str,
        wall_seconds: float,
        *,
        ok: bool,
        error: str,
    ) -> TrialRecord:
        """构造本 Runner 对应类型的记录骨架。

        前置条件: execution 为本次执行产物，可为 None（进程没起来）。
        后置条件: 返回该 Runner 对应的记录类型；EXAM 可携带部分成功的答卷。
        禁止: 把 execution 当成"必须有"——启动失败路径下它是 None。
        异常: 无。
        """

    @staticmethod
    def _exit_error(outcome: ProcessOutcome) -> str:
        """把非零退出码翻译成一句可读原因（带 stderr 尾部）。"""
        tail = " ".join(line.strip() for line in outcome.stderr.strip().splitlines()[-3:])
        reason = f"退出码 {outcome.returncode}"
        return f"{reason}: {tail}" if tail else reason

    def _fit_args(self) -> list[str]:
        """返回显存适配参数（`-fitt <余量 MiB>`）。

        前置条件: 无。
        后置条件: 返回 `["-fitt", "<MiB>"]`，MiB 为 config.vram_margin_bytes 向下取整的非负整数。
        禁止: 在此处按文件大小手工推算 `-ngl`——上卡层数交给 llama.cpp 的 fit 逻辑（ADR-0019）。
        异常: 无。
        """
        return ["-fitt", str(self._config.vram_margin_bytes // 1024**2)]

    def _base_record_kwargs(self, context: TrialContext, started_at: str, wall_seconds: float, ok: bool, error: str) -> dict:
        """组装 TrialRecord 的公共字段，并盖上本轮的负载档与显存余量戳。

        戳来自 config 而不是 context：负载档与显存余量是"这次进程用哪套条件"的环境事实，
        它们的唯一来源是 AppConfig（ADR-0013 / ADR-0018 / ADR-0019）。
        """
        return {
            "run_id": context.run_id,
            "model_path": context.model.path,
            "model_name": context.model.display_name,
            "started_at": started_at,
            "wall_seconds": wall_seconds,
            "ok": ok,
            "error": error,
            "profile": self._config.profile,
            "vram_margin_bytes": self._config.vram_margin_bytes,
        }


class BenchRunner(TrialRunner):
    """用 llama-bench 测吞吐（pp / tg 的 t/s）。"""

    def __init__(
        self,
        config: AppConfig,
        process: SubprocessRunner,
        artifacts: RunArtifacts | None = None,
        parser: BenchJsonParser | None = None,
    ) -> None:
        super().__init__(config, process, artifacts)
        self._parser = parser or BenchJsonParser()

    @property
    def kind(self) -> TestKind:
        """本 Runner 负责 BENCH。

        前置条件: 无。
        后置条件: 恒返回 TestKind.BENCH。
        禁止: 无。
        异常: 无。
        """
        return TestKind.BENCH

    def _build_command(self, context: TrialContext) -> Sequence[str]:
        """拼出 llama-bench 命令行。

        -p 与 -n 会被 llama-bench 各自展开成一条独立记录（tracer F7），
        因此这里不做任何拆分，交给工具自己输出。
        -fitt 让超过显存的模型自动部分卸载而不是直接失败（ADR-0019）。
        """
        plan = context.plan
        return [
            str(self._config.binary("llama-bench")),
            "-m",
            context.model.path,
            "-p",
            str(plan.bench_case.n_prompt),
            "-n",
            str(plan.bench_case.n_gen),
            "-d",
            str(plan.bench_case.n_depth),
            "-r",
            str(plan.bench_reps),
            *self._fit_args(),
            "-o",
            "json",
        ]

    def _execute(self, context: TrialContext) -> TrialExecution:
        """执行一次 llama-bench。"""
        outcome = self._run_process(
            context,
            self._build_command(context),
            artifact_prefix="bench",
            timeout_s=self._config.timeouts.bench,
        )
        return TrialExecution(outcomes=(outcome,))

    def _build_record(
        self,
        context: TrialContext,
        execution: TrialExecution,
        started_at: str,
        wall_seconds: float,
    ) -> BenchRecord:
        """解析 JSON 数组，并记下这次用的重复次数。

        解析只吃 stdout：stderr 里只有 CUDA 初始化噪声（tracer F1）。
        reps 取自计划（`-r` 是 llama-bench 的开关，不体现在每条结果的 case 里），
        它是"这条吞吐有几个样本"的唯一线索（ADR-0018）。
        """
        results = self._parser.parse(execution.outcomes[0].stdout)
        return BenchRecord(
            **self._base_record_kwargs(context, started_at, wall_seconds, True, ""),
            results=results,
            reps=context.plan.bench_reps,
        )

    def _new_record(
        self,
        context: TrialContext,
        execution: TrialExecution | None,
        started_at: str,
        wall_seconds: float,
        *,
        ok: bool,
        error: str,
    ) -> BenchRecord:
        """构造 BenchRecord（bench 只有一个子进程，不消费 execution 里的部分结果）。"""
        return BenchRecord(
            **self._base_record_kwargs(context, started_at, wall_seconds, ok, error),
            results=[],
        )


class PerplexityRunner(TrialRunner):
    """用 llama-perplexity 测语言建模质量（PPL）。"""

    def __init__(
        self,
        config: AppConfig,
        process: SubprocessRunner,
        artifacts: RunArtifacts | None = None,
        parser: PerplexityLogParser | None = None,
    ) -> None:
        super().__init__(config, process, artifacts)
        self._parser = parser or PerplexityLogParser()

    @property
    def kind(self) -> TestKind:
        """本 Runner 负责 PERPLEXITY。

        前置条件: 无。
        后置条件: 恒返回 TestKind.PERPLEXITY。
        禁止: 无。
        异常: 无。
        """
        return TestKind.PERPLEXITY

    def _build_command(self, context: TrialContext) -> Sequence[str]:
        """拼出 llama-perplexity 命令行（-fitt 负责显存适配，ADR-0019）。

        `--chunks -1` 是工具的"全部语料"（ADR-0020）：完成条件是跑完 corpus 全文，
        而不是跑固定的 N 个 chunk。
        """
        plan = context.plan
        return [
            str(self._config.binary("llama-perplexity")),
            "-m",
            context.model.path,
            "-f",
            plan.corpus.path,
            "-c",
            str(plan.ppl_n_ctx),
            "--chunks",
            str(plan.ppl_chunks),
            *self._fit_args(),
        ]

    def _execute(self, context: TrialContext) -> TrialExecution:
        """执行一次 llama-perplexity，并把进度串实时翻译成比例。

        进度来自 stdout 的 `[k]ppl,` 串（tracer F2）；但**总数不在进度串里**：
        计划值可能是 -1（=全部语料，见 ADR-0020），此时分母改由工具在 stderr 自报的
        `calculating perplexity over N chunks` 提供（F13：以工具自报为准）。
        拿到 N 之前一律不报进度——不猜分母，也不报假比例。
        """
        planned = context.plan.ppl_chunks
        state = {"total": planned if planned > 0 else 0}

        def read_total(line: str) -> None:
            if state["total"] > 0:
                return
            try:
                chunks, _ = self._parser.parse_setup(line)
            except PerplexityParseError:
                return
            state["total"] = chunks

        def report(line: str) -> None:
            total = state["total"]
            if total <= 0:
                return
            fraction = self._parser.parse_progress(line, total)
            if fraction is not None:
                context.sink.on_progress(self.kind, fraction, f"chunk {round(fraction * total)}/{total}")

        outcome = self._run_process(
            context,
            self._build_command(context),
            artifact_prefix="perplexity",
            timeout_s=self._config.timeouts.perplexity,
            on_stdout_line=report,
            on_stderr_line=read_total,
        )
        return TrialExecution(outcomes=(outcome,))

    def _build_record(
        self,
        context: TrialContext,
        execution: TrialExecution,
        started_at: str,
        wall_seconds: float,
    ) -> PerplexityRecord:
        """解析最终 PPL 与实际运行参数（两者都在 stderr）。"""
        outcome = execution.outcomes[0]
        ppl, ppl_stderr = self._parser.parse_final(outcome.stderr)
        chunks, n_ctx = self._parser.parse_setup(outcome.stderr)
        result = PerplexityResult(
            ppl=ppl,
            ppl_stderr=ppl_stderr,
            n_chunks=chunks,
            n_ctx=n_ctx,
            corpus_name=context.plan.corpus.name,
        )
        return PerplexityRecord(
            **self._base_record_kwargs(context, started_at, wall_seconds, True, ""), result=result
        )

    def _new_record(
        self,
        context: TrialContext,
        execution: TrialExecution | None,
        started_at: str,
        wall_seconds: float,
        *,
        ok: bool,
        error: str,
    ) -> PerplexityRecord:
        """构造 PerplexityRecord（perplexity 只有一个子进程，不消费 execution 里的部分结果）。"""
        return PerplexityRecord(
            **self._base_record_kwargs(context, started_at, wall_seconds, ok, error),
            result=None,
        )


class ExamRunner(TrialRunner):
    """逐题答卷：起一个常驻 llama-server，一题一个独立请求，最后拼成一份整卷答卷。

    与 BENCH/PERPLEXITY 不同，EXAM 不再是"一个 Trial 一个子进程"，而是"一个 Trial 一个 server
    + N 个请求"：模型只加载一次；每题一个独立请求（不携带历史、`cache_prompt:false`）。
    详见 ADR-0025；端点选择与隔离性的实测依据见 F29/F30/F31/F32。

    题内进度：请求本身**不流式**，但 server 会把生成统计写进自己的日志；Runner 每约 10 秒
    读一次那个文件，把"本题 N tok · R t/s · 日志 Ns 前"报进进度条（ADR-0026）。
    """

    def __init__(
        self,
        config: AppConfig,
        process: SubprocessRunner,
        artifacts: RunArtifacts | None = None,
        writer: AnswerSheetWriter | None = None,
        timing_parser: ServerTimingParser | None = None,
    ) -> None:
        super().__init__(config, process, artifacts)
        self._writer = writer or AnswerSheetWriter()
        self._timing_parser = timing_parser or ServerTimingParser()

    @property
    def kind(self) -> TestKind:
        """本 Runner 负责 EXAM。

        前置条件: 无。
        后置条件: 恒返回 TestKind.EXAM。
        禁止: 无。
        异常: 无。
        """
        return TestKind.EXAM

    def _execute(self, context: TrialContext) -> TrialExecution:
        """起 server，逐卷逐题请求，逐题落盘原始证据，逐卷拼出答卷文件。

        前置条件: plan.papers 非空；本机存在可执行的 llama-server（否则抛 ProcessStartError）。
        后置条件: 返回 TrialExecution——每个请求一条 Outcome；已完成的卷带上 sheets/answer_paths；
                   失败记进 paper_failures（形如 `paper_1: K3: 原因`）并停止该卷后续题；
                   上下文被 thinking 打满、`content` 为空但 `reasoning` 非空时，用 `reasoning`
                   作为该题正文（不判失败，继续下一题）；
                   server 无论成败都已按进程组收尾。
        禁止: 用裸 `/completion`（不套 chat template 会吃到上下文满，ADR-0025 的 F29）；
              也禁止 server 起来后不收它——孤儿会一直占显存（F34）。
        异常: ProcessStartError —— llama-server 无法启动/就绪（由 run() 转成失败记录）。
        """
        plan = context.plan
        outcomes: list[ProcessOutcome] = []
        sheets: list[AnswerSheet] = []
        paths: list[str] = []
        paper_failures: list[PaperFailure] = []
        coverages: list[tuple[str, Coverage]] = []
        started = time.perf_counter()

        try:
            binary = self._config.binary("llama-server")
        except FileNotFoundError as exc:
            raise ProcessStartError(str(exc)) from exc

        server = LlamaServer(
            binary,
            context.model.path,
            n_ctx=plan.exam_n_ctx,
            vram_margin_bytes=self._config.vram_margin_bytes,
            startup_timeout_s=self._config.timeouts.server_startup,
            log_path=self._server_log_path(context),
        )
        try:
            try:
                server.start()
            except ServerStartError as exc:
                raise ProcessStartError(f"llama-server 启动失败: {exc}") from exc
            completer = ChatCompleter(server.base_url)

            for paper_index, paper in enumerate(plan.papers):
                try:
                    questions = split_paper(paper.text, plan.exam_prompt_suffix)
                except ValueError as exc:
                    paper_failures.append(PaperFailure(paper.name, f"切题失败: {exc}"))
                    break
                blocks: list[str] = []
                answered_qids: list[str] = []
                missing_qids: list[str] = []
                prompt_tokens = generated_tokens = 0
                prompt_seconds = generation_seconds = 0.0
                paper_wall = 0.0
                failed = False
                for question_index, question in enumerate(questions):
                    self._report_alive(
                        context, paper_index, len(plan.papers), question_index, len(questions), started
                    )
                    # 题内心跳（ADR-0026）：日志偏移在请求发出前对齐到末尾，
                    # 因此这里读到的统计行只可能属于本题，不会把上一题的数字算进来。
                    tail = ServerLogTail(self._server_log_path(context))
                    tail.begin()
                    timing: ServerTiming | None = None
                    timing_seen_at: float | None = None

                    def heartbeat(
                        paper_index: int = paper_index,
                        question_index: int = question_index,
                        tail: ServerLogTail = tail,
                    ) -> None:
                        """把本题当前的日志统计变成一帧进度（由 ChatCompleter 每 10 s 调一次）。"""
                        nonlocal timing, timing_seen_at
                        for line in tail.new_lines():
                            found = self._timing_parser.parse(line)
                            if found is not None:
                                timing = found
                                timing_seen_at = time.perf_counter()
                        age = None if timing_seen_at is None else time.perf_counter() - timing_seen_at
                        self._report_alive(
                            context,
                            paper_index,
                            len(plan.papers),
                            question_index,
                            len(questions),
                            started,
                            timing=timing,
                            timing_age=age,
                        )

                    try:
                        completion = completer.complete(
                            question.prompt,
                            n_predict=plan.exam_n_predict,
                            temperature=plan.exam_temperature,
                            seed=plan.exam_seed,
                            timeout_s=self._config.timeouts.exam,
                            cancel_check=context.cancel_check,
                            on_heartbeat=heartbeat,
                        )
                    except CompletionCancelled:
                        outcomes.append(self._outcome(server.base_url, cancelled=True))
                        paper_failures.append(PaperFailure(paper.name, f"{question.qid}: 用户取消"))
                        failed = True
                        break
                    except CompletionTimeout as exc:
                        outcomes.append(self._outcome(server.base_url, timed_out=True))
                        paper_failures.append(PaperFailure(paper.name, f"{question.qid}: 执行超时（{exc}）"))
                        failed = True
                        break
                    except CompletionError as exc:
                        outcomes.append(self._outcome(server.base_url, stderr=str(exc)))
                        paper_failures.append(PaperFailure(paper.name, f"{question.qid}: {exc}"))
                        failed = True
                        break
                    # 上下文被 thinking 打满时，llama-server 已经把全部 token 记进
                    # reasoning_content，而 content 是空串。按用户口径：不改变正常答卷，
                    # 但把这段已经生成的内容作为该题正文记下来，然后继续下一题（不判失败）。
                    answer_text = completion.text if completion.text.strip() else completion.reasoning
                    self._save_question_evidence(context, paper.name, question, completion)
                    outcomes.append(
                        self._outcome(server.base_url, stdout=answer_text, wall_seconds=completion.wall_seconds)
                    )
                    # 块首用**中性标签**而不是题号标题：逐题后"答到哪一题"不再由文本核对决定；
                    # 若注入 `### K1｜…`，ADR-0022 的题号核对会退化成核对"我们自己写的字"（假精确）。
                    label = question.heading.lstrip("#").strip()
                    blocks.append(f"#### 第 {question_index + 1} 题 · {label}\n\n{answer_text}")
                    # 覆盖度仍按**最终答案** `content` 算：reasoning 兜底只是把已生成文本留证，
                    # 不能把"没写完的思考"伪装成答完（ADR-0025 决策 4 的反假精确口径）。
                    if completion.text.strip():
                        answered_qids.append(question.qid)
                    else:
                        missing_qids.append(question.qid)
                    paper_wall += completion.wall_seconds
                    prompt_tokens += completion.prompt_n
                    generated_tokens += completion.predicted_n
                    if completion.prompt_ts > 0:
                        prompt_seconds += completion.prompt_n / completion.prompt_ts
                    if completion.generation_ts > 0:
                        generation_seconds += completion.predicted_n / completion.generation_ts
                if failed:
                    break
                # 整卷吞吐是逐题 token 与耗时之和的比值（不是"一次测量的 t/s"，ADR-0025 决策 5）。
                sheet = AnswerSheet(
                    paper_name=paper.name,
                    model_name=context.model.display_name,
                    text="\n\n".join(blocks),
                    prompt_ts=(prompt_tokens / prompt_seconds) if prompt_seconds > 0 else 0.0,
                    generation_ts=(generated_tokens / generation_seconds) if generation_seconds > 0 else 0.0,
                    wall_seconds=paper_wall,
                )
                sheets.append(sheet)
                # 逐题后完整性 = "产出了非空回答的题数"，不再依赖答卷文本里是否出现题号：
                # 题号是我们自己排版的，文本核对会退化成核对注入字。ADR-0022 的文本口径
                # 对逐题答卷不适用（answers.txt 会显示"无法判定"）——见 ADR-0025。
                coverage = Coverage(
                    expected=len(questions),
                    found=len(answered_qids),
                    missing=tuple(missing_qids),
                    basis="逐题应答",
                )
                coverages.append((paper.name, coverage))
                answers_dir = self._answers_dir(context)
                if answers_dir is not None:
                    paths.append(self._writer.write(sheet, answers_dir, note=coverage.summary()))
        finally:
            server.stop()

        if coverages:
            self._report_coverage(context, coverages)
        return TrialExecution(
            outcomes=tuple(outcomes),
            sheets=tuple(sheets),
            answer_paths=tuple(paths),
            paper_failures=tuple(paper_failures),
        )

    def _server_log_path(self, context: TrialContext):
        """返回本轮 server 日志的落盘路径（无产物目录时返回 None）。

        前置条件: 无。
        后置条件: 有产物目录时返回 runs/<run_id>/server.log；否则返回 None。
        禁止: 在无产物目录时编造路径——留证是尽力而为，不能因此让测量失败（ADR-0005）。
        异常: 无。
        """
        if self._artifacts is None:
            return None
        return self._artifacts.run_dir(context.run_id) / "server.log"

    def _save_question_evidence(
        self, context: TrialContext, paper_name: str, question: Question, completion: Completion
    ) -> None:
        """把一题的 prompt 与 server 响应落成一份原始证据 JSON。

        前置条件: completion 来自该题的那次请求。
        后置条件: 有产物目录时写出 `q-<卷>-<题号>.json`；无产物目录时是空操作。
                  除最终正文 `content` 外，另记 `reasoning`（思考链原始文本）与 `recorded_from`
                  （答卷块实际取了哪一边：`content` / `reasoning_content` / `none`），
                  这样上下文打满、正文为空时仍能回溯模型到底生成了什么。
        禁止: 把逐题证据塞进 TrialRecord——记录 schema 不变是 ADR-0025 的既定边界。
        异常: OSError —— 目录不可创建或文件不可写（由 run() 统一转成失败记录）。
        """
        if self._artifacts is None:
            return
        if completion.text.strip():
            recorded_from = "content"
        elif completion.reasoning.strip():
            recorded_from = "reasoning_content"
        else:
            recorded_from = "none"
        payload = {
            "paper": paper_name,
            "qid": question.qid,
            "prompt": question.prompt,
            "content": completion.text,
            "reasoning": completion.reasoning,
            "recorded_from": recorded_from,
            "stop_reason": completion.stop_reason,
            "prompt_n": completion.prompt_n,
            "predicted_n": completion.predicted_n,
            "prompt_ts": completion.prompt_ts,
            "generation_ts": completion.generation_ts,
            "wall_seconds": completion.wall_seconds,
        }
        self._artifacts.save(
            context.run_id,
            f"q-{paper_name}-{question.qid}.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _outcome(
        self,
        base_url: str,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        wall_seconds: float = 0.0,
        cancelled: bool = False,
        timed_out: bool = False,
    ) -> ProcessOutcome:
        """为"一次逐题请求"合成一条 ProcessOutcome（供基类的失败判定复用）。

        前置条件: base_url 为本次 server 的根地址。
        后置条件: 返回带 command/returncode/输出与取消/超时标记的 Outcome。
        禁止: 把它当成子进程的证据——command 只是定位用的端点串，真正的物证是逐题 JSON 与 server.log。
        异常: 无。
        """
        return ProcessOutcome(
            command=(f"{base_url}/v1/chat/completions",),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            wall_seconds=wall_seconds,
            cancelled=cancelled,
            timed_out=timed_out,
        )

    def _report_coverage(self, context: TrialContext, coverages: list[tuple[str, "Coverage"]]) -> None:
        """上报每份答卷的题目覆盖度（收尾事实）。

        前置条件: coverages 非空，元素为 (试卷名, Coverage)。
        后置条件: 通过 sink 发出一帧进度，形如 `答卷覆盖 paper_1 30/30 · paper_2 无法判定`。
        禁止: 因覆盖不足改写 ok 或 paper_failures——覆盖度是**答卷内容**的属性，不是执行成败；
              把它当失败会重演"用参数上限冒充完成条件"的老错（ADR-0022）。
        异常: 无。
        """
        detail = " · ".join(f"{name} {coverage.short()}" for name, coverage in coverages)
        context.sink.on_progress(self.kind, None, f"答卷覆盖 {detail}")

    def _report_alive(
        self,
        context: TrialContext,
        paper_index: int,
        paper_total: int,
        question_index: int,
        question_total: int,
        started: float,
        *,
        timing: ServerTiming | None = None,
        timing_age: float | None = None,
    ) -> None:
        """上报一次 EXAM 存活信号（进度不可量化，只报事实）。

        前置条件: paper_index/question_index 从 0 起；paper_total/question_total 为正；started 为 perf_counter；
                   timing/timing_age 为本题当前的 server 日志统计与"该统计距今多少秒"（尚无统计时都是 None）。
        后置条件: 通过 sink 发出一帧进度（fraction=None，表示"无法量化"）。题开始那一帧只有"已用"；
                   题内出现过统计行后，每帧额外带"本题 N tok · R t/s · 日志 Ns 前"。
        禁止: 编造题内完成比例——server 是非流式响应，题内没有可靠进度源，猜一个百分比会让进度条说谎；
              也禁止把"日志 Ns 前"读成结论——它只陈述"日志里 N 秒没有新的统计行"，是不是真的卡住由人判断。
        异常: 无。
        """
        elapsed = time.perf_counter() - started
        message = (
            f"卷 {paper_index + 1}/{paper_total} · 题 {question_index + 1}/{question_total} · 已用 {elapsed:.0f}s"
        )
        if timing is not None:
            message += f" · 本题 {timing.n_gen} tok · {timing.tokens_per_second:.1f} t/s"
            if timing_age is not None:
                message += f" · 日志 {timing_age:.0f}s 前"
        context.sink.on_progress(self.kind, None, message)

    def _build_record(
        self,
        context: TrialContext,
        execution: TrialExecution,
        started_at: str,
        wall_seconds: float,
    ) -> ExamRecord:
        """把逐卷落盘时拼好的答卷汇总成记录。

        前置条件: execution 的 paper_failures 为空（有失败时 run() 走失败路径）。
        后置条件: 返回 ok=True 的 ExamRecord，sheets/answer_paths 与磁盘上的答卷一一对应；
                   load 为本次实际采样参数（ADR-0018）。
        禁止: 在这里重新解析一遍输出——那会让"落盘的答卷"与"记录里的答卷"有两条来源（ADR-0021）。
        异常: 无。
        """
        plan = context.plan
        return ExamRecord(
            **self._base_record_kwargs(context, started_at, wall_seconds, True, ""),
            sheets=list(execution.sheets),
            answer_paths=list(execution.answer_paths),
            load=ExamLoad(
                n_predict=plan.exam_n_predict,
                n_ctx=plan.exam_n_ctx,
                temperature=plan.exam_temperature,
                seed=plan.exam_seed,
            ),
            paper_failures=list(execution.paper_failures),
        )

    def _new_record(
        self,
        context: TrialContext,
        execution: TrialExecution | None,
        started_at: str,
        wall_seconds: float,
        *,
        ok: bool,
        error: str,
    ) -> ExamRecord:
        """构造 ExamRecord，并把 execution 里**已落盘**的答卷与逐卷失败照实带上。

        前置条件: execution 可为 None（server 没起来，此时没有任何部分结果）。
        后置条件: 返回 ok 与 error 如实、但 sheets/answer_paths 可能非空的记录（部分成功）。
        禁止: 因为有失败就把 sheets 清空——那些答卷文件已经在磁盘上，记录里没有它就成了撒谎（ADR-0021）。
        异常: 无。
        """
        plan = context.plan
        sheets = list(execution.sheets) if execution is not None else []
        paths = list(execution.answer_paths) if execution is not None else []
        failures = list(execution.paper_failures) if execution is not None else []
        return ExamRecord(
            **self._base_record_kwargs(context, started_at, wall_seconds, ok, error),
            sheets=sheets,
            answer_paths=paths,
            load=ExamLoad(
                n_predict=plan.exam_n_predict,
                n_ctx=plan.exam_n_ctx,
                temperature=plan.exam_temperature,
                seed=plan.exam_seed,
            ),
            paper_failures=failures,
        )

    def _answers_dir(self, context: TrialContext):
        """返回本次 Run 的答卷目录（无产物目录时返回 None）。"""
        if self._artifacts is None:
            return None
        return self._artifacts.run_dir(context.run_id) / "answers"


def build_runners(
    config: AppConfig,
    process: SubprocessRunner,
    artifacts: RunArtifacts | None = None,
) -> dict[TestKind, TrialRunner]:
    """装配三类 Trial 的 Runner 表。

    前置条件: config 里的 llama_bin_dir 已就绪。
    后置条件: 返回可直接交给 ArenaSession 的 {TestKind: TrialRunner}，键覆盖 RUN_ORDER 全部种类。
    禁止: 在装配处做任何测试逻辑判断——这里只是把对象接起来。
    异常: 无。
    """
    writer = AnswerSheetWriter()
    return {
        TestKind.BENCH: BenchRunner(config, process, artifacts),
        TestKind.PERPLEXITY: PerplexityRunner(config, process, artifacts),
        TestKind.EXAM: ExamRunner(config, process, artifacts, writer),
    }
