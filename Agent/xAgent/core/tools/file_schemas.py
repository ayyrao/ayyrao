"""10 个文件工具的 function-calling 声明与注册。

工具声明与实现分开，便于单独审阅模型可见的契约。
"""

from __future__ import annotations

from typing import Any

from .file_tools import FileTools
from .registry import ToolRegistry

# (name, description, parameters)
FILE_TOOL_DEFINITIONS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "get_cwd",
        "获取当前工作目录（Agent 沙箱根目录）的绝对路径。",
        {"type": "object", "properties": {}, "required": []},
    ),
    (
        "list_dir",
        "列出指定目录下的所有文件和子目录。不传 path 时列出工作目录。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于工作目录的路径，默认为 .",
                }
            },
            "required": [],
        },
    ),
    (
        "get_file_type",
        "获取指定文件的类型（后缀名）。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于工作目录的文件路径",
                }
            },
            "required": ["path"],
        },
    ),
    (
        "read_text_file",
        "读取纯文本文件内容；二进制文件或非 UTF-8 文件会被拒绝。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于工作目录的文件路径",
                }
            },
            "required": ["path"],
        },
    ),
    (
        "create_file",
        "新建一个空文件，会自动创建缺失的父目录；文件已存在时拒绝执行。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要创建的文件路径（相对于工作目录）",
                }
            },
            "required": ["path"],
        },
    ),
    (
        "edit_text_file",
        "用新内容覆盖编辑已有的纯文本文件。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径（相对于工作目录）",
                },
                "content": {
                    "type": "string",
                    "description": "写入文件的完整新内容（会覆盖原内容）",
                },
            },
            "required": ["path", "content"],
        },
    ),
    (
        "make_dir",
        "创建新目录，父目录不存在时会自动创建。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要创建的目录路径（相对于工作目录）",
                }
            },
            "required": ["path"],
        },
    ),
    (
        "remove_dir",
        "删除空目录；非空目录会被拒绝删除。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的目录路径（相对于工作目录）",
                }
            },
            "required": ["path"],
        },
    ),
    (
        "remove_file",
        "删除指定文件。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的文件路径（相对于工作目录）",
                }
            },
            "required": ["path"],
        },
    ),
    (
        "rename_item",
        "重命名文件或目录；目标已存在时拒绝覆盖。",
        {
            "type": "object",
            "properties": {
                "old_path": {
                    "type": "string",
                    "description": "原路径（相对于工作目录）",
                },
                "new_path": {
                    "type": "string",
                    "description": "新路径（相对于工作目录）",
                },
            },
            "required": ["old_path", "new_path"],
        },
    ),
)


def register_file_tools(registry: ToolRegistry, tools: FileTools) -> None:
    """把 FileTools 的 10 个方法按声明注册进注册表。"""
    for name, description, parameters in FILE_TOOL_DEFINITIONS:
        registry.register(
            name=name,
            description=description,
            parameters=parameters,
            handler=getattr(tools, name),
        )
