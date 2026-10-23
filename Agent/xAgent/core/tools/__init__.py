"""工具层：注册表 + 文件工具。"""

from __future__ import annotations

from ..security import PathSandbox
from .file_schemas import FILE_TOOL_DEFINITIONS, register_file_tools
from .file_tools import FileTools
from .registry import ToolError, ToolExecution, ToolRegistry, ToolSpec

__all__ = [
    "FILE_TOOL_DEFINITIONS",
    "FileTools",
    "PathSandbox",
    "ToolError",
    "ToolExecution",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "register_file_tools",
]


def build_default_registry(sandbox: PathSandbox) -> ToolRegistry:
    """构建默认注册表（当前包含 10 个文件工具）。"""
    registry = ToolRegistry()
    register_file_tools(registry, FileTools(sandbox))
    return registry
