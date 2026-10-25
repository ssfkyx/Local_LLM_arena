# ADR-0028：llama.cpp 可执行文件目录作为用户偏好持久化（配置 v2 + 选择期校验）

- **状态**：Accepted
- **日期**：2026-10-25
- **相关 ADR**：ADR-0001（可读性与可溯源性）、ADR-0013（配置是环境事实的唯一来源）、
  ADR-0027（模型目录作为用户偏好持久化）、ADR-0019（上卡策略交给 llama.cpp）

## 背景（Context）

ADR-0027 只把**模型目录**做成了可持久化的用户偏好，llama.cpp 可执行文件目录仍然只有两个入口：
环境变量 `ARENA_LLAMA_BIN_DIR`，或写死的默认值 `~/llama.cpp/build/bin`。

这在"同一台机器"上没问题，但换电脑时暴露两个具体缺口：

- **入口不对称**：模型目录能在 GUI 里点 `MODEL FOLDER` 换并记住，llama.cpp 目录却要求用户先
  `export` 一个环境变量。桌面用户必须记住一个部署细节，而且每次开新终端都要重来。
- **失败点太晚**：`AppConfig.binary()` 直到开跑才检查可执行文件（`FileNotFoundError`），
  用户可能已经等了模型加载才看到"找不到 llama 工具"。目录选错了，本该在选择的那一刻就说清楚。

同时必须承认一条硬边界：**本软件不携带推理引擎**。`llama-bench` / `llama-perplexity` /
`llama-server` 都是外部可执行文件，且与操作系统、GPU 后端绑定。目标机必须自己装好一份能跑的
llama.cpp；本决策只解决"怎么告诉软件它在哪"，不解决"怎么把它装上去"。

## 决策（Decision）

1. **配置结构升到 `version = 2`**：新增字段 `llama_bin_dir`（绝对路径字符串或 `null`），
   与 `models_dir` 并列。写回时恒写当前版本。
2. **v1 可读并在内存里迁移**：`READABLE_VERSIONS = (1, 2)`。读到 v1 时 `llama_bin_dir`
   就是 `None`（旧结构里没有这个键，不是"猜"出来的值）；下次保存自动写成 v2。
   **读不出来的版本仍然整体拒绝**（`SettingsError`），不静默降级。
3. **优先级与模型目录保持同一套**：
   `ARENA_LLAMA_BIN_DIR` > 配置文件 `llama_bin_dir` > `~/llama.cpp/build/bin`。
   环境变量仍最高，因此 CI、临时排查、一次性换盘都不受用户偏好影响（ADR-0027 决策 3 的延续）。
4. **GUI 新增 `LLAMA BIN` 入口**：`Gtk.FileChooserDialog(SELECT_FOLDER)`，与 `MODEL FOLDER`
   相邻放在页脚右侧。运行中禁用，与选择模型/模型目录一致。
5. **选择期校验，而不是运行期才发现**：确认目录前用 `AppConfig.missing_tools()`
   检查 `LLAMA_TOOLS`（`llama-bench`、`llama-perplexity`、`llama-server`）是否存在且可执行。
   缺任何一项就**不保存**，状态行列出具体缺的工具名；全齐才写配置。
6. **只改一项，不清空另一项**：两个入口保存前都先 `SettingsStore.load()` 取磁盘现值，
   再用 `dataclasses.replace` 只替换自己那一项。禁止用 `UserSettings(models_dir=...)`
   这样的"半份偏好"覆盖文件——那会把另一项悄悄清成 `None`。
   也禁止用内存里的 `AppConfig` 拼偏好：它可能被 `ARENA_*` 覆盖过，把环境值写进磁盘会让
   "临时覆盖"变成持久设置（ADR-0027 决策 4）。
7. **选择器不下载、不安装、不编译**：它只记录用户已经装好的那份构建在哪里。
8. **配置损坏仍不阻止启动**：沿用 ADR-0027 决策 7，`AppConfig.from_env()` 回退默认值并写
   `settings_warning`；GUI 状态行显示，headless 打到 stderr。
9. **运行期校验不变**：`binary()` 仍然拒绝 PATH 兜底，仍然在缺文件时抛 `FileNotFoundError`；
   选择期校验是**提前告知**，不是把运行期检查删掉。

## 理由（Rationale）

- **入口对称降低使用门槛**："模型在哪"和"工具在哪"是同一类环境事实，用户不应该为前者点按钮、
  为后者记环境变量。
- **校验放在选择点是唯一能给出可行动反馈的位置**：那里知道用户刚选了哪个目录，
  也知道缺哪个工具；放到运行期只能给一行异常，且已经浪费了加载时间。
- **迁移而不是拒绝，是版本化的意义所在**：v1 文件里缺的字段没有歧义（只能是"未设置"），
  拒绝它只会让老用户升级后平白丢掉已选的模型目录。
- **不把下载/安装塞进选择器**：那会把"路径偏好"变成"发行版打包 + GPU 后端匹配"问题，
  与 ADR-0019"上卡策略交给 llama.cpp"的分工相冲突，也会让仓库体积与平台矩阵失控。

## 后果（Consequences）

- **首次启动行为不变**：没有配置文件时仍扫 `~/models`、仍用 `~/llama.cpp/build/bin`；
  已有 `ARENA_LLAMA_BIN_DIR` 的脚本行为完全不变（环境变量优先）。
- **老配置自动升级**：v1 的 `models_dir` 被保留；第一次保存后文件变成 v2（`llama_bin_dir` 为 `null`
  或被选中值）。不需要手工编辑配置，也不需要删除重来。
- **每台机器仍需自备 llama.cpp**：本功能不能替代安装。二进制与平台/后端绑定，
  从一台机器拷贝到另一台通常不可用；同一目录里的 `libggml*` 等运行库也必须齐。
- **目录改名/删除后仍会失败**：配置不会被自动改写，表现为选择后的下一次运行报"找不到可执行的
  llama 工具"。这与模型目录的处理一致（ADR-0027 后果）。
- **GUI 多了一个页脚按钮**：页脚从三个入口变四个；样式仍复用 `ghost-button`，不需要新 CSS 类。
- **跨机器数字仍不可直接对比**：不同构建/后端测出的吞吐与 PPL 不是同一实验条件；
  本决策只让"装在哪"可配置，不改变"同负载才可比"的前提（ADR-0018）。

## 相关代码位置（Code Locations）

- `arena/settings.py::SETTINGS_VERSION`、`READABLE_VERSIONS`、`UserSettings.llama_bin_dir`、
  `SettingsStore.load`（v1 迁移）、`SettingsStore.save`（恒写 v2）、`_optional_path`
- `arena/config.py::LLAMA_TOOLS`、`AppConfig.from_env`（三级优先级）、`AppConfig.missing_tools`、
  `AppConfig.binary`（运行期校验不变）
- `arena/ui/controller.py::ArenaController.bind`、`choose_llama_bin_dir`、`choose_models_dir`、
  `_current_settings`
- `arena/ui/window.py::ArenaWindow._build_footer`、`ArenaWindow.bind`、`ArenaWindow.set_running`
- `README.md`（环境要求、GUI 入口、配置表、配置版本说明）
