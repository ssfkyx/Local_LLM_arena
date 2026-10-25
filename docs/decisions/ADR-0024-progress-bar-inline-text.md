# ADR-0024：进度条的条内文字与"可量化/不可量化"两档

- **状态**：Accepted
- **日期**：2026-10-18
- **相关 ADR**：ADR-0007（外观的唯一来源是 CSS）、ADR-0008（worker 线程与 UI 的桥）、
  ADR-0016（值列只放事实）、ADR-0018（负载档不可调）、ADR-0020（完成条件而非 token 上限）

## 背景（Context）

需求：「想把进度条做得更精致一点，能否做成进度条？」——这句里有隐含假设，先把它验掉再说。

用 `Gtk.OffscreenWindow` 离屏渲染真实控件树并量到的事实（2026-10-18，窗口宽 1020 px）：

| 观感问题 | 实测 | 结论 |
|---|---|---|
| "进度条是不是只有一小截？" | 进度条分配尺寸 **988 × 12 px** | **满宽**。"改成满宽"是个不存在的问题——`pack_start(expand=False, fill=False)` 在**垂直** Box 里只管纵向分配，横向由子控件自身的 `halign` 决定，而 `Gtk.ProgressBar` 默认就是 `FILL` |
| 为什么看着不像进度条 | 高度只有 12 px；`set_show_text(False)` 所以条内**零文字** | 缺的是"厚度 + 文字"，不是宽度 |
| 能不能显示百分比 | 三类 Trial 里只有 PERPLEXITY 有真实比例（`arena/parsers.py::parse_progress` 的 chunk k/N） | 见下 |

两类"没有比例"的来源不同，必须分开对待：

- **BENCH**：`BenchRunner._execute` 是**一个** `llama-bench` 子进程（`-r <reps>` 由工具自己展开，
  `-o json` 最后一次性输出）。中途拿不到 rep 粒度。要拿到就得把 `-r 3` 拆成三次调用——
  那会改变测量方式，与历史里的数字**不再可比**（ADR-0010 的可比性前提是"同负载"）。
- **EXAM**：`ExamRunner._report_alive` 手里**有**"卷 k/N"这个真实事实，但当前刻意传 `fraction=None`。

## 决策（Decision）

1. 打开条内文字（`set_show_text(True)`），文字内容与 `fraction` **同源**：
   可量化 → 百分比；不可量化（脉冲档）→ 传入的 `message`（事实文案，如 `卷 1/2 · 已用 137s · 最后输出 3s 前`）。
2. **不给脉冲档编造百分比**，也不把 `fraction=None` 当成 0——两条都延续 ADR-0020 与 ADR-0016 的口径：
   显示层只报事实，比例必须来自真实计数。
3. **边界澄清（对 ADR-0007 的补充）**："长什么样"留在 `arena.css`（18px 高、圆角、内阴影、发光），
   "说什么"留在 `arena/ui/window.py`。ADR-0007 说"改配色/圆角/字号不该触碰 Python"，那是**外观**；
   条内文字的内容与开关属于**行为**，改它必须动 Python——本 ADR 把这条界线写明，避免以后被误读成违规。
4. 条内文字自带半透明深色底衬。理由：文字居中于整个控件，横跨**填充段（亮橙渐变）**与
   **未填充段（暗棕）**两种底色；纯靠字色无解（深色在暗棕底上消失、浅色在亮橙底上发飘），
   底衬让它恒为"浅字压深底"。
5. 归零时一并清空文字，不留上一轮的百分比（`reset_progress`）。

## 理由（Rationale）

- 进度条的价值是回答"现在到哪了"。脉冲档恰恰是最长的一档（EXAM 可能几十分钟），
  若条内也留空，这一档就退化成一个没有任何信息的滑动色块——这是最该补信息的地方。
- **不拆 BENCH 子进程**：短期能换来一个漂亮的比例，代价是现有历史里的 bench 数字失去可比性。
  宁可 BENCH 保持脉冲，把"这一个比例拿不到"如实呈现。
- **不改旁路**：不动 `ProgressSink` 的接口签名（`on_progress(kind, fraction, message)` 本来就能表达
  这两种档），因此 ADR-0008 的线程纪律与既有测试面都不受影响。

## 后果（Consequences）

### 实测证据（2026-10-18，离屏渲染 + 截图人工核对）

| 场景 | 结果 |
|---|---|
| 改前 | `988 × 12 px`，`show_text=False`，`text=None` |
| 改后（可量化） | `988 × 36 px`，`show_text=True`，`text` 随 fraction 变化：`0.42→'42%'`、`0.06→'6%'`、`1.0→'100%'` |
| 改后（脉冲档） | `988 × 37 px`，`fraction=0.00`（脉冲），`text='卷 1/2 · 已用 137s · 最后输出 3s 前'` |
| 可读性 | 0.42 / 1.00 / 脉冲三档截图人工核对：百分比与文案在两种底色上都清晰 |
| CSS 解析 | `ArenaTheme.apply()` 未触发（该方法会把任何 CSS 解析错误变成启动期异常） |
| 回归 | `tools/check_traceability.py` 通过；`--compare` 三行数字不变 |

### 代价与残余限制

- 文字**居中于整个控件**（GTK 既有行为）：fraction 很小时百分比会落在填充段右侧的空白处。
  要"紧跟填充末端"必须自绘或用额外控件，本 ADR 明确不做——那是 ADR 里"三段式指示器"那一档的成本。
- **BENCH 永远只有脉冲**：这是刻意保留的可比性代价，不是遗留缺陷。
- **未验证**：真实窗口（WSLg 之外的真实桌面、不同 GTK 主题/字体）下的字距与底衬观感；
  以及窗口很窄时（< 400 px）长文案是否会被挤压。当前只在默认 1020 px 宽度下核对过。

## 相关代码位置（Code Locations）

- `arena/ui/window.py::ArenaWindow._build_status_band`（`set_show_text(True)`）
- `arena/ui/window.py::ArenaWindow.set_progress`（百分比 / 事实文案两档）、`reset_progress`（清空文字）
- `arena/ui/controller.py::ArenaController.apply_progress`、`apply_stage`（进度与阶段的投递）
- `arena/theme/arena.css`（`progressbar trough` / `progressbar progress` / `progressbar text`）
- `arena/runners.py::ExamRunner._report_alive`（EXAM 的事实文案来源）、`PerplexityRunner`（唯一的真实比例）
