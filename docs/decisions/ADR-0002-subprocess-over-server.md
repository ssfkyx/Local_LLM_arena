# ADR-0002：用子进程调用 llama 工具，而不是起 llama-server

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0003（Runner 抽象）、ADR-0004（解析器）

## 背景（Context）

三类测试对应三个 llama.cpp 工具：`llama-bench`（吞吐）、`llama-perplexity`（困惑度）、
`llama-cli`（答卷）。它们既可以用 `subprocess` 一次性调用，也可以统一走 `llama-server`
（常驻进程 + HTTP `/v1/chat/completions` 等接口）。

本机的实际需求是：**一次性批处理**——选一个模型，跑完三类测试，拿到数字与文件，落盘历史，退出。
没有并发请求、没有流式会话、没有多客户端。

约束「非必要不使用集成」明确要求：能不给系统加组件就不加。

## 决策（Decision）

统一走 `subprocess` 调用 `llama-bench` / `llama-perplexity` / `llama-cli`，
**不引入 `llama-server`、不引入 HTTP 客户端、不引入端口与生命周期管理**。

进程执行与取消集中在 `arena.process.SubprocessRunner`：两个读取线程按行泵出 stdout/stderr，
主线程按 `poll_seconds` 轮询退出码，同时检查 `cancel_check()` 与 `timeout_s`。

## 理由（Rationale）

- `llama-server` 会额外引入三份复杂度：端口分配与冲突、进程生命周期（守着/杀掉/重启）、
  JSON 客户端与流式解析。本项目一份都用不上。
- `llama-bench -o json` 直接给结构化结果（tracer F1），`llama-perplexity` 的最终值是一行文本，
  `llama-cli -st` 的答卷是文本——用子进程拿原始流反而更容易「留证据、可事后重建」。
- 子进程退出码非零/native crash 是能被观察的失败，不会变成「服务没起来但客户端以为连上了」。

## 后果（Consequences）

- 代价：进度只能从被调工具的文本流里刮（perplexity 依赖 stdout 的 `[k]ppl,` 串）。
  该代价由 ADR-0004 吸收——解析器独立成层，UI 与编排层永不见原始文本。
- 代价：每次 Trial 都重新加载模型（约 2~3 s / 8B-Q8_0）。被接受：分钟级的测试里这不是瓶颈。
- 收益：一次 Trial 的完整证据（命令、stdout、stderr、退出码、墙钟）就是一个
  `ProcessOutcome` 值对象，可直接落盘、可在测试里断言。
- 退路：若上游 TUI 文本格式漂移到无法解析（见 `pre_architecture.md` 风险表），
  再引入 `llama-server + /v1/chat/completions` 作为 Exam 的第二实现——只替换一个 Runner。

## 修订 2026-10-17：子进程放进独立进程组 + 读取线程有界等待

**背景**：验证「运行中取消」这条路径时（用假 llama 工具把子进程伪装成不退出）实测到一个死锁：
只对组长进程 `terminate()`，它会留下仍持有 stdout 管道的孙进程，
于是两个读取线程的 `join()` 永不返回 → Trial 不产记录、界面永久停在 CANCEL、
已完成的测试结果也写不进历史。实测证据：孤儿 `sleep 300` 存活，取消后 4 s 界面仍在运行态且无记录。

**补充决策**：

1. `Popen(..., start_new_session=True)`：子进程成为自己进程组的组长。
2. `_terminate()` 用 `os.killpg(os.getpgid(pid), SIGTERM)` → 5 s 后 `SIGKILL` 收掉**整组**，
   不再只对组长发信号。
3. 读取线程改为 `join(timeout=reader_grace_seconds)`（默认 5 s）：`run()` 由此获得
   **"一定在有限时间内返回"**的硬保证；线程是 daemon，代价是被取消 Trial 的尾部输出可能丢失。
4. 代价：终端 SIGINT 不再自动落到子进程身上。因此 headless 侧把 Ctrl-C 显式实现为"取消"
   （`ConsoleSink.request_cancel()` + `run_headless` 安装 SIGINT 处理函数），语义与 GUI 的 CANCEL 一致。

**验证**：`/tmp/arena_cancel_check.py`（假工具 + GTK 主循环）在修订后通过——
取消 2.6 s 后落盘 `ok=false / error='用户取消'`，圆键复位 START、选择键恢复可用、无孤儿进程残留。

## 修订 2026-10-19：EXAM 改用 `llama-server`（bench / perplexity 仍子进程）

**背景**：用户把 EXAM 的形态定为"同一进程、每题清空上下文、独立请求"（见 ADR-0025）。
"每题重开 `llama-cli`"意味着每卷 30 次、两卷 60 次模型重载——ADR-0002 原本接受的
"每次 Trial 重新加载 2~3 s / 8B"在逐题下会放大成分钟级纯加载，并显著提高 ADR-0017/F18
（环境性 CUDA OOM）的命中概率。

**补充决策**：

1. **EXAM 走 `llama-server`**：模型只加载一次，逐题发 `POST /v1/chat/completions`
   （每题独立、`cache_prompt:false`）。bench 与 perplexity **不变**，仍由
   `SubprocessRunner` 一次性调用——即本 ADR 原本预留的退路（"只替换一个 Runner"）。
2. **server 的生命周期归 `arena.server`**：`LlamaServer` 用 `Popen(start_new_session=True)`
   启动、轮询 `/health` 就绪、收尾用 `killpg`——沿用 2026-10-17 修订确立的"整组终止"纪律。
   仅杀会话首 PID 会留下占显存的孤儿（F34 实测）。
3. **取消语义不变**：客户端关闭连接 → server 自取消该次生成（F31 实测约 1 s 内）。
4. **裸 `/completion` 被明确禁止**：不套 chat template 时 instruct 模型不吐 EOS，会吃到上下文满，
   与 ADR-0020 的完成条件冲突（F29 实测 380 s 死循环）。
5. **解析负担下降**：EXAM 的答案与吞吐改从 server 的 JSON 读，不再依赖 `llama-cli` 的 TUI 文本
   （F29 证明 chat 端点的 `content` 与 `llama-cli -st` 抽取结果逐字一致）。`CliTranscriptParser`
   保留在 `arena.parsers`，但不再接线到 EXAM。

**理由**：这正是 ADR-0002 结论中写下的退路（"若上游 TUI 文本格式漂移到无法解析，再引入
`llama-server` 作为 Exam 的第二实现——只替换一个 Runner"），且 tracer 显示 server 端点与
原 CLI 行为逐字一致，因此改造不改变被测行为，只改变喂题粒度。

**后果**：EXAM 不再有"每次 Trial 重新加载模型"的代价（改为每次 Run 一次加载）；
但新增了端口分配、就绪轮询、进程组收尾三件事（`arena/server.py`）。
`TrialRunner._build_command` 对 EXAM 不再适用（其命令是 server 的 argv，由 `arena.server` 负责），
因此基类该方法改为"子进程型 Runner 实现"的可选钩子。

**验证**：`docs/tracer/f29-*`（端点对齐）、`f31-*`（取消）、`f32-*`（单进程 30 题稳定）、
`f33-*`（能力与 `--context-shift` 默认）、`f34`（进程组收尾必要性）。

## 相关代码位置（Code Locations）

- `arena/process.py::SubprocessRunner.run`（`start_new_session` + 有界 join）、`__init__`
  （`reader_grace_seconds`）
- `arena/process.py::SubprocessRunner._terminate`、`_signal_group`（整组终止）
- `arena/process.py::ProcessOutcome`、`CancellationToken`
- `arena/runners.py::TrialRunner._run_process`（bench / perplexity 启动子进程的地方）
- `arena/runners.py::BenchRunner._build_command`、`PerplexityRunner._build_command`
- `arena/server.py::LlamaServer`（EXAM 的进程生命周期；`Popen(start_new_session=True)` + `killpg`）
- `arena/runners.py::ExamRunner._execute`（逐题请求，见 ADR-0025）
- `arena/config.py::AppConfig.binary`（工具路径必须显式存在，不静默回退 PATH）
- `run_arena.py::ConsoleSink.request_cancel`、`run_headless`（Ctrl-C = 取消）

