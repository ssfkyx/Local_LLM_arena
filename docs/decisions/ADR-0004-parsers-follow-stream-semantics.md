# ADR-0004：解析器跟随「流语义」而不是「想象中的格式」

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0002（子进程）、ADR-0003（模块边界）、ADR-0011（答卷吞吐）

## 背景（Context）

三个 llama 工具的输出格式是**实测出来的事实**，而且与"想当然"差别很大：

| 事实 | 内容 | 证据 |
|---|---|---|
| F1 | `llama-bench -o json` 的 **stdout 是纯 JSON 数组**；CUDA 日志只走 stderr | `docs/tracer/bench.json` / `bench.stderr` |
| F2 | `llama-perplexity`：逐 chunk 进度串走 **stdout**；`Final estimate: PPL = x +/- y` 走 **stderr** | `docs/tracer/ppl.out` vs `ppl.err` |
| F3 | perplexity 的 stderr 行带 `秒.毫秒.微秒 I ` 前缀（且可能与上一行拼接）→ 必须**行内搜索** | `ppl.err` 尾行 |
| F4 | `llama-cli -st` 的 stdout 是 TUI 文字记录：加载转轮 + banner + 命令帮助 + `> <回显>` + 答案 + 性能页脚 + `Exiting...` | `docs/tracer/cli.out` |
| F6→F12 | 原以为"回显之后即答案"。**实测只对 ≤500 字节的 prompt 成立**：`tools/cli/cli-ui.h:161` 在 `buffer.size() > 500` 时只回显前 500 字节并追加 ` ... (truncated)` | 源码行 + 23:14 那次 EXAM 解析失败的记录 |

F6 是本项目**唯一被实测推翻的架构假设**：旧实现按"完整回显"定位答案起点，
在 19 KB 的中文试卷上直接抛错（历史里那条 `ok=false` 的记录就是现场）。

## 决策（Decision）

1. **解析逻辑全部集中在 `arena/parsers.py`**，UI 与编排层永远看不到原始文本。
2. **每个解析器只认自己那条流的语义**：
   - `BenchJsonParser.parse(stdout)`：直接 `json.loads`，**绝不读 stderr**（含 CUDA 噪声）。
   - `PerplexityLogParser.parse_final(stderr)` / `parse_setup(stderr)`：行内正则；
     `parse_progress(stdout, total)`：进度只从 stdout 取。
   - `CliTranscriptParser.extract_answer(stdout, prompt)`：先按性能页脚截断，再**优先匹配
     ` ... (truncated)` 标记**（长 prompt 路径），退化为完整回显匹配（短 prompt 路径）；
     两条路都找不到时抛 `TranscriptParseError` 而不是返回整段记录。
3. **解析失败必须抛 `ParseError` 子类**，由 `TrialRunner.run` 转成 `ok=False` 记录；
   同时原始流已落盘（`runs/<run_id>/<tool>.out|.err`），保证"数字可回溯到原文"。
4. **数值一律 `float()`/`int()` 显式转换**，落地为 Python 内建类型（见 ADR-0006）。

## 理由（Rationale）

- 脆弱性不可消除（格式由上游决定），但可以**关进一个文件**：格式漂移时只改 `parsers.py`，
  且有单测可依赖的纯函数签名（输入文本 → 输出值）。
- 「拒绝猜测」是硬要求：定位不到答案起点时返回整段记录，会把 banner 与帮助文本写进答卷文件——
  这种"看起来成功了"的失败比直接报错更贵。
- 解析器完全不知道文件系统与进程，因此可以在测试里直接喂 tracer 的原始文件。

## 后果（Consequences）

- 代价：`parsers.py` 里的正则与标记是"上游格式的镜像"，上游一改就要跟进（风险登记在
  `pre_architecture.md` §10）；缓解手段是原始流留档 + 抽取器可单测。
- 代价：`pre_architecture.md` 里 F6 必须显式标注「已推翻」，否则文档会继续骗人。
- 收益：一次 EXAM 失败不会污染历史——它是一条带原因的 `ok=false` 记录，而不是半个答卷对象。

## 相关代码位置（Code Locations）

- `arena/parsers.py::BenchJsonParser.parse`
- `arena/parsers.py::PerplexityLogParser.parse_final`、`parse_setup`、`parse_progress`
- `arena/parsers.py::CliTranscriptParser.extract_answer`、`parse_throughput`、`_cut_footer`
- `arena/parsers.py::ParseError`、`BenchParseError`、`PerplexityParseError`、`TranscriptParseError`
- `arena/runners.py::BenchRunner._build_record`、`PerplexityRunner._build_record`、
  `ExamRunner._build_record`
- `arena/runners.py::TrialRunner._run_process`（原始流落盘）
