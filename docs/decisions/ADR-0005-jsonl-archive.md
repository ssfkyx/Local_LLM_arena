# ADR-0005：历史用追加式 JSONL，不引入数据库

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0003（生命周期与失败记账）、ADR-0010（对比）、ADR-0011（答卷）

## 背景（Context）

需求要求：三类测试结果要「保存到软件里、留存在历史记录、并能与其他模型的测试结果比较」；
答卷要「输出一份文件」。

候选方案：
1. 关系库（sqlite3 或 PostgreSQL）；
2. 单文件 JSON（每次读出全量、写回全量）；
3. 追加式 JSONL（一行一条记录）；
4. 无历史，只看屏幕。

本项目的读模式极其简单：**全量读取 + 内存分组**（跨模型对比就是按 `model_path` 分组），
没有任何按条件查询、并发写、事务、索引的需求。

## 决策（Decision）

**方案 3：`runs/arena_history.jsonl`，一行一条 `TrialRecord`（UTF-8、`ensure_ascii=False`）。**

1. `ResultStore` 是端口（`append` / `load_all`），`JsonlResultStore` 是唯一实现。
2. **每次 Trial 结束立即 append**（先落盘再继续下一个 Trial）——即使下一刻崩溃或被取消，
   已完成的记录也不会丢。
3. 单行损坏**不放弃整份历史**：`load_all` 告警后跳过该行（`warnings.warn` 带行号）。
4. 原始输出流另存为文件（`RunArtifacts`：`runs/<run_id>/bench.out`、`perplexity.err`、
   `transcript-paper_1.out` …），JSONL 里只放**已解析的数字**与答卷正文。
5. 答卷正文另写成人类可读的 Markdown（`AnswerSheetWriter`），文件名含模型标识。

## 理由（Rationale）

- 依赖为零：`json` + `pathlib` 而已，符合「优先使用标准库已有能力」。
- 追加语义与「事实序列」天然匹配：历史是只读的过去，不存在 UPDATE。
- 人类可读/可手工修复：出问题时 `tail` 就能看，损坏行能肉眼定位（本次盘点就是这么发现
  第 7 行被写成 NUL 的）。
- 对比是**内存里的分组**，不是数据库查询——见 ADR-0010。

## 后果（Consequences）

- 代价：删除/改写记录不方便（本阶段不需要）；全量读取在记录极多时线性变慢
  （当前 9 条，量级无关紧要）。
- 代价：没有 schema 版本号字段。缓解：`TrialRecord.from_dict` 用 `payload.get()` 容忍缺字段，
  未知 `kind` 抛错而不是静默丢弃（`ValueError`）。
- 收益：失败也是一等公民。23:14 那次 EXAM 解析失败留下了一条 `ok=false` 记录，
  保留了错误原因，成为 F6→F12 修正的现场证据。
- 收益：`--compare` 可以脱离 GUI 与数据库，在纯 CLI 下重建对比表。

## 相关代码位置（Code Locations）

- `arena/store.py::ResultStore`、`JsonlResultStore.append`、`JsonlResultStore.load_all`
- `arena/store.py::AnswerSheetWriter.write`、`_render`
- `arena/store.py::RunArtifacts.save`、`run_dir`
- `arena/domain.py::TrialRecord.to_dict`、`TrialRecord.from_dict`、`payload`、`decode_payload`
- `arena/session.py::ArenaSession.run`（先 append 再继续）
- `arena/history.py::Archive.all_records`（每次重新读盘）
- `run_arena.py::build_session`（历史文件名的唯一来源 `HISTORY_FILENAME`）
