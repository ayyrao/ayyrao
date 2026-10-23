"""Agent 对外事件协议 —— 界面层与核心层之间唯一的通信契约。

任何界面（TUI / Web / Desktop）都只需要消费 `AgentEvent` 流，
不需要了解工具、沙箱、OpenAI 协议等内部细节。
每个事件都提供 `to_dict()`，便于后续 WebSocket / JSON-RPC 序列化。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, ClassVar


@dataclass(slots=True)
class AgentEvent:
    """所有事件的基类。"""

    type: ClassVar[str] = "event"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **asdict(self)}


@dataclass(slots=True)
class RoundStarted(AgentEvent):
    """开始一轮模型推理（一轮内可能包含多次工具调用）。"""

    type: ClassVar[str] = "round_started"
    round: int


@dataclass(slots=True)
class TextDelta(AgentEvent):
    """模型流式输出的一个文本片段。"""

    type: ClassVar[str] = "text_delta"
    text: str


@dataclass(slots=True)
class ToolCallStarted(AgentEvent):
    """模型请求调用工具，即将执行。"""

    type: ClassVar[str] = "tool_call_started"
    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass(slots=True)
class ToolCallFinished(AgentEvent):
    """工具执行结束。ok=False 时 output 为错误说明。"""

    type: ClassVar[str] = "tool_call_finished"
    call_id: str
    name: str
    ok: bool
    output: str
    elapsed: float


@dataclass(slots=True)
class ErrorEvent(AgentEvent):
    """可向用户展示的错误。"""

    type: ClassVar[str] = "error"
    message: str


@dataclass(slots=True)
class TurnFinished(AgentEvent):
    """一轮用户请求处理结束。"""

    type: ClassVar[str] = "turn_finished"
    final_text: str
    rounds: int
    tool_calls: int
    interrupted: bool = False
