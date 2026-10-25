# ADR-0026：题内进度来自 `llama-server` 自己的日志（心跳，不改请求结构）

- **状态**：Accepted
- **日期**：2026-10-19
- **相关 ADR**：ADR-0004（解析器跟随流语义）、ADR-0006（单调计时）、ADR-0008（worker 线程与 UI 的桥）、
  ADR-0011（答卷只记吞吐）、ADR-0017（失败如实记录）、ADR-0020（完成条件=模型写完）、
  ADR-0021（跑批生命周期与协议标记）、ADR-0024（进度条内联文本）、
  ADR-0025（逐题 EXAM，其决策 11「不做流式 token 进度」由本 ADR 修订）

## 背景（Context）

2026-10-19 09:43 那一轮真实档（`runs/2026-10-19T09-43-52_Qwen3-30B-A3B/`）的 EXAM 停在
`paper_1` 第 15 题（`C5｜发现隐藏错误`）：进度条最后一行是 `卷 1/2 · 题 15/30 · 已用 974s`，
用户在"时间一直卡在 974s、又等了几分钟还在同一题"的情况下判定**程序卡死**，按了 CANCEL。

事后从那一轮的 `server.log` 逐行重建，事实与"卡死"相反：

| 事实 | 数值 | 证据 |
|---|---|---|
| 第 15 题（task 27891）何时开始 | server 启动后 **974.7 s**——正是屏上那个冻住的 "已用 974s" | `server.log` 的 `launch_slot_` 行 |
| 它到取消为止做了什么 | **93 行** `slot print_timing`，`n_gen` 从 107 **单调涨到 8116**，`tg` 稳定 **28.9 t/s** | `server.log`；回放 `docs/tracer/f39-log-replay.out` |
| 取消发生在 | server 启动后 1257.6 s（题内 281.5 s） | `server.log` 的 `cleaning up before exit` |
| 记录 | `ok=False / error='用户取消' / paper_failures=[{paper_1, "C5: 用户取消"}] / sheets=[]` | `runs/arena_history.jsonl` 末行 |

也就是说：**它一直在解码，只是 UI 一个字都没有再更新**。同一个冻住的 "已用 974s"，在
"真的卡死"与"还在正常生成"两个世界里的样子完全一样。

结构性原因是 ADR-0025 自己写下的两条限制的合力：
1. 问答请求**非流式**（`stream` 不传）→ 响应返回前客户端手里没有任何题内事实；
2. `_report_alive` 只在**题开始时**发一帧 → "最小进度粒度=题"，长题期间屏幕静止。

用户给出的硬约束是：**不要靠跑这个模型来验证**（30B 真实档会拖垮本机 WSL2）。因此本决策的
证据必须全部来自离线资产——既有 `server.log` 与假 `llama-server` 探针。

## 决策（Decision）

1. **心跳**：`ChatCompleter.complete` 在既有的"取消轮询循环"里每 `_HEARTBEAT_SECONDS = 10 s`
   调用一次调用方给的 `on_heartbeat`。**不新增线程**，也**不改请求结构**：仍是
   `POST /v1/chat/completions`、单条 user 消息、`max_tokens=-1`、`temperature=0.0`、`seed=42`、
   `cache_prompt=false`。心跳只改变"屏幕上写什么"，不改变"被测的是什么"。
2. **取数来自 server 自己的日志**：`llama-server` 每约 3 秒往 `server.log` 写一行
   `slot print_timing: id 0 | task N | n_gen = X, tg = Y t/s`。这是**现成的、不改被测行为**的
   题内事实源，比"把请求改成流式"便宜得多，也不触碰"同参数可比"。
   - 行解析归 `arena/parsers.py::ServerTimingParser`（本仓库唯一允许出现正则的地方，ADR-0004）；
   - 增量读取归 `arena/server.py::ServerLogTail`：**字节偏移**（日志是中英混排的 UTF-8，
     按字符回退偏移会错位），只消费**完整行**（末尾半行不前进偏移，下次连起来读）。
3. **读取位置在请求发出前对齐到文件末尾**（`ServerLogTail.begin()`）→ 读到的统计行只可能属于
   本题，不会把上一题的数字算进"本题 N tok"。
4. **文案**（`ExamRunner._report_alive`）：
   - 题开始那一帧**保持原样**：`卷 k/N · 题 m/M · 已用 Xs`；
   - 题内出现统计行后追加事实：`· 本题 N tok · R t/s · 日志 As 前`。
   `A` = "日志最后一条统计行距今多少秒"，由 `perf_counter` 现算（ADR-0006），**不是**判断。
5. **心跳回调异常一律吞掉**（`ChatCompleter._beat`）：与 `SubprocessRunner._pump` 对行回调的口径
   一致——进度是旁路，出错绝不能把正在进行的测量带崩。
6. **不改 `PROTOCOL_NAME`、不归档历史**：请求结构、采样参数、完成条件都没变，变的只是可见性。
   因此不触发 ADR-0021 的"协议变更 ⇒ 归档重置"。
7. **明确不做**：
   - 不把请求改成流式（那会改请求结构，正是 ADR-0025 决策 11 拒绝的东西，且破坏同参数可比）；
   - 不编造题内百分比（`fraction` 仍为 `None`，唯一的真实比例仍然只有 PERPLEXITY 的 chunk 比例，ADR-0024）；
   - 不因心跳做任何自动中止或重试：`A` 变大只是"日志 N 秒没有新统计行"，是不是真的卡住由人判断。

## 理由（Rationale）

- **用户要的是一个二值问题的答案："它还在动吗"**。事后看，这个答案一直存在（server 每 3 秒写一行），
  只是没有任何一条路径把它送到屏幕上；补上这条路径，就不需要在"等"与"杀"之间凭感觉选。
- **为什么不打开流式**：流式会改请求结构，落在"同参数可比"之外；而 server **已经**在写同一份事实。
  用"读它自己的日志"换"改请求"，是这次取舍的全部。
- **为什么是 10 秒**：人类读得出数字在动的最粗量级；真 server 的统计行间隔约 3 秒，因此正常情况
  下每个心跳都能带上新数字，卡住时则表现为"数字不动而秒钟在跑"。
- **为什么把"日志 As 前"一起报**：只看"本题 N tok"停止增长，人还得记住上一次的数字；
  把"距最后一条统计行多久"直接写出来，卡住档就有了可读形态——与 ADR-0024 的"最后输出 3s 前"
  是同一套语言（那一档来自子进程 stdout，逐题 EXAM 之后没有了，本 ADR 用日志把它补回来）。
- **先有证据再改架构**：探针与回放都是离线可复现的（假 server + 真 `server.log`），
  因此"没有跑模型"不等于"没有验证"。

## 后果（Consequences）

### 实测证据（2026-10-19，全部离线；原始输出见 `docs/tracer/f39-*`）

| 场景 | 结果 | 证据 |
|---|---|---|
| **假 server 端到端**（探针 `/tmp/arena_progress_probe.py`，心跳压到 0.5 s 以便几秒看完） | **15 项断言全过**：正常题 `已用 1s · 本题 14 tok · 30.0 t/s · 日志 0s 前` → `28 tok`（数字在涨）；`STALL` 题 `已用 2s→4s` 继续走、`本题 14 tok` **冻结**、`日志 0s→2s 前` **递增** | `docs/tracer/f39-progress-probe.out` |
| 请求结构未被改动 | 假 server 收到的请求体逐字为 `{"cache_prompt": false, "max_tokens": -1, "messages": [单条 user 消息], "seed": 42, "temperature": 0.0}`；每题一个独立请求 | 同上（`C1/C2`） |
| 取消路径不变 | 取消后仍落 `ok=false`、`error='用户取消'`、`paper_failures=[{paper_1, "K1: 用户取消"}]`（**精度到题**；探针的取消点落在第 3 帧，靠心跳计时与请求结束赛跑，偶尔会落在 `K2`——断言只要求"某一题的题号 + 用户取消"）、`sheets=[]`；无残留进程 | 同上（`D1/D1b/D2`） |
| 无产物目录时降级 | 心跳仍刷新，但只带"已用"（没有日志可读，就不报统计，也不崩） | 同上（`D3`） |
| **真日志回放**（`/tmp/arena_log_replay.py` + 事故那一轮的 `server.log`） | 15 次请求 / **402 行**统计全部解析；第 15 题 93 行、末行 **8116 tok / 28.92 t/s**（与 `server.log` 逐字节一致）；同一刻文案从 `卷 1/2 · 题 15/30 · 已用 974s`（冻结）变成 `… 已用 1260s · 本题 8116 tok · 28.9 t/s · 日志 0s 前`（在动）；`ServerLogTail` 从文件头读到 **529 行 = 文件行数**（无重复、无漏行），`begin()` 后读不到历史行 | `docs/tracer/f39-log-replay.out` |
| 文案宽度（离屏真控件树 + 真主题，1020×780） | 进度条 **988×36 px**；旧文案 201 px，最长新文案（`已用 12345s · 本题 16057 tok · 28.9 t/s · 日志 9999s 前`）**451 px**，放得下 | `docs/tracer/f39-progress-width.out` |
| 文档自检与既有口径 | `tools/check_traceability.py` 通过（模块头/四段契约/ADR 双向可达/编号连续/无 TODO）；`tools/check_completion.py` 退出码 0（既有答卷判定不变）；`--compare` 数字与表头不变、无协议告警 | 见文末验收命令 |

### 代价与残余限制

- **题内仍是粗粒度**：10 s 一拍；且 `print_timing` 要等 prompt 处理完才出现，大 prompt 的头十几秒
  仍然只有"已用"在动（比"整题静止"好，但不是逐 token 进度）。
- **依赖上游日志格式**：`print_timing` 字段一旦改名，解析就 miss——此时心跳**退化为"只有已用在动"**，
  既不报错也不误报（这是刻意的：宁可少报，也不要编造一个数字）。行形状钉在
  `arena/parsers.py` 的模块头与探针里。
- **日志的实时可见性未在真模型上盯过**：本轮用假 server（显式 flush）与既有 `server.log` 回放验证；
  按用户要求**没有**在真模型长题上观察过屏（也不打算）。若上游把日志改成全缓冲，
  "本题 N tok"会停在上一次读到的行、"日志 As 前"会持续变大——**这正是这套文案能自我暴露的失效形态**。
- **`n_gen` 是 server 自报的累计值**：心跳不做插值、不折算、不据此判断完成度；它与题末 `Completion.predicted_n`
  是两条独立来源（都来自 server，但一个来自日志、一个来自响应体），本 ADR 不要求二者相等。
- **头几次心跳发生在 `docs/tracer` 之外的真实会话里才有意义**；`--headless` 下 ConsoleSink 会按同样的
  频率打印一行（长题约 10 s 一行），日志体积增加约 6 行/分钟——知情接受。

## 相关代码位置（Code Locations）

- `arena/server.py::ChatCompleter.complete`（`on_heartbeat`）、`ChatCompleter._beat`、
  `ServerLogTail.begin`/`new_lines`、`_HEARTBEAT_SECONDS`
- `arena/parsers.py::ServerTiming`、`ServerTimingParser.parse`
- `arena/runners.py::ExamRunner._execute`（心跳闭包）、`ExamRunner._report_alive`
- `arena/ui/window.py::ArenaWindow.set_progress`（脉冲档条内文字，ADR-0024 的既有路径）
- `docs/tracer/f39-progress-probe.out`、`docs/tracer/f39-log-replay.out`、`docs/tracer/f39-progress-width.out`
