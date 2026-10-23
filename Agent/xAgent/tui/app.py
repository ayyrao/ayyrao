"""Textual TUI 应用。

这一层只做三件事：
    1. 渲染界面
    2. 收集用户输入
    3. 消费 core.Agent 的事件流并展示

没有文件操作、没有 function call 调度、没有路径校验。
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input

from core.agent import Agent
from core.config import Settings
from core.events import (
    AgentEvent,
    ErrorEvent,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)

from .widgets import AssistantMessage, ChatView, StatusBar, ToolCallPanel

PROMPT_PLACEHOLDER = "输入指令，Enter 发送 · /help 查看命令 · Esc 中止"
BUSY_PLACEHOLDER = "Agent 正在处理，请稍候…（Esc 中止）"

HELP_TEXT = """[bold]可用命令[/]
[bold #5b8def]/help[/]    显示本帮助
[bold #5b8def]/clear[/]   清空界面记录并重置上下文
[bold #5b8def]/reset[/]   仅重置 Agent 上下文
[bold #5b8def]/tools[/]   列出 Agent 可用的工具
[bold #5b8def]/status[/]  查看模型、沙箱与上下文状态
[bold #5b8def]/quit[/]    退出

[bold]快捷键[/]
[bold #5b8def]Enter[/]    发送指令
[bold #5b8def]Esc[/]      中止当前请求（上下文自动回滚）
[bold #5b8def]Ctrl+L[/]   清空对话
[bold #5b8def]Ctrl+C[/]   退出"""


def _welcome_markup(model: str, workspace: str) -> str:
    return (
        "[bold #5b8def]xAgent[/]  [#8b949e]· 文件管理助手[/]\n"
        f"[#8b949e]模型[/]  {escape(model)}\n"
        f"[#8b949e]沙箱[/]  {escape(workspace)}\n"
        "[#8b949e]直接输入自然语言指令即可，例如「列出当前目录」「新建 notes/todo.md」。"
        "输入 /help 查看命令。[/]"
    )


class XAgentApp(App[None]):
    """xAgent 终端界面。"""

    CSS_PATH = "styles.tcss"
    TITLE = "xAgent"
    SUB_TITLE = "文件管理 Agent · TUI"

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", priority=True),
        Binding("ctrl+l", "clear_chat", "清空"),
        Binding("escape", "cancel_turn", "中止", show=False),
    ]

    def __init__(
        self,
        agent: Agent | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._settings = settings

        self._turn_worker: Any = None
        self._busy = False
        self._assistant: AssistantMessage | None = None
        self._pending_tools: dict[str, ToolCallPanel] = {}

        self._ready_text = "就绪"
        self._model_name = "未初始化"
        self._workspace_hint = "-"

    # ---------- 组件 ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatView(id="chat")
        yield StatusBar(id="status")
        yield Input(placeholder=PROMPT_PLACEHOLDER, id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        chat = self.query_one(ChatView)
        prompt = self.query_one("#prompt", Input)

        try:
            if self._agent is None:
                self._agent = Agent(settings=self._settings or Settings.load())
        except Exception as exc:
            await chat.add_error(f"初始化失败：{exc}")
            prompt.disabled = True
            self.query_one(StatusBar).set_status("配置错误，请检查 config.ini", "error")
            return

        settings = getattr(self._agent, "settings", None) or self._settings
        sandbox = getattr(self._agent, "sandbox", None)
        self._model_name = getattr(settings, "model", "unknown")
        self._workspace_hint = str(getattr(sandbox, "root", "-"))
        self.sub_title = f"文件管理 Agent · {self._model_name}"
        self._ready_text = f"就绪 · {self._model_name} · 沙箱 {self._workspace_hint}"

        await chat.add_welcome(_welcome_markup(self._model_name, self._workspace_hint))
        self.query_one(StatusBar).set_status(self._ready_text, "idle")
        prompt.focus()

    async def on_unmount(self) -> None:
        if self._agent is not None:
            try:
                await self._agent.aclose()
            except Exception:
                pass

    # ---------- 输入 ----------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""

        if not value:
            return
        if self._busy:
            await self.query_one(ChatView).add_system(
                "当前请求尚未结束，请稍候或按 Esc 中止。", variant="warn"
            )
            return
        if value.startswith("/"):
            await self._handle_command(value)
            return

        chat = self.query_one(ChatView)
        await chat.add_user(value)
        self._start_turn(value)

    def _start_turn(self, text: str) -> None:
        self._set_busy(True)
        self._turn_worker = self._run_turn(text)

    # ---------- Agent 事件消费 ----------

    @work(exclusive=True, group="agent")
    async def _run_turn(self, text: str) -> None:
        chat = self.query_one(ChatView)
        try:
            assert self._agent is not None
            async for event in self._agent.stream_turn(text):
                await self._handle_event(event, chat)
        except asyncio.CancelledError:
            try:
                await chat.add_system("已中止本轮请求，上下文已回滚。", variant="warn")
            except Exception:
                pass
            raise
        except Exception as exc:
            await chat.add_error(f"界面层捕获到未处理异常：{exc}")
        finally:
            self._assistant = None
            self._pending_tools.clear()
            self._set_busy(False)

    async def _handle_event(self, event: AgentEvent, chat: ChatView) -> None:
        status = self.query_one(StatusBar)

        if isinstance(event, RoundStarted):
            status.set_status(f"第 {event.round} 轮 · 等待模型响应…", "busy")

        elif isinstance(event, TextDelta):
            assistant = await self._ensure_assistant(chat)
            if assistant.append_text(event.text):
                chat.scroll_to_end()
            status.set_status("正在生成回答…", "busy")

        elif isinstance(event, ToolCallStarted):
            if self._assistant is not None:
                self._assistant.append_text("", force=True)
                self._assistant = None
            panel = await chat.add_tool_call(
                event.name, event.arguments, event.raw_arguments
            )
            self._pending_tools[event.call_id] = panel
            status.set_status(f"执行工具 {event.name} …", "busy")

        elif isinstance(event, ToolCallFinished):
            panel = self._pending_tools.pop(event.call_id, None)
            if panel is not None:
                panel.set_result(event.ok, event.output, event.elapsed)
                chat.scroll_to_end()
            status.set_status(
                f"工具 {event.name} {'完成' if event.ok else '失败'}"
                f" · {event.elapsed * 1000:.0f} ms",
                "busy" if event.ok else "error",
            )

        elif isinstance(event, ErrorEvent):
            await chat.add_error(event.message)
            status.set_status("出现错误", "error")

        elif isinstance(event, TurnFinished):
            if self._assistant is not None:
                self._assistant.append_text("", force=True)
            self._assistant = None

    async def _ensure_assistant(self, chat: ChatView) -> AssistantMessage:
        if self._assistant is None:
            self._assistant = await chat.add_assistant()
        return self._assistant

    # ---------- 状态与命令 ----------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            prompt = self.query_one("#prompt", Input)
            status = self.query_one(StatusBar)
        except Exception:
            return

        prompt.disabled = busy
        if busy:
            prompt.placeholder = BUSY_PLACEHOLDER
            status.set_status("正在处理…", "busy")
        else:
            prompt.placeholder = PROMPT_PLACEHOLDER
            status.set_status(self._ready_text, "idle")
            prompt.focus()

    async def _handle_command(self, raw: str) -> None:
        chat = self.query_one(ChatView)
        command = raw.split(maxsplit=1)[0].lower()

        if command in {"/quit", "/exit", "/q"}:
            self.exit()

        elif command == "/help":
            await chat.add_system(HELP_TEXT)

        elif command == "/clear":
            if self._agent is not None:
                self._agent.reset()
            await chat.clear_messages()
            await chat.add_system("对话记录与 Agent 上下文已清空。")

        elif command == "/reset":
            if self._agent is not None:
                self._agent.reset()
            await chat.add_system("Agent 上下文已重置（界面记录保留）。")

        elif command == "/tools":
            if self._agent is None:
                await chat.add_error("Agent 尚未初始化。")
                return
            specs = self._agent.registry.specs()
            lines = [
                f"[bold #5b8def]{escape(spec.name)}[/]  "
                f"[#8b949e]{escape(spec.description)}[/]"
                for spec in specs
            ]
            await chat.add_system(
                f"[bold]可用工具（{len(specs)} 个）[/]\n" + "\n".join(lines)
            )

        elif command == "/status":
            history_len = len(self._agent.history) if self._agent else 0
            rounds = getattr(getattr(self._agent, "settings", None), "max_tool_rounds", "-")
            await chat.add_system(
                "[bold]运行状态[/]\n"
                f"[#8b949e]模型[/]  {escape(self._model_name)}\n"
                f"[#8b949e]沙箱[/]  {escape(self._workspace_hint)}\n"
                f"[#8b949e]最大工具轮数[/]  {rounds}\n"
                f"[#8b949e]上下文消息数[/]  {history_len}"
            )

        else:
            await chat.add_system(
                f"未知命令：{escape(command)}。输入 /help 查看可用命令。",
                variant="warn",
            )

    # ---------- 快捷键 ----------

    def action_cancel_turn(self) -> None:
        if not self._busy or self._turn_worker is None:
            return
        self.query_one(StatusBar).set_status("正在中止…", "busy")
        self._turn_worker.cancel()

    async def action_clear_chat(self) -> None:
        if self._busy:
            await self.query_one(ChatView).add_system(
                "请求进行中，无法清空。请先按 Esc 中止。", variant="warn"
            )
            return
        await self._handle_command("/clear")


def main() -> None:
    XAgentApp().run()


if __name__ == "__main__":
    main()
