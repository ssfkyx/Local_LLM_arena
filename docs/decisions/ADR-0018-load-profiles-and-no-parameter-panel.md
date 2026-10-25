# ADR-0018：负载档只有两种、quick 只作自检、不提供参数面板

- **状态**：Accepted
- **日期**：2026-10-17
- **相关 ADR**：ADR-0010（对比是并列）、ADR-0013（配置是唯一来源）、ADR-0014（headless/quick）、
  ADR-0016（记分板显示策略）

## 背景（Context）

本软件的用途被明确为一句话：**在当前硬件下尽量让模型跑满，以此做性能与效率比较。**
这句话里藏着一条硬前提——「比较」只在**同一负载、同一参数**下才成立。而上一轮的实现与这条前提是脱节的：

1. `ComparisonBuilder.build()` 只用 `model_path` 分组，取「最新有效值」，**完全不看**这组数字是在什么负载下测的。
2. 因此 `--headless --quick` 会把该模型的正式成绩顶掉：实测 `2026-10-17T01-20-56` 那次 quick 自检就落在
   `01-21-28` 的真实档成绩之前 32 秒，两者在表里争同一个位置。
3. quick 的数字**看起来完全正常**（pp 1318 t/s、PPL 13.04 这种量级）——ADR-0014 用文字禁止了使用它，
   但代码结构拦不住，禁令与机制脱节。
4. 「同参数」在历史里**无法核验**：`BenchRecord` 没记 `reps`，`ExamRecord` 完全没记
   `n_predict / n_ctx / temperature / seed`。也就是说，即使有人想验证「这两行是不是同一条件」，
   数据也不够。

同时上一轮留了两个未决问题：是否给对比表加「负载档」维度，是否给 GUI 加参数面板。

## 决策（Decision）

1. **负载档只有两个名字**：`PROFILE_REAL` / `PROFILE_QUICK`，定义在 `arena/vocabulary.py`；
   具体的负载参数仍住在 `arena/config.py`。两者的装配入口只有 `AppConfig.from_env()` 与
   `AppConfig.quick()`。
2. **`AppConfig.profile` 是白名单字段**：构造期拒绝任何不在 `PROFILES` 里的值，因此
   「第三种负载」无法被表达——这是 `profile` 能当作可信对比键的前提。
3. **每条 `TrialRecord` 落盘 `profile`**，由 `TrialRunner._base_record_kwargs` 从 config 盖戳。
   缺该字段的历史行按 `OFFICIAL_PROFILE` 读；能判定的遗留行另做一次性回填（见第 9 条）。
4. **对比只收 official 档**：`arena.history.official_records()` 过滤后，`ComparisonBuilder` 才分组；
   历史记分板 `Archive.latest_scoreboard()` **优先** official 档（只有 quick 记录时才照实显示 quick，
   并由 `Scoreboard.profile` 标明来源）。这是结构性保证：不依赖读者知不知道 quick 是什么。
5. **quick 只作自检**：它的记录照常落盘、照常出现在 `HISTORY` 视图（并多一列「档位」），
   但不参与跨模型对比；`--headless --quick` 结束时打印一行说明，避免「我跑过了，怎么表里没有」的困惑。
6. **补全负载参数落盘**：`BenchRecord.reps`、`ExamRecord.load`（`ExamLoad` 值对象）。
   让「同参数」从口头承诺变成可核验的事实。
7. **不提供参数面板**（对上一轮未决问题 4 的反向决策）：软件不设任何调整负载参数的入口——
   CLI、环境变量、GUI 都没有。参数是**实验条件**，不是用户偏好。
8. **不给 `ComparisonRow` 加「负载档」维度**：既然负载不可调，就不存在「同一模型多种负载同时被比较」
   的真实需求；键仍是 `model_path`。将来若真的需要，按 ADR-0010 的「后果」另开 ADR。
9. **遗留数据一次性回填，且只回填可判定的**：`profile` 之前的历史行里，4 轮 quick 自检可由该行
   **自身落盘的负载参数**判定（bench `n_prompt=64`、perplexity `n_chunks=2`；当时的真实档是 512 / 8），
   这 12 行补上 `"profile": "quick"`；判不出来的行保持无字段、读作 official。
   回填方式是**纯文本插入一个键**（原文件备份到 `/tmp/arena_history.jsonl.pre-profile.bak`），
   不触碰任何既有数值——逐行比对已验证「除插入外零改动，且插入后仍是合法 JSON」。
   （真实档的 PPL chunk 数随后由 ADR-0020 改为 `-1`＝全语料；档位机制本身不变，
   判定仍以本行落盘的 `n_chunks`/`n_predict` 为准。）

## 理由（Rationale）

- **「同负载同参数才可比」决定了实现形态。** 它可以靠两种方式满足：让负载进入对比键，或者让负载**不可变**
  从而只剩一个档。本轮选后者，因为它同时满足了用户「不要参数面板」的要求，并且更少机制：
  一个白名单字段 + 一处过滤，就替代了「多档并存 + 扩表 + 重定排名规则」的整套复杂度。
- **用档名判定，而不是「参数看起来像不像 quick」。** 从记录里反推档位是猜测：`reps` 此前根本没落盘，
  无法区分 `-r 1` 与 `-r 3`。落盘的档名是事实，可被机器判定。
- **拒绝参数面板的理由不是「麻烦」，而是它会把事故变成日常。** 面板一上线，
  「参数不同却记在同一模型名下」就从理论风险变成随手可得的操作——而这正是 ADR-0017
  拒绝自动降级所防的同一件事。ADR-0013 已经给了参数的唯一来源，再开一个入口等于把那个决定推翻。
- **指导原则是「跑满」，不是「可调」。** 想比较性能与效率，正确做法是固定一套满载参数、
  一次只改一个变量（模型）；参数可调反而引入第二个变量，使对比失去意义。
- **补记 `reps` 与 exam 参数是低成本高收益的**：它们已经存在于 `TestPlan` 里，只是没写进记录；
  写进去之后，「这两行是不是同一条件」不再需要信任任何人。

## 后果（Consequences）

- **代价（已知限制，必须如实说明，2026-10-17 修订）**：`profile` 是 2026-10-17 新增字段，
  此前的历史行没有它。经逐轮核对（`runs/arena_history.jsonl`），其中
  **4 轮 quick 自检**（`23-14-40`、`23-22-40`、`23-38-56`、`01-20-56`）可由落盘的
  `n_prompt=64 / n_chunks=2` 判定，已按决策 9 回填 `profile: quick`（12 行）。
  剩余无字段的行（21 条）读作 official，其中包含 **4 轮全程失败**（F14 的 CUDA OOM）
  ——工具在**模型加载阶段**就失败，stderr 里没有打印任何负载参数，因此**无法从数据判定档位**，
  只能维持 official；另外 3 轮本就是真实档，标 official 是正确的。
  由于这 4 轮不产出任何数字，数值对比不受影响（迁移前后对比表逐值核对一致）。
  原始文件备份在 `/tmp/arena_history.jsonl.pre-profile.bak`。
- **为什么破例动了历史文件**（ADR-0005 把 JSONL 定义为只追加的事实序列）：HISTORY 视图新增
  「档位」列后，不回填会让屏上出现**同一行内自相矛盾**的记录——负载列写着 `pp64`（quick 的负载），
  档位列却写着 `real`。让界面主动说错话，比「暂时没有这个字段」更糟。
  回填的值不是新事实，而是对**同一行里已经存在的事实**的读取（`pp64` 蕴含 quick）；
  对判不出来的行则坚决不填，宁可不标也不猜。
- **代价**：用户不能临时试「`-p 256` 会怎样」。要试必须改配置代码或新增装配档——这是刻意的。
- **收益**：一次 `--headless --quick` 不再可能污染对比表；这是结构保证，而不是靠注释提醒。
- **收益**：实验条件（`reps`、`n_ctx`、`n_predict`、`temperature`、`seed`）现在全部可核验，
  未来若要重跑「同一条件」，条件本身有据可查。
- **收益**：`profile` 出现在记分板末行、`HISTORY` 的「档位」列、`COMPARE` 标题与 `--compare` 表头，
  读者在任何一处都能看到「这组数字是哪一档的」。
- **后续若要参数面板**，需要新 ADR，并同时回答：参数如何进入记录（哪种负载形状）、
  对比键如何扩展、以及如何避免「参数不同却记在同一模型名下」。

## 相关代码位置（Code Locations）

- `arena/vocabulary.py::PROFILE_REAL`、`PROFILE_QUICK`、`PROFILES`、`OFFICIAL_PROFILE`
- `arena/config.py::AppConfig.profile`、`AppConfig.__post_init__`、`from_env`、`quick`
- `arena/domain.py::TrialRecord.profile`、`TrialRecord.to_dict`、`TrialRecord.from_dict`、
  `BenchRecord.reps`、`ExamLoad`、`ExamRecord.load`、`Scoreboard.profile`
- `arena/runners.py::TrialRunner._base_record_kwargs`、`BenchRunner._build_record`、`ExamRunner._build_record`
- `arena/history.py::official_records`、`ComparisonBuilder.build`、`Archive.latest_scoreboard`
- `arena/session.py::ArenaSession.default_plan`
- `arena/ui/views.py::ScoreboardView.render`、`HistoryView.present_records`、`ComparisonView`
- `arena/ui/controller.py::show_comparison`
- `run_arena.py::selected_model`、`run_headless`、`format_scoreboard`、`format_comparison`
