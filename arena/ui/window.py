"""Local Arena — 主窗口（壳：标题带 / 舞台 / 状态 / 记分板 / 页脚）。

模块名: arena.ui.window
职责: 组织控件树与窗口级状态（运行中/空闲、进度、状态文案）；把用户操作转成回调，
      由 controller 决定动作含义——窗口不直接驱动 Session。
依赖: PyGObject(gtk)；arena.domain、arena.ui.views。
被依赖: arena.ui.controller、run_arena.py。
相关 ADR: docs/decisions/ADR-0007-external-css-theme.md、
          docs/decisions/ADR-0008-worker-thread-ui-bridge.md、
          docs/decisions/ADR-0020-completion-condition-instead-of-token-cap.md、
          docs/decisions/ADR-0024-progress-bar-inline-text.md、
          docs/decisions/ADR-0027-persisted-model-directory.md、
          docs/decisions/ADR-0028-persisted-llama-bin-directory.md
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from arena.domain import Scoreboard
from arena.ui.views import ScoreboardView

# 类名与 arena/theme/arena.css 的契约保持一致；改这里必须同步改 CSS。
_START_LABEL_IDLE = "START"
_START_LABEL_RUNNING = "CANCEL"


class ArenaWindow(Gtk.Window):
    """火热 arena 主窗口。"""

    def __init__(self, title: str = "Local Arena") -> None:
        super().__init__(title=title)
        self.set_default_size(1020, 780)
        self.set_border_width(0)
        # 关闭窗口先走收尾回调（取消并等在跑的子进程退出），再由 destroy 退出主循环。
        # 顺序很重要：直接 connect("destroy", Gtk.main_quit) 会跳过收尾，
        # 留下仍在占显存的孤儿进程（ADR-0021）。
        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", Gtk.main_quit)
        self.scoreboard_view = ScoreboardView()
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._build()

    # -- 装配 ---------------------------------------------------------------

    def _build(self) -> None:
        """搭建控件树。"""
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.get_style_context().add_class("arena-root")
        root.pack_start(self._build_title_band(), False, False, 0)
        root.pack_start(self._build_stage(), False, False, 0)
        root.pack_start(self._build_status_band(), False, False, 0)
        root.pack_start(self.scoreboard_view, False, False, 0)
        root.pack_start(self._build_footer(), False, False, 0)
        self.add(root)

    def _build_title_band(self) -> Gtk.Widget:
        """标题带：LOCAL ARENA / LLM TEST ARENA / 副标题。"""
        band = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        band.get_style_context().add_class("title-band")
        band.pack_start(self._label("LOCAL ARENA", "arena-title"), False, False, 0)
        band.pack_start(self._label("LLM TEST ARENA", "arena-subtitle"), False, False, 0)
        band.pack_start(self._label("bench · perplexity · exam", "arena-tagline"), False, False, 0)
        return band

    def _build_stage(self) -> Gtk.Widget:
        """舞台：方形"Choose Your Model!"键 + 圆形 START 键。"""
        stage = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=52)
        stage.get_style_context().add_class("stage")
        stage.set_halign(Gtk.Align.CENTER)

        self.choose_button = Gtk.Button(label="Choose Your Model!\n\n(未选择)")
        self.choose_button.get_style_context().add_class("choose-model")
        self.choose_button.set_size_request(240, 240)
        self.choose_button.connect("clicked", lambda _button: self._emit("choose"))

        self.primary_button = Gtk.Button(label=_START_LABEL_IDLE)
        self.primary_button.get_style_context().add_class("start-button")
        self.primary_button.set_size_request(168, 168)
        self.primary_button.set_valign(Gtk.Align.CENTER)
        self.primary_button.connect("clicked", lambda _button: self._emit("primary"))

        stage.pack_start(self.choose_button, False, False, 0)
        stage.pack_start(self.primary_button, False, False, 0)
        return stage

    def _build_status_band(self) -> Gtk.Widget:
        """状态行：模型名 + 状态文案 + 进度条。"""
        band = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        band.get_style_context().add_class("status-band")
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.model_label = self._label("model: —", "model-name")
        self.status_label = self._label("status: idle", "status-label")
        line.pack_start(self.model_label, False, False, 0)
        line.pack_end(self.status_label, False, False, 0)
        self.progress = Gtk.ProgressBar()
        # 条内文字打开：可量化时显示百分比，脉冲档显示事实文案（见 set_progress）。
        # 文字颜色由 arena.css 的 `progressbar text` 负责——它同时要在填充段（亮橙）与
        # 未填充段（暗棕）上可读，所以用浅色 + 深色阴影，而不是跟随主题的暖棕色。
        self.progress.set_show_text(True)
        self.progress.set_fraction(0.0)
        band.pack_start(line, False, False, 0)
        band.pack_start(self.progress, False, False, 0)
        return band

    def _build_footer(self) -> Gtk.Widget:
        """页脚：HISTORY / MODEL FOLDER / LLAMA BIN / COMPARE 入口。"""
        band = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        band.get_style_context().add_class("footer-band")
        self.history_button = self._ghost_button("HISTORY", "history")
        self.folder_button = self._ghost_button("MODEL FOLDER", "folder")
        self.llama_bin_button = self._ghost_button("LLAMA BIN", "llama-bin")
        self.compare_button = self._ghost_button("COMPARE", "compare")
        band.pack_start(self.history_button, False, False, 0)
        # pack_end 从右往左排：先 COMPARE，再 LLAMA BIN，最后 MODEL FOLDER，
        # 最终顺序为 HISTORY … MODEL FOLDER | LLAMA BIN | COMPARE。
        # 两个目录入口相邻、都在右侧，是因为它们回答同一类问题："这份环境装在哪"。
        band.pack_end(self.compare_button, False, False, 0)
        band.pack_end(self.llama_bin_button, False, False, 0)
        band.pack_end(self.folder_button, False, False, 0)
        return band

    @staticmethod
    def _label(text: str, css_class: str) -> Gtk.Label:
        """构造一个带样式类、居中的标签。"""
        label = Gtk.Label(label=text)
        label.get_style_context().add_class(css_class)
        return label

    def _ghost_button(self, text: str, event: str) -> Gtk.Button:
        """构造一个页脚幽灵按钮。"""
        button = Gtk.Button(label=text)
        button.get_style_context().add_class("ghost-button")
        button.connect("clicked", lambda _button: self._emit(event))
        return button

    # -- 对外接口 -----------------------------------------------------------

    def bind(
        self,
        *,
        on_choose: Callable[[], None],
        on_primary: Callable[[], None],
        on_folder: Callable[[], None],
        on_llama_bin: Callable[[], None],
        on_history: Callable[[], None],
        on_compare: Callable[[], None],
        on_close: Callable[[], None],
    ) -> None:
        """注册用户动作的回调（六个控件 + 窗口关闭）。

        前置条件: 回调均为无参可调用对象，且由 controller 提供；on_close 会阻塞主循环直到收尾完成。
        后置条件: 点击相应控件即调用对应回调（在 UI 线程内）；关闭窗口先调用 on_close 再销毁。
        禁止: 传入会长时间阻塞的回调——它们在主循环里执行，阻塞会让窗口假死；
              on_close 内部也只允许"有界等待"（见 controller.shutdown）。
        异常: 无。
        """
        self._callbacks = {
            "choose": on_choose,
            "primary": on_primary,
            "folder": on_folder,
            "llama-bin": on_llama_bin,
            "history": on_history,
            "compare": on_compare,
            "close": on_close,
        }

    def _on_delete_event(self, *_args: object) -> bool:
        """窗口被要求关闭：先让收尾回调把在跑的子进程收干净，再允许销毁。

        前置条件: 无（未注册收尾回调时是空操作，便于单独预览窗口）。
        后置条件: 返回 False，交回 GTK 继续走 destroy → Gtk.main_quit。
        禁止: 在这里直接调 Gtk.main_quit——那会跳过收尾，留下占显存的孤儿进程（ADR-0021）。
        异常: 无。
        """
        self._emit("close")
        return False

    def _emit(self, event: str) -> None:
        """触发已注册的回调（未注册时是空操作，便于单独预览窗口）。"""
        callback = self._callbacks.get(event)
        if callback is not None:
            callback()

    def set_model_name(self, name: str | None) -> None:
        """更新已选模型显示。

        前置条件: 无。
        后置条件: 选择键与状态行同时反映所选模型（None 表示未选）。
        禁止: 无。
        异常: 无。
        """
        if name is None:
            self.choose_button.set_label("Choose Your Model!\n\n(未选择)")
            self.model_label.set_text("model: —")
        else:
            self.choose_button.set_label(f"Choose Your Model!\n\n{name}")
            self.model_label.set_text(f"model: {name}")

    def set_status(self, text: str) -> None:
        """更新状态文案（也用于把错误显示给用户）。

        前置条件: text 为单行文本（窗口只有一行状态位）。
        后置条件: 状态行显示 `status: <text>`。
        禁止: 拼接多行信息——长文本会把状态行顶宽（记分板才是放细节的地方，ADR-0016）。
        异常: 无。
        """
        self.status_label.set_text(f"status: {text}")

    def set_running(self, running: bool) -> None:
        """切换运行态外观。

        前置条件: 无。
        后置条件: 运行中时圆键显示 CANCEL；选择键、模型文件夹键与 llama.cpp 文件夹键被禁用，
                   防止"跑一半换模型/换目录"。
        禁止: 在运行中允许再次点击开始——重入由本方法 + controller 双重拦截（ADR-0008）。
        异常: 无。
        """
        self.primary_button.set_label(_START_LABEL_RUNNING if running else _START_LABEL_IDLE)
        context = self.primary_button.get_style_context()
        if running:
            context.add_class("running")
        else:
            context.remove_class("running")
        self.choose_button.set_sensitive(not running)
        self.folder_button.set_sensitive(not running)
        self.llama_bin_button.set_sensitive(not running)

    def set_progress(self, fraction: float | None, message: str = "") -> None:
        """更新进度条。

        前置条件: fraction 为 0.0~1.0 或 None（无法量化）；message 为单行事实文案（可为空）。
        后置条件: 条内文字与 fraction 同源——可量化时是百分比，不可量化时是 message；
                   message 非空时状态行同步更新。
        禁止: 把 None 当成 0——那会让人以为卡住了；也禁止给脉冲档编造百分比，
              脉冲只表示"还在跑"，比例必须来自真实计数（ADR-0020）。
        异常: 无。
        """
        if fraction is None:
            self.progress.pulse()
            # 脉冲档没有百分比可显示，留给它的条内位置正好放事实文案；
            # 若这里也留空，这一档的进度条就退化成一条没有任何信息的滑动色块。
            self.progress.set_text(message)
        else:
            self.progress.set_fraction(fraction)
            self.progress.set_text(f"{fraction * 100:.0f}%")
        if message:
            self.set_status(message)

    def reset_progress(self) -> None:
        """把进度条归零（一轮 Run 结束时调用）。

        前置条件: 无。
        后置条件: 进度条 fraction 回到 0.0，条内文字清空（不留下上一轮的百分比）。
        禁止: 用 pulse 代替归零——脉冲表示"还在跑"，与"已结束"语义相反。
        异常: 无。
        """
        self.progress.set_fraction(0.0)
        self.progress.set_text("")

    def show_scoreboard(self, scoreboard: Scoreboard) -> None:
        """把一轮结果画到记分板上。

        前置条件: scoreboard 属于刚结束的一轮 Run，或由历史重建。
        后置条件: 记分板三行与失败行已更新为该 scoreboard 的内容。
        禁止: 在此处补算缺失指标——渲染层的职责是显示，不是推断（ADR-0010）。
        异常: 无。
        """
        self.scoreboard_view.render(scoreboard)

    def launch(self) -> int:
        """显示窗口并进入 GTK 主循环。

        前置条件: 已在 UI 线程调用；主题已挂载（否则外观为默认灰）。
        后置条件: 用户关闭窗口后返回 0。
        禁止: 在 worker 线程调用。
        异常: 无。
        """
        self.show_all()
        Gtk.main()
        return 0
