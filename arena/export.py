"""Local Arena — 一轮答卷的纯文本汇总（跨系统可读的产物）。

模块名: arena.export
职责: 把一条 ExamRecord 渲染成一份自包含的纯文本（元数据头 + 逐卷正文），
      让答卷能脱离 JSONL 与 Markdown 被别的系统/应用直接读。
      渲染是纯函数：只读传入的记录与试卷文件，不落盘、不判分、不改写正文。
依赖: 标准库 pathlib；arena.coverage、arena.domain。
被依赖: arena.session（一轮 Run 的收尾落盘）、tools/export_answers.py（回查已有轮次）。
相关 ADR: docs/decisions/ADR-0023-run-answer-text-export.md、
          docs/decisions/ADR-0004-parsers-follow-stream-semantics.md、
          docs/decisions/ADR-0022-answer-coverage-check.md
"""

from __future__ import annotations

from pathlib import Path

from arena.coverage import compare
from arena.domain import AnswerSheet, ExamRecord

# 分隔线：`=` 隔"轮次/试卷"这种大块，`-` 隔"元数据/正文"。纯文本读者靠它们定位。
_RULE = "=" * 78
_HR = "-" * 78


def render(record: ExamRecord, papers_dir: Path | None = None) -> str:
    """把一轮 EXAM 记录渲染成汇总文本。

    前置条件: record 为一条 EXAM 记录；papers_dir 为试卷目录（可为 None，表示不核对覆盖度）。
    后置条件: 返回以换行结尾的文本：先是该轮元数据头，其后每份答卷一段
              （该卷元数据 + 分隔线 + 正文）。正文按 sheet.text 原样嵌入，不 strip、不截断、
              不剥离思考链。record.sheets 为空时只返回元数据头。
    禁止: 改写、重排或截断正文——它是"模型原话"（ADR-0004），任何加工都属于阅卷阶段（ADR-0009）；
          也禁止在试卷文件缺失时编造覆盖度，只能照实写"未核对"（ADR-0022）。
    异常: OSError —— 试卷文件存在但不可读；UnicodeDecodeError —— 试卷不是 UTF-8。
    """
    lines = _header(record)
    total = len(record.sheets)
    for index, sheet in enumerate(record.sheets, start=1):
        lines.extend(_sheet_block(index, total, sheet, papers_dir))
    return "\n".join(lines) + "\n"


def _header(record: ExamRecord) -> list[str]:
    """渲染轮次级元数据头。

    前置条件: record 为一条 EXAM 记录。
    后置条件: 返回头部各行；逐卷失败、无答卷、覆盖度口径与正文口径都如实写明。
    禁止: 省略覆盖度口径或正文口径——读这份文本的人看不到运行日志，
          不写清楚就会把"导出时重算"误当成"运行时结论"。
    异常: 无。
    """
    lines = [
        _RULE,
        f"Local Arena 答卷汇总 · {record.model_name}",
        _RULE,
        "",
        f"- 轮次: {record.run_id}",
        f"- 模型: {record.model_name}",
        f"- 模型文件: {record.model_path}",
        f"- 负载档: {record.profile}",
        f"- 开始时刻 (UTC): {record.started_at}",
        f"- EXAM 墙钟: {record.wall_seconds:.2f} s",
        f"- 采样参数: {_load_line(record)}",
        f"- 答卷份数: {len(record.sheets)}{_paper_names(record)}",
        f"- 逐卷失败: {_failures_line(record)}",
        "- 覆盖度口径: 按**导出时**的试卷文本重算（ADR-0022）；运行当时的结论见同目录 answers/ 下各卷文件头",
        "- 正文口径: 逐字保留模型原话（含思考链），本文件不做任何加工，也不含判分（ADR-0004 / ADR-0009）",
    ]
    if not record.ok:
        lines.append(f"- 记录状态: ok=False · {record.error or '（未记录原因）'}")
    if not record.sheets:
        lines.append("- 注意: 本轮没有落盘任何答卷（未跑 EXAM，或每一卷都没跑成）")
    lines.append("")
    return lines


def _load_line(record: ExamRecord) -> str:
    """把采样参数渲染成一行；未记录时明说未记录。

    前置条件: record 为一条 EXAM 记录。
    后置条件: load 存在时返回参数名值对；为 None 时返回"未记录（旧记录）"。
    禁止: 用当前配置里的默认值顶替缺失的 load——那是拿"现在"冒充"当时"。
    异常: 无。
    """
    load = record.load
    if load is None:
        return "未记录（旧记录）"
    return (
        f"n_predict={load.n_predict}, n_ctx={load.n_ctx}, "
        f"temperature={load.temperature:g}, seed={load.seed}"
    )


def _paper_names(record: ExamRecord) -> str:
    """列出本轮的试卷名（便于人头一眼看到"少了一卷"）。

    前置条件: record 为一条 EXAM 记录。
    后置条件: 有答卷时返回 `（paper_1、paper_2）`；无答卷时返回空串。
    禁止: 从计划或配置里推断"本该有几卷"——这里只报实际落盘的事实。
    异常: 无。
    """
    names = [sheet.paper_name for sheet in record.sheets]
    return f"（{'、'.join(names)}）" if names else ""


def _failures_line(record: ExamRecord) -> str:
    """渲染逐卷失败原因。

    前置条件: record 为一条 EXAM 记录。
    后置条件: 无失败时返回"无"；有失败时返回 `卷名(原因)` 的顿号串。
    禁止: 把失败折成"成功/失败"两值——"超时"与"抽取失败"指向的下一步动作不同（ADR-0021）。
    异常: 无。
    """
    if not record.paper_failures:
        return "无"
    return "、".join(f"{failure.paper_name}（{failure.reason}）" for failure in record.paper_failures)


def _sheet_block(
    index: int,
    total: int,
    sheet: AnswerSheet,
    papers_dir: Path | None,
) -> list[str]:
    """渲染一份答卷：该卷元数据 + 分隔线 + 正文。

    前置条件: index 从 1 起、不超过 total；sheet 为该卷答卷；papers_dir 可为 None。
    后置条件: 返回该卷各行，正文行按 sheet.text 原样附在分隔线之后。
    禁止: 对正文做 strip/折行/加前缀——分隔线已经保证了块边界，正文不需要被"照顾"。
    异常: 无（覆盖度核对的异常口径见 _coverage_for）。
    """
    lines = [
        "",
        _RULE,
        f"[{index}/{total}] {sheet.paper_name} · {sheet.model_name}",
        _RULE,
        "",
        f"- Prompt 吞吐: {sheet.prompt_ts:.2f} t/s",
        f"- Generation 吞吐: {sheet.generation_ts:.2f} t/s",
        f"- 墙钟耗时: {sheet.wall_seconds:.2f} s",
        f"- 答卷字符数: {sheet.chars}",
        f"- 题目覆盖: {_coverage_for(sheet, papers_dir)}",
        "",
        _HR,
        "",
        sheet.text,
    ]
    return lines


def _coverage_for(sheet: AnswerSheet, papers_dir: Path | None) -> str:
    """核对一份答卷的题目覆盖度（复用 ADR-0022 的纯文本核对）。

    前置条件: sheet 为某卷答卷；papers_dir 为试卷目录或 None。
    后置条件: 能读到试卷时返回 Coverage.summary()；读不到时返回带原因的"未核对"文本。
    禁止: 在核对不成立时给出比例——"未核对"与"没答完"必须可区分（ADR-0022），
          因此缺试卷只能报未核对，不能报 0/30。
    异常: OSError —— 试卷文件存在但不可读；UnicodeDecodeError —— 试卷不是 UTF-8。
    """
    if papers_dir is None:
        return "未核对（未提供试卷目录）"
    paper_path = Path(papers_dir) / sheet.paper_name
    if not paper_path.is_file():
        return f"未核对（找不到试卷文件 {paper_path}）"
    return compare(paper_path.read_text(encoding="utf-8"), sheet.text).summary()
