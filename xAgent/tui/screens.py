"""模态界面：首次启动配置提示、历史会话选择。"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, ListItem, ListView, Static

from core.sessions import SessionMeta


class ConfigNoticeScreen(ModalScreen[None]):
    """首次启动生成 config.ini 后的确认框。"""

    BINDINGS = [Binding("escape", "confirm", "退出", show=False)]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog config-dialog"):
            yield Static(
                "[bold #e3b341]已生成默认配置文件[/]",
                classes="modal-title",
                markup=True,
            )
            yield Static(
                f"配置文件路径：\n  {escape(str(self._config_path))}\n\n"
                "请先修改 [bold][llm] api_key[/]（以及需要时的 base_url / "
                "model_name），然后重新启动 xAgent。\n\n"
                "在完成配置之前，Agent 不会发起任何大模型请求。",
                classes="modal-body",
                markup=True,
            )
            yield Button("确认并退出", id="config-confirm", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#config-confirm", Button).focus()

    @on(Button.Pressed, "#config-confirm")
    def _confirm_button(self) -> None:
        self.action_confirm()

    def action_confirm(self) -> None:
        self.dismiss(None)
        self.app.exit()


class SessionPicker(ModalScreen[str | None]):
    """历史会话列表：↑↓ 选择、Enter 恢复、Esc 取消。"""

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

    def __init__(self, sessions: list[SessionMeta]) -> None:
        super().__init__()
        self._sessions = sessions

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog session-dialog"):
            yield Static(
                f"[bold]历史会话（{len(self._sessions)}）[/]",
                classes="modal-title",
                markup=True,
            )
            if self._sessions:
                items = [
                    ListItem(
                        Static(self._label(meta), markup=False),
                    )
                    for meta in self._sessions
                ]
                yield ListView(*items, id="session-list")
            else:
                yield Static(
                    "还没有保存过任何会话。先和 Agent 对话，"
                    "然后使用 /save 或 /new 保存。",
                    classes="modal-body",
                    markup=False,
                )
            yield Static(
                "↑↓ 选择 · Enter 恢复 · Esc 取消",
                classes="modal-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        try:
            self.query_one("#session-list", ListView).focus()
        except Exception:
            pass

    @staticmethod
    def _label(meta: SessionMeta) -> str:
        when = meta.updated_at.replace("T", " ") or "未知时间"
        return f"{when}  {meta.title}  ({meta.message_count} 条)"

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        try:
            index = self.query_one("#session-list", ListView).index
            session_id = self._sessions[index].id
        except Exception:
            return
        self.dismiss(session_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
