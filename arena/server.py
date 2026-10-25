"""Local Arena — `llama-server` 生命周期、逐题问答客户端与日志跟踪（EXAM 专用）。

模块名: arena.server
职责: 把"起一个常驻 llama-server、等它就绪、按题发独立问答请求、按题读自己的统计行、按进程组收尾"
      这件事做对一次。供 EXAM Runner 逐题调用（ADR-0025；ADR-0002 的 2026-10-19 修订）。
依赖: 标准库 http.client/json/os/signal/socket/subprocess/threading/time/urllib.request/pathlib。
被依赖: arena.runners（ExamRunner）。
相关 ADR: docs/decisions/ADR-0002-subprocess-over-server.md（含 2026-10-19 修订）、
          docs/decisions/ADR-0025-per-question-exam.md、
          docs/decisions/ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md、
          docs/decisions/ADR-0020-completion-condition-instead-of-token-cap.md、
          docs/decisions/ADR-0026-in-question-progress-from-server-log.md
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# 取消轮询间隔：与 SubprocessRunner 的 poll_seconds 同量级；越小取消越灵敏、空转越多。
_POLL_SECONDS = 0.2
# 关连接后等待 worker 线程退出的上界（秒）：线程是 daemon，超时不会阻塞调用者。
_CANCEL_GRACE_SECONDS = 5.0
# 题内心跳间隔（秒）：一次问答请求在途时，每这么久叫一次 on_heartbeat（ADR-0026）。
# 10 s 是"人读得出数字在动"与"不把 UI 刷爆"之间的量级；它只影响显示频率，不影响测量。
_HEARTBEAT_SECONDS = 10.0


class ServerStartError(RuntimeError):
    """`llama-server` 无法启动或未在时限内就绪。"""


class CompletionError(RuntimeError):
    """一次问答请求失败（连接错误、非 200、响应结构不可解析）。"""


class CompletionCancelled(CompletionError):
    """一次问答请求被用户取消（连接被主动关闭）。"""


class CompletionTimeout(CompletionError):
    """一次问答请求超过本给予的墙钟上限。"""


@dataclass(frozen=True)
class Completion:
    """一次问答请求的结果快照（取自 server 的 JSON，不含任何 TUI 文本）。

    `text` 是 server 解析出的**最终答案正文**（`message.content`）；thinking 模型的
    思考链由 llama-server 默认的 `reasoning-format=deepseek` 放进 `message.reasoning_content`，
    因此单独记在 `reasoning` 里。正常答题时 `text` 非空；上下文被思考链打满时
    `text` 可能为空而 `reasoning` 非空——调用方据此决定是否用 `reasoning` 兜底。
    """

    text: str
    stop_reason: str
    prompt_n: int
    predicted_n: int
    prompt_ts: float
    generation_ts: float
    wall_seconds: float
    # 默认空串：非 thinking 模型/旧响应没有这个字段，调用方无需做特殊分支。
    reasoning: str = ""


def _free_port() -> int:
    """向内核要一个当前空闲的本地端口号（不保证后续不被抢占，够用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LlamaServer:
    """一个常驻 `llama-server` 进程。

    以 `start_new_session=True` 启动（它因此是自己进程组的组长），收尾时对**整个进程组**发信号——
    只杀会话首进程会留下仍在占显存的孤儿（ADR-0025 的 F34）。
    """

    def __init__(
        self,
        binary: Path,
        model_path: str,
        *,
        n_ctx: int,
        vram_margin_bytes: int,
        startup_timeout_s: float,
        host: str = "127.0.0.1",
        log_path: Path | None = None,
    ) -> None:
        if n_ctx <= 0:
            raise ValueError(f"n_ctx 必须为正数: {n_ctx}")
        if vram_margin_bytes < 0:
            raise ValueError(f"vram_margin_bytes 不能为负: {vram_margin_bytes}")
        if startup_timeout_s <= 0:
            raise ValueError(f"startup_timeout_s 必须为正数: {startup_timeout_s}")
        self._binary = Path(binary)
        self._model_path = model_path
        self._n_ctx = n_ctx
        self._vram_margin_bytes = vram_margin_bytes
        self._startup_timeout_s = startup_timeout_s
        self._host = host
        self._log_path = Path(log_path) if log_path is not None else None
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._port: int | None = None

    def command(self) -> list[str]:
        """返回本 server 的启动命令行（端口在 start() 时才定，这里用占位符 `0`）。

        前置条件: 无（可在 start() 之前调用，用于留证/调试）。
        后置条件: 返回 argv 列表，首元素为可执行文件路径；端口段此时为 `0`。
        禁止: 用它直接启动——`start()` 会插入真实端口，避免端口冲突。
        异常: 无。
        """
        return [
            str(self._binary),
            "-m",
            self._model_path,
            "-c",
            str(self._n_ctx),
            "-fitt",
            str(self._vram_margin_bytes // 1024**2),
            "--no-webui",
            "--host",
            self._host,
            "--port",
            "0",
            "-np",
            "1",
        ]

    @property
    def base_url(self) -> str:
        """返回已就绪 server 的根地址。

        前置条件: 已成功调用 start()。
        后置条件: 返回形如 `http://127.0.0.1:<port>` 的字符串。
        禁止: 在 start() 之前调用——端口尚未确定，返回地址没有意义。
        异常: RuntimeError —— 尚未 start()。
        """
        if self._port is None:
            raise RuntimeError("server 尚未启动：base_url 不可用")
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        """启动 server 并等到 `/health` 就绪。

        前置条件: binary 指向可执行的 llama-server；model_path 可读。
        后置条件: 进程已启动、`/health` 返回 200、base_url 可用；日志已写入 log_path（若给出）。
        禁止: 只发进程就算成功——必须等到就绪，否则第一批请求会撞上连接拒绝；
              也禁止在未就绪时静默继续。
        异常: ServerStartError —— 可执行文件不存在、进程提前退出、或超时未就绪；
              OSError —— log_path 不可写。
        """
        argv = self.command()
        port = _free_port()
        argv[argv.index("--port") + 1] = str(port)
        self._port = port
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self._log_path.open("w", encoding="utf-8")
        try:
            self._process = subprocess.Popen(
                argv,
                stdout=self._log_handle if self._log_handle is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_log()
            self._port = None
            raise ServerStartError(f"无法启动 llama-server {argv[0]}: {exc}") from exc

        deadline = time.perf_counter() + self._startup_timeout_s
        url = self.base_url + "/health"
        while time.perf_counter() < deadline:
            if self._process.poll() is not None:
                raise ServerStartError(
                    f"llama-server 提前退出（码 {self._process.returncode}）：{self._log_tail()}"
                )
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(_POLL_SECONDS)
        raise ServerStartError(f"llama-server 在 {self._startup_timeout_s:.0f}s 内未就绪：{self._log_tail()}")

    def stop(self) -> None:
        """按进程组终止 server（幂等）。

        前置条件: 无（未启动时是空操作）。
        后置条件: server 及其同组子进程都已退出，日志句柄已关闭；重复调用无副作用。
        禁止: 只对组长发信号——llama 侧可能还有同组子进程，孤儿会继续占显存（F34）。
        异常: 无（进程已消失视为"无需再杀"）。
        """
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
        self._close_log()

    def _close_log(self) -> None:
        """关闭日志句柄（若已打开）。"""
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None

    def _log_tail(self, lines: int = 3) -> str:
        """读取日志末尾若干行，供失败原因使用；读不到时返回空串。"""
        if self._log_path is None or not self._log_path.is_file():
            return ""
        try:
            content = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return " ".join(line.strip() for line in content[-lines:])

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        """把信号发给子进程所在的整个进程组（组长已退出时退化为点对点发送）。"""
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                return


class ServerLogTail:
    """按字节偏移增量读取 `llama-server` 日志的新增**完整行**（题内进度的取数口）。

    存在的理由：EXAM 的问答请求是**非流式**的（不传 `stream`），响应返回前客户端手里没有任何
    题内事实；而 server 自己每约 3 秒往日志写一行 `slot print_timing`（n_gen / tg）。
    读这个文件既**不改请求结构**（同参数可比性不受影响），也不额外起进程（ADR-0026）。

    偏移一律用**字节**：日志是 UTF-8 中文与 ASCII 混排，按字符数回退偏移会在多字节字符上错位。
    """

    def __init__(self, path: Path | None) -> None:
        """绑定日志路径（None 表示本轮没有产物目录，此时一切读取都是空操作）。"""
        self._path = Path(path) if path is not None else None
        self._offset = 0

    def begin(self) -> None:
        """把读取位置对齐到当前文件末尾（一次请求开始前调用）。

        前置条件: 无（路径缺失/不可 stat 都允许）。
        后置条件: 下次 new_lines() 只返回此后写入的行；调用前已存在的行（上一题留下的）不会再被读到。
        禁止: 在请求发出之后才对齐——那会把上一题的统计行算进本题（"本题 N tok"会从别人的数字开始）。
        异常: 无（stat 失败按偏移 0 处理，最坏是重复读一遍旧行，不会丢数据）。
        """
        self._offset = 0
        if self._path is None:
            return
        try:
            self._offset = self._path.stat().st_size
        except OSError:
            self._offset = 0

    def new_lines(self) -> list[str]:
        """返回自上次调用以来新增的完整行。

        前置条件: 可反复调用；未 begin() 过时偏移为 0（即从头读）。
        后置条件: 只返回以换行符结尾的行；末尾那半行**不消费**（偏移不前进，下次连同续写内容一起读），
                   因此调用方永远拿不到被截断的行；无新增/文件不存在/不可读时返回空列表。
        禁止: 把半行当完整行返回（统计行被切开会解析失败，进而把"在生成"误报成"没有统计"）。
        异常: 无（OSError 视为"这次读不到"，由下一次心跳重试）。
        """
        if self._path is None or not self._path.is_file():
            return []
        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return []
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return []
        self._offset += cut + 1
        return chunk[: cut + 1].decode("utf-8", "replace").splitlines()


class ChatCompleter:
    """对 `llama-server` 的 `POST /v1/chat/completions` 发一次问答请求。

    只用 chat 端点：裸 `/completion` 不套 chat template，instruct 模型不吐 EOS，
    会一路吃到上下文满，与 ADR-0020 的"完成条件=模型写完"冲突（ADR-0025 的 F29）。
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        host, _, port = base_url.split("//", 1)[-1].partition(":")
        self._host = host
        self._port = int(port)

    def complete(
        self,
        prompt: str,
        *,
        n_predict: int,
        temperature: float,
        seed: int,
        timeout_s: float,
        cancel_check: Callable[[], bool] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> Completion:
        """发一次独立请求并返回结果。

        前置条件: prompt 为本题完整 prompt；timeout_s 为本次请求的墙钟上限。
        后置条件: 返回 Completion（正文、停止原因、token 数与吞吐取自 server 的 JSON）；
                  请求体只带这一条 user 消息，且 `cache_prompt=false`——每题上下文互不继承；
                  请求在途时每 `_HEARTBEAT_SECONDS` 调用一次 on_heartbeat（供调用方刷题内进度）。
        禁止: 携带历史消息或复用 KV 前缀——"每题清空上下文"必须由请求结构保证，而不是靠运气；
              也不要在超时后静默返回半截答案；更**不要**为了让进度可见而打开流式——
              那会改掉请求结构本身（ADR-0026 的取舍）。
        异常: CompletionCancelled —— cancel_check() 为真（连接被关闭，server 会自取消）；
              CompletionTimeout —— 超过 timeout_s；CompletionError —— 连接/HTTP/解析失败。
        """
        payload = json.dumps(
            {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": n_predict,
                "temperature": temperature,
                "seed": seed,
                "cache_prompt": False,
            }
        ).encode("utf-8")

        outcome: dict[str, object] = {}
        connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout_s)

        def worker() -> None:
            try:
                connection.request(
                    "POST", "/v1/chat/completions", body=payload, headers={"Content-Type": "application/json"}
                )
                response = connection.getresponse()
                raw = response.read()
                if response.status != 200:
                    outcome["error"] = CompletionError(
                        f"server 返回 {response.status}: {raw[:200].decode('utf-8', 'replace')}"
                    )
                    return
                outcome["json"] = json.loads(raw.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - 统一转成完成异常，由调用方归类
                outcome["error"] = exc

        started = time.perf_counter()
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        next_heartbeat = started + _HEARTBEAT_SECONDS
        while thread.is_alive():
            if cancel_check is not None and cancel_check():
                connection.close()  # 关连接 → server 自取消该次生成（F31）
                thread.join(timeout=_CANCEL_GRACE_SECONDS)
                raise CompletionCancelled("用户取消")
            thread.join(timeout=_POLL_SECONDS)
            now = time.perf_counter()
            # 心跳与取消轮询共用这个循环：不需要第二条线程，也不会把请求变成流式。
            if on_heartbeat is not None and now >= next_heartbeat:
                next_heartbeat = now + _HEARTBEAT_SECONDS
                self._beat(on_heartbeat)
        wall_seconds = time.perf_counter() - started

        error = outcome.get("error")
        if error is not None:
            if isinstance(error, (socket.timeout, TimeoutError)):
                raise CompletionTimeout(f"单题超过 {timeout_s:.0f}s: {error!r}") from error
            if isinstance(error, CompletionError):
                raise error
            raise CompletionError(f"请求失败: {error!r}") from error

        body = outcome.get("json")
        if not isinstance(body, dict):
            raise CompletionError("server 响应不是 JSON 对象")
        choices = body.get("choices") or [{}]
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        timings = body.get("timings") or {}
        # llama-server 的 `reasoning-format=deepseek`（默认）把思考链放在 reasoning_content；
        # 部分构建也可能用 reasoning。两种都接受，非字符串一律当没有，避免把 "None" 写进答卷。
        # content 同理：上下文打满时它可能是 null 而不是 ""，不能 str(None) 成 "None"。
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = message.get("reasoning")
        if not isinstance(reasoning, str):
            reasoning = ""
        return Completion(
            text=text,
            stop_reason=str(choice.get("finish_reason") or body.get("stop_type") or ""),
            prompt_n=int(timings.get("prompt_n", 0)),
            predicted_n=int(timings.get("predicted_n", 0)),
            prompt_ts=float(timings.get("prompt_per_second", 0.0)),
            generation_ts=float(timings.get("predicted_per_second", 0.0)),
            wall_seconds=wall_seconds,
            reasoning=reasoning,
        )

    @staticmethod
    def _beat(callback: Callable[[], None]) -> None:
        """调用一次心跳回调，吞掉它抛出的一切异常。

        与 SubprocessRunner._pump 对行回调的口径一致：进度是"尽力而为"的旁路，
        它出错绝不能把正在进行的测量带崩（否则"看得见"会以"测不成"为代价）。
        """
        try:
            callback()
        except Exception:  # noqa: BLE001 - 进度回调不影响取数
            pass
