"""Local Arena — 子进程执行与取消。

模块名: arena.process
职责: 把"跑一个外部命令、按行回吐输出、可被取消/超时"这件事做对一次，供所有 Runner 复用。
      包含一条硬保证：**run() 在有限时间内返回**（取消/超时后不无限等待读取线程）。
依赖: 标准库 os/signal/subprocess/threading/time。
被依赖: arena.runners.*、arena.session（构造 CancellationToken）、run_arena.py（装配）。
相关 ADR: docs/decisions/ADR-0002-subprocess-over-server.md（含 2026-10-17 进程组修订）、
          docs/decisions/ADR-0008-worker-thread-ui-bridge.md
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

LineCallback = Callable[[str], None]


class ProcessStartError(RuntimeError):
    """命令无法启动（可执行文件不存在或无执行权限）。"""


class CancellationToken:
    """跨线程的取消信号。

    用 threading.Event 而非布尔量：Session 在 UI 线程调用 cancel()，
    Runner 在 worker 线程轮询 is_cancelled()，两者之间需要内存可见性保证。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """置位取消信号。

        前置条件: 无。
        后置条件: 此后 is_cancelled() 恒为 True；已启动的子进程由 SubprocessRunner 负责终止。
        禁止: 置位后复用本对象开始新的 Trial（需新建 token）。
        异常: 无。
        """
        self._event.set()

    def is_cancelled(self) -> bool:
        """查询取消信号。

        前置条件: 无。
        后置条件: 无副作用；可从任意线程调用。
        禁止: 无。
        异常: 无。
        """
        return self._event.is_set()


@dataclass(frozen=True)
class ProcessOutcome:
    """一次命令执行的完整结果快照（原始文本，不做解析）。"""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    wall_seconds: float
    cancelled: bool = False
    timed_out: bool = False
    # 为便于诊断而保留：被调工具自己写的日志文件路径（llama-cli 在本版本恒为空文件）。
    log_path: Path | None = field(default=None)

    @property
    def ok(self) -> bool:
        """命令是否"干净成功"：退出码 0 且既未取消也未超时。

        前置条件: 无。
        后置条件: 三条件同时成立时为 True。
        禁止: 只看退出码——被 terminate 的进程退出码可能是 0 或 143，取消不能算成功。
        异常: 无。
        """
        return self.returncode == 0 and not self.cancelled and not self.timed_out


class SubprocessRunner:
    """执行外部命令并把 stdout/stderr 按行回调出去。

    计时一律用 time.perf_counter()（单调），不用 time.time()：见 ADR-0006。
    """

    def __init__(self, poll_seconds: float = 0.2, reader_grace_seconds: float = 5.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds 必须为正数")
        if reader_grace_seconds <= 0:
            raise ValueError("reader_grace_seconds 必须为正数")
        self._poll_seconds = poll_seconds
        self._reader_grace_seconds = reader_grace_seconds

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_s: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
        on_stdout_line: LineCallback | None = None,
        on_stderr_line: LineCallback | None = None,
        cwd: Path | None = None,
    ) -> ProcessOutcome:
        """执行命令并收集输出。

        前置条件: command 非空；首元素为可执行文件路径。
        后置条件: 返回的 ProcessOutcome 必带 stdout/stderr 文本（可能是被终止前读到的部分）与墙钟耗时；
                   本方法**保证在有限时间内返回**：取消/超时后即使子进程留下了持有管道的孙进程，
                   也只等到 reader_grace_seconds 为止，不会永久阻塞调用者。
        禁止: 两个回调都直接在 GTK 主循环外触碰控件——它们在读取线程中被调用，
               必须自行切回主循环（见 ADR-0008）。
        异常: ProcessStartError —— 可执行文件不存在/无权限时抛出，绝不静默返回失败码。
        """
        if not command:
            raise ValueError("command 不能为空")

        argv = [str(part) for part in command]
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                # 行缓冲：perplexity 的逐 chunk 进度在 stdout 上是"一行内多段"，
                # 只有按行提供才能在分钟级运行中实时回报进度（ADR-0004）。
                bufsize=1,
                cwd=str(cwd) if cwd is not None else None,
                # 独立会话/进程组：取消时能连同 llama 工具拉起的子进程一起收掉，
                # 否则孤儿孙进程会一直持有管道（ADR-0002 的 2026-10-17 修订）。
                start_new_session=True,
            )
        except OSError as exc:
            raise ProcessStartError(f"无法启动命令 {argv[0]}: {exc}") from exc

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        readers = [
            threading.Thread(
                target=self._pump, args=(process.stdout, stdout_lines, on_stdout_line), daemon=True
            ),
            threading.Thread(
                target=self._pump, args=(process.stderr, stderr_lines, on_stderr_line), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()

        cancelled = False
        timed_out = False
        while True:
            returncode = process.poll()
            if returncode is not None:
                break
            if cancel_check is not None and cancel_check():
                cancelled = True
                returncode = self._terminate(process)
                break
            if timeout_s is not None and time.perf_counter() - started > timeout_s:
                timed_out = True
                returncode = self._terminate(process)
                break
            time.sleep(self._poll_seconds)

        # 有界等待读取线程：子进程被终止后，它可能留下仍持有管道的孙进程，
        # 此时无限 join 会让整个 Trial 永远挂住（2026-10-17 实测到的取消死锁）。
        # 线程是 daemon，未结束的那个不会阻止进程退出；代价是可能丢掉被取消 Trial 的尾部输出。
        for reader in readers:
            reader.join(timeout=self._reader_grace_seconds)
        wall_seconds = time.perf_counter() - started

        return ProcessOutcome(
            command=tuple(argv),
            returncode=returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
            wall_seconds=wall_seconds,
            cancelled=cancelled,
            timed_out=timed_out,
        )

    @staticmethod
    def _pump(stream, sink: list[str], callback: LineCallback | None) -> None:
        """把一条流按行读尽；回调异常不得吃掉剩余行（回调异常只记录不抛出）。"""
        if stream is None:  # pragma: no cover - Popen 已保证非 None
            return
        try:
            for line in stream:
                sink.append(line)
                if callback is not None:
                    try:
                        callback(line)
                    except Exception:  # noqa: BLE001 - 进度回调是尽力而为，不能影响取数
                        pass
        finally:
            stream.close()

    @staticmethod
    def _terminate(process: subprocess.Popen) -> int:
        """先请求优雅退出，5 秒不退则强杀整个进程组；返回最终退出码。

        前置条件: process 由本类以 start_new_session=True 启动（因此它是自己进程组的组长）。
        后置条件: 返回最终退出码；属于该组的全部子进程都收到过终止信号。
        禁止: 只对组长发信号——llama 工具可能拉起子进程，孤儿孙进程会继续持有管道与显存。
        异常: 无（进程组已消失时退化为对组长单个发信号）。
        """
        SubprocessRunner._signal_group(process, signal.SIGTERM)
        try:
            return process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            SubprocessRunner._signal_group(process, signal.SIGKILL)
            return process.wait()

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        """把信号发给子进程所在的整个进程组。

        前置条件: sig 为 signal.SIGTERM 或 signal.SIGKILL。
        后置条件: 组内所有进程都收到该信号（组长已退出时退化为点对点发送）。
        禁止: 对不属于本进程组的其他进程发信号——目标由 getpgid(pid) 现算，不缓存。
        异常: 无（进程已消失、权限不足都视为"无需再杀"）。
        """
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                return
