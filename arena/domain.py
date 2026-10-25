"""Local Arena — 领域模型（数据、取值、序列化；不含 I/O 与流程）。

模块名: arena.domain
职责: 定义 Ubiquitous Language 里的名词：Model/ModelCatalog、Paper/Corpus、
      Bench/Perplexity/Exam 的结果与记录、Scoreboard、ComparisonRow、TestPlan，
      以及它们在 JSONL 历史中的稳定文本表示。
依赖: 标准库 dataclasses/re/enum/pathlib；arena.vocabulary（枚举名字）。
被依赖: arena.runners、arena.store、arena.history、arena.session、arena.ui.*。
相关 ADR: docs/decisions/ADR-0005-jsonl-archive.md、
          docs/decisions/ADR-0010-comparison-across-models.md、
          docs/decisions/ADR-0011-answer-sheet-reports-throughput.md、
          docs/decisions/ADR-0006-fp64-arithmetic.md、
          docs/decisions/ADR-0015-vram-budget-excludes-oversized-models.md、
          docs/decisions/ADR-0018-load-profiles-and-no-parameter-panel.md、
          docs/decisions/ADR-0019-tool-side-vram-fit-instead-of-static-exclusion.md、
          docs/decisions/ADR-0020-completion-condition-instead-of-token-cap.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from arena.vocabulary import OFFICIAL_PROFILE, TestKind

# ---------------------------------------------------------------------------
# Model 与 ModelCatalog
# ---------------------------------------------------------------------------

# 只认 llama.cpp 的量化命名习惯：IQ*/Q*_K*/Q*_0/BF16/F16/F32。
# 用词边界收尾，避免把 "Meta-Llama-3.1-8B" 里的 "3.1" 之类误判为量化名。
_QUANTIZATION_RE = re.compile(
    r"(IQ\d+_[A-Z0-9_]+|Q\d+_K_[A-Z_]+|Q\d+_\d+|Q\d+_K|Q\d+|BF16|F16|F32)",
    re.IGNORECASE,
)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Model:
    """一个本地 .gguf 模型文件。"""

    path: str
    display_name: str
    quantization: str
    size_bytes: int

    def slug(self) -> str:
        """返回可安全用于目录名/文件名的短标识。

        前置条件: 无。
        后置条件: 只含字母数字与 . _ -；长度不超过 64；空串时退化为 "model"。
        禁止: 无。
        异常: 无。
        """
        slug = _SLUG_RE.sub("-", self.display_name).strip("-._")
        return slug[:64] or "model"

    def fingerprint(self) -> str:
        """返回模型的稳定指纹。

        前置条件: 无。
        后置条件: 同一文件内容在改名后可能相同也可能不同；不同内容必然不同（含字节数）。
        禁止: 用 mtime 参与指纹——复制/同步会改变 mtime 却不改变模型。
        异常: 无。
        """
        return f"{self.display_name}#{self.size_bytes}"


class ModelCatalog:
    """本机可用 Model 的集合（扫描模型目录得到）。

    这里**不按文件大小做任何排除**：文件大于显存只意味着运行时要部分卸载，不意味着不能跑；
    上卡策略交给 llama.cpp 的显存适配（ADR-0019，取代 ADR-0015 的硬排除）。
    需要知道大小的调用方直接读 Model.size_bytes。
    """

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = Path(models_dir)

    def list_models(self) -> list[Model]:
        """扫描模型目录，返回全部模型。

        前置条件: 无。
        后置条件: 返回按 display_name 排序的 Model 列表；目录不存在时返回空列表；
                   忽略 `*:Zone.Identifier` 这类 Windows 旁车文件。
        禁止: 读取模型内容或加载模型——此处只做目录级元数据收集，必须毫秒级返回；
              也禁止任何隐藏——超显存模型现在同样可选（ADR-0019），但"列表里必须看得见"
              这条要求本身仍然成立（ADR-0015 的可见性部分未被取代）。
        异常: 无。
        """
        if not self._models_dir.is_dir():
            return []
        models = [
            self._describe(path)
            for path in self._models_dir.glob("*.gguf")
            if path.is_file() and ":" not in path.name
        ]
        return sorted(models, key=lambda model: model.display_name.lower())

    def find(self, name: str) -> Model:
        """按展示名或文件名查找单个模型。

        前置条件: name 非空。
        后置条件: 返回唯一匹配的 Model。
        禁止: 在多个匹配中"随便挑一个"——歧义必须暴露给调用方。
        异常: KeyError —— 无匹配；ValueError —— 匹配到多个。
         """
        if not name:
            raise KeyError("模型名不能为空")
        wanted = name.lower()
        matches = [
            model
            for model in self.list_models()
            if wanted in (model.display_name.lower(), Path(model.path).name.lower(), Path(model.path).stem.lower())
        ]
        if not matches:
            raise KeyError(f"未找到模型: {name}")
        if len(matches) > 1:
            raise ValueError(f"模型名 {name} 匹配到多个文件: {[m.path for m in matches]}")
        return matches[0]

    @staticmethod
    def _describe(path: Path) -> Model:
        """把文件路径描述成 Model（不打开文件）。"""
        stem = path.stem
        match = _QUANTIZATION_RE.search(stem)
        quantization = match.group(0).upper() if match else "UNKNOWN"
        # 量化名通常位于文件名尾部，剥掉它之后剩下的才是"模型叫什么"。
        display = stem[: match.start()].rstrip("-._ ") if match else stem
        return Model(
            path=str(path),
            display_name=display or stem,
            quantization=quantization,
            size_bytes=path.stat().st_size,
        )


# ---------------------------------------------------------------------------
# 输入内容
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paper:
    """一份试卷（题目文本）。"""

    name: str
    path: str
    text: str

    @classmethod
    def from_path(cls, path: Path) -> Paper:
        """从磁盘读入试卷。

        前置条件: path 指向可读的 UTF-8 文本文件。
        后置条件: Paper.text 为文件全文；name 取去掉后缀的文件名。
        禁止: 在领域层做题目切分——本阶段答卷是整卷一次问答（ADR-0009）。
        异常: OSError —— 读取失败；UnicodeDecodeError —— 非 UTF-8 编码。
        """
        return cls(name=path.stem, path=str(path), text=path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Corpus:
    """供 Perplexity 使用的一份语料。"""

    name: str
    path: str

    @classmethod
    def from_path(cls, path: Path) -> Corpus:
        """从磁盘定位语料。

        前置条件: path 存在。
        后置条件: Corpus 只记录路径，不把语料内容读进内存（wiki.train 有 10MB+）。
        禁止: 无。
        异常: 无。
        """
        return cls(name=path.name, path=str(path))


# ---------------------------------------------------------------------------
# Bench
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchCase:
    """llama-bench 的一次负载定义。

    三者一一对应 llama-bench 的 -p / -n / -d；n_prompt>0 得到 pp 值，n_gen>0 得到 tg 值。
    """

    n_prompt: int
    n_gen: int
    n_depth: int = 0

    def label(self) -> str:
        """返回人类可读的负载标签，如 "pp512/tg128"。

        前置条件: 无。
        后置条件: 只列出非零项；三项全零时返回 "empty"（不返回空串——空串在日志里看不出是哪种情况）。
        禁止: 用与 llama-bench 命令行不同的记法——这个标签会被直接读作参数。
        异常: 无。
        """
        parts = []
        if self.n_prompt:
            parts.append(f"pp{self.n_prompt}")
        if self.n_gen:
            parts.append(f"tg{self.n_gen}")
        if self.n_depth:
            parts.append(f"d{self.n_depth}")
        return "/".join(parts) or "empty"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写结构。

        前置条件: 无。
        后置条件: 返回值只含 str/int/float/bool/None 与容器，可被 json.dumps 直接写出。
        禁止: 省略字段——本结构与 from_dict 共同定义历史文件格式，两侧必须对称。
        异常: 无。
        """
        return {"n_prompt": self.n_prompt, "n_gen": self.n_gen, "n_depth": self.n_depth}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BenchCase:
        """从 JSON 结构还原。

        前置条件: payload 由同结构产出（允许旧记录缺 n_depth）。
        后置条件: 返回值字段为 int；数值语义与 to_dict() 对称。
        禁止: 对缺字段猜测填充——只容忍明确声明过的可选字段（n_depth）。
        异常: KeyError —— 必填字段缺失；ValueError/TypeError —— 值不可转为 int。
        """
        return cls(
            n_prompt=int(payload["n_prompt"]),
            n_gen=int(payload["n_gen"]),
            n_depth=int(payload.get("n_depth", 0)),
        )


@dataclass(frozen=True)
class BenchResult:
    """llama-bench 一条记录的已解析数值。"""

    case: BenchCase
    avg_ts: float
    stddev_ts: float
    n_gpu_layers: int
    backend: str
    build_commit: str
    test_time: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写结构。

        前置条件: 无。
        后置条件: 返回值可被 json.dumps 直接写出；case 递归序列化。
        禁止: 把 backend 写成单数键——上游 JSON 用 `backends`，旧文档写 `backend`，
              两种读法只在解析器（arena.parsers）里兼容，落盘格式只认归一化后的名字。
        异常: 无。
        """
        return {
            "case": self.case.to_dict(),
            "avg_ts": self.avg_ts,
            "stddev_ts": self.stddev_ts,
            "n_gpu_layers": self.n_gpu_layers,
            "backend": self.backend,
            "build_commit": self.build_commit,
            "test_time": self.test_time,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BenchResult:
        """从 JSON 结构还原。

        前置条件: payload 由 to_dict() 产出。
        后置条件: avg_ts/stddev_ts 为 Python float（FP64，见 ADR-0006）。
        禁止: 用 0 代替缺失的吞吐——0 是"测出来为零"，与"没测到"不同。
        异常: KeyError —— 缺 case/avg_ts；ValueError/TypeError —— 数值不可转换。
        """
        return cls(
            case=BenchCase.from_dict(payload["case"]),
            avg_ts=float(payload["avg_ts"]),
            stddev_ts=float(payload["stddev_ts"]),
            n_gpu_layers=int(payload["n_gpu_layers"]),
            backend=str(payload.get("backend", "")),
            build_commit=str(payload.get("build_commit", "")),
            test_time=str(payload.get("test_time", "")),
        )


# ---------------------------------------------------------------------------
# Perplexity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerplexityResult:
    """llama-perplexity 的最终困惑度。"""

    ppl: float
    ppl_stderr: float
    n_chunks: int
    n_ctx: int
    corpus_name: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写结构。

        前置条件: 无。
        后置条件: 返回值可被 json.dumps 直接写出。
        禁止: 只写 ppl 而丢掉 ppl_stderr——没有标准误的困惑度无法判断"这个差距是否显著"。
        异常: 无。
        """
        return {
            "ppl": self.ppl,
            "ppl_stderr": self.ppl_stderr,
            "n_chunks": self.n_chunks,
            "n_ctx": self.n_ctx,
            "corpus_name": self.corpus_name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PerplexityResult:
        """从 JSON 结构还原。

        前置条件: payload 由 to_dict() 产出。
        后置条件: ppl/ppl_stderr 为 Python float（FP64）。
        禁止: 把 n_chunks/n_ctx 缺失当成默认值猜测——它们是"实际用了多少"的事实记录。
        异常: KeyError —— 必填字段缺失；ValueError/TypeError —— 数值不可转换。
        """
        return cls(
            ppl=float(payload["ppl"]),
            ppl_stderr=float(payload["ppl_stderr"]),
            n_chunks=int(payload["n_chunks"]),
            n_ctx=int(payload["n_ctx"]),
            corpus_name=str(payload["corpus_name"]),
        )


# ---------------------------------------------------------------------------
# Exam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExamLoad:
    """一次 Exam Trial 实际使用的采样参数。

    这些参数此前完全没有落盘，导致"两份答卷是不是同一条件下答的"无法从历史回答；
    参数面板一旦上线（本项目决定不做，见 ADR-0018）这种不可追溯会立刻变成事故，
    因此把它们补进记录，让"同参数"成为可核验的事实而不是口头承诺。

    n_predict 为负值（-1）表示"不限生成"——生成到 EOS 或上下文满为止（ADR-0020）。
    读历史时不要把它当成"负数说明写错了"：旧记录里的正数是当时那个 token 上限的遗迹。
    """

    n_predict: int
    n_ctx: int
    temperature: float
    seed: int

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写结构。

        前置条件: 无。
        后置条件: 返回值只含 str/int/float，可被 json.dumps 直接写出。
        禁止: 省略 seed 或 temperature——复现同一份答卷两者都不可缺。
        异常: 无。
        """
        return {
            "n_predict": self.n_predict,
            "n_ctx": self.n_ctx,
            "temperature": self.temperature,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExamLoad:
        """从 JSON 结构还原。

        前置条件: payload 由 to_dict() 产出。
        后置条件: temperature 为 Python float（即 FP64，见 ADR-0006），其余字段为 int。
        禁止: 对缺失字段猜默认值——这些字段是"实际用了什么"的事实记录。
        异常: KeyError —— 字段缺失；ValueError/TypeError —— 数值不可转换。
        """
        return cls(
            n_predict=int(payload["n_predict"]),
            n_ctx=int(payload["n_ctx"]),
            temperature=float(payload["temperature"]),
            seed=int(payload["seed"]),
        )


@dataclass(frozen=True)
class PaperFailure:
    """一份试卷没跑成的原因（EXAM 逐卷落盘的配套）。

    试卷之间是**独立**的：一份失败不该让已完成的另一份从记录里消失（ADR-0021）。
    因此失败按"卷"记，而不是把整条 ExamRecord 变成一句错误。
    """

    paper_name: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写结构。

        前置条件: 无。
        后置条件: 返回只含 str 的 dict，可被 json.dumps 直接写出。
        禁止: 把 reason 压成成功/失败两值——"超时"与"抽取失败"对下一步动作的指向不同。
        异常: 无。
        """
        return {"paper_name": self.paper_name, "reason": self.reason}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PaperFailure":
        """从 JSON 结构还原。

        前置条件: payload 由 to_dict() 产出。
        后置条件: 返回字段均为 str 的 PaperFailure。
        禁止: 缺字段时猜测填充——原因缺失的失败记录没有诊断价值，宁可让读取方告警跳过。
        异常: KeyError —— 字段缺失；TypeError —— 值不可转为 str。
        """
        return cls(paper_name=str(payload["paper_name"]), reason=str(payload["reason"]))


@dataclass(frozen=True)
class AnswerSheet:
    """模型对一份试卷的答卷（本阶段不含判分，见 ADR-0009）。"""

    paper_name: str
    model_name: str
    text: str
    prompt_ts: float
    generation_ts: float
    wall_seconds: float

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写结构。

        前置条件: 无。
        后置条件: 返回值可被 json.dumps 直接写出；text 保留原样（不截断、不转义中文）。
        禁止: 在此处判分或改写正文——答卷是"模型原话"，要动也是在阅卷阶段（ADR-0009）。
        异常: 无。
        """
        return {
            "paper_name": self.paper_name,
            "model_name": self.model_name,
            "text": self.text,
            "prompt_ts": self.prompt_ts,
            "generation_ts": self.generation_ts,
            "wall_seconds": self.wall_seconds,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AnswerSheet:
        """从 JSON 结构还原。

        前置条件: payload 由 to_dict() 产出。
        后置条件: 吞吐与墙钟为 Python float（FP64）。
        禁止: 在 text 缺失时返回空答卷——那会把"抽取失败"伪装成"模型没说话"。
        异常: KeyError —— paper_name/model_name/text 缺失；ValueError/TypeError —— 数值不可转换。
        """
        return cls(
            paper_name=str(payload["paper_name"]),
            model_name=str(payload["model_name"]),
            text=str(payload["text"]),
            prompt_ts=float(payload.get("prompt_ts", 0.0)),
            generation_ts=float(payload.get("generation_ts", 0.0)),
            wall_seconds=float(payload.get("wall_seconds", 0.0)),
        )

    @property
    def chars(self) -> int:
        """答卷正文的字符数。

        前置条件: 无。
        后置条件: 返回 len(text)；0 表示没有抽取到任何正文。
        禁止: 拿它当"答完了"的判据——长度只说明抽到多少字，卷子答没答完要人来读（ADR-0009）；
              它的用途是让**明显异常**（空卷、被截断的短卷）肉眼可见（ADR-0021）。
        异常: 无。
        """
        return len(self.text)


# ---------------------------------------------------------------------------
# TrialRecord 族
# ---------------------------------------------------------------------------

_RECORD_TYPES: dict[TestKind, type["TrialRecord"]] = {}


@dataclass
class TrialRecord:
    """一次 Trial 的持久化快照（抽象基类）。

    子类通过类属性 KIND 声明自己是哪种 Trial；kind 因此不是字段，
    避免"字段值与子类类型不一致"这种非法状态。
    """

    run_id: str
    model_path: str
    model_name: str
    started_at: str
    wall_seconds: float
    ok: bool
    error: str = ""
    # 产生这条记录时用的负载档。它是"这些数字是在什么负载下测的"唯一凭据，
    # 因此落盘而不是在读取时推断；对比表只收 OFFICIAL_PROFILE 的记录（ADR-0018）。
    profile: str = OFFICIAL_PROFILE
    # 产生这条记录时传给工具的显存余量（-fitt/--fit-target，字节）。
    # 它决定大模型被卸载多少层到 CPU，直接影响力与速度，因此和 load/profile 一样必须落盘；
    # None 表示历史行没记录（不是"用了默认值"）。见 ADR-0019。
    vram_margin_bytes: int | None = None

    KIND: ClassVar[TestKind | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.KIND is not None:
            _RECORD_TYPES[cls.KIND] = cls

    @property
    def kind(self) -> TestKind:
        """本记录属于哪种 Trial。

        前置条件: 具体子类已声明 KIND（基类本身未声明）。
        后置条件: 返回子类 KIND 声明的枚举值；与落盘 JSON 的 kind 字段一致。
        禁止: 把 kind 做成实例字段——那会出现"字段值与子类类型不一致"的非法状态。
        异常: TypeError —— 直接实例化基类 TrialRecord（或未声明 KIND 的子类）。
        """
        if type(self).KIND is None:
            raise TypeError(f"{type(self).__name__} 未声明 KIND")
        return type(self).KIND

    def payload(self) -> dict[str, Any]:
        """返回子类特有字段的 JSON 结构（基类无内容）。

        前置条件: 无。
        后置条件: 基类返回空 dict；子类返回与 decode_payload 对称的结构。
        禁止: 把基类字段写进 payload——基类字段由 to_dict() 统一负责。
        异常: 无。
        """
        return {}

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSONL 一行所需的结构。

        前置条件: 无。
        后置条件: 返回值可被 json.dumps 直接写出；不含 set/Path 等非 JSON 类型。
        禁止: 在记录里塞入原始文本流——原始流另存为文件以便溯源（ADR-0005）。
        异常: 无。
        """
        return {
            "kind": self.kind.value,
            "run_id": self.run_id,
            "model_path": self.model_path,
            "model_name": self.model_name,
            "started_at": self.started_at,
            "wall_seconds": self.wall_seconds,
            "ok": self.ok,
            "error": self.error,
            "profile": self.profile,
            "vram_margin_bytes": self.vram_margin_bytes,
            "payload": self.payload(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TrialRecord":
        """从 JSONL 的一行还原记录。

        前置条件: payload 至少含 kind 与基类字段。
        后置条件: 返回对应子类实例，数值字段为 Python float（即 FP64）；
                   缺 profile 的历史行按 OFFICIAL_PROFILE 读——该字段是 2026-10-17 新增的
                   （ADR-0018），此前的记录全部由真实档产出，只有 4 轮 quick 自检无法从
                   已落盘的数据判别（工具在加载阶段就失败，参数未打印），这是已知限制；
                   缺 vram_margin_bytes 的历史行读作 None（表示"没记录策略"，不猜值，ADR-0019）。
        禁止: 对未知 kind 静默丢弃——历史文件被写坏时必须让调用方看见。
        异常: KeyError —— 缺字段；ValueError —— kind 非法或未知。
        """
        kind = TestKind(payload["kind"])
        target = cls if cls is not TrialRecord else _RECORD_TYPES.get(kind)
        if target is None:
            raise ValueError(f"没有注册 {kind} 的记录类型")
        raw_margin = payload.get("vram_margin_bytes")
        return target(
            run_id=str(payload["run_id"]),
            model_path=str(payload["model_path"]),
            model_name=str(payload.get("model_name", Path(str(payload["model_path"])).stem)),
            started_at=str(payload["started_at"]),
            wall_seconds=float(payload["wall_seconds"]),
            ok=bool(payload["ok"]),
            error=str(payload.get("error", "")),
            profile=str(payload.get("profile", OFFICIAL_PROFILE)),
            vram_margin_bytes=int(raw_margin) if raw_margin is not None else None,
            **target.decode_payload(payload.get("payload", {})),
        )

    @classmethod
    def decode_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """把 payload 结构还原成子类构造参数（基类无内容）。

        前置条件: payload 来自同一子类的 payload()（允许旧记录缺可选字段）。
        后置条件: 基类返回空 dict；返回值将被展开为子类构造参数。
        禁止: 在此处吞掉损坏结构——还原不了就让它抛，读取方会告警跳过该行（ADR-0005）。
        异常: KeyError/ValueError/TypeError —— 结构不可还原。
        """
        return {}


@dataclass
class BenchRecord(TrialRecord):
    """一次 Bench Trial 的记录。"""

    KIND: ClassVar[TestKind] = TestKind.BENCH
    results: list[BenchResult] = field(default_factory=list)
    # 重复次数（llama-bench 的 -r）。它决定样本数，进而决定这条吞吐有没有统计意义，
    # 因此必须落盘——否则"两次 bench 是不是同一条件"无法回答（ADR-0018）。
    # 缺省为 None 表示"历史行里没有这个事实"，绝不用 1 或 3 顶替。
    reps: int | None = None

    def payload(self) -> dict[str, Any]:
        """返回 results 与 reps 的 JSON 结构。

        前置条件: 无。
        后置条件: 每条 BenchResult 序列化成 dict；顺序与解析顺序一致（pp 在前、tg 在后）；
                   reps 保持 None 表示未记录（旧行），不写 0。
        禁止: 合并同类项或只保留最大值——原始记录条数是"跑了几次"的证据；
              也禁止在 reps 缺失时填默认值——那是在伪造实验条件。
        异常: 无。
        """
        return {"reps": self.reps, "results": [result.to_dict() for result in self.results]}

    @classmethod
    def decode_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """还原 results 与 reps。

        前置条件: payload 来自 payload()；缺 results 时视为空列表（失败记录即如此）。
        后置条件: 返回 {"reps": int|None, "results": [...]}，元素为 BenchResult。
        禁止: 对单条损坏做静默跳过——那会让记录数悄悄变少。
        异常: KeyError/ValueError/TypeError —— 单条结构不可还原。
        """
        raw_reps = payload.get("reps")
        return {
            "reps": int(raw_reps) if raw_reps is not None else None,
            "results": [BenchResult.from_dict(item) for item in payload.get("results", [])],
        }

    def throughput(self, *, n_prompt: bool) -> float | None:
        """取出 pp（n_prompt=True）或 tg（n_prompt=False）吞吐。

        前置条件: 无。
        后置条件: 返回匹配记录中样本数最大者的 avg_ts；无匹配返回 None。
        禁止: 把 pp 与 tg 混算平均——它们是两个不同的指标。
        异常: 无。
        """
        candidates = [r for r in self.results if (r.case.n_prompt > 0) == n_prompt]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.avg_ts).avg_ts


@dataclass
class PerplexityRecord(TrialRecord):
    """一次 Perplexity Trial 的记录。"""

    KIND: ClassVar[TestKind] = TestKind.PERPLEXITY
    result: PerplexityResult | None = None

    def payload(self) -> dict[str, Any]:
        """返回 result 的 JSON 结构。

        前置条件: 无。
        后置条件: 有结果时返回 {"result": {...}}；失败记录返回 {"result": None}。
        禁止: 用 0.0 顶替缺失的 PPL——"没测出来"与"测出来是 0"必须可区分。
        异常: 无。
        """
        return {"result": self.result.to_dict() if self.result else None}

    @classmethod
    def decode_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """还原 result。

        前置条件: payload 来自 payload()。
        后置条件: 有结构时返回 PerplexityResult，否则返回 None。
        禁止: 在 result 缺失时造假对象——None 是"这次没得到结果"的合法表达。
        异常: KeyError/ValueError/TypeError —— 结构不可还原。
        """
        raw = payload.get("result")
        return {"result": PerplexityResult.from_dict(raw) if raw else None}


@dataclass
class ExamRecord(TrialRecord):
    """一次 Exam Trial 的记录：覆盖该 TestPlan 里的全部试卷。

    **部分成功**是合法状态：每一份试卷的子进程一结束就落盘自己的答卷文件，
    因此 ok=False 时 sheets/answer_paths 里仍可能有已完成的那几卷，
    没跑成的卷记在 paper_failures 里（原因逐卷给，见 ADR-0021）。
    """

    KIND: ClassVar[TestKind] = TestKind.EXAM
    sheets: list[AnswerSheet] = field(default_factory=list)
    answer_paths: list[str] = field(default_factory=list)
    # 本次答卷用的采样参数；None 表示历史行没记录（不是"用了默认值"）。
    load: ExamLoad | None = None
    # 逐卷失败原因；空列表表示"每卷都跑成了"（旧记录解码后也为空，读作未记录）。
    paper_failures: list[PaperFailure] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        """返回 sheets、答卷文件路径、采样参数与逐卷失败的 JSON 结构。

        前置条件: 无。
        后置条件: sheets 为答卷列表；answer_paths 为磁盘路径列表（可能为空——未配置产物目录时）；
                   load 为实际采样参数或 None（旧行）；
                   paper_failures 为逐卷失败原因列表（ok=False 时才有内容）。
        禁止: 只写路径不写正文——历史必须能在答卷文件被删后仍然读出"模型答了什么"；
              也禁止漏掉 load——没有它，'同参数比较'退化成人对人的信任；
              更禁止因为某一卷失败就丢掉已成功那几卷的正文——那是把"少了一卷"伪装成"整轮没跑"。
        异常: 无。
        """
        return {
            "sheets": [sheet.to_dict() for sheet in self.sheets],
            "answer_paths": list(self.answer_paths),
            "load": self.load.to_dict() if self.load else None,
            "paper_failures": [failure.to_dict() for failure in self.paper_failures],
        }

    @classmethod
    def decode_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """还原 sheets、answer_paths、load 与 paper_failures。

        前置条件: payload 来自 payload()（允许旧记录缺 paper_failures）。
        后置条件: 返回 {"sheets": [...], "answer_paths": [...], "load": ExamLoad|None,
                   "paper_failures": [...]}，缺字段时为空列表 / None。
        禁止: 去重或多卷合并——试卷与答卷是一一对应的。
        异常: KeyError/ValueError/TypeError —— 单份答卷、load 或失败原因结构不可还原。
        """
        raw_load = payload.get("load")
        return {
            "sheets": [AnswerSheet.from_dict(item) for item in payload.get("sheets", [])],
            "answer_paths": [str(path) for path in payload.get("answer_paths", [])],
            "load": ExamLoad.from_dict(raw_load) if raw_load else None,
            "paper_failures": [
                PaperFailure.from_dict(item) for item in payload.get("paper_failures", [])
            ],
        }


# ---------------------------------------------------------------------------
# 汇总视图模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scoreboard:
    """一轮 Run 结束后屏幕上呈现的汇总。"""

    model: Model
    run_id: str
    bench_pp_ts: float | None
    bench_tg_ts: float | None
    perplexity: float | None
    perplexity_stderr: float | None
    answer_paths: tuple[str, ...]
    failures: tuple[str, ...]
    wall_seconds: float
    # 这组数字来自哪个负载档；空串表示"没有来源可标"（无历史占位板）。
    # 界面必须把它显示出来，否则一块来自 quick 自检的记分板会被误读成正式成绩（ADR-0018）。
    profile: str = ""

    @property
    def complete(self) -> bool:
        """三个指标是否都有值且无失败项。

        前置条件: 无。
        后置条件: 三指标非 None 且 failures 为空时为 True。
        禁止: 把"部分成功"也算作 complete——界面文案与状态判断都依赖这个语义。
        异常: 无。
        """
        return not self.failures and None not in (self.bench_pp_ts, self.bench_tg_ts, self.perplexity)


@dataclass(frozen=True)
class ComparisonRow:
    """跨模型对比表格中的一行。"""

    model_name: str
    model_path: str
    bench_pp_ts: float | None
    bench_tg_ts: float | None
    perplexity: float | None
    last_run_at: str


# ---------------------------------------------------------------------------
# TestPlan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestPlan:
    """对一个 Model 施加的一轮测试计划（Run 的输入）。

    放在 domain 而不是 application 层：Runner 需要读它，而 Runner 不能依赖 Session，
    否则形成循环（见 ADR-0003 的模块边界一节）。

    两个字段用负值表达"不设上限"，与工具原生语义一致（ADR-0020）：
    ppl_chunks=-1 表示跑完整语料，exam_n_predict=-1 表示生成到 EOS 或上下文满。
    代价都是"时间不可预估"，由 TrialTimeouts 的墙钟保护兜底。
    """

    model: Model
    kinds: frozenset[TestKind]
    bench_case: BenchCase
    bench_reps: int
    corpus: Corpus
    ppl_n_ctx: int
    ppl_chunks: int
    papers: tuple[Paper, ...]
    exam_n_predict: int
    exam_n_ctx: int
    exam_temperature: float
    exam_seed: int
    exam_prompt_suffix: str

    def wants(self, kind: TestKind) -> bool:
        """该计划是否包含某一类 Trial。

        前置条件: kind 为 TestKind 成员。
        后置条件: kinds 含该成员时为 True。
        禁止: 用"配置里有没有相关参数"推断——计划里的 kinds 才是唯一事实。
        异常: 无。
        """
        return kind in self.kinds
