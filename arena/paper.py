"""Local Arena — 试卷结构（逐题切分与前言归属）。

模块名: arena.paper
职责: 把一份试卷文本解析成**逐题**结构：题号、题块原文、所属节，以及该题应当携带的前言
      （文档级前言，如 `## 通用要求`；节内前言，如 F 节的 `### 共同材料`），并据此拼出逐题 prompt。
      纯文本函数：不读盘、不加载模型、不判分（ADR-0025）。
依赖: 标准库 dataclasses/re。
被依赖: arena.runners（EXAM 逐题作答）、arena.coverage（题号语法的单一来源）。
相关 ADR: docs/decisions/ADR-0025-per-question-exam.md、
          docs/decisions/ADR-0022-answer-coverage-check.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 题号声明：`### K1｜短标题`、`K1：说明`（Markdown 井号前缀可有可无）。
# 与 arena.coverage 的答卷侧核对共用同一套题号语法——这里是它的唯一来源。
QUESTION_HEADING_RE = re.compile(r"^#{0,4}[ \t]*([A-Z]\d{1,2})[ \t]*[｜|:：]", re.MULTILINE)

# 节标题：恰好两个井号（`## A. 知识问答`）。用 `(?!#)` 把 `### K1｜…` 这类题号标题排除在外。
SECTION_HEADING_RE = re.compile(r"^##(?!#)[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Question:
    """试卷里的一道题：题号、所属节、题块原文，以及拼好前言的完整 prompt。"""

    qid: str
    section: str
    heading: str
    text: str
    prompt: str


def question_ids(paper_text: str) -> tuple[str, ...]:
    """从试卷文本里提取题号清单（按出现顺序、去重）。

    前置条件: paper_text 是试卷原文（Markdown 或纯文本皆可）。
    后置条件: 返回试卷里声明的题号（如 K1..L5）；没有声明时返回空元组。
    禁止: 对提取结果做补全或猜测——试卷没写题号就是没写，返回空元组让调用方自己决定怎么办。
    异常: 无。
    """
    seen: dict[str, None] = {}
    for qid in QUESTION_HEADING_RE.findall(paper_text):
        seen.setdefault(qid, None)
    return tuple(seen)


def split_paper(paper_text: str, prompt_suffix: str) -> tuple[Question, ...]:
    """把试卷切成逐题结构，并为每题拼出完整 prompt。

    前置条件: paper_text 为试卷原文；prompt_suffix 为收尾作答指令（通常是 config 里的那一条）。
    后置条件: 返回按题号出现顺序排列的 Question；每题的 prompt = 文档级前言 + 节内前言 + 题块 +
               prompt_suffix（空前言不参与拼接）。题块边界取"下一题号"与"下一个 `## ` 节标题"两者的**较近者**
              （否则本节最后一题会吞掉下一节前言，见 ADR-0025 的 F35）。**无题号的 `## ` 节**整体视为
              文档级前言拼进每一题（如 `## 通用要求`）；**节内前言**只拼进该节的题（如 F 节的 `### 共同材料`）。
    禁止: 用"下一个 `###` 标题"当边界——`### 共同材料` 没有题号，会切错；也禁止在题块里夹带相邻节的前言。
    异常: ValueError —— 试卷里没有可识别的题号（逐题作答无从谈起，必须让调用方看见）。
    """
    matches = list(QUESTION_HEADING_RE.finditer(paper_text))
    if not matches:
        raise ValueError("试卷里没有可识别的题号（`### K1｜…`），无法逐题作答")

    sections = list(SECTION_HEADING_RE.finditer(paper_text))
    ranges: list[tuple[int, int, str]] = []
    for index, match in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(paper_text)
        ranges.append((match.start(), end, match.group(1).strip()))

    def section_of(position: int) -> tuple[int, int, str] | None:
        for start, end, name in ranges:
            if start < position < end:
                return start, end, name
        return None

    # 文档级前言：没有任何题号的 `## ` 节（含其标题行）按顺序拼起来。
    document_parts = [
        paper_text[start:end].strip("\n")
        for start, end, _ in ranges
        if not any(start < match.start() < end for match in matches)
    ]
    document_preamble = "\n\n".join(part for part in document_parts if part)

    questions: list[Question] = []
    for index, match in enumerate(matches):
        next_question = matches[index + 1].start() if index + 1 < len(matches) else len(paper_text)
        next_section = next((start for start, _, _ in ranges if start > match.start()), len(paper_text))
        block = paper_text[match.start():min(next_question, next_section)].strip("\n")

        owner = section_of(match.start())
        section_name = ""
        section_preamble = ""
        if owner is not None:
            owner_start, owner_end, section_name = owner
            first_question_here = next(
                (item.start() for item in matches if owner_start < item.start() < owner_end), None
            )
            preamble_end = first_question_here if first_question_here is not None else owner_end
            # 去掉 `## X. …` 标题行本身；A~E 节的前言因此退化为分隔线/空白，视作空。
            header_line_end = paper_text.index("\n", owner_start) + 1
            candidate = paper_text[header_line_end:preamble_end].strip("\n")
            section_preamble = "" if set(candidate) <= set("-\n \t") else candidate

        parts = [part for part in (document_preamble, section_preamble, block) if part]
        questions.append(
            Question(
                qid=match.group(1),
                section=section_name,
                heading=block.splitlines()[0] if block else "",
                text=block,
                prompt="\n\n".join(parts) + prompt_suffix,
            )
        )
    return tuple(questions)
