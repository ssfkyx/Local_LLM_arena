"""答卷覆盖度核对。

模块名: coverage
职责: 从试卷自身推断题目清单，再核对一份答卷答到了哪些题——只做"答到没有"的文本核对，
      不判断答案对错（ADR-0009）。
依赖: 标准库 re / dataclasses；arena.paper（题号语法的单一来源，同为纯文本函数）。
      不读盘、不加载模型，因此 EXAM 收尾与 tools/check_completion.py 都能直接调用。
被依赖: arena.runners（EXAM 收尾步骤）、arena.export（导出时重算覆盖度）、tools/check_completion.py。
相关 ADR: docs/decisions/ADR-0009-no-automatic-grading.md、
          docs/decisions/ADR-0022-answer-coverage-check.md、
          docs/decisions/ADR-0025-per-question-exam.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arena.paper import question_ids

# 答卷侧的题号引用：允许行首有 Markdown 标记与空白；`K1` 之后必须是词边界，
# 否则 `K10` 会被 `K1` 命中。（题号声明语法本身见 arena.paper.QUESTION_HEADING_RE。）
_ANSWER_ID_TEMPLATE = r"^[\s#>*\-]*{qid}\b"

# 逐题答卷的块首标签：EXAM 逐题作答时由 ExamRunner 写入（`#### 第 3 题 · R3｜约束排序`，ADR-0025）。
# 它是"逐题答卷"的机械标记——见到它就不能再用文本核对推断覆盖率（否则会误报），
# 因此它是 arena.coverage 与 arena.runners 之间的**格式契约**，改动需两侧同步。
PER_QUESTION_BLOCK_RE = re.compile(r"^#{0,4}[ \t]*第[ \t]*\d+[ \t]*题[ \t]*·", re.MULTILINE)


@dataclass(frozen=True)
class Coverage:
    """一份答卷的题目覆盖度。

    expected 为 None 表示**无法判定**（试卷里没有可识别的题号）——此时 found 也是 None，
    调用方必须把"无法判定"与"未覆盖"分开表达，不能把前者当成失败。
    """

    expected: int | None
    found: int | None
    missing: tuple[str, ...] = ()
    basis: str = ""
    note: str = ""

    @property
    def complete(self) -> bool | None:
        """覆盖是否完整。

        前置条件: 无。
        后置条件: 返回 True（全覆盖）/ False（明确有缺）/ None（无法判定；note 非空即属此档）。
        禁止: 把无法判定返回成 False——那会把"不知道"写成"没答完"。
        异常: 无。
        """
        if self.note or self.expected is None or self.found is None:
            return None
        return self.found >= self.expected

    def short(self) -> str:
        """一句话短标签，用于进度行（`30/30`、`1/30`、`无法判定`）。

        前置条件: 无。
        后置条件: 返回只含比例（或"无法判定"）的短文本——缺失题号细节不放这里，
                  进度行要能塞进 GUI 状态栏；细节见 summary() 与 tools/check_completion.py。
        禁止: 在此编造比例——所需数字只能来自 expected/found。
        异常: 无。
        """
        if self.complete is None:
            return "无法判定"
        return f"{self.found}/{self.expected}"

    def summary(self) -> str:
        """一句话完整说明，含判定依据，用于答卷文件头。

        前置条件: 无。
        后置条件: 返回形如 `覆盖 30/30（按试卷题号核对）`、
                  `覆盖 28/30（缺 R1、L5；按试卷题号核对）`、
                  `覆盖无法判定（答卷未引用任何题号）` 的文本。
        禁止: 省略判定依据或无法判定的原因——同一份答卷的核对方式不同，结论的可信度不同（ADR-0022）。
        异常: 无。
        """
        if self.note:
            return f"覆盖无法判定（{self.note}）"
        if self.expected is None or self.found is None:
            return "覆盖无法判定（试卷里没有可识别的题号）"
        if self.complete:
            return f"覆盖 {self.found}/{self.expected}（按{self.basis}核对）"
        detail = f"缺 {'、'.join(self.missing)}；" if self.missing else ""
        return f"覆盖 {self.found}/{self.expected}（{detail}按{self.basis}核对）"


def compare(paper_text: str, answer_text: str) -> Coverage:
    """核对一份答卷覆盖了试卷里的哪些题。

    前置条件: paper_text 为试卷原文；answer_text 为答卷正文（元数据头已由调用方剔除亦可）。
    后置条件: 返回 Coverage；**逐题答卷**、**试卷未声明题号**或**答卷一个题号都没引用**时返回"无法判定"
              （note 非空），否则按"答卷行首是否引用了该题号"逐题核对，并列出缺失题号。
    禁止: 把"答卷一个题号都没引用"直接判为 0/30——改版前的答卷（用旧版无题号试卷答的）
          正是这种形态，那是试卷版本问题，不是模型没答（ADR-0022）；也禁止反向假设
          "没引用题号就是全答了"——那是把不知道当成好。
    异常: 无。
    """
    if PER_QUESTION_BLOCK_RE.search(answer_text):
        # 逐题答卷：每题独立作答，完整性由执行过程决定（ADR-0025），文本核对不适用。
        # 若照旧核对，模型答案里偶然出现的行首题号会造出"覆盖 3/30"这类**误报**——
        # 那比没有核对更糟（ADR-0022 的理由）。因此这里必须落到"无法判定（不适用）"。
        return Coverage(
            expected=None, found=None, missing=(), basis="题号", note="逐题答卷（每题独立作答），文本核对不适用"
        )
    ids = question_ids(paper_text)
    if not ids:
        return Coverage(expected=None, found=None, missing=(), basis="题号", note="试卷里没有可识别的题号")
    found = tuple(
        qid
        for qid in ids
        if re.search(_ANSWER_ID_TEMPLATE.format(qid=re.escape(qid)), answer_text, flags=re.MULTILINE)
    )
    if not found:
        # 一个题号都没引用：既可能是没按题号作答的完整卷，也可能是改版前试卷的答卷，无法区分。
        return Coverage(
            expected=len(ids), found=0, missing=ids, basis="题号", note="答卷未引用任何题号"
        )
    missing = tuple(qid for qid in ids if qid not in set(found))
    return Coverage(expected=len(ids), found=len(found), missing=missing, basis="题号")
