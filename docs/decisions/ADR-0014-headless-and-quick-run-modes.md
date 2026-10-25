# ADR-0014：headless 与 quick 两种非 GUI 运行方式

- **状态**：Accepted（决策一于 2026-10-17 修订）
- **日期**：2026-10-16
- **相关 ADR**：ADR-0008（线程桥）、ADR-0010（对比）、ADR-0013（配置）、
  ADR-0017（环境性 CUDA OOM）、ADR-0018（负载档与不提供参数面板）

## 背景（Context）

GUI 是最终交付形态，但开发和验证过程需要满足两类需求：

1. **自检/回归**：验证「命令拼对了吗、解析器还对得上工具输出吗、历史写入了吗」——
   这些都不需要图形环境，也不该被显示服务器绑住（受 ADR-0008 影响，GUI 路径还牵扯线程）。
2. **批量跑图**：给多个模型排队跑，然后比对——在 GUI 里点 N 次既慢又不可脚本化。

同时，真实参数下一轮 Run 是分钟级，开发期每次验证都跑真实参数会让迭代慢到不可接受。

## 决策（Decision）

**决策一：`--headless` 提供无界面运行路径，且一次只跑一个模型。**
`run_arena.run_headless()` 用 `ConsoleSink`（把阶段与进度打到 stdout）替代 `_UiSink`，
其余装配（Runner/Store/Archive）与 GUI 完全共用 `build_session()`。
一条命令跑**一个**模型（`--model`，必须且只能给一个）→ 落盘 → 打印记分板 → 打印对比表。
退出码：`0` 全成功；`1` 有 Trial 失败；`2` 参数或环境错误
（未给 `--model` 或给了多个都走 `2`，见 `run_arena.selected_model`）。

**决策二：`--quick` 提供"数十秒级"极小负载档。**
`AppConfig.quick()` 只改负载：bench `-p 64 -n 16 -r 1`、perplexity `2 chunks`、答卷 `-n 64 -c 8192`；
它同时把 `profile` 标成 `quick`，因此其记录不会进入对比表（2026-10-17 起，见 ADR-0018）。

**决策三：quick 的数字禁止用于模型对比结论。**
2026-10-17 起，这条禁令**不再依赖文字提醒**：`ComparisonBuilder` 只收 official 档记录，
quick 结构性进不去对比表（ADR-0018）。约束同时留在 `AppConfig.quick()` 与 `run_headless()` 的 docstring 里。

**决策四：GUI 之外一律不 import gi。**
`run_arena` 顶部不导入 GTK；`run_gui()` 内部延迟导入 `arena.ui.*`，
使 headless 自检不受显示服务器与 PyGObject 影响。

## 理由（Rationale）

- 没有 headless 路径，就只能"用眼睛看 GUI"来验证解析器——这既不可重复，也无法在无显示器
  的会话里进行（本项目正是 WSL2 + WSLg）。
- 没有 quick 档，每次验证要等几分钟；quick 档把"命令拼写错误""正则失配"这类问题
  在 20~30 秒内暴露，这正是 tracer bullet 的用途。
- 把"quick 数字不可比"写成明文禁令，是因为这类数字**看起来**完全正常
  （1318 t/s、PPL 13.04…），不写清楚一定会被误用。2026-10-17 起这条禁令升级为结构过滤（ADR-0018），
  因为实测证明「写清楚」仍拦不住：quick 记录会按"最新值取胜"顶掉真实档成绩。
- 背景里第 2 条「批量跑图」在 2026-10-17 被显式放弃（见「后果」的决策一修订）：
  它的收益是省几次调用，代价是让「第二个模型整轮失败」成为一个看起来像模型问题的现象。
  两者相权，宁可多调用几次。留在背景里而不是删掉，是因为"为什么当初想要批量"仍是理解本 ADR 的上下文。

## 后果（Consequences）

- 代价：两种档位意味着两套参数，历史里会混着两类数字。缓解：记录里带档名（`profile`）与参数
  （`payload` 含 `n_prompt`/`reps`/`n_ctx` 等），且 2026-10-17 起 quick 记录结构性地不进对比表
  （ADR-0018）；正式基线必须用真实档重跑。
- 代价（2026-10-17 修订，决策一）：**一轮进程只跑一个模型**。原因是实测 F14——真实档连续跑两个模型时，
  第二个模型的三个 Trial 全部挂在 `cudaMalloc failed: out of memory`，而 `nvidia-smi` 同时报
  10688 MiB free、无 compute app，约 10 分钟后自行恢复。也就是说前一个模型退出后显存**没有立即**归还给
  主机侧，同一进程内接着跑下一个模型必然踩到。代价是「一次点一下、跑 N 个模型要点 N 次」，
  以及「批量跑图」这个原始动机被放弃——换来的是「后面的模型整轮失败」这类假失败不再发生。
  真正想要批量，应在外层用 shell/脚本逐个调用本程序（每次是独立进程，显存有完整的释放机会），
  而不是让本程序自己在进程内排队。
- 代价（2026-10-17 修订）：`ConsoleSink` 不再恒返回"未取消"——**Ctrl-C 被显式实现为取消**
  （`request_cancel()` + `run_headless` 里的 SIGINT 处理函数）。原因是 ADR-0002 的进程组修订
  让终端信号不再自动落到子进程上；现在语义与 GUI 的 CANCEL 一致：终止当前子进程、
  保留已完成记录、退出码 1。
- 收益：GUI 与 headless 共用同一条核心路径（`build_session` → `ArenaSession.run`），
  因此在 headless 上验证过的解析/落盘逻辑，在 GUI 里不会变成另一套行为。

## 相关代码位置（Code Locations）

- `run_arena.py::run_headless`（安装 SIGINT 处理函数）、`selected_model`（"恰好一个模型"的守卫）、
  `run_gui`、`build_parser`、`main`、`ConsoleSink.is_cancelled`、`ConsoleSink.request_cancel`、
  `format_scoreboard`、`format_comparison`
- `arena/config.py::AppConfig.quick`、`AppConfig.profile`
- `arena/sink.py::NullSink`（单测用空实现）
- `arena/session.py::ArenaSession.run`（GUI 与 headless 的共同入口）
