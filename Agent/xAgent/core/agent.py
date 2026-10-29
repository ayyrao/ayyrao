"""Agent 核心：多轮对话 + function call 调度。

对外只暴露 `stream_turn()` 异步事件流：
    async for event in agent.stream_turn("列出当前目录"):
        ...

core 层不 import 任何 UI 库，也不做 print/input，因此可以被 CLI、
Textual TUI、WebSocket 服务或桌面程序同时复用。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .config import Settings
from .events import (
    AgentEvent,
    ErrorEvent,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from .llm.client import LLMClient
from .security import PathSandbox
from .tools import ToolError, ToolExecution, ToolRegistry, build_default_registry
from .tools.web_tools import WEB_TOOL_SCHEMAS, fetch_url, search_web

SYSTEM_PROMPT = """你是一个运行在本地沙箱中的文件管理与联网助手。

工作目录：{workspace}

规则：
1. 你只能操作工作目录以内的文件与目录，所有路径都使用相对于工作目录的路径。
2. 需要了解文件现状时先调用工具（get_cwd / list_dir 等）确认，不要凭空猜测。
3. 创建文件前先确认是否已存在；修改内容使用 edit_text_file。
4. remove_dir 只能删除空目录；rename_item 不会覆盖已存在的目标。
5. 需要最新信息或不知道具体网址时，先调用 searchWeb；拿到链接后用 fetchUrl 阅读正文。
6. 工具返回错误时，阅读 JSON 中的 error.type / error.message 并调整方案，不要重复同样的失败操作。
7. 执行完成后，用简洁的中文总结你做了什么，并给出来源链接（如有）。
"""


@dataclass(slots=True)
class _PendingCall:
    """流式返回中，一个工具调用的累积状态。"""

    id: str | None = None
    name: str = ""
    arguments: str = ""


def _safe_json_object(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class Agent:
    """文件管理 Agent。

    依赖注入点：
        settings  —— 运行配置
        sandbox   —— 路径沙箱（可替换为别的目录）
        registry  —— 工具注册表（可扩展新工具）
        client    —— LLM 客户端（可注入假客户端做测试）
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sandbox: PathSandbox | None = None,
        registry: ToolRegistry | None = None,
        client: LLMClient | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self.sandbox = sandbox or PathSandbox(self.settings.workspace)
        self.registry = registry or build_default_registry(self.sandbox)
        # 全局 tools 列表：原有文件工具 schema + 两个联网工具 schema
        self.tools: list[dict[str, Any]] = self.registry.schemas() + WEB_TOOL_SCHEMAS
        self._client = client or LLMClient(self.settings)
        self._owns_client = client is None
        self._system_prompt = SYSTEM_PROMPT.format(workspace=self.sandbox.root)
        self.messages: list[dict[str, Any]] = []
        self.reset()

    # ---------- 生命周期 ----------

    def reset(self) -> None:
        """清空上下文，仅保留 system prompt。"""
        self.messages = [{"role": "system", "content": self._system_prompt}]

    @property
    def history(self) -> list[dict[str, Any]]:
        """当前上下文快照（浅拷贝）。"""
        return list(self.messages)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------- 主循环 ----------

    async def stream_turn(self, user_input: str) -> AsyncIterator[AgentEvent]:
        """处理一次用户输入，产出完整的事件流。

        事件顺序：
            RoundStarted -> (TextDelta)* -> (ToolCallStarted -> ToolCallFinished)*
            -> RoundStarted -> ... -> TurnFinished
        """
        text_input = (user_input or "").strip()
        if not text_input:
            yield ErrorEvent("输入不能为空。")
            return

        # 快照用于异常/取消时回滚，避免留下半截 tool_calls 导致后续请求不合法
        snapshot = len(self.messages)
        self.messages.append({"role": "user", "content": text_input})

        rounds_done = 0
        tools_used = 0

        try:
            for round_no in range(1, self.settings.max_tool_rounds + 1):
                rounds_done = round_no
                yield RoundStarted(round_no)

                content_parts: list[str] = []
                pending: dict[int, _PendingCall] = {}

                async for delta in self._client.stream(
                    self.messages, self.tools
                ):
                    if delta.content:
                        content_parts.append(delta.content)
                        yield TextDelta(delta.content)

                    for call_delta in delta.tool_calls:
                        slot = pending.setdefault(call_delta.index, _PendingCall())
                        if call_delta.id:
                            slot.id = call_delta.id
                        if call_delta.name:
                            slot.name += call_delta.name
                        if call_delta.arguments:
                            slot.arguments += call_delta.arguments

                text = "".join(content_parts)

                # 模型没有请求工具 -> 本轮结束
                if not pending:
                    self.messages.append({"role": "assistant", "content": text})
                    yield TurnFinished(
                        final_text=text,
                        rounds=rounds_done,
                        tool_calls=tools_used,
                        interrupted=False,
                    )
                    return

                calls = self._finalize_calls(pending)

                # 协议要求：带 tool_calls 的 assistant 消息必须先入历史，
                # 随后才能追加对应的 role=tool 消息。
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": text,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                },
                            }
                            for call in calls
                        ],
                    }
                )

                for call in calls:
                    yield ToolCallStarted(
                        call_id=call.id,
                        name=call.name,
                        arguments=_safe_json_object(call.arguments),
                        raw_arguments=call.arguments,
                    )
                    execution = self._dispatch_tool(call.name, call.arguments)
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": execution.output,
                        }
                    )
                    tools_used += 1
                    yield ToolCallFinished(
                        call_id=call.id,
                        name=call.name,
                        ok=execution.ok,
                        output=execution.output,
                        elapsed=execution.elapsed,
                    )

                if rounds_done < self.settings.max_tool_rounds and self.settings.request_interval:
                    await asyncio.sleep(self.settings.request_interval)

            # 达到工具轮数上限
            notice = f"[已达到最大工具调用轮数 {self.settings.max_tool_rounds}，本轮停止]"
            self.messages.append({"role": "assistant", "content": notice})
            yield ErrorEvent(
                f"工具调用轮数过多（上限 {self.settings.max_tool_rounds} 轮），已停止本轮请求。"
            )
            yield TurnFinished(
                final_text="",
                rounds=rounds_done,
                tool_calls=tools_used,
                interrupted=True,
            )

        except asyncio.CancelledError:
            del self.messages[snapshot:]
            raise
        except Exception as exc:  # 网络/鉴权/解析异常都转成事件
            del self.messages[snapshot:]
            yield ErrorEvent(f"请求出错：{exc}")
            yield TurnFinished(
                final_text="",
                rounds=rounds_done,
                tool_calls=tools_used,
                interrupted=True,
            )

    async def run_turn(self, user_input: str) -> str:
        """便捷方法：忽略中间事件，只返回最终文本（适合脚本与测试）。"""
        final_text = ""
        async for event in self.stream_turn(user_input):
            if isinstance(event, TurnFinished):
                final_text = event.final_text
        return final_text

    # ---------- 工具分发 ----------

    def _dispatch_tool(
        self,
        name: str,
        raw_arguments: str | dict[str, Any] | None,
    ) -> ToolExecution:
        """按工具名分发调用。

        - fetchUrl / searchWeb：显式分支调用联网工具；
        - 其余工具（10 个文件工具）：交给 ToolRegistry 原有逻辑。
        """
        start = time.perf_counter()
        parsed, parse_error = self._parse_arguments(raw_arguments)
        if parse_error is not None:
            return ToolExecution(
                name,
                parsed,
                self._error_json("invalid_arguments", parse_error),
                False,
                time.perf_counter() - start,
            )

        try:
            if name == "fetchUrl":
                output = fetch_url(**parsed)
            elif name == "searchWeb":
                output = search_web(**parsed)
            else:
                # 文件工具保持原行为：由注册表统一解析、校验与收敛异常
                return self.registry.invoke(name, raw_arguments)
        except ToolError as exc:
            # web_tools 抛出的已经是 {"ok": false, "error": {...}} 结构
            return ToolExecution(
                name, parsed, str(exc), False, time.perf_counter() - start
            )
        except TypeError as exc:  # 模型给出的参数与函数签名不匹配
            return ToolExecution(
                name,
                parsed,
                self._error_json("invalid_arguments", f"工具参数错误：{exc}"),
                False,
                time.perf_counter() - start,
            )
        except Exception as exc:  # 兜底：任何工具异常都不能让 Agent 崩溃
            return ToolExecution(
                name,
                parsed,
                self._error_json("tool_error", f"工具执行异常：{exc}"),
                False,
                time.perf_counter() - start,
            )

        return ToolExecution(
            name, parsed, str(output), True, time.perf_counter() - start
        )

    @staticmethod
    def _parse_arguments(
        raw_arguments: str | dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None]:
        """把模型返回的 arguments 统一解析为 dict。"""
        if raw_arguments is None or raw_arguments == "":
            return {}, None
        if isinstance(raw_arguments, dict):
            return raw_arguments, None
        if not isinstance(raw_arguments, str):
            return {}, "工具参数必须是 JSON 对象。"
        try:
            data = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return {}, f"工具参数解析失败：{exc}"
        if not isinstance(data, dict):
            return {}, "工具参数必须是 JSON 对象。"
        return data, None

    @staticmethod
    def _error_json(error_type: str, message: str) -> str:
        """把分发层异常也转成 LLM 易读的结构化 JSON。"""
        return json.dumps(
            {"ok": False, "error": {"type": error_type, "message": message}},
            ensure_ascii=False,
        )

    @staticmethod
    def _finalize_calls(pending: dict[int, _PendingCall]) -> list[_PendingCall]:
        calls: list[_PendingCall] = []
        for index in sorted(pending):
            slot = pending[index]
            calls.append(
                _PendingCall(
                    id=slot.id or f"call_{uuid4().hex[:12]}",
                    name=(slot.name or "").strip(),
                    arguments=(slot.arguments or "").strip() or "{}",
                )
            )
        return calls
