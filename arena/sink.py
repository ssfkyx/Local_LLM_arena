"""Local Arena — 进度汇端口与 Trial 上下文。

模块名: arena.sink
职责: 定义 Runner 向外汇报进度的**唯一**接口（ProgressSink）、其空实现，
      以及把一次 Trial 所需的全部输入打包成 TrialContext。
      本模块存在的理由是断开循环依赖：Runner 需要 Sink 与 TestPlan，
      而 Sink 由 Session 提供（见 ADR-0003 模块边界一节）。
依赖: arena.domain、arena.process、arena.vocabulary。
被依赖: arena.runners、arena.session、arena.ui.controller、run_arena.py。
相关 ADR: docs/decisions/ADR-0003-runner-polymorphism-and-module-boundaries.md、
          docs/decisions/ADR-0008-worker-thread-ui-bridge.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from arena.domain import Model, TestPlan
from arena.process import CancellationToken
from arena.vocabulary import Stage, TestKind


class ProgressSink(ABC):
    """进度汇：Trial 向外汇报阶段与进度的回调接口。

    实现者必须自证线程安全：Runner 在 worker 线程调用这些方法（ADR-0008）。
    """

    @abstractmethod
    def on_stage(self, kind: TestKind, stage: Stage) -> None:
        """汇报某一 Trial 进入新阶段。

        前置条件: kind 为该 Trial 的种类；stage 为当前阶段。
        后置条件: 实现方已记录/展示该阶段；不得抛异常影响 Trial（异常会被调用方吞掉）。
        禁止: 在实现内长时间阻塞——它在 worker 线程里，会拖慢测试本身。
        异常: 实现方不应抛出。
        """

    @abstractmethod
    def on_progress(self, kind: TestKind, fraction: float | None, message: str) -> None:
        """汇报某一 Trial 的进度。

        前置条件: fraction 为 0.0~1.0 的完成度，或 None 表示"无法量化"。
        后置条件: 实现方已展示进度；不改变 Trial 的行为。
        禁止: 把 fraction 当成精确值用于判断是否结束——结束由 Trial 自己决定。
        异常: 实现方不应抛出。
        """

    @abstractmethod
    def is_cancelled(self) -> bool:
        """查询用户是否要求取消当前 Run。

        前置条件: 无。
        后置条件: 无副作用；可从任意线程调用。
        禁止: 返回随机/带状态的"一次性"结果——Runner 会反复轮询。
        异常: 无。
        """


class NullSink(ProgressSink):
    """不做任何事的进度汇（headless 测试与单元测试用）。"""

    def on_stage(self, kind: TestKind, stage: Stage) -> None:
        """忽略阶段事件。

        前置条件: 无。
        后置条件: 无副作用，恒返回 None。
        禁止: 偷偷记录状态——空实现的语义就是"没人关心"。
        异常: 无。
        """
        return None

    def on_progress(self, kind: TestKind, fraction: float | None, message: str) -> None:
        """忽略进度事件。

        前置条件: 无。
        后置条件: 无副作用，恒返回 None。
        禁止: 校验参数并抛错——空实现不能成为隐形的契约检查点。
        异常: 无。
        """
        return None

    def is_cancelled(self) -> bool:
        """永不取消。

        前置条件: 无。
        后置条件: 恒返回 False。
        禁止: 无。
        异常: 无。
        """
        return False


@dataclass(frozen=True)
class TrialContext:
    """一次 Trial 的全部输入：谁、按什么计划、归属哪一轮、向谁汇报、能否取消。"""

    run_id: str
    model: Model
    plan: TestPlan
    sink: ProgressSink
    token: CancellationToken

    @property
    def cancel_check(self) -> Callable[[], bool]:
        """适配 SubprocessRunner 需要的取消检查回调。

        前置条件: 无。
        后置条件: 返回 sink.is_cancelled 本身（不包闭包，调试时能看清来源）。
        禁止: 缓存 sink 的返回值——取消必须每次实时查询。
        异常: 无。
        """
        return self.sink.is_cancelled
