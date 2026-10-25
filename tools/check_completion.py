#!/usr/bin/env python3
"""Local Arena — 答卷题目覆盖度核对（ADR-0022 的执行者）。

模块名: tools.check_completion
职责: 对已有的答卷文件核对"试卷声明的题号是否都答到了"，用于回答"模型答完整卷了吗"。
      只读试卷与答卷文件，不修改任何东西；核对逻辑复用 arena.coverage（与 EXAM 收尾同一套）。
依赖: 标准库 argparse/pathlib/sys；arena.coverage（纯文本函数）。
被依赖: 使用者（开发者、评审）。
相关 ADR: docs/decisions/ADR-0009-no-automatic-grading.md、
          docs/decisions/ADR-0022-answer-coverage-check.md

用法:
    python3 tools/check_completion.py [--runs-dir DIR] [--papers-dir DIR] [--current-only] [-v]

    `--current-only` 只核对**当前协议索引**（`runs/arena_history.jsonl`）里出现过的轮次。
    注意：归档轮次的答卷仍留在 `runs/` 下（ADR-0021 只归档索引、不动 run 目录），
    因此"按目录名排除 archive"是不够的——必须按索引判定。

退出码:
    0 —— 所有被核对的答卷都有覆盖度结论且都完整（"无法判定"记 warning，不算失败）
    1 —— 存在**明确未覆盖**的答卷（列出缺哪些题号）

判定依据（ADR-0022）:
    - 试卷侧：从试卷文本提取题号声明（如 `### K1｜…`）。
    - 答卷侧：核对答卷里是否逐题出现该题号。
    - 试卷没写题号 ⇒ "无法判定"；改版前的答卷（用旧版无题号试卷答的）会落到这一档，
      那是试卷版本问题，不是模型没答完。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arena.coverage import compare  # noqa: E402  (需先插入 ROOT 才能导入 arena)
from arena.store import JsonlResultStore  # noqa: E402
from arena.vocabulary import HISTORY_FILENAME  # noqa: E402

DEFAULT_RUNS_DIR = ROOT / "runs"
DEFAULT_PAPERS_DIR = ROOT / "test_content" / "real_test"


def answer_body(text: str) -> str:
    """剥掉答卷文件头的元数据，只留模型正文。

    前置条件: text 为答卷文件全文。
    后置条件: 返回第一个 `---` 分隔线之后的正文；没有分隔线时原样返回。
    禁止: 解析或校验元数据字段——核对只需要正文（元数据是给人看的）。
    异常: 无。
    """
    _, _, body = text.partition("\n---\n")
    return body


def iter_answer_files(runs_dir: Path, current_run_ids: set[str] | None) -> list[Path]:
    """列出待核对的答卷文件。

    前置条件: runs_dir 存在（不存在时返回空列表）；current_run_ids 为 None 时不做索引过滤。
    后置条件: 返回 runs_dir 下所有 `answers/*.md`；给定 current_run_ids 时只保留轮次在其中的答卷。
    禁止: 递归进无关目录做文本匹配——只认父目录名为 answers 的 Markdown。
    异常: 无。
    """
    found: list[Path] = []
    for path in sorted(runs_dir.rglob("*.md")):
        if path.parent.name != "answers":
            continue
        if current_run_ids is not None and path.parent.parent.name not in current_run_ids:
            continue
        found.append(path)
    return found


def main(argv: list[str]) -> int:
    """命令行入口。

    前置条件: argv 为参数列表（可为空）。
    后置条件: 打印逐份答卷的覆盖度；返回退出码（0 通过 / 1 存在明确未覆盖）。
    禁止: 因"无法判定"而返回失败——无法判定是信息不足，不是答卷有问题。
    异常: 无（文件缺失按行报告，不抛异常）。
    """
    parser = argparse.ArgumentParser(description="核对答卷是否覆盖试卷全部题目（ADR-0022）")
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="答卷根目录（默认 runs/）")
    parser.add_argument("--papers-dir", default=str(DEFAULT_PAPERS_DIR), help="试卷目录（默认 test_content/real_test）")
    parser.add_argument("--current-only", action="store_true", help="只核对当前协议索引（runs/arena_history.jsonl）里的轮次")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每份答卷的判定依据")
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    papers_dir = Path(args.papers_dir)
    current_ids: set[str] | None = None
    if args.current_only:
        current_ids = {record.run_id for record in JsonlResultStore(runs_dir / HISTORY_FILENAME).load_all()}
        if not current_ids:
            print(f"当前协议索引为空：{runs_dir / HISTORY_FILENAME}")
            return 0
    sheets = iter_answer_files(runs_dir, current_ids)
    if not sheets:
        print(f"没有找到答卷文件：{runs_dir}/**/answers/*.md")
        return 0

    incomplete: list[str] = []
    unknown: list[str] = []
    complete = 0

    for sheet in sheets:
        paper_name = sheet.stem.split("__")[-1]
        paper_path = papers_dir / paper_name
        if not paper_path.is_file():
            print(f"WARN  {sheet.relative_to(runs_dir)}: 找不到试卷 {paper_path}，跳过")
            unknown.append(str(sheet))
            continue
        coverage = compare(paper_path.read_text(encoding="utf-8"), answer_body(sheet.read_text(encoding="utf-8")))
        row = f"{sheet.relative_to(runs_dir)!s:64s} {coverage.summary()}"
        if coverage.complete is None:
            print(f"WARN  {row}")
            unknown.append(str(sheet))
        elif coverage.complete:
            print(f"OK    {row}")
            complete += 1
        else:
            print(f"MISS  {row}")
            incomplete.append(str(sheet))
        if args.verbose and coverage.missing:
            print(f"        缺失题号: {'、'.join(coverage.missing)}")

    print()
    print(f"答卷 {len(sheets)} 份：完整 {complete} · 未覆盖 {len(incomplete)} · 无法判定 {len(unknown)}")
    if incomplete:
        print("未覆盖的答卷（明确结论）：")
        for path in incomplete:
            print(f"  - {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
