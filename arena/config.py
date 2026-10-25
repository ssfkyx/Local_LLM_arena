"""Local Arena — 配置层（唯一允许写死本机环境的地方）。

模块名: arena.config
职责: 集中持有可执行文件位置、内容目录、产物目录、主题文件、显存预算、负载档名与三类测试的默认参数；
      对外只暴露"路径 + 默认参数 + 预算 + 档名"，不认识任何领域对象。
依赖: 标准库 dataclasses/pathlib/os；arena.settings（持久化用户偏好）、
      arena.vocabulary（负载档名是跨模块共用的"名字"）。
被依赖: run_arena.py、arena.runners.*、arena.session、arena.ui.controller。
相关 ADR: docs/decisions/ADR-0013-config-as-single-source.md、
          docs/decisions/ADR-0014-headless-and-quick-run-modes.md、
          docs/decisions/ADR-0015-vram-budget-excludes-oversized-models.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md、
          docs/decisions/ADR-0020-completion-condition-instead-of-token-cap.md、
          docs/decisions/ADR-0025-per-question-exam.md、
          docs/decisions/ADR-0027-persisted-model-directory.md、
          docs/decisions/ADR-0028-persisted-llama-bin-directory.md
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from arena.settings import (
    SETTINGS_DIR_NAME,
    SETTINGS_FILENAME,
    SettingsError,
    SettingsStore,
    UserSettings,
)
from arena.vocabulary import PROFILE_QUICK, PROFILE_REAL, PROFILES

# 三类测试各自调用的可执行文件；选择目录时用它做完整性校验（ADR-0028）。
# 顺序即报错文案的顺序，也是 README「环境要求」列出的顺序。
LLAMA_TOOLS: tuple[str, ...] = ("llama-bench", "llama-perplexity", "llama-server")


@dataclass(frozen=True)
class BenchDefaults:
    """llama-bench 的默认负载。

    reps >= 3 是硬要求：单次采样在笔记本功耗墙下漂移过大，跨模型对比会失真。
    """

    n_prompt: int = 512
    n_gen: int = 128
    n_depth: int = 0
    reps: int = 3


@dataclass(frozen=True)
class PerplexityDefaults:
    """llama-perplexity 的默认负载。

    完成条件是**完整语料**，不是固定 chunk 数：chunks=-1 是工具原生语义（`--chunks -1` = all，
    见 tools/perplexity/perplexity.cpp:493,515）。工具会在 stderr 自报
    `calculating perplexity over N chunks`，那个 N 就是全语料的 chunk 数，
    并随记录落盘（PerplexityResult.n_chunks）——因此"跑完了没有"是可核验的事实。
    跑不完由 TrialTimeouts.perplexity 兜底（超时即如实记失败，见 ADR-0020、ADR-0017）。
    """

    corpus_name: str = "wiki.test.raw"
    n_ctx: int = 512
    # -1 = 全部语料（不再用固定 chunk 数当完成条件）。
    chunks: int = -1


@dataclass(frozen=True)
class ExamDefaults:
    """llama-cli 答卷的默认负载。

    完成条件是**模型自己把答案写完**：n_predict=-1 是工具原生语义（"infinity"，
    生成到 EOS 或上下文满为止），不再用固定 token 数把答卷截断——
    截断后比的就不是"最终答案质量"，而是"前 1024 个 token 的质量"（ADR-0020）。
    占显存的时间由 TrialTimeouts.exam 兜底（每份试卷各一次，超时如实记失败，见 ADR-0017）。
    temperature=0.0 与固定 seed 是为了让"同一模型同一试卷"的答卷可复现，
    否则跨模型对比会掺入采样噪声。
    """

    # -1 = 不限（生成到 EOS 或上下文满）；不再用 1024 这类 token 上限当完成条件。
    n_predict: int = -1
    n_ctx: int = 16384
    temperature: float = 0.0
    seed: int = 42
    prompt_suffix: str = "\n\n---\n请现在开始作答，按各题要求输出。"


@dataclass(frozen=True)
class TrialTimeouts:
    """墙钟上限（秒）；超时按失败处理而不是无限等。

    语义按 Trial 种类分开，别互相套用：
    - bench / perplexity：**整个子进程**的上限（各自只有一个子进程）。
    - exam：**每一道题**各一个上限（ExamRunner 逐题计时），不是整卷/整轮 Exam 的上限。
    - server_startup：llama-server 从启动到 `/health` 就绪的上限（EXAM 专用，ADR-0025）。
    这里的值是"退出保护"而不是完成条件——完成条件分别是"跑完整语料"与"模型自己写完"（ADR-0020）。
    """

    bench: float = 3600.0
    # 完整语料没有固定 token 数上限，只能靠墙钟兜底：60 分钟内跑不完就如实记失败。
    perplexity: float = 3600.0
    # 每道题 1 小时：模型可以写到自己停，但不能无限占着显存（ADR-0025 把计时单位从"卷"改成"题"）。
    exam: float = 3600.0
    # 模型加载 + 就绪轮询：大模型在 WSL2 下可能要一两分钟，给足余量。
    server_startup: float = 180.0


@dataclass(frozen=True)
class AppConfig:
    """一次进程生命周期内的全部环境事实。"""

    root: Path
    llama_bin_dir: Path
    models_dir: Path
    content_dir: Path
    runs_dir: Path
    tracer_dir: Path
    theme_css: Path
    # 用户配置文件路径（XDG 配置目录）；GUI 的"模型文件夹"写入这里。
    settings_path: Path
    # 显存余量：直接映射到三种工具的 -fitt/--fit-target（单位字节）。
    # 语义是"上卡后至少留出这么多显存"，工具据此自动决定哪些层放 GPU、哪些留 CPU；
    # 因此文件大于显存的模型不会被静态排除，而是自动部分卸载（ADR-0019 取代 ADR-0015 的硬排除）。
    # 默认 1 GiB：给桌面与其它进程留出余量，避免在 WSL2 下抢显存。
    vram_margin_bytes: int = 1024 * 1024**2
    # 负载档名（real / quick）：它唯一决定下面三组负载参数用哪一套，也是跨模型对比的分组键。
    # 只有 official（real）档的记录进入对比表；quick 仅供自检（ADR-0018）。
    profile: str = PROFILE_REAL
    bench: BenchDefaults = BenchDefaults()
    perplexity: PerplexityDefaults = PerplexityDefaults()
    exam: ExamDefaults = ExamDefaults()
    timeouts: TrialTimeouts = TrialTimeouts()
    # 启动期配置读取告警（损坏/不可读）。为空表示没有告警；GUI 状态行会显示它，
    # headless 入口会打到 stderr。它不是环境事实，所以不参与任何业务判断。
    settings_warning: str = ""

    def __post_init__(self) -> None:
        """构造期校验负载档名与显存余量。

        前置条件: 无。
        后置条件: profile 属于 PROFILES、vram_margin_bytes 非负时原样返回。
        禁止: 在此处做路径存在性校验——启动期不校验路径是 ADR-0013 决策 6。
        异常: ValueError —— profile 不在白名单内（profile 是对比键，不能是自由文本），
              或 vram_margin_bytes 为负（负余量没有意义，只会把风险藏起来）。
        """
        if self.profile not in PROFILES:
            raise ValueError(f"未知负载档: {self.profile!r}（只允许 {sorted(PROFILES)}）")
        if self.vram_margin_bytes < 0:
            raise ValueError(f"vram_margin_bytes 不能为负: {self.vram_margin_bytes}")

    @classmethod
    def from_env(cls, root: Path | None = None) -> AppConfig:
        """从环境变量、用户配置与本机默认值装配配置。

        前置条件: 无；环境变量与配置文件均可缺失。
        后置条件: 返回的路径字段可能指向不存在的路径（不在此处校验，避免启动即崩）；
                   是否可用由 binary() 与各使用点按需校验；vram_margin_bytes 可被
                   ARENA_VRAM_MARGIN_BYTES 覆盖（换机器/换策略时只改环境，不改代码）；
                   models_dir 的优先级是 ARENA_MODELS_DIR > 配置文件 models_dir > ~/models；
                   llama_bin_dir 的优先级是 ARENA_LLAMA_BIN_DIR > 配置文件 llama_bin_dir
                   > ~/llama.cpp/build/bin；settings_path 可被 ARENA_SETTINGS_PATH 覆盖，默认是
                   $XDG_CONFIG_HOME/local-arena/config.json（未设置 XDG_CONFIG_HOME 时
                   ~/.config/local-arena/config.json）；配置文件损坏时回退默认值并写入
                   settings_warning；profile 恒为 PROFILE_REAL —— 这是"满载档"。
        禁止: 在本方法之外散落 os.environ 读取或绝对路径字面量；也禁止在负载参数上开环境变量口子
              （参数一旦可被外部改写，"同负载才可比"就失去保障，见 ADR-0018）。
        异常: ValueError —— ARENA_VRAM_MARGIN_BYTES 不是整数（宁可启动即报错，也不静默用默认值）。
        """
        arena_root = Path(os.environ.get("ARENA_ROOT", "")).expanduser() if os.environ.get("ARENA_ROOT") else None
        resolved_root = (root or arena_root or Path(__file__).resolve().parent.parent).resolve()
        home = Path.home()

        def pick(env_name: str, default: Path) -> Path:
            raw = os.environ.get(env_name)
            return Path(raw).expanduser().resolve() if raw else default

        def pick_saved(env_name: str, saved: Path | None, default: Path) -> Path:
            """环境变量 > 已保存偏好 > 默认值（ADR-0027 / ADR-0028 的同一套优先级）。"""
            raw = os.environ.get(env_name)
            if raw:
                return Path(raw).expanduser().resolve()
            if saved is not None:
                return saved.expanduser().resolve()
            return default

        def pick_int(env_name: str, default: int) -> int:
            raw = os.environ.get(env_name)
            if not raw:
                return default
            return int(raw)

        # 配置文件默认放在 XDG 配置目录：它属于"某个用户"，不属于某个克隆。
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        settings_base = Path(xdg_config).expanduser() if xdg_config else home / ".config"
        settings_path = pick(
            "ARENA_SETTINGS_PATH", settings_base / SETTINGS_DIR_NAME / SETTINGS_FILENAME
        )
        settings = UserSettings()
        settings_warning = ""
        try:
            settings = SettingsStore(settings_path).load()
        except SettingsError as exc:
            # 配置损坏不能阻止启动：ADR-0013 决策 6 要求 GUI 能启动并把问题说清楚。
            settings_warning = f"配置文件不可用，已回退到默认目录: {exc}"

        return cls(
            root=resolved_root,
            llama_bin_dir=pick_saved(
                "ARENA_LLAMA_BIN_DIR", settings.llama_bin_dir, home / "llama.cpp" / "build" / "bin"
            ),
            models_dir=pick_saved("ARENA_MODELS_DIR", settings.models_dir, home / "models"),
            content_dir=pick("ARENA_CONTENT_DIR", resolved_root / "test_content"),
            runs_dir=pick("ARENA_RUNS_DIR", resolved_root / "runs"),
            tracer_dir=resolved_root / "docs" / "tracer",
            theme_css=resolved_root / "arena" / "theme" / "arena.css",
            settings_path=settings_path,
            vram_margin_bytes=pick_int("ARENA_VRAM_MARGIN_BYTES", cls.vram_margin_bytes),
            profile=PROFILE_REAL,
            settings_warning=settings_warning,
        )

    @classmethod
    def quick(cls, root: Path | None = None) -> AppConfig:
        """装配一套"能在数十秒内跑完"的极小负载配置。

        前置条件: 无。
        后置条件: 除三类负载参数与 profile 外与 from_env() 完全一致；profile 被显式标成 quick，
                   因此本档产生的记录会被对比表结构性排除（ADR-0018），不需要读者去认数字。
        禁止: 用本配置产出的数字做模型对比——样本太小，无可比性。
        异常: 无。
        """
        base = cls.from_env(root)
        return replace(
            base,
            profile=PROFILE_QUICK,
            bench=BenchDefaults(n_prompt=64, n_gen=16, reps=1),
            # quick 是数十秒级自检，故意保留小负载：chunks=2 与两处 -1（不限）不是一回事，
            # 它不承载 ADR-0020 的"完成条件"语义，只是样本小到不可用于对比（ADR-0018）。
            perplexity=PerplexityDefaults(corpus_name=base.perplexity.corpus_name, n_ctx=512, chunks=2),
            exam=replace(base.exam, n_predict=64, n_ctx=8192),
        )

    def binary(self, tool: str) -> Path:
        """定位 llama.cpp 工具的可执行文件。

        前置条件: tool 为工具名（如 "llama-bench"）。
        后置条件: 返回存在的可执行文件绝对路径。
        禁止: 静默回退到 PATH 查找——测试结果必须可追溯到确定的那份 llama.cpp 构建。
        异常: FileNotFoundError —— 文件不存在或不可执行。
        """
        path = self.llama_bin_dir / tool
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FileNotFoundError(f"找不到可执行的 llama 工具: {path}")
        return path

    @staticmethod
    def missing_tools(bin_dir: Path) -> list[str]:
        """列出候选目录里缺失或不可执行的 llama 工具。

        前置条件: bin_dir 为候选目录（不要求存在）。
        后置条件: 返回 LLAMA_TOOLS 中缺失或不可执行的名字（顺序与 LLAMA_TOOLS 一致）；
                   空列表表示整份构建就绪。
        禁止: 用 PATH 兜底——"选了哪个目录"必须就是"实际用哪个构建"（ADR-0028）；
              也不在此处抛异常：调用方要用同一份结果生成一行可读文案。
        异常: 无。
        """
        missing: list[str] = []
        for tool in LLAMA_TOOLS:
            candidate = bin_dir / tool
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                missing.append(tool)
        return missing

    @property
    def papers_dir(self) -> Path:
        """试卷目录（test_content/real_test）。

        前置条件: 无。
        后置条件: 返回 content_dir/real_test；不校验存在性。
        禁止: 在此处做存在性校验——"目录不存在"由 paper_paths() 用空列表表达。
        异常: 无。
        """
        return self.content_dir / "real_test"

    @property
    def speed_dir(self) -> Path:
        """语料目录（test_content/speed）。

        前置条件: 无。
        后置条件: 返回 content_dir/speed；不校验存在性。
        禁止: 在此处做存在性校验——"语料缺失"由 corpus_path() 抛 FileNotFoundError。
        异常: 无。
        """
        return self.content_dir / "speed"

    def corpus_path(self, name: str | None = None) -> Path:
        """返回 Perplexity 语料文件的路径。

        前置条件: name 缺省时用 perplexity.corpus_name。
        后置条件: 返回路径；不校验存在性。
        禁止: 无。
        异常: FileNotFoundError —— 语料文件不存在（Perplexity 无输入时必然失败，早报错）。
        """
        target = self.speed_dir / (name or self.perplexity.corpus_name)
        if not target.is_file():
            raise FileNotFoundError(f"找不到语料文件: {target}")
        return target

    def paper_paths(self) -> list[Path]:
        """列出全部试卷文件，按文件名排序。

        前置条件: 无。
        后置条件: 返回排序后的 paper_* 文件列表；目录不存在时返回空列表。
        禁止: 无。
        异常: 无。
        """
        if not self.papers_dir.is_dir():
            return []
        return sorted(path for path in self.papers_dir.glob("paper_*") if path.is_file())

    def ensure_runtime_dirs(self) -> None:
        """创建运行期必须存在的目录。

        前置条件: 无。
        后置条件: runs_dir 存在；tracer_dir 存在。
        禁止: 无。
        异常: OSError —— 无写权限时抛出（启动阶段早失败优于运行中途失败）。
        """
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.tracer_dir.mkdir(parents=True, exist_ok=True)
