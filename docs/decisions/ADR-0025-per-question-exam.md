# ADR-0025：逐题 EXAM（一题一请求，仍一卷一份答卷）

- **状态**：Accepted
- **日期**：2026-10-19
- **相关 ADR**：ADR-0002（子进程 vs server，含 2026-10-19 修订）、ADR-0004（解析器跟随流语义）、
  ADR-0005（JSONL 历史）、ADR-0009（不自动判分）、ADR-0011（答卷只记吞吐）、
  ADR-0013（配置唯一来源）、ADR-0017（失败如实记录）、ADR-0019（`-fitt` 显存适配）、
  ADR-0020（完成条件=模型写完）、ADR-0021（逐卷落盘/部分成功/协议标记）、
  ADR-0022（题号化与覆盖度核对）、
  ADR-0026（题内心跳：其决策 11 的「不做流式 token 进度」由该 ADR 修订——请求仍不流式，
  改为读 `server.log` 的 `print_timing` 行）

## 背景（Context）

需求从"整份卷子一次做"改为"**一道题一道题喂给大模型**"。原实现是"一份试卷一个 prompt、
一个 `llama-cli` 子进程"（ADR-0002/0020），整卷 30 题共享一次生成。

用户的形态选择（2026-10-19）：**同一进程、每题清空上下文、独立请求**——即模型只加载一次，
但每道题是一个互不携带历史的独立请求（兼顾公平与效率，避免"每题重开子进程"的 60 次重载，
那会放大 ADR-0017/F18 的环境性显存问题）。

tracer（`pre_architecture.md` §2 的 F29–F35，原始证据在 `docs/tracer/`）给出了三个硬事实：

- **F29**：`llama-server` 的裸 `/completion` **不能**用于答卷——不套 chat template 时 instruct
  模型不吐 EOS，`n_predict=-1` 会吃到上下文满（实测 16346 token / 380.57 s 的重复死循环）；
  而 `/v1/chat/completions` 正常 `finish_reason='stop'`，且其 `content` 与现有 `llama-cli -st`
  的答案（用 `CliTranscriptParser` 抽取）**逐字一致**。
- **F30**：逐题独立请求的隔离成立；`cache_prompt:false` 时 `cache_n=0`（KV 完全不复用）。
- **F35**：`paper_1` 的题块边界必须同时受 `## ` 节标题约束，否则本节最后一题会吞掉下一节前言
  （实测 `I5` 吞入 F 节的 `### 共同材料`，题块由 211 字符膨胀到 2363）。

## 决策（Decision）

1. **EXAM 改为逐题**：由 `arena.paper.split_paper()` 把试卷切成题块，一题一个 prompt。
   题块边界 = **下一题号** 或 **下一个 `## ` 节标题**，取更近者（F35）。
   前言归属：**无题号的 `## ` 节**视为文档级前言（如 `## 通用要求`）拼进全部题；**节内前言**
   （如 F 节的 `### 共同材料`）只拼进该节的题。题号语法与 `arena.coverage` 同源。
2. **一题一个独立 HTTP 请求**：走 `llama-server` 的 `POST /v1/chat/completions`，
   请求体只带该题消息、显式 `cache_prompt:false`（F29/F30）。**禁止使用裸 `/completion`**。
3. **完成条件不变**（ADR-0020）：`max_tokens=-1`，生成到 EOS 或上下文满；
   server 的 `--context-shift` 默认关闭（F33），因此"上下文满即停"。
4. **记录粒度不变**（用户决定"先内部逐题、仍一卷一份答卷"）：逐题答案按题号顺序拼回**一份**
   `AnswerSheet`，块首用**中性标签**（`#### 第 3 题 · R3｜约束排序`）而**不是**题号标题行。
   `ExamRecord`/`AnswerSheet`/JSONL schema **不变**；`ExamLoad` 仍记
   `n_predict/n_ctx/temperature/seed`。
   **覆盖度口径随之改变**：逐题后"有没有答到某题"已由执行过程决定，不再从答卷文本推断；
   完整性 = **产出非空回答的题数**（`Coverage(basis="逐题应答")`）。之所以**不**注入
   `### K1｜…` 这类题号标题，是因为那会让 ADR-0022 的题号核对去核对"我们自己排版的字"——
   实测在 quick 档 64-token 截断下也会报 `30/30`，属本仓库明确拒绝的"假精确"。
   ADR-0022 的文本核对**保留**用于历史整卷答卷与 `tools/check_completion.py` 回查，但**对逐题答卷不适用**：
   `arena.coverage.compare` 见到逐题块首标签（`PER_QUESTION_BLOCK_RE`）即返回
   "无法判定（逐题答卷（每题独立作答），文本核对不适用）"，因此 `answers.txt` 的"导出时重算"与
   `tools/check_completion.py` 都落到"无法判定"档（不计失败、退出码 0）。
   **实测教训**：不加这条判断时，模型自己答案里偶然出现的行首题号会让 `check_completion` 报出
   "覆盖 3/30（未覆盖）"的**误报**——那正是 ADR-0022 说的"会误报的检查比没有检查更糟"。
   （该块首标签因此成为 `arena.runners` 与 `arena.coverage` 之间的格式契约，改动需两侧同步。）
5. **整卷吞吐为逐题求和比**：`AnswerSheet.prompt_ts = Σprompt_n / Σprompt_time`、
   `generation_ts = Σpredicted_n / Σpredicted_time`（沿用 ADR-0011"只记吞吐"，不记 token 数）。
6. **逐题原始证据落盘**：每题一条 `q-<卷>-<题号>.json`（prompt + content + timings），
   server 的 stdout/stderr 记为 `server.log`——数字仍可回溯到原始文本（ADR-0005/0004）。
7. **失败下移到题**：某题失败即记 `PaperFailure(paper_name, "<题号>: 原因")` 并停止该卷后续题
   （与既有"抽取失败即停"一致：同一格式问题重复发生没有意义）；已完成的题照常落盘（ADR-0021）。
8. **超时语义改为"每题"**：`TrialTimeouts.exam` 仍是退出保护，但计时单位从"每份试卷"改为"每题"。
9. **取消**：请求在 worker 线程执行、主线程按 `sink.is_cancelled()` 轮询；取消即**关闭连接**，
   server 会在约 1 s 内自动取消该次生成（F31），无需重启 server、无需槽位擦除。
10. **历史口径**：EXAM 从"整卷"变"逐题"，**旧整卷答卷不可与新逐题答卷并列比较**。
    处理方式沿用 ADR-0022 的窄口径先例：**不改 `PROTOCOL_NAME`**（对比表只收 bench/PPL，口径未变），
    由人把当前机读索引移入 `runs/archive/`。**代码不代劳**（ADR-0021：重置历史是人的动作）。
11. **明确不做**：判分（ADR-0009）；逐题进记录 schema；题目语义解析；多轮会话（每题独立，
    不携带历史）；~~流式 token 进度（最小进度粒度=题）~~ → **2026-10-19 由 ADR-0026 修订**：
    请求**仍然不流式**（改请求结构就落在"同参数可比"之外），改为每 10 s 读一次 `server.log` 的
    `slot print_timing` 行，把"本题 N tok · R t/s · 日志 Ns 前"报进进度条。

## 理由（Rationale）

- **先有证据再改架构**：F29 直接否掉了"用裸补全"这条看起来最省事的路——它会把 ADR-0020 的
  "完成条件=模型写完"退化成"跑满上下文"，而 chat 端点与现有 `llama-cli -st` 逐字一致，
  说明这次改造**不改变被测行为**，只改变喂题粒度。
- **`cache_prompt:false` 是"每题清空"的可核验表达**：默认开启时 `cache_n≈28`（模板前缀复用），
  虽无内容泄露（F30），但要"清空"成为事实而不是口头承诺，就必须显式关闭并让 `cache_n=0` 可核验。
- **切题规则的坑必须写进 ADR**：F35 的 I5 污染是"看起来对、实际错"的典型——不写下来，
  实现者一定会再踩一次。
- **记录不扩 schema 是刻意的**：用户明确选择"改动小"。代价是逐题细节只能从
  `docs/tracer`/运行目录的逐题 JSON 与答卷文件头的覆盖度读，而不是从历史索引读（知情接受）。

## 后果（Consequences）

### 实测证据（2026-10-19，tracer，`docs/tracer/`）

| 场景 | 结果 |
|---|---|
| 端点选择（F29） | 裸 `/completion`：`stop_type='limit'`、16346 token、380.57 s、24868 字符重复；`/v1/chat/completions`：`stop`、424 token、9.94 s；后者与 `llama-cli -st` 抽取答案**逐字一致**（715 字符） |
| 隔离（F30） | 追问探针回"不知道。"，无泄露；`cache_prompt:false` → `cache_n=0` |
| 取消/超时（F31） | 客户端断开后 server 约 1 s 内 `cancel task`，`is_processing` 转 `false`；随后请求 0.26 s 正常 |
| 稳定性（F32） | 单进程连续 30 题：30/30 成功、0 空答、`finish_reason` 全 `stop`，总 132.17 s、单题均值 4.41 s、GPU 稳定 ~10965 MiB |
| 实现后端到端（2026-10-19，`--quick`，隔离产物目录） | exit 0、85.5 s；EXAM **60 次请求**（2 卷 × 30 题）、进度 `卷 k/2 · 题 m/30` 正常、两卷各出一份答卷；**并当场发现并修掉**"注入题号标题使覆盖率恒为 30/30"的假精确——改为"逐题应答"口径（见决策 4）；`runs/` 与 `arena_history.jsonl` 未被触碰 |
| 切题（F35） | 修正边界后 30 题全部切出；`通用要求` 进全部题、F 节材料只进 L1–L5；L1/L5 运行时引用 D 编号，材料确实被使用 |
| 收尾（F34） | server 必须按**进程组**收尾：只杀会话首 PID 会留下占 10972 MiB 的孤儿 |
| **真实档端到端（F36）** | 2026-10-19 02:26 · Qwen3-MOE · real 档：EXAM **255.2 s**、60 次请求全成功、`sheets=2`、`failures=0`；**60 题 `stop_reason` 全为 `stop`（无一截断）**；60 份逐题证据 + `server.log`；`--compare` pp 4300.4 / tg 50.9 / **PPL 14.1946**（与 F26 同模型全语料值一致） |
| **取消路径（F37）** | 逐题进行中取消 → `ok=False / error='用户取消'`、`paper_failures=[{paper_1, "K3: 用户取消"}]`（**精度到题**）、`sheets=0`；已完成题的逐题 JSON 仍在盘上；无 `llama-server` 残留 |

### 代价与残余限制

- **题内无进度**：非流式请求，进度最小粒度是"题"；ADR-0021 的"最后输出距今"这一档不再提供。
  F32 里单题 0.149–13.62 s，短题足够；大模型长题时这一档会明显变粗。
  **（2026-10-19 修订：由 ADR-0026 补上题内心跳——不改请求结构，改读 `server.log` 的
  `print_timing` 行。这一档在实证上真的发生过，见该 ADR 的背景。）**
- **在飞那一卷的 `.md` 仍会丢，但逐题证据不丢**：答卷 `.md` 在该卷 30 题全部完成后才写；
  若中途取消/失败，已完成题的 `q-*.json` 与 `server.log` 仍在盘上（F37 实测），
  比旧实现"整卷零留痕"更好，但**没有**把逐题答案拼进任何 `.md`。
- **前言拼装改变了 prompt**：逐题 prompt 会比原整卷重复携带 `通用要求`（160 字符）与
  共享材料（2136 字符）。这**不改变"同参数"**（每题都带同一份前言），但 prompt 总量上升。
- **整卷吞吐是加权比，不是"一次测量的 t/s"**：与旧整卷的数字不可直接比较（口径不同）。
- **第二款模型本轮未验证成（F38）**：Meta-Llama-3.1-8B-Instruct 在**加载模型阶段**环境性失败
  （`unable to allocate CUDA0 buffer` / `failed to create_context`），按 ADR-0017 如实记录、不重试；
  其 3 条 `ok=False` 记录留在当前索引里（`--compare` 该行显示 `—`）。逐题路径目前只在 Qwen3-MOE 上
  端到端验证过。这次失败顺带验证了新的 **server 启动失败路径**：EXAM 落 `ok=False` + 完整原因 +
  `server.log` 留证 + 无残留进程。
- **超显存模型在 server 上的 `-fitt` 部分卸载未验证**（唯一超显存模型是 30B，按用户要求不跑）。
- **旧整卷答卷与新逐题答卷不可并列**：需要人把旧机读索引移入 `runs/archive/`
  （见决策 10；本次已由人执行为 `runs/archive/arena_history.pre-per-question-exam.jsonl`）。

### 2026-10-19 补丁：上下文打满时保留已生成内容

实测 `runs/2026-10-19T16-24-29_Qwen3-30B-A3B/` 的 `C5`：`predicted_n=16096`、
`wall_seconds=565.9`、`stop_reason=length`、`content=""`；`server.log` 的 `task 27891`
`samples` 到 `n_tokens=16383, truncated=1`。这不是进程崩溃——`llama-server` 默认
`reasoning-format=deepseek` 把整段思考链放进了 `message.reasoning_content`，而旧代码只读
`message.content`，于是 16k token 的已生成文本被丢掉，答卷块为空。

按用户口径（**不关闭 thinking、不改变正常答卷**）：

1. `Completion` 增加 `reasoning` 字段，承载 `message.reasoning_content`（兼容 `reasoning`）；
2. `ExamRunner` 仍以 `content` 为正常正文；仅当 `content` 为空、`reasoning` 非空时，
   用 `reasoning` 作为该题正文落盘，**不判失败，继续下一题**；
3. `q-*.json` 增记 `reasoning` 与 `recorded_from`（`content` / `reasoning_content` / `none`），
   `stop_reason=length` 仍原样保留，读者可据此区分"最终答案"与"上下文打满的未完成思考"；
4. 答卷块首格式与"继续下一题"的执行语义不变；`Coverage` 仍按 `content` 是否非空计算——
   reasoning 兜底只负责留证，不把"没写完的思考"伪装成答完（决策 4 的反假精确口径）。

代价：上下文打满的题会把很长的思考链写进答卷块，同时覆盖度仍把它记为未答完；
这是"保留已生成内容"与"不虚报覆盖"两条约束的直接结果，不是新的判分口径。

## 相关代码位置（Code Locations）

- `arena/paper.py::Question`、`question_ids`、`split_paper`
- `arena/server.py::LlamaServer`（start/stop/base_url）、`ChatCompleter.complete`、
  `Completion`、`CompletionCancelled`、`CompletionTimeout`、`ServerStartError`
- `arena/runners.py::ExamRunner._execute`、`_build_record`、`_new_record`、`_report_alive`
- `arena/config.py::TrialTimeouts`（`exam` 语义=每题、`server_startup`）
- `arena/coverage.py::question_ids`（复用 `arena.paper` 的题号语法）
- `arena/vocabulary.py::PROTOCOL_NAME`（本 ADR 决定**不改**）
- `docs/tracer/f29-*`、`f30-*`、`f31-*`、`f32-*`、`f33-*`、`f35-*`（原始证据）
