"""Local Arena — 控制器（UI 事件 ⇄ ArenaSession 的桥）。

模块名: arena.ui.controller
职责: 把窗口上的六个动作翻译成 Session 调用；在 worker 线程跑测试，并用 GLib.idle_add
      把进度与结果投回主循环。
依赖: PyGObject(glib)；arena.config、arena.domain、arena.session、arena.settings、arena.sink、
      arena.ui.views、arena.ui.window、arena.vocabulary。
被依赖: run_arena.py。
相关 ADR: docs/decisions/ADR-0008-worker-thread-ui-bridge.md、
          docs/decisions/ADR-0010-comparison-across-models.md、
          docs/decisions/ADR-0015-vram-budget-excludes-oversized-models.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md、
          docs/decisions/ADR-0027-persisted-model-directory.md、
          docs/decisions/ADR-0028-persisted-llama-bin-directory.md
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402  (require_version 必须先执行)

from arena.config import AppConfig
from arena.domain import Model, ModelCatalog, Scoreboard, TestPlan
from arena.session import ArenaSession
from arena.settings import SettingsError, SettingsStore, UserSettings
from arena.sink import ProgressSink
from arena.ui.views import ComparisonView, HistoryView, ModelChooserDialog
from arena.ui.window import ArenaWindow
from arena.vocabulary import TERMINAL_STAGES, Stage, TestKind

# 关窗/退出时等待 worker 收尾的上界（秒）。取值依据：取消轮询 0.2 s + 进程组优雅退出 5 s
# + 落盘余量，因此 12 s 足以覆盖正常路径；超过它说明子进程不肯退，此时宁可留下警告也不卡住主进程。
_WORKER_JOIN_GRACE_SECONDS = 12.0

_STATUS_LABELS: dict[Stage, str] = {
    Stage.QUEUED: "queued",
    Stage.LOADING: "loading model",
    Stage.RUNNING: "running",
    Stage.PARSING: "parsing output",
    Stage.DONE: "done",
    Stage.FAILED: "failed",
    Stage.CANCELLED: "cancelled",
}


def _pump_events() -> None:
    """把当前排队的事件立刻画出去（在阻塞主循环之前用一次）。

    前置条件: 在 UI 线程调用。
    后置条件: 调用时刻已排队的事件都被处理完；返回后不等待新事件。
    禁止: 在 worker 线程调用——GTK 控件只允许主线程触碰（ADR-0008）。
    异常: 无。
    """
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


class ArenaController:
    """窗口动作 → 会话调用 → 界面反馈。"""

    def __init__(self, config: AppConfig, session: ArenaSession, window: ArenaWindow) -> None:
        self._config = config
        self._session = session
        self._window = window
        self._model: Model | None = None
        self._running = False
        # worker 线程句柄：关窗收尾时要有界等待它，因此必须留引用（ADR-0021）。
        self._worker: threading.Thread | None = None

    @property
    def selected_model(self) -> Model | None:
        """当前已选模型（未选时为 None）。

        前置条件: 无。
        后置条件: 返回用户最近一次确认选定的 Model；从未选过时为 None。
        禁止: 在此处回退到"目录里第一个模型"——未选就是未选，START 会提示选择。
        异常: 无。
        """
        return self._model

    @property
    def running(self) -> bool:
        """是否有一轮 Run 正在跑。

        前置条件: 无。
        后置条件: 运行态为 True；结束/失败/取消后回到 False（由主循环回调复位）。
        禁止: 用线程是否存活来判断——线程可能在投递结果前就退出了。
        异常: 无。
        """
        return self._running

    def bind(self) -> None:
        """把窗口事件接到本控制器。

        前置条件: 窗口已构建（ArenaWindow.__init__ 已返回）。
        后置条件: 六个控件的点击都会走到对应方法；启动时扫描一次模型目录，
                   并把目录/模型数量或配置告警写进状态行。
        禁止: 重复绑定——会重复触发同一次点击。
        异常: 无。
        """
        self._window.bind(
            on_choose=self.choose_model,
            on_primary=self.on_primary,
            on_folder=self.choose_models_dir,
            on_llama_bin=self.choose_llama_bin_dir,
            on_history=self.show_history,
            on_compare=self.show_comparison,
            on_close=self.shutdown,
        )
        if self._config.settings_warning:
            self._window.set_status(self._config.settings_warning)
        else:
            self._window.set_status("idle")
            self._report_models_dir()

    def on_primary(self) -> None:
        """处理圆形主键：空闲时开始，运行中时取消。

        前置条件: 无。
        后置条件: 空闲 → 尝试开始一轮 Run；运行中 → 请求取消。
        禁止: 无。
        异常: 无。
        """
        if self._running:
            self.cancel()
        else:
            self.start()

    def choose_model(self) -> None:
        """弹出模型选择对话框并记住选择。

        前置条件: 无。
        后置条件: 每次打开都重新扫描当前模型目录；用户确认时更新选中模型与界面，
                   并把该模型的历史记分板摆上来；用户取消时保持原选择；
                   目录为空时在对话框里给出提示。
        禁止: 在运行中弹出选择（开始键已禁用，这里再拦一次以防键盘/脚本触发）。
        异常: 无。
        """
        if self._running:
            return
        models = self._catalog_models()
        chosen = ModelChooserDialog(self._window).pick(models, self._config.models_dir)
        if chosen is None:
            if not models:
                self._window.set_status(f"模型文件夹中没有 .gguf 文件: {self._config.models_dir}")
            return
        self._model = chosen
        self._window.set_model_name(chosen.display_name)
        history = self._session.latest_scoreboard(chosen)
        if history is None:
            self._window.set_status("ready")
            return
        # 该模型跑过：先把历史成绩摆上来，让"这次比上次好还是差"当场可见。
        self._window.show_scoreboard(history)
        self._window.set_status("loaded history")

    def choose_models_dir(self) -> None:
        """让用户选择 GGUF 所在文件夹，保存到用户配置并刷新状态。

        前置条件: 在 UI 线程调用；未在运行中。
        后置条件: 用户确认且保存成功时，配置的内存副本与磁盘配置都指向新目录，
                   已选模型被清空（旧文件可能不在新目录里），状态行显示新目录的扫描结果；
                   取消、目录不可用或保存失败时保持原配置不变，并在状态行说明原因。
        禁止: 在运行中切换目录；也禁止绕过 SettingsStore 直接写 JSON——
              配置的版本与原子写入由该模块统一负责。
        异常: 无（文件系统错误被转成状态行文案）。
        """
        if self._running:
            return
        dialog = Gtk.FileChooserDialog(
            title="选择 GGUF 模型文件夹",
            transient_for=self._window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "选择", Gtk.ResponseType.OK)
        current = self._config.models_dir
        dialog.set_current_folder(str(current if current.is_dir() else Path.home()))
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not filename:
            return
        path = Path(filename).expanduser().resolve()
        if not path.is_dir():
            self._window.set_status(f"模型文件夹不可用: {path}")
            return
        try:
            SettingsStore(self._config.settings_path).save(
                replace(self._current_settings(), models_dir=path)
            )
        except OSError as exc:
            self._window.set_status(f"保存配置失败: {exc}")
            return
        self._config = replace(self._config, models_dir=path, settings_warning="")
        # 旧选择可能指向别的目录；清掉它，避免"目录换了、模型还是旧的"。
        self._model = None
        self._window.set_model_name(None)
        self._report_models_dir()

    def choose_llama_bin_dir(self) -> None:
        """让用户选择 llama.cpp 可执行文件文件夹，校验后保存到用户配置。

        前置条件: 在 UI 线程调用；未在运行中。
        后置条件: 用户确认、目录存在且 LLAMA_TOOLS 三个工具都可执行时，磁盘配置与内存配置都指向
                   该目录，状态行报告就绪路径；取消、目录不可用、工具不齐或保存失败时保持原配置
                   不变，并在状态行说明原因（缺哪个工具就列哪个）。
        禁止: 在运行中切换目录；也禁止在工具不全时仍然保存——那会把失败推到下一次 START，
              比在选择点拒绝更晚也更难懂（ADR-0028）；同样禁止在此处下载或安装 llama.cpp，
              本入口只记录"用户已经装好的那份构建"在哪里。
        异常: 无（文件系统错误被转成状态行文案）。
        """
        if self._running:
            return
        dialog = Gtk.FileChooserDialog(
            title="选择 llama.cpp 可执行文件文件夹",
            transient_for=self._window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "选择", Gtk.ResponseType.OK)
        current = self._config.llama_bin_dir
        dialog.set_current_folder(str(current if current.is_dir() else Path.home()))
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not filename:
            return
        path = Path(filename).expanduser().resolve()
        if not path.is_dir():
            self._window.set_status(f"llama.cpp 文件夹不可用: {path}")
            return
        missing = AppConfig.missing_tools(path)
        if missing:
            self._window.set_status(f"该文件夹缺少 llama 工具: {'、'.join(missing)}（{path}）")
            return
        try:
            SettingsStore(self._config.settings_path).save(
                replace(self._current_settings(), llama_bin_dir=path)
            )
        except OSError as exc:
            self._window.set_status(f"保存配置失败: {exc}")
            return
        self._config = replace(self._config, llama_bin_dir=path, settings_warning="")
        self._window.set_status(f"llama.cpp 已就绪: {path}")

    def start(self) -> None:
        """开始一轮 Run（在 worker 线程执行）。

        前置条件: 未在运行中；已选模型。
        后置条件: 成功启动时窗口进入运行态且 worker 线程开跑，结束时结果回到 UI；
                   参数或内容缺失时不启动，原因显示在状态行。
        禁止: 在已运行时再次启动——UI 不可重入是这套线程模型的前提（ADR-0008）。
        异常: 无（worker 内的意外异常被捕获并转成界面文案）。
        """
        if self._running:
            return
        if self._model is None:
            self._window.set_status("请先选择模型")
            return
        try:
            plan = self._session.default_plan(self._model)
        except (FileNotFoundError, ValueError) as exc:
            self._window.set_status(f"无法开始: {exc}")
            return

        self._running = True
        self._window.set_running(True)
        self._window.set_progress(0.0, "starting")
        self._worker = threading.Thread(target=self._run_worker, args=(plan,), daemon=True)
        self._worker.start()

    def shutdown(self) -> bool:
        """收尾：请求取消并在有界时间内等待 worker 退出（关窗与进程退出都走这里）。

        前置条件: 可在 UI 线程（关窗回调）或退出钩子里调用；未在运行时是空操作。
        后置条件: 返回 True 表示 worker 已退出（子进程已由 Runner 用 killpg 收干净）；
                   超时未退出时返回 False，并把状态行留成收尾提示——绝不静默假装停住了。
        禁止: 无限期等待——主进程卡死比留下一条失败记录更糟；
              也禁止跳过取消直接退出——那会留下仍在占显存的孤儿进程（ADR-0021）。
        异常: 无。
        """
        worker = self._worker
        if not self._running or worker is None:
            return True
        self._session.cancel()
        self._window.set_status("cancelling… 等待子进程退出")
        _pump_events()
        worker.join(_WORKER_JOIN_GRACE_SECONDS)
        if worker.is_alive():
            _pump_events()
            self._window.set_status("收尾超时：子进程可能仍在退出中")
            return False
        _pump_events()
        return True

    def cancel(self) -> None:
        """请求取消当前 Run。

        前置条件: 无（未在运行时是空操作）。
        后置条件: 当前子进程将被终止；已完成的 Trial 记录保留。
        禁止: 无。
        异常: 无。
        """
        self._session.cancel()
        self._window.set_status("cancelling")

    def show_history(self) -> None:
        """打开历史记录对话框。

        前置条件: 在 UI 线程调用。
        后置条件: 弹出模态表格，内容为调用时刻从磁盘读到的全部 TrialRecord（含失败记录）。
        禁止: 在此过滤失败记录——失败与成功同为历史事实（ADR-0005）。
        异常: OSError —— 历史文件不可读（由主循环的异常处理显示给用户）。
        """
        HistoryView(self._window).present_records(self._session.history())

    def show_comparison(self) -> None:
        """打开跨模型对比对话框。

        前置条件: 在 UI 线程调用。
        后置条件: 弹出模态表格，按 pp 吞吐降序、缺值显示 `—`（ADR-0010）；
                   标题标明本表的负载档——表里只有该档的数字（非 official 档已被
                   ComparisonBuilder 滤掉，ADR-0018）；
                   历史里存在旧评估协议记录时，表格上方显示告警（ADR-0021）。
        禁止: 在此重新计算或聚合数字——渲染的数据必须来自 ComparisonBuilder；
              也禁止把其他档位当作"补充信息"塞进同一张表。
        异常: OSError —— 历史文件或协议标记不可读。
        """
        ComparisonView(self._window, self._config.profile).present_rows(
            self._session.compare(), notice=self._session.protocol_warning() or ""
        )

    # -- 主循环侧（由 worker 通过 GLib.idle_add 调用） -----------------------

    def apply_stage(self, kind: TestKind, stage: Stage) -> bool:
        """把阶段变化画到状态行与进度条（只在主循环线程执行）。

        前置条件: 由 GLib.idle_add 投递；kind/stage 取自 vocabulary。
        后置条件: 状态行反映当前 Trial 与阶段；进入终态时进度条归零。
        禁止: 直接“顺手”改测试状态——本方法只负责显示。
        异常: 无。
        """
        self._window.set_status(f"{kind.value.lower()} · {_STATUS_LABELS.get(stage, stage.value)}")
        if stage in TERMINAL_STAGES:
            self._window.reset_progress()
        return False

    def apply_progress(self, kind: TestKind, fraction: float | None, message: str) -> bool:
        """把进度画到进度条（只在主循环线程执行）。

        前置条件: fraction 为 0.0~1.0 或 None。
        后置条件: 进度条与状态文案更新。
        禁止: 无。
        异常: 无。
        """
        text = f"{kind.value.lower()} · {message}" if message else kind.value.lower()
        self._window.set_progress(fraction, text)
        return False

    # -- worker 线程侧 ------------------------------------------------------

    def _run_worker(self, plan: TestPlan) -> None:
        """在 worker 线程里跑完一轮 Run，并把结果投回主循环。"""
        sink = _UiSink(self._session, self)
        try:
            scoreboard = self._session.run(plan, sink)
        except Exception as exc:  # noqa: BLE001 - worker 里的意外必须回到 UI，否则按钮永久卡死
            GLib.idle_add(self._on_worker_failed, str(exc))
            return
        GLib.idle_add(self._on_worker_finished, scoreboard)

    def _on_worker_finished(self, scoreboard: Scoreboard) -> bool:
        """主循环回调：把记分板画出来并解除运行态。"""
        self._running = False
        self._window.set_running(False)
        self._window.reset_progress()
        self._window.show_scoreboard(scoreboard)
        if scoreboard.failures:
            self._window.set_status("finished with failures")
        elif scoreboard.complete:
            self._window.set_status(f"finished in {scoreboard.wall_seconds:.1f}s")
        else:
            self._window.set_status("finished (partial)")
        return False

    def _on_worker_failed(self, message: str) -> bool:
        """主循环回调：把 worker 的意外异常显示出来并解除运行态。"""
        self._running = False
        self._window.set_running(False)
        self._window.reset_progress()
        self._window.set_status(f"error: {message}")
        return False

    def _current_settings(self) -> UserSettings:
        """读取磁盘上的当前偏好，用于"只改一项、保留另一项"的保存。

        前置条件: 在 UI 线程调用。
        后置条件: 返回 SettingsStore.load() 的结果；文件损坏时返回空偏好
                   （本次保存即修复该文件，另一项回到"未设置"）。
        禁止: 改用 self._config 拼偏好——那里可能被 ARENA_* 环境变量覆盖过，
              把环境值写进磁盘会让"临时覆盖"变成持久设置（ADR-0027 决策 4）。
        异常: 无（SettingsError 在此收口为"空偏好"）。
        """
        try:
            return SettingsStore(self._config.settings_path).load()
        except SettingsError:
            return UserSettings()

    def _report_models_dir(self) -> None:
        """扫描当前模型目录，把结果写进状态行。

        前置条件: 在 UI 线程调用；self._config.models_dir 可指向不存在目录。
        后置条件: 有模型时显示数量与目录；没有模型时显示"该目录没有 .gguf"，
                   让人能区分"目录选错了"和"文件还没放进去"。
        禁止: 在此处自动选中第一个模型——空目录不是"替用户做决定"的理由，
              未选就是未选，START 会明确要求先选。
        异常: 无（目录不可读时 ModelCatalog 返回空列表）。
        """
        models = self._catalog_models()
        if models:
            self._window.set_status(f"{len(models)} 个模型 · {self._config.models_dir}")
        else:
            self._window.set_status(f"模型文件夹中没有 .gguf 文件: {self._config.models_dir}")

    def _catalog_models(self) -> list[Model]:
        """读取本机模型清单（供选择对话框使用）。

        前置条件: 无。
        后置条件: 返回模型目录里的全部模型，按展示名排序。
        禁止: 在此处按文件大小过滤——超过显存的模型会自动部分卸载，不是"点了就崩"的选项（ADR-0019）。
        异常: 无。
        """
        return ModelCatalog(self._config.models_dir).list_models()


class _UiSink(ProgressSink):
    """把 core 的进度事件投递到 UI 线程的进度汇。

    线程纪律：on_stage/on_progress 可能在 worker 线程被调用，因此只做 GLib.idle_add 投递，
    绝不直接触碰控件；is_cancelled 是纯查询，可被任意线程调用。
    """

    def __init__(self, session: ArenaSession, controller: ArenaController) -> None:
        self._session = session
        self._controller = controller

    def on_stage(self, kind: TestKind, stage: Stage) -> None:
        """投递阶段变化到主循环。

        前置条件: 无（可能在 worker 线程被调用）。
        后置条件: 事件已排入主循环队列，由 apply_stage 在主循环线程渲染。
        禁止: 直接调用 set_status 等控件方法——跨线程改控件是数据竞争（ADR-0008）。
        异常: 无。
        """
        GLib.idle_add(self._controller.apply_stage, kind, stage)

    def on_progress(self, kind: TestKind, fraction: float | None, message: str) -> None:
        """投递进度到主循环。

        前置条件: fraction 为 0.0~1.0 或 None。
        后置条件: 事件已排入主循环队列，由 apply_progress 渲染。
        禁止: 直接触碰进度条控件。
        异常: 无。
        """
        GLib.idle_add(self._controller.apply_progress, kind, fraction, message)

    def is_cancelled(self) -> bool:
        """查询会话级取消信号（唯一事实来源在 ArenaSession）。

        前置条件: 无（可被任意线程调用）。
        后置条件: 返回会话当前的取消状态，无副作用。
        禁止: 自行维护取消标志——那会与 Session 的 token 出现两个事实来源。
        异常: 无。
        """
        return self._session.cancelled


def build_controller(config: AppConfig, session: ArenaSession, window: ArenaWindow) -> ArenaController:
    """装配控制器并完成绑定。

    前置条件: window 已构建；session 已注入 store/archive。
    后置条件: 返回已 bind() 的控制器。
    禁止: 无。
    异常: 无。
    """
    controller = ArenaController(config, session, window)
    controller.bind()
    return controller
