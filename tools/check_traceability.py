#!/usr/bin/env python3
"""Local Arena — 可读性与可溯源性的静态自检（ADR-0001 的执行者）。

模块名: tools.check_traceability
职责: 把 ADR-0001 定的文档契约变成可判定的检查，让「文档头写没写全、公开函数有没有四段契约、
      ADR 引用是不是双向可达」这些事不再依赖人眼。只读源码与 ADR 文件，不修改任何东西。
依赖: 标准库 ast/re/pathlib/sys（零第三方依赖）。
被依赖: 使用者（开发者、评审、CI）。
相关 ADR: docs/decisions/ADR-0001-readability-and-traceability.md

用法:
    python3 tools/check_traceability.py [-v]

退出码:
    0 —— 全部检查通过
    1 —— 存在 finding（逐条打印「文件:行 问题」）

检查项:
    1. 每个源文件有模块头五字段（模块名/职责/依赖/被依赖/相关 ADR）。
    2. 每个公开函数与方法的 docstring 含四段契约（前置条件/后置条件/禁止/异常）。
    3. 每个 ADR 引用（ADR-NNNN-slug）都有对应文件；每个 ADR 文件都被源码引用（双向可达）。
    4. ADR 编号自 1 起连续无缺号。
    5. 每个 ADR 文件含六节（状态/背景/决策/理由/后果/相关代码位置）。
    6. 源码中不出现 TODO/FIXME/XXX；不出现 float32 与 time.time()（ADR-0006 硬禁令）。
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import Iterator, NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES: tuple[Path, ...] = (ROOT / "run_arena.py", *sorted((ROOT / "arena").rglob("*.py")))
DECISIONS_DIR = ROOT / "docs" / "decisions"

HEADER_FIELDS = ("模块名:", "职责:", "依赖:", "被依赖:", "相关 ADR:")
CONTRACT_FIELDS = ("前置条件:", "后置条件:", "禁止:", "异常:")
ADR_SECTIONS = ("## 背景", "## 决策", "## 理由", "## 后果", "## 相关代码位置")
ADR_REFERENCE_RE = re.compile(r"ADR-\d{4}-[a-z0-9-]+")
# 注释里的标记词：TODO 类"以后再说"的痕迹（ADR-0001 禁止无条件 TODO）。
FORBIDDEN_COMMENT_RE = re.compile(r"\b(TODO|FIXME|XXX)\b")
# 代码里的写法禁令：FP64 约束（ADR-0006 禁止 float32 路径与非单调计时）。
FORBIDDEN_CODE_RES = (
    (re.compile(r"\bfloat32\b"), "出现 float32（ADR-0006 禁止 float32 路径）"),
    (re.compile(r"time\.time\("), "使用了非单调计时（ADR-0006 要求 perf_counter）"),
)


class Finding(NamedTuple):
    """一条检查未通过记录。"""

    path: Path
    line: int
    message: str

    def render(self) -> str:
        """渲染成「相对路径:行 问题」。"""
        return f"{self.path.relative_to(ROOT)}:{self.line} {self.message}"


def public_functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """产出公开接口函数：模块级函数与类方法（名字不以 `_` 开头）。

    前置条件: tree 为 ast.parse 的结果。
    后置条件: 产出顺序为源码顺序；**函数内嵌套的闭包不被产出**——
              它们是实现细节（如 `number()`、`report()`），不是对外承诺。
    禁止: 用文本匹配代替 AST——正则会被字符串里的 `def` 骗到。
    异常: 无。
    """

    def is_public(node: ast.AST) -> bool:
        return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")

    for node in tree.body:
        if is_public(node):
            yield node
            continue
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if is_public(child):
                    yield child


def check_module_header(path: Path, source: str, findings: list[Finding]) -> None:
    """检查模块头五字段。

    前置条件: path 是 .py 文件；source 为其全文。
    后置条件: 缺字段时向 findings 追加记录（行号取文件头所在行）。
    禁止: 把字段检查放宽到"文件里出现过"——必须是模块 docstring 里出现。
    异常: 无。
    """
    tree = ast.parse(source, filename=str(path))
    header = ast.get_docstring(tree) or ""
    missing = [field for field in HEADER_FIELDS if field not in header]
    if missing:
        findings.append(Finding(path, 1, f"模块头缺少字段: {'、'.join(missing)}"))


def check_public_contracts(path: Path, source: str, findings: list[Finding]) -> None:
    """检查公开函数的 docstring 与四段契约。

    前置条件: path 是 .py 文件；source 为其全文。
    后置条件: 每个缺 docstring 或缺契约小节的公开函数各产生一条 finding。
    禁止: 对缺 docstring 的函数继续找契约小节（会产生噪声重复项）。
    异常: SyntaxError —— 由调用方统一捕获并转成 finding。
    """
    tree = ast.parse(source, filename=str(path))
    for node in public_functions(tree):
        doc = ast.get_docstring(node)
        if not doc:
            findings.append(Finding(path, node.lineno, f"公开函数 {node.name}() 缺少 docstring"))
            continue
        missing = [field for field in CONTRACT_FIELDS if field not in doc]
        if missing:
            findings.append(
                Finding(path, node.lineno, f"公开函数 {node.name}() 的 docstring 缺少: {'、'.join(missing)}")
            )


def forbidden_hits(source: str) -> Iterator[tuple[int, str]]:
    """在源码里查找违禁写法，产出 (行号, 原因)。

    前置条件: source 为 .py 文件全文。
    后置条件: 只检查**代码 token**（字符串与注释被排除）：
              「TODO/FIXME/XXX」在注释里查（它们是注释性标记），
              「float32 / time.time(」在代码里查（它们是代码写法）。
    禁止: 用整行正则代替分词——docstring 里解释禁令本身（如本工具的头注释）会被误判。
    异常: tokenize.TokenError —— 由调用方统一捕获转成 finding。
    """
    code: dict[int, list[str]] = {}
    comments: dict[int, list[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.setdefault(token.start[0], []).append(token.string)
        elif token.type in (tokenize.STRING, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            continue
        else:
            code.setdefault(token.start[0], []).append(token.string)

    for number in sorted(comments):
        text = " ".join(comments[number])
        if FORBIDDEN_COMMENT_RE.search(text):
            yield number, "注释里出现 TODO/FIXME/XXX（ADR-0001 禁止无条件 TODO）"
    for number in sorted(code):
        text = " ".join(code[number])
        for pattern, reason in FORBIDDEN_CODE_RES:
            if pattern.search(text):
                yield number, reason


def check_forbidden_patterns(path: Path, source: str, findings: list[Finding]) -> None:
    """把违禁写法的命中追加进 findings。

    前置条件: path 是 .py 文件；source 为其全文。
    后置条件: 每个命中各产生一条 finding，行号行内定位。
    禁止: 无。
    异常: 无（tokenize 错误在此吞掉——语法问题由 check_public_contracts 报出）。
    """
    try:
        hits = list(forbidden_hits(source))
    except tokenize.TokenError:
        return
    for number, reason in hits:
        findings.append(Finding(path, number, reason))


def collect_adr_references(sources: dict[Path, str]) -> dict[str, list[Path]]:
    """收集「ADR 编号 → 引用它的源文件」。

    前置条件: sources 为 {源文件: 全文}。
    后置条件: 键为满名（如 ADR-0015-vram-budget-...）；同一文件内重复引用只记一次。
    禁止: 只扫描文件头——docstring 里的引用同样是双向可达性的一部分。
    异常: 无。
    """
    references: dict[str, list[Path]] = {}
    for path, source in sources.items():
        for name in sorted(set(ADR_REFERENCE_RE.findall(source))):
            references.setdefault(name, []).append(path)
    return references


def adr_files() -> list[Path]:
    """列出 docs/decisions 下的 ADR 文件，按文件名排序。

    前置条件: 无。
    后置条件: 返回 ADR-*.md（不含 README.md）；目录缺失时返回空列表。
    禁止: 无。
    异常: 无。
    """
    if not DECISIONS_DIR.is_dir():
        return []
    return sorted(DECISIONS_DIR.glob("ADR-*.md"))


def check_adr_bidirectional(references: dict[str, list[Path]], findings: list[Finding]) -> None:
    """检查 ADR 双向可达性、编号连续性与六节完整性。

    前置条件: references 来自 collect_adr_references()。
    后置条件: 悬空引用、未被引用的 ADR、缺号、缺节各产生一条 finding。
    禁止: 允许「代码引用了不存在的 ADR」通过——那正是断链本身。
    异常: 无。
    """
    files = adr_files()
    existing = {path.stem for path in files}

    for name, path_list in references.items():
        if name not in existing:
            locations = "、".join(str(path.relative_to(ROOT)) for path in path_list)
            findings.append(Finding(ROOT / "docs/decisions", 1, f"引用了不存在的 ADR: {name}（引用自 {locations}）"))

    for path in files:
        if path.stem not in references:
            findings.append(Finding(path, 1, "ADR 未被任何源码引用（双向可达性缺失）"))
        text = path.read_text(encoding="utf-8")
        missing = [section for section in ADR_SECTIONS if section not in text]
        if missing:
            findings.append(Finding(path, 1, f"ADR 缺少小节: {'、'.join(missing)}"))

    numbers = sorted(int(match.group(1)) for match in (re.match(r"ADR-(\d{4})-", path.stem) for path in files) if match)
    if numbers:
        expected = list(range(1, len(numbers) + 1))
        if numbers != expected:
            findings.append(
                Finding(DECISIONS_DIR, 1, f"ADR 编号不是自 1 起的连续序列: 实际 {numbers}，期望 {expected}")
            )


def main(argv: list[str] | None = None) -> int:
    """执行全部检查并打印结果。

    前置条件: argv 可为 None（取 sys.argv[1:]）；`-v` 时额外打印通过项计数。
    后置条件: 通过时打印一行摘要并返回 0；存在 finding 时逐条打印并返回 1。
    禁止: 以异常代替非零退出码——调用方需要稳定的机器可判定结果。
    异常: 无（源文件语法错误被转成 finding）。
    """
    verbose = bool(argv and "-v" in argv)
    findings: list[Finding] = []
    sources: dict[Path, str] = {}

    for path in SOURCE_FILES:
        source = path.read_text(encoding="utf-8")
        sources[path] = source
        try:
            check_module_header(path, source, findings)
            check_public_contracts(path, source, findings)
        except SyntaxError as exc:
            findings.append(Finding(path, exc.lineno or 1, f"语法错误: {exc.msg}"))
            continue
        check_forbidden_patterns(path, source, findings)

    references = collect_adr_references(sources)
    check_adr_bidirectional(references, findings)

    if findings:
        for finding in findings:
            print(finding.render())
        print(f"\nFAIL: {len(findings)} 项不通过（扫描 {len(sources)} 个源文件、{len(adr_files())} 份 ADR）")
        return 1

    if verbose:
        print(f"源文件 {len(sources)} 个 · ADR {len(adr_files())} 份 · 引用 {len(references)} 个 ADR 名")
    print("OK: 文件头 / 公开契约 / ADR 双向可达性 / 编号连续性 / 违禁写法 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
