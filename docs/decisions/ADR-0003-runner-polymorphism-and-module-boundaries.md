# ADR-0003：Runner 多态 + 模块边界（谁可以依赖谁）

- **状态**：Accepted
- **日期**：2026-10-16
- **相关 ADR**：ADR-0002（子进程）、ADR-0004（解析器）、ADR-0008（线程桥）

## 背景（Context）

三类 Trial 的差异只在两处：「命令怎么拼」与「输出怎么解析成记录」；其余生命周期完全相同：
落阶段 → 执行 → 判定（取消/超时/退出码）→ 解析 → 产出记录 → 报进度 → 落盘。

同时代码里存在两个**方向相反**的依赖诉求：

1. `ArenaSession`（应用层）需要按种类调度 Trial，却不该认识 `llama-bench` 等具体工具；
2. `TrialRunner`（适配器）需要读 `TestPlan`、需要往外报进度，但 `TestPlan` 由 Session 构造、
   进度汇由 UI 提供——若 Runner 直接 import Session/UI，就形成循环依赖。

## 决策（Decision）

**决策一：模板方法模式。** `TrialRunner` 是抽象基类，固化 `run(context)` 生命周期；
子类只实现 `_build_command`、`_execute`、`_build_record`、`_new_record`。

**决策二：把「Runner 需要、但由上层构造」的东西下沉为不依赖任何上层的值对象。**

- `TestPlan` 放在 `arena/domain.py`（而非 Session 所在模块）：Runner 需要它，
  而 Runner 不能依赖 Session。
- `ProgressSink` / `NullSink` / `TrialContext` 放在独立的 `arena/sink.py`：
  端口与上下文都不认识 Session 与 UI。
- `TrialContext` 打包一次 Trial 的全部输入（`run_id/model/plan/sink/token`），
  Runner 的签名因此只有 `run(context)` 一个参数。

**决策三：依赖方向单向且可枚举。**

```
run_arena → arena.ui → arena.session → arena.runners → arena.parsers / arena.process / arena.sink
arena.session → arena.store / arena.history / arena.domain
arena.domain / arena.vocabulary / arena.parsers → 不依赖任何上层
arena.ui.* → 唯一 import gi 的地方
```

## 理由（Rationale）

- 生命周期只有一份实现，重复是 bug 的温床：三个 Runner 各自实现「超时了怎么记账」迟早不一致。
- 闭包扩展：新增第四种工具（如 `llama-batched-bench`）只需加一个子类 + 注册进
  `build_runners()`，`ArenaSession` 一行不改。
- 把 `TestPlan`/`ProgressSink` 下沉，是**为消除循环依赖付出的最小代价**：两个模块都更"哑"了，
  反而更好测（`parsers`/`domain` 可以脱离 GTK 与 Session 单测）。

## 后果（Consequences）

- `TrialRunner.run()` 内不允许抛异常：失败（取消/超时/退出码/解析）都转成 `ok=False` 的记录。
  这样一次失败不会抹掉整轮历史的其余部分（见 ADR-0005 的追加语义）。
- 代价：`arena/sink.py` 这个模块看起来"只为断依赖而存在"。接受——它的职责在文件头写明。
- 代价：`TestPlan` 字段较多（三类负载参数都在里面），是"记录一个 Run 的全部输入"的必然结果；
  它换来的是 Runner 只读一个对象。

## 相关代码位置（Code Locations）

- `arena/runners.py::TrialRunner`、`run`、`_execute`、`_build_record`、`_failure`、
  `arena/runners.py::BenchRunner`、`PerplexityRunner`、`ExamRunner`、`build_runners`
- `arena/sink.py::ProgressSink`、`NullSink`、`TrialContext`
- `arena/domain.py::TestPlan`（下沉理由写在类 docstring）
- `arena/session.py::ArenaSession.run`、`ArenaSession._context`
- `arena/vocabulary.py::RUN_ORDER`（Run 内 Trial 顺序的唯一来源）
