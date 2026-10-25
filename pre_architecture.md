# Local Arena — 前期架构（pre-architecture）

> 阶段：**tracer bullet 已实跑通过 + 代码已实现并回灌** → 本文件描述**当前代码的真实结构**与**实测事实**，不是设想稿。
> 平台：Linux（WSL2 + WSLg，RTX 4080 Laptop 12282 MiB） · Python 3.14.4 · GTK3 (PyGObject, Gtk 3.24) · llama.cpp b11574 / 8bb3cf0d2 (CUDA)
> 证据目录：`docs/tracer/`（原始流） · `docs/ui/`（界面截图） · `runs/`（产物与历史）

---

## 1. 目标（一句话）

选定一个本地 GGUF 模型 → 依次跑 `llama-bench`、`llama-perplexity`、让模型答卷 → 屏幕上给出吞吐与困惑度数字，答卷落盘为文件，结果进历史库并可跨模型对比。
软件的目的是一句话：**在当前硬件下尽量让模型跑满，以此做性能与效率比较。** 因此负载参数是**实验条件**，
只有 `real`（满载档）的数字参与对比，`quick` 仅作自检（ADR-0018）。

**软件边界（2026-10-18 明确）**：GPU 温度与功耗属于**电脑管理系统层面的策略**，由用户在软件外手动调整——
软件不采样、不记录、不干预；跨轮漂移视为"一个可以重复跑的工具天然带有的误差"，不做管理，
只在 §10 如实登记实测区间（30% 与 76%），对比表不承诺跨时间窗口可比（F25）。

---

## 2. Tracer bullet 实测事实（架构的地基）

### 2.1 工具行为（第一手证据）

| # | 事实 | 证据 |
|---|---|---|
| F1 | `llama-bench -o json` 的 **stdout 是纯 JSON 数组**（每条 44 字段）；CUDA 初始化日志只走 stderr | `docs/tracer/bench.json` 可 `json.loads`；`bench.stderr` 160B 含 `ggml_cuda_init` |
| F2 | ⚠ `llama-perplexity` **不是"全部走 stderr"**：逐 chunk 进度串 `[1]8.8652,[2]13.0436,` 走 **stdout**；`Final estimate: PPL = x +/- y` 走 **stderr** | `docs/tracer/ppl.out`(36B) vs `ppl.err`(856B) |
| F3 | perplexity 的 stderr 行带 `秒.毫秒.微秒 I ` 前缀 → 解析必须**行内搜索**而非整行匹配 | `ppl.err` 尾行 `0.03.058.938 I Final estimate: …` |
| F4 | `llama-cli -st` 的 **stdout 是一份 TUI 文字记录**：加载转轮 + banner + 命令帮助 + `> <回显 prompt>` + 答案 + `[ Prompt: … t/s \| Generation: … t/s ]` + `Exiting...`；stderr 为 0B | `docs/tracer/cli.out` / `cli3.out` |
| F5 | `--simple-io` 只去掉加载转轮（`\b` 退格动画），**不去掉 banner/命令帮助/回显/页脚**；`--no-display-prompt` **不抑制** `> ` 回显 → 二者都不采用 | `cli.out`(1090B) 与 `cli2.out`(1090B) 逐字节相同 |
| F6 | ❌ **已被推翻**：原以为"多行 prompt 被逐字回显，回显之后即答案"。实测**只有 ≤500 字节的 prompt 才完整回显** | `cli3.out`（短 prompt 成立）vs 23:14 那次 EXAM 失败记录 |
| F12 | ✅ 修正 F6：`llama.cpp/tools/cli/cli-ui.h:161` 的 `echo()` 在 `buffer.size() > 500` 时只打印 `\n> <前 500 字节> ... (truncated)\n`。**真实试卷（19KB 中文）必然走截断分支** → 抽取器以 `" ... (truncated)\n"` 标记为答案起点 | 源码行 + `runs/2026-10-16T23-14-40_*/transcript-paper_1.out` 含该标记；修复后 23:22 / 23:38 两次 EXAM 均成功 |
| F7 | `llama-bench -p 64 -n 16` 自动展开成**两条独立记录**（pp64 与 tg16），各自计时 → 一次调用即得两类指标 | `bench.json` 记录数 = 2 |
| F8 | Python 内建 `float` 即 **FP64**（`epsilon = 2.22e-16`），`time.perf_counter()` 亦为 FP64 → FP64 约束零成本满足，只需**禁用 numpy/struct 的 float32 路径**（本工程不含 numpy） | tracer 输出 |
| F9 | GTK3 + CSS 在 WSLg 下可用：径向渐变、圆角、`add_provider_for_screen` 外部主题全部生效 | `docs/ui/arena-window.png`（2275×790，渐变与圆键均渲染） |
| F10 | Wayland 下 `Gdk.pixbuf_get_from_window` 截图**空白**；强制 `GDK_BACKEND=x11` 后正常 | 两次截图对比；本次截图脚本固定该变量 |
| F11 | 实测量级：quick 全流程（bench+perplexity+2 份答卷）**24.4 s**；真实参数将达分钟级 → UI 必须后台线程 + 可取消 + 进度回报 | 23:38:56 那轮墙钟 |
| F13 | `llama-perplexity` 自报实际参数 `over N chunks, n_ctx=M`，可能与请求值不同 → 记录以**工具自报**为准 | `ppl.err` 的 `_SETUP_RE` 命中 |
| F15 | 负载档是**落盘事实**：同一次 Run 的三条记录带同一个 `profile`，取值来自进程的 `AppConfig.profile`（白名单 `real`/`quick`），不靠事后从数字推断 | `arena/runners.py::TrialRunner._base_record_kwargs` 盖戳；历史 36 行均可读到档名或按约定读作 real |
| F16 | 历史派生视图按档位分流：`ComparisonBuilder` 只收 official（real）档；`Archive.latest_scoreboard` 优先 official、无 official 时才显示 quick；`HISTORY` 视图照实列全部记录并多一列「档位」 | `--compare` 表头只有 real；`arena/ui/views.py::HistoryView` 列头含「档位」（ADR-0018 决策 4/5） |
| F17 | ✅ **超显存模型可以跑**：三种工具都支持 `-fitt/--fit-target`，按"至少留出 N MiB"自动部分卸载。30B（17697 MiB）在 `-fitt 1024` 下 `llama-bench` 成功（exit 0，25.2 s，峰值 RSS 18.7 GB，pp 124.8 / tg 33.4）；对照纯 CPU `-ngl 0` 仅 pp 48.7 / tg 11.4 | `llama-bench --help` 的 `-ngl` 默认 `-1` 是崩溃根源；`-fitt` 实测三工具皆支持；软件端到端 quick 自检 30B 通过（答卷 2 份，见 §2.5） |
| F18 | 30B 连续三次加载（bench→perplexity→exam）时，**第三次**曾出现 `cublasCreate_v2` 失败（`CUDA error: the resource allocation failed`，退出码 -6）；等待后再跑即通过 | `runs/2026-10-17T18-50-03_Qwen3-30B-A3B/transcript-paper_1.err` 对 `runs/2026-10-17T18-53-02_*` 成功——仍属 F14 的环境性显存回收问题，按 ADR-0017 只记录不重试 |
| F19 | `llama-cli -n` 的原生默认就是 **-1 = infinity**（生成到 EOS 或上下文满）→ 答卷的"完成条件"可以直接是"模型写完"，不需要我们发明上限；实测 `-n -1` exit 0、答案完整、页脚照旧 | `llama-cli --help`；`docs/tracer/cli-n1.out`（答案 `2` + `[ Prompt: 921.9 t/s \| Generation: 34.3 t/s ]`） |
| F20 | `llama-perplexity --chunks` 的原生默认就是 **-1 = all**；实测截断语料（90926 B）`--chunks -1` exit 0，stderr 自报 `over 41 chunks`、`Final estimate: PPL = 7.1239 +/- 0.16640`，stdout `0.17 minutes` → 全语料可跑，且"跑了多少 chunk"由工具自报、可核验 | `llama.cpp/tools/perplexity/perplexity.cpp:493,515`；`docs/tracer/ppl-chunks-all.out/.err` |
| F21 | 全语料自报的 chunk 数是**模型相关**的：同一份 `wiki.test.raw`，Meta-Llama 报 **564** chunk、Qwen-MOE 报 **584** chunk（chunk = 语料 token 数 / n_ctx，分词器不同则 token 数不同）→ `n_chunks` 是"跑到底"的证据，**不是**语料的常数，跨模型比较时不能把它当同一个量 | `runs/2026-10-17T23-27-09_*/perplexity.err` vs `runs/2026-10-17T23-37-44_*/perplexity.err` |
| F22 | 真实档全语料 PPL 的实测墙钟：Meta-Llama **123.1 s**、Qwen-MOE **133.8 s**（8B 级 ≈ 2~2.3 分钟，此前按 pp 外推的 2~3 分钟量级正确）；`-n -1` 后答卷不再被截断——Meta-Llama `paper_1` 由旧口径的 **3716 B** 涨到 **29562 B（8×）**，Qwen-MOE 由 3369 B 到 3695 B | `runs/arena_history.jsonl` 第 55~60 行；`runs/2026-10-17T2{1-51-34,3-27-09}_*/answers/` |
| F23 | **"关掉它"现在真的收得干净**（ADR-0021）：关窗 / SIGINT / SIGTERM / SIGHUP 走同一条收尾路径（取消 → 有界等待 → `killpg`）。实测：第二卷进行中给 python 发 SIGTERM，1 s 内退出、无残留 llama 进程；**逐卷落盘生效**——paper_2 仍在生成时 paper_1 的答卷（224 B）已在磁盘上；记录 `ok=False` 但 `sheets=['paper_1']`、`paper_failures=[{paper_2, 用户取消}]` | `/tmp/arena_v2_partial_runs/arena_history.jsonl`（一次性）；GUI 关窗用构造测试验证接线：`delete-event` 返回 False、cancel 1 次、0.30 s join 到 worker |
| F24 | **残余缺口（如实声明）**：只有登记过的三个信号与关窗会被收尾。**父 shell 被强杀（SIGKILL）时 python 与 llama-cli 都会活下来**——本轮验证时误把 `pgrep -f` 匹配到 bash 包装进程并杀了它，python 与 llama-cli 继续运行，需再对 python 补一次 SIGTERM 才收干净。要覆盖这一档只能靠 `prctl(PR_SET_PDEATHSIG)`，而它必须经 `preexec_fn` 设置，Python 官方警告其在多线程程序中不安全，因此刻意不做 | 本轮验证现场（`kill -TERM` 打错 PID 的那次） |
| F25 | **bench 的跨轮漂移足以翻转模型排名**：同一模型、同一真实档命令，三轮实测——Meta-Llama pp **2957.1 → 2777.1 → 4333.5**，Qwen-MOE pp **2492.0 → 4380.1 → 4325.7**。第一轮 Meta-Llama 更快（2957 vs 2492）、第二轮 Qwen-MOE 反超 1.58×（4380 vs 2777）、第三轮两者几乎持平（4333 vs 4326）。**"谁更快"这个结论在三轮之间变了两次**；`reps>=3` 只能吸收进程内抖动 | 三轮各 6 条 real 记录：`runs/archive/arena_history.pre-adr0021.jsonl`、`runs/archive/arena_history.pre-paper2-v2.jsonl`、`runs/arena_history.jsonl` |
| F26 | **PPL 逐位可复现；答卷在同参数下"通常完全一致、但会分叉一次后稳定"**：三轮全语料 PPL 都是 7.3289 / 14.1946（逐位相同）；答卷方面 Qwen-MOE 的 `paper_1` 三轮**逐字相同**（1536 字符），Meta-Llama 的 `paper_1` 则是 13104 → 14284 → 14284 字符——**后两轮逐字相同，只有第一轮不同**。三轮记录的 `load` 完全一致（`n_predict -1 / n_ctx 16384 / temp 0 / seed 42`），因此这个分叉**无法由记录中的参数解释**（猜测是 GPU 侧数值非确定性，未验证） | 同上；`--temp 0 --seed 42` 是 ADR-0018 记的负载参数 |
| F27 | **新口径下模型能答完整卷**：按真实档答卷逐题核对覆盖度——`-n -1` 的答卷**全部覆盖 30/30**（K1–L5 全在，paper_2 六类各 5 题全在）；而旧口径 `-n 1024` 的 **6 份答卷全部未答完**（正文都在半句上截断；`22-10-37` 的 30B 更是整卷只回了一句开头）。这次核对当时是临时脚本做的，现已变成软件的常规动作（见 F28/ADR-0022） | 旧口径：`runs/2026-10-17T21-51-34_*`、`21-54-45_*`、`22-10-37_*`；新口径：两个 8B 的真实档各两轮（见 F28） |
| F28 | **答卷覆盖度已成机械事实**（ADR-0022）：`paper_2` 题号化（题面 **30/30 逐字未改**，只加标题行；旧版归档 `test_content/real_test/archive/paper_2.v1`）后，两份试卷都能精确提取 30 个题号；EXAM 收尾在答卷头写 `- 题目覆盖: 覆盖 30/30（按题号核对）`，并在进度行报 `答卷覆盖 paper_1 30/30 · paper_2 30/30`（**2026-10-18 12:50 Meta-Llama 与 13:00 Qwen-MOE 的真实档两卷都是 30/30**）；独立工具 `tools/check_completion.py` 对 30 份既有答卷复算得 **完整 6 · 未覆盖 9 · 无法判定 15**，`--current-only`（当前协议 4 份）**全部 30/30、退出码 0**，**无一被误报为未覆盖** | 真实档 `runs/2026-10-18T12-50-12_*`、`runs/2026-10-18T13-00-31_*`；quick 端到端（隔离目录）；`tools/check_completion.py`；题面逐题比对 30/30 一致 |
| F29 | ⚠ **`llama-server` 的裸 `/completion` 不能用于答卷**：不套 chat template 时 instruct 模型不吐 EOS，`n_predict=-1` 会一路吃到上下文满（`stop_type='limit'`、`predicted_n=16346`、墙钟 **380.57 s**、24868 字符重复死循环）；同一 prompt 走 **`/v1/chat/completions`**（套模板）则 `finish_reason='stop'`、`predicted_n=424`、**9.94 s**。且 `llama-cli -st` 的答案（用 `CliTranscriptParser` 抽取的 715 字符）与 `/v1/chat/completions` 的 `content` **逐字一致** → EXAM 若走 server，**必须用 chat 端点**，裸补全违反 ADR-0020 的"完成条件=模型写完" | `docs/tracer/f29-prompt.txt`（155 B）、`f29-server-completion.json`、`f29-server-chat.json`、`f29-cli.out`/`.err`、`f29-server.log` |
| F30 | **逐题独立请求的上下文隔离成立**：探针 Q1（含代号 `ZQ7X`）→ Q2（追问代号、题面**不含**该代号）→ Q1；Q2 回"不知道。"（无泄露），Q1 两次一致。`cache_prompt:false` 时 `cache_n=0`（KV 完全不复用）；不传则 `cache_n≈27~28`，但那是**所有请求共用的 chat 模板前缀**，非题内容泄露。→ 应显式带 `cache_prompt:false`，让"清空"可核验 | `docs/tracer/f30-nocache.json`、`f30-cache.json` |
| F31 | **客户端超时/断开 → server 自动取消该次生成**：5 s 超时断开后 +1 s 仍 `is_processing:true`，+5 s 已 `false`，日志 `W srv stop: cancel task, id_task = 25`（约 1 s 内收尾，不留占显存的任务）；随后短请求 0.26 s 正常返回。`/slots?action=erase` → 404，`/slots/0?action=erase` → 501（需 `--slot-save-path`）——**本方案不需要槽位擦除** | `docs/tracer/f31-timeout.json`、`f30-f32-server.log` |
| F32 | **单 server 进程连续 30 题稳定**（paper_2 全部题目，temp 0 / seed 42 / `n_predict=-1` / `cache_prompt:false`）：**30/30 成功、0 空答、`finish_reason` 全为 `stop`**，总墙钟 **132.17 s**，单题均值 4.41 s（0.149–13.62 s），GPU 稳定 **~10965 MiB** 无泄漏。对照"每题重开子进程"（ADR-0002 记的每次加载 2~3 s / 8B，两卷 60 次 ≈ 2~3 分钟纯加载且放大 F18），server 只加载一次（本次 5.6 s） | `docs/tracer/f32-paper2-30q.json`、`f32-progress.log`、`f30-f32-server.log` |
| F33 | **server 能力事实**（本机 `llama-server` 0.6.0-dev / build 11574）：`-fitt` 被接受且进程正常（该模型可整卡装入，**未产生部分卸载证据**）；`GET /health`、`/props`、`/slots`、`/v1/models`、`POST /tokenize`、`POST /completion`、`POST /v1/chat/completions` 均 200，`GET /openapi.json` 404；`--context-shift` **默认 disabled**（生成到上下文满即停，与 ADR-0020 一致）；`/props` 提供 `chat_template`。**未验证**：超显存模型在 server 上的 `-fitt` 部分卸载（唯一超显存模型是 30B，按用户要求不跑） | `docs/tracer/f33-capabilities.json`、`f33-server-props.json`、`f29-server-version.txt` |
| F34 | **server 方案同样必须按进程组收尾**：本次用 `setsid` 起 server 后，`kill` 会话首进程 PID **没有**收掉 `llama-server` 子进程——现场留下占用 **10972 MiB** 的孤儿，需 `pkill -x llama-server` 补杀才回到 1.49 GB 基线。这印证 F24/ADR-0021 的孤儿缺口在新方案下依然存在 → 实现必须复用 `start_new_session=True` + `killpg` 那套收尾 | 本次 tracer 现场（`pgrep -x llama-server` → 12576 残留） |
| F35 | **paper_1 的题块边界必须同时受节标题约束**：只按"下一题号"切，本节最后一题会吞掉下一节前言——实测 `I5`（E 节末题）把 `## F. 长上下文阅读` 与 `### 共同材料`（D01–D18）整块吸入，题块由 211 字符膨胀到 **2363**、混入 9 条材料。规则修正为"题块结束 = 下一题号 **或** 下一个 `## ` 节标题，取更近者"后：30 题全部切出；`## 通用要求`（160 字符）进入全部 30 题；F 节共享材料（2136 字符）**只**进 L1–L5；原文仅 `# LLM 能力测试卷 v1.0` 一行未被覆盖（文档标题，预期）。运行时抽验（逐题 prompt 走 `/v1/chat/completions`，temp 0/seed 42）：L1 引用 D02/D04/D07/D10/D16、L5 引用 D11–D17 → **共享材料确实进入模型并被使用**；I5 回归通过（395 字符题面、无材料污染、5 行格式正确）；K1 对照正常。附观察：L3 内容正确但**未按要求逐条打印材料编号**（模型行为，非切题问题） | `docs/tracer/f35-split-report.json`、`f35-runtime-{L1,L3,L5,I5,K1}.json`、`f35-server.log` |

| F36 | **真实档逐题 EXAM 端到端**（2026-10-19 02:26，Qwen3-MOE，real 档）：EXAM **255.2 s**、**60 次请求**（2 卷 × 30 题）全成功、`sheets=2`、`failures=0`；**60 题的 `stop_reason` 全为 `stop`——无一被截断**（对比 quick 档 64 token 时全是 `length`）；落 60 份逐题证据 `q-*.json` + `server.log`；答卷头 `覆盖 30/30（按逐题应答核对）`，而 `answers.txt`/`check_completion` 落"无法判定（逐题答卷…文本核对不适用）"且不计失败（退出码 0）；`--compare` 新行 pp **4300.4** / tg **50.9** / PPL **14.1946**（PPL 与 F26 记的同模型全语料值 14.1946 一致） | `runs/2026-10-19T02-26-26_Qwen3-MOE-2x4B-8B-Jan-Nano-Instruct-II/`、`runs/arena_history.jsonl`（旧整卷索引已按 ADR-0025 移入 `runs/archive/arena_history.pre-per-question-exam.jsonl`） |
| F37 | **逐题 EXAM 的取消路径成立**（确定性探针，非信号）：EXAM 第 3 帧进度后置位取消 → 记录 `ok=False / error='用户取消'`、`paper_failures=[{paper_1, "K3: 用户取消"}]`（**失败精度到题**）、`sheets=0`；盘上**保留已完成题的逐题证据** `q-paper_1-K1.json`/`K2.json` + `server.log`（该卷的 `.md` 未写，因为它没跑完）；无 `llama-server` 残留（显存回 1.42 GB 基线） | 探针 `/tmp/arena_cancel_probe.py`；现场 `/tmp/arena_cancel3/`（一次性） |

| F38 | **server 启动失败路径成立 + Meta-Llama 本轮环境性装不下**（2026-10-19 02:39，real 档）：`Meta-Llama-3.1-8B-Instruct` 三个 Trial **全部在加载模型阶段失败**（`llama-bench`：`unable to allocate CUDA0 buffer`；`llama-perplexity`：`unable to load model`；EXAM：新 `LlamaServer.start()` 报 `llama-server 提前退出（码 1）：failed to create_context`）。EXAM 记录 `ok=False`、原因完整、`sheets=0`、`paper_failures=[]`（server 根本没起来）、`server.log` 留证，**无残留进程**（显存回 1.44 GB）。按 ADR-0017 如实记录、不重试。这是环境性 VRAM 不足（8B-Q8_0 + n_ctx 16384 ≈ 10.5 GB，本机 12.28 GB 且桌面占 ~1.4 GB），**不是逐题实现的问题**：同一天 Qwen3-MOE 两种档都跑通（F36） | `runs/2026-10-19T02-39-49_Meta-Llama-3.1-8B-Instruct/`（`server.log`、`bench.err`、`perplexity.err`）、`runs/arena_history.jsonl` 第 4~6 行 |
| **F39** | **题内心跳成立，且"卡住"与"在生成"在屏上可分**（ADR-0026）：`llama-server` 每约 3 秒往自己的日志写一行 `slot print_timing`（`n_gen` / `tg`），读它就够了——**不必把请求改成流式**。实测 ① 假 server 端到端（心跳压到 0.5 s）**15 项断言全过**：正常题 `已用 1s · 本题 14 tok · 30.0 t/s · 日志 0s 前`→`28 tok`；`STALL` 题 `已用` 继续走、`本题 14 tok` **冻结**、`日志 0s→2s 前` 递增；请求体仍 `max_tokens=-1 / cache_prompt=false / temperature=0.0 / seed=42 / 单条 user 消息`；取消仍落 `ok=false + '用户取消' + paper_failures=[{paper_1,"K1: 用户取消"}] + sheets=[]`、无残留进程；无产物目录时降级为"只有已用在动"。② 真日志回放那轮事故（`runs/2026-10-19T09-43-52_Qwen3-30B-A3B/server.log`，**不跑模型**）：15 次请求 / 402 行统计全部解析，第 15 题（`C5`，task 27891）**93 行、`n_gen` 107→8116 单调、`tg` 稳定 28.9 t/s**，末行与日志逐字节一致；同一刻文案由冻死的 `卷 1/2 · 题 15/30 · 已用 974s` 变成 `… 已用 1260s · 本题 8116 tok · 28.9 t/s · 日志 0s 前`。③ `ServerLogTail` 字节偏移增量读：从文件头读到 529 行 = 文件行数（无重复/漏行），`begin()` 后读不到历史行。④ 文案宽度（离屏真控件树 + 真主题 1020×780）：进度条 988×36 px，最长文案 **451 px** | `docs/tracer/f39-progress-probe.out`、`f39-log-replay.out`、`f39-progress-width.out`；探针 `/tmp/arena_progress_probe.py`、`/tmp/arena_log_replay.py`、`/tmp/arena_progress_width.py` |

### 2.2 端到端实跑（tracer bullet 的落点）

```
$ python3 run_arena.py --headless --quick --model Qwen3-MOE-2x4B-8B-Jan-Nano-Instruct-II
[BENCH] LOADING→RUNNING→PARSING→DONE      3.0 s   pp 1196.8 t/s · tg 50.8 t/s
[PERPLEXITY] …                            4.2 s   PPL 13.0436 ± 2.1659
[EXAM] LOADING（起 llama-server）→ RUNNING ×60（2 卷 × 30 题）
  EXAM: 卷 1/2 · 题 1/30 · 已用 0s  …  EXAM: 卷 2/2 · 题 30/30 · 已用 74s
  EXAM: 答卷覆盖 paper_1 30/30 · paper_2 30/30      74.1 s  2 份答卷已落盘
=== COMPARE ===  缺值显示 —，按 pp 降序
```
逐题 EXAM 的形态（ADR-0025）：一个常驻 `llama-server` + 每题一个独立请求（`cache_prompt:false`），
答卷仍按卷拼成一份 `.md`（块首为 `#### 第 N 题 · <题号>｜<标题>`），逐题原始证据另存 `q-<卷>-<题号>.json`。
真实档实测（F36）：Qwen3-MOE 两卷 60 题 **255.2 s**、`stop_reason` 全为 `stop`、无残留进程。

### 2.3 真实参数基线

**旧口径（2026-10-17 00:30 起；`--chunks 8` / 答卷 `-n 1024`）——只作历史对照**

| 轮次 | 档位 | 结果 |
|---|---|---|
| `--quick`（两模型） | bench `-p 64 -n 16 -r 1`、ppl 2 chunks、答卷 `-n 64` | 全通过（24.4 s / 模型） |
| **真实档** Meta-Llama-3.1-8B | bench `-p 512 -n 128 -r 3`、ppl 8 chunks、答卷 `-n 1024 -c 16384` | **通过 74.1 s** → pp **4121.1** t/s · tg **46.2** t/s · PPL **7.8425 ± 0.4247** · 2 份答卷 |
| **真实档** Qwen3-MOE-2x4B-8B | 同上 | **通过 85.5 s**（环境恢复后重跑）→ pp **2624.9** t/s · tg **38.1** t/s · PPL **14.4146 ± 1.1112** · 2 份答卷 |
| **quick 自检** Qwen3-30B-A3B（超显存，ADR-0019 后新增） | bench `-p 64 -n 16 -r 1`、ppl 2 chunks、答卷 `-n 64`，`-fitt 1024` 自动部分卸载 | **通过 39.6 s** → pp **123.9** t/s · tg **28.3** t/s · PPL **8.7946 ± 1.2505** · 2 份答卷（另有一次 EXAM 因环境性显存回收失败，见 F18） |

**新口径（2026-10-17 23:27 起，ADR-0020：PPL `--chunks -1` 全语料 / 答卷 `-n -1` 不限，PPL 保护阀 3600 s）——这是当前口径**

| 轮次 | 档位 | 结果 |
|---|---|---|
| **真实档** Meta-Llama-3.1-8B-Instruct（当前，2026-10-18 12:50） | bench `-p 512 -n 128 -r 3`、ppl `--chunks -1`（自报 **564** chunk）、答卷 `-n -1 -c 16384` | **通过 429.6 s** → pp **4333.5** t/s · tg **47.4** t/s · PPL **7.3289 ± 0.0468** · 2 份答卷（字符数 14284 / 6255，**两卷都 30/30**） |
| **真实档** Qwen3-MOE-2x4B-8B（当前，2026-10-18 13:00） | 同上（自报 **584** chunk） | **通过 170.8 s** → pp **4325.7** t/s · tg **50.8** t/s · PPL **14.1946 ± 0.1305** · 2 份答卷（字符数 1536 / 3204，**两卷都 30/30**） |
| **真实档** Qwen3-30B-A3B | — | **未跑**：用户明确要求不跑（会拖垮本机 WSL2）。按 F22 的 8B 实测（123~134 s / 564~584 chunk）以 pp 吞吐比外推，全语料 PPL 约 **45~50 分钟**，紧贴 3600 s 保护阀，具体数字仍未知 |

**本表只列"当前这一轮"**。同一真实档命令在此之前已跑过两轮，数字并排看是这样的：

| 轮次 | Meta-Llama pp / tg | Qwen-MOE pp / tg | PPL（两模型） |
|---|---|---|---|
| 第一轮（2026-10-17 23:27 / 23:37，已归档） | 2957.1 / 35.7 | 2492.0 / 39.0 | 7.3289 / 14.1946 |
| 第二轮（2026-10-18 10:20 / 10:36，已归档） | 2777.1 / 36.2 | 4380.1 / 50.7 | 7.3289 / 14.1946 |
| **第三轮（当前，12:50 / 13:00）** | **4333.5 / 47.4** | **4325.7 / 50.8** | **7.3289 / 14.1946** |

**PPL 三轮逐位相同，bench 却从"Meta 更快"变到"Qwen 反超"再变到"几乎持平"**（F25）。
所以本表只说明"这一轮各模型是多少"，**不能**当作"这两个模型谁更快"的结论；对比表的可信度问题登记在 §10 与 F25。

新口径的可核验事实（当前 `runs/arena_history.jsonl`，6 条 = 上表第三轮）：都带 `profile: real`；
两条 PPL 记录带**工具自报**的 `n_chunks`（564 / 584，见 F21）；两条 EXAM 记录带 `n_predict: -1`、
`paper_failures: []`（两卷都跑成）、每份答卷的正文与字符数；答卷头另有**题目覆盖度**行（ADR-0022）。
归档：`runs/archive/arena_history.pre-adr0021.jsonl`（ADR-0021 重置前的 60 条：旧口径 + 第一轮）、
`runs/archive/arena_history.pre-paper2-v2.jsonl`（`paper_2` 题号化前的第二轮 6 条）——都可回查、都不参与对比。

对比表（`--compare`，数字与时间戳同源，见 ADR-0010 修订；**只收 official 档**，见 ADR-0018）——
以下是**新口径**下 2026-10-17 23:27/23:37 两轮之后的实际输出：

```
负载档: real（只有本档的数字参与对比；其他档的自检记录见 HISTORY）
模型                                              pp t/s    tg t/s       PPL  最近运行
Meta-Llama-3.1-8B-Instruct                      2957.1      35.7    7.3289  2026-10-17T21:27:30+00:00
Qwen3-MOE-2x4B-8B-Jan-Nano-Instruct-II          2492.0      39.0   14.1946  2026-10-17T21:38:02+00:00
Qwen3-30B-A3B                                    729.1      24.3    8.9614  2026-10-17T20:11:21+00:00
```

**读法**：两模型各有所长——Meta-Llama 的 pp 吞吐更高（2957.1 vs 2492.0，1.19×）但 tg 略低（35.7 vs 39.0，0.92×），
而困惑度低得多（7.33 vs 14.19，标准误不重叠）；"快"与"准"不是一回事，正是这张表要并排呈现的。
**但注意第三行**：30B 的两个吞吐与 PPL 都出自**旧口径**（它的 PPL 是 8 chunk 的），
与本表前两行（全语料）不是同一个量，**不要横向比**；30B 的新口径数字不存在，因为按用户要求不跑它。
（Meta-Llama 的 pp/tg 与 00:30 那轮的 4121.1/46.2 不同，是笔记本功耗墙导致的跨轮次漂移，
见 §10 风险表；这也是"对比表必须同负载、同温条件"的实证。）

### 2.4 F14 — 环境性 CUDA OOM（本轮最重要的实测发现）

| 事实 | 证据 |
|---|---|
| F14a | 真实档连续跑两个模型时，**第二个模型**（6.6 GiB）三个 Trial 全部加载失败：`allocating 6802.45 MiB … cudaMalloc failed: out of memory` | `runs/2026-10-17T00-32-05_*/bench.err` |
| F14b | 失败**与代码路径无关**：直接执行同一条 `llama-bench` 命令同样失败；十几分钟后同样的模型与命令又能成功 | 直接 CLI 复现；00:46 那次成功 |
| F14c | 失败时 `nvidia-smi` 仍报告 **10688 MiB free、无 compute app**：WSL2 报的是设备视角空闲量，看不到 Windows 主机侧对同一块 GPU 的占用 | `nvidia-smi` 输出 |
| F14d | 该状态下**任意** ≥6.8 GiB 的 `cudaMalloc` 都失败（6.6 GiB 与 8.5 GiB 模型都试过），等 90 s + 重试仍未恢复；再过约 10 分钟自行恢复 | 三轮重试日志 + 00:56 成功 |
| F14e | 失败是**温和**的：三个 Trial 各自落成 `ok=false` 记录 + 原因，进程退出码 1，界面不假死、历史不丢 | `runs/2026-10-17T00-37-*/`、退出码 |
| F14f | 由此暴露出对比表的一处误导：失败记录会刷新 `last_run_at`，造成「旧数字 + 新时间戳」 | 已按 ADR-0010 修订为"数字与时间戳同源" |

**结论**：真实档的模型加载受**主机侧显存可用性**约束，而非本机 12 GiB 卡的总量；
一次 Run 可能整体失败，也可能几十分钟后自行恢复。应用按 ADR-0017 只如实记录、
不自动重试、不偷偷降参数；恢复手段（重启 WSL 发行版 / 释放主机显存）由用户决定。

### 2.5 F17/F18 — 超显存模型（30B）实测（ADR-0019）

ADR-0015 曾按「文件 > 显存」把 30B 静态排除；2026-10-17 复查推翻了这条前提
——文件大于显存只意味着需要**部分卸载**，不意味着不能跑：

| 命令 | 结果 |
|---|---|
| `llama-bench -m Qwen3-30B-A3B-Q4_K_M.gguf -p 64 -n 16 -r 1 -fitt 1024` | exit 0 · 25.2 s · 峰值 RSS 18.7 GB · pp **124.8** / tg **33.4** t/s |
| 同上但 `-ngl 0`（纯 CPU 对照） | pp **48.7** / tg **11.4** t/s |
| 软件端到端 `--headless --quick --model Qwen3-30B-A3B`（环境恢复后第二次） | 通过 **39.6 s** · pp 123.9 / tg 28.3 · PPL 8.7946 ± 1.2505 · **2 份答卷** |

第一次端到端时 EXAM 因 `cublasCreate_v2` 失败（F18），属 F14 同类的环境性显存回收问题：
连续第三次加载 18.7 GB 模型时驱动来不及释放，等待后重跑即通过——这正好验证 ADR-0017
「失败落记录、不自动重试、用户择时重跑」在超显存模型上同样成立。

**读法**：30B 的 pp 比 8B 低一个数量级（124 vs 2896），但 tg 与 8B 同量级（33 vs 33.5）——
这正是 MoE「总参数大、激活参数小」的特征，也是"跨模型对比"值得回答的问题。
`-fitt` 的余量（默认 1 GiB）随每条记录落盘（`vram_margin_bytes`），卸载策略可核验。

---

## 3. Ubiquitous Language（统一语言）

代码、UI 文案、文档使用同一套词；**一个概念只有一个词**。

| 术语 | 含义 | 明确不是什么 |
|---|---|---|
| **Model** | 一个本地 `.gguf` 模型文件 | 不是"一次运行" |
| **ModelCatalog** | 本机可用 Model 的集合（扫描模型目录，毫秒级，不加载模型） | 不是配置 |
| **Paper**（试卷） | `test_content/real_test/paper_*` 的文本 | 不是"一道题"——题由 `arena.paper` 在 EXAM 里切分（ADR-0025） |
| **Question**（题） | `arena.paper` 从 Paper 切出的一道题：题号 + 题块 + 拼上前言后的 prompt | 不是 Paper，也不是答案 |
| **Corpus**（语料） | `test_content/speed/wiki.*.raw`，只记路径不入内存 | | 
| **Trial**（试炼） | 对某 Model 施加的**一次**测试过程（Bench / Perplexity / Exam 之一） | 不是"整套测试" |
| **TestKind** | Trial 种类：`BENCH` / `PERPLEXITY` / `EXAM` | | 
| **Stage** | Trial 生命周期阶段：`QUEUED/LOADING/RUNNING/PARSING/DONE/FAILED/CANCELLED` | | 
| **BenchCase** | 一次 bench 负载（`n_prompt/n_gen/n_depth`），`label()` 给人类可读名 | 不是结果 |
| **BenchResult** | 一条已解析的 bench 数值（`avg_ts`、`stddev_ts`、backend、commit） | 不含质量 |
| **PerplexityResult** | 已解析的 `ppl`、`ppl_stderr` 与工具自报的 `n_chunks/n_ctx` | 不含速度 |
| **AnswerSheet** | 模型对一份 Paper 的答卷正文 + 吞吐 + 墙钟 | 本阶段不判分 |
| **TrialRecord** | Trial 的**持久化快照**（基类），子类 `BenchRecord`/`PerplexityRecord`/`ExamRecord` 用 `KIND` 声明种类 | 不塞原始文本流 |
| **Profile**（负载档） | 一次 Run 用的那套负载参数的**名字**：`real`（official，唯一参与对比）/ `quick`（仅自检）；是白名单闭集，随每条记录落盘 | 不是负载参数本身，也不是用户偏好 |
| **ExamLoad** | 一次 Exam Trial **实际使用**的采样参数（`n_predict/n_ctx/temperature/seed`） | 不是配置默认值，也不含 token 数 |
| **VramMargin**（显存余量） | 运行时传给三种工具的 `-fitt` 值（字节）：上卡后至少留出的显存，决定超显存模型卸载多少层到 CPU | 不是显存总量，也不是"能不能跑"的门槛（ADR-0019） |
| **TrialContext** | 一次 Trial 的全部输入：`run_id/model/plan/sink/token` | 不是全局状态 |
| **TrialExecution** | 一次 Trial 的原始执行产物（一个或多个 `ProcessOutcome`，按序） | 未解析 |
| **ProcessOutcome** | 一次子进程的完整快照：命令、退出码、stdout、stderr、墙钟、取消/超时标记 | | 
| **ProgressSink**（进度汇） | Trial 向外汇报阶段/进度并查询取消的端口 | 不是日志 |
| **ResultStore** | TrialRecord 的持久化端口；实现 `JsonlResultStore` | 不是数据库 |
| **RunArtifacts** | 一轮 Run 的原始产物目录（物证） | 不是历史索引 |
| **Scoreboard**（记分板） | 一轮 Run 的汇总：Model + bench 两个数字 + PPL + 答卷路径 + 失败项 | 不是历史库 |
| **Archive**（历史） | 全部 TrialRecord 的查询视图（只读，每次重新读盘） | 不缓存 |
| **Comparison / ComparisonRow** | 跨 Model 把同一指标并排；缺值显示 `—` | 不做聚合平均 |
| **Run**（一轮） | 对同一 Model 连续执行 BENCH → PERPLEXITY → EXAM 的一次编排 | Trial 的容器 |
| **TestPlan** | Run 的输入：Model + kinds + 三类负载参数 + papers | 不是配置本身 |

补充的"反撒谎"约定：缺值一律 `None` / 显示 `—`，**绝不用 0 代替**（0 是"测出来为零"）。

---

## 4. 分层与模块职责

分离原则：**配置 / 主题外观 / 核心逻辑 三者分开**，再往下只做必要分层；不引入 DI 框架、ORM、HTTP 服务。

```
run_arena.sh                  ← 启动脚本：内容即启动所需的两行命令（cd 到项目 + python3 run_arena.py）
run_arena.py                  ← 唯一入口：装配 + 启动（GUI / headless / --compare / --list-models）
arena/
  config.py       AppConfig(+profile 白名单, +vram_margin_bytes) / *Defaults / TrialTimeouts  ← 配置层，唯一可写死本机环境的地方
  vocabulary.py   TestKind / Stage / RUN_ORDER / TERMINAL_STAGES / PROFILE_* / PROFILES / OFFICIAL_PROFILE  ← 只放名字，无依赖
  domain.py       Model, ModelCatalog(不排除任何模型), Paper, Corpus, BenchCase, BenchResult,
                  PerplexityResult, AnswerSheet, ExamLoad, TrialRecord(+3 子类, 含 profile/vram_margin),
                  Scoreboard, ComparisonRow, TestPlan   ← 纯数据 + 取值 + 序列化，可单测
  process.py      SubprocessRunner, ProcessOutcome, ProcessStartError, CancellationToken  ← 子进程与取消
  parsers.py      BenchJsonParser, PerplexityLogParser, CliTranscriptParser, ParseError 族  ← 脆弱性隔离层
  runners.py      TrialRunner(抽象, 含 _fit_args) / BenchRunner / PerplexityRunner / ExamRunner,
                  TrialExecution, build_runners()          ← 工具差异的唯一归属地（含 -fitt 显存适配）
  sink.py         ProgressSink(端口) / NullSink / TrialContext  ← 断开 Runner↔Session 循环依赖
  store.py        ResultStore(端口) / JsonlResultStore / AnswerSheetWriter / RunArtifacts
  history.py      official_records(档位过滤) / ScoreboardAssembler / Archive / ComparisonBuilder
  session.py      ArenaSession, make_run_id                ← 用例编排（Run 的唯一实现）
  ui/
    theme.py      ArenaTheme, ThemeError                   ← 只把 CSS 挂到 Screen，解析错误=启动期异常
    window.py     ArenaWindow                              ← 壳：标题带/舞台/状态行/记分板/页脚 + 运行态
    views.py      ScoreboardView, _TableDialog, ModelChooserDialog, HistoryView, ComparisonView
    controller.py ArenaController, _UiSink, build_controller ← 事件⇄Session，线程与主循环桥接
  theme/
    arena.css     外观的唯一来源（火热 arena 主题；类名契约写在文件头）
tools/
  check_traceability.py   可读性与可溯源性的静态自检（ADR-0001 的执行者，零依赖）
docs/
  tracer/         工具行为的原始证据（bench.json / ppl.* / cli*.out）
  decisions/      ADR-0001 ~ ADR-0019 + README 索引（代码 ⇄ 决策双向引用）
  ui/             界面截图（arena-window.png / arena-choose-model.png）
runs/             产物：历史 JSONL + 每轮 Run 的原始流与答卷
```

**依赖方向**：`run_arena → ui → session → runners → parsers/process/sink`；`session → store/history/domain`；
`tools/check_traceability → 只读源码与 ADR 文件`。
`domain`、`vocabulary`、`parsers` **不依赖任何上层**，可单独测试。
**模块 docstring 契约**：每个模块头部写「模块名 / 职责 / 依赖 / 被依赖 / 相关 ADR」+ 每个公开方法写「前置条件 / 后置条件 / 禁止 / 异常」，
由 `python3 tools/check_traceability.py` 机械校验（ADR-0001）。

---

## 5. 类图（Mermaid Class Diagram）

```mermaid
classDiagram
    direction LR

    namespace Entrypoint {
        class run_arena {
            <<module>>
            +main(argv) int
            +build_session(config) ArenaSession
            +run_headless(config, names) int
            +run_gui(config) int
            +format_scoreboard(sb) str
            +format_comparison(session) str
            +selected_model(config, names) Model
        }
        class ConsoleSink {
            +on_stage(kind, stage) void
            +on_progress(kind, fraction, message) void
            +is_cancelled() bool
            +request_cancel() void
        }
    }

    namespace Configuration {
        class AppConfig {
            +Path root
            +Path llama_bin_dir
            +Path models_dir
            +Path content_dir
            +Path runs_dir
            +Path tracer_dir
            +Path theme_css
            +int vram_margin_bytes
            +str profile
            +BenchDefaults bench
            +PerplexityDefaults perplexity
            +ExamDefaults exam
            +TrialTimeouts timeouts
            +__post_init__() void
            +from_env(root) AppConfig
            +quick(root) AppConfig
            +binary(tool) Path
            +papers_dir() Path
            +speed_dir() Path
            +corpus_path(name) Path
            +paper_paths() list~Path~
            +ensure_runtime_dirs() void
        }
        class BenchDefaults {
            +int n_prompt
            +int n_gen
            +int n_depth
            +int reps
        }
        class PerplexityDefaults {
            +str corpus_name
            +int n_ctx
            +int chunks
        }
        class ExamDefaults {
            +int n_predict
            +int n_ctx
            +float temperature
            +int seed
            +str prompt_suffix
        }
        class TrialTimeouts {
            +float bench
            +float perplexity
            +float exam
        }
    }

    namespace Vocabulary {
        class TestKind {
            <<enumeration>>
            BENCH
            PERPLEXITY
            EXAM
        }
        class Stage {
            <<enumeration>>
            QUEUED
            LOADING
            RUNNING
            PARSING
            DONE
            FAILED
            CANCELLED
        }
        class RUN_ORDER {
            <<constants>>
            (BENCH, PERPLEXITY, EXAM)
        }
        class TERMINAL_STAGES {
            <<constants>>
            (DONE, FAILED, CANCELLED)
        }
        class PROFILES {
            <<constants>>
            PROFILE_REAL / PROFILE_QUICK
            PROFILES / OFFICIAL_PROFILE
        }
    }

    namespace Domain {
        class Model {
            +str path
            +str display_name
            +str quantization
            +int size_bytes
            +slug() str
            +fingerprint() str
        }
        class ModelCatalog {
            -Path _models_dir
            +list_models() list~Model~
            +find(name) Model
            -_describe(path) Model
        }
        class Paper {
            +str name
            +str path
            +str text
            +from_path(path) Paper
        }
        class Corpus {
            +str name
            +str path
            +from_path(path) Corpus
        }
        class BenchCase {
            +int n_prompt
            +int n_gen
            +int n_depth
            +label() str
            +to_dict() dict
            +from_dict(payload) BenchCase
        }
        class BenchResult {
            +BenchCase case
            +float avg_ts
            +float stddev_ts
            +int n_gpu_layers
            +str backend
            +str build_commit
            +str test_time
            +to_dict() dict
            +from_dict(payload) BenchResult
        }
        class PerplexityResult {
            +float ppl
            +float ppl_stderr
            +int n_chunks
            +int n_ctx
            +str corpus_name
            +to_dict() dict
            +from_dict(payload) PerplexityResult
        }
        class AnswerSheet {
            +str paper_name
            +str model_name
            +str text
            +float prompt_ts
            +float generation_ts
            +float wall_seconds
            +int chars
            +to_dict() dict
            +from_dict(payload) AnswerSheet
        }
        class ExamLoad {
            +int n_predict
            +int n_ctx
            +float temperature
            +int seed
            +to_dict() dict
            +from_dict(payload) ExamLoad
        }
        class TrialRecord {
            <<abstract>>
            +str run_id
            +str model_path
            +str model_name
            +str started_at
            +float wall_seconds
            +bool ok
            +str error
            +str profile
            +int vram_margin_bytes
            +TestKind KIND
            +kind() TestKind
            +payload() dict
            +to_dict() dict
            +from_dict(payload) TrialRecord
            +decode_payload(payload) dict
        }
        class BenchRecord {
            +list~BenchResult~ results
            +int reps
            +throughput(n_prompt) float
        }
        class PerplexityRecord {
            +PerplexityResult result
        }
        class ExamRecord {
            +list~AnswerSheet~ sheets
            +list~str~ answer_paths
            +ExamLoad load
            +list~PaperFailure~ paper_failures
        }
        class PaperFailure {
            +str paper_name
            +str reason
            +to_dict() dict
            +from_dict(payload) PaperFailure
        }
        class Scoreboard {
            +Model model
            +str run_id
            +float bench_pp_ts
            +float bench_tg_ts
            +float perplexity
            +float perplexity_stderr
            +tuple~str~ answer_paths
            +tuple~str~ failures
            +float wall_seconds
            +str profile
            +complete() bool
        }
        class ComparisonRow {
            +str model_name
            +str model_path
            +float bench_pp_ts
            +float bench_tg_ts
            +float perplexity
            +str last_run_at
        }
        class TestPlan {
            +Model model
            +frozenset~TestKind~ kinds
            +BenchCase bench_case
            +int bench_reps
            +Corpus corpus
            +int ppl_n_ctx
            +int ppl_chunks
            +tuple~Paper~ papers
            +int exam_n_predict
            +int exam_n_ctx
            +float exam_temperature
            +int exam_seed
            +str exam_prompt_suffix
            +wants(kind) bool
        }
    }

    namespace Ports {
        class ProgressSink {
            <<interface>>
            +on_stage(kind, stage) void
            +on_progress(kind, fraction, message) void
            +is_cancelled() bool
        }
        class NullSink
        class TrialContext {
            +str run_id
            +Model model
            +TestPlan plan
            +ProgressSink sink
            +CancellationToken token
            +cancel_check() Callable
        }
        class CancellationToken {
            +cancel() void
            +is_cancelled() bool
        }
    }

    namespace Adapters {
        class TrialRunner {
            <<abstract>>
            -AppConfig _config
            -SubprocessRunner _process
            -RunArtifacts _artifacts
            +run(context) TrialRecord
            +kind() TestKind
            #_build_command(context) Sequence
            #_execute(context) TrialExecution
            #_build_record(context, execution, started_at, wall) TrialRecord
            #_fit_args() list~str~
            #_base_record_kwargs(context, started_at, wall, ok, error) dict
            #_new_record(context, started_at, wall, ok, error) TrialRecord
            #_run_process(context, command, artifact_prefix, timeout_s, on_stdout_line) ProcessOutcome
            #_failure(context, started_at, wall, error, stage) TrialRecord
        }
        class BenchRunner {
            #_build_command(context) Sequence
            #_execute(context) TrialExecution
            #_build_record(context, ...) BenchRecord
        }
        class PerplexityRunner {
            #_build_command(context) Sequence
            #_execute(context) TrialExecution
            #_build_record(context, ...) PerplexityRecord
        }
        class ExamRunner {
            -AnswerSheetWriter _writer
            -CliTranscriptParser _parser
            #_build_command(context, paper_name) Sequence
            #_execute(context) TrialExecution
            #_build_record(context, ...) ExamRecord
            -_prompt_for(paper_text) str
            -_sheet_for(context, paper, outcome) AnswerSheet
            -_report_alive(context, done, papers, ...) void
            -_report_coverage(context, coverages) void
            -_answers_dir(context) Path
        }
        class Coverage {
            +int expected
            +int found
            +tuple~str~ missing
            +str basis
            +str note
            +complete() bool
            +short() str
            +summary() str
        }
        class TrialExecution {
            +tuple~ProcessOutcome~ outcomes
            +tuple~AnswerSheet~ sheets
            +tuple~str~ answer_paths
            +tuple~PaperFailure~ paper_failures
            +cancelled() bool
            +timed_out() bool
            +first_failure() ProcessOutcome
        }
        class SubprocessRunner {
            +float _reader_grace_seconds
            +run(command, timeout_s, cancel_check, on_stdout_line, on_stderr_line, cwd) ProcessOutcome
            -_terminate(process) int
            -_signal_group(process, sig) void
        }
        class ProcessOutcome {
            +tuple~str~ command
            +int returncode
            +str stdout
            +str stderr
            +float wall_seconds
            +bool cancelled
            +bool timed_out
            +ok() bool
        }
        class ProcessStartError
        class ParseError {
            <<abstract>>
        }
        class BenchParseError
        class PerplexityParseError
        class TranscriptParseError
        class BenchJsonParser {
            +parse(stdout) list~BenchResult~
        }
        class PerplexityLogParser {
            +parse_final(stderr) tuple~float_float~
            +parse_setup(stderr) tuple~int_int~
            +parse_progress(stdout, total_chunks) float
        }
        class CliTranscriptParser {
            +extract_answer(stdout, prompt) str
            +parse_throughput(stdout) tuple~float_float~
            -_cut_footer(stdout) str
            -_strip_noise(answer) str
        }
    }

    namespace Storage {
        class ResultStore {
            <<interface>>
            +append(record) void
            +load_all() list~TrialRecord~
        }
        class JsonlResultStore {
            +Path index_path
        }
        class AnswerSheetWriter {
            +write(sheet, dest_dir, note) str
        }
        class ProtocolMarker {
            +Path path
            +ensure() str
            +read_since() str
        }
        class RunArtifacts {
            +run_dir(run_id) Path
            +save(run_id, filename, text) str
        }
    }

    namespace Application {
        class ArenaSession {
            -AppConfig _config
            -dict~TestKind_TrialRunner~ _runners
            -ResultStore _store
            -Archive _archive
            -CancellationToken _token
            +default_plan(model) TestPlan
            +run(plan, sink) Scoreboard
            +cancel() void
            +cancelled() bool
            +history() list~TrialRecord~
            +compare() list~ComparisonRow~
            +latest_scoreboard(model) Scoreboard
            +register_protocol() str
            +protocol_warning() str
        }
        class ScoreboardAssembler {
            +build(model, records, run_id) Scoreboard
        }
        class Archive {
            -ResultStore _store
            +all_records() list~TrialRecord~
            +records_for(model_path) list~TrialRecord~
            +models() list~str~
            +latest_scoreboard(model_path) Scoreboard
            -_newest_run(records, require_success) list~TrialRecord~
            -_model_of(records) Model
            -_empty_model() Model
        }
        class ComparisonBuilder {
            +build(records) list~ComparisonRow~
        }
        class protocol_warning {
            <<function>>
            +protocol_warning(records, since) str
        }
        class make_run_id {
            <<function>>
            +make_run_id(model) str
        }
    }

    namespace Presentation {
        class ArenaTheme {
            +Path css_path
            +tuple~str~ parse_messages
            +apply() void
            -_on_parsing_error(provider, section, error) void
        }
        class ThemeError
        class ArenaWindow {
            +ScoreboardView scoreboard_view
            +Button choose_button
            +Button primary_button
            +Button history_button
            +Button compare_button
            +bind(on_choose, on_primary, on_history, on_compare, on_close) void
            +set_model_name(name) void
            +set_status(text) void
            +set_running(running) void
            +set_progress(fraction, message) void
            +reset_progress() void
            +show_scoreboard(sb) void
            +launch() int
            -_on_delete_event(...) bool
        }
        class ScoreboardView {
            +render(sb) void
        }
        class _TableDialog {
            +fill(rows) void
            +set_notice(text) void
            +show_modal() void
        }
        class ModelChooserDialog {
            +pick(models) Model
        }
        class HistoryView {
            +present_records(records) void
        }
        class ComparisonView {
            +__init__(parent, profile) void
            +present_rows(rows, notice) void
        }
        class ArenaController {
            -ArenaSession _session
            -ArenaWindow _window
            -Model _model
            -bool _running
            -Thread _worker
            +bind() void
            +on_primary() void
            +choose_model() void
            +start() void
            +shutdown() bool
            +cancel() void
            +show_history() void
            +show_comparison() void
            +apply_stage(kind, stage) bool
            +apply_progress(kind, fraction, message) bool
            -_run_worker(plan) void
            -_on_worker_finished(sb) bool
            -_on_worker_failed(message) bool
        }
        class _UiSink {
            +on_stage(kind, stage) void
            +on_progress(kind, fraction, message) void
            +is_cancelled() bool
        }
    }

    %% ---- 配置注入 ----
    AppConfig *-- BenchDefaults
    AppConfig *-- PerplexityDefaults
    AppConfig *-- ExamDefaults
    AppConfig *-- TrialTimeouts
    AppConfig ..> ModelCatalog : 注入 models_dir
    AppConfig ..> TrialRunner : 注入路径与参数
    AppConfig ..> ArenaTheme : 注入 theme_css

    %% ---- 领域内部 ----
    ModelCatalog o-- Model : 产出
    TestPlan o-- Model
    TestPlan o-- BenchCase
    TestPlan o-- Corpus
    TestPlan o-- "1..*" Paper
    TrialRecord <|-- BenchRecord
    TrialRecord <|-- PerplexityRecord
    TrialRecord <|-- ExamRecord
    TrialRecord ..> TestKind : 由 KIND 声明
    BenchRecord o-- "1..*" BenchResult
    BenchResult *-- BenchCase
    PerplexityRecord o-- PerplexityResult
    ExamRecord o-- "0..*" AnswerSheet
    ExamRecord o-- ExamLoad
    Scoreboard o-- Model
    ComparisonRow ..> Model : 以名/路径标识
    AppConfig ..> PROFILES : profile 落在白名单内（ADR-0018）

    %% ---- 适配器 ----
    TrialRunner <|-- BenchRunner
    TrialRunner <|-- PerplexityRunner
    TrialRunner <|-- ExamRunner
    TrialRunner ..> SubprocessRunner : 用
    TrialRunner ..> TrialContext : 接受
    TrialRunner ..> TrialExecution : 产出
    TrialRunner ..> TrialRecord : 产出
    TrialExecution o-- "1..*" ProcessOutcome
    SubprocessRunner ..> ProcessOutcome : 产出
    SubprocessRunner ..> ProcessStartError : 抛
    SubprocessRunner ..> CancellationToken : 轮询 cancel_check
    SubprocessRunner ..> ParseError : 不涉及（只搬原始文本）
    BenchRunner ..> BenchJsonParser : 用（只吃 stdout）
    PerplexityRunner ..> PerplexityLogParser : 用（ppl 吃 stderr / 进度吃 stdout）
    ExamRunner ..> CliTranscriptParser : 用（吃 stdout）
    ExamRunner ..> AnswerSheetWriter : 用
    ExamRunner ..> RunArtifacts : 落原始 transcript
    ParseError <|-- BenchParseError
    ParseError <|-- PerplexityParseError
    ParseError <|-- TranscriptParseError

    %% ---- 端口实现 ----
    ProgressSink <|.. NullSink
    ProgressSink <|.. ConsoleSink
    ProgressSink <|.. _UiSink
    TrialContext *-- ProgressSink
    TrialContext *-- CancellationToken
    ResultStore <|.. JsonlResultStore

    %% ---- 应用层 ----
    ArenaSession o-- "3" TrialRunner
    ArenaSession o-- ResultStore
    ArenaSession o-- Archive
    ArenaSession ..> make_run_id : 生成 run_id
    ArenaSession ..> TestPlan : 接受
    ArenaSession ..> ProgressSink : 调用
    ArenaSession ..> ScoreboardAssembler : 用
    ArenaSession ..> ComparisonBuilder : 用
    ArenaSession ..> TrialContext : 构造
    ScoreboardAssembler ..> TrialRecord : 折叠
    ScoreboardAssembler ..> Scoreboard : 产出
    Archive o-- ResultStore
    Archive ..> TrialRecord : 读
    Archive ..> ScoreboardAssembler : 用
    ComparisonBuilder ..> TrialRecord : 分组
    ComparisonBuilder ..> ComparisonRow : 产出

    %% ---- 表现层 ----
    ArenaWindow o-- ScoreboardView
    ArenaWindow ..> Scoreboard : 渲染
    ScoreboardView ..> Scoreboard : 渲染
    _TableDialog <|-- ModelChooserDialog
    _TableDialog <|-- HistoryView
    _TableDialog <|-- ComparisonView
    ModelChooserDialog ..> ModelCatalog : 读
    HistoryView ..> TrialRecord : 渲染
    ComparisonView ..> ComparisonRow : 渲染
    ArenaController o-- ArenaSession
    ArenaController o-- ArenaWindow
    ArenaController ..> ModelCatalog : 查
    ArenaController ..> _UiSink : 创建
    ArenaController ..> GLib : idle_add 投递
    _UiSink ..> ArenaWindow : 经主循环
    ArenaTheme ..> GtkCssProvider : 挂到 Gdk.Screen

    %% ---- 入口 ----
    run_arena ..> AppConfig : 装配
    run_arena ..> ArenaSession : 装配
    run_arena ..> ConsoleSink
    run_arena ..> ArenaWindow
    run_arena ..> ArenaTheme
    run_arena ..> ArenaController : build_controller
```

> 说明：`Domain` 与 `Vocabulary` 不依赖任何上层；`ui.*` 是唯一 import `gi` 的地方（除 `run_arena.run_gui` 的延迟导入）。

---

## 6. 关键设计决策

| # | 决策 | 理由 / 代价 | ADR |
|---|---|---|---|
| D1 | 三种测试统一走 **subprocess**，不起 `llama-server` | 满足"非必要不使用集成"；只需一次性批处理结果，HTTP 会引入端口/生命周期/客户端三份复杂度。代价：进度只能从文本流里刮 → 由 D3 吸收 | ADR-0002 |
| D2 | 每个工具一个 Runner，共用抽象 `TrialRunner` | 命令构造不同但**生命周期相同**（执行→判定→解析→落记录→报进度）；新增工具只加子类，`ArenaSession` 只认抽象 | ADR-0003 |
| D3 | 解析器独立成 `parsers.py`，按 F2/F3/F4/F12 分别实现 | 本架构**唯一被实测推翻过**的地方：ppl 在 stderr、进度在 stdout、答卷靠截断标记；把这层脆弱性关进可单测的文件，UI 与编排层永不见原始文本 | ADR-0004 |
| D4 | 结果存储用 **JSONL 追加**，不引入数据库 | 标准库即可、人类可读、天然追加安全；跨模型对比只是内存分组。代价：改写记录不便（本阶段不需要） | ADR-0005 |
| D5 | **FP64 硬约束**落到三处：一律内建 `float`；禁止 numpy/`struct.pack('f')`（本工程不含 numpy）；计时只用 `time.perf_counter()` | 需求明文要求；F8 证明零额外成本 | ADR-0006 |
| D6 | 主题是**外置 CSS**（`arena/theme/arena.css`），类名即 Python↔CSS 契约 | 需求要求外观/配置/逻辑分离；改配色不碰 Python。**CSS 解析错误 = 启动期异常**，不静默降级 | ADR-0007 |
| D7 | UI 线程只画图，Trial 在 worker 线程，进度经 `GLib.idle_add` 回主循环 | F11：分钟级运行否则窗口假死、取消点不动。代价：UI 不可重入（运行中禁用选择键 + controller 拦一道） | ADR-0008 |
| D8 | 截屏/自检必须 `GDK_BACKEND=x11` | F10：Wayland 下 `pixbuf_get_from_window` 返回空白 | —（F10） |
| D9 | 本阶段**不自动判分**；答卷只承载文本 + 吞吐 | 需求只说"输出答案文件"；判分需要第二个模型与评分标准，超出范围 | ADR-0009 |
| D10 | 对比是**跨模型并列**（每模型一行，取各类 Trial 最新值），不聚合平均 | 平均会把"快但笨"与"慢但准"混成一个假数字 | ADR-0010 |
| D11 | 答卷吞吐取自 `llama-cli` 页脚自报的 `[ Prompt: x t/s \| Generation: y t/s ]` | 该版本不报告 token 数，反推会引入假设 | ADR-0011 |
| D12 | ~~不预先为显存做 MoE 分层策略~~ → **已由 D21 的"交给工具自动适配"实现** | 原判断被 F14/用户决策推翻（30B 会崩），ADR-0015 的硬排除又被 F17 推翻 | ADR-0012（Superseded） |
| D13 | 配置是**唯一环境事实来源**（`AppConfig.from_env()` / `.quick()`），`--models-dir/--runs-dir` 用 `dataclasses.replace` 覆盖 | 环境变量读取与绝对路径字面量只允许出现在 config.py | ADR-0013 |
| D14 | 提供 `--headless` 与 `--quick` 两种非 GUI 运行方式（且一次只跑一个模型） | 自检与批量跑图不需要图形环境；quick 只作自检，其数字**已被 D20 在结构上排除出对比表** | ADR-0014 |
| D15 | ~~**显存预算硬排除超规格模型**~~ → **已由 D21 取代** | 原判断"文件 > 显存即不可测"只在默认 `-ngl -1` 下成立（F17 推翻） | ADR-0015（Superseded） |
| D16 | 记分板**值列只放事实、文本列必须可截断**（含失败行），全文进 tooltip | 实测同一类缺陷把窗口从 1020px 顶到 2275px 与 4644px 两次 | ADR-0016 |
| D17 | 环境性 CUDA OOM **只如实记录，不自动重试、不自动降级** | 自动降级会产出"参数不同却记在同一模型名下"的数字，破坏可比性 | ADR-0017 |
| D18 | 子进程进**独立进程组**，取消时 `killpg` 收全组；读取线程有界 join；headless 的 Ctrl-C 即取消 | 修掉实测到的取消死锁（孤儿孙进程持有管道→Trial 永不返回） | ADR-0002 修订、ADR-0014 修订 |
| D19 | 历史派生的取值规则：对比表与历史记分板都取**最新有效**（对比表逐指标、记分板整轮），失败记录只进历史视图 | 失败记录不能给旧数字"续期"，也不能让"最近一次失败"抹掉已有成绩 | ADR-0010 修订 |
| D20 | **负载档只有 `real`/`quick` 两个名字**：`AppConfig.profile` 构造期白名单校验，每条 `TrialRecord` 落盘 `profile`；对比表只收 official 档，历史记分板优先 official；补记 `BenchRecord.reps` 与 `ExamRecord.load`；**不提供任何参数面板**（CLI/环境变量/GUI 都没有） | "同负载同参数才可比"是这张表的硬前提。用档名过滤是结构保证（不靠注释提醒），落盘参数让实验条件可核验；参数是实验条件不是用户偏好，开面板等于把"参数不同却记在同一模型名下"变成日常（与 ADR-0017 拒绝自动降级同源） | ADR-0018 |
| D21 | **不按文件大小排除任何模型**：三种 Runner 统一追加 `-fitt <vram_margin_bytes>`，由 llama.cpp 自动决定上卡层数；`ModelCatalog` 去掉预算与排除；每条记录落盘 `vram_margin_bytes`；环境变量改为 `ARENA_VRAM_MARGIN_BYTES` | F17：30B（17697 MiB）在 `-fitt 1024` 下 pp 124.8 / tg 33.4、exit 0；"文件 > 显存"只意味着部分卸载。静态排除会把最有比较价值的 MoE 大模型从可测集合里删掉，与"尽量跑满"的目标冲突 | ADR-0019 |
| D22 | **完成条件与退出保护分离**：答卷 `n_predict=-1`（模型写到 EOS/上下文满）、PPL `chunks=-1`（完整语料）；`TrialTimeouts.perplexity` 由 7200 s 收紧为 **3600 s**，`exam` 保持 3600 s（**每份试卷**各计一次）；PPL 进度分母改由工具 stderr 自报 | 旧口径把"我们给的参数上限"当完成条件：1024 token 会截断答卷（实测同一题 3716 B → 29562 B）、8 chunks 只覆盖语料约 1.4%。工具原生 -1 即"不限"（F19/F20），不必发明新常量；"不限"的代价是耗时不可预估，只能靠墙钟兜底，超时按 ADR-0017 如实记失败 | ADR-0020 |
| D23 | **退出路径统一 + EXAM 逐卷落盘 + 协议标记**：关窗/`SIGINT`/`SIGTERM`/`SIGHUP` → 取消 → 有界等待（12 s）→ `killpg`，+ `atexit` 兜底；每份试卷一结束就写答卷文件，`ok=False` 的记录仍带已完成那几卷；`runs/PROTOCOL` 记协议生效时刻，对比表对旧协议记录告警；协议变更 ⇒ 归档并重置机读历史 | 放开上限后单卷可达 1 小时（实测 PPL 4.2→123.1 s、EXAM 68.5→378.1 s），"关掉它"必须真的干净：旧行为是界面关了、子进程还占着显存，且在飞那一卷零留痕（F23）。不做续跑 ⇒ 只能靠"不丢已完成的那几卷"把最坏损失压到 1 卷；协议字段换成"分界线 + 告警"是拿纪律换掉一次 schema 迁移（F24 如实声明 SIGKILL 档未覆盖） | ADR-0021 |
| D24 | **试卷题号化 + 答卷覆盖度核对**：`paper_2` 重排成 `paper_1` 的格式（`## A. …` + `### K1｜短标题`，**题面一字未改**），让"答到第几题"可机械核对；核对逻辑 = `arena/coverage.py`（零资产：试卷自描述题号，不引入题号清单文件），在 EXAM 收尾写进答卷文件头并上报进度；**只报事实、不改 `ok`**，且"无法判定"与"未覆盖"严格分开；另配 `tools/check_completion.py` 回查历史答卷 | "答到第几题"是长跑最该显示的事实，而字符数分不出"13000 字符的截断卷"和"13000 字符的完整卷"（F27）。根因是 `paper_2` 不自描述——**修数据比在代码里堆启发式便宜**，于是"要不要加题号清单资产"这个取舍被整个删掉；把覆盖度当失败会与 ADR-0017 的失败语义（执行层面）冲突，故只报事实（F28） | ADR-0022 |
| D25 | **EXAM 逐题喂**：试卷切成逐题（`arena/paper.py`，题块边界 = 下一题号或下一节标题取近者；无题号的 `## ` 节作文档级前言、节内前言只给本节的题）；一题一个**独立请求**走常驻 `llama-server` 的 `/v1/chat/completions`（`cache_prompt:false`，每题清空上下文）；逐题答案按题号顺序拼回**一份** `AnswerSheet`（记录 schema 不变），逐题原始证据落 `q-<卷>-<题号>.json` + `server.log`；完整性口径改为"**产出了非空回答的题数**" | 需求从"整卷一次做"改为"一题一题喂"；tracer 实测裸 `/completion` 不套模板会吃到上下文满（380 s 死循环，F29），而 chat 端点与 `llama-cli -st` **逐字一致**；单进程连续 30 题稳定（F32）、取消即断开（F31）；切题边界若不约束会吞掉下一节前言（F35）。注入题号标题会让覆盖率恒为 30/30（假精确），故改用中性块首标签 + 逐题应答口径 | ADR-0025（+ADR-0002 修订） |

---

## 7. 落盘布局（实测）

```
runs/
  arena_history.jsonl                                 # 每行一条 TrialRecord（含 profile 档名 + vram_margin + payload）
  PROTOCOL                                            # 评估协议标记：name=<协议名> + since=<生效时刻>（ADR-0021）
  archive/                                            # 归档的旧协议/旧试卷历史；只归档索引，run 目录原样保留
    arena_history.pre-adr0021.jsonl                   # ADR-0021 重置前的那 60 条（可回查，不参与对比）
    arena_history.pre-paper2-v2.jsonl                 # paper_2 题号化前那一轮的 6 条（ADR-0022）
  2026-10-16T23-38-56_Meta-Llama-3.1-8B-Instruct/      # 一轮 Run 一个目录（run_id = 时间去冒号_模型slug）
    bench.out  bench.err                              # llama-bench 原始流（stdout 是 JSON）
    perplexity.out  perplexity.err                    # 进度串 / Final estimate
    transcript-paper_1.out  transcript-paper_1.err    # llama-cli TUI 全文，一份试卷一对
    transcript-paper_2.out  transcript-paper_2.err
    answers/
      Meta-Llama-3.1-8B-Instruct__paper_1.md          # 需求要求的"一份文件"（含吞吐元数据头 + 题目覆盖度行）
      Meta-Llama-3.1-8B-Instruct__paper_2.md
    answers.txt                                       # 该轮全部答卷的纯文本汇总，收尾自动落盘（ADR-0023；跨系统直读用）
test_content/real_test/
  paper_1  paper_2                                    # 两份试卷都题号化：`## A. …` + `### K1｜短标题`（ADR-0022）
  archive/paper_2.v1                                  # 题号化之前的 paper_2（旧版答卷只能得出"无法判定"）
arena/paper.py                                         # 试卷结构：逐题切分 + 前言/共享材料归属（纯文本，ADR-0025）
arena/server.py                                        # llama-server 生命周期 + 逐题问答客户端（EXAM 专用，ADR-0025）
arena/coverage.py                                     # 试卷题号提取 + 答卷覆盖度核对（纯文本，零资产；逐题答卷落"无法判定"档）
arena/export.py                                       # 答卷汇总文本渲染（纯函数，所有轮次共用一份实现，ADR-0023）
tools/
  check_traceability.py                               # ADR-0001 的执行者（文档契约静态检查）
  check_completion.py                                 # ADR-0022 的执行者（答卷覆盖度复算，可回查历史）
  export_answers.py                                   # ADR-0023 的执行者（把已有轮次的答卷导出为 answers.txt）
docs/
  tracer/     bench.json bench.stderr ppl.out ppl.err cli.out cli2.out cli3.out prompt.txt
              ppl-chunks-all.out/.err cli-n1.out/.err      # ADR-0020 烟测的原始流（对应 F19/F20）
  ui/         arena-window.png arena-choose-model.png
```
原始流一律留档：解析失败时仍可事后重建；JSONL 只存"已解析的数字 + 答卷正文"。

---

## 8. 界面结构（与截图一一对应）

```
┌───────────────────────────────────────────────────────────────┐
│                     L O C A L   A R E N A                     │  .arena-title
│                     LLM TEST ARENA                            │  .arena-subtitle
│                     bench · perplexity · exam                 │  .arena-tagline
│      ┌──────────────────────┐        ╭──────────────╮         │
│      │  Choose Your Model!  │        │    START     │         │  .choose-model（方形 240²）
│      │  <已选模型名>          │        ╰──────────────╯         │  .start-button（圆形 168²，运行中→CANCEL 蓝色）
│      └──────────────────────┘                                 │
│ model: Meta-Llama-3.1-8B-Instruct          status: finished…  │  .status-band
│ ░░░░░░░░░░░░░░░░░░░░ progressbar ░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
│ ┌───────────────────────────────────────────────────────────┐ │
│ │ BENCH        pp 4121.1 t/s · tg 46.2 t/s       吞吐        │ │  ScoreboardView
│ │ PERPLEXITY   7.8425 ± 0.4247                   困惑度       │ │
│ │ EXAM         2 份答卷已落盘                     <答卷路径>   │ │
│ │ run 2026-… · real 档 · 墙钟 74.1s                           │ │
│ └───────────────────────────────────────────────────────────┘ │
│ [ HISTORY ]                                       [ COMPARE ] │  .footer-band
└───────────────────────────────────────────────────────────────┘
```
- 截图证据：`docs/ui/arena-window.png`（主窗口，含真实历史记分板）、`docs/ui/arena-choose-model.png`（选模型对话框，列出 3 个本机模型）。
  **注意**：两张截图摄于 2026-10-17 01:17，早于 ADR-0018 的「档位」显示，故图内尚未出现 `real 档 · ` 字样；
  界面上档位出现在记分板末行、`HISTORY` 的「档位」列、`COMPARE` 标题与 `--compare` 表头（ADR-0018）。

---

## 9. 现状：已完成 / 待办

### 已完成（有实测证据）
1. **Tracer bullet**：三类工具的输出语义、参数展开、FP64、GTK/CSS/WSLg 截图行为全部实测固化（§2）。
2. **核心逻辑**：`arena/` 10 个模块 + `arena/ui/` 4 个模块 + 外置 CSS，全部实现；职责单一、依赖单向。
3. **端到端跑通**：`--list-models`、`--compare`、`--headless [--quick]`、GUI 均实跑通过；三个模型（含超显存的 30B）都留下了完整产物与答卷文件。
4. **失败是一等公民**：取消/超时/非零退出码/解析失败都落成 `ok=false` 记录（23:14 那次 EXAM 解析失败就是活证据）。
5. **界面**：火热 arena 主题生效（径向渐变、圆形 START、运行中变冷色 CANCEL），记分板显示 bench 两个数字与 PPL；对话框也已套上主题。
6. **文档与自检闭环**：`docs/decisions/` 19 份 ADR（ADR-0001 ~ ADR-0019，编号连续）+ `tools/check_traceability.py` 机械校验
   「模块头五字段 / 公开函数四段契约 / ADR 双向可达性 / 编号连续性 / 无 TODO / 无 float32 与 time.time()」，当前 **全部通过**。
7. **负载档闭环（ADR-0018）**：`real`/`quick` 是白名单闭集，每条记录落盘 `profile`；对比表只收 official 档、
   历史记分板优先 official、HISTORY 多一列「档位」；补记 `BenchRecord.reps` 与 `ExamRecord.load`，让"同参数"可核验；
   明确不提供任何参数面板。quick 只作自检，`--headless --quick` 结束会打印「不进对比表」的说明。
8. **超显存模型可测（ADR-0019）**：删除按文件大小的静态排除，三种 Runner 统一追加 `-fitt`（余量来自
   `AppConfig.vram_margin_bytes`，随记录落盘）。30B 已端到端 quick 自检通过（39.6 s / 2 份答卷，§2.5），
   `--list-models` 与 GUI 选择器都列出全部 3 个模型。

### 已修问题（本轮）
| # | 问题 | 处理 | 证据 |
|---|---|---|---|
| G1 | `runs/arena_history.jsonl` 第 7 行是 1729 字节 NUL（上一轮会话被中断写坏） | 截到 6 行有效记录并重跑补回 Meta-Llama 的 3 条；原件备份 `/tmp/arena_history.jsonl.corrupt.bak` | `--compare` 无告警，9+ 条记录可读 |
| G2 | 记分板 EXAM 提示行直拼绝对路径 → 窗口从 1020px 被顶到 2275px | 值列只留事实，提示列改文件名 + `EllipsizeMode.MIDDLE` + `max_chars=56`，完整路径进 tooltip（ADR-0016） | 截图恢复 **1020×790** |
| G3 | 三个对话框（选模型/历史/对比）是 GTK 默认浅色外观 | `_TableDialog` 统一挂 `.arena-dialog`，CSS 补齐背景/表格/表头/按钮配色（ADR-0007 决策 4） | `docs/ui/arena-choose-model.png` 已是深色主题 |
| G4 | 代码引用的 14 份 ADR 不存在 | 落成 **ADR-0001 ~ ADR-0017** + `docs/decisions/README.md` 索引（含状态、六节正文、`路径::符号` 反引用） | 检查器通过双向可达性 |
| G5 | docstring 引用不存在的 `tools/check_traceability.py` | 实现该工具（ast + tokenize，零依赖），并把 ADR-0001 的四段契约升级为可校验项 | `python3 tools/check_traceability.py -v` 输出 `OK` |
| G6 | 本文件旧版仍写着已被推翻的 F6 | 已重写（旧版备份 `/tmp/pre_architecture.v1.md`），F6 标注"已推翻"、F12 记录修正 | 本文件 §2.1 |
| G7 | ~~30B 模型（17697 MiB > 12282 MiB 显存）会被默认路径选中并崩溃~~ → **该决策已被 G12 取代** | 当时决策"不纳入"：`max_model_bytes` + `exclusion_reason`（ADR-0015 取代 ADR-0012） | 见 G12 |
| **G8** | **取消死锁**（验证「运行中取消」时实测发现）：只对组长发 SIGTERM 会留下持有管道的孙进程 → 读取线程 `join()` 永不返回 → Trial 不产记录、界面永久停在 CANCEL | 子进程改 `start_new_session=True` 进独立进程组、`_terminate` 用 `killpg` 收全组、读取线程改为有界 `join(timeout)`；headless 侧把 Ctrl-C 显式做成取消（ADR-0002 修订 + ADR-0014 修订） | 取消验证脚本由 **FAIL → PASS**：2.6 s 后落盘 `ok=false / error='用户取消'`，圆键复位 START，无孤儿进程 |
| **G9** | 对比表「旧数字 + 新时间戳」：失败记录也会刷新 `last_run_at`（F14f） | `last_run_at` 改为与数字同源；只有失败记录时退化为最后一次尝试时间（ADR-0010 修订） | `--compare` 中两行的时间戳分别对应各自数字的产生时刻 |
| **G10** | 记分板**失败行未截断** → 窗口被顶到 **4644px**（与 G2 同类缺陷的第二现场）；且历史记分板取"最新记录"会把「最近一次失败」摆上台，抹掉已有成绩 | 失败行加 `EllipsizeMode.END` + `max_width_chars=120` + tooltip（ADR-0016 补充）；`Archive.latest_scoreboard` 改为取"最近一次含成功记录的 Run"（ADR-0010 补充） | 窗口回到 **1020×790**；选模型后屏上显示真实档 `pp 4121.1 / tg 46.2 / PPL 7.8425`（见 `docs/ui/arena-window.png`） |
| **G11** | quick 自检的数字会顶掉真实档成绩（对比表只按 `model_path` 取值，不看负载）；且"是不是同一条件"在历史里无法核验（bench 未记 `reps`、exam 未记采样参数） | 引入负载档闭集：`AppConfig.profile` 构造期白名单 + 每条 `TrialRecord` 落盘 `profile`；`arena.history.official_records()` 过滤使 quick **结构性**不进对比；记分板/HISTORY/COMPARE 显示档位；补记 `BenchRecord.reps`、`ExamLoad`；明确不做参数面板（ADR-0018） | `--compare` 表头为 real 且无 quick 行；12 行可判定的遗留 quick 记录已回填档名（原文件备份 `/tmp/arena_history.jsonl.pre-profile.bak`）；`check_traceability.py` 通过（18 ADR） |
| **G12** | 30B（17697 MiB > 12282 MiB）被 ADR-0015 静态排除，**不可选**；而它其实跑得动——"文件大于显存"只意味着要部分卸载 | 删除静态排除：`ModelCatalog` 去掉预算与 `exclusion_reason`；三种 Runner 统一追加 `-fitt`（`AppConfig.vram_margin_bytes`，默认 1 GiB，`ARENA_VRAM_MARGIN_BYTES` 可覆盖）；`TrialRecord` 落盘 `vram_margin_bytes`；`--list-models` 改显示文件大小（ADR-0019） | 30B 端到端 quick 自检通过：39.6 s · pp 123.9 / tg 28.3 · 2 份答卷（`runs/2026-10-17T18-53-02_Qwen3-30B-A3B/`）；`--list-models` 列出全部 3 个模型 |
| **G13** | `runs/arena_history.jsonl` 第 52 行是一条 **Qwen3-30B-A3B 的 BENCH 记录**（run `22-10-37`，real 档，pp 729.1 / tg 24.3），但行首被写了 **761 个 NUL 字节** → 该记录被读档器跳过（`--compare` 里 30B 的 pp/tg 显示 `—`）。**经比对确认是本轮之前就已存在的损坏**（本轮开跑前取的备份里一模一样） | 先备份到 `/tmp/arena_history.pre-repair.bak`，再**逐行去掉行首 NUL**（记录正文完好，剥掉即还原），最后全量校验 | 60 条记录全部可 `json.loads`、全文 NUL = 0、文件 130562 → 129801 字节；`--compare` 里 30B 恢复为 pp 729.1 / tg 24.3 |
| **G14** | **跑批三处缺口**（ADR-0021）：① 关窗/被 kill 会留下占显存的孤儿子进程，在飞那一卷零留痕；② EXAM 整轮连坐——第二卷失败会抹掉第一卷已答完的答卷；③ 对比表把旧协议（8 chunk / `-n 1024`）与新协议（全语料 / `-n -1`）的数字并列 | ① 关窗 / `SIGINT` / `SIGTERM` / `SIGHUP` 统一走"取消 → 有界等待 12 s → `killpg`"，并加 `atexit` 兜底；② 每份试卷一结束就写答卷文件，`ok=False` 的记录仍带已完成那几卷 + 逐卷失败原因；③ `runs/PROTOCOL` 记协议生效时刻，对比表（CLI+GUI）对旧记录告警，并**归档重置机读历史** | ① SIGTERM 实测 1 s 内退出、无残留 llama；GUI `delete-event` 构造测试：cancel 1 次、0.30 s join；② 实测 `ok=False` 但 `sheets=['paper_1']`、`paper_failures=[{paper_2,用户取消}]`，paper_2 生成时 paper_1 答卷已落盘；③ 含旧记录的目录会打印"36 条早于当前协议"告警，重置后的真实目录无告警 |
| **G15** | **长题在屏上与"卡死"同形**（2026-10-19 09:43 那轮 real 档的实证）：`Qwen3-30B-A3B` 的 EXAM 停在 `paper_1` 第 15 题（`C5｜发现隐藏错误`），进度条冻在 `卷 1/2 · 题 15/30 · 已用 974s`，用户判定"程序卡死"并按了 CANCEL——事后 `server.log` 显示它当时**一直在解码**（第 15 题 93 行统计、`n_gen` 107→8116 单调、`tg` 稳定 28.9 t/s、题内已跑 281.5 s；记录如实落成 `ok=False / '用户取消' / paper_failures=[{paper_1,"C5: 用户取消"}]`）。根因是 ADR-0025 的两条残余限制叠加：非流式请求 + "最小进度粒度=题" | 新增**题内心跳**（ADR-0026）：`ChatCompleter.complete` 的取消轮询循环里每 10 s 读一次 `server.log` 的 `slot print_timing` 行（`arena.parsers.ServerTimingParser` + `arena.server.ServerLogTail`），进度文案由 `卷 k/N · 题 m/M · 已用 Xs` 扩成 `… · 本题 N tok · R t/s · 日志 As 前`。**请求结构一字未改**（仍非流式 / `max_tokens=-1` / `cache_prompt=false`），因此**不改 `PROTOCOL`、不归档历史** | ① 假 server 端到端探针 **15 项断言全过**：正常题数字在涨、`STALL` 题 token 冻结而"已用 / 日志 Ns 前"在走、请求体与取消路径不变、无产物目录降级为只有"已用"；② **真日志回放**（那轮的 `server.log`，不跑模型）：15 请求 / 402 行统计全解析，第 15 题 93 行、末行 8116 tok / 28.92 t/s 与日志逐字节一致，同刻文案由冻死的 `已用 974s` 变成 `已用 1260s · 本题 8116 tok · 28.9 t/s · 日志 0s 前`；③ 增量读 529 行 = 文件行数（无重复/漏行）；④ 离屏量宽：988×36 px 的条里最长文案 451 px；⑤ `check_traceability.py` 通过（21 源文件 / 26 ADR）、`check_completion.py --current-only` 退出码 0、`--compare` 数字不变 | `docs/tracer/f39-progress-probe.out`、`f39-log-replay.out`、`f39-progress-width.out`、`docs/decisions/ADR-0026-in-question-progress-from-server-log.md` |

### 未验证项（诚实声明）
- XPU/CPU-only 回退、非 UTF-8 试卷、试卷目录为空等边界未实测。
- 取消路径已用**假 llama 工具**验证（G8），但**未在真实模型加载过程中**点过取消（真实档被取消时尾部落盘仍在，验证脚本覆盖的是机制而非显存回收）。
- 30B 只跑过 **quick 档**（§2.5）。**（2026-10-19 更新：real 档也跑过了——见 G15 与 `runs/2026-10-19T09-43-52_Qwen3-30B-A3B/`：
  `llama-bench` pp **741.0** / tg **39.6**；全语料 PPL **9.6534 ± 0.0776**（584 chunk、**451.8 s**）——
  此前按吞吐比外推的"45~50 分钟"高估了约 6 倍，它在 3600 s 保护阀内跑完；只有 EXAM 在第 15 题被人工取消。
  仍**未验证**的是：30B 的 EXAM 能否凭自己跑完整卷，以及它的 `-fitt` 部分卸载在 server 上的行为。）**
- **新口径已坐实到 8B**：两个 8B 的真实档全语料都跑通（§2.3 新口径表；记录见 `runs/archive/arena_history.pre-adr0021.jsonl`
  第 55~60 行——`runs/arena_history.jsonl` 已按 ADR-0021 重置归档），
  另有一次 `/tmp` 隔离目录下的截断语料端到端（31.2 s，日志 `chunk 41/41`，落盘 `n_chunks=41`、`n_predict=-1`）。
  仍未验证的是：30B 的新口径（未跑）、以及**同一模型重复 3 轮以上**的 drift 分布（目前每个 8B 只有 2 轮，
  已知 2 轮之间 bench 可差 +76%（F25）、PPL 逐位相同、长答卷会分叉（F26）——样本还不够画分布）。
- ADR-0021 的收尾路径：SIGTERM 与 GUI `delete-event` 的接线都有实测（F23），
  但**没有在真实模型运行中用鼠标点过 ×**（GUI 那条是构造测试）；宽限期超时后的"真的不肯退"分支也未在真机复现；
  **SIGKILL / 断电 / 父 shell 被强杀这一档明确不覆盖**（F24），需人工 `pkill -f llama-` 收尾。
- ADR-0022 的覆盖度核对：只做过 **quick 档端到端**（`paper_1` 1/30、`paper_2` 2/30 是 64 token 的**如实**结果），
  **没有**在新版 `paper_2` 上跑完整真实档并核对 30/30。两处已知边界：改版前的答卷（旧版无题号试卷）一概落到"无法判定"，
  这是改版的真实代价、无法用代码挽回；题号后紧跟中文字符（`K3题是关于…`）刻意**不算**引用，因此 30B 的 thinking 答卷落在"无法判定"。

---

## 10. 风险与未决

| 风险 | 影响 | 处理 |
|---|---|---|
| **环境性 CUDA OOM**（F14）：主机侧显存被占用时，`nvidia-smi` 仍报空闲而 `cudaMalloc` 失败 | 一整轮 Run 全失败，真实档基线可能取不到数 | 如实记录（ADR-0017）；用户在外部重跑；不要相信 WSL2 的 `nvidia-smi` 空闲量 |
| `llama-cli` 的 TUI 文本格式随上游漂移（回显截断阈值 500B 是上游硬编码常量） | 答卷抽取失败 | 抽取器独立可单测；原始 transcript 已留档可重建；退路是 `llama-server + /v1/chat/completions` |
| **长上下文下 bench 结果受笔记本功耗墙/温度漂移影响，量级足以翻转模型排名**（F25） | 跨模型对比可能给出相反结论——这不是理论风险：同一对模型三轮实测里，**"谁更快"变了两次**（第一轮 Meta 2957 vs Qwen 2492；第二轮 Qwen 4380 vs Meta 2777；第三轮 4333 vs 4326 几乎持平） | **已接受，不管理**（用户决定，2026-10-18）：温度/功耗是**电脑管理系统层面的策略**，由用户在软件外手动调整，软件不采样、不记录；跨轮漂移视为"可重复运行的工具天然带有的误差"。现状：`reps>=3` 只吸收进程内抖动，记录 `test_time`，对比表 `last_run_at` 与数字同源（ADR-0010）。**实测证据**：① 同一模型同一命令 pp **4121.1 → 2896.0**、tg **46.2 → 33.5**（约 30%）；② 另一模型 pp **2492.0 → 4380.1**（**+76%**）；③ 两个 8B 的第三轮 pp 分别为 4333.5 / 4325.7，与第二轮各自相比变动 +56% / −1%。因此对比表的吞吐列**只在同一时间窗口内可比**，跨窗口请按区间读 |
| ~~显存预算把硬件事实写进了默认值（`max_model_bytes = 12 GiB`）~~ **（已解决，2026-10-17，ADR-0019）** | 曾经：换机器需改环境变量，且排除了可跑的 30B | 改为 `vram_margin_bytes`（默认 1 GiB，`ARENA_VRAM_MARGIN_BYTES` 覆盖）：它只是 `-fitt` 的余量，不是"能不能跑"的门槛 |
| **超显存模型连续加载 3 次时会遇上环境性显存回收**（F18）：第三次加载可能 `cublasCreate_v2` 失败 | 该 Trial 落 `ok=false`，不产出答卷 | 同 F14：如实记录（ADR-0017），等显存回收后重跑即通过（实测第二次成功）；不要在同一轮里假设"前两次成功=第三次也会成功" |
| **全语料 PPL 接近保护阀**（ADR-0020）：8B 实测跑完全语料（564~584 chunk）用 **123~134 s**；30B（pp ≈ 124 t/s）按吞吐比外推约 **45~50 分钟**，紧贴 3600 s 阀 | 若 30B 在阀内跑不完，该 Trial 落 `ok=false`，该模型没有 PPL 数字可进对比表 | 刻意的取舍：宁可"没有数字"，也不要"用 1.4% 的样本假装是全语料的困惑度"。**按用户要求不跑 30B**（会拖垮本机 WSL2），所以"它能否在阀内跑完"保持未知；要拿它的全语料 PPL 只能改装配档/放宽保护阀，不开运行时开关（ADR-0018 决策 7） |
| 答卷不再有 token 上限（ADR-0020），长度由模型自己决定 | 跨模型"答了多少"不可比；个别模型可能迟迟不吐 EOS，一直生成到上下文满 | 比的是答得对不对（人读）与多快（工具自报吞吐），不是长度；兜底是每题 3600 s 的超时（超时即 `ok=false`，ADR-0017）。**（2026-10-19 补充：这一档真的发生了——30B 在第 15 题生成了 8116 token 还没停（G15）；现在屏上会显示"本题 N tok · 日志 Ns 前"，等不等由人判断，超时阀本身不变，见 ADR-0026。）** |
| **强杀（SIGKILL / 断电 / 父 shell 被 SIGKILL）会留下孤儿子进程**（F24）：登记的信号与关窗都覆盖不到这一档 | 子进程继续占显存，下一轮可能因显存不足整轮失败 | 刻意不引入 `prctl(PR_SET_PDEATHSIG)`——它只能经 `preexec_fn` 设置，而 Python 明确警告其在多线程程序中不安全（本程序正是多线程）。人工收尾：`pkill -f llama-`；已如实写进 ADR-0021 的代价 |
| **改口径后忘记重置历史**（ADR-0021）：协议标记只能"告警"，不能替你归档 | 旧协议数字仍留在表里，读者可能混读 | 机械保险：`runs/PROTOCOL` + 对比表告警（实测能报出"36 条早于当前协议"）；根治动作是把旧索引移进 `runs/archive/` |
| `paper_1` 含反注入/知识边界题，输出格式多样 | 答卷文件结构不齐 | 本阶段原样落盘，不做题目级解析 |
| 文档契约靠文本匹配，可能写出"无意义的四段契约" | 契约形式化 | 已知代价，记在 ADR-0001 的 Consequences；行内注释仍要求写"为什么" |
| ~~**quick 档数字会顶掉真实档数字**~~ **（已解决，2026-10-17，ADR-0018）**：曾实测对比表按"最新有效值"取值，跑一次 `--quick` 后该模型的 pp/PPL 就换成小负载的数 | 曾经：读者可能把 quick 数字当成正式成绩 | 已由结构解决，不再依赖读者自觉：`profile` 是白名单闭集且随记录落盘，`official_records()` 过滤使 quick 记录**不可能**进入对比表；`HISTORY` 视图仍照实列出并显示「档位」 |

**下一层问题**：
1. ~~真实档下每个模型是否应一次独立 Run？~~ **已定**：`selected_model()` 强制"一次一个模型"，`--headless` 多给或
   不给模型名都直接报错退出（ADR-0014 修订）；不再存在"一个进程连跑多个模型"的默认路径。
2. 是否要**自动判分**？（引入 LLM-as-judge 或人工评分表；若做，需要新 ADR 定义评分标准与评委的显存预算）——**仍未决**。
3. ~~历史对比维度是否扩展到"同一模型不同负载"？~~ **已否决**：ADR-0018 决策 8——负载不可调，不存在"同一模型多负载同台比较"
   的真实需求，`ComparisonRow` 的键仍是 `model_path`。
4. ~~是否给 GUI 增加参数面板？~~ **已否决**：ADR-0018 决策 7——参数是实验条件不是用户偏好；要试新参数必须新增装配档，这是刻意的。
5. ~~跨轮漂移把排名翻掉了，要不要处理？（记录 GPU 温度/功耗、提高 reps、重复多轮取中位数…）~~ **已否决**（用户决定，2026-10-18）：
   温度/功耗属于**电脑管理系统层面的策略**，由用户在软件外手动调整——软件不采样、不记录，这一条是刻意划出的
   软件边界；跨轮漂移则视为"一个可以重复跑的工具天然带有的误差"，不做管理。落实方式：§10 风险表如实登记
   漂移区间（实测 30% 与 76%），对比表**不**承诺跨时间窗口可比。
6. **"为什么停的"要不要机读化？**（"答到哪一题"已由 ADR-0022 机读化；剩下的是停止原因：EOS / 上下文满 / 被截断）——**未决**：
   `llama-cli` 只报吞吐、不报停止原因，要机读就得先 spike 高 verbosity 的 stderr，或引入 `llama-tokenize`
   之类的额外依赖（ADR-0011 当初拒绝过类似做法）。当前的替代事实：覆盖度 + 答卷字符数（F28）。

---

## 11. 交付物清单（本阶段收尾）

| 类别 | 路径 | 状态 |
|---|---|---|
| 入口 | `run_arena.py`（GUI / `--headless` / `--quick` / `--compare` / `--list-models`） | 通过 |
| 启动脚本 | `run_arena.sh`（内容就是启动所需的两行：`cd <项目>` + `python3 run_arena.py`） | 通过 |
| 核心逻辑 | `arena/`（11 模块）、`arena/ui/`（4 模块）、`arena/theme/arena.css` | 通过 |
| Doc 契约执行者 | `tools/check_traceability.py` | 通过（21 源文件 / 26 ADR 双向可达） |
| 覆盖度执行者 | `tools/check_completion.py`（ADR-0022；`--current-only` 只看当前协议索引，不必重跑模型） | 通过（全部 44 份：完整 16 · 未覆盖 9 · 无法判定 19，未覆盖的全是旧口径整卷答卷；`--current-only` 2 份：**无法判定 2（逐题答卷）· 退出码 0**） |
| 决策记录 | `docs/decisions/ADR-0001 … ADR-0026` + `README.md` 索引 | 通过 |
| 架构说明 | `pre_architecture.md`（本文件，含 mermaid 类图与全部实测事实） | 通过 |
| 试卷资产 | `test_content/real_test/paper_1`、`paper_2`（均题号化：30 题/卷）+ `archive/paper_2.v1`（题号化前的旧版） | 通过（新版题面与旧版逐题比对 30/30 一致） |
| 工具行为证据 | `docs/tracer/`（bench.json / ppl.* / ppl-chunks-all.* / cli*.out / cli-n1.out / prompt.txt / f39-progress-probe.out / f39-log-replay.out / f39-progress-width.out） | 保留 |
| 界面证据 | `docs/ui/arena-window.png`、`docs/ui/arena-choose-model.png`（1020×790 / 820×420，真实渲染） | 通过 |
| 运行产物 | `runs/arena_history.jsonl`（**当前 9 条**：Qwen3-MOE real 档 3 条、Meta-Llama real 档 3 条失败记录、Qwen3-30B real 档 3 条——其中 EXAM 在 `paper_1` 第 15 题被人工取消，见 G15）+ `runs/PROTOCOL`（协议标记）+ `runs/archive/arena_history.pre-adr0021.jsonl`（60 条）+ `runs/archive/arena_history.pre-paper2-v2.jsonl`（6 条）+ `runs/archive/arena_history.pre-per-question-exam.jsonl` + 每轮 Run 的原始流与答卷 | 保留 |

**验收命令**（`./run_arena.sh` 与 `python3 run_arena.py` 等价，前者固定工作目录）：
```bash
python3 tools/check_traceability.py -v      # 文档契约与 ADR 双向可达性
python3 tools/check_completion.py -v        # 答卷覆盖度核对（既有答卷，不必重跑模型）
./run_arena.sh --list-models                # 列出全部模型（含 30B）与文件大小
./run_arena.sh --compare                    # 跨模型对比（真实档数字，时间戳与数字同源）
./run_arena.sh --headless --quick --model <模型名>   # 端到端自检（8B ~25 s / 30B ~40 s）
./run_arena.sh                              # GUI
```

