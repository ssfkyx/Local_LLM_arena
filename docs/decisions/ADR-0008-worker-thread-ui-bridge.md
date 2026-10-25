# ADR-0008：UI 线程只画图，试炼在 worker 线程

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0003（模块边界）、ADR-0007（主题）、ADR-0014（headless）

## 背景（Context）

实测（tracer F11）：quick 档全流程 24.4 s；真实档（bench `-p 512 -n 128 -r 3`、
perplexity 8 chunks、答卷 `-n 1024`）将达**分钟级**。
（该组数值是当时的真实档口径；PPL 与答卷的负载随后由 ADR-0020 改为"全语料 / 写到自己停"，
结论只会更强——后台线程 + 可取消 + 进度回报这一条与负载数值无关。）

若在 GTK 主循环里直接跑 Trial：
- 窗口在此期间不重绘（假死），
- 「取消」按钮点不动（而取消恰恰是最需要能点的时刻），
- 进度永远只能等结束才一次性出现。

同时取消信号有两个来源（用户点圆键、未来的快捷键/脚本），必须只有一个事实来源。

## 决策（Decision）

1. **线程分工**：UI 线程只做构建与渲染；一轮 Run 跑在 `threading.Thread(daemon=True)` 里
   （`ArenaController.start` → `_run_worker`）。
2. **跨线程只传值，不碰控件**：worker 里的进度事件经 `_UiSink` 用 `GLib.idle_add` 投回主循环，
   由 `ArenaController.apply_stage` / `apply_progress` 在主循环线程更新界面。
3. **取消只有一个事实来源**：`ArenaSession.cancelled`（内部 `CancellationToken`，
   基于 `threading.Event` 提供跨线程内存可见性）。`_UiSink.is_cancelled()` 只是转发它。
4. **UI 不可重入**：运行中 `START` 变 `CANCEL`（`set_running` 切换 `.running` 类），
   选择键被 `set_sensitive(False)` 禁用；`ArenaController.start/choose_model` 再各拦一道。
5. **worker 的未捕获异常必须回到界面**：`_run_worker` 里捕获一切并 `idle_add(_on_worker_failed)`——
   否则按钮会永久卡在 CANCEL 状态（比崩溃更糟：用户以为还在跑）。
6. **终态归零**：进入 `TERMINAL_STAGES` 时进度条归零；`fraction=None` 走 `pulse()`
   而不是当成 0（0 会被读成"卡住了"）。

## 理由（Rationale）

- GTK3 不是线程安全的：任何 `set_text`/`set_label` 都可能触发重绘与信号，跨线程调用是数据竞争。
  只允许「创建时构建控件、主循环里改文本」这条单向路径，是把竞态压到零的最省做法。
- 取消用 `threading.Event` 而非布尔量：Session 在 UI 线程 `cancel()`，Runner 在 worker 线程
  轮询 `is_cancelled()`，需要一个内存可见性保证。
- 把「唯一的取消来源」放在 Session，而不是 UI 各存一份，避免「UI 以为取消了、Runner 还在跑」。

## 后果（Consequences）

- 代价：需要「UI 不可重入」纪律——一旦允许并发 Run，token/进度/记分板都会互相覆盖。
  纪律由上面第 4 条双重拦截保证。
- 代价：`SubprocessRunner` 的 `on_stdout_line` 回调在**读取线程**里被调用，回调实现必须自己
  切回主循环（`_pump` 会吞掉回调异常，保证取数不被进度回调拖累）。
- 收益：窗口在分钟级测试期间保持响应，进度条按 chunk 实时推进，取消能在一个轮询周期
  （0.2 s）内终止子进程，已完成的 Trial 记录仍然保留（见 ADR-0005 的「先落盘再继续」）。

## 相关代码位置（Code Locations）

- `arena/ui/controller.py::ArenaController.start`、`_run_worker`、`_on_worker_finished`、
  `_on_worker_failed`、`apply_stage`、`apply_progress`、`_UiSink`
- `arena/ui/window.py::ArenaWindow.set_running`、`set_progress`、`reset_progress`
- `arena/session.py::ArenaSession.run`、`cancel`、`cancelled`、`_token`
- `arena/process.py::CancellationToken`、`SubprocessRunner.run`（`cancel_check` 轮询周期）
- `arena/sink.py::ProgressSink`（实现者必须自证线程安全）、`TrialContext.cancel_check`
- `arena/vocabulary.py::TERMINAL_STAGES`
