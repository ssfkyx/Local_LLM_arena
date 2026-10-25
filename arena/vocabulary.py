"""Local Arena — 统一语言（枚举与阶段名）。

模块名: arena.vocabulary
职责: 用代码固化 Ubiquitous Language 中会被跨模块引用的**名字**：Trial 的种类、阶段与负载档。
      只放名字，不放行为、不放 UI 文案、不放参数。
依赖: 标准库 enum。
被依赖: arena.config、arena.domain、arena.sink、arena.runners、arena.parsers、arena.session、
      arena.history、arena.ui.*、run_arena.py。
相关 ADR: docs/decisions/ADR-0003-runner-polymorphism-and-module-boundaries.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md
"""

from __future__ import annotations

from enum import Enum


class TestKind(str, Enum):
    """一次 Trial 的种类。

    继承 str 使得 JSONL 序列化、比较与调试输出都无需转换。
    """

    BENCH = "BENCH"
    PERPLEXITY = "PERPLEXITY"
    EXAM = "EXAM"


class Stage(str, Enum):
    """Trial 在生命周期中的阶段，是 ProgressSink 唯一对外汇报的状态维度。"""

    QUEUED = "QUEUED"
    LOADING = "LOADING"
    RUNNING = "RUNNING"
    PARSING = "PARSING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Run 内 Trial 的规范执行顺序：先量吞吐，再量语言建模质量，最后让模型答卷。
# 之所以写死顺序而不是让调用方排：Scoreboard 的三行顺序必须恒定，否则跨模型对比会错位。
RUN_ORDER: tuple[TestKind, ...] = (TestKind.BENCH, TestKind.PERPLEXITY, TestKind.EXAM)

# 终态集合：完成、失败、取消三者互斥，Session 依赖它判断是否继续下一个 Trial。
TERMINAL_STAGES: frozenset[Stage] = frozenset({Stage.DONE, Stage.FAILED, Stage.CANCELLED})

# 负载档名：一次 Run 用的那套负载参数由它唯一确定（参数本身住在 arena.config）。
# 只有这两个值——没有"第三种负载"，因为软件不提供调参数入口（ADR-0018）。
PROFILE_REAL = "real"
PROFILE_QUICK = "quick"

# 白名单：AppConfig 用它在构造期拒绝未知档名，从而保证 profile 是可信的对比键。
PROFILES: frozenset[str] = frozenset({PROFILE_REAL, PROFILE_QUICK})

# 唯一参与跨模型对比的档：满载真实档。quick 只用于自检，其数字不得进入对比（ADR-0018）。
OFFICIAL_PROFILE = PROFILE_REAL

# 评估协议名：它标记"这组数字是在哪套口径下测的"（负载参数 + 完成条件）。
# 改口径（例如把 PPL 从 8 chunk 改成全语料、把答卷从 -n 1024 改成 -n -1）必须同时改这个名字，
# 并归档重置机读历史——跨协议的数字不进同一张对比表（ADR-0021）。
# 它不是负载参数（参数在 arena.config），也不是档名（档名是同协议内的自检/正式之分）。
PROTOCOL_NAME = "full-corpus-ppl-uncapped-exam-v1"

# 协议标记文件的落地名：放在 runs 目录下，紧挨它不是产物的历史索引。
PROTOCOL_FILENAME = "PROTOCOL"

# 机读历史索引的文件名。它原先是入口文件里的局部常量，现按 ADR-0013 的单一来源原则放在这里——
# tools/check_completion.py 等工具需要它，而工具不该反向依赖入口（ADR-0022）。
HISTORY_FILENAME = "arena_history.jsonl"

# 一轮 Run 的答卷汇总文本名：落在该轮 run 目录下，紧挨 answers/ 目录（ADR-0023）。
# 它与 answers/ 里的逐卷文件是同一份正文的两种切法：目录版按卷分文件、这个文件按轮汇总，
# 用途是"跨系统/跨应用直接读"，因此是纯 .txt 而不是 .md（渲染见 arena.export）。
ANSWERS_TEXT_FILENAME = "answers.txt"
