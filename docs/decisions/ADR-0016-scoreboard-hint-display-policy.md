# ADR-0016：记分板提示列的显示策略（值列只放事实，路径只作提示）

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0007（外置 CSS）、ADR-0009（不判分）、ADR-0011（答卷吞吐）

## 背景（Context）

记分板是需求里的核心产出面：「最终界面包含 bench 的数字，和 perplexity 的最终数字」。

初版把答卷的**绝对路径**直接写进提示列，实测后果是外观走形：
窗口默认 1020×790，但提示列按最长文本（
`/home/cielo_1/projects/local_arena/runs/2026-10-16T23-38-56_Meta-Llama-3.1-8B-Instruct/answers/…`）
撑出自然宽度，整个窗口被顶到 **2275×790**：标题居中而状态行两端对齐，版面失衡。

同时另一个诉求是真实存在的：答卷路径需要**可查**（用户要去找那份文件），不能删掉了事。

## 决策（Decision）

1. **值列只放事实**：`BENCH` 放吞吐、`PERPLEXITY` 放 PPL ± stderr、`EXAM` 放"`N` 份答卷已落盘"。
   值列**不放**解释性文字，也不放路径。
2. **提示列放辅助信息，且必须可截断**：所有提示标签统一
   `set_ellipsize(Pango.EllipsizeMode.MIDDLE)` + `set_max_width_chars(56)`
   （常量 `_HINT_MAX_CHARS`）——让提示列的**自然宽度有上限**，从而窗口不会被内容顶宽。
3. **路径退化为"文件名 + tooltip"**：提示列显示文件名（多份以 ` · ` 分隔，中间截断），
   完整路径放进 `set_tooltip_text()`（每行一条）。要全部路径的场景走 `HISTORY` 视图与
   `runs/arena_history.jsonl`。
4. **缺值仍然是 `—`**（不是 0、不是空白），这条与 ADR-0010 的对比表一致。
5. **失败行同样必须可截断**（2026-10-17 补充）：失败原因里塞的是工具的 stderr 尾部，
   实测一次 CUDA OOM 让这一行把窗口从 1020px 顶到 **4644px**。
   因此 `_failure_label` 用 `EllipsizeMode.END` + `max_width_chars=120`（`_FAILURE_MAX_CHARS`），
   完整原因进 tooltip。**结论：这条纪律覆盖记分板上所有"文本量不可控"的控件，不只提示列。**

## 理由（Rationale）

- 界面的宽度必须由**布局**决定，不能由数据内容决定——只要有一个"跟随内容变宽"的标签，
  任意长路径/长错误信息都能再次把窗口顶坏。
- 中间截断（MIDDLE）而不是尾部截断：答卷文件名形如
  `<模型>__<试卷>.md`，首尾两端（模型名与 `.md` 后缀）都是识别信息，中间是可省的部分。
- tooltip 是 GTK 已有的能力（"优先使用框架已有能力"），不需要自造弹窗或复制按钮。
- 值列只放事实，是因为这一行会被跨模型对比的读者当作唯一依据；
  混入路径会让"读数字"变成"读文件名"（并顺带把版面顶坏）。

## 后果（Consequences）

- 代价：一屏内看不到答卷的完整路径。缓解：tooltip + HISTORY 视图 + JSONL，
  三者都能拿到原始路径；`Scoreboard.answer_paths` 本身没有被削减。
- 收益：窗口稳定在 1020×790，标题/状态行/记分板三者的对齐关系固定，
  可回归比对（`docs/ui/arena-window.png` 是当前基线）。
- 约束的可检查性：新增任何提示类文本都必须走 `_HINT_MAX_CHARS` 的截断路径；
  这条纪律写在 `ScoreboardView._build_rows` 与 `_set_exam_hint` 的契约里。

## 相关代码位置（Code Locations）

- `arena/ui/views.py::ScoreboardView._build_rows`（统一截断与宽度上限）
- `arena/ui/views.py::ScoreboardView.render`、`_set_exam_hint`（文件名 + tooltip）
- `arena/ui/views.py::_HINT_MAX_CHARS`、`_MISSING`
- `arena/theme/arena.css::.metric-hint`、`.metric-value`、`.scoreboard`
- `arena/domain.py::Scoreboard.answer_paths`（数据仍是完整路径）
