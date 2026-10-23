"""core —— Agent 核心层（无任何界面依赖）。

对外公开的稳定接口：
    Settings      配置
    Agent         Agent 主循环
    PathSandbox   路径安全沙箱
    ToolRegistry  工具注册表
    事件类         core.events
"""

from .agent import Agent
from .config import ConfigError, Settings
from .events import (
    AgentEvent,
    ErrorEvent,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from .security import PathSandbox, SecurityError
from .tools import build_default_registry
from .tools.registry import ToolError, ToolExecution, ToolRegistry, ToolSpec

__all__ = [
    "Agent",
    "AgentEvent",
    "ConfigError",
    "ErrorEvent",
    "PathSandbox",
    "RoundStarted",
    "SecurityError",
    "Settings",
    "TextDelta",
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolError",
    "ToolExecution",
    "ToolRegistry",
    "ToolSpec",
    "TurnFinished",
    "build_default_registry",
]
