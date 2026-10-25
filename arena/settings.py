"""Local Arena — 用户配置的持久化（跨进程记住模型目录与 llama.cpp 目录）。

模块名: arena.settings
职责: 读写 XDG 配置目录下的 `config.json`，保存用户选择的模型目录与 llama.cpp 可执行文件目录；
      提供版本校验、结构迁移、损坏配置的显式错误与原子保存。
      它只认识"路径"这一种用户偏好，不认识任何领域对象。
依赖: 标准库 dataclasses/json/os/tempfile/pathlib。
被依赖: arena.config、arena.ui.controller。
相关 ADR: docs/decisions/ADR-0027-persisted-model-directory.md、
          docs/decisions/ADR-0028-persisted-llama-bin-directory.md、
          docs/decisions/ADR-0013-config-as-single-source.md
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

# 配置目录/文件名：放在 XDG 配置目录下，避免把带本机绝对路径的文件写进仓库。
SETTINGS_DIR_NAME = "local-arena"
SETTINGS_FILENAME = "config.json"
# 配置结构版本：字段语义变化时递增。新增字段用「可读旧版本 + 内存迁移」承接，
# 读不出来的版本才由 load() 明确拒绝（ADR-0028）。
SETTINGS_VERSION = 2
# 能读的版本：v1（只有 models_dir）读到即按当前结构在内存里迁移，写回时总是 v2。
READABLE_VERSIONS = (1, 2)


class SettingsError(RuntimeError):
    """配置文件存在但不可用（不可读、JSON 非法、版本不支持或字段类型错误）。"""


@dataclass(frozen=True)
class UserSettings:
    """跨进程保留的用户偏好。

    两项都是"这台机器上的路径"：模型目录与 llama.cpp 可执行文件目录。值对象保持 frozen，
    读取后由 AppConfig 用 replace 派生，不在运行期被某个模块偷偷改写。
    """

    models_dir: Path | None = None
    llama_bin_dir: Path | None = None


class SettingsStore:
    """`config.json` 的读写端口（不做路径存在性校验，校验归使用点）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """返回配置文件路径。

        前置条件: 无。
        后置条件: 返回构造时传入的路径（已展开用户目录由调用方负责）。
        禁止: 在属性访问里创建目录或读取文件——存在性判断归 load()，创建归 save()。
        异常: 无。
        """
        return self._path

    def load(self) -> UserSettings:
        """读取配置；文件不存在时返回空偏好。

        前置条件: 无（路径可以不存在）。
        后置条件: 文件不存在时返回 `UserSettings()`；存在且合法时返回其字段；
                   任一路径为 null/缺失时该字段为 `None`；v1 配置按当前结构迁移
                   （llama_bin_dir 为 `None`），迁移只在内存里发生。
        禁止: 静默吞掉损坏文件——JSON 非法、版本不可读或字段类型错误必须抛 SettingsError，
              让调用方决定"回退默认值并告警"还是"拒绝启动"；
              也禁止把 v1 的迁移写成"猜字段"——旧结构里没有的字段只能是 `None`。
        异常: SettingsError —— 文件不可读、JSON 非法、version 不在 READABLE_VERSIONS，
              或 models_dir/llama_bin_dir 不是非空字符串。
        """
        if not self._path.is_file():
            return UserSettings()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SettingsError(f"无法读取配置文件 {self._path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SettingsError(f"配置文件不是合法 JSON {self._path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise SettingsError(f"配置文件顶层必须是对象: {self._path}")
        version = raw.get("version")
        if version not in READABLE_VERSIONS:
            raise SettingsError(
                f"配置文件版本不受支持: {version!r}（可读 {READABLE_VERSIONS}，当前写 {SETTINGS_VERSION}）: "
                f"{self._path}"
            )

        # v1 没有 llama_bin_dir 这个键，读出来就是 None——这正是迁移的全部内容。
        return UserSettings(
            models_dir=_optional_path(raw, "models_dir", self._path),
            llama_bin_dir=_optional_path(raw, "llama_bin_dir", self._path) if version >= 2 else None,
        )

    def save(self, settings: UserSettings) -> None:
        """原子写入配置（先写同目录临时文件，再 replace 覆盖）。

        前置条件: settings 是完整偏好（models_dir 与 llama_bin_dir 均可为 None）。
        后置条件: 目标文件内容为 UTF-8 JSON，含 version（恒为 SETTINGS_VERSION）与两个路径字段；
                   父目录已创建；写入成功后不会留下临时文件。
        禁止: 直接覆盖目标文件——中途失败会留下半截 JSON，下次启动就无法读取；
              也禁止把路径原样写成相对路径，配置必须能跨工作目录复用；
              更禁止在保存某一项时把另一项写成 `None`——调用方必须传入完整偏好。
        异常: OSError —— 目录不可创建、临时文件不可写或 replace 失败。
        """
        models_dir = settings.models_dir.expanduser().resolve() if settings.models_dir else None
        llama_bin_dir = settings.llama_bin_dir.expanduser().resolve() if settings.llama_bin_dir else None
        payload = {
            "version": SETTINGS_VERSION,
            "models_dir": str(models_dir) if models_dir is not None else None,
            "llama_bin_dir": str(llama_bin_dir) if llama_bin_dir is not None else None,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            # replace 之前失败时清理临时文件；replace 成功后 temporary 已不存在。
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def _optional_path(raw: dict, field: str, path: Path) -> Path | None:
    """从配置字典取出可空路径字段；缺失或 `null` 时返回 None。

    前置条件: raw 是配置顶层对象；field 为字段名；path 仅用于错误文案。
    后置条件: 字段缺失或为 None 时返回 None；否则返回展开 `~` 的 Path（不做存在性校验）。
    禁止: 把空字符串当作"没设置"——空路径不是偏好，是损坏，必须报出来。
    异常: SettingsError —— 字段存在但不是非空字符串。
    """
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{field} 必须是非空字符串: {path}")
    return Path(value).expanduser()
