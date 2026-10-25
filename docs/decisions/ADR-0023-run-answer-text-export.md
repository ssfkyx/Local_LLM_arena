# ADR-0023：答卷纯文本导出（一轮一份 answers.txt）

- **状态**：Accepted
- **日期**：2026-10-18
- **相关 ADR**：ADR-0004（正文即模型原话）、ADR-0005（JSONL 历史）、ADR-0009（不判分）、
  ADR-0010（对比是并列；同一功能不允许两条实现路径）、ADR-0013（配置/名字单一来源）、
  ADR-0018（负载档）、ADR-0021（逐卷落盘与部分成功）、ADR-0022（覆盖度核对）

## 背景（Context）

需求：「会跨系统/跨应用使用，只保留 JSON 有点蛋疼」，要求把模型回答试卷的文本单独输出成文件；
用户给出的形态是「第一步跑完输出 json 不变，**最后多一步**输出文本」。

先盘点现状（实测，不是推测）：

| 已有产物 | 位置 | 为什么不够 |
|---|---|---|
| 逐卷答卷 `.md` | `runs/<轮次>/answers/<模型>__<试卷>.md`（ADR-0021） | 已满足"一份答卷一个文件"，但一轮里散成多个文件，跨系统拷贝要连带目录 |
| 正文内嵌在 JSONL 里的 `payload.sheets[].text` | `runs/arena_history.jsonl` | 机读友好、人读不便；正因为 ADR-0021 写了"禁止只写路径不写正文"，正文其实**已经在索引里** |
| `transcript-paper_*.out` | 同轮目录 | llama-cli 的 TUI 全文，含启动 banner 与 prompt 回显，不是给阅读的答卷 |

所以缺的不是"有没有文本"，而是**一轮一份、自包含、纯文本**的产物。而正文已在索引这一事实意味着：
导出可以是**纯离线**的——不重跑模型、不依赖 transcript 重新解析，因此也能回查已有轮次。

## 决策（Decision）

1. 新增 `arena/export.py::render(record, papers_dir=None)`：把一条 `ExamRecord` 渲染成汇总文本。
   它是**纯函数**——只读传入的记录与试卷文件，不落盘、不判分、不改写正文。
2. **一轮 Run 收尾多一步**：`ArenaSession.run()` 在写完历史之后调 `_export_answers`，落
   `runs/<run_id>/answers.txt`。文件名常量 `ANSWERS_TEXT_FILENAME` 按 ADR-0013 放 `arena/vocabulary.py`。
3. **导出失败只告警**（`warnings.warn`），绝不让 `run()` 抛错、绝不动已经写好的历史记录：
   附加产物没有资格杀死一轮真实测量。
4. **无答卷时不写文件**：EXAM 没跑或每一卷都没跑成时，`_export_answers` 返回 `None`、不产出空文件——
   否则"没跑"会被读成"跑出来是空的"。
5. **正文逐字嵌入**：不 strip、不折行、不剥离思考链，块边界靠分隔线而不是靠裁剪正文（ADR-0004）。
6. **覆盖度按"导出时"的试卷文本重算**（复用 `arena.coverage`），并在文件头写明这个口径。
   之所以不写成"运行时的结论"：覆盖度**没有**落进记录 schema（ADR-0022 第 4 条明确不改），
   记录里根本没有这个字段；运行当时的结论仍留在 `answers/` 各卷 `.md` 的文件头。
7. 配 `tools/export_answers.py` 回查已有轮次。选择器：不给参数 = **每个模型最新一轮**；
   `--all` = 索引里所有有答卷的轮次；`--run-id` = 指定轮次。默认**跳过**已存在的文件（`--force` 才覆盖），
   避免抹掉人工编辑过的产物。
8. **不改 `PROTOCOL_NAME`**：它标记的是"负载参数 + 完成条件"，导出不改任何测量口径，
   因此历史不作废、不需要像 ADR-0021 那样归档重置。这是本 ADR 与 ADR-0021 的关键区别。
9. 产物用 `.txt` 而不是 `.md`：它的用途是跨系统/跨应用直读（双击就能开），内容里的 Markdown 记号
   照旧保留即可，不需要靠扩展名去声明格式。

## 理由（Rationale）

- **复用索引里的正文，而不是重新解析 transcript**：`sheets[].text` 是 EXAM 收尾时已经抽好并写进历史的
  那一份；从原始流再解析一次等于让导出"重新解释"事实，解析链有 bug 时两个产物会互相矛盾。
- **一份实现、两处调用**：渲染放 `arena/export.py`，`ArenaSession` 收尾与 `tools/export_answers.py`
  都调它。ADR-0010 第 5 条已经因为"同一 `ComparisonBuilder`"立过这条规矩。
- **收尾步骤而不是新入口**：用户明确要"输出 json 不变，最后多一步"，因此不动 `run()` 里既有的
  逐条落盘顺序（ADR-0021 的部分持久化前提），只在循环之后追加。
- **默认"每模型最新一轮"**：这正是用户所说"最新的三个模型"的语义；把"全部轮次"留给显式的 `--all`，
  避免默认就把归档前的重复轮次铺满一屏。

## 后果（Consequences）

### 实测证据（2026-10-18）

| 场景 | 结果 |
|---|---|
| 回溯导出（每模型最新一轮，默认选择器） | 3 轮：写出 3 · 跳过 0 · 失败 0。文件字符数 Qwen3-30B **11116**、Meta-Llama **21972**、Qwen3-MOE **6241** |
| **保真**：每卷正文是否被改写 | 三份文件里，每条 `sheet.text` 都是文件的**子串**（逐字一致）；Qwen3-30B 的 `[Start thinking]…[End thinking]` 段完整保留 |
| 覆盖度行 | 三份文件都是 `覆盖 30/30（按题号核对）`，与 `answers/*.md` 文件头一致（试卷自 12:25 起未再改动） |
| 幂等 | 再跑一次同一条命令：3 轮全部 `SKIP`（同一渲染器，输出稳定） |
| **收尾钩子端到端** | `--headless --quick --model Qwen3-MOE… --runs-dir /tmp/arena_quick_check`（隔离产物目录）：18.2 s 跑完并在轮次目录产生 `answers.txt`；覆盖度 `2/30`（quick 的 `n_predict=64` 所致，如实报告） |
| 官方产物未被触碰 | `runs/arena_history.jsonl` 仍 **24** 条、`runs/` 目录数仍 **35**、`--compare` 三行数字完全不变 |
| 部分成功路径（合成记录） | 真实历史里**没有**样本：9 条失败 EXAM 记录的 `sheets` 全是 0。用合成记录（1 卷成功 + 1 卷超时）验证：头部出现 `- 逐卷失败: paper_2（执行超时）` 与 `- 记录状态: ok=False · …`，成功那卷的正文照常输出 |
| 零答卷路径（合成记录） | 只输出头部，含 `- 注意: 本轮没有落盘任何答卷（未跑 EXAM，或每一卷都没跑成）`；不产生试卷块，也不编造覆盖度 |

### 代价与残余限制

- **同一份答卷在磁盘上有三份**（JSONL 内嵌 + `answers/*.md` + `answers.txt`）。知情接受：
  三者用途不同——机读索引 / 逐卷文件 / 整轮直读；体积在 KB~几十 KB 量级，不构成负担。
- **覆盖度可能"导出时重算"≠"运行当时"**：若试卷在导出前被改版，重算结果会与 `answers/*.md` 文件头不一致。
  文件头已显式写明口径，且运行当时的结论仍在 `.md` 里，因此两个数字都可追溯；但读者需要知道看的是哪一个。
- **旧记录若缺 `sheets[].text`**（理论上不会发生，ADR-0021 的禁止条就是为它写的），渲染会得到空正文段，
  当前不做特殊处理——那种记录本就违背了已有 ADR。
- **未验证**：真实的多卷部分成功样本（历史里不存在）；跨系统打开时的换行/编码表现（文件是 UTF-8 无 BOM + LF，
  由 `Path.write_text` 保证，但未在 Windows 上实测）。

## 相关代码位置（Code Locations）

- `arena/export.py::render`（`_header` / `_sheet_block` / `_coverage_for` / `_load_line` / `_failures_line`）
- `arena/session.py::ArenaSession.run`、`ArenaSession._export_answers`
- `arena/vocabulary.py::ANSWERS_TEXT_FILENAME`
- `arena/store.py::RunArtifacts.save`（写出）、`JsonlResultStore.load_all`（回查）
- `arena/coverage.py::compare`（覆盖度重算，与 ADR-0022 同一套）
- `tools/export_answers.py`（回查工具；`exam_records_with_sheets` / `latest_per_model` / `pick` / `export_one`）
