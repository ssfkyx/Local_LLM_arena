# ADR-0012：不预先为显存做分层策略

- **状态**：**Superseded by [ADR-0015](ADR-0015-vram-budget-excludes-oversized-models.md)**
  （2026-10-16 修订：本机实测 30B-Q4_K_M 会崩溃，因此"不做预约束"的做法被"显存预算硬排除"取代；
  本文件保留为历史记录，其关于"不在 Runner 里写 MoE 参数"的结论仍然有效）
- **日期**：2026-10-16
- **相关 ADR**：ADR-0003（Runner 与命令构造）、ADR-0013（配置单一来源）

## 背景（Context）

本机显存为 RTX 4080 Laptop **12282 MiB**，而候选模型里 `Qwen3-30B-A3B-Q4_K_M` 文件有
**17697 MiB**（> 显存），`Meta-Llama-3.1-8B-Q8_0` 约 8 GiB。

llama.cpp 提供 `-ngl`（GPU 层数）、`-ncmoe`（MoE 专家留在 CPU）等参数来控制分层。
问题：是否要在代码里预设一套"按模型大小自动分层"的策略？

## 决策（Decision）

（当时的决策）**不在代码里预设分层策略**：

- `BenchRunner` / `PerplexityRunner` / `ExamRunner` 的命令行里**不写** `-ngl` / `-ncmoe`
  等显存相关参数，完全交给 llama.cpp 的默认值（本机默认 `-ngl -1`，即全部上卡）。
- `TestPlan` 也不携带这类参数。

## 理由（Rationale）

- 分层参数的最优值取决于模型结构、量化方式、上下文长度与机器的实际占用，属于**运行期测量问题**，
  不是可以写死在代码里的常量。写一套"自动策略"就是写一套猜测。
- 手工调参的正确位置是命令行/环境变量，而不是硬编码在 Runner 里——否则每次调参都要改代码、
  跑检查器、改 ADR。
- 当时的证据只覆盖了"能跑通"的两个模型（8B-Q8_0、2x4B-MoE-Q8_0），没有崩溃复现。

## 后果（Consequences）

- 代价（**后来被证实**）：`Qwen3-30B-A3B-Q4_K_M`（17697 MiB）一旦被选中会直接崩溃，
  而不是"慢"或"报错退出"。这使"不预约束"变成一个真实的可用性风险。
- 因此 ADR-0015 取代本决策：**在配置层引入显存预算，超预算模型的默认路径根本不给跑，
  显式点名则提前报错**，而不是等到 `llama-bench` 把显存吃干。
- 仍然有效的部分：Runner 的命令构造里依旧不写 `-ngl` / `-ncmoe`；
  分层策略若将来要做，应以"测量 + 显式参数注入"的形式进入 `TestPlan`，而不是自动猜测。

## 相关代码位置（Code Locations）

- `arena/runners.py::BenchRunner._build_command`（写此 ADR 时无 `-ngl` / `-ncmoe`；2026-10-17 起统一追加 `-fitt`，见 ADR-0019）
- `arena/runners.py::PerplexityRunner._build_command`、`ExamRunner._build_command`
- `arena/domain.py::TestPlan`（无显存相关字段）
- 取代本决策的实现：`arena/config.py::AppConfig.vram_margin_bytes`、
  `arena/runners.py::TrialRunner._fit_args`（见 ADR-0019；ADR-0015 的静态排除同样已被取代）
