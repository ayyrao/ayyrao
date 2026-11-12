"""顶部 / 底部状态栏。"""

from __future__ import annotations

from rich.markup import escape
from textual.widgets import Static


class TopBar(Static):
    """顶部状态栏（任务 2 新增）。

    固定显示四项全局状态：
        当前模型 · 上下文占用率 · 已连接 MCP 数量 · 已加载技能数

    只做展示，数据由 App._refresh_sidebar() 推送；不发起任何 IO。
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("id", "topbar")
        kwargs.setdefault("classes", "top-bar")
        kwargs.setdefault("markup", True)
        # 初始化完成前先显示占位文本，避免顶部出现空白行
        super().__init__("[#6e7681]正在初始化…[/]", **kwargs)

    def update_state(
        self,
        *,
        model: str,
        percent: float,
        mcp_connected: int | None,
        mcp_total: int,
        skills_loaded: int,
        skills_total: int,
    ) -> None:
        """刷新顶部状态。mcp_connected=None 表示 MCP 已禁用。"""
        percent_color = (
            "#3fb950" if percent < 60 else ("#e3b341" if percent < 80 else "#f85149")
        )
        if mcp_connected is None:
            mcp_text = "[#6e7681]已禁用[/]"
        else:
            dot_color = "#3fb950" if mcp_connected else "#6e7681"
            mcp_text = (
                f"[{dot_color}]●[/] {mcp_connected}/{mcp_total}"
            )
        self.update(
            "[bold #5b8def]xAgent[/]"
            f"  [#5c6675]│[/]  [#8b949e]模型[/] [bold #d8dee9]{escape(model)}[/]"
            f"  [#5c6675]│[/]  [#8b949e]上下文[/] [{percent_color}]{percent:.1f}%[/]"
            f"  [#5c6675]│[/]  [#8b949e]MCP[/] {mcp_text}"
            f"  [#5c6675]│[/]  [#8b949e]技能[/] [#d8dee9]{skills_loaded}[/]"
            f"[#6e7681]/{skills_total}[/]"
        )


class StatusBar(Static):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("classes", "status-bar")
        kwargs.setdefault("markup", False)
        super().__init__("", **kwargs)

    def set_status(self, text: str, tone: str = "idle") -> None:
        self.update(text)
        for name in ("idle", "busy", "error"):
            self.set_class(name == tone, name)


class ContextBar(Static):
    """固定位置的上下文 Token 状态栏。

    显示格式（需求固定）：
        上下文：{当前token} / {窗口上限} | 占用 {百分比}%
    无法读取窗口上限时使用默认 128000，并在数字后标注【默认】。
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("classes", "context-bar")
        kwargs.setdefault("markup", False)
        super().__init__("", **kwargs)

    def set_usage(
        self,
        used: int,
        window: int,
        percent: float,
        window_is_default: bool = False,
        locked: bool = False,
    ) -> None:
        default_tag = "【默认】" if window_is_default else ""
        text = (
            f"上下文：{used} / {window}{default_tag} | 占用 {percent:.1f}%"
        )
        if locked:
            text += " | ⚠ 请执行 /compact"
        self.update(text)
        self.set_class(locked, "locked")
