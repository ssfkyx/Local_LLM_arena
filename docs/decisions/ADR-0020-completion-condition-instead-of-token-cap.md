# ADR-0020：完成条件由「模型写完 / 跑完语料」定义，参数值降级为退出保护

- **状态**：Accepted
- **日期**：2026-10-17
- **相关 ADR**：ADR-0004（解析器跟随流语义）、ADR-0006（FP64）、ADR-0009（不自动判分）、
  ADR-0011（答卷只记吞吐）、ADR-0013（配置是唯一来源）、ADR-0017（失败如实记录）、
  ADR-0018（负载档与不提供参数面板）、ADR-0019（`-fitt` 显存适配）

## 背景（Context）

旧负载用「参数上限」充当「完成条件」，两处都不成立：

1. **答卷 `-n 1024`**（`ExamDefaults.n_predict`）：模型写到第 1024 个 token 被硬截断。
   试卷的评分点是「最终答案」，被截断的答卷可能只是写了一半；拿它比质量，
   比的是「前 1024 个 token 的质量」，而不是这个模型答这份卷子的质量。
2. **PPL `--chunks 8`**（`PerplexityDefaults.chunks`）：只测语料前 8 个 chunk（8 × 512 token）。
   实测 `wiki.test.raw` 为 1 290 590 字节，全语料实际是 **564 chunk**（Meta-Llama 分词器）/ **584 chunk**
   （Qwen-MOE 分词器）——chunk 数是**模型相关**的（语料 token 数 / n_ctx），
   所以 8 chunk 只覆盖约 **1.4%**；用 1.4% 的样本去排「困惑度」名次，样本误差与「跑没跑完」混在一起，无法区分。

llama.cpp 这两个工具都原生支持「不设上限」：

| 工具 | 参数 | 默认 | 语义（`--help` 与源码核对） |
|---|---|---|---|
| `llama-cli` | `-n / --n-predict` | `-1` | `-1 = infinity`：生成到 EOS 或上下文满 |
| `llama-perplexity` | `--chunks` | `-1` | `-1 = all`：`n_chunk = params.n_chunks < 0 ? n_chunk_max : min(params.n_chunks, n_chunk_max)`（`llama.cpp/tools/perplexity/perplexity.cpp:493,515`） |

也就是说：**「不限」不需要我们发明常量，它就是工具的默认行为。**

### 实测证据（2026-10-17，本机 RTX 4080 Laptop / 8B Q8_0）

| 命令 | 结果 | 原始流 |
|---|---|---|
| `llama-perplexity -f <截断语料 90 926 B> -c 512 --chunks -1 -fitt 1024` | **exit 0** · stderr 自报 `over 41 chunks` · `Final estimate: PPL = 7.1239 +/- 0.16640` · stdout 耗时行 `0.17 minutes` | `docs/tracer/ppl-chunks-all.out/.err` |
| `llama-cli -st -c 2048 -n -1 --temp 0 --seed 42 -fitt 1024 -p "请只输出结果：1+1=?"` | **exit 0** · 答案 `2` · 页脚 `[ Prompt: 921.9 t/s \| Generation: 34.3 t/s ]` · 模型在 EOS 自然停止 | `docs/tracer/cli-n1.out/.err` |
| **软件端到端真实档**：`./run_arena.sh --headless --model Meta-Llama-3.1-8B-Instruct`（`ARENA_CONTENT_DIR`/`ARENA_RUNS_DIR` 都指向 `/tmp`，语料换成上表那份截断版、试卷换成 1 行迷你卷，以便在数十秒内跑完） | **exit 0** · 31.2 s · 日志出现 `PERPLEXITY: chunk 41/41`（分母来自 stderr 自报，旧代码在 `chunks=-1` 时给不出分母）· 落盘 `n_chunks=41`、`ExamLoad.n_predict=-1`、2 份答卷 | `/tmp/arena_smoke_runs/`（一次性，不随仓库留存） |
| **真实档全语料 · Meta-Llama-3.1-8B-Instruct**（`./run_arena.sh --headless --model Meta-Llama-3.1-8B-Instruct`，真实 `wiki.test.raw` 与真实试卷） | **exit 0** · **522.3 s** · pp **2957.1** / tg **35.7** t/s · 落盘 `n_chunks=564`、`ppl=7.3289 ± 0.0468`、`ExamLoad.n_predict=-1` · 2 份答卷，`paper_1` **29562 B**（旧口径同题仅 3716 B） | `runs/2026-10-17T23-27-09_Meta-Llama-3.1-8B-Instruct/`、`runs/arena_history.jsonl` 第 55~57 行 |
| **真实档全语料 · Qwen3-MOE-2x4B-8B**（同上命令） | **exit 0** · **230.7 s** · pp **2492.0** / tg **39.0** t/s · 落盘 `n_chunks=584`、`ppl=14.1946 ± 0.1305`、`ExamLoad.n_predict=-1` · 2 份答卷 | `runs/2026-10-17T23-37-44_Qwen3-MOE-2x4B-8B-Jan-Nano-Instruct-II/`、`runs/arena_history.jsonl` 第 58~60 行 |

这些证据各证明一件事：A) `--chunks -1` 会让工具跑到语料尽头，并把**真实的 N** 打在 stderr 上；
B) `-n -1` 被工具接受，且不会把答案截断（短提示走完整回显分支，抽取器表现不变）；
C) 新口径在**应用内**真的生效（命令、进度分母、落盘字段三处对得上）；
D) 全语料在两个 8B 上都真的跑完了，PPL 的标准误从 8-chunk 时代的 ±0.42/±1.11 降到 **±0.05/±0.13**
（样本大了，估计自然更稳），而答卷体积显示旧口径确实在截断（8 倍差距）。

## 决策（Decision）

1. **答卷完成条件 = 模型自己写完**：`ExamDefaults.n_predict = -1`，命令行照发 `-n -1`。
   `ExamLoad.n_predict = -1` 直接落盘，含义即「不限」，不新增字段、不新增哨兵常量。
   答案质量仍靠人读文件（ADR-0009 不变）。
2. **PPL 完成条件 = 跑完整语料**：`PerplexityDefaults.chunks = -1`，命令行照发 `--chunks -1`。
   工具自报的 `calculating perplexity over N chunks` 的 N 随记录落盘
   （`PerplexityResult.n_chunks`）——「是否跑完」因此是可核验的事实（与 ADR-0004、F13 同源）。
3. **参数值降级为退出保护**：`TrialTimeouts.perplexity` 由 7200 s 收紧为 **3600 s（60 分钟）**；
   `TrialTimeouts.exam` 保持 **3600 s，且是每份试卷各计一次**。
   到点即终止子进程并落 `ok=false`（ADR-0017：不重试、不降级、不产半真记录）。
4. **对所有模型一致生效**：不按文件大小/吞吐分支，不开环境变量、不开参数面板（ADR-0018 决策 7 不变）。
5. **quick 档不被波及**：`AppConfig.quick()` 仍是 `chunks=2` / `n_predict=64`——
   它是数十秒级自检，不承载「完成条件」语义（ADR-0018）。
6. **进度分母来自工具自报**：`PerplexityRunner` 同时监听 stderr 的 setup 行；
   在拿到 N 之前不报进度（不猜分母、不报假比例）。为此 `TrialRunner._run_process`
   增加可选的 `on_stderr_line` 回调，留档内容不变。

## 理由（Rationale）

- 完成条件应由「任务本身做完了没有」定义，而不是由「我们给的 token 上限」定义。
  把上限降级成保护阀后，**超时是异常（如实记失败），被截断才是常态**——旧口径正好相反。
- 用工具原生默认值（-1）表达「不限」，不引入自定义常量、不需给记录加字段；
  「-1 = 不限」这件事有 `--help` 与源码两处可查，不靠我们的注释自证。
- 全语料 PPL 的 N 由工具自报，沿用 F13「以工具自报为准」，同时天然给出「跑完了没有」的证据。
- 两条规则对所有模型一致，因此跨模型比较的分母（同一份语料、同一份试卷）不再随参数变化。

## 后果（Consequences）

- **代价（必须如实说明）**：低吞吐模型可能在全语料 PPL 上撞到 60 分钟保护阀，该 Trial 落 `ok=false`
  ——**「没有 PPL 数字」本身就是结果**。实测（两个 8B）：全语料 564~584 chunk 用 **123.1 s / 133.8 s**；
  30B（pp ≈ 124 t/s，见 ADR-0019 的实测）按吞吐比外推约 **45~50 分钟**，紧贴 3600 s 阀。
  **按用户要求不跑 30B**（会拖垮本机 WSL2），所以它能否在阀内跑完保持未知。
  即「同一条规则，不同模型给出不同的可完成性」——这正是本轮要暴露的事实，不是缺陷掩饰。
- 代价：`ExamLoad.n_predict` 出现负值，读历史时要知道「负数 = 不限」；
  旧记录仍是 1024。跨档比较只在同一档内发生（ADR-0018），新旧不混用。
- 代价：答卷长度不再可比（模型自己决定何时停），跨模型比的是「质量」与「吞吐」，不是长度。
- 代价：进度条在拿到 stderr 的 setup 行之前不动；且工具 stdout 被管道缓冲时
  进度行可能只刷出一部分（实测 41 chunk 只见到前 19 个序号，`docs/tracer/ppl-chunks-all.out`）——
  这是上游缓冲行为，进度本来就是「尽力而为」，取数不依赖它。
- 收益：试卷不再被截断（去掉 1024 上限，实测同一题的答卷体积从 3716 B 涨到 29562 B）；
  PPL 样本从约 1.4% 提升到全语料，标准误从 ±0.42/±1.11 降到 **±0.05/±0.13**；
  两处都从「参数约定」变成「可核验事实」。
- 未验证：**30B 的新口径完全没跑**（按用户要求，跑它会拖垮本机 WSL2），它能否在 3600 s 内跑完全语料未知；
  另外每个 8B 只跑了 **1 次**，全语料 PPL 的**跨轮重现性**（热条件漂移，见 §10）尚未采样。
  见 `pre_architecture.md` §9「未验证项」。

## 相关代码位置（Code Locations）

- `arena/config.py::PerplexityDefaults`、`ExamDefaults`、`TrialTimeouts`、`AppConfig.quick`
- `arena/session.py::ArenaSession.default_plan`（把配置装进 `TestPlan` 的那一处）
- `arena/domain.py::TestPlan`、`ExamLoad`
- `arena/runners.py::PerplexityRunner._build_command`、`PerplexityRunner._execute`
- `arena/runners.py::ExamRunner._build_command`、`ExamRunner._execute`
- `arena/runners.py::TrialRunner._run_process`
