# ADR-0006：浮点一律 FP64（内建 float + 单调计时）

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0004（解析器）、ADR-0010（对比）

## 背景（Context）

运行环境约束原文要求：「要求使用浮点数时统一使用FP64」。

候选做法：
1. 引入 `numpy.float64` / `decimal.Decimal` 显式表达 FP64；
2. 直接用 Python 内建 `float`；
3. 用 `struct`/`array` 打包成 `float32` 以"省内存"。

实测（tracer F8）：Python 内建 `float` 就是 IEEE-754 双精度（`epsilon = 2.22e-16`），
`time.perf_counter()` 同样是 FP64 且单调。

## 决策（Decision）

1. **所有浮点一律 Python 内建 `float`**：`data class` 字段、解析器返回值、计时结果，无例外。
2. **禁止 float32 路径**：不引入 `numpy`，不使用 `struct.pack('f')` / `array('f')`
   ——本项目不依赖 numpy，因此该禁令零成本可守。
3. **计时只用 `time.perf_counter()`**：`SubprocessRunner` 与 `TrialRunner` 都不使用
   `time.time()`——后者不单调，NTP 校时会污染墙钟。
4. **时间戳（人类可读）用 `datetime.now(timezone.utc).isoformat(timespec="seconds")`**：
   它只用于记录「什么时候跑的」，不参与任何算术。

## 理由（Rationale）

- 需求说的是「精度统一」，内建 `float` 已经满足，引入 `numpy` 只为类型名好看，违反
  「非必要不使用集成」。
- `decimal.Decimal` 是十进制精确算术，与 llama 工具输出的二进制浮点语义不符，
  且会让 `json.dumps` 需要自定义编码器——纯增加耦合。
- FP64 约束**不能靠"记得"**，所以把它写成 ADR 里的硬禁令 + 本次决策的后果说明。

## 后果（Consequences）

- 代价：FP64 意味着 8 字节/值。本项目一轮 Run 至多几百个浮点，内存代价可忽略。
- 收益：`json.dumps` 直接可写，`json.loads` 直接还原为 `float`；跨模型的对比表不需要任何
  精度转换层，也不存在「解析出来是 float32，写回历史变成另一种数」的隐患。
- 约束的可检查性：全仓库 `grep -n "float32\|struct.pack('f')\|array('f')\|time.time()"` 必须为空；
  这是评审时唯一的机械检查点（正式检查器见 ADR-0001）。

## 相关代码位置（Code Locations）

- `arena/parsers.py::BenchJsonParser._to_result`（`float(item["avg_ts"])`）
- `arena/parsers.py::PerplexityLogParser.parse_final`、`CliTranscriptParser.parse_throughput`
- `arena/domain.py::BenchResult`、`PerplexityResult`、`AnswerSheet`、`TrialRecord`（float 字段）
- `arena/process.py::SubprocessRunner.run`（`time.perf_counter()` 计时）
- `arena/runners.py::TrialRunner.run`（墙钟）
- `arena/history.py::ScoreboardAssembler.build`（`float(record.wall_seconds)` 汇总）
