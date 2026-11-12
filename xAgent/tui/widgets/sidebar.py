"""固定左侧边栏。

三个分区（任务 2：每个分区都有独立标题与分隔线）：
    1. 会话信息：当前会话名称、上下文占用
    2. 已加载技能：扫描到的技能 + 是否已加载
    3. MCP 服务器：连接状态（绿/黄/红圆点）、工具数量、失败原因

边栏只读 Agent 状态，不主动发起任何网络请求。MCP 连接完成时由
App 调用 refresh 接口刷新，因此不需要重启界面。
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static


def _shorten(text: str, limit: int = 58) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Sidebar(VerticalScroll):
    """左侧固定边栏。"""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("id", "sidebar")
        kwargs.setdefault("classes", "sidebar")
        super().__init__(**kwargs)
        # 每个分区由「标题 Static + 内容 Static」组成，
        # 标题与内容分离后，CSS 才能给分区画分隔线并做悬停高亮。
        self._title_session = Static(
            "[bold #7aa2f7]会话信息[/]", classes="side-title", markup=True
        )
        self._title_skills = Static(
            "[bold #7aa2f7]已加载技能[/]", classes="side-title", markup=True
        )
        self._title_mcp = Static(
            "[bold #7aa2f7]MCP 服务器[/]", classes="side-title", markup=True
        )
        self._session = Static("", id="side-session", classes="side-body", markup=True)
        self._skills = Static("", id="side-skills", classes="side-body", markup=True)
        self._mcp = Static("", id="side-mcp", classes="side-body", markup=True)

    def compose(self):
        with Vertical(classes="side-section"):
            yield self._title_session
            yield self._session
        with Vertical(classes="side-section"):
            yield self._title_skills
            yield self._skills
        with Vertical(classes="side-section"):
            yield self._title_mcp
            yield self._mcp

    # ---------- 会话状态 ----------

    def update_session(
        self,
        title: str,
        session_id: str,
        used: int,
        window: int,
        percent: float,
        *,
        agents_md: bool = False,
    ) -> None:
        bar = _progress_bar(percent)
        lines = [
            f"[#8b949e]名称[/]  {escape(_shorten(title, 40))}",
            f"[#8b949e]编号[/]  {escape(session_id or '-')}",
            f"[#8b949e]上下文[/]  {used} / {window}",
            f"[#8b949e]占用[/]  {bar} {percent:.1f}%",
        ]
        if agents_md:
            lines.append("[#8b949e]AGENTS.md[/]  [#3fb950]已注入[/]")
        self._session.update("\n".join(lines))

    # ---------- 技能 ----------

    def update_skills(self, skills: list[Any], loaded: set[str]) -> None:
        self._title_skills.update(
            f"[bold #7aa2f7]已加载技能[/]  "
            f"[#6e7681]{len(loaded)}/{len(skills)}[/]"
        )
        lines: list[str] = []
        if not skills:
            lines.append("[#6e7681]（暂无技能）[/]")
        else:
            for skill in skills[:12]:
                name = getattr(skill, "name", str(skill))
                # 已加载：绿色实心圆；未加载：灰色空心圆
                mark = "[#3fb950]●[/]" if name in loaded else "[#6e7681]○[/]"
                lines.append(f"{mark} {escape(_shorten(name, 42))}")
            if len(skills) > 12:
                lines.append(f"[#6e7681]… 另有 {len(skills) - 12} 个[/]")
        self._skills.update("\n".join(lines))

    # ---------- MCP ----------

    def update_mcp(self, manager: Any) -> None:
        if manager is None:
            self._title_mcp.update("[bold #7aa2f7]MCP 服务器[/]")
            self._mcp.update("[#6e7681]（已禁用）[/]")
            return

        if not getattr(manager, "sdk_available", True):
            self._title_mcp.update("[bold #7aa2f7]MCP 服务器[/]")
            self._mcp.update(
                "[#e3b341]未安装 MCP SDK[/]\n[#6e7681]pip install mcp 后重启[/]"
            )
            return

        servers = list(getattr(manager, "servers", {}).values())
        connected = sum(
            1 for item in servers if getattr(item, "status", "") == "connected"
        )
        self._title_mcp.update(
            f"[bold #7aa2f7]MCP 服务器[/]  "
            f"[#6e7681]{connected}/{len(servers)}[/]"
        )

        if not servers:
            config_error = getattr(manager, "config_error", None)
            if config_error:
                self._mcp.update(
                    f"[#e3b341]配置未加载[/]\n"
                    f"[#6e7681]{escape(_shorten(config_error, 60))}[/]"
                )
            else:
                self._mcp.update("[#6e7681]（未配置服务器）[/]")
            return

        # 状态圆点：绿色=已连接，黄色=连接中，红色=连接失败
        lines: list[str] = []
        for server in servers[:8]:
            status = getattr(server, "status", "pending")
            name = escape(_shorten(getattr(server, "name", "?"), 30))
            if status == "connected":
                count = len(getattr(server, "tools", []))
                lines.append(
                    f"[#3fb950]●[/] {name}  [#6e7681]{count} 工具[/]"
                )
            elif status == "failed":
                lines.append(
                    f"[#f85149]●[/] {name}  [#f85149]连接失败[/]"
                )
                error = getattr(server, "error", None)
                if error:
                    lines.append(
                        f"  [#6e7681]{escape(_shorten(error, 52))}[/]"
                    )
            elif status == "connecting":
                lines.append(
                    f"[#e3b341]●[/] {name}  [#e3b341]连接中…[/]"
                )
            elif status == "stopped":
                lines.append(f"[#6e7681]○[/] {name}  [#6e7681]已停止[/]")
            else:
                lines.append(f"[#6e7681]○[/] {name}  [#6e7681]等待连接[/]")
        if len(servers) > 8:
            lines.append(f"[#6e7681]… 另有 {len(servers) - 8} 个[/]")
        self._mcp.update("\n".join(lines))


def _progress_bar(percent: float, width: int = 10) -> str:
    """用纯文本画一个上下文占用条，避免依赖额外控件。"""
    ratio = max(0.0, min(percent, 100.0)) / 100.0
    filled = int(round(ratio * width))
    color = "#3fb950" if ratio < 0.6 else ("#e3b341" if ratio < 0.8 else "#f85149")
    return f"[{color}]{'█' * filled}[/][#232a3a]{'░' * (width - filled)}[/]"
