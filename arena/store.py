"""Local Arena — 结果落盘（历史索引、答卷文件、原始流归档、协议标记）。

模块名: arena.store
职责: 把 TrialRecord 追加进 JSONL 历史、把答卷写成独立文件、把原始输出流留档以便溯源，
      并维护 runs 目录下的评估协议标记（ProtocolMarker）。
依赖: 标准库 json/warnings/pathlib/datetime；arena.domain、arena.vocabulary。
被依赖: arena.session、arena.history、arena.ui.controller。
相关 ADR: docs/decisions/ADR-0005-jsonl-archive.md、
          docs/decisions/ADR-0021-batch-lifecycle-partial-persistence-and-protocol-marker.md
"""

from __future__ import annotations

import json
import warnings
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from arena.domain import AnswerSheet, TrialRecord
from arena.vocabulary import PROTOCOL_FILENAME, PROTOCOL_NAME


class ResultStore(ABC):
    """TrialRecord 的持久化端口。"""

    @abstractmethod
    def append(self, record: TrialRecord) -> None:
        """追加一条记录。

        前置条件: record 已完成（含成功与失败两种）。
        后置条件: 记录在方法返回后已对后续进程可见。
        禁止: 覆盖既有记录——历史是只追加的事实序列。
        异常: OSError —— 磁盘不可写。
        """

    @abstractmethod
    def load_all(self) -> list[TrialRecord]:
        """载入全部记录，按写入顺序。

        前置条件: 无（文件不存在时视为空历史）。
        后置条件: 返回全部可解析记录；损坏的行被跳过并告警，不会中断载入。
        禁止: 静默丢弃损坏行——必须发出可被看到的告警。
        异常: OSError —— 文件存在但不可读。
        """


class JsonlResultStore(ResultStore):
    """一行一条 TrialRecord 的追加式存储。"""

    def __init__(self, index_path: Path) -> None:
        self.index_path = Path(index_path)

    def append(self, record: TrialRecord) -> None:
        """把记录序列化成一行 JSON 并追加。

        前置条件: record.kind 已由子类确定。
        后置条件: 文件末尾新增一行以 \n 结尾的 UTF-8 JSON（不转义中文，便于人工阅读）。
        禁止: 使用写入临时文件再替换的"原子写"——追加语义下没必要，且会打断并行读。
        异常: OSError —— 目录不可创建或文件不可写。
        """
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def load_all(self) -> list[TrialRecord]:
        """逐行解析日记文件。

        前置条件: 无。
        后置条件: 返回按写入顺序的全部记录；空行忽略；损坏行告警后跳过。
        禁止: 因单行损坏而放弃整份历史。
        异常: OSError —— 文件存在但不可读。
        """
        if not self.index_path.is_file():
            return []
        records: list[TrialRecord] = []
        with self.index_path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(TrialRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                    warnings.warn(f"{self.index_path}:{number} 记录损坏，已跳过（{exc}）", stacklevel=2)
        return records


class AnswerSheetWriter:
    """把 AnswerSheet 写成人类可读的 Markdown 文件。"""

    def write(self, sheet: AnswerSheet, dest_dir: Path, note: str = "") -> str:
        """写出答卷文件。

        前置条件: dest_dir 可创建；sheet.text 非 None；note 为可选的补充事实行（如题目覆盖度）。
        后置条件: 返回写出的文件绝对路径；文件名形如 `<模型sl>__<试卷名>.md`，
                   文件头带来源元数据，note 非空时多出一行 `- <note>`。
        禁止: 覆盖不同模型的同名答卷——文件名必须含模型标识；
              也禁止把 note 混进正文——正文必须是模型原话（ADR-0004）。
        异常: OSError —— 目录不可创建或文件不可写。
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{self._slug(sheet.model_name)}__{sheet.paper_name}.md"
        target.write_text(self._render(sheet, note), encoding="utf-8")
        return str(target)

    @staticmethod
    def _slug(name: str) -> str:
        """把模型名压成安全文件名片段。"""
        safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in name).strip("-._")
        return safe[:64] or "model"

    @staticmethod
    def _render(sheet: AnswerSheet, note: str = "") -> str:
        """渲染答卷正文。

        元数据放在正文之前：阅卷人先看到"谁答的、多快、多少字、答到哪一题"，再读答案
        （ADR-0009：本阶段不判分；字符数是"截断/空卷"的可见线索，见 ADR-0021；
        题目覆盖度与判定依据见 ADR-0022）。
        """
        extra = f"- 题目覆盖: {note}\n" if note else ""
        return (
            f"# 答卷 · {sheet.paper_name} · {sheet.model_name}\n\n"
            f"- 试卷文件: {sheet.paper_name}\n"
            f"- 模型: {sheet.model_name}\n"
            f"- Prompt 吞吐: {sheet.prompt_ts:.2f} t/s\n"
            f"- Generation 吞吐: {sheet.generation_ts:.2f} t/s\n"
            f"- 墙钟耗时: {sheet.wall_seconds:.2f} s\n"
            f"- 答卷字符数: {sheet.chars}\n"
            f"{extra}\n"
            f"---\n\n"
            f"{sheet.text}\n"
        )


class RunArtifacts:
    """一轮 Run 的原始产物目录（可溯源用的"物证"）。"""

    def __init__(self, runs_dir: Path) -> None:
        self._runs_dir = Path(runs_dir)

    def run_dir(self, run_id: str) -> Path:
        """返回某一轮 Run 的产物目录路径（不创建）。

        前置条件: run_id 非空且不含路径分隔符。
        后置条件: 返回 runs_dir/run_id。
        禁止: 允许 run_id 逃逸出 runs_dir（含 ".." 或绝对路径）。
        异常: ValueError —— run_id 为空或包含路径分隔符。
        """
        if not run_id or run_id != Path(run_id).name:
            raise ValueError(f"run_id 非法: {run_id!r}")
        return self._runs_dir / run_id

    def save(self, run_id: str, filename: str, text: str) -> str:
        """把原始文本流写入该轮 Run 的目录。

        前置条件: filename 为简单文件名（如 "bench.json"）。
        后置条件: 返回写出文件的绝对路径；同名文件被覆盖（同一轮内同名即同一份物证）。
        禁止: 解析失败时丢弃原始流——解析器抛异常时更要留下原文（ADR-0004）。
        异常: OSError —— 目录不可创建或文件不可写；ValueError —— filename 非法。
        """
        if not filename or filename != Path(filename).name:
            raise ValueError(f"filename 非法: {filename!r}")
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        target.write_text(text, encoding="utf-8")
        return str(target)


class ProtocolMarker:
    """runs 目录下的评估协议标记（协议名 + 生效时刻）。

    它是"跨协议的数字不同表"这条纪律里唯一的机械部分（ADR-0021）：
    历史记录本身不带协议字段，因此用**生效时刻**当分界线——早于它的记录按旧协议告警。
    协议名变了（或标记文件被删）就重新登记生效时刻，于是旧记录立刻"变旧"并被警告看见；
    真正把历史归档重置仍是人的动作，本类不碰历史文件。
    """

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)

    @property
    def path(self) -> Path:
        """标记文件路径（runs_dir/PROTOCOL）。

        前置条件: 无。
        后置条件: 返回 runs_dir / PROTOCOL_FILENAME；不校验存在性、不创建目录。
        禁止: 在属性访问里做 I/O——存在性判断归 read_since()，创建归 ensure()。
        异常: 无。
        """
        return self.runs_dir / PROTOCOL_FILENAME

    def ensure(self) -> str:
        """确保标记文件存在并返回当前协议的生效时刻。

        前置条件: 无（runs_dir 不存在时会创建）。
        后置条件: 文件内含 name=<PROTOCOL_NAME> 与 since=<UTC ISO 秒级>；
                   已存在且 name 相同时**不改动**（保证同一协议内 since 稳定）；
                   name 不同或文件缺失时以当前时刻重写，并返回该时刻。
        禁止: 在 name 相同时刷新 since——那会让旧记录永远不触发告警，保险就失效了。
        异常: OSError —— 目录不可创建或文件不可写。
        """
        since = self.read_since()
        if since is not None:
            return since
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"name={PROTOCOL_NAME}\nsince={stamp}\n", encoding="utf-8")
        return stamp

    def read_since(self) -> str | None:
        """读取当前协议的生效时刻；文件缺失、无 since 或协议名不符时返回 None。

        前置条件: 无。
        后置条件: 协议名等于 PROTOCOL_NAME 时返回 since 字符串，否则返回 None。
        禁止: 在协议名不符时返回旧的 since——那会把"换了协议"读成"同一协议"。
        异常: OSError —— 文件存在但不可读。
        """
        if not self.path.is_file():
            return None
        name: str | None = None
        since: str | None = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "name":
                name = value.strip()
            elif key.strip() == "since":
                since = value.strip()
        if name != PROTOCOL_NAME or not since:
            return None
        return since
