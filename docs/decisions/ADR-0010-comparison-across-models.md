# ADR-0010：跨模型对比是并列，不是聚合

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0005（历史）、ADR-0006（FP64）、ADR-0011（答卷吞吐）、ADR-0014（quick 档）、
  ADR-0018（负载档：只有 official 档进对比）

## 背景（Context）

需求：「基于llama的测试结果要保存到软件里，并且留存在历史记录，并且可以与其他模型的
llama工具的测试结果进行比较」。

历史里会出现同一模型的**多次** Run（不同时间、不同参数），以及不同模型的 Run。
摆在面前的问题是：一个模型在表里出现几行？旧值怎么办？能不能求平均？

事实背景：
- bench 结果受笔记本功耗墙/温度漂移影响（`pre_architecture.md` §10 风险表）；
- quick 档与真实档的负载不同，数字不可混比（ADR-0014）。

## 决策（Decision）

1. **每个 `model_path` 一行**（`ComparisonRow`），行内三类指标各取**该模型最新**的一次有效值。
2. **不聚合、不平均、不插值**：跨模型的记录永远并列展示（「对比是并列，不是聚合」）。
3. **缺值显示 `—`，排序时排在最后**：`rows.sort(key=lambda row: (row.bench_pp_ts is None, -(row.bench_pp_ts or 0.0)))`
   —— 名次只在有数字的行之间成立，缺值不参与排名。
4. **失败记录不进对比数值**：`ComparisonBuilder` 只采纳 `record.ok` 的记录；
   失败仍留在历史视图里（失败也是事实，见 ADR-0005）。
5. **对比入口有两个**：GUI 的 `COMPARE` 对话框与 `run_arena.py --compare`（同一
   `ComparisonBuilder`，不允许两条实现路径）。
6. **历史记分板遵循同一条取值精神**（2026-10-17 补充）：`Archive.latest_scoreboard()` 取
   **最近一次含成功记录的 Run**（该轮整轮展示，不跨 run 拼凑）；该模型全轮失败时退化为最新一轮，
   屏上显示"三个 `—` + 失败原因"。选模型时先看到的因此是"上次的成绩"，而不是最近一次失败的空白。
7. **只有 official 档进对比**（2026-10-17 补充，ADR-0018）：分组之前先经
   `arena.history.official_records()` 过滤，quick 自检记录一律不参与对比；
   `latest_scoreboard()` 同样**优先** official 档（只有 quick 记录时才照实显示，并由
   `Scoreboard.profile` 标明）。理由是"取最新有效值"这条策略本身没有能力区分负载——
   实测 quick 记录会以"更新"为名顶掉真实档成绩（ADR-0018 记录了复现现场）。

## 理由（Rationale）

- 求平均会把「快但笨」与「慢但准」混成一个不存在的模型——这是最容易被误读的失真。
- 取"最新有效值"而不是"最好值"：历史是时间序列，用户关心的是"现在这个模型跑出来多少"，
  而不是"历史上最好的一次碰巧是多少"（后者会随机器变热而系统性偏高）。
- 排序只看 pp 吞吐且缺值沉底：任何"补零参与排序"的做法都会让没测过的模型看起来最慢。

## 后果（Consequences）

- 代价：同一模型的历次 Run 在对比表里被"压扁"成一行，看不到方差。
  缓解：完整历史在 `HISTORY` 对话框与 `runs/arena_history.jsonl` 里逐条可见。
- `last_run_at` 与数字同源（2026-10-17 修订）：它取"产生这行数字的那次记录"的时间，
  而不是最新一次尝试的时间。起因是实测到一次环境性 CUDA OOM（ADR-0017）后，
  对比行出现「旧数字 + 新时间戳」的组合，读起来像"刚测出来的"。
  当该模型只有失败记录时，三个指标全为 `—`、时间戳退化为最后一次尝试时间，
  读作"试过但没成"——这是刻意的，比留空更有信息量。
- 后续若要比较"同一模型不同参数"，需要新 ADR：`ComparisonRow` 的键从 `model_path`
  改为 `(model_path, 负载形状)`，并给表加一列负载标签。
  **2026-10-17 更新**：这条"后续"被显式**否决**了，而不是被实现——既然软件不提供调参数入口
  （ADR-0018），就不存在"同一模型多种负载同时参与对比"的真实需求；负载不可调本身就让
  "同负载才可比"这个前提成立。将来若真出现第二种负载，仍需按本条的形态新开 ADR。

## 相关代码位置（Code Locations）

- `arena/history.py::ComparisonBuilder.build`（先滤档、再分组、取最新有效值、缺值沉底）
- `arena/history.py::official_records`（"只有 official 档进对比"的唯一执行点）
- `arena/history.py::Archive.records_for`、`latest_scoreboard`、`models`
- `arena/domain.py::ComparisonRow`、`Scoreboard.complete`、`Scoreboard.profile`
- `arena/ui/views.py::ComparisonView.present_rows`
- `arena/ui/controller.py::show_comparison`
- `run_arena.py::format_comparison`
