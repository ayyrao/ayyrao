"""Agent Skills 动态加载。

对外提供：
    SkillLoader              发现 .agents/skills 并实现 loadSkill
    SkillInfo                单个技能的元数据
    LOAD_SKILL_SCHEMA        loadSkill 的 function-calling 声明
    FrontmatterError         Frontmatter 解析失败
    parse_frontmatter        解析 YAML Frontmatter（测试 / 复用）

目录约定与安全边界见 loader.py。本模块不依赖任何 UI 库，
CLI / TUI / Web 都可以复用同一套技能机制。
"""

from __future__ import annotations

from .frontmatter import FrontmatterError, parse_frontmatter, read_frontmatter_block
from .loader import (
    MAX_CONTENT_CHARS,
    MAX_READ_BYTES,
    SKILL_FILENAME,
    SKILLS_DIR_PARTS,
    SkillInfo,
    SkillLoader,
)
from .schemas import (
    LOAD_SKILL_DESCRIPTION,
    LOAD_SKILL_PARAMETERS,
    LOAD_SKILL_SCHEMA,
    SKILL_TOOL_SCHEMAS,
)

__all__ = [
    "FrontmatterError",
    "LOAD_SKILL_DESCRIPTION",
    "LOAD_SKILL_PARAMETERS",
    "LOAD_SKILL_SCHEMA",
    "MAX_CONTENT_CHARS",
    "MAX_READ_BYTES",
    "SKILLS_DIR_PARTS",
    "SKILL_FILENAME",
    "SKILL_TOOL_SCHEMAS",
    "SkillInfo",
    "SkillLoader",
    "parse_frontmatter",
    "read_frontmatter_block",
]
