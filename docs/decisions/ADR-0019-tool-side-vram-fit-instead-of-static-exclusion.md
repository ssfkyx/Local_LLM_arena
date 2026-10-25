# ADR-0019：用工具自带的显存适配取代静态排除（允许超显存模型）

- **状态**：Accepted（取代 [ADR-0015](ADR-0015-vram-budget-excludes-oversized-models.md)）
- **日期**：2026-10-17
- **相关 ADR**：ADR-0015（被取代）、ADR-0013（配置单一来源）、ADR-0017（运行期 OOM 只记录）、
  ADR-0018（负载档与可核验的实验条件）

## 背景（Context）

ADR-0015 用「文件大小 > 显存预算」把 `Qwen3-30B-A3B-Q4_K_M.gguf`（17697 MiB > 12288 MiB）静态排除，
理由是「整体加载会崩溃」。这条判断只对**默认卸载策略**成立：本机 `llama-bench` 的 `-ngl` 默认值是 `-1`
（全部层上卡），而 Runner 没有传任何卸载参数，所以 18.5 GB 的模型确实会 `cudaMalloc` 失败。

2026-10-17 实测复查（`llama-bench -m Qwen3-30B-A3B-Q4_K_M.gguf -p 64 -n 16 -r 1 -fitt 1024 -o json`）：

| 条件 | pp t/s | tg t/s | 墙钟 | 峰值 RSS | 退出码 |
|---|---|---|---|---|---|
| `-fitt 1024`（自动部分卸载） | **124.8** | **33.4** | 25.2 s | 18.7 GB | 0 |
| `-ngl 0`（纯 CPU，对照） | 48.7 | 11.4 | — | — | 0 |

也就是说：**文件大于显存只意味着需要部分卸载，不意味着不能运行**。三种工具
（`llama-bench` / `llama-perplexity` / `llama-cli`）都支持 `--fit-target/-fitt`：
它按「上卡后至少留出 N MiB」自动决定哪些层放 GPU、哪些留 CPU，不需要我们手工猜 `-ngl`。

静态排除与软件目标（「在当前硬件下尽量让模型跑满，以此做性能与效率比较」）直接冲突：
它把最有比较价值的 MoE 大模型从可测集合里删掉了。

## 决策（Decision）

1. **删除静态排除**：`ModelCatalog` 不再持有显存预算，`exclusion_reason()` / `list_testable()` 一并删除；
   `--model` 与 GUI 选择器都列出并接受目录里的全部模型。
2. **三种 Runner 统一追加 `-fitt <余量 MiB>`**，值来自 `AppConfig.vram_margin_bytes`
   （默认 `1 GiB`，环境变量 `ARENA_VRAM_MARGIN_BYTES`）。语义是「至少留出这么多显存」，
   实际的上卡层数由 llama.cpp 决定。
3. **每条 `TrialRecord` 落盘 `vram_margin_bytes`**，让「这组数字用的是哪种卸载策略」可核验
   （与 ADR-0018 把负载参数写进记录同一原则）。
4. **列表照实显示**：`--list-models` 输出全部模型并带文件大小；GUI 选择器同样列出全部。
5. **运行期失败仍按 ADR-0017 处理**：真的加载失败（例如主机侧显存被占、或系统内存不足）
   一律落成 `ok=false` 记录并如实报告，不做自动重试，也不偷偷降参数。

## 理由（Rationale）

- **`-fitt` 是工具自报的能力，不是我们猜的策略。** 把「哪些层上卡」交给 llama.cpp，
  比「文件大小 vs 显存大小」这个二元判断准确得多，也不需要把硬件总量写进配置。
- **统一策略保证可比性。** 所有模型都在「留出同样余量」的条件下测量：小模型自然全量上卡，
  大模型自动部分卸载。读者从数字本身就能看出差别（30B 的 pp 124.8 明显低于 8B 的数千 t/s）。
- **符合软件目标。** 部分卸载下的 30B（tg 33.4 t/s）是有意义的测量结果——它恰好回答
  「MoE 小激活参数模型能不能在消费级卡上跑出可用的生成速度」。
- **配置只留一个与工具参数一一对应的量。** 删掉 `max_model_bytes` 之后，配置里不再有
  「本机显存是多少」这种硬件事实，只有一个直接映射到 `-fitt` 的余量。

## 后果（Consequences）

- **收益**：30B 变得可测；对比表能回答「同一负载下 MoE 大模型 vs 8B 谁更快」，
  已实测 30B 的 tg（33.4 t/s）与 8B 同量级，而 pp（124.8 t/s）低一个数量级。
- **代价**：部分卸载会显著拉低大模型的 pp；跨模型对比时要说明「这一行是部分卸载」。
  本阶段通过记录 `vram_margin_bytes` 让策略可核验，但不解析工具实际卸载了多少层（见已知限制）。
- **代价**：若模型连系统内存也放不下，llama.cpp 会在加载期失败；此时按 ADR-0017
  落成 `ok=false`，不再有「提前拦截」的保护。这是把可控的静态错误换成真实的测量结果。
- **破坏性变更**：环境变量 `ARENA_MAX_MODEL_BYTES` 被 `ARENA_VRAM_MARGIN_BYTES` 取代，旧变量不再生效。
- **已知限制**：`-fitt` 的实际上卡层数由工具日志给出，本阶段只记录输入（余量），不记录结果（层数）；
  将来若需要，按 ADR-0004 在解析层补一条 stderr 解析即可。

## 相关代码位置（Code Locations）

- `arena/config.py::AppConfig.vram_margin_bytes`、`AppConfig.__post_init__`、`AppConfig.from_env`
- `arena/domain.py::ModelCatalog.__init__`、`ModelCatalog.list_models`、`ModelCatalog.find`、
  `TrialRecord.vram_margin_bytes`、`TrialRecord.to_dict`、`TrialRecord.from_dict`
- `arena/runners.py::TrialRunner._base_record_kwargs`、`BenchRunner._build_command`、
  `PerplexityRunner._build_command`、`ExamRunner._build_command`
- `arena/ui/controller.py::ArenaController._catalog_models`
- `run_arena.py::selected_model`、`main`（`--list-models`）
