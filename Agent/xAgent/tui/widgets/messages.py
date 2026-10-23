"""聊天消息类控件。

只负责渲染，不包含任何业务逻辑。
"""

from __future__ import annotations

import json
import time
from typing import Any

from rich.markup import escape
from textual.widgets import Collapsible, Markdown, Static

TOOL_PREVIEW_LIMIT = 4000
STREAM_FLUSH_INTERVAL = 0.08


class UserMessage(Static):
    """用户输入。"""

    def __init__(self, text: str) -> None:
        super().__init__(text, classes="msg-user", markup=False)


class AssistantMessage(Markdown):
    """助手的流式回答。

    Textual 的 Markdown 每次 update 都会重新解析，因此这里做 80ms 节流，
    避免逐 token 刷新造成的卡顿。
    """

    def __init__(self) -> None:
        super().__init__("", classes="msg-assistant")
        self._buffer = ""
        self._last_flush = 0.0
        self._dirty = False

    @property
    def text(self) -> str:
        return self._buffer

    def append_text(self, chunk: str, *, force: bool = False) -> bool:
        """追加文本；返回是否真正触发了渲染。"""
        if chunk:
            self._buffer += chunk
            self._dirty = True
        if not self._dirty:
            return False
        now = time.monotonic()
        if force or now - self._last_flush >= STREAM_FLUSH_INTERVAL:
            self._flush()
            return True
        return False

    def _flush(self) -> None:
        self._dirty = False
        self._last_flush = time.monotonic()
        self.update(self._buffer)


class SystemMessage(Static):
    """系统提示 / 命令反馈。"""

    def __init__(self, text: str, variant: str = "info") -> None:
        super().__init__(text, classes=f"msg-system {variant}", markup=True)


class ErrorMessage(Static):
    """错误提示，按纯文本渲染，避免内容里的 Rich 标记被解析。"""

    def __init__(self, text: str) -> None:
        super().__init__(text, classes="msg-error", markup=False)


class ToolCallPanel(Collapsible):
    """单次工具调用：标题显示状态，展开后显示参数与结果。"""

    def __init__(
        self,
        name: str,
        arguments: dict[str, Any],
        raw_arguments: str,
    ) -> None:
        self._name = name
        self._raw = (
            raw_arguments.strip()
            if raw_arguments and raw_arguments.strip()
            else json.dumps(arguments, ensure_ascii=False)
        )
        self._body = Static(
            f"调用参数\n{self._raw}\n\n执行中…",
            classes="tool-body",
            markup=False,
        )
        super().__init__(
            self._body,
            title=escape(f"🔧 调用工具 {name}"),
            collapsed=False,
            classes="tool-panel",
        )

    def set_result(self, ok: bool, output: str, elapsed: float) -> None:
        icon = "✅" if ok else "❌"
        self.title = escape(f"{icon} 工具 {self._name} · {elapsed * 1000:.0f} ms")

        if len(output) > TOOL_PREVIEW_LIMIT:
            output = output[:TOOL_PREVIEW_LIMIT] + "\n…（输出过长，已截断）"
        label = "执行结果" if ok else "执行失败"
        self._body.update(f"调用参数\n{self._raw}\n\n{label}\n{output}")
