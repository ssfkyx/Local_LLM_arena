# ADR-0021：跑批生命周期、逐卷持久化与协议标记

- **状态**：Accepted
- **日期**：2026-10-17
- **相关 ADR**：ADR-0002（子进程与进程组）、ADR-0005（JSONL 历史）、ADR-0008（worker 线程与 UI）、
  ADR-0009（不判分）、ADR-0010（对比表取值规则）、ADR-0014（headless 模式）、
  ADR-0017（失败如实记录，不重试/不降级）、ADR-0018（负载档与不提供参数面板）、
  ADR-0020（完成条件取代 token 上限）

## 背景（Context）

ADR-0020 解除了 token / chunk 上限之后，**单次运行的时长不再由参数封顶**。同一模型同一真实档实测：

| 子项 | 旧口径（有上限） | 新口径（放开） |
|---|---|---|
| PPL | 4.2 s | 123.1 s |
| EXAM（2 卷） | 68.5 s | 378.1 s |
| 其中单卷 paper_1 | 35.4 s | 272.6 s |

也就是说：软件的定位从"跑得快"变成了"跑得久"。而当时的实现有三处与"跑得久"直接冲突：

1. **退出路径不安全**：`ArenaWindow` 直接 `connect("destroy", Gtk.main_quit)`，worker 线程是 `daemon=True`，
   且**只有 headless 注册了 SIGINT**。于是"关窗口"的真实行为是：主进程退出 → worker 被砍 →
   **子进程（独立会话）继续满载占显存**，而在飞那一卷的产物一个字都没留——与用户"不对就关掉它"的期待正好相反。
2. **EXAM 以整轮为成败单位**：`run()` 见到任一 Outcome 失败就整条记录 `ok=false` 且 `sheets=[]`，
   于是"第二卷超时"会连带抹掉"第一卷已经答完的答卷文件与记录"。
3. **跨协议数字同表**：`ComparisonBuilder` 只按 `model_path` 分组、只按 `profile` 过滤，记录里没有协议字段。
   改口径后（8 chunk → 全语料、`-n 1024` → `-n -1`），旧数字与新数字会在同一张 `--compare` 表里并列——
   这不是隐患，而是**当时正在发生的事实**（30B 那行是旧协议，两个 8B 是新协议）。

用户的使用模型很明确：**"崩了就直接关窗口"应当是通用逃生门**；同时明确**不做**断点续跑，
并接受**"在飞的那一卷允许丢，但已完成的不许丢"**。

## 决策（Decision）

1. **退出路径统一为一条收尾路径**：`window.delete-event → controller.shutdown()`，其中
   `shutdown()` = 请求取消 → `_pump_events()` 让"cancelling…"可见 → **有界等待 worker（12 s）** → 超时则返回 False
   并如实显示"收尾超时"（子进程由 Runner 的 `killpg` 负责收干净）。
   headless 侧把 **SIGINT / SIGTERM / SIGHUP** 一律映射成"取消"；GUI 侧注册 `atexit(controller.shutdown)` 兜底。
2. **EXAM 逐卷落盘 + 部分成功**：每一份试卷的子进程一结束就写 `answers/*.md`；
   `TrialExecution` 携带已落盘的 `sheets/answer_paths/paper_failures`；
   `ok` 的语义仍是"整轮无失败"，但**失败记录的 payload 里保留已完成的那几卷**，
   未跑成的卷逐条记 `PaperFailure(paper_name, reason)`（`用户取消` / `执行超时` / 退出码 / 抽取失败）。
   抽取失败也从"整轮崩"降级为"这一卷没成"。
3. **协议标记 `runs/PROTOCOL` + 对比表告警**：文件两行（`name=<PROTOCOL_NAME>`、`since=<UTC 秒级>`），
   由装配期 `session.register_protocol()` 幂等登记；协议名未变时不刷新 `since`。
   对比表（CLI 与 GUI 各一处）在存在早于 `since` 的 official 记录时显示告警。
   **协议变更 ⇒ 归档并重置机读历史**——这是人的动作，代码只负责把旧记录标出来，不做 schema 迁移。
4. **长跑的存活信号与事实可见**：EXAM 上报 `卷 k/N · 已用 Xs · 最后输出 Ys 前`（`fraction=None`，
   即"不可量化"，绝不编造百分比，逐 token 输出按 1 s 节流）；答卷文件头新增 `答卷字符数`。
5. **明确不做**：断点续跑/恢复、无人值守自动重试、JSONL 原子写、机读 `finish_reason`
   （`llama-cli` 页脚只报吞吐、不报停止原因，没有可靠信号源）。

## 理由（Rationale）

- **逃生门只有在"关掉之后真的干净"时才成立。** 关窗 = 取消 + 有界等待 + `killpg`，
  把"用户的期待"变成"代码的行为"；有界（12 s）而不是无限等待，是因为主进程卡死比留下一条失败记录更糟。
- **逐卷落盘让"允许丢"有边界。** 既然不做续跑，任何丢失都等于重跑；因此把持久化粒度从"整轮 EXAM"
  降到"单份试卷"，把最坏损失从 2 小时压到 1 卷。实测：paper_2 仍在生成时，paper_1 的答卷已在磁盘上。
- **进度不可量化时就说不可量化。** EXAM 卷内没有可靠进度源，`fraction=None` + "最后输出距今"
  既给出了"还活着"的证据，又不编造百分比（与 ADR-0016"值列只放事实"同源）。
- **用纪律换迁移成本。** 记录里没有协议字段，改用 `PROTOCOL` 分界线 + 告警：省掉一次 schema 变更与历史回填，
  代价是"改口径时记得重置历史"这条纪律，以及一个 20 行的机械保险。这与 ADR-0018 偏好"结构保证"的取向不同，
  是刻意接受的取舍（历史是可重跑再生的事实序列，不是需要迁移的资产）。

## 后果（Consequences）

### 实测证据（2026-10-17/18）

| 场景 | 结果 |
|---|---|
| quick 回归（隔离产物目录） | exit 0 · 26.2 s · 2 份答卷 · 存活信号 `卷 2/2 · 已用 7s · 最后输出 0s 前` · `PROTOCOL` 已生成 |
| **第二卷进行中向 python 发 SIGTERM** | 1 s 内退出 · 打印 `answers: 1 份` + `failure: exam: 用户取消` · 记录 `ok=False, sheets=['paper_1'], paper_failures=[{paper_2, 用户取消}]` · **无残留 llama 进程** |
| 逐卷落盘的时机 | paper_2 仍在生成时，`answers/*__paper_1.md`（224 B）已在磁盘上 |
| GUI 关窗（`delete-event` → `shutdown`） | `delete-event` 返回 False（允许销毁）· cancel 调用 1 次 · 0.30 s 内 join 到 worker 退出 |
| 关窗但 worker 不合作 | `shutdown()` 0.30 s 返回 False（宽限期压到 0.3 s 的构造测试），不阻塞主进程 |

### 代价与残余限制（必须如实说明）

- **SIGKILL / 断电 / 父 shell 被强杀时，子进程仍会残留**，这是本方案**没有覆盖**的：登记的三个信号
  （SIGINT/SIGTERM/SIGHUP）之外，Python 无法执行任何清理代码。这不是推测——做本轮验证时误把
  `pgrep -f` 匹配到了 bash 包装进程并杀了它，结果 **python 与 llama-cli 都继续运行**，
  必须再对 python 补一次 SIGTERM 才收干净。要覆盖这一档需要 `prctl(PR_SET_PDEATHSIG)`，
  而它只能通过 `preexec_fn` 设置，Python 官方明确警告其在多线程程序中不安全（本程序正是多线程），
  因此**刻意不做**，改为如实声明 + 用户可用 `pkill -f llama-` 收尾。
- 提交式关闭（关窗）最多阻塞主循环 12 s；超时后如实显示"收尾超时"，不假装已停。
- 失败记录现在会带 `load` 与部分 `sheets`：读历史时要理解"`ok=false` 不代表什么都没产出"。
- `PaperFailure.reason` 缺席时会让读取方告警跳过整行；新增字段对旧记录缺席是容忍的（解码默认空列表）。
- 协议告警是**文本告警而不是拒绝查询**：旧记录仍会出现在表里，靠人读那一行；`PROTOCOL` 被删会
  以"当前时刻"重写 `since`，于是历史全部被标为旧协议（宁可误报，不放过）。
- 未验证：GUI 关窗路径用构造测试验证了接线与有界等待，**没有**在真实模型运行中用鼠标点过 ×；
  3.0 秒级以上的真实"卡住"分支也未实测。

## 相关代码位置（Code Locations）

- `arena/vocabulary.py::PROTOCOL_NAME`、`PROTOCOL_FILENAME`
- `arena/store.py::ProtocolMarker`、`AnswerSheetWriter._render`（答卷字符数）
- `arena/domain.py::PaperFailure`、`AnswerSheet.chars`、`ExamRecord.paper_failures`
- `arena/history.py::protocol_warning`、`ScoreboardAssembler.build`（部分成功照实计数）
- `arena/session.py::ArenaSession.register_protocol`、`ArenaSession.protocol_warning`
- `arena/runners.py::TrialExecution`、`TrialRunner.run`、`TrialRunner._failure`、`TrialRunner._new_record`
- `arena/runners.py::ExamRunner._execute`、`_report_alive`、`_sheet_for`、`_build_record`、`_new_record`
- `arena/ui/window.py::ArenaWindow._on_delete_event`、`ArenaWindow.bind`
- `arena/ui/controller.py::ArenaController.shutdown`、`_pump_events`、`show_comparison`
- `arena/ui/views.py::_TableDialog.set_notice`、`ComparisonView.present_rows`
- `arena/theme/arena.css`（`.arena-dialog-notice`）
- `run_arena.py::build_session`、`format_comparison`、`run_headless`（三个信号）、`run_gui`（atexit）
