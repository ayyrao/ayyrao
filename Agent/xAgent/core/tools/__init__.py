"""工具层：注册表 + 文件工具 + 联网工具 + 命令行工具。

10 个文件工具与 RunCli 使用 ToolRegistry 注册与分发；联网工具
（fetchUrl / searchWeb）由 agent.py 的显式分支调用，声明通过
WEB_TOOL_SCHEMAS 追加到全局 tools 列表。
"""

from __future__ import annotations

from ..security import PathSandbox
from .cli_tools import (
    CLI_TOOL_SCHEMAS,
    RUN_CLI_PARAMETERS,
    RUN_CLI_SCHEMA,
    CliTools,
    register_cli_tools,
    run_cli,
)
from .file_schemas import FILE_TOOL_DEFINITIONS, register_file_tools
from .file_tools import FileTools
from .registry import ToolError, ToolExecution, ToolRegistry, ToolSpec
from .web_tools import (
    FETCH_URL_SCHEMA,
    SEARCH_WEB_SCHEMA,
    WEB_TOOL_SCHEMAS,
    SearchConfig,
    WebTools,
    fetch_url,
    search_web,
)

__all__ = [
    "CLI_TOOL_SCHEMAS",
    "CliTools",
    "FETCH_URL_SCHEMA",
    "FILE_TOOL_DEFINITIONS",
    "FileTools",
    "PathSandbox",
    "RUN_CLI_PARAMETERS",
    "RUN_CLI_SCHEMA",
    "SEARCH_WEB_SCHEMA",
    "SearchConfig",
    "ToolError",
    "ToolExecution",
    "ToolRegistry",
    "ToolSpec",
    "WEB_TOOL_SCHEMAS",
    "WebTools",
    "build_default_registry",
    "fetch_url",
    "register_cli_tools",
    "register_file_tools",
    "run_cli",
    "search_web",
]


def build_default_registry(sandbox: PathSandbox) -> ToolRegistry:
    """构建默认注册表：10 个文件工具 + RunCli。

    联网工具不走注册表：它们的 schema 由 Agent 追加到全局 tools 列表，
    调用由 Agent 的显式分支分发，便于和作业要求的分发结构保持一致。
    """
    registry = ToolRegistry()
    register_file_tools(registry, FileTools(sandbox))
    register_cli_tools(registry)
    return registry
