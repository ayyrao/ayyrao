"""loadSkill 的 Function Calling 声明。

schema 单独放在这里，便于和实现分开审阅；Agent 初始化时把
LOAD_SKILL_SCHEMA 追加到全局 tools 列表，调用由 _dispatch_tool 分发。
"""

from __future__ import annotations

LOAD_SKILL_DESCRIPTION = (
    "加载 Agent Skill 的完整说明或技能附件。只传 name 时返回该技能 SKILL.md "
    "的全文；同一技能在本次对话中只会真正加载一次，重复调用会返回 "
    "already_loaded=true 且不再重复正文。同时传 name 和 filename 时读取技能"
    "目录内的附件文件，不会重复加载 SKILL.md。filename 支持技能相对路径"
    "（如 references/rules.md）、项目相对路径（如 "
    ".agents/skills/<name>/references/rules.md）和绝对路径，所有路径最终必须"
    "位于该技能目录内部；只写文件名时会在技能目录内递归搜索，若找到多个"
    "同名文件则返回 candidates 列表，需要从中选择一个完整路径后再次调用。"
    "技能名称必须是 System Prompt 中列出的名称。"
)

LOAD_SKILL_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "技能名称，必须与 System Prompt 中列出的名称一致",
        },
        "filename": {
            "type": "string",
            "description": (
                "可选。技能目录内的附件文件名或路径，例如 "
                "'references/rules.md'、'scripts/demo.py'。只写文件名（如 "
                "'rules.md'）时会在技能目录内递归搜索；同名文件有多个时会返回"
                "候选列表。禁止访问技能目录以外的文件。"
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

LOAD_SKILL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "loadSkill",
        "description": LOAD_SKILL_DESCRIPTION,
        "parameters": LOAD_SKILL_PARAMETERS,
    },
}

SKILL_TOOL_SCHEMAS: list[dict[str, object]] = [LOAD_SKILL_SCHEMA]

__all__ = [
    "LOAD_SKILL_DESCRIPTION",
    "LOAD_SKILL_PARAMETERS",
    "LOAD_SKILL_SCHEMA",
    "SKILL_TOOL_SCHEMAS",
]
