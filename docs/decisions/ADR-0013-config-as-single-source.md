# ADR-0013：配置是环境事实的唯一来源

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0002（工具路径）、ADR-0014（headless/quick）、ADR-0015（显存预算）

## 背景（Context）

程序需要知道：llama.cpp 的可执行文件在哪、模型目录在哪、语料与试卷在哪、产物写到哪里、
主题文件在哪、三类测试用什么参数、显存预算是多少。

这些是**环境事实**，会随机器、随用户、随实验意图变化。若散落在各模块里
（如 Runner 里写 `"/home/cielo_1/llama.cpp/build/bin/llama-bench"`，或在 Session 里读
`os.environ`），会出现三个问题：换机器要改多处；同一事实出现多个版本；单测无法替换。

## 决策（Decision）

1. **唯一入口**是 `arena.config.AppConfig`（frozen dataclass），字段全是路径或数值，
   **不认识任何领域对象**。
2. **唯一允许读环境变量的地方**是 `AppConfig.from_env()`；其它任何模块禁止 `os.environ`。
3. **唯一允许出现绝对路径默认值的地方**是 `from_env()`（其它模块不得写字面量路径）。
4. **两种装配档**：`from_env()`（真实档）与 `quick()`（自检档，只改负载参数）。
5. **命令行覆盖用 `dataclasses.replace`**：`--models-dir` / `--runs-dir` 只替换对应字段，
   不重新走一遍环境变量解析（保证"只改一个事实"）。
6. **不做启动期路径校验**：`binary()` 与 `corpus_path()` 在使用点按需校验并抛出可读异常
   （`FileNotFoundError`）。理由是"启动即崩"比"跑到一半才发现"更糟——
   GUI 应该在缺少模型时说清楚，而不是拒绝启动。
7. **子配置也是值对象**：`BenchDefaults` / `PerplexityDefaults` / `ExamDefaults` /
   `TrialTimeouts` 各自 frozen，避免"改一个默认值顺手改了另一个"。

## 理由（Rationale）

- 「一个事实一个来源」是把环境依赖关进一处的唯一办法；它让 ADR-0014 的 headless/quick 档、
  ADR-0015 的显存预算都只需改一个字段。
- frozen dataclass + `replace` 让配置在运行期不可变：没有"某处偷偷改了 models_dir"的可能。
- 不在启动期校验，是因为 `--compare` / `--list-models` 这类只读路径**不需要** llama 二进制，
  强制校验会把它们一起弄坏。

## 后果（Consequences）

- 代价：所有需要路径/参数的模块都必须接受 `AppConfig` 注入（`TrialRunner.__init__`、
  `ArenaSession.__init__`、`ArenaController.__init__`）——构造函数变长了一点。
- 收益：单测与 headless 自检可以构造任意 `AppConfig`（如指向 fixture 目录），
  不需要 monkeypatch 全局状态。
- 收益：换机器/换 llama 构建只需改环境变量（`ARENA_LLAMA_BIN_DIR` 等），代码零改动。

## 相关代码位置（Code Locations）

- `arena/config.py::AppConfig`、`from_env`、`quick`、`binary`、`corpus_path`、`paper_paths`、
  `ensure_runtime_dirs`、`papers_dir`、`speed_dir`
- `arena/config.py::BenchDefaults`、`PerplexityDefaults`、`ExamDefaults`、`TrialTimeouts`
- `run_arena.py::main`（`replace` 覆盖目录）、`build_session`、`run_gui`
- `arena/runners.py::TrialRunner.__init__`（注入 config）
- `arena/session.py::ArenaSession.default_plan`（把配置翻译成 TestPlan）
