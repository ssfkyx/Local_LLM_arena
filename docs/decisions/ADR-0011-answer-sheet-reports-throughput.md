# ADR-0011：答卷记录的是吞吐，不是 token 数

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0004（解析器）、ADR-0009（不判分）、ADR-0010（对比）

## 背景（Context）

`AnswerSheet` 需要携带"这次答卷有多快"。可选来源：

1. `llama-cli` 的 TUI 性能页脚：`[ Prompt: 2484.9 t/s | Generation: 27.9 t/s ]`；
2. 从输出文本反推 token 数再除以墙钟；
3. 让 `llama-cli` 打印 token 计数（改命令行参数/换工具）。

实测（tracer F4）：页脚稳定存在，且是工具**自报**的吞吐。
实测事实：该版本的 `llama-cli -st` **不报告 token 数**。

## 决策（Decision）

1. `AnswerSheet` 记录 `prompt_ts` / `generation_ts`（单位 t/s）与 `wall_seconds`，
   **不记录 token 数**。
2. 吞吐取自页脚，由 `CliTranscriptParser.parse_throughput` 行内正则提取，解析失败抛
   `TranscriptParseError`（不猜、不置零）。
3. 答卷 Markdown 文件头把两个吞吐与墙钟一起写出（阅卷人先看到"谁答的、多快"）。

## 理由（Rationale）

- 工具自报值 = 单一事实来源；从文本反推 token 数会引入"中文一字几 token"的假设，
  这种假设在不同分词器上不成立。
- 若强行记录 token 数，就必须引入 `llama-tokenize` 或改 `llama-cli` 调用方式——
  为一条辅助信息增加一个工具依赖，不符合「非必要不使用集成」。
- 吞吐本身就是最有用的信息：它让「答卷质量」与「作答成本」可以分开讨论。

## 后果（Consequences）

- 代价：无法从记录里回答"这份答卷到底生成了多少 token"。缓解：`wall_seconds` 与
  `generation_ts` 相乘可给出量级估计，但**结论必须标明这是估计**。
- 代价：若上游页脚格式变更，吞吐解析失败会让整条 Exam 记录变成 `ok=false`
  （而不是"答卷成功但吞吐缺失"）。这是刻意的：宁可暴露格式漂移，也不产出半真记录。
- 收益：`Scoreboard` 与答卷文件都能显示同一组数字，无二次计算。

## 相关代码位置（Code Locations）

- `arena/parsers.py::CliTranscriptParser.parse_throughput`、`_FOOTER_RE`、`_cut_footer`
- `arena/domain.py::AnswerSheet`（`prompt_ts` / `generation_ts`）
- `arena/runners.py::ExamRunner._build_record`
- `arena/store.py::AnswerSheetWriter._render`
- `arena/history.py::ScoreboardAssembler.build`
