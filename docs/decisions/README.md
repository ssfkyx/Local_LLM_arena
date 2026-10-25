# Architecture Decision Records（ADR）索引

本目录是 Local Arena 的全部架构决策记录。编号连续（ADR-0001 ~ ADR-0028），
**代码引用 ADR，ADR 反引用代码位置**——双向可查由 `tools/check_traceability.py` 机械校验。

## 怎么读

- 想知道「为什么这么写」：从代码文件头的「相关 ADR」跳到对应文件。
- 想知道「决策落在哪儿」：看 ADR 末尾的「相关代码位置」（`路径::符号`）。
- 想加新决策：下一个编号，按「状态 / 背景 / 决策 / 理由 / 后果 / 相关代码位置」六节写，
  并在被改动代码的文件头与 docstring 里回填引用。

## 清单

| 编号 | 决策 | 状态 |
|---|---|---|
| [ADR-0001](ADR-0001-readability-and-traceability.md) | 可读性与可溯源性（文件头/docstring 契约 + 静态自检 + ADR 双向引用） | Accepted |
| [ADR-0002](ADR-0002-subprocess-over-server.md) | 用子进程调用 llama 工具；**2026-10-19 修订**：EXAM 改用 `llama-server`（bench/perplexity 仍子进程） | Accepted |
| [ADR-0003](ADR-0003-runner-polymorphism-and-module-boundaries.md) | Runner 模板方法 + 模块依赖方向（TestPlan/Sink 下沉断环） | Accepted |
| [ADR-0004](ADR-0004-parsers-follow-stream-semantics.md) | 解析器跟随实测流语义（ppl 在 stderr、进度在 stdout、答卷靠截断标记） | Accepted |
| [ADR-0005](ADR-0005-jsonl-archive.md) | 历史用追加式 JSONL，原始流另存文件 | Accepted |
| [ADR-0006](ADR-0006-fp64-arithmetic.md) | 浮点一律 FP64（内建 `float` + 单调计时） | Accepted |
| [ADR-0007](ADR-0007-external-css-theme.md) | 主题是外置 CSS，类名即契约，解析错误即异常 | Accepted |
| [ADR-0008](ADR-0008-worker-thread-ui-bridge.md) | UI 线程只画图，试炼在 worker 线程，取消单一来源 | Accepted |
| [ADR-0009](ADR-0009-no-automatic-grading.md) | 本阶段不自动判分 | Accepted |
| [ADR-0010](ADR-0010-comparison-across-models.md) | 跨模型对比是并列（取最新有效值），不是聚合 | Accepted |
| [ADR-0011](ADR-0011-answer-sheet-reports-throughput.md) | 答卷记录吞吐，不记录 token 数 | Accepted |
| [ADR-0012](ADR-0012-vram-not-a-constraint.md) | 不预先为显存做分层策略 | Superseded by ADR-0015 |
| [ADR-0013](ADR-0013-config-as-single-source.md) | 配置是环境事实的唯一来源 | Accepted |
| [ADR-0014](ADR-0014-headless-and-quick-run-modes.md) | `--headless` 与 `--quick` 两种非 GUI 运行方式 | Accepted |
| [ADR-0015](ADR-0015-vram-budget-excludes-oversized-models.md) | 用显存预算硬排除超规格模型（30B） | Accepted |
| [ADR-0016](ADR-0016-scoreboard-hint-display-policy.md) | 记分板提示列显示策略（值列只放事实，路径只作提示） | Accepted |
| [ADR-0017](ADR-0017-environmental-cuda-oom-is-recorded-not-retried.md) | 环境性 CUDA OOM 只如实记录，不自动重试/降级 | Accepted |
| [ADR-0018](ADR-0018-load-profiles-and-no-parameter-panel.md) | 负载档只有两种（real/quick），quick 只作自检且不进对比表；不提供参数面板 | Accepted |
| [ADR-0019](ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md) | 用工具的 `-fitt` 显存适配取代静态排除（允许超显存模型） | Accepted |
| [ADR-0020](ADR-0020-completion-condition-instead-of-token-cap.md) | 完成条件=模型写完/跑完语料（`-n -1`、`--chunks -1`），参数值只作退出保护 | Accepted |
| [ADR-0021](ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md) | 跑批生命周期（关窗/信号统一收尾）、EXAM 逐卷落盘与部分成功、`PROTOCOL` 标记与跨协议告警 | Accepted |
| [ADR-0022](ADR-0022-answer-coverage-check.md) | 试卷题号化 + 答卷覆盖度核对（EXAM 收尾事实，不改 `ok`；含独立工具） | Accepted |
| [ADR-0023](ADR-0023-run-answer-text-export.md) | 答卷纯文本导出（一轮一份 `answers.txt`，离线可回查） | Accepted |
| [ADR-0024](ADR-0024-progress-bar-inline-text.md) | 进度条内联文本（进度与事实同行显示） | Accepted |
| [ADR-0025](ADR-0025-per-question-exam.md) | 逐题 EXAM（一题一请求、`/v1/chat/completions`、`cache_prompt:false`，仍一卷一份答卷） | Accepted |
| [ADR-0026](ADR-0026-in-question-progress-from-server-log.md) | 题内心跳：读 `llama-server` 日志的 `print_timing` 行做进度，**不改请求结构**（修订 ADR-0025 决策 11） | Accepted |
| [ADR-0027](ADR-0027-persisted-model-directory.md) | 模型目录作为用户偏好持久化（XDG 配置、原子写入、环境变量仍优先；GUI 增加 `MODEL FOLDER`） | Accepted |
| [ADR-0028](ADR-0028-persisted-llama-bin-directory.md) | llama.cpp 目录作为用户偏好持久化（配置 v2、v1 内存迁移、GUI `LLAMA BIN`、选择期校验三个工具） | Accepted |

## 自检

```bash
python3 tools/check_traceability.py
```

检查项：模块头五字段、公开函数四段契约、ADR 双向可达性、ADR 编号连续性、ADR 六节完整性、
源码中无 TODO/FIXME/XXX。失败时以非零码退出并逐条列出「文件:行 问题」。
