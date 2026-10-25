#!/usr/bin/env python3
"""Local Arena — 唯一入口（装配 + 启动）。

模块名: run_arena
职责: 解析命令行、按配置把各层接起来、选择 GUI 或 headless 运行方式；
      本文件不做任何测试逻辑，也不做任何解析。
依赖: 标准库 argparse/signal/sys/threading；arena.config/domain/history/process/runners/session/sink/store；
      GTK 仅在 GUI 模式下按需导入（headless 自检不应被图形环境绑住）。
被依赖: 用户、tools/check_traceability.py（作为被扫描的源文件）。
相关 ADR: docs/decisions/ADR-0013-config-as-single-source.md、
          docs/decisions/ADR-0014-headless-and-quick-run-modes.md、
          docs/decisions/ADR-0015-vram-budget-excludes-oversized-models.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md、
          docs/decisions/ADR-0027-persisted-model-directory.md
"""

from __future__ import annotations

import argparse
import atexit
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from arena.config import AppConfig
from arena.domain import Model, ModelCatalog, Scoreboard
from arena.history import Archive, ComparisonBuilder
from arena.process import SubprocessRunner
from arena.runners import build_runners
from arena.session import ArenaSession
from arena.sink import ProgressSink
from arena.store import JsonlResultStore, RunArtifacts
from arena.vocabulary import HISTORY_FILENAME, OFFICIAL_PROFILE, Stage, TestKind


class ConsoleSink(ProgressSink):
    """把进度打到标准输出的进度汇（headless 用）。

    Ctrl-C 就是 headless 的"取消"：信号处理函数置位本对象里的标志，Runner 轮询到后终止子进程，
    已完成的 Trial 记录照常落盘。之所以不靠默认的 SIGINT 传播，是因为子进程被放进了独立进程组
    （ADR-0002 的 2026-10-17 修订），终端信号不会再自动落到它们身上。
    """

    def __init__(self, stream=sys.stdout) -> None:
        self._stream = stream
        self._cancelled = threading.Event()

    def on_stage(self, kind: TestKind, stage: Stage) -> None:
        """打印阶段变化。

        前置条件: 无（可由 worker 线程调用）。
        后置条件: 一行 `[KIND] STAGE` 已带 flush 写出。
        禁止: 在此累积状态或做测试判断——它只负责显示。
        异常: OSError —— 输出流不可写（如管道被关闭）。
        """
        print(f"[{kind.value}] {stage.value}", file=self._stream, flush=True)

    def on_progress(self, kind: TestKind, fraction: float | None, message: str) -> None:
        """打印进度消息（无消息时不打印，避免刷屏）。

        前置条件: fraction 为 0.0~1.0 或 None。
        后置条件: message 非空时输出一行；为空时静默（阶段变化已足够表达"在动"）。
        禁止: 把 fraction 也打出来充数——perplexity 的 chunk 计数已在 message 里。
        异常: OSError —— 输出流不可写。
        """
        if message:
            print(f"  {kind.value}: {message}", file=self._stream, flush=True)

    def is_cancelled(self) -> bool:
        """查询是否收到了 Ctrl-C（headless 的取消入口）。

        前置条件: 无（可从任意线程调用）。
        后置条件: 收到过 SIGINT 时为 True，否则 False。
        禁止: 返回一次性结果——Runner 会反复轮询。
        异常: 无。
        """
        return self._cancelled.is_set()

    def request_cancel(self) -> None:
        """置位取消标志（由 SIGINT 信号处理函数调用）。

        前置条件: 无。
        后置条件: 此后 is_cancelled() 恒为 True；当前子进程会在一个轮询周期内被终止。
        禁止: 在此直接杀进程或抛异常——信号处理函数只做"置位"这种最小动作。
        异常: 无。
        """
        self._cancelled.set()


def build_session(config: AppConfig) -> ArenaSession:
    """按配置装配一轮 Run 所需的一切，并登记当前评估协议。

    前置条件: config 已就绪（目录可创建）。
    后置条件: 返回可执行 run() 的 ArenaSession；runs 目录下已存在当前协议的 PROTOCOL 标记
               （协议名未变时不会刷新既有生效时刻）。
    禁止: 在此处做参数校验以外的业务判断；也禁止顺手归档/删除历史——重置历史是人的动作（ADR-0021）。
    异常: OSError —— 运行目录不可创建或标记不可写。
    """
    config.ensure_runtime_dirs()
    store = JsonlResultStore(config.runs_dir / HISTORY_FILENAME)
    artifacts = RunArtifacts(config.runs_dir)
    runners = build_runners(config, SubprocessRunner(), artifacts)
    session = ArenaSession(config, runners, store, Archive(store))
    session.register_protocol()
    return session


def format_scoreboard(scoreboard: Scoreboard) -> str:
    """把记分板渲染成终端文本。

    前置条件: scoreboard 来自一轮 Run 或历史重建。
    后置条件: 返回多行文本；缺测指标显示为 "—"；第二行写出该组数字的负载档
               （无来源可标时显示 "—"）。
    禁止: 用 0 代替缺失值（0 是"测出来为零"，不是"没测"）；也禁止省略负载档——
          不写档位，quick 自检的数字看起来和正式成绩一模一样。
    异常: 无。
    """

    def number(value: float | None, digits: int) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    lines = [
        f"  model      : {scoreboard.model.display_name}",
        f"  profile    : {scoreboard.profile or '—'}",
        f"  run_id     : {scoreboard.run_id}",
        f"  bench      : pp {number(scoreboard.bench_pp_ts, 1)} t/s · tg {number(scoreboard.bench_tg_ts, 1)} t/s",
        f"  perplexity : {number(scoreboard.perplexity, 4)} ± {number(scoreboard.perplexity_stderr, 4)}",
        f"  answers    : {len(scoreboard.answer_paths)} 份",
        f"  wall clock : {scoreboard.wall_seconds:.1f} s",
    ]
    for path in scoreboard.answer_paths:
        lines.append(f"    - {path}")
    for failure in scoreboard.failures:
        lines.append(f"  failure    : {failure}")
    return "\n".join(lines)


def format_comparison(session: ArenaSession) -> str:
    """把跨模型对比渲染成终端表格。

    前置条件: 无（历史为空时返回只有表头的表）。
    后置条件: 返回等宽文本表，首行写明本表所属负载档；缺测列显示为 "—"；
               历史里存在旧评估协议记录时，在负载档行之后插入一行告警（ADR-0021）。
    禁止: 对缺失值排序或补零——排名只在有数字的行之间成立；
          也禁止在同一张表里并列不同负载的数字——那正是"最新值取胜"导致 quick 顶掉真实档的老路。
    异常: OSError —— 历史文件或协议标记不可读。
    """
    rows = ComparisonBuilder.build(session.history())
    header = f"{'模型':<44} {'pp t/s':>9} {'tg t/s':>9} {'PPL':>9}  {'最近运行':<20}"
    lines = [
        f"负载档: {OFFICIAL_PROFILE}（只有本档的数字参与对比；其他档的自检记录见 HISTORY）",
        header,
        "-" * len(header),
    ]
    warning = session.protocol_warning()
    if warning:
        lines.insert(1, warning)

    def number(value: float | None, digits: int) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    for row in rows:
        lines.append(
            f"{row.model_name[:44]:<44} {number(row.bench_pp_ts, 1):>9} {number(row.bench_tg_ts, 1):>9} "
            f"{number(row.perplexity, 4):>9}  {row.last_run_at:<20}"
        )
    return "\n".join(lines)


def selected_model(config: AppConfig, names: list[str]) -> Model:
    """解析出本轮唯一要测试的模型。

    前置条件: names 为命令行 `--model` 的取值列表（可为空，但空即错误）。
    后置条件: 返回唯一一个匹配到的 Model（无论文件多大）。
    禁止: 在未点名时"随便挑一个"或"全都跑"——一次只跑一个模型是规避显存释放问题的纪律
          （ADR-0014 修订），替用户决定既隐蔽又危险；也禁止按文件大小提前排除模型
          ——超过显存只意味着要部分卸载，上卡层数由 llama.cpp 的 fit 决定（ADR-0019）。
    异常: ValueError —— 未指定模型、或指定了多个；KeyError —— 名字无法匹配任何文件。
    """
    if len(names) > 1:
        raise ValueError(f"一次只能跑一个模型，收到 {len(names)} 个: {'、'.join(names)}")
    if not names:
        raise ValueError("请用 --model 指定一个模型（--list-models 可查看清单）")
    catalog = ModelCatalog(config.models_dir)
    return catalog.find(names[0])


def run_headless(config: AppConfig, names: list[str]) -> int:
    """在无界面模式下跑**一个**模型的完整计划。

    前置条件: config 与 names 已解析（names 里恰好一个模型名）。
    后置条件: 该模型的三类 Trial 依次执行，逐条落盘历史与产物；随后打印记分板与
               official 档的对比表；收到 SIGINT/SIGTERM/SIGHUP（Ctrl-C、被 kill、终端被关）时
               一律转成"取消"：终止当前子进程、保留已完成记录、退出码为 1；
               非 official 档运行时额外打印一行"本轮不进对比表"的说明。
    禁止: 在一个进程里连续跑多个模型——实测（F14）前一个模型释放显存不及时会让后一个模型
          整轮加载失败，因此拆成用户逐个发起（ADR-0014 修订）；
          也禁止用 quick 模式的数字做模型对比结论（样本太小，见 ADR-0014）。
    异常: 无（测试失败会体现在记录与退出码里，而不是异常）。
    """
    model = selected_model(config, names)
    session = build_session(config)
    sink = ConsoleSink()
    # 三种"人要它停"的方式走同一条路：置位取消标志，由 Runner 终止整个进程组并落失败记录。
    # 不注册 SIGHUP 的话，关掉终端会让 Python 直接退出、子进程（独立会话）继续占着显存（ADR-0021）。
    for signal_name in ("SIGINT", "SIGTERM", "SIGHUP"):
        number = getattr(signal, signal_name, None)
        if number is not None:
            signal.signal(number, lambda _signum, _frame: sink.request_cancel())

    print(f"=== {model.display_name} ({model.quantization}) · {config.profile} 档 ===", flush=True)
    plan = session.default_plan(model)
    scoreboard = session.run(plan, sink)
    print(format_scoreboard(scoreboard), flush=True)
    if config.profile != OFFICIAL_PROFILE:
        print(
            f"note: 本轮是 {config.profile} 档自检，记录已落盘但不进入跨模型对比（ADR-0018）。",
            flush=True,
        )
    print(flush=True)
    print("=== COMPARE ===")
    print(format_comparison(session))
    return 1 if scoreboard.failures else 0


def run_gui(config: AppConfig) -> int:
    """启动图形界面。

    前置条件: 有可用显示（WSLg 下 DISPLAY 或 WAYLAND_DISPLAY 至少一个存在）。
    后置条件: 用户关闭窗口后返回 0；关窗前先取消并等在跑的子进程退出（见 controller.shutdown），
               并注册 atexit 兜底；主题解析失败时抛 ThemeError。
    禁止: 在 headless 场景调用（需要 GTK 与显示）；
          也禁止跳过收尾直接退出——那会留下仍在占显存的孤儿进程（ADR-0021）。
    异常: arena.ui.theme.ThemeError —— 主题文件缺失或 CSS 解析失败。
    """
    from arena.ui.controller import build_controller
    from arena.ui.theme import ArenaTheme
    from arena.ui.window import ArenaWindow

    ArenaTheme(config.theme_css).apply()
    session = build_session(config)
    window = ArenaWindow()
    controller = build_controller(config, session, window)
    # 退出钩子兜底：关窗路径（window delete-event）已经收尾，这里覆盖"主循环因其它原因结束"的残余路径。
    atexit.register(controller.shutdown)
    return window.launch()


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    前置条件: 无。
    后置条件: 返回覆盖 GUI / headless / compare / list-models 四种入口的解析器。
    禁止: 在此处读环境变量或校验路径——参数默认值归 AppConfig 管（ADR-0013）。
    异常: 无。
    """
    parser = argparse.ArgumentParser(prog="run_arena", description="Local Arena · 本地大模型测试")
    parser.add_argument("--headless", action="store_true", help="不开窗口，直接跑测试并打印结果")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="要测试的模型名（headless 下必须且只能给一个；--list-models 可查看清单）",
    )
    parser.add_argument("--quick", action="store_true", help="使用极小负载自检（数字不参与跨模型对比）")
    parser.add_argument("--compare", action="store_true", help="只打印历史对比表后退出")
    parser.add_argument("--list-models", action="store_true", help="列出模型目录里的模型后退出")
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=None,
        help="本次运行覆盖模型目录（不写入用户配置；--list-models 也适用）",
    )
    parser.add_argument("--runs-dir", type=Path, default=None, help="覆盖产物目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口。

    前置条件: argv 为命令行参数（None 时取 sys.argv[1:]）。
    后置条件: 返回进程退出码：0 成功；1 有 Trial 失败；2 参数或环境错误。
    禁止: 在参数错误时抛未捕获异常（必须给出可读信息与退出码）。
    异常: 无（内部异常被转成退出码 2 与 stderr 信息）。
    """
    args = build_parser().parse_args(argv)
    config = AppConfig.quick() if args.quick else AppConfig.from_env()
    if args.models_dir:
        config = replace(config, models_dir=args.models_dir.expanduser().resolve())
    if args.runs_dir:
        config = replace(config, runs_dir=args.runs_dir.expanduser().resolve())
    # 配置损坏时 from_env() 已经回退到默认值；这里只负责把原因送到 stderr。
    # GUI 路径会把它显示在状态行，headless 路径没有状态行，所以用 warning 行。
    if config.settings_warning:
        print(f"warning: {config.settings_warning}", file=sys.stderr)

    try:
        if args.list_models:
            catalog = ModelCatalog(config.models_dir)
            for model in catalog.list_models():
                size_mib = model.size_bytes / 1024**2
                print(f"{model.display_name}\t{model.quantization}\t{size_mib:.0f} MiB\t{model.path}")
            return 0
        if args.compare:
            print(format_comparison(build_session(config)))
            return 0
        if args.headless:
            return run_headless(config, args.model)
        return run_gui(config)
    except (FileNotFoundError, KeyError, ValueError, RuntimeError, OSError) as exc:
        # KeyError 的 str() 会带一层引号（未找到模型: x → '未找到模型: x'），报错文案不该有这层噪声。
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
