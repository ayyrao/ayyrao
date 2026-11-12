"""对话滚动区：负责消息的挂载与滚动。"""

from __future__ import annotations

from typing import Any

from textual.containers import Horizontal, VerticalScroll

from .messages import (
    AssistantMessage,
    ErrorMessage,
    SystemMessage,
    ToolCallPanel,
    UserMessage,
)


class ChatView(VerticalScroll):
    """所有聊天消息都挂载在这里。"""

    def _autoscroll(self, force: bool = False) -> None:
        """用户停留在底部时才自动跟随输出。"""
        if force or self.is_vertical_scroll_end:
            self.call_after_refresh(self.scroll_end, animate=False)

    def scroll_to_end(self) -> None:
        self._autoscroll()

    async def add_welcome(self, markup_text: str) -> SystemMessage:
        widget = SystemMessage(markup_text)
        await self.mount(widget)
        self._autoscroll(force=True)
        return widget

    async def add_system(self, markup_text: str, variant: str = "info") -> SystemMessage:
        widget = SystemMessage(markup_text, variant=variant)
        await self.mount(widget)
        self._autoscroll(force=True)
        return widget

    async def add_user(self, text: str) -> UserMessage:
        # 任务 2：消息外层包一个 Horizontal 行容器，用 align-horizontal 实现
        # “用户消息靠右、助手消息靠左”的左右分栏；Textual 8.x 的 margin
        # 不支持 auto，因此用行容器对齐，窗口缩放时也不会错位。
        widget = UserMessage(text)
        await self.mount(
            Horizontal(widget, classes="msg-row msg-row-user")
        )
        self._autoscroll(force=True)
        return widget

    async def add_assistant(self) -> AssistantMessage:
        widget = AssistantMessage()
        await self.mount(
            Horizontal(widget, classes="msg-row msg-row-assistant")
        )
        self._autoscroll(force=True)
        return widget

    async def add_tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        raw_arguments: str,
    ) -> ToolCallPanel:
        widget = ToolCallPanel(name, arguments, raw_arguments)
        await self.mount(widget)
        self._autoscroll(force=True)
        return widget

    async def add_error(self, text: str) -> ErrorMessage:
        widget = ErrorMessage(text)
        await self.mount(widget)
        self._autoscroll(force=True)
        return widget

    async def clear_messages(self) -> None:
        for child in list(self.children):
            await child.remove()
        self.scroll_home(animate=False)
