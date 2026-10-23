"""底部状态栏。"""

from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("classes", "status-bar")
        kwargs.setdefault("markup", False)
        super().__init__("", **kwargs)

    def set_status(self, text: str, tone: str = "idle") -> None:
        self.update(text)
        for name in ("idle", "busy", "error"):
            self.set_class(name == tone, name)
