"""Local Arena — 主题挂载（外观与代码的唯一接缝）。

模块名: arena.ui.theme
职责: 把 arena/theme/arena.css 挂到 GDK Screen 上，并把 CSS 解析错误变成启动期异常。
依赖: PyGObject 的 Gtk/Gdk；标准库 pathlib。
被依赖: run_arena.py。
相关 ADR: docs/decisions/ADR-0007-external-css-theme.md
"""

from __future__ import annotations

import warnings
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402  (require_version 必须先执行)


class ThemeError(RuntimeError):
    """主题文件缺失或无法被 GTK 解析。"""


class ArenaTheme:
    """把一份外部 CSS 挂到当前 Screen。"""

    def __init__(self, css_path: Path) -> None:
        self._css_path = Path(css_path)
        self._parse_messages: list[str] = []

    @property
    def css_path(self) -> Path:
        """返回被挂载的主题文件路径。

        前置条件: 无。
        后置条件: 返回构造时给定的路径（不校验存在性）。
        禁止: 在此处加载文件——加载与报错归 apply() 负责。
        异常: 无。
        """
        return self._css_path

    @property
    def parse_messages(self) -> tuple[str, ...]:
        """返回 GTK 在解析 CSS 时产生的告警（挂载后才有值）。

        前置条件: 无。
        后置条件: 挂载前为空元组；apply() 之后含全部解析告警文本。
        禁止: 清空已收集的告警——它们是"主题坏在哪"的唯一线索。
        异常: 无。
        """
        return tuple(self._parse_messages)

    def apply(self) -> None:
        """加载并挂载主题。

        前置条件: GTK 已初始化（至少 import 完成）；css_path 指向存在的文件。
        后置条件: 样式对之后创建或已存在的控件生效；解析告警通过 parse_messages 暴露。
        禁止: 在 CSS 中依赖 GTK 不支持的属性——那会让整段规则失效（本方法会把它变成异常）。
        异常: ThemeError —— 文件不存在、加载失败或存在 CSS 解析错误。
        """
        if not self._css_path.is_file():
            raise ThemeError(f"主题文件不存在: {self._css_path}")

        provider = Gtk.CssProvider()
        provider.connect("parsing-error", self._on_parsing_error)
        try:
            provider.load_from_path(str(self._css_path))
        except Exception as exc:  # noqa: BLE001 - GLib 抛的是 GError 包装后的异常
            raise ThemeError(f"主题加载失败: {exc}") from exc

        screen = Gdk.Screen.get_default()
        if screen is None:
            raise ThemeError("没有可用的 Gdk.Screen：无法挂载主题（是否缺少 DISPLAY?）")
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        if self._parse_messages:
            # 启动期就把样式问题暴露出来：静默降级会让"外观坏了"变成难以定位的谜题。
            raise ThemeError("主题存在 CSS 解析错误:\n" + "\n".join(self._parse_messages))

    def _on_parsing_error(self, _provider: Gtk.CssProvider, _section, error) -> None:
        """收集 GTK 的 CSS 解析告警。"""
        message = getattr(error, "message", str(error))
        self._parse_messages.append(f"{self._css_path}: {message}")
        warnings.warn(f"CSS 解析告警: {message}", stacklevel=2)
