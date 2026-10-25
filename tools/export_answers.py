#!/usr/bin/env python3
"""Local Arena — 把已有轮次的答卷导出成纯文本汇总（ADR-0023 的执行者）。

模块名: tools.export_answers
职责: 回查 runs/arena_history.jsonl，把其中有答卷的 EXAM 记录渲染成该轮目录下的 answers.txt，
      让"只有 JSONL"这件事不再挡住跨系统/跨应用阅读。只新增文本产物，不改历史、不重跑模型；
      渲染逻辑复用 arena.export（与一轮 Run 收尾落盘同一套，避免两条实现路径漂移）。
依赖: 标准库 argparse/pathlib/sys；arena.domain、arena.export、arena.store、arena.vocabulary。
被依赖: 使用者（开发者、评审）。
相关 ADR: docs/decisions/ADR-0023-run-answer-text-export.md

用法:
    python3 tools/export_answers.py [--runs-dir DIR] [--papers-dir DIR]
                                    [--run-id ID ...] [--all] [--force] [-v]

    选择器（默认行为在最前，越靠后越具体）:
      不给任何选择器 —— 每个模型**最新一轮**有答卷的轮次（"最新三个模型"就是这个语义）
      --all          —— 索引里**所有**有答卷的轮次
      --run-id ID    —— 只转指定轮次（可重复）

    --force 覆盖已存在的 answers.txt；默认跳过，避免抹掉人工编辑过的文件。

退出码:
    0 —— 全部处理成功（含"已存在故跳过"）
    1 —— 存在写出失败的轮次
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arena.domain import ExamRecord  # noqa: E402  (需先插入 ROOT 才能导入 arena)
from arena.export import render  # noqa: E402
from arena.store import JsonlResultStore, RunArtifacts  # noqa: E402
from arena.vocabulary import ANSWERS_TEXT_FILENAME, HISTORY_FILENAME  # noqa: E402

DEFAULT_RUNS_DIR = ROOT / "runs"
DEFAULT_PAPERS_DIR = ROOT / "test_content" / "real_test"


def exam_records_with_sheets(runs_dir: Path) -> list[ExamRecord]:
    """读出索引里有答卷的 EXAM 记录。

    前置条件: runs_dir 为产物根目录（索引缺失时视为空）。
    后置条件: 返回按 started_at 升序排列的 ExamRecord，只含 sheets 非空、ok 的记录。
    禁止: 把没有答卷的 EXAM 记录也当候选——那会导出"跑了个空的"这种假产物（ADR-0021）；
          也禁止在索引损坏时报错退出，损坏行由 JsonlResultStore 告警后跳过。
    异常: OSError —— 索引文件存在但不可读。
    """
    store = JsonlResultStore(runs_dir / HISTORY_FILENAME)
    records = [
        record
        for record in store.load_all()
        if isinstance(record, ExamRecord) and record.sheets
    ]
    return sorted(records, key=lambda record: record.started_at)


def latest_per_model(records: list[ExamRecord]) -> list[ExamRecord]:
    """每个模型取最新一轮。

    前置条件: records 已按 started_at 升序排列。
    后置条件: 每个 model_path 只保留最后一条（即最新一轮），整体仍按时间升序返回。
    禁止: 按 model_name 分组——同名的两次量化是两个不同的被测对象（ADR-0010 以 model_path 为键）。
    异常: 无。
    """
    newest: dict[str, ExamRecord] = {}
    for record in records:
        newest[record.model_path] = record
    return sorted(newest.values(), key=lambda record: record.started_at)


def pick(records: list[ExamRecord], run_ids: list[str], take_all: bool) -> list[ExamRecord]:
    """按选择器挑出要转换的轮次。

    前置条件: records 为候选记录；run_ids 为显式指定的轮次（可为空）；take_all 表示全取。
    后置条件: 返回待转换记录；run_ids 非空时其顺序即输出顺序（便于按用户给的次序核对）。
    禁止: 在 run_ids 命中不到任何记录时静默返回空——调用方要用返回值区分"没选中"与"索引为空"。
    异常: 无。
    """
    if run_ids:
        wanted = set(run_ids)
        return [record for record in records if record.run_id in wanted]
    if take_all:
        return list(records)
    return latest_per_model(records)


def export_one(record: ExamRecord, runs_dir: Path, papers_dir: Path, force: bool) -> str:
    """把一条记录写成它那一轮的 answers.txt。

    前置条件: record 为有答卷的 EXAM 记录；runs_dir/papers_dir 为目录；force 为是否覆盖。
    后置条件: 返回 "write"（已写出）或 "skip"（目标已存在且未给 --force）；返回路径由调用方打印。
    禁止: 在这里改写正文或补算指标——渲染细节只属于 arena.export（ADR-0023）。
    异常: OSError —— 目录不可创建或文件不可写；ValueError —— run_id 非法。
    """
    target = RunArtifacts(runs_dir).run_dir(record.run_id) / ANSWERS_TEXT_FILENAME
    if target.is_file() and not force:
        return "skip"
    RunArtifacts(runs_dir).save(
        record.run_id, ANSWERS_TEXT_FILENAME, render(record, papers_dir)
    )
    return "write"


def main(argv: list[str]) -> int:
    """命令行入口。

    前置条件: argv 为参数列表（可为空）。
    后置条件: 逐轮打印 write/skip/fail 与目标路径，最后给一行汇总；返回退出码（0 成功 / 1 有失败）。
    禁止: 因某轮写出失败而中断其余轮次——每轮是独立的，失败按轮报。
    异常: 无（文件与目录问题按轮报告，不抛异常）。
    """
    parser = argparse.ArgumentParser(description="把已有轮次的答卷导出为纯文本汇总（ADR-0023）")
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="产物根目录（默认 runs/）")
    parser.add_argument("--papers-dir", default=str(DEFAULT_PAPERS_DIR), help="试卷目录（默认 test_content/real_test）")
    parser.add_argument("--run-id", action="append", default=[], help="只转指定轮次（可重复）")
    parser.add_argument("--all", action="store_true", help="转索引里所有有答卷的轮次")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 answers.txt")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每轮的模型与答卷份数")
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    papers_dir = Path(args.papers_dir)
    candidates = exam_records_with_sheets(runs_dir)
    if not candidates:
        print(f"索引里没有带答卷的 EXAM 记录：{runs_dir / HISTORY_FILENAME}")
        return 0

    selected = pick(candidates, args.run_id, args.all)
    if not selected:
        print(f"指定的 --run-id 在索引里找不到：{'、'.join(args.run_id)}")
        print(f"索引里有答卷的轮次：{'、'.join(record.run_id for record in candidates)}")
        return 0

    written: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for record in selected:
        target = RunArtifacts(runs_dir).run_dir(record.run_id) / ANSWERS_TEXT_FILENAME
        try:
            outcome = export_one(record, runs_dir, papers_dir, args.force)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            print(f"FAIL   {record.run_id}: {exc}")
            failed.append(record.run_id)
            continue
        if outcome == "skip":
            print(f"SKIP   {record.run_id}  （{target} 已存在，加 --force 可覆盖）")
            skipped.append(record.run_id)
            continue
        print(f"WRITE  {record.run_id}  ->  {target}")
        if args.verbose:
            print(f"       {len(record.sheets)} 卷（{'、'.join(s.paper_name for s in record.sheets)}）· 模型 {record.model_name}")
        written.append(record.run_id)

    print()
    print(f"轮次 {len(selected)} 轮：写出 {len(written)} · 跳过 {len(skipped)} · 失败 {len(failed)}")
    if failed:
        print("写失败的轮次：")
        for run_id in failed:
            print(f"  - {run_id}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
