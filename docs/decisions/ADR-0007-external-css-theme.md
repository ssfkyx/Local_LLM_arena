# ADR-0007：主题是外置 CSS，类名即契约

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0001（文档契约总纲）、ADR-0016（记分板显示策略）

## 背景（Context）

需求要求：
- 「打开窗口是一个火热的 arena 界面」；
- 「屏幕中央有一个方形的 Choose Your Model! 按键：和圆形的 START 按键」；
- 「要求外观，配置，主题与核心逻辑分开」。

候选做法：
1. 用 GTK API 在 Python 里逐控件设置颜色/圆角（`override_color`、`CssProvider.load_from_data` 内嵌字符串）；
2. 把样式写在 `.py` 里的常量字符串；
3. **外置 CSS 文件 + `Gtk.CssProvider`**。

实测（tracer F9）：WSLg 下 GTK3 的 `add_provider_for_screen` 能正常渲染径向渐变、
圆角（`border-radius`）与内外阴影（`box-shadow`），且「没挂 CSS 时截图 14 KB 无主题 →
挂上后 167 KB 出渐变」可分辨。

## 决策（Decision）

1. 外观的**唯一来源**是 `arena/theme/arena.css`；Python 侧只有 `ArenaTheme.apply()`
   负责挂载（`load_from_path` + `add_provider_for_screen`）。
2. **类名即契约**：CSS 文件头列出全部被使用的类名
   （`.arena-root` / `.title-band` / `.arena-title` / `.choose-model` / `.start-button` /
   `.scoreboard` / `.metric-*` / `.ghost-button` / `.arena-dialog` / `.arena-dialog-title` …），
   改类名必须同步改两侧。
3. **CSS 解析错误 = 启动期异常**：`ArenaTheme` 监听 `parsing-error` 信号，
   只要 GTK 报过任何告警，`apply()` 就抛 `ThemeError`。**禁止静默降级**——
   静默降级会让「外观坏了」变成难查的谜题。
4. **对话框必须自带样式锚点**：`Gtk.Dialog` 是独立顶层窗口，不会继承主窗口 `.arena-root` 的样式，
   因此 `_TableDialog` 统一挂 `.arena-dialog` 类，CSS 里为它补齐背景、表格、表头与按钮配色。
   （2026-10-16 修复：此前对话框是 GTK 默认浅色外观，与 arena 主题割裂。）
5. 形状语义不做妥协：正方形按钮 = 选择一个 Model（240×240，`border-radius: 10px`）；
   圆形按钮 = START / CANCEL（168×168，`border-radius: 168px`，运行中渐变转冷色）。

## 理由（Rationale）

- 外置 CSS 让「改配色」这一最高频的审美调整**不必触碰任何 Python 文件**，也不会误伤逻辑。
- 用 GTK 原生 `CssProvider` 而不是自绘：满足「优先使用框架已有能力」，且主题可被外部替换。
- 把解析错误升级为异常，是因为 GTK 对不支持的属性只发警告而丢弃整条规则，
  其后果是「界面看起来只是有点怪」，最难定位。

## 后果（Consequences）

- 代价：类名在两侧重复出现（Python `add_class` 与 CSS 选择器），改名要两处同改；
  缓解是把契约写在 CSS 文件头，并由 ADR-0001 的检查器覆盖 `.arena-dialog` 等锚点存在性。
- 代价：只使用 GTK 3.24 确认支持的属性子集（如不用 `min-width: 0`、不用 CSS 变量）。
- 收益：`docs/ui/arena-window.png` 与 `arena-choose-model.png` 就是挂载后的真实渲染结果，
  可作为外观回归的基线。

## 相关代码位置（Code Locations）

- `arena/theme/arena.css`（外观唯一来源；文件头写类名契约）
- `arena/ui/theme.py::ArenaTheme.apply`、`_on_parsing_error`、`ThemeError`
- `arena/ui/window.py::ArenaWindow._build_title_band`、`_build_stage`、`_build_status_band`、
  `_build_footer`、`set_running`（`.running` 类切换）
- `arena/ui/views.py::_TableDialog.__init__`（`.arena-dialog` 锚点）、`_title_label`
- `run_arena.py::run_gui`（启动时挂载主题）
