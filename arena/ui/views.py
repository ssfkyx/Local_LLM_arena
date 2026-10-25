"""Local Arena — 视图（记分板、选模型、历史、对比）。

模块名: arena.ui.views
职责: 把领域对象渲染成控件；不认识 Session，也不做任何测试逻辑。
依赖: PyGObject(gtk)；arena.domain、arena.vocabulary。
被依赖: arena.ui.window（ScoreboardView）、arena.ui.controller（其余视图）。
相关 ADR: docs/decisions/ADR-0007-external-css-theme.md、
          docs/decisions/ADR-0010-comparison-across-models.md、
          docs/decisions/ADR-0016-scoreboard-hint-display-policy.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md、
          docs/decisions/ADR-0027-persisted-model-directory.md
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402

from arena.domain import (
    BenchRecord,
    ComparisonRow,
    ExamRecord,
    Model,
    PerplexityRecord,
    Scoreboard,
    TrialRecord,
)
from arena.vocabulary import TestKind

# 缺值统一用长破折号：它必须与"数字 0.0"在视觉上截然不同——0 是测出来零，— 是没测。
_MISSING = "—"

# 提示列的字符上限：它决定记分板的最小宽度，进而决定窗口是否被撑宽（ADR-0016）。
_HINT_MAX_CHARS = 56

# 失败行的字符上限：失败原因里可能塞着整段工具 stderr（实测把窗口顶到过 4644px）。
_FAILURE_MAX_CHARS = 120

_METRIC_LABELS: dict[TestKind, str] = {
    TestKind.BENCH: "BENCH",
    TestKind.PERPLEXITY: "PERPLEXITY",
    TestKind.EXAM: "EXAM",
}


def human_bytes(size_bytes: int) -> str:
    """把字节数格式化成人类可读形式。

    前置条件: size_bytes 非负（文件大小）。
    后置条件: 返回值形如 "8.0 GiB"；超过 TiB 时上限为 TiB。
    禁止: 用 1000 进制——模型文件大小按 1024 进制才有意义（与 llama.cpp 报告一致）。
    异常: 无。
    """
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


class ScoreboardView(Gtk.Frame):
    """屏幕上那块记分板：三行指标 + 一行失败说明。"""

    def __init__(self) -> None:
        super().__init__()
        self.get_style_context().add_class("scoreboard")
        self._grid = Gtk.Grid(column_spacing=22, row_spacing=8)
        self._grid.set_margin_top(2)
        self.add(self._grid)
        self._value_labels: dict[TestKind, Gtk.Label] = {}
        self._hint_labels: dict[TestKind, Gtk.Label] = {}
        self._failure_label = Gtk.Label(xalign=0.0)
        self._failure_label.get_style_context().add_class("metric-hint")
        # 失败原因可能包含整段工具 stderr（实测曾把窗口从 1020px 顶到 4644px），
        # 因此与提示列同样必须可截断，全文进 tooltip（ADR-0016）。
        self._failure_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._failure_label.set_max_width_chars(_FAILURE_MAX_CHARS)
        self._build_rows()

    def _build_rows(self) -> None:
        """一次性铺好三行骨架，之后只更新文本（避免每次 render 重建控件树）。"""
        for row, kind in enumerate(_METRIC_LABELS):
            name = Gtk.Label(label=_METRIC_LABELS[kind], xalign=0.0)
            name.get_style_context().add_class("metric-name")
            value = Gtk.Label(label=_MISSING, xalign=0.0)
            value.get_style_context().add_class("metric-value")
            hint = Gtk.Label(label="", xalign=0.0)
            hint.get_style_context().add_class("metric-hint")
            # 提示列必须"想要多窄就多窄"：否则它按最长文本撑出自然宽度，把窗口顶宽（ADR-0016）。
            hint.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            hint.set_max_width_chars(_HINT_MAX_CHARS)
            self._grid.attach(name, 0, row, 1, 1)
            self._grid.attach(value, 1, row, 1, 1)
            self._grid.attach(hint, 2, row, 1, 1)
            self._value_labels[kind] = value
            self._hint_labels[kind] = hint
        self._grid.attach(self._failure_label, 0, len(_METRIC_LABELS), 3, 1)

    def render(self, scoreboard: Scoreboard) -> None:
        """把记分板渲染到界面。

        前置条件: scoreboard 属于刚刚结束（或从历史重建）的一轮 Run。
        后置条件: 三行文本更新为本次数字或 _MISSING；失败原因汇总显示在末行；
                   答卷行只显示文件名，完整路径进 tooltip（ADR-0016）；
                   末行同时标出这组数字的负载档（如 "real 档"）——否则一块来自 quick 自检的
                   记分板会被读成正式成绩（ADR-0018）。
        禁止: 在缺值时显示 0——那会被读成"测出来是零"；也禁止把绝对路径写进值列或提示列正文；
              禁止省略负载档标注——档位是这组数字能否拿去比较的前提。
        异常: 无。
        """
        bench_pp = scoreboard.bench_pp_ts
        bench_tg = scoreboard.bench_tg_ts
        if bench_pp is None and bench_tg is None:
            bench_text = _MISSING
        else:
            bench_text = f"pp {_format(bench_pp, 1)} t/s · tg {_format(bench_tg, 1)} t/s"

        perplexity = scoreboard.perplexity
        if perplexity is None:
            ppl_text = _MISSING
        else:
            ppl_text = f"{perplexity:.4f} ± {_format(scoreboard.perplexity_stderr, 4)}"

        answers = scoreboard.answer_paths
        exam_text = f"{len(answers)} 份答卷已落盘" if answers else _MISSING

        self._value_labels[TestKind.BENCH].set_text(bench_text)
        self._value_labels[TestKind.PERPLEXITY].set_text(ppl_text)
        self._value_labels[TestKind.EXAM].set_text(exam_text)
        self._hint_labels[TestKind.BENCH].set_text("吞吐")
        self._hint_labels[TestKind.PERPLEXITY].set_text("困惑度")
        self._set_exam_hint(answers)

        # 档位取自闭集（real/quick），长度有界，不会把窗口顶宽（ADR-0016 的宽度纪律）。
        # 两个分支都必须带上它：有失败不等于没有数字，而缺了档位，一块残缺的记分板同样读不出可比性。
        profile_text = f"{scoreboard.profile} 档 · " if scoreboard.profile else ""
        if scoreboard.failures:
            # 失败原因可能含整段 stderr：正文截断显示，全文进 tooltip（ADR-0016）。
            self._failure_label.set_text(f"{profile_text}失败: " + " | ".join(scoreboard.failures))
            self._failure_label.set_tooltip_text("\n".join(scoreboard.failures))
        else:
            self._failure_label.set_text(
                f"run {scoreboard.run_id} · {profile_text}墙钟 {scoreboard.wall_seconds:.1f}s"
            )
            self._failure_label.set_tooltip_text(None)

    def _set_exam_hint(self, answer_paths: tuple[str, ...]) -> None:
        """把答卷路径压成"文件名 + tooltip 全文"。

        前置条件: answer_paths 为 AnswerSheetWriter 返回的路径元组（可为空）。
        后置条件: 提示列显示文件名（以 · 分隔），tooltip 为完整路径（每行一条）；
                   路径为空时提示列为普通说明文案、无 tooltip。
        禁止: 把完整路径当正文——它会撑宽窗口（ADR-0016）。
        异常: 无。
        """
        hint = self._hint_labels[TestKind.EXAM]
        if not answer_paths:
            hint.set_text("答卷文件")
            hint.set_tooltip_text(None)
            return
        hint.set_text(" · ".join(Path(path).name for path in answer_paths))
        hint.set_tooltip_text("\n".join(answer_paths))


def _format(value: float | None, digits: int) -> str:
    """格式化可缺省浮点数。"""
    return _MISSING if value is None else f"{value:.{digits}f}"


class _TableDialog(Gtk.Dialog):
    """带表格的模态对话框底板（历史/对比/选模型共用）。

    统一挂 `arena-dialog` 类：对话框是独立顶层窗口，不继承主窗口的 `.arena-root` 样式，
    必须自己声明样式锚点，否则会退回 GTK 默认的浅色外观（ADR-0007）。
    """

    def __init__(self, parent: Gtk.Window, title: str, columns: list[str], width: int = 900) -> None:
        super().__init__(title=title, transient_for=parent, modal=True)
        self.get_style_context().add_class("arena-dialog")
        self.set_default_size(width, 420)
        self.add_button("关闭", Gtk.ResponseType.CLOSE)
        self._store = Gtk.ListStore(*([str] * len(columns)))
        self._view = Gtk.TreeView(model=self._store)
        for index, column in enumerate(columns):
            renderer = Gtk.CellRendererText()
            column_view = Gtk.TreeViewColumn(column, renderer, text=index)
            column_view.set_resizable(True)
            self._view.append_column(column_view)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self._view)
        content = self.get_content_area()
        content.pack_start(self._title_label(title), False, False, 8)
        # 告警行：默认不存在（no_show_all + hide），由 set_notice() 显式点亮。
        # 它承载"历史里有别的评估协议"这类必须被看见的事实（ADR-0021）。
        self._notice_label = Gtk.Label(label="", xalign=0.0)
        self._notice_label.set_line_wrap(True)
        self._notice_label.get_style_context().add_class("arena-dialog-notice")
        self._notice_label.set_no_show_all(True)
        self._notice_label.hide()
        content.pack_start(self._notice_label, False, False, 4)
        content.pack_start(scroller, True, True, 8)

    def set_notice(self, text: str) -> None:
        """点亮/熄灭顶部告警行。

        前置条件: 在 show_modal() 之前调用（show_all 会把未标记 no_show_all 的子控件全部显示）。
        后置条件: text 非空时告警行可见并显示该文本；为空时彻底隐藏（不留空白条）。
        禁止: 用空串占位来"保持布局稳定"——一条空告警会被读成"没有告警"的反面。
        异常: 无。
        """
        if text:
            self._notice_label.set_text(text)
            self._notice_label.set_no_show_all(False)
            self._notice_label.show()
        else:
            self._notice_label.set_text("")
            self._notice_label.set_no_show_all(True)
            self._notice_label.hide()

    def _title_label(self, title: str) -> Gtk.Label:
        """构造对话框标题行（样式来自 theme 的 .arena-dialog-title）。"""
        label = Gtk.Label(label=title, xalign=0.0)
        label.get_style_context().add_class("arena-dialog-title")
        return label

    def fill(self, rows: list[list[str]]) -> None:
        """填入表格行（调用后需 show_all 才能看到）。

        前置条件: 每行长度等于构造时的列数。
        后置条件: 表格内容被整体替换（先清空再插入）。
        禁止: 逐行追加到旧内容上——历史视图必须反映当前磁盘事实。
        异常: ValueError —— 行长度与列数不符。
        """
        expected = self._store.get_n_columns()
        for row in rows:
            if len(row) != expected:
                raise ValueError(f"行有 {len(row)} 列，期望 {expected} 列")
        self._store.clear()
        for row in rows:
            self._store.append(row)

    def show_modal(self) -> None:
        """显示对话框并阻塞到用户关闭（仅可在 UI 线程调用）。

        前置条件: 已在 UI 线程；表格内容已通过 fill() 填好。
        后置条件: 用户关闭后对话框被 destroy，控件不可再复用。
        禁止: 在 worker 线程调用——GTK 的 run() 会接管主循环。
        异常: 无。
        """
        self.show_all()
        self.run()
        self.destroy()


class ModelChooserDialog(_TableDialog):
    """Choose Your Model! 对话框。"""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(parent, "Choose Your Model!", ["模型", "量化", "大小", "文件"], width=820)
        self._selection = self._view.get_selection()
        self._selection.set_mode(Gtk.SelectionMode.SINGLE)

    def pick(self, models: list[Model], models_dir: Path | None = None) -> Model | None:
        """弹出对话框让用户选一个模型。

        前置条件: models 来自 ModelCatalog（可为空）；models_dir 是本次扫描的目录，
                  仅用于列表为空时把"哪个目录没有模型"说清楚（可为 None）。
        后置条件: 用户确认时返回选中的 Model；用户取消或列表为空时返回 None；
                   列表为空时对话框顶部显示目录提示，且不提供"选定"按钮。
        禁止: 在空列表时返回假模型——返回 None 让调用方保持"未选"状态；
              也禁止在空目录时仍给出确认按钮——那会返回 None 却让人以为已经选过。
        异常: 无。
        """
        if models:
            self.set_notice("")
        else:
            where = f": {models_dir}" if models_dir is not None else ""
            self.set_notice(f"模型文件夹中没有 .gguf 文件{where}")
        self.fill(
            [[model.display_name, model.quantization, human_bytes(model.size_bytes), model.path] for model in models]
        )
        if models:
            self.add_button("选定", Gtk.ResponseType.OK)
        self.show_all()
        response = self.run()
        selected = None
        if response == Gtk.ResponseType.OK:
            rows = self._selection.get_selected_rows()[1]
            if rows:
                index = rows[0].get_indices()[0]
                selected = models[index]
        self.destroy()
        return selected


class HistoryView(_TableDialog):
    """历史记录对话框。"""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(parent, "HISTORY · 历史记录", ["时间", "模型", "种类", "档位", "结果", "状态"], width=980)

    def present_records(self, records: list[TrialRecord]) -> None:
        """渲染全部历史记录。

        前置条件: records 为 Archive 的全部记录（写入顺序）。
        后置条件: 表格按时间倒序显示每条 Trial 的一句话摘要，并逐条标出所属负载档——
                   历史是"试过什么"的完整账本，quick 自检也照实列出（只是不进对比表）。
        禁止: 隐藏失败记录——失败与成功同样是历史事实；
              也禁止隐藏 quick 记录——那是把发生过的实验从账本上抹掉（ADR-0018）。
        异常: 无。
        """
        rows = [
            [
                record.started_at,
                record.model_name,
                record.kind.value,
                record.profile,
                self._summarize(record),
                "OK" if record.ok else f"FAILED: {record.error}",
            ]
            for record in reversed(records)
        ]
        self.fill(rows)
        self.show_modal()

    @staticmethod
    def _summarize(record: TrialRecord) -> str:
        """把一条记录压成一行人类可读结果。

        按记录类型分派而不是读 payload 字典：类型即契约，字典只是落盘形态。
        """
        if isinstance(record, BenchRecord):
            return " · ".join(f"{item.case.label()} {item.avg_ts:.1f} t/s" for item in record.results) or _MISSING
        if isinstance(record, PerplexityRecord):
            if record.result is None:
                return _MISSING
            return f"PPL {record.result.ppl:.4f} ± {record.result.ppl_stderr:.4f}"
        if isinstance(record, ExamRecord):
            return f"{len(record.sheets)} 份答卷 · " + ", ".join(sheet.paper_name for sheet in record.sheets)
        return _MISSING


class ComparisonView(_TableDialog):
    """跨模型对比对话框。"""

    def __init__(self, parent: Gtk.Window, profile: str) -> None:
        title = f"COMPARE · 跨模型对比 · {profile} 档" if profile else "COMPARE · 跨模型对比"
        super().__init__(parent, title, ["模型", "pp t/s", "tg t/s", "PPL", "最近运行"], width=900)

    def present_rows(self, rows: list[ComparisonRow], notice: str = "") -> None:
        """渲染对比表。

        前置条件: rows 由 ComparisonBuilder 产出（每个模型一行；非 official 档已在构建时被滤掉）；
                   notice 为"历史含旧评估协议记录"之类的告警文案（可为空）。
        后置条件: 按 pp 吞吐降序显示；缺值显示为 _MISSING；标题写明本表是哪个负载档的数字，
                   因为"不同负载的数字不可比"是这张表能成立的前提（ADR-0018）；
                   notice 非空时在表格上方显示该告警（ADR-0021）。
        禁止: 显示汇总平均或排名以外的推断值——本视图只并列原始数字；
              也禁止在此处把被滤掉的 quick 记录加回来——过滤是构建者的职责，渲染层不得绕过。
        异常: 无。
        """
        self.set_notice(notice)
        self.fill(
            [
                [
                    row.model_name,
                    _format(row.bench_pp_ts, 1),
                    _format(row.bench_tg_ts, 1),
                    _format(row.perplexity, 4),
                    row.last_run_at,
                ]
                for row in rows
            ]
        )
        self.show_modal()
