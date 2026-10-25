# ADR-0017：环境性 CUDA OOM 只如实记录，不自动重试、不自动降级

- **状态**：Accepted
- **日期**：2026-10-17
- **相关 ADR**：ADR-0005（失败也进历史）、ADR-0012 / ADR-0015（显存约束）、ADR-0011（记录以工具自报为准）

## 背景（Context）

2026-10-17 00:30 起实测到一组互相矛盾的事实（同一台机器、同一份二进制、同一批模型）：

| 时刻（本地） | 操作 | 结果 |
|---|---|---|
| 00:30–00:32 | 应用真实档跑 Meta-Llama-3.1-8B-Q8_0（8.5 GiB）：bench `-p 512 -n 128 -r 3`、perplexity 8 chunks、exam `-n_ctx 16384 -n 1024`（当时的真实档口径，现为全语料 / `-n -1`，见 ADR-0020） | **成功**（pp 4121.1 t/s · tg 46.2 t/s · PPL 7.8425） |
| 00:32–00:34 | 紧接着跑 Qwen3-MOE-2x4B-8B-Q8_0（6.6 GiB） | **三个 Trial 全部失败**：`cudaMalloc failed: out of memory`（尝试分配 6802 MiB） |
| 00:34 | 单独重跑 Qwen3-MOE | 仍然失败 |
| 00:46 | 直接调 `llama-bench`（同模型、极小负载 `-p 32 -n 8`） | **成功**（tg 41.1 t/s） |
| 00:39 / 00:47–00:50 | 应用真实档与直接调用 `llama-bench`（6.6 GiB 与 8.5 GiB） | **全部失败**，而 `nvidia-smi` 同时报告 10688 MiB free、无 compute app |

结论：**失败与代码路径无关**（直接 CLI 与应用同样失败），而与「此刻主机/驱动是否愿意给出 ≥6.8 GiB 连续显存」有关。
WSL2 的 `nvidia-smi` 报告的是设备视角的空闲量，看不到 Windows 主机侧对同一块 GPU 的占用，
因此"报告空闲但 `cudaMalloc` 失败"是会真实发生的状态。

## 决策（Decision）

1. **不自动重试**：模型加载失败（含 CUDA OOM）按 `ok=false` 记录一次，不做"等一会儿再来一遍"的内部重试。
2. **不自动降级**：不因为一次 OOM 就偷偷改 `-ngl` / `-ncmaxoe` / 批大小——那会产出**参数不同、却记在同一个模型名下**的数字，
   直接破坏 ADR-0010 的可比性前提。
3. **如实记录并让用户看见**：失败原因（含工具原文尾部）进 `TrialRecord.error`，同时进 `Scoreboard.failures`，
   屏幕上显示 `失败: bench: 退出码 1: …`，`--headless` 退出码为 1；原始 stderr 落在 `runs/<run_id>/<tool>.err`。
4. **重试由人在外面做**：需要重试时重新执行一次 Run（新 `run_id`），历史上两次尝试都留着——
   于是"这次为什么没数字"永远可查。
5. **失败不污染已有成绩**：`ComparisonBuilder` 只采纳 `ok=true` 的数值（ADR-0010），因此一次 OOM 不会把该模型的对比行清成 `—`。

## 理由（Rationale）

- 自动重试会让"一分钟的失败"变成"几分钟的失败"，而且在环境持续劣化时照样失败——它掩盖问题、不解决问题。
- 自动降级最危险：数字看起来完整，却与其它模型/其它轮次的参数不一致；用户拿它做选型结论会出错。
  参数要变，必须由人显式改（`AppConfig`）并重跑，让历史里留下"换了参数"的事实。
- 本项目已经把"失败"设计成一等公民（ADR-0005）：既然失败能被完整记录，就没有必要把它藏起来。

## 后果（Consequences）

- 代价：环境性 OOM 需要用户自己重跑；应用不会"自己好起来"。
- 代价：`--headless` 不带 `--model` 连续跑多个模型时，若环境在两次之间劣化，后面的模型会整轮失败
  （实测正是如此）。缓解办法是让用户能看见 `skip:`/`failure:` 行，并按需单模型重跑
  （`--model <名字>`），而不是让应用猜测。
- 收益：任何一轮 Run 的数字与参数、失败与原因都在历史里自洽：`ok=true` 的记录一定伴随完整原始流。
- 收益：实测证明失败路径在生产条件下可用——三个 Trial 全部失败时进程正常退出、历史可读、界面不假死。

## 相关代码位置（Code Locations）

- `arena/runners.py::TrialRunner.run`（失败转记录，不抛异常）、`TrialRunner._failure`、`TrialRunner._exit_error`
- `arena/runners.py::TrialRunner._run_process`（失败也落盘原始流）
- `arena/history.py::ScoreboardAssembler.build`（失败进 failures，不进数值）
- `arena/history.py::ComparisonBuilder.build`（只采纳 ok 记录）
- `arena/ui/views.py::ScoreboardView.render`（失败行显示）
- `run_arena.py::run_headless`（退出码 1）、`format_scoreboard`（`failure` 行）
- `arena/runners.py::TrialRunner._fit_args`、`TrialRunner._base_record_kwargs`
  （显存适配取代静态排除；运行期 OOM 仍只记录，见 ADR-0019）
