"""Local Arena — 输出解析器（脆弱性隔离层）。

模块名: arena.parsers
职责: 把 llama.cpp 各工具的原始输出文本翻译成领域数值（含 `llama-server` 日志的逐题统计行）；
      本模块是唯一允许出现"正则/字符串切割"的地方，UI 与编排层永远看不到原始文本。
依赖: 标准库 dataclasses/json/re；arena.domain、arena.vocabulary。
被依赖: arena.runners.*。
相关 ADR: docs/decisions/ADR-0004-parsers-follow-stream-semantics.md、
          docs/decisions/ADR-0026-in-question-progress-from-server-log.md
实测依据（证据见 docs/tracer/）:
  - bench.json/stderr：llama-bench 的 stdout 是纯 JSON 数组；日志只走 stderr。
  - ppl.out/ppl.err：进展串 `[1]8.8652,[2]13.0436,` 在 stdout；
    `Final estimate: PPL = x +/- y` 在 stderr，且带 `秒.毫秒.微秒 I ` 前缀。
  - cli.out/cli2.err：llama-cli 的 stdout 是 TUI 文字记录，stderr 恒为 0 字节。
  - runs/2026-10-19T09-43-52_Qwen3-30B-A3B/server.log：llama-server 每约 3 秒写一行
    `slot print_timing: id 0 | task N | n_gen = X, tg = Y t/s, tg_3s = Z t/s`（题内唯一的进度源）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from arena.domain import BenchCase, BenchResult

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ParseError(RuntimeError):
    """解析失败的共同基类；消息必须包含"期望什么、看到什么"以便事后从原始流重建。"""


class BenchParseError(ParseError):
    """llama-bench 的 stdout 不是合法 JSON 记录数组。"""


class PerplexityParseError(ParseError):
    """llama-perplexity 的日志里找不到最终困惑度或运行参数。"""


class TranscriptParseError(ParseError):
    """llama-cli 的记录里找不到试卷回显或性能页脚。"""


# ---------------------------------------------------------------------------
# BenchJsonParser
# ---------------------------------------------------------------------------


class BenchJsonParser:
    """解析 llama-bench -o json 的 stdout。"""

    def parse(self, stdout: str) -> list[BenchResult]:
        """把 stdout 解析为 BenchResult 列表。

        前置条件: stdout 来自 `llama-bench ... -o json`。
        后置条件: 每条记录对应一次 pp/tg 测量（-p 与 -n 会各产生一条，见 tracer F7）；
                   数值字段为 Python float（FP64）。
        禁止: 从 stderr 取数——那里只有 CUDA 初始化噪声与被调工具的日志。
        异常: BenchParseError —— 非法 JSON 或不是数组。
        """
        text = stdout.strip()
        if not text:
            raise BenchParseError("llama-bench 的 stdout 为空，预期是 JSON 数组")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BenchParseError(f"llama-bench stdout 不是合法 JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise BenchParseError(f"预期 JSON 数组，实际是 {type(payload).__name__}")
        if not payload:
            raise BenchParseError("llama-bench 返回了空数组：没有任何测量结果")
        return [self._to_result(item) for item in payload]

    @staticmethod
    def _to_result(item: dict) -> BenchResult:
        """把一条 JSON 记录映射为 BenchResult。"""
        try:
            return BenchResult(
                case=BenchCase(
                    n_prompt=int(item["n_prompt"]),
                    n_gen=int(item["n_gen"]),
                    n_depth=int(item.get("n_depth", 0)),
                ),
                avg_ts=float(item["avg_ts"]),
                stddev_ts=float(item.get("stddev_ts", 0.0)),
                n_gpu_layers=int(item.get("n_gpu_layers", 0)),
                # 键名是复数 backends（tracer 实测），旧版本文档写作 backend，两者都容忍。
                backend=str(item.get("backends") or item.get("backend") or ""),
                build_commit=str(item.get("build_commit", "")),
                test_time=str(item.get("test_time", "")),
            )
        except KeyError as exc:
            raise BenchParseError(f"llama-bench 记录缺少字段 {exc}") from exc


# ---------------------------------------------------------------------------
# PerplexityLogParser
# ---------------------------------------------------------------------------


class PerplexityLogParser:
    """解析 llama-perplexity 的 stdout（进度）与 stderr（最终值）。"""

    # 行内搜索而非整行匹配：stderr 行的真实样子是
    # `0.03.094.030 I Final estimate: PPL = 13.0436 +/- 2.16592`，
    # 且它可能紧贴在上一行的 `ETA ` 之后（同行拼接，见 tracer F3）。
    _FINAL_RE = re.compile(r"Final estimate:\s*PPL\s*=\s*([0-9.]+)\s*\+/-\s*([0-9.]+)")
    _SETUP_RE = re.compile(r"over\s+(\d+)\s+chunks,\s*n_ctx\s*=\s*(\d+)")
    # 进度串形如 `[1]8.8652,[2]13.0436,`：方括号里的序号即已完成 chunk 数。
    _PROGRESS_RE = re.compile(r"\[(\d+)\]")

    def parse_final(self, stderr: str) -> tuple[float, float]:
        """取出最终困惑度及其标准误。

        前置条件: stderr 来自正常跑完的 llama-perplexity。
        后置条件: 返回 (ppl, ppl_stderr)；两者均为 Python float（FP64）。
        禁止: 用进度串里的中间值当作最终值——那只是逐 chunk 的累计估计。
        异常: PerplexityParseError —— 找不到 `Final estimate: PPL = ...`。
        """
        matches = self._FINAL_RE.findall(stderr)
        if not matches:
            raise PerplexityParseError("stderr 中找不到 'Final estimate: PPL = ...'")
        ppl, stderr_value = matches[-1]
        return float(ppl), float(stderr_value)

    def parse_setup(self, stderr: str) -> tuple[int, int]:
        """取出实际使用的 chunk 数与上下文长度。

        前置条件: stderr 来自 llama-perplexity。
        后置条件: 返回 (n_chunks, n_ctx)。
        禁止: 把 TestPlan 里的请求值当作实际值——实际值以工具自报为准。
        异常: PerplexityParseError —— 找不到 `over N chunks, n_ctx=M`。
        """
        match = self._SETUP_RE.search(stderr)
        if match is None:
            raise PerplexityParseError("stderr 中找不到 'over N chunks, n_ctx=M'")
        return int(match.group(1)), int(match.group(2))

    def parse_progress(self, stdout: str, total_chunks: int) -> float | None:
        """从进度串推断完成比例。

        前置条件: total_chunks > 0；stdout 可为任意时刻的累积文本。
        后置条件: 返回 0.0~1.0 的比例；无法判定时返回 None（不猜）。
        禁止: 在 total_chunks <= 0 时做除法。
        异常: ValueError —— total_chunks <= 0。
        """
        if total_chunks <= 0:
            raise ValueError("total_chunks 必须为正数")
        indexes = [int(found) for found in self._PROGRESS_RE.findall(stdout)]
        if not indexes:
            return None
        return min(1.0, max(indexes) / total_chunks)


# ---------------------------------------------------------------------------
# CliTranscriptParser
# ---------------------------------------------------------------------------


class CliTranscriptParser:
    """从 llama-cli 的 TUI 文字记录里取出答案与吞吐。"""

    _FOOTER_RE = re.compile(r"\[ Prompt:\s*([0-9.]+)\s*t/s\s*\|\s*Generation:\s*([0-9.]+)\s*t/s\s*\]")
    # llama.cpp/tools/cli/cli-ui.h:159-165 的 echo() 有两条分支：
    #   buffer.size() > 500 → console::log("\n> %s ... (truncated)\n", buffer.substr(0, 500))
    #   否则                → console::log("\n> %s\n", buffer)
    # 500 是**字节**上限，所以长中文试卷必然走截断分支（tracer F12 修正了 F6）。
    _TRUNCATED_MARKER = " ... (truncated)\n"
    _NOISE_LINES = ("Exiting...",)

    def extract_answer(self, stdout: str, prompt: str) -> str:
        """从记录中切出模型答案。

        前置条件: stdout 来自 `llama-cli -st -p <prompt>`。
        后置条件: 返回答案正文（已去掉 banner、命令帮助、回显、性能页脚与 "Exiting..."）；
                   模型没产出内容时返回空串。
        禁止: 依赖行号或 banner 文本——二者都随版本漂移；也禁止在无法定位起点时返回整段记录。
        异常: TranscriptParseError —— 记录里既没有截断标记也找不到完整回显。
        """
        body = self._cut_footer(stdout)
        truncated_at = body.find(self._TRUNCATED_MARKER)
        if truncated_at >= 0:
            # 长 prompt 路径：回显被工具自己截断，标记之后就是答案。
            return self._strip_noise(body[truncated_at + len(self._TRUNCATED_MARKER) :])

        echoed = "\n> " + prompt + "\n"
        index = body.find(echoed)
        if index < 0:
            raise TranscriptParseError(
                "记录中既没有 ' ... (truncated)' 标记，也没有完整回显；无法确定答案起点，拒绝猜测"
            )
        return self._strip_noise(body[index + len(echoed) :])

    def parse_throughput(self, stdout: str) -> tuple[float, float]:
        """取出页脚自报的 prompt / generation 吞吐。

        前置条件: stdout 来自 llama-cli。
        后置条件: 返回 (prompt_ts, generation_ts)，单位 t/s，均为 FP64。
        禁止: 用 token 数反推吞吐——llama-cli 不报告 token 数（ADR-0011）。
        异常: TranscriptParseError —— 找不到页脚。
        """
        match = self._FOOTER_RE.search(stdout)
        if match is None:
            raise TranscriptParseError("记录中找不到 '[ Prompt: x t/s | Generation: y t/s ]' 页脚")
        return float(match.group(1)), float(match.group(2))

    @classmethod
    def _cut_footer(cls, stdout: str) -> str:
        """截断性能页脚及其后的内容（`Exiting...` 在页脚之后）。"""
        match = cls._FOOTER_RE.search(stdout)
        return stdout[: match.start()] if match else stdout

    @classmethod
    def _strip_noise(cls, answer: str) -> str:
        """去掉首尾空行与残留的 TUI 噪声行。"""
        lines = answer.splitlines()
        while lines and lines[0].strip() == "":
            lines.pop(0)
        while lines and (lines[-1].strip() == "" or lines[-1].strip() in cls._NOISE_LINES):
            lines.pop()
        return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# ServerTimingParser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerTiming:
    """`llama-server` 的一次生成统计（一行 `slot print_timing`）。

    `n_gen` 与 `tokens_per_second` 都是 server **自己**报的累计事实：前者是这次 task 已生成的
    token 数，后者是整段的平均吞吐（`tg_3s` 是近 3 秒短窗，滑动噪声大，刻意不取）。
    """

    task: int
    n_gen: int
    tokens_per_second: float


class ServerTimingParser:
    """解析 `llama-server` 日志里的 `slot print_timing` 行（题内进度的唯一来源）。

    行样子（真实 server.log，`=` 两侧空格数不固定）：
    `18.01.228.040 I slot print_timing: id  0 | task 27891 | n_gen =   3071, tg =  29.07 t/s, tg_3s =  28.53 t/s`

    行首时间戳是 `分.秒.毫秒.微秒`，本解析器**不解释**它：一切时间都取调用方的
    `time.perf_counter()`（ADR-0006 禁止非单调计时）。
    """

    _TIMING_RE = re.compile(
        r"print_timing: id\s+\d+ \| task (\d+) \| n_gen =\s*(\d+), tg =\s*([0-9]+(?:\.[0-9]+)?) t/s"
    )

    def parse(self, line: str) -> ServerTiming | None:
        """从一行日志文本里取出生成统计。

        前置条件: line 为 server 日志的一行（允许带任意前后缀/前缀噪声）。
        后置条件: 命中 `print_timing` 时返回 ServerTiming；其他任何行返回 None（不猜、不抛）。
        禁止: 在 miss 时回退到"最后一行统计"或返回零值——那会把"还没有统计"伪造成"生成了 0 个 token"。
        异常: 无（正则已把数值限定为整数/小数，不存在转换失败路径）。
        """
        match = self._TIMING_RE.search(line)
        if match is None:
            return None
        return ServerTiming(
            task=int(match.group(1)),
            n_gen=int(match.group(2)),
            tokens_per_second=float(match.group(3)),
        )
