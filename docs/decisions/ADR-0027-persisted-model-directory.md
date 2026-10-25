# ADR-0027：模型目录作为用户偏好持久化（XDG 配置，环境变量仍优先）

- **状态**：Accepted
- **日期**：2026-10-24
- **相关 ADR**：ADR-0001（可读性与可溯源性）、ADR-0007（外置 CSS 主题）、
  ADR-0008（worker 线程与 UI 的桥）、ADR-0013（配置是环境事实的唯一来源）、
  ADR-0014（headless 与 quick 两种运行方式）、ADR-0018（负载档与不提供参数面板）、
  ADR-0019（允许超显存模型）

## 背景（Context）

模型目录此前只有两个入口：环境变量 `ARENA_MODELS_DIR`，或命令行 `--models-dir`。
两者都只对**当前进程**有效：

- 桌面用户每次启动 GUI 前都要先 `export ARENA_MODELS_DIR=...`，否则只能扫默认的 `~/models`；
- GUI 里没有"换文件夹"的入口，把模型放在别处就只能改环境或改代码；
- `--models-dir` 的语义是"本次运行覆盖"，把它当成持久设置会让一次临时排查悄悄改变后续所有运行。

同时，仓库里**不能**写入本机绝对路径：那会让同一份代码在不同机器上产生不同默认行为，
也会把个人目录结构带进版本历史。因此需要一份"属于某个用户、但不属于某个克隆"的配置文件。

## 决策（Decision）

1. **配置文件放在 XDG 配置目录**：默认 `$XDG_CONFIG_HOME/local-arena/config.json`；
   未设置 `XDG_CONFIG_HOME` 时用 `~/.config/local-arena/config.json`。
   `ARENA_SETTINGS_PATH` 可整体覆盖该路径（便于测试与便携场景）。
2. **配置结构带版本**：当前 `version = 1`，唯一字段是 `models_dir`（绝对路径字符串或 `null`）。
   读到未知版本、非法 JSON、非对象顶层或字段类型错误时，`SettingsStore.load()` 抛
   `SettingsError`，**不猜测、不静默迁移**。
   > 2026-10-25 修订：结构升级到 `version = 2`，新增 `llama_bin_dir`；v1 仍可读并在内存里迁移，
   > 见 ADR-0028。本条"不猜字段"的原则不变——v1 里没有的键只能是 `None`。
3. **优先级固定为**：`ARENA_MODELS_DIR` > 配置文件里的 `models_dir` > `~/models`。
   环境变量仍是最高优先级，因此 CI、临时换盘、一次性排查都不受用户偏好影响。
4. **`--models-dir` 只覆盖本次进程**（`--list-models` 同样适用），**不写回配置文件**。
   "临时覆盖"与"持久偏好"是两种意图，不能共用一个落盘副作用。
5. **GUI 提供 `MODEL FOLDER` 入口**：`Gtk.FileChooserDialog(SELECT_FOLDER)` 选择目录后，
   经 `SettingsStore.save()` 持久化。保存成功后同时：
   - 用 `dataclasses.replace` 更新内存中的 `AppConfig.models_dir`；
   - 清空已选模型（旧选择可能不在新目录里）；
   - 重新扫描目录并把数量或"没有 .gguf"写进状态行。
   取消、目录不可用或保存失败时，内存与磁盘配置都保持原样，原因写进状态行。
6. **保存必须原子**：先写同目录临时文件、`flush + fsync`，再 `os.replace` 覆盖目标；
   失败时清理临时文件。禁止直接覆盖目标文件——中途崩溃会留下半截 JSON，下次启动无法读取。
7. **损坏配置不阻止启动**：`AppConfig.from_env()` 捕获 `SettingsError`，回退到默认值并写入
   `settings_warning`；GUI 把它显示在状态行，headless 打到 stderr。配置问题必须被看见，
   但不能让程序连窗口都开不了。
8. **模块边界**：`arena.settings` 只认识"路径"这一种用户偏好，不校验目录是否存在
   （校验归使用点）；`AppConfig` 仍是配置的唯一来源，读取后以 frozen dataclass + `replace`
   派生，不在运行期被某个模块改写。

## 理由（Rationale）

- **GUI 用户不该被环境变量绑架**：桌面程序把"上次选的文件夹"记住是基本预期；要求用户先改
  shell 环境才能选模型，等于把部署细节泄漏给使用者。
- **环境变量优先保留了可复现性**：脚本、CI、临时换盘都能用 `ARENA_MODELS_DIR` 一键覆盖，
  不会因为某台机器上点过一次 GUI 就改变测量环境。
- **配置不进仓库**：用户偏好写入 XDG 目录，克隆里没有任何本机绝对路径；删除配置即回到默认值，
  不需要"清理仓库"。
- **显式版本 + 显式错误**：配置格式一旦变化，拒绝旧结构比"尽力猜字段"更安全；启动告警而不是
  静默回退，避免用户以为目录已生效。
- **原子写入是崩溃后的最低保障**：这个文件正是"上次崩溃后还要靠它启动"的东西，半截 JSON
  会把下一次启动也拖进回退路径。
- **换目录清空选择是有意为之**：保留旧模型会让界面显示一个不在当前目录里的文件，下一次
  START 才在更深的地方失败；在切换点就把"未选"摆出来更诚实。

## 后果（Consequences）

- **首次启动行为不变**：没有配置文件时仍扫 `~/models`；已有 `ARENA_MODELS_DIR` 的脚本行为
  完全不变（环境变量优先）。
- **GUI 会写用户配置**：这是本仓库第一个写入仓库外路径的功能；headless 模式只读不写。
- **换目录后需要重新选模型**：这是刻意的，不是回归。
- **配置版本升级需要迁移**：v1 仍可读（ADR-0028 提供内存迁移，下次保存写回 v2）；
  更早或未知版本整体拒绝并告警，未来若再改结构，必须同时提供迁移或明确的破坏性说明。
- **配置文件不在 git 里**：不会被提交、不会被 `git clean` 清掉，也不会随仓库复制到别的机器；
  跨机器迁移需要用户自己复制或重新选择。
- **路径存在性仍由使用点判断**：保存时只要求目录当前存在；之后目录被删除/重命名时，
  表现为"没有 .gguf"或运行期启动失败，配置本身不会被自动改写。

## 相关代码位置（Code Locations）

- `arena/settings.py::SETTINGS_DIR_NAME`、`SETTINGS_FILENAME`、`SETTINGS_VERSION`、
  `SettingsError`、`UserSettings`、`SettingsStore.load`/`save`
- `arena/config.py::AppConfig.settings_path`、`AppConfig.settings_warning`、
  `AppConfig.from_env`（优先级与损坏回退）
- `run_arena.py::build_parser`（`--models-dir` 仅本次运行）、`run_arena.py::main`（告警到 stderr）
- `arena/ui/controller.py::ArenaController.bind`、`choose_models_dir`、`_report_models_dir`
- `arena/ui/window.py::ArenaWindow._build_footer`、`ArenaWindow.bind`
- `arena/ui/views.py::ModelChooserDialog.pick`（空目录提示，不提供"选定"按钮）
