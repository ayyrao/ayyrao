"""Textual TUI 应用。

这一层只做五件事：
    1. 渲染界面（含固定左侧边栏）
    2. 收集用户输入与斜杠命令
    3. 消费 core.Agent 的事件流并展示
    4. 管理会话持久化（/new /resume /save /quit）
    5. 后台异步加载 MCP，并动态刷新边栏

没有文件操作、没有 function call 调度、没有路径校验。
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from typing import Any

from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input

from core.agent import AGENTS_MD_PREFIX, Agent
from core.config import ConfigError, Settings, ensure_config
from core.events import (
    AgentEvent,
    ContextUsage,
    ErrorEvent,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from core.sessions import (
    SessionError,
    SessionStore,
    derive_title,
    has_conversation,
)

from .screens import ConfigNoticeScreen, SessionPicker
from .widgets import (
    AssistantMessage,
    ChatView,
    CommandPalette,
    CompactPanel,
    ContextBar,
    Sidebar,
    StatusBar,
    ToolCallPanel,
    TopBar,
)

PROMPT_PLACEHOLDER = "输入指令，Enter 发送 · / 弹出命令 · Esc 中止"
BUSY_PLACEHOLDER = "Agent 正在处理，请稍候…（Esc 中止）"


# 斜杠命令注册表：命令面板、/help 与补全共用一个来源
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "显示所有可用命令与快捷键"),
    ("/new", "保存当前会话并新建会话"),
    ("/resume", "打开历史会话列表并恢复选中会话"),
    ("/save", "手动保存当前会话"),
    ("/status", "查看会话、上下文、技能与 MCP 状态"),
    ("/compact", "压缩当前上下文（先归档，再生成摘要）"),
    ("/clear", "清空当前上下文（不保存）"),
    ("/skills", "查看已发现/已加载的技能"),
    ("/mcp", "查看 MCP 服务器连接状态"),
    ("/tools", "列出 Agent 可用工具"),
    ("/reset", "仅重置 Agent 上下文（界面记录保留）"),
    ("/quit", "保存当前会话并退出"),
]


def _help_text() -> str:
    lines = ["[bold]可用命令[/]"]
    for command, description in SLASH_COMMANDS:
        lines.append(
            f"[bold #5b8def]{command:<9}[/] [#8b949e]{escape(description)}[/]"
        )
    lines.extend(
        [
            "",
            "[bold]快捷键[/]",
            "[bold #5b8def]Enter[/]    发送指令",
            "[bold #5b8def]↑ / ↓[/]    在命令面板中移动",
            "[bold #5b8def]Tab[/]      补全当前命令",
            "[bold #5b8def]Esc[/]      关闭命令面板 / 中止请求",
            "[bold #5b8def]Ctrl+L[/]   清空对话",
            "[bold #5b8def]Ctrl+C[/]   退出",
        ]
    )
    return "\n".join(lines)


def _welcome_markup(
    model: str, workspace: str, *, agents_md: bool = False
) -> str:
    extra = "\n[#8b949e]项目规范[/]  已注入 AGENTS.md" if agents_md else ""
    return (
        "[bold #5b8def]xAgent[/]  [#8b949e]· 文件管理助手[/]\n"
        f"[#8b949e]模型[/]  {escape(model)}\n"
        f"[#8b949e]沙箱[/]  {escape(workspace)}"
        f"{extra}\n"
        "[#8b949e]直接输入自然语言指令即可；输入 / 会弹出命令面板。"
        "输入 /help 查看全部命令。[/]"
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
        # Tab 在命令面板打开时用于补全；否则保持默认的焦点切换行为
        Binding("tab", "complete_command", "补全", show=False, priority=True),
    ]

    def __init__(
        self,
        agent: Agent | None = None,
        settings: Settings | None = None,
        config_path: str | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._settings = settings
        self._config_path = config_path

        self._turn_worker: Any = None
        self._compact_worker: Any = None
        self._busy = False
        self._assistant: AssistantMessage | None = None
        self._pending_tools: dict[str, ToolCallPanel] = {}

        self._ready_text = "就绪"
        self._model_name = "未初始化"
        self._workspace_hint = "-"
        self._compact_percent = 60

        # 会话状态
        self._store: SessionStore | None = None
        self._session_id = ""
        self._session_created_at = ""
        self._session_title = "新会话"

    # ---------- 组件 ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # 任务 2：顶部状态栏（模型 / 上下文占用 / 已连接 MCP / 已加载技能）
        yield TopBar()
        with Horizontal(id="body"):
            yield Sidebar()
            with Vertical(id="main"):
                yield ChatView(id="chat")
                # 固定位置：Token 状态栏在输入框上方，不随对话滚动
                yield ContextBar(id="context")
                yield StatusBar(id="status")
                yield CommandPalette()
                yield Input(placeholder=PROMPT_PLACEHOLDER, id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        chat = self.query_one(ChatView)
        prompt = self.query_one("#prompt", Input)
        # 任务 2：输入框圆角边框上的提示文字
        prompt.border_title = " ✎ 输入 "

        # =====================================================================
        # Task 3：首次启动检测 config.ini，缺失时生成模板并弹窗退出
        # =====================================================================
        try:
            config_path, created = ensure_config(self._config_path)
        except ConfigError as exc:
            await chat.add_error(str(exc))
            prompt.disabled = True
            self.query_one(StatusBar).set_status("配置错误", "error")
            return

        if created:
            prompt.disabled = True
            self.query_one(StatusBar).set_status(
                "已生成 config.ini，请修改后重新启动", "error"
            )
            self.push_screen(ConfigNoticeScreen(config_path))
            return

        try:
            settings = self._settings or Settings.load(config_path)
            self._settings = settings
            if self._agent is None:
                self._agent = Agent(settings=settings)
        except Exception as exc:
            await chat.add_error(f"初始化失败：{exc}")
            prompt.disabled = True
            self.query_one(StatusBar).set_status("配置错误，请检查 config.ini", "error")
            return

        settings = getattr(self._agent, "settings", None) or self._settings
        sandbox = getattr(self._agent, "sandbox", None)
        self._model_name = getattr(settings, "model", "unknown")
        self._workspace_hint = str(getattr(sandbox, "root", "-"))
        self._compact_percent = int(
            getattr(settings, "compact_threshold", 0.6) * 100
        )
        self.sub_title = f"文件管理 Agent · {self._model_name}"
        self._ready_text = f"就绪 · {self._model_name} · 沙箱 {self._workspace_hint}"

        # ---------- 会话目录与初始会话 ----------
        session_dir = settings.session_dir
        if settings.session_dir_is_default and self._agent is not None:
            session_dir = self._agent.skills.project_root / ".agents" / "sessions"
        self._store = SessionStore(session_dir)
        self._session_id = self._store.new_session_id()
        self._session_created_at = datetime.now().isoformat(timespec="seconds")
        self._session_title = "新会话"

        # ---------- TUI 配置化：边栏宽度 / 主题 / 状态条 ----------
        sidebar = self.query_one(Sidebar)
        try:
            sidebar.styles.width = settings.sidebar_width
        except Exception:
            pass
        if not settings.show_status_bar:
            for selector in ("#context", "#status"):
                try:
                    self.query_one(selector).styles.display = "none"
                except Exception:
                    pass
        try:
            self.theme = settings.tui_theme
        except Exception:
            # 主题名不存在时保持 Textual 默认主题，不影响功能
            pass

        await chat.add_welcome(
            _welcome_markup(
                self._model_name,
                self._workspace_hint,
                agents_md=self._agent.has_agents_md(),
            )
        )
        self.query_one(StatusBar).set_status(self._ready_text, "idle")
        self._refresh_context_bar()
        self._refresh_sidebar()
        prompt.focus()

        # =====================================================================
        # Task 4：TUI 先完整渲染，再在后台逐个连接 MCP Server
        # =====================================================================
        self._start_mcp_loading()

    async def on_unmount(self) -> None:
        if self._agent is not None:
            try:
                await self._agent.aclose()
            except Exception:
                pass

    # ---------- MCP 后台加载 ----------

    def _start_mcp_loading(self) -> None:
        if self._agent is None:
            return

        def _on_update() -> None:
            # 回调发生在后台 asyncio.Task 中，用 call_later 回到界面线程
            try:
                self.call_later(self._refresh_sidebar)
            except Exception:
                pass

        try:
            self._agent.start_mcp_background(on_update=_on_update)
        except Exception:
            # MCP 失败绝不能影响本地工具与会话
            pass

    # ---------- 输入 ----------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        self._update_palette(event.value)

    def _update_palette(self, value: str) -> None:
        palette = self.query_one(CommandPalette)
        text = value.strip()
        # 用原始 value 判断是否含空白：Tab 补全后会在命令末尾补一个空格，
        # 此时面板应立刻收起，而不是把 "/help " strip 成 "/help" 又弹出来。
        if not text.startswith("/") or any(ch.isspace() for ch in value):
            palette.close()
            return
        prefix = text.lower()
        matches = [
            (command, description)
            for command, description in SLASH_COMMANDS
            if command.startswith(prefix)
        ]
        if matches:
            palette.set_items(matches)
        else:
            palette.close()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        self.query_one(CommandPalette).close()

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

        # 上下文达到阈值后，普通输入被暂停；/compact 仍然可用
        if self._agent is not None and self._agent.context_locked:
            await self.query_one(ChatView).add_system(
                f"上下文占用已达到 {self._compact_percent}%，普通输入已暂停。"
                "请输入 /compact 压缩上下文，压缩成功后自动解锁。",
                variant="warn",
            )
            return

        # 立即用第一条用户消息更新边栏会话名
        self._session_title = derive_title(
            self._agent.history + [{"role": "user", "content": value}]
            if self._agent is not None
            else [{"role": "user", "content": value}],
            self._title_limit(),
        )

        chat = self.query_one(ChatView)
        await chat.add_user(value)
        self._refresh_sidebar()
        self._start_turn(value)

    def _title_limit(self) -> int:
        settings = getattr(self._agent, "settings", None) or self._settings
        return int(getattr(settings, "session_title_max_chars", 30))

    def _start_turn(self, text: str) -> None:
        self._set_busy(True)
        self._turn_worker = self._run_turn(text)

    # ---------- 键盘：命令面板 ----------

    def _main_screen_active(self) -> bool:
        try:
            return self.screen is self.screen_stack[0]
        except Exception:
            return True

    async def on_key(self, event: events.Key) -> None:
        """命令面板打开时拦截 ↑/↓（Tab 与 Esc 走绑定）。"""
        if not self._main_screen_active():
            return
        palette = self.query_one(CommandPalette)
        if not palette.is_open:
            return
        if event.key in {"up", "down"}:
            palette.move(-1 if event.key == "up" else 1)
            event.prevent_default()
            event.stop()

    def action_complete_command(self) -> None:
        palette = self.query_one(CommandPalette)
        if self._main_screen_active() and palette.is_open:
            command = palette.current()
            if command:
                prompt = self.query_one("#prompt", Input)
                prompt.value = f"{command} "
                prompt.cursor_position = len(prompt.value)
                prompt.focus()
            palette.close()
            return
        # 原有行为：Tab 切换焦点
        self.screen.focus_next()

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
            self._refresh_sidebar()

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
            # createSkill 等操作可能新增技能，实时刷新边栏
            self._refresh_sidebar()

        elif isinstance(event, ContextUsage):
            try:
                self.query_one(ContextBar).set_usage(
                    event.used,
                    event.window,
                    event.percent,
                    event.window_is_default,
                    event.locked,
                )
            except Exception:
                pass
            self._refresh_sidebar()
            if event.locked and event.message:
                await chat.add_system(event.message, variant="warn")

        elif isinstance(event, ErrorEvent):
            await chat.add_error(event.message)
            status.set_status("出现错误", "error")

        elif isinstance(event, TurnFinished):
            if self._assistant is not None:
                self._assistant.append_text("", force=True)
            self._assistant = None
            # 每轮结束自动保存，保证 /resume 列表始终是最新的
            settings = getattr(self._agent, "settings", None) or self._settings
            if getattr(settings, "auto_save_sessions", True):
                await self._save_current_session(notify=False)
            self._refresh_sidebar()

    async def _ensure_assistant(self, chat: ChatView) -> AssistantMessage:
        if self._assistant is None:
            self._assistant = await chat.add_assistant()
        return self._assistant

    # ---------- /compact 压缩流程 ----------

    def _start_compact(self) -> None:
        """启动压缩流水线；压缩期间输入框被禁用。"""
        self._set_busy(True)
        try:
            self.query_one("#prompt", Input).placeholder = (
                "正在压缩上下文，请稍候…"
            )
        except Exception:
            pass
        self._compact_worker = self._run_compact()

    @work(exclusive=True, group="compact")
    async def _run_compact(self) -> None:
        chat = self.query_one(ChatView)
        result = None
        try:
            if self._agent is None:
                await chat.add_error("Agent 尚未初始化。")
                return
            result = await self._agent.compact()
        except Exception as exc:
            await chat.add_error(f"压缩流程异常：{exc}")
        finally:
            self._set_busy(False)
            self._refresh_context_bar()
            self._refresh_sidebar()

        if result is None:
            return
        if not result.ok:
            await chat.add_error(
                f"压缩失败：{result.error}\n原始上下文已完整保留。"
            )
            return

        await chat.add_system(
            f"压缩成功。归档编号：{result.archive_id}\n"
            f"归档文件：{result.archive_path}"
        )
        # 双栏浮动面板：左栏=压缩前原文，右栏=压缩后摘要
        self.push_screen(
            CompactPanel(
                result.original_text,
                result.summary,
                result.archive_id,
            )
        )

    def _refresh_context_bar(self) -> None:
        """从 Agent 读取最新统计，刷新固定位置的 Token 状态栏。"""
        try:
            bar = self.query_one(ContextBar)
        except Exception:
            return
        if self._agent is None:
            return
        stats = self._agent.get_context_stats()
        bar.set_usage(
            stats.used,
            stats.window,
            stats.percent,
            stats.window_is_default,
            self._agent.context_locked,
        )

    # ---------- 侧边栏 ----------

    def _refresh_sidebar(self) -> None:
        if self._agent is None:
            return
        try:
            sidebar = self.query_one(Sidebar)
            stats = self._agent.get_context_stats()
            sidebar.update_session(
                self._session_title,
                self._session_id,
                stats.used,
                stats.window,
                stats.percent,
                agents_md=self._agent.has_agents_md(),
            )
            sidebar.update_skills(
                self._agent.skills.available_skills(),
                self._agent.skills.loaded_names(),
            )
            sidebar.update_mcp(self._agent.mcp)

            # =================================================================
            # 任务 2：顶部状态栏随边栏一起刷新（同一份 Agent 状态，避免重复计算）
            # 显示：当前模型 / 上下文占用率 / 已连接 MCP 数 / 已加载技能数
            # =================================================================
            manager = self._agent.mcp
            if manager is None:
                mcp_connected: int | None = None
                mcp_total = 0
            else:
                servers = list(getattr(manager, "servers", {}).values())
                mcp_total = len(servers)
                mcp_connected = sum(
                    1
                    for server in servers
                    if getattr(server, "status", "") == "connected"
                )
            self.query_one(TopBar).update_state(
                model=self._model_name,
                percent=stats.percent,
                mcp_connected=mcp_connected,
                mcp_total=mcp_total,
                skills_loaded=len(self._agent.skills.loaded_names()),
                skills_total=len(self._agent.skills.skills),
            )
        except Exception:
            # 边栏刷新失败不能影响主对话流程
            pass

    # ---------- 会话持久化 ----------

    async def _save_current_session(self, *, notify: bool = False) -> Any:
        chat = self.query_one(ChatView)

        if self._agent is None or self._store is None:
            if notify:
                await chat.add_error("会话存储尚未初始化。")
            return None

        messages = copy.deepcopy(self._agent.history)
        if not has_conversation(messages):
            if notify:
                await chat.add_system(
                    "当前没有可保存的对话内容。", variant="warn"
                )
            return None

        title = derive_title(messages, self._title_limit())
        settings = getattr(self._agent, "settings", None) or self._settings
        try:
            path = self._store.save(
                self._session_id,
                messages,
                title=title,
                created_at=self._session_created_at,
                model=getattr(settings, "model", ""),
                workspace=self._workspace_hint,
            )
        except SessionError as exc:
            if notify:
                await chat.add_error(f"保存会话失败：{exc}")
            return None

        self._session_title = title
        self._refresh_sidebar()
        if notify:
            await chat.add_system(f"会话已保存：\n{path}")
        return path

    def _new_session_state(self) -> None:
        """生成一个新的会话 id（不写文件）。"""
        if self._store is not None:
            self._session_id = self._store.new_session_id()
        else:
            self._session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._session_created_at = datetime.now().isoformat(timespec="seconds")
        self._session_title = "新会话"

    async def _show_welcome(self) -> None:
        if self._agent is None:
            return
        await self.query_one(ChatView).add_welcome(
            _welcome_markup(
                self._model_name,
                self._workspace_hint,
                agents_md=self._agent.has_agents_md(),
            )
        )

    async def _reset_ui_context(self) -> None:
        chat = self.query_one(ChatView)
        await chat.clear_messages()
        await self._show_welcome()

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
            self.query_one(CommandPalette).close()
        else:
            prompt.placeholder = PROMPT_PLACEHOLDER
            status.set_status(self._ready_text, "idle")
            prompt.focus()

    async def _handle_command(self, raw: str) -> None:
        chat = self.query_one(ChatView)
        command = raw.split(maxsplit=1)[0].lower()
        self.query_one(CommandPalette).close()

        if command in {"/quit", "/exit", "/q"}:
            # Task 1：/quit 先保存再安全退出
            await self._save_current_session(notify=False)
            self.exit()

        elif command == "/help":
            await chat.add_system(_help_text())

        elif command == "/new":
            await self._command_new()

        elif command == "/resume":
            await self._command_resume()

        elif command == "/save":
            await self._save_current_session(notify=True)

        elif command == "/compact":
            if self._agent is None:
                await chat.add_error("Agent 尚未初始化。")
                return
            if self._busy:
                await chat.add_system(
                    "当前请求尚未结束，请稍候或按 Esc 中止后再压缩。",
                    variant="warn",
                )
                return
            self._start_compact()

        elif command == "/clear":
            # Task 1：清空上下文，但不保存
            if self._agent is not None:
                self._agent.reset()
            self._new_session_state()
            await self._reset_ui_context()
            await chat.add_system("已清空当前会话上下文（未保存）。")
            self._refresh_context_bar()
            self._refresh_sidebar()

        elif command == "/reset":
            if self._agent is not None:
                self._agent.reset()
            await chat.add_system("Agent 上下文已重置（界面记录保留）。")
            self._refresh_context_bar()
            self._refresh_sidebar()

        elif command == "/tools":
            if self._agent is None:
                await chat.add_error("Agent 尚未初始化。")
                return
            tools = self._agent.tools
            lines = [
                f"[bold #5b8def]{escape(tool['function']['name'])}[/]  "
                f"[#8b949e]{escape(tool['function'].get('description', ''))}[/]"
                for tool in tools
            ]
            await chat.add_system(
                f"[bold]可用工具（{len(tools)} 个）[/]\n" + "\n".join(lines)
            )

        elif command == "/skills":
            await self._command_skills()

        elif command == "/mcp":
            await self._command_mcp()

        elif command == "/status":
            await self._command_status()

        else:
            await chat.add_system(
                f"未知命令：{escape(command)}。输入 /help 查看可用命令。",
                variant="warn",
            )

    # ---------- 各命令实现 ----------

    async def _command_new(self) -> None:
        chat = self.query_one(ChatView)
        saved = await self._save_current_session(notify=False)
        if self._agent is not None:
            self._agent.reset()
        self._new_session_state()
        await self._reset_ui_context()
        if saved is not None:
            await chat.add_system(
                f"已保存上一会话：{saved.name}\n已开始新会话：{self._session_id}"
            )
        else:
            await chat.add_system(f"已开始新会话：{self._session_id}")
        self._refresh_context_bar()
        self._refresh_sidebar()

    async def _command_resume(self) -> None:
        chat = self.query_one(ChatView)
        if self._store is None:
            await chat.add_error("会话存储尚未初始化。")
            return
        sessions = self._store.list_sessions()
        if not sessions:
            await chat.add_system("还没有保存过任何会话。", variant="warn")
            return
        self.query_one(CommandPalette).close()
        self.push_screen(
            SessionPicker(sessions),
            callback=self._on_session_picked,
        )

    def _on_session_picked(self, session_id: str | None) -> None:
        if session_id:
            self._resume_session_worker(session_id)

    @work(exclusive=True, group="session")
    async def _resume_session_worker(self, session_id: str) -> None:
        chat = self.query_one(ChatView)
        if self._store is None or self._agent is None:
            await chat.add_error("Agent 或会话存储尚未初始化。")
            return

        self._set_busy(True)
        try:
            data = self._store.load(session_id)
        except SessionError as exc:
            self._set_busy(False)
            await chat.add_error(f"恢复会话失败：{exc}")
            return

        try:
            self._agent.restore_session(data["messages"])
        except Exception as exc:
            self._set_busy(False)
            await chat.add_error(f"恢复会话失败：{exc}")
            return

        self._session_id = str(data.get("id") or session_id)
        self._session_created_at = str(data.get("created_at") or "")
        self._session_title = str(data.get("title") or "未命名会话")

        await self._reset_ui_context()
        await self._render_history(self._agent.history)
        await chat.add_system(
            f"已恢复会话：{escape(self._session_title)}"
            f"（{len(self._agent.history)} 条消息）"
        )
        self._set_busy(False)
        self._refresh_context_bar()
        self._refresh_sidebar()

    async def _render_history(self, messages: list[dict[str, Any]]) -> None:
        """把恢复出来的历史消息重新渲染到聊天区（工具结果不重复展示）。"""
        chat = self.query_one(ChatView)
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str):
                if content.startswith(AGENTS_MD_PREFIX):
                    await chat.add_system(
                        "[#8b949e]已注入项目规范 AGENTS.md[/]"
                    )
                elif content.strip():
                    await chat.add_user(content)
            elif role == "assistant" and isinstance(content, str):
                if content.strip():
                    widget = await chat.add_assistant()
                    widget.append_text(content, force=True)
            # role == tool 的历史结果体积可能很大，界面不重复展示；
            # 完整内容仍保存在会话 JSON 中，需要时可用 recallContext 检索。

    async def _command_status(self) -> None:
        chat = self.query_one(ChatView)
        if self._agent is None:
            await chat.add_error("Agent 尚未初始化。")
            return

        stats = self._agent.get_context_stats()
        default_tag = "【默认】" if stats.window_is_default else ""
        settings = getattr(self._agent, "settings", None) or self._settings
        loaded = self._agent.skills.loaded_names()

        mcp_line = "已禁用"
        if self._agent.mcp is not None:
            if not self._agent.mcp.sdk_available:
                mcp_line = "未安装 MCP SDK"
            else:
                servers = list(self._agent.mcp.servers.values())
                connected = sum(1 for item in servers if item.connected)
                mcp_line = (
                    f"{connected}/{len(servers)} 已连接 · "
                    f"工具 {len(self._agent.mcp.tool_names)} 个"
                )

        await chat.add_system(
            "[bold]运行状态[/]\n"
            f"[#8b949e]会话[/]  {escape(self._session_title)}"
            f"（{escape(self._session_id or '-')}）\n"
            f"[#8b949e]模型[/]  {escape(self._model_name)}\n"
            f"[#8b949e]沙箱[/]  {escape(self._workspace_hint)}\n"
            f"[#8b949e]上下文[/]  {stats.used} / {stats.window}{default_tag}"
            f"（{stats.percent:.1f}%）\n"
            f"[#8b949e]消息数[/]  {len(self._agent.history)}\n"
            f"[#8b949e]工具[/]  {len(self._agent.tools)} 个\n"
            f"[#8b949e]技能[/]  {len(self._agent.skills.skills)} 个"
            f"（已加载 {len(loaded)} 个）\n"
            f"[#8b949e]AGENTS.md[/]  "
            f"{'已注入' if self._agent.has_agents_md() else '未发现'}\n"
            f"[#8b949e]MCP[/]  {escape(mcp_line)}\n"
            f"[#8b949e]工具轮数上限[/]  "
            f"{getattr(settings, 'max_tool_rounds', '-')} 轮 / "
            f"{getattr(settings, 'max_tool_calls', '-')} 次"
        )

    async def _command_skills(self) -> None:
        chat = self.query_one(ChatView)
        if self._agent is None:
            await chat.add_error("Agent 尚未初始化。")
            return
        skills = self._agent.skills.available_skills()
        loaded = self._agent.skills.loaded_names()
        if not skills:
            await chat.add_system("未发现任何 Skill。")
        else:
            lines = [
                f"{'●' if skill.name in loaded else '○'} "
                f"[bold #5b8def]{escape(skill.name)}[/]  "
                f"[#8b949e]{escape(skill.description)}[/]"
                for skill in skills
            ]
            await chat.add_system(
                f"[bold]Agent Skills（{len(skills)} 个，● 已加载）[/]\n"
                + "\n".join(lines)
            )
        for warning in self._agent.skills.warnings:
            await chat.add_system(f"[#e3b341]技能警告：{escape(warning)}[/]")

    async def _command_mcp(self) -> None:
        chat = self.query_one(ChatView)
        if self._agent is None or self._agent.mcp is None:
            await chat.add_system("MCP 已禁用。")
            return
        manager = self._agent.mcp
        if not manager.sdk_available:
            await chat.add_system(
                "[#e3b341]未安装 MCP Python SDK。[/]\n"
                "安装命令：pip install mcp",
                variant="warn",
            )
            return
        lines = [escape(line) for line in manager.status_lines()]
        await chat.add_system("[bold]MCP 状态[/]\n" + "\n".join(lines))
        for warning in manager.warnings:
            await chat.add_system(
                f"[#e3b341]MCP 警告：{escape(warning)}[/]"
            )

    # ---------- 快捷键 ----------

    def action_cancel_turn(self) -> None:
        # Esc 优先关闭命令面板，其次才中止当前请求
        try:
            palette = self.query_one(CommandPalette)
            if palette.is_open:
                palette.close()
                return
        except Exception:
            pass

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
