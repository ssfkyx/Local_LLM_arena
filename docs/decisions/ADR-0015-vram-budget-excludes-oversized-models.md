# ADR-0015：用显存预算硬排除超规格模型

- **状态**：Superseded by [ADR-0019](ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md)（2026-10-17）
- **日期**：2026-10-16
- **相关 ADR**：ADR-0012（被取代）、ADR-0013（配置单一来源）、ADR-0010(对比表不含不可测模型)、
  ADR-0019（取代本决策：改用工具的 `-fitt` 显存适配）

> **2026-10-17 被取代说明**：本 ADR 的「文件大小 > 显存预算即不可测」只在默认
> `-ngl -1`（全部上卡）下成立。实测 `llama-bench … -fitt 1024` 对同一 30B 模型
> 成功跑出 pp 124.8 / tg 33.4 t/s（见 ADR-0019），因此静态排除被撤销。
> 本文件保留作为决策历史；`max_model_bytes` / `exclusion_reason()` 已删除。

## 背景（Context）

本机：RTX 4080 Laptop，**12282 MiB** 显存。模型目录里：

| 模型 | 文件大小 | 结果 |
|---|---|---|
| `Meta-Llama-3.1-8B-Instruct-Q8_0.gguf` | 8.0 GiB | 实跑通过 |
| `Qwen3-MOE-2x4B-8B-Jan-Nano-Instruct-II.Q8_0.gguf` | 6.6 GiB | 实跑通过 |
| `Qwen3-30B-A3B-Q4_K_M.gguf` | **17697 MiB** | **整体加载会崩溃** |

用户明确决策：**不要把 30B 模型纳入测试，会引发崩溃**。

而 `WSL2` 下显存崩溃的代价特别高：不是"报错退出"，而是把系统拖进无响应状态
（需要重启 WSL 发行版）。因此"跑跑看再说"是最差的选择。

同时必须避免另一种错误：**静默隐藏**。模型文件还在目录里，若列表里看不到它，
用户会以为文件丢了或软件坏了。

## 决策（Decision）

1. **配置层持有预算**：`AppConfig.max_model_bytes`（默认 `12 * 1024**3`，
   即 12288 MiB，略高于实测可用的 12282 MiB 以容纳模板与上下文开销；
   可用 `ARENA_MAX_MODEL_BYTES` 覆盖——换机器只改环境，不改代码）。
2. **领域层做判定，不做隐藏**：`ModelCatalog(models_dir, max_bytes)` 的
   `list_models()` **照旧返回全部模型**；新增 `list_testable()`（子集）与
   `exclusion_reason(model)`（人类可读理由，含实际大小与预算）。
3. **排除必须看得见**（2026-10-17 修订）：`--list-models` 列出**全部**模型并逐行标状态
   （`testable` / `EXCLUDED (<理由>)`）；显式点名超预算模型时报错信息里带同一句理由。
   修订原因：`--headless` 改为一次只跑一个模型（ADR-0014 决策一修订）后，
   「未点名时默认跑全部可测模型」这条路径已不存在——"未点名"现在是错误
   （`run_arena.selected_model()` 抛 `ValueError`，退出码 2），因此不再有
   `skip: …` 这类默认路径的打印；可见性改由 `--list-models` 承担。
4. **显式点名超预算模型 = 提前报错，而不是崩溃**：`--model Qwen3-30B-A3B`
   以退出码 2 结束并说明原因（禁止"用户点名了就照跑"）。
5. **GUI 只提供可测模型**：`ArenaController._catalog_models()` 用 `list_testable()`，
   界面上永远不会出现一个点了就崩的选项。
6. **`--list-models` 显示全部 + 状态列**：`testable` / `EXCLUDED (<理由>)`，
   保证信息不丢。

## 理由（Rationale）

- 文件大小是**可静态判断**的与显存相关的唯一可靠事实；加载中崩溃无法预先测出，
  但"文件比显存大"已经足够下结论（llama.cpp 默认 `-ngl -1` 会尝试全部上卡）。
- 把预算放在 `AppConfig` 而不是 `ModelCatalog` 的硬编码里：它是**环境事实**，
  与 llama 二进制路径、模型目录同级（ADR-0013）。
- 判定与展示分离（`exclusion_reason` 返回字符串而不是布尔）：UI、CLI、错误信息
  可以复用同一句理由，避免三处各写一套解释。
- 拒绝在 Runner 里写 `-ncmoe`/`-ngl` 自动策略：那属于运行期测量问题（ADR-0012 的结论仍有效），
  在没有测量数据前加进去只会引入新的猜测。

## 后果（Consequences）

- 代价：预算值与本机显存绑定，换到更大显存的机器需要改环境变量或改默认值。
  缓解：理由字符串里带着预算数字，误配置时一眼能看出来。
- 代价：30B 模型在历史与对比表里永远缺席。这是用户明确要求的取舍。
- 收益：`--headless` 不存在"不小心跑到崩"的路径：它必须点名一个模型，而未点名与点名超预算
  都在启动阶段以退出码 2 结束（2026-10-17 修订；此前是"默认只跑可测模型"）。
- 收益：崩溃风险从"运行期不可控"变成"配置期可读的一条理由"。

## 相关代码位置（Code Locations）

- `arena/config.py::AppConfig.max_model_bytes`、`AppConfig.from_env`（`ARENA_MAX_MODEL_BYTES`）
- `arena/domain.py::ModelCatalog.__init__`、`list_testable`、`exclusion_reason`、`list_models`
- `run_arena.py::selected_model`（未点名或点名超预算都报错）、`main`（`--list-models` 状态列）
- `arena/ui/controller.py::ArenaController._catalog_models`
- `arena/domain.py::Model.size_bytes`（判定的数据来源）
