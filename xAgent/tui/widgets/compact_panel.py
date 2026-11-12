"""/compact 成功后的双栏浮动面板。

左栏：压缩前的原始对话文本（只用于查看，完整内容在归档文件里）。
右栏：压缩后的摘要。

两栏都可以独立滚动，底部有“关闭”按钮，按 Esc 也能关闭。
这个文件只负责显示，不包含任何压缩业务逻辑。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class CompactPanel(ModalScreen[None]):
    """压缩结果浮层：左右双栏 + 滚动 + 关闭按钮。"""

    BINDINGS = [("escape", "close_panel", "关闭")]

    def __init__(
        self,
        original_text: str,
        summary_text: str,
        archive_id: str,
    ) -> None:
        super().__init__()
        self._original_text = original_text
        self._summary_text = summary_text
        self._archive_id = archive_id

    def compose(self) -> ComposeResult:
        with Vertical(id="compact-dialog"):
            yield Static(
                f"上下文压缩结果 · 归档编号 {self._archive_id}",
                id="compact-title",
            )
            with Horizontal(id="compact-columns"):
                with Vertical(classes="compact-column"):
                    yield Static("压缩前原文", classes="compact-header")
                    with VerticalScroll(classes="compact-scroll"):
                        yield Static(
                            self._original_text,
                            classes="compact-text",
                            markup=False,
                        )
                with Vertical(classes="compact-column"):
                    yield Static("压缩后摘要", classes="compact-header")
                    with VerticalScroll(classes="compact-scroll"):
                        yield Static(
                            self._summary_text,
                            classes="compact-text",
                            markup=False,
                        )
            yield Button("关闭", id="compact-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "compact-close":
            self.dismiss()

    def action_close_panel(self) -> None:
        self.dismiss()
