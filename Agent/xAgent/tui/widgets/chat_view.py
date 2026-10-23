"""对话滚动区：负责消息的挂载与滚动。"""

from __future__ import annotations

from typing import Any

from textual.containers import VerticalScroll

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
        widget = UserMessage(text)
        await self.mount(widget)
        self._autoscroll(force=True)
        return widget

    async def add_assistant(self) -> AssistantMessage:
        widget = AssistantMessage()
        await self.mount(widget)
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
