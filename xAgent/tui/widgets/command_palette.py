"""输入 `/` 时弹出的斜杠命令补全面板。

面板本身只负责渲染；键盘事件由 App 统一处理（见 tui/app.py）：
    ↑ / ↓  移动高亮项
    Tab    补全到输入框
    Esc    关闭面板
"""

from __future__ import annotations

from rich.markup import escape
from textual.widgets import Static


class CommandPalette(Static):
    """显示匹配的斜杠命令及简短说明。"""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("id", "palette")
        kwargs.setdefault("classes", "command-palette")
        kwargs.setdefault("markup", True)
        super().__init__("", **kwargs)
        # 任务 2：面板边框上显示标题，和输入框的圆角边框保持统一样式
        self.border_title = " 命令 "
        self._items: list[tuple[str, str]] = []
        self._index = 0

    # ---------- 状态 ----------

    @property
    def is_open(self) -> bool:
        return bool(self._items) and self.has_class("visible")

    def set_items(self, items: list[tuple[str, str]]) -> None:
        self._items = list(items)
        self._index = 0
        if not self._items:
            self.close()
            return
        # 注意：必须先刷新内容、再显示。否则布局系统会在「可见但内容还是空」
        # 的瞬间测量高度；另外本类的渲染方法不能叫 _render，那是 Textual
        # Widget 的内部方法，覆盖后会让 get_content_height 拿到 None。
        self._render_content()
        self.add_class("visible")

    def move(self, delta: int) -> None:
        if not self._items:
            return
        self._index = (self._index + delta) % len(self._items)
        self._render_content()

    def current(self) -> str | None:
        if not self._items:
            return None
        return self._items[self._index][0]

    def close(self) -> None:
        self._items = []
        self._index = 0
        self.remove_class("visible")
        self.update("")

    # ---------- 渲染 ----------

    def _render_content(self) -> None:
        lines = ["[#6e7681]↑↓ 选择 · Tab 补全 · Esc 关闭[/]"]
        for index, (command, description) in enumerate(self._items):
            if index == self._index:
                # 选中项：整行反色高亮，保证在深色主题下足够醒目
                lines.append(
                    f"[bold #0f1117 on #5b8def] {escape(command):<12} "
                    f"{escape(description)} [/]"
                )
            else:
                lines.append(
                    f"  [#7aa2f7]{escape(command):<12}[/] "
                    f"[#8b949e]{escape(description)}[/]"
                )
        self.update("\n".join(lines))
