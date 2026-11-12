"""Agent 核心：多轮对话 + function call 调度。

对外只暴露 `stream_turn()` 异步事件流：
    async for event in agent.stream_turn("列出当前目录"):
        ...

core 层不 import 任何 UI 库，也不做 print/input，因此可以被 CLI、
Textual TUI、WebSocket 服务或桌面程序同时复用。
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:  # 可选依赖：未安装 PyYAML 时使用内置降级解析器
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from .config import Settings
from .events import (
    AgentEvent,
    ContextUsage,
    ErrorEvent,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from .llm.client import LLMClient
from .mcp_client import MCPManager, configure_mcp_logging
from .security import PathSandbox
from .tools import (
    ToolError,
    ToolExecution,
    ToolRegistry,
    build_default_registry,
    run_cli,
)
from .tools.web_tools import WEB_TOOL_SCHEMAS, fetch_url, fetch_web, search_web


# =============================================================================
# 新增 ①：Agent Skills 动态加载
# 插入位置：全部原有 import 之后、SYSTEM_PROMPT 之前
#
# 包含：
#   - Frontmatter 只读解析（PyYAML 优先，缺失时降级）
#   - 扫描 <项目根>/.agents/skills
#   - System Prompt 技能清单拼接
#   - loadSkill：SKILL.md 去重加载、附件读取、路径隔离
#   - createSkill：新建 SKILL.md，校验名称与 Frontmatter，禁止覆盖
#   - LOAD_SKILL_SCHEMA / CREATE_SKILL_SCHEMA
# =============================================================================

SKILLS_DIR_PARTS: tuple[str, ...] = (".agents", "skills")
SKILL_FILENAME = "SKILL.md"

# =============================================================================
# AGENTS.md：一次注入，全程复用
#
# 会话首次初始化时读取项目根目录的 AGENTS.md，作为一条 user 消息追加到
# System Prompt 之后。内容缓存在 Agent 实例里，后续轮次和 reset() 都直接
# 复用这条消息，不再重复读文件，从而保持前缀稳定、提高缓存命中率。
# =============================================================================
AGENTS_MD_FILENAME = "AGENTS.md"
AGENTS_MD_PREFIX = "【项目规范 · AGENTS.md 自动注入】"
AGENTS_MD_MAX_BYTES = 256 * 1024
AGENTS_MD_TEMPLATE = (
    "{prefix}\n"
    "以下是项目根目录 AGENTS.md 中的项目规范与协作要求。"
    "它是对 System Prompt 的补充，不覆盖 System Prompt 与工具定义。"
    "请在本次会话的后续所有回复中遵守：\n\n"
    "{body}"
)

MAX_READ_BYTES = 1024 * 1024
MAX_CONTENT_CHARS = 100_000
MAX_PROMPT_DESCRIPTION_CHARS = 500
TRUNCATION_MARKER = "【内容已截断】"

MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_FRONTMATTER_LINES = 400

# createSkill：名称规则与 Agent Skills 规范一致（小写字母/数字/连字符，
# 不能以连字符开头或结尾，也不能出现连续连字符）。
SKILL_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_SKILL_NAME_RE = re.compile(SKILL_NAME_PATTERN)
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_CONTEXT_BYTES = MAX_READ_BYTES

_BLOCK_HEADER_RE = re.compile(r"^([|>])([+-]?)([1-9]?)$")
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


class SkillFrontmatterError(ValueError):
    """Frontmatter 缺失、格式错误或无法解析。"""


def read_skill_frontmatter(path: str | Path) -> str:
    """从 SKILL.md 头部读取 YAML Frontmatter 文本（不含分隔符）。

    只按行读取到结束分隔符为止，不会把 SKILL.md 正文全部读进内存。
    """
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8-sig", newline=None) as handle:
            first = handle.readline()
            if not first:
                raise SkillFrontmatterError("文件为空，缺少 YAML Frontmatter。")
            if first.strip() != "---":
                raise SkillFrontmatterError(
                    "文件开头不是 YAML Frontmatter（第一行必须为 ---）。"
                )

            lines: list[str] = []
            total_bytes = 0
            for _ in range(MAX_FRONTMATTER_LINES):
                line = handle.readline()
                if not line:
                    raise SkillFrontmatterError(
                        "YAML Frontmatter 缺少结束分隔符 ---。"
                    )
                total_bytes += len(line.encode("utf-8", errors="replace"))
                if total_bytes > MAX_FRONTMATTER_BYTES:
                    raise SkillFrontmatterError(
                        f"YAML Frontmatter 超过 {MAX_FRONTMATTER_BYTES} 字节上限。"
                    )
                if line.strip() in {"---", "..."}:
                    return "".join(lines)
                lines.append(line)

            raise SkillFrontmatterError(
                f"YAML Frontmatter 超过 {MAX_FRONTMATTER_LINES} 行上限。"
            )
    except UnicodeDecodeError as exc:
        raise SkillFrontmatterError("SKILL.md 不是合法的 UTF-8 文本。") from exc
    except OSError as exc:
        raise SkillFrontmatterError(f"读取 SKILL.md 失败：{exc}") from exc


def parse_skill_frontmatter(
    text: str, *, use_pyyaml: bool = True
) -> dict[str, Any]:
    """解析 Frontmatter：PyYAML 优先，缺失时使用内置最小解析器。"""
    if use_pyyaml and yaml is not None:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SkillFrontmatterError(f"YAML 解析失败：{exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise SkillFrontmatterError("YAML Frontmatter 必须是键值映射。")
        return data
    return _skill_parse_minimal(text)


def _skill_parse_minimal(text: str) -> dict[str, Any]:
    """内置最小 YAML 解析器，只覆盖 name / description 常见写法。"""
    data: dict[str, Any] = {}
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            continue

        key, separator, rest = line.partition(":")
        if not separator:
            continue
        key = key.strip().strip("'\"")
        if not key:
            continue
        rest = rest.strip()

        header = _BLOCK_HEADER_RE.match(rest)
        if header is not None:
            block_lines, index = _skill_read_block(lines, index)
            data[key] = _skill_fold_block(
                block_lines, header.group(1), header.group(2)
            )
            continue

        if rest.startswith(('"', "'")):
            data[key] = _skill_parse_quoted(rest)
        else:
            data[key] = _INLINE_COMMENT_RE.sub("", rest).strip()

    return data


def _skill_read_block(lines: list[str], start: int) -> tuple[list[str], int]:
    block: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        block.append(line)
        index += 1
    return block, index


def _skill_fold_block(block: list[str], style: str, chomp: str) -> str:
    non_empty = [line for line in block if line.strip()]
    indent = (
        min(len(line) - len(line.lstrip(" \t")) for line in non_empty)
        if non_empty
        else 0
    )
    stripped = [line[indent:] if line.strip() else "" for line in block]

    if style == "|":
        value = "\n".join(stripped)
    else:
        parts: list[str] = []
        buffer: list[str] = []
        for line in stripped:
            if line.strip():
                buffer.append(line.strip())
            else:
                if buffer:
                    parts.append(" ".join(buffer))
                    buffer = []
                parts.append("")
        if buffer:
            parts.append(" ".join(buffer))
        value = "\n".join(parts)

    if chomp != "+":
        value = value.rstrip("\n")
    return value


def _skill_parse_quoted(raw: str) -> str:
    if raw.startswith('"'):
        try:
            value = ast.literal_eval(raw)
            if isinstance(value, str):
                return value
        except (ValueError, SyntaxError):
            pass
        return raw.strip('"')
    if len(raw) >= 2 and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw.strip("'")


def _skill_fail(error_type: str, message: str, **details: object) -> None:
    """抛出结构化 ToolError，由 _dispatch_tool 原异常分支收敛。"""
    error: dict[str, object] = {"type": error_type, "message": message}
    error.update(details)
    raise ToolError(json.dumps({"ok": False, "error": error}, ensure_ascii=False))


def _skill_ok(**payload: object) -> str:
    return json.dumps({"ok": True,** payload}, ensure_ascii=False)


def _skill_one_line(text: str, limit: int = MAX_PROMPT_DESCRIPTION_CHARS) -> str:
    """把多行 description 折叠成单行，避免破坏 System Prompt 的列表结构。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _validate_skill_name(raw_name: object) -> str:
    """校验 createSkill 的 name 参数，返回规范化后的名称。"""
    if not isinstance(raw_name, str) or not raw_name.strip():
        _skill_fail(
            "invalid_arguments",
            "name 不能为空，必须由小写字母、数字和连字符组成。",
        )

    name = raw_name.strip()
    if len(name) > MAX_SKILL_NAME_CHARS:
        _skill_fail(
            "invalid_skill_name",
            f"Skill 名称最多 {MAX_SKILL_NAME_CHARS} 个字符。",
            name=name,
        )
    if not _SKILL_NAME_RE.fullmatch(name):
        _skill_fail(
            "invalid_skill_name",
            "Skill 名称只能由小写字母、数字和连字符组成，且不能以连字符"
            "开头或结尾，也不能出现连续连字符，例如 'weekly-report'。",
            name=name,
        )
    return name


def _validate_skill_context(
    raw_context: object, *, expected_name: str
) -> tuple[str, str]:
    """校验 createSkill 的 skillContext，返回 (frontmatter 文本, 正文)。

    只做规范校验，不修改内容；真正写入磁盘的是调用方传进来的原文。
    """
    if not isinstance(raw_context, str):
        _skill_fail("invalid_arguments", "skillContext 必须是字符串。")

    text = raw_context.removeprefix(chr(0xFEFF))
    if not text.strip():
        _skill_fail("invalid_arguments", "skillContext 不能为空。")

    size = len(text.encode("utf-8"))
    if size > MAX_SKILL_CONTEXT_BYTES:
        _skill_fail(
            "skill_context_too_large",
            f"skillContext 为 {size} 字节，超过 {MAX_SKILL_CONTEXT_BYTES} 字节上限。",
        )

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        _skill_fail(
            "invalid_skill_context",
            "skillContext 必须符合 Agent Skills 规范：第一行是 YAML "
            "Frontmatter 的起始分隔符 ---。",
        )

    closing: int | None = None
    frontmatter_bytes = 0
    for index in range(1, len(lines)):
        if index > MAX_FRONTMATTER_LINES:
            _skill_fail(
                "invalid_skill_context",
                f"YAML Frontmatter 超过 {MAX_FRONTMATTER_LINES} 行上限。",
            )
        line = lines[index]
        frontmatter_bytes += len(line.encode("utf-8", errors="replace"))
        if frontmatter_bytes > MAX_FRONTMATTER_BYTES:
            _skill_fail(
                "invalid_skill_context",
                f"YAML Frontmatter 超过 {MAX_FRONTMATTER_BYTES} 字节上限。",
            )
        if line.strip() in {"---", "..."}:
            closing = index
            break

    if closing is None:
        _skill_fail(
            "invalid_skill_context",
            "skillContext 的 YAML Frontmatter 缺少结束分隔符 ---。",
        )

    frontmatter_text = "".join(lines[1:closing])
    body = "".join(lines[closing + 1:])

    try:
        metadata = parse_skill_frontmatter(frontmatter_text)
    except SkillFrontmatterError as exc:
        _skill_fail("invalid_skill_context", f"YAML Frontmatter 解析失败：{exc}")

    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not name.strip():
        _skill_fail(
            "invalid_skill_context",
            "YAML Frontmatter 必须包含非空的 name 字段。",
        )
    if not isinstance(description, str) or not description.strip():
        _skill_fail(
            "invalid_skill_context",
            "YAML Frontmatter 必须包含非空的 description 字段。",
        )
    if name.strip() != expected_name:
        _skill_fail(
            "invalid_skill_context",
            f"Frontmatter 中的 name（{name.strip()!r}）必须与工具参数 "
            f"name（{expected_name!r}）完全一致。",
            expected=expected_name,
            actual=name.strip(),
        )
    if not body.strip():
        _skill_fail(
            "invalid_skill_context",
            "YAML Frontmatter 之后必须有非空正文，用于说明技能的使用场景和步骤。",
        )

    return frontmatter_text, body


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """一个已发现的 Skill；路径均为 resolve() 后的绝对路径。"""

    name: str
    description: str
    directory: Path
    skill_file: Path
    directory_name: str

class SkillLoader:
    """扫描 <项目根>/.agents/skills 并实现 loadSkill。
    project_root 默认取构造时的进程工作目录，即程序启动目录；
    初始化阶段只读取每个 SKILL.md 的 Frontmatter（name / description）。
    """
    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        skills_dir: str | Path | None = None,
    ) -> None:
        root = Path.cwd() if project_root is None else Path(project_root)
        self.project_root: Path = root.expanduser().resolve()
        if skills_dir is not None:
            self.skills_root = Path(skills_dir).resolve()
        else:
            self.skills_root = self.project_root / ".agents" / "skills"

        self.skills: dict[str, SkillInfo] = {}
        self.warnings: list[str] = []
        self._loaded: set[str] = set()

        self.discover()





    # ---------- 扫描 skills 目录 ----------

    def discover(self) -> None:
        """扫描 skills 根目录，只解析每个 SKILL.md 的 Frontmatter。"""
        self.skills = {}
        self.warnings = []
        self._loaded.clear()

        root = self.skills_root
        if not root.is_dir():
            return

        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            self.warnings.append(f"无法读取 skills 目录 {root}：{exc}")
            return

        for entry in children:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            self._register(entry, root)

    def _register(self, entry: Path, root: Path) -> None:
        """校验并注册单个 Skill 目录；异常只记 warning，不阻断启动。"""
        try:
            directory = entry.resolve()
        except OSError as exc:
            self.warnings.append(f"跳过 {entry.name}：目录无法解析（{exc}）。")
            return

        if not directory.is_relative_to(root.resolve()):
            self.warnings.append(
                f"跳过 {entry.name}：目录解析后位于 skills 根目录之外。"
            )
            return

        skill_file = self._find_skill_file(directory)
        if skill_file is None:
            self.warnings.append(
                f"跳过 {entry.name}：目录内没有 {SKILL_FILENAME}。"
            )
            return

        try:
            block = read_skill_frontmatter(skill_file)
            metadata = parse_skill_frontmatter(block)
        except SkillFrontmatterError as exc:
            self.warnings.append(f"跳过 {entry.name}：{exc}")
            return

        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not name.strip():
            self.warnings.append(
                f"跳过 {entry.name}：Frontmatter 缺少非空的 name 字段。"
            )
            return
        if not isinstance(description, str) or not description.strip():
            self.warnings.append(
                f"跳过 {entry.name}：Frontmatter 缺少非空的 description 字段。"
            )
            return

        name = name.strip()
        description = description.strip()

        if any(ch in "\r\n\t`" for ch in name):
            self.warnings.append(
                f"跳过 {entry.name}：name 含有非法字符（换行、制表符或反引号）。"
            )
            return

        existing = {skill.name.casefold() for skill in self.skills.values()}
        if name.casefold() in existing:
            self.warnings.append(
                f"跳过 {entry.name}：Skill 名称 {name!r} 与已注册技能重复。"
            )
            return

        self.skills[name] = SkillInfo(
            name=name,
            description=description,
            directory=directory,
            skill_file=skill_file,
            directory_name=entry.name,
        )

    @staticmethod
    def _find_skill_file(directory: Path) -> Path | None:
        """定位 SKILL.md，兼容大小写，并确保仍位于技能目录内。"""
        exact = directory / SKILL_FILENAME
        if exact.is_file():
            try:
                resolved = exact.resolve()
            except OSError:
                return None
            return resolved if resolved.is_relative_to(directory) else None

        try:
            children = list(directory.iterdir())
        except OSError:
            return None
        for child in children:
            if not child.is_file():
                continue
            if child.name.casefold() != SKILL_FILENAME.casefold():
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved.is_relative_to(directory):
                return resolved
        return None

    # ---------- System Prompt 注入 ----------

    def available_skills(self) -> list[SkillInfo]:
        return [self.skills[name] for name in sorted(self.skills, key=str.casefold)]

    def prompt_section(self) -> str:
        """生成注入 System Prompt 的 Skill 清单；没有技能时返回空字符串。"""
        skills = self.available_skills()
        if not skills:
            return ""

        lines = ["You have access to the following skills:"]
        for skill in skills:
            lines.append(f"- `{skill.name}`: {_skill_one_line(skill.description)}")
        lines.append("")
        lines.append(
            "Use the `loadSkill` tool to load the full instructions of a skill "
            "when needed. You can also load additional files referenced by a "
            "skill by passing the `filename` argument."
        )
        return "\n".join(lines)

    # ---------- 加载状态 ----------

    def reset_loaded(self) -> None:
        self._loaded.clear()

    def is_loaded(self, name: str) -> bool:
        info = self._find_skill(name)
        return info is not None and info.name.casefold() in self._loaded

    def loaded_names(self) -> set[str]:
        """返回本次会话中已真正加载过 SKILL.md 的技能名集合。"""
        return {
            info.name
            for info in self.skills.values()
            if info.name.casefold() in self._loaded
        }

    # ---------- loadSkill 入口 ----------

# ---------- loadSkill 入口 ----------
    def load_skill(self, name: str, filename: str | None = None) -> str:
        """仅 name 读 SKILL.md; name+filename 读附件；附件不改变加载状态。"""
        skill = self._get_skill(name)
        if filename is None or (
            isinstance(filename, str) and not filename.strip()
        ):
            return self._load_skill_md(skill)
        if not isinstance(filename, str):
            _skill_fail("invalid_arguments", "filename 必须是字符串。")
        return self._load_attachment(skill, filename.strip())

    def create_skill(self, name: str, skillContext: str) -> dict:
        # 名校验
        if not _SKILL_NAME_RE.fullmatch(name):
            return {
                "ok": False,
                "error": {
                    "type": "invalid_skill_name",
                    "message": "技能名校验失败：只能小写字母、数字、短横线，不能首尾横线、不能连续横线"
                }
            }
        # 内容大小校验
        if len(skillContext.encode("utf-8")) > MAX_SKILL_CONTEXT_BYTES:
            return {
                "ok": False,
                "error": {
                    "type": "invalid_skill_context",
                    "message": "skillContext 超出最大字节限制"
                }
            }
        # 校验YAML头
        if not skillContext.strip().startswith("---"):
            return {
                "ok": False,
                "error": {
                    "type": "invalid_skill_context",
                    "message": "skillContext 必须以 --- 开头，符合YAML Frontmatter规范"
                }
            }
        # 拼接路径 .agents/skills/name
        skill_dir = self.skills_root / name
        skill_md_path = skill_dir / SKILL_FILENAME
        # 判断是否已经存在，禁止覆盖
        if skill_md_path.exists():
            return {
                "ok": False,
                "error": {
                    "type": "skill_already_exists",
                    "message": f"技能 {name} 已存在，禁止覆盖"
                }
            }
        # 创建目录+写入文件
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md_path.write_text(skillContext, encoding="utf-8")
        return {
            "ok": True,
            "skill": name,
            "path": str(skill_md_path)
        }


        

    def _find_skill(self, raw_name: object) -> SkillInfo | None:
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None

        key = raw_name.strip()
        info = self.skills.get(key)
        if info is not None:
            return info

        matches = [
            skill
            for skill in self.skills.values()
            if skill.name.casefold() == key.casefold()
        ]
        return matches[0] if len(matches) == 1 else None

    def _get_skill(self, raw_name: object) -> SkillInfo:
        if not isinstance(raw_name, str) or not raw_name.strip():
            _skill_fail(
                "invalid_arguments",
                "name 不能为空，必须是系统提示中列出的技能名。",
            )

        info = self._find_skill(raw_name)
        if info is not None:
            return info

        _skill_fail(
            "skill_not_found",
            f"未找到 Skill：{raw_name.strip()}。请只使用系统提示中列出的技能名。",
            available=sorted(self.skills),
        )

    def _load_skill_md(self, skill: SkillInfo) -> str:
        key = skill.name.casefold()
        if key in self._loaded:
            return _skill_ok(
                skill=skill.name,
                filename=SKILL_FILENAME,
                already_loaded=True,
                message=(
                    "该 Skill 的 SKILL.md 已在本次对话中加载过，正文就在前面的"
                    "工具结果里。请直接参考，不要重复调用 loadSkill；"
                    "如需附件请传 filename。"
                ),
            )

        content, truncated = self._read_text(skill.skill_file)
        self._loaded.add(key)
        return _skill_ok(
            skill=skill.name,
            filename=SKILL_FILENAME,
            path=SKILL_FILENAME,
            content=content,
            content_chars=len(content),
            truncated=truncated,
        )

    def _load_attachment(self, skill: SkillInfo, filename: str) -> str:
        target = self._resolve_attachment(skill, filename)

        if target == skill.skill_file:
            return self._load_skill_md(skill)

        content, truncated = self._read_text(target)
        relative = self._display_path(skill, target)
        return _skill_ok(
            skill=skill.name,
            filename=relative,
            path=relative,
            content=content,
            content_chars=len(content),
            truncated=truncated,
        )

    # ---------- 附件路径解析与安全 ----------

    def _resolve_attachment(self, skill: SkillInfo, filename: str) -> Path:
        if "\x00" in filename:
            _skill_fail("invalid_filename", "filename 中包含非法空字符。")

        normalized = filename.replace("\\", "/").strip()
        if not normalized:
            _skill_fail("invalid_filename", "filename 不能为空。")

        path = Path(normalized).expanduser()

        if "/" not in normalized:
            # 仅文件名：技能根精确匹配 -> 递归搜索（大小写不敏感）
            exact = self._inside(path.name, skill.directory)
            if exact is not None and exact.is_file():
                return exact

            matches = self._search_by_name(skill.directory, path.name)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                _skill_fail(
                    "ambiguous_filename",
                    f"在 Skill {skill.name!r} 中找到 {len(matches)} 个同名文件"
                    f" {path.name!r}，请从 candidates 中选择一个完整路径，"
                    "再次调用 loadSkill 并传入 filename。",
                    candidates=[
                        self._display_path(skill, item) for item in matches
                    ],
                )
            _skill_fail(
                "file_not_found",
                f"Skill {skill.name!r} 中没有找到文件：{path.name}",
                skill=skill.name,
            )

        # 含目录：skill 相对 -> 项目相对 -> 绝对，精确匹配
        if path.is_absolute():
            candidates = [path]
        else:
            candidates = [
                skill.directory / path,
                self.project_root / path,
            ]

        blocked: list[str] = []
        for candidate in candidates:
            resolved = self._inside(candidate, skill.directory)
            if resolved is None:
                blocked.append(str(candidate))
                continue
            if resolved.is_file():
                return resolved

        if blocked and not any(
            self._inside(candidate, skill.directory) is not None
            for candidate in candidates
        ):
            _skill_fail(
                "path_outside_skill",
                "拒绝读取：文件必须位于当前 Skill 目录内部"
                f"（{self._display_path(skill, skill.directory)}）。",
                skill=skill.name,
                requested=filename,
            )

        _skill_fail(
            "file_not_found",
            f"Skill {skill.name!r} 中不存在该路径：{filename}。"
            "含目录的路径需要精确匹配；如果只记得文件名，可以只传文件名让工具递归搜索。",
            skill=skill.name,
        )

    @staticmethod
    def _inside(candidate: str | Path, directory: Path) -> Path | None:
        """resolve() 展开符号链接/junction 后，必须仍在技能目录内。"""
        try:
            resolved = Path(candidate).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        return resolved if resolved.is_relative_to(directory) else None

    @staticmethod
    def _search_by_name(directory: Path, basename: str) -> list[Path]:
        wanted = basename.casefold()
        matches: list[Path] = []
        seen: set[str] = set()

        for current, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames if not name.startswith(".")
            )
            for name in sorted(filenames):
                if name.casefold() != wanted:
                    continue
                try:
                    resolved = (Path(current) / name).resolve()
                except OSError:
                    continue
                if not resolved.is_relative_to(directory):
                    continue
                key = os.path.normcase(str(resolved))
                if key in seen:
                    continue
                seen.add(key)
                matches.append(resolved)

        matches.sort(key=lambda item: str(item).casefold())
        return matches

    # ---------- 文件读取 ----------

    @staticmethod
    def _read_text(path: Path) -> tuple[str, bool]:
        try:
            size = path.stat().st_size
            if size > MAX_READ_BYTES:
                _skill_fail(
                    "file_too_large",
                    f"文件过大（{size} 字节），超过 {MAX_READ_BYTES} 字节上限："
                    f"{path.name}",
                )
            data = path.read_bytes()
        except OSError as exc:
            _skill_fail("read_error", f"读取文件失败：{exc}")

        if b"\x00" in data:
            _skill_fail("binary_file", f"非文本文件，禁止读取：{path.name}")

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            _skill_fail(
                "not_utf8",
                f"文件不是 UTF-8 文本，禁止读取：{path.name}（{exc}）",
            )

        truncated = len(text) > MAX_CONTENT_CHARS
        if truncated:
            text = (
                text[:MAX_CONTENT_CHARS]
                + f"\n{TRUNCATION_MARKER}原始内容 {len(text)} 字符，"
                f"仅保留前 {MAX_CONTENT_CHARS} 字符。"
            )
        return text, truncated

    @staticmethod
    def _display_path(skill: SkillInfo, path: Path) -> str:
        try:
            return path.relative_to(skill.directory).as_posix() or "."
        except ValueError:
            return str(path)

    # ---------- createSkill 入口 ----------

    def create_skill(self, name: str, skillContext: str) -> str:
        """createSkill 工具实现：新建 <skills_root>/<name>/SKILL.md。

        安全约束：
        - name 只允许小写字母、数字和连字符，不能用来拼接出 ../；
        - 目标目录已存在时直接返回 skill_exists，绝不覆盖任何已有文件；
        - 目录创建与文件写入均使用“不覆盖”语义（mkdir 无 exist_ok、
          open("x")），并发场景下也不会覆盖；
        - 只注册这一个新技能，不调用 discover()，因此不会清空已有技能的
          “已加载”状态。
        """
        skill_name = _validate_skill_name(name)
        _validate_skill_context(skillContext, expected_name=skill_name)

        root = self.skills_root
        skill_dir = root / skill_name

        try:
            root_resolved = root.resolve()
            target = skill_dir.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            _skill_fail("write_error", f"无法解析技能目录路径：{exc}")

        if not target.is_relative_to(root_resolved):
            _skill_fail(
                "path_outside_skills",
                "拒绝创建：目标目录解析后位于 .agents/skills 之外。",
                path=str(skill_dir),
            )

        # 目录已存在：直接返回提示，禁止覆盖
        if target.exists():
            _skill_fail(
                "skill_exists",
                f"Skill {skill_name!r} 已存在，createSkill 不会覆盖任何已有文件。"
                "如需修改，请使用 read_text_file / edit_text_file 等 fileOps "
                "工具；如需新建，请换一个 name。",
                skill=skill_name,
                path=self._relative_to_project(target),
            )

        # 同名 Skill 可能来自其他目录：避免 Frontmatter name 冲突
        if self._find_skill(skill_name) is not None:
            _skill_fail(
                "skill_name_conflict",
                f"Skill 名称 {skill_name!r} 已被另一个目录注册，不能重复创建。"
                "请换一个 name。",
                skill=skill_name,
                available=sorted(self.skills),
            )

        try:
            root.mkdir(parents=True, exist_ok=True)
            # exist_ok=False：检查与创建之间若有并发创建，也不会覆盖
            skill_dir.mkdir(exist_ok=False)
        except FileExistsError:
            _skill_fail(
                "skill_exists",
                f"Skill 目录 {skill_name!r} 已存在，createSkill 不会覆盖任何"
                "已有文件。",
                skill=skill_name,
                path=self._relative_to_project(skill_dir),
            )
        except OSError as exc:
            _skill_fail(
                "write_error", f"创建 Skill 目录失败：{exc}", skill=skill_name
            )

        skill_file = skill_dir / SKILL_FILENAME
        try:
            # "x" 模式：即使并发创建也绝不覆盖已有 SKILL.md
            handle = skill_file.open("x", encoding="utf-8", newline="\n")
        except FileExistsError:
            _skill_fail(
                "skill_exists",
                f"SKILL.md 已存在于 {self._relative_to_project(skill_dir)}，"
                "createSkill 不会覆盖任何已有文件。",
                skill=skill_name,
                path=self._relative_to_project(skill_file),
            )
        except OSError as exc:
            # 本次刚创建的空目录可以安全删除
            try:
                skill_dir.rmdir()
            except OSError:
                pass
            _skill_fail(
                "write_error", f"创建 SKILL.md 失败：{exc}", skill=skill_name
            )

        try:
            with handle:
                handle.write(skillContext)
        except OSError as exc:
            # 只回滚本次自己创建的文件与目录
            try:
                skill_file.unlink(missing_ok=True)
                skill_dir.rmdir()
            except OSError:
                pass
            _skill_fail(
                "write_error", f"写入 SKILL.md 失败：{exc}", skill=skill_name
            )

        # 只注册这一个新技能；不调用 discover()，避免清空 _loaded 状态
        warning_start = len(self.warnings)
        self._register(skill_dir, root)
        registered = skill_name in self.skills
        registration_warnings = self.warnings[warning_start:]

        if registered:
            message = (
                "Skill 创建成功，SKILL.md 已写入并注册到当前会话。"
                "现在可以调用 loadSkill 读取完整说明；如需 references/、"
                "scripts/ 等附件，请继续使用 create_file / make_dir 等 "
                "fileOps 工具。"
            )
        else:
            message = (
                "SKILL.md 已写入，但本次会话未能注册（详见 warnings）。"
                "文件已经创建，请勿重复调用 createSkill；重启 Agent 后会"
                "重新扫描并加载该技能。"
            )

        return _skill_ok(
            created=True,
            skill=skill_name,
            directory=self._relative_to_project(skill_dir),
            path=self._relative_to_project(skill_file),
            registered=registered,
            warnings=registration_warnings,
            message=message,
        )

    def _relative_to_project(self, path: Path) -> str:
        """回显路径：优先相对项目根，其次原样返回绝对路径。"""
        try:
            return path.relative_to(self.project_root).as_posix()
        except ValueError:
            return str(path)


LOAD_SKILL_DESCRIPTION = (
    "加载 Agent Skill 的完整说明或技能附件。只传 name 时返回该技能 SKILL.md "
    "的全文；同一技能在本次对话中只会真正加载一次，重复调用返回 "
    "already_loaded=true 且不再重复正文。同时传 name 和 filename 时读取技能"
    "目录内的附件，不会重复加载 SKILL.md。filename 支持技能相对路径、项目"
    "相对路径和绝对路径，最终必须位于该技能目录内部；只写文件名时会在技能"
    "目录内递归搜索，同名文件有多个时返回 candidates 列表。技能名称必须是 "
    "System Prompt 中列出的名称。"
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
                "'references/rules.md'。只写文件名时递归搜索；同名文件有多个"
                "时返回候选列表。禁止访问技能目录以外的文件。"
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
CREATE_SKILL_DESCRIPTION = (
"创建一个新的 Agent Skill。会在项目根目录的 .agents/skills/<name>/ 下"
"新建 SKILL.md，并原样写入 skillContext。name 只能由小写字母、数字和"
"连字符组成，且不能以连字符开头/结尾或出现连续连字符。skillContext 必须"
"是完整的 SKILL.md 内容：第一行是 --- 开始的 YAML Frontmatter，"
"Frontmatter 必须包含 name 和 description，其中 name 必须与参数 name "
"完全一致；Frontmatter 结束分隔符 --- 之后必须有非空正文。如果同名目录"
"已经存在，本工具只返回错误提示，绝不会覆盖任何已有文件。references/"
"scripts/ 等附件不在本工具中创建；创建成功后请继续使用 create_file / "
"make_dir 等 fileOps 工具。"
)

CREATE_SKILL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "createSkill",
        "description": CREATE_SKILL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "required": ["name", "skillContext"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名称，小写字母、数字、短横线，不能首尾横线、不能连续横线"
                },
                "skillContext": {
                    "type": "string",
                    "description": "完整SKILL.md文本，以---开头，包含yaml frontmatter"
                }
            },
            "additionalProperties": False,
        },
    },
}


CREATE_SKILL_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Skill 名称，只能由小写字母、数字和连字符组成，例如 "
                "'weekly-report'；不能以连字符开头/结尾，也不能出现连续连字符。"
            ),
            "pattern": SKILL_NAME_PATTERN,
            "maxLength": MAX_SKILL_NAME_CHARS,
        },
        "skillContext": {
            "type": "string",
            "description": (
                "要写入 SKILL.md 的完整内容。必须以 YAML Frontmatter 开头"
                "（第一行为 ---），Frontmatter 内必须包含 name 和 description，"
                "name 与参数 name 一致；结束分隔符 --- 之后必须是正文。"
            ),
        },
    },
    "required": ["name", "skillContext"],
    "additionalProperties": False,
}

CREATE_SKILL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "createSkill",
        "description": CREATE_SKILL_DESCRIPTION,
        "parameters": CREATE_SKILL_PARAMETERS,
    },
}

# =============================================================================
# 新增 ① 结束
# =============================================================================


# =============================================================================
# 新增 ②：上下文压缩 + Token 监控 + recallContext 历史检索
#
# 插入位置：Skill 模块结束之后、SYSTEM_PROMPT 之前
#
# 设计目标（全部使用 Python 标准库，不引入 embedding / 向量数据库）：
#   1. /compact：归档原始上下文 -> LLM 生成摘要 -> 用摘要替换对话历史
#   2. Token 监控：优先使用 API 返回的 usage，缺失时用简单字符估算
#   3. recallContext：在 .agents/context/*.jsonl 中做纯字符串关键词检索
#
# 重要：本段代码与上面的 Skill 模块完全独立，不修改任何 Skill 逻辑。
# =============================================================================

CONTEXT_ARCHIVE_SUFFIX = ".jsonl"
CONTEXT_ARCHIVE_ID_RE = re.compile(r"^\d{8}-\d{6}$")

# 上下文窗口、压缩阈值等参数统一由 core.config.Settings 提供，
# 不再在 Agent 中保留第二份硬编码默认值。

# recallContext 的输出上限：匹配太多时截断，避免把上下文再次撑爆
RECALL_MAX_MATCHES = 20
RECALL_MAX_CHARS = 6000
RECALL_SNIPPET_RADIUS = 80
RECALL_MAX_FILE_BYTES = 20 * 1024 * 1024
# TUI 双栏面板左侧“压缩前原文”的显示上限；只影响显示，不影响归档完整性
COMPACT_PANEL_MAX_CHARS = 200_000

# 压缩摘要的固定 Prompt（一字不改使用）
COMPACT_PROMPT = """你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制，需要将历史对话压缩为一份高质量的工作摘要，以便 Agent 在新的上下文中继续执行任务。

请认真阅读提供的历史上下文，提取对后续工作仍然有价值的信息，并生成一份约 5,000 Tokens 的 Markdown 摘要。

摘要应重点保留以下内容：
1. **用户目标与要求**：用户希望完成什么任务？有哪些必须遵守的约束、偏好和验收条件？
2. **已经完成的工作**：哪些任务已经完成？实现了哪些功能？获得了哪些已经验证的结果？
3. **当前工作状态**：Agent 正在处理什么任务？当前进展到哪一步？是否存在尚未解决的问题？
4. **关键技术信息**：保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。
5. **关键决策与原因**：记录用户和 Agent 已经确定的技术方案，以及重要的设计取舍。
6. **下一步任务**：明确接下来应该执行的操作，以及需要继续验证的事项。

请遵守以下要求：
- 优先保留对后续任务有直接影响的信息，删除寒暄、重复讨论、过时的尝试和无关内容。
- 对已经验证的事实与尚未验证的推测作出明确区分。
- 不要将计划完成的任务描述为已经完成。
- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。
- 对失败的尝试，只保留有助于避免重复错误的信息。
- 如果信息存在冲突，应优先保留用户最新确认的要求，并简要说明仍然存在的冲突。
- 如果某些历史细节无法在摘要中完整保留，可简要说明它们已保存到原始上下文归档中，后续可以通过 recallContext 工具重新检索。
- 使用清晰的 Markdown 结构输出摘要，不要附加解释或无关内容。

最终目标：使一个从未阅读过原始对话的 Agent，仅凭这份摘要，就能够理解当前任务、继承关键决策，并继续开展工作。"""

# 摘要末尾强制追加的固定文本；{} 由归档时间 / 归档 id 替换
COMPACT_FOOTER_TEMPLATE = (
    "> 此前上下文已于 {archive_time} 完成压缩，原始上下文保存在编号为 "
    "{archive_id} 的归档文件中。如需查找历史信息，请使用 recallContext 工具。"
)


class ContextArchiveError(RuntimeError):
    """归档目录、归档文件或检索过程出错。"""


@dataclass(slots=True)
class ContextStats:
    """一次上下文 Token 统计结果。"""

    used: int
    window: int
    percent: float
    window_is_default: bool
    source: str = "estimate"  # "api" 表示锚点来自接口，"estimate" 表示本地估算


@dataclass(slots=True)
class CompactResult:
    """/compact 的返回值。ok=False 时调用方必须保留原始上下文。"""

    ok: bool
    message: str = ""
    error: str = ""
    archive_id: str = ""
    archive_path: str = ""
    archive_time: str = ""
    original_text: str = ""
    summary: str = ""


def estimate_text_tokens(text: str) -> int:
    """不依赖第三方库的粗略 Token 估算。

    规则（对中文偏保守，宁可提前提醒用户）：
    - ASCII 字符约 4 个 = 1 token；
    - 中文等非 ASCII 字符约 1 个 = 1 token。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_chars = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + other_chars)


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算消息列表的 token：按 JSON 序列化后估算，并加一点结构开销。"""
    total = 0
    for message in messages:
        try:
            payload = json.dumps(message, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(message)
        total += estimate_text_tokens(payload) + 4
    return total


def _estimate_tools_tokens(tools: list[dict[str, Any]]) -> int:
    """估算工具定义占用的 token（工具 schema 也会进入模型上下文）。"""
    if not tools:
        return 0
    try:
        payload = json.dumps(tools, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(tools)
    return estimate_text_tokens(payload)


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    """把历史消息整理成 LLM 易读的纯文本，供生成摘要使用。

    这里不做截断：摘要必须基于完整历史；归档文件才是最终兜底。
    """
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = message.get("role", "unknown")
        lines.append(f"\n--- 消息 {index} | role={role} ---")
        content = message.get("content")
        if content is not None:
            lines.append(f"content:\n{content}")
        tool_calls = message.get("tool_calls")
        if tool_calls:
            lines.append(
                "tool_calls:\n"
                + json.dumps(tool_calls, ensure_ascii=False, indent=2)
            )
        if message.get("tool_call_id"):
            lines.append(f"tool_call_id: {message['tool_call_id']}")
    return "\n".join(lines)


def format_messages_for_display(
    messages: list[dict[str, Any]], limit: int = COMPACT_PANEL_MAX_CHARS
) -> str:
    """把消息格式化成 TUI 左栏可读文本；超过 limit 时只截断显示。"""
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = message.get("role", "unknown")
        lines.append(f"===== [{index}] {role} =====")
        content = message.get("content")
        if content is not None:
            lines.append(str(content))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            lines.append(
                "tool_calls: "
                + json.dumps(tool_calls, ensure_ascii=False, indent=2)
            )
        if message.get("tool_call_id"):
            lines.append(f"tool_call_id: {message['tool_call_id']}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > limit:
        text = (
            text[:limit]
            + f"\n\n【显示已截断】完整内容共 {len(text)} 字符，"
            "已保存到归档文件，可用 recallContext 检索。"
        )
    return text


def _context_json_error(error_type: str, message: str, **extra: object) -> str:
    """生成 recallContext 的结构化错误 JSON。"""
    error: dict[str, object] = {"type": error_type, "message": message}
    error.update(extra)
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


def _record_search_text(record: dict[str, Any]) -> str:
    """把一条归档记录拼成用于关键词匹配的文本。"""
    parts: list[str] = []
    role = record.get("role")
    if role:
        parts.append(f"role: {role}")
    content = record.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))
    tool_calls = record.get("tool_calls")
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False))
    tool_return = record.get("tool_return")
    if isinstance(tool_return, str):
        parts.append(tool_return)
    return "\n".join(parts)


def _make_snippet(text: str, position: int, query_len: int) -> str:
    """截取关键词前后的一小段文本，作为命中片段返回。"""
    start = max(0, position - RECALL_SNIPPET_RADIUS)
    end = min(len(text), position + query_len + RECALL_SNIPPET_RADIUS)
    snippet = text[start:end].replace("\n", " ")
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


class ContextArchive:
    """管理 <项目根>/.agents/context/ 下的 jsonl 归档。

    归档格式：一行一条消息，字段固定包含
    index / role / content / tool_calls / tool_return，
    tool 消息额外保存 tool_call_id，保证可以完整还原原始对话。
    """

    def __init__(
        self,
        project_root: str | Path,
        *,
        archive_dir: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        # 归档目录可由 config.ini 的 [paths] context_archive_dir 覆盖；
        # 未配置时保持原有约定：<项目根>/.agents/context
        self.archive_dir = (
            Path(archive_dir).expanduser().resolve()
            if archive_dir is not None
            else self.project_root / ".agents" / "context"
        )

    # ---------- 目录与归档 ----------

    def ensure_dir(self) -> Path:
        """确保归档目录存在（程序自动创建，无需手动 mkdir）。"""
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ContextArchiveError(
                f"无法创建归档目录 {self.archive_dir}：{exc}"
            ) from exc
        if not self.archive_dir.is_dir():
            raise ContextArchiveError(
                f"归档路径不是目录：{self.archive_dir}"
            )
        return self.archive_dir

    def archive_messages(self, messages: list[dict[str, Any]]) -> tuple[str, str, str]:
        """把消息列表写入新归档。

        返回 (archive_id, 绝对路径, 可读归档时间)。
        使用 "x" 模式打开文件：绝不覆盖已有归档。
        """
        directory = self.ensure_dir()
        now = datetime.now()
        archive_id = now.strftime("%Y%m%d-%H%M%S")
        archive_time = now.strftime("%Y-%m-%d %H:%M:%S")
        path = directory / f"{archive_id}{CONTEXT_ARCHIVE_SUFFIX}"

        if path.exists():
            raise ContextArchiveError(
                f"归档文件 {path.name} 已存在（同一秒内重复压缩），请稍后重试。"
            )

        lines: list[str] = []
        for index, message in enumerate(messages, start=1):
            role = message.get("role")
            content = message.get("content")
            record: dict[str, Any] = {
                "index": index,
                "role": role,
                "content": content,
                "tool_calls": message.get("tool_calls"),
                # tool_return 与 content 相同，单独保存是为了让检索/还原更直观
                "tool_return": content if role == "tool" else None,
            }
            if "tool_call_id" in message:
                record["tool_call_id"] = message.get("tool_call_id")
            if "name" in message:
                record["name"] = message.get("name")
            lines.append(json.dumps(record, ensure_ascii=False))

        payload = "\n".join(lines) + "\n"
        try:
            # "x"：文件已存在则抛 FileExistsError，保证不覆盖旧归档
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ContextArchiveError(
                f"归档文件 {path.name} 已存在，拒绝覆盖。"
            ) from exc
        except OSError as exc:
            # 写入失败时删除本次创建的不完整文件，避免留下坏归档
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ContextArchiveError(f"写入归档文件失败：{exc}") from exc

        try:
            if path.stat().st_size <= 0:
                raise ContextArchiveError("归档文件写入后为空。")
        except OSError as exc:
            raise ContextArchiveError(f"无法校验归档文件：{exc}") from exc

        return archive_id, str(path), archive_time

    # ---------- 检索 ----------

    def search(self, text: str, archive_id: str | None = None) -> str:
        """recallContext 的实际实现：纯字符串子串匹配，不做语义检索。"""
        if not isinstance(text, str):
            return _context_json_error(
                "invalid_arguments", "text 必须是字符串。"
            )
        if archive_id is not None and not isinstance(archive_id, str):
            return _context_json_error(
                "invalid_arguments", "id 必须是字符串或省略。"
            )

        query = text.strip()
        if not query:
            return _context_json_error(
                "invalid_arguments", "text 不能为空，请提供要检索的关键词。"
            )

        try:
            paths = self._find_files(archive_id)
        except ContextArchiveError as exc:
            return _context_json_error("recall_error", str(exc))

        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        warnings: list[str] = []
        truncated = False
        total_chars = 0
        searched_files = 0

        for path in paths:
            searched_files += 1
            try:
                if path.stat().st_size > RECALL_MAX_FILE_BYTES:
                    warnings.append(
                        f"{path.name} 超过 {RECALL_MAX_FILE_BYTES} 字节，已跳过。"
                    )
                    continue
                with path.open(
                    "r", encoding="utf-8", errors="replace"
                ) as handle:
                    for line_no, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            warnings.append(
                                f"{path.name} 第 {line_no} 行不是合法 JSON，已跳过。"
                            )
                            continue
                        if not isinstance(record, dict):
                            continue

                        haystack = _record_search_text(record)
                        position = haystack.casefold().find(needle)
                        if position < 0:
                            continue

                        snippet = _make_snippet(haystack, position, len(query))
                        matches.append(
                            {
                                "archive_id": path.stem,
                                "line": line_no,
                                "message_index": record.get("index"),
                                "role": record.get("role"),
                                "position": position,
                                "snippet": snippet,
                            }
                        )
                        total_chars += len(snippet)

                        # 防止匹配太多把上下文再次撑爆
                        if (
                            len(matches) >= RECALL_MAX_MATCHES
                            or total_chars >= RECALL_MAX_CHARS
                        ):
                            truncated = True
                            break
            except OSError as exc:
                warnings.append(f"读取 {path.name} 失败：{exc}")

            if truncated:
                break

        result: dict[str, Any] = {
            "ok": True,
            "query": query,
            "archive_id": archive_id or None,
            "searched_files": searched_files,
            "match_count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }
        if truncated:
            result["note"] = (
                f"匹配结果超过上限（{RECALL_MAX_MATCHES} 条或 "
                f"{RECALL_MAX_CHARS} 字符），已截断。请使用更精确的关键词，"
                "或通过 id 参数只检索某个归档。"
            )
        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, ensure_ascii=False)

    def _find_files(self, archive_id: str | None) -> list[Path]:
        """定位归档文件；传 id 时只返回该文件。"""
        directory = self.ensure_dir()

        if archive_id is not None and str(archive_id).strip():
            clean = str(archive_id).strip()
            if not CONTEXT_ARCHIVE_ID_RE.fullmatch(clean):
                raise ContextArchiveError(
                    "id 格式错误，应为 YYYYMMDD-HHMMSS，"
                    "例如 20261106-153012。"
                )
            path = directory / f"{clean}{CONTEXT_ARCHIVE_SUFFIX}"
            if not path.is_file():
                raise ContextArchiveError(
                    f"未找到归档 {clean}。请检查 id 是否正确，"
                    "或不传 id 检索全部归档。"
                )
            # 防御路径穿越：解析后必须仍在归档目录内
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ContextArchiveError("归档路径越界，已拒绝访问。")
            return [path]

        try:
            files = sorted(
                directory.glob(f"*{CONTEXT_ARCHIVE_SUFFIX}"),
                key=lambda item: item.name,
                reverse=True,  # 新归档优先
            )
        except OSError as exc:
            raise ContextArchiveError(f"读取归档目录失败：{exc}") from exc
        return [item for item in files if item.is_file()]


def _context_tools_schema() -> dict[str, object]:
    """recallContext 的工具声明（与 loadSkill / createSkill 并列注册）。"""
    return {
        "type": "function",
        "function": {
            "name": "recallContext",
            "description": (
                "在历史上下文归档中检索原始对话记录。当压缩摘要中的信息不足，"
                "需要找回具体的历史消息、工具调用参数或工具返回结果时使用。"
                "检索方式为纯字符串关键词匹配，不做语义/向量检索。"
                "不传 id 时检索 .agents/context/ 下的全部归档；传 id 时只检索"
                "该归档。结果过多时会自动截断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "可选。归档编号，格式 YYYYMMDD-HHMMSS，"
                            "例如 20261106-153012。"
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "必填。要检索的关键词，按原样子串匹配。",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }


RECALL_CONTEXT_SCHEMA: dict[str, object] = _context_tools_schema()


SYSTEM_PROMPT = """你是一个运行在本地沙箱中的文件管理与联网助手。

工作目录：{workspace}

规则：
1. 你只能操作工作目录以内的文件与目录，所有路径都使用相对于工作目录的路径。
2. 需要了解文件现状时先调用工具（get_cwd / list_dir 等）确认，不要凭空猜测。
3. 创建文件前先确认是否已存在；修改内容使用 edit_text_file。
4. remove_dir 只能删除空目录；rename_item 不会覆盖已存在的目标。
5. 需要最新信息或不知道具体网址时，先调用 searchWeb；拿到链接后用 fetchWeb（或 fetchUrl）阅读正文。
6. 需要运行本地命令行程序时使用 RunCli：command 传程序名，args 传参数列表，不要拼接 shell 字符串。
7. 工具返回错误时，阅读 JSON 中的 error.type / error.message 并调整方案，不要重复同样的失败操作。
8. 执行完成后，用简洁的中文总结你做了什么，并给出来源链接（如有）。
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
        project_root: str | Path | None = None,
        mcp_config_path: str | Path | None = None,
        enable_mcp: bool = True,
    ) -> None:
        self.settings = settings or Settings.load()
        self.sandbox = sandbox or PathSandbox(self.settings.workspace)
        self.registry = registry or build_default_registry(self.sandbox)
        # 全局 tools 列表：文件工具 + RunCli（均来自注册表）+ 两个联网工具
        self.tools: list[dict[str, Any]] = self.registry.schemas() + WEB_TOOL_SCHEMAS
        self._client = client or LLMClient(self.settings)
        self._owns_client = client is None
        self._system_prompt = SYSTEM_PROMPT.format(workspace=self.sandbox.root)

        # =====================================================================
        # 新增 ②：初始化 SkillLoader、追加 loadSkill 工具、注入技能清单
        # 插入位置：self.tools 与 self._system_prompt 赋值完成之后、reset() 之前
        # 说明：原类属性名为 _system_prompt，这里沿用，避免破坏原有 reset 逻辑
        # =====================================================================
        # 配置文件中显式给出了 skills_dir 才覆盖；否则沿用
        # <project_root>/.agents/skills，便于测试把根目录指向临时目录。
        self.skills = SkillLoader(
            project_root=project_root,
            skills_dir=(
                None
                if self.settings.skills_dir_is_default
                else self.settings.skills_dir
            ),
        )
        self.tools = self.tools + [LOAD_SKILL_SCHEMA, CREATE_SKILL_SCHEMA]
        skill_section = self.skills.prompt_section()
        if skill_section:
            self._system_prompt = f"{self._system_prompt}\n\n{skill_section}"

        # =====================================================================
        # 新增 ②：上下文压缩 / Token 监控 / recallContext 初始化
        # 只追加状态与工具，不修改上面已有的 Skill 代码
        # =====================================================================
        self.tools = self.tools + [RECALL_CONTEXT_SCHEMA]
        self.context_archive = ContextArchive(
            self.skills.project_root,
            archive_dir=(
                None
                if self.settings.context_archive_dir_is_default
                else self.settings.context_archive_dir
            ),
        )
        # 上下文窗口与压缩阈值全部来自 Settings（config.ini + 默认值）
        self._context_window = self.settings.context_window
        self._window_is_default = self.settings.context_window_is_default
        self._compact_ratio = self.settings.compact_threshold
        self._context_locked = False   # 达到阈值后锁定普通输入
        self._compacting = False       # 压缩流水线执行中
        # (锚点 token 数, 锚点对应的消息下标)；用于优先采用 API usage
        self._api_token_anchor: tuple[int, int] | None = None

        # =====================================================================
        # Task 5：AGENTS.md 一次注入，全程复用
        # 在 __init__ 中只读一次文件；reset()/restore_session() 直接复用缓存。
        # =====================================================================
        self.agents_md_path = self.skills.project_root / AGENTS_MD_FILENAME
        self._agents_md_text = self._load_agents_md()
        self._agents_md_message: dict[str, Any] | None = (
            {
                "role": "user",
                "content": AGENTS_MD_TEMPLATE.format(
                    prefix=AGENTS_MD_PREFIX, body=self._agents_md_text
                ),
            }
            if self._agents_md_text
            else None
        )

        # =====================================================================
        # 新增：MCP 客户端管理器
        # 配置优先级：显式参数 > config.ini [paths] mcp_config > cwd/.mcp.json
        # 真正的网络连接/工具发现由 async astart() / start_mcp_background()
        # 完成，__init__ 不做任何网络 IO。
        # =====================================================================
        resolved_mcp_path = mcp_config_path
        if resolved_mcp_path is None and not self.settings.mcp_config_path_is_default:
            resolved_mcp_path = self.settings.mcp_config_path
        self.mcp: MCPManager | None = (
            MCPManager(config_path=resolved_mcp_path) if enable_mcp else None
        )
        self._mcp_start_lock = asyncio.Lock()
        self._mcp_bg_task: asyncio.Task[None] | None = None

        self.messages: list[dict[str, Any]] = []
        self.reset()

    # ---------- 生命周期 ----------

    def reset(self) -> None:
        """清空上下文，仅保留 system prompt 与 AGENTS.md 注入消息。"""
        self.messages = [{"role": "system", "content": self._system_prompt}]
        if self._agents_md_message is not None:
            # 复用缓存的同一份内容，不重新读取 AGENTS.md
            self.messages.append(dict(self._agents_md_message))

        # =====================================================================
        # 新增 ③：/clear 或 reset 后允许重新加载 SKILL.md
        # 插入位置：self.messages 重置语句之后
        # =====================================================================
        self.skills.reset_loaded()

        # 新增 ②：重置后解除上下文锁定，并丢弃旧的 API Token 锚点
        self._context_locked = False
        self._api_token_anchor = None

    def _load_agents_md(self) -> str:
        """读取项目根目录的 AGENTS.md；只调用一次。"""
        if not self.settings.agents_md_enabled:
            return ""
        path = self.skills.project_root / AGENTS_MD_FILENAME
        if not path.is_file():
            return ""
        try:
            size = path.stat().st_size
            if size <= 0 or size > AGENTS_MD_MAX_BYTES:
                return ""
            return path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError):
            # 读不到或编码不合法时静默跳过，不影响 Agent 启动
            return ""

    def has_agents_md(self) -> bool:
        """当前会话是否已注入 AGENTS.md（供 /status 与边栏展示）。"""
        return self._agents_md_message is not None

    # ---------- 会话恢复 ----------

    def restore_session(self, messages: list[dict[str, Any]]) -> None:
        """把持久化的消息历史恢复到当前 Agent。

        规则：
            - 第一条 system 消息替换为当前 System Prompt（技能清单/工具说明
              可能已经变化），其余历史原样恢复；
            - 重新计算上下文占用，必要时直接锁定普通输入；
            - 清空 Token 锚点与技能加载状态。
        """
        if not isinstance(messages, list) or not messages:
            raise ValueError("会话历史不能为空。")

        restored = copy.deepcopy([item for item in messages if isinstance(item, dict)])
        if not restored:
            raise ValueError("会话历史中没有有效消息。")

        if restored[0].get("role") == "system":
            restored[0] = {"role": "system", "content": self._system_prompt}
        else:
            restored.insert(0, {"role": "system", "content": self._system_prompt})

        self.messages = restored
        self.skills.reset_loaded()
        self._api_token_anchor = None
        self._context_locked = False

        stats = self.get_context_stats()
        if stats.percent >= self._compact_ratio * 100:
            self._context_locked = True

    # ---------- MCP 异步初始化 ----------

    async def astart(self) -> None:
        """连接 .mcp.json 中的所有 MCP 服务并注册工具。

        可以安全地重复调用；单个服务失败只记录日志，不会抛异常。
        CLI / TUI 应该在启动时 await 一次；即使忘记调用，stream_turn
        也会在第一次请求前自动完成初始化。
        """
        await self._ensure_mcp_started()

    def _reserved_tool_names(self) -> set[str]:
        """已注册的本地工具名：MCP 工具不能与它们重名。"""
        return {
            tool["function"]["name"]
            for tool in self.tools
            if isinstance(tool, dict)
            and isinstance(tool.get("function"), dict)
            and isinstance(tool["function"].get("name"), str)
        }

    def _sync_mcp_tools(self) -> None:
        """把 MCPManager 已发现的工具增量合并进 self.tools。"""
        if self.mcp is None:
            return
        existing = {
            tool["function"]["name"]
            for tool in self.tools
            if isinstance(tool, dict)
            and isinstance(tool.get("function"), dict)
            and isinstance(tool["function"].get("name"), str)
        }
        for schema in self.mcp.schemas:
            name = schema["function"]["name"]
            if name not in existing:
                self.tools.append(schema)
                existing.add(name)

    async def _ensure_mcp_started(self) -> None:
        if self.mcp is None:
            return
        if self.mcp.started:
            # 后台或前一轮已经完成加载：同步新注册的工具即可
            self._sync_mcp_tools()
            return
        if self._mcp_bg_task is not None and not self._mcp_bg_task.done():
            # TUI 正在后台逐个连接 MCP：本轮不等它，避免阻塞用户输入。
            # 已连接成功的服务器工具会在下一轮请求时自动出现在 self.tools 中。
            self._sync_mcp_tools()
            return

        async with self._mcp_start_lock:
            if self.mcp.started:
                self._sync_mcp_tools()
                return
            await self.mcp.start(reserved_names=self._reserved_tool_names())
            self._sync_mcp_tools()

    def start_mcp_background(self, on_update: Any = None) -> asyncio.Task[None] | None:
        """在后台逐个连接 MCP Server，供 TUI 启动时调用，不阻塞界面。

        on_update：每连接完一个服务器（成功或失败）回调一次，用于刷新边栏。
        返回后台 Task；MCP 已初始化或未启用时返回 None。
        """
        if self.mcp is None:
            if on_update is not None:
                on_update()
            return None

        if self.mcp.started:
            self._sync_mcp_tools()
            if on_update is not None:
                on_update()
            return None

        if self._mcp_bg_task is not None and not self._mcp_bg_task.done():
            return self._mcp_bg_task

        self._mcp_bg_task = asyncio.create_task(
            self._run_mcp_background(on_update), name="xagent-mcp-load"
        )
        return self._mcp_bg_task

    async def _run_mcp_background(self, on_update: Any = None) -> None:
        """后台任务主体：串行连接，每个服务器结束后同步工具并通知界面。"""
        try:
            async with self._mcp_start_lock:
                if self.mcp is None or self.mcp.started:
                    return

                def _notify() -> None:
                    self._sync_mcp_tools()
                    if on_update is not None:
                        try:
                            on_update()
                        except Exception:
                            # 界面刷新失败不能影响 MCP 连接
                            pass

                await self.mcp.start(
                    reserved_names=self._reserved_tool_names(),
                    on_update=_notify,
                    sequential=True,
                )
                self._sync_mcp_tools()
        except asyncio.CancelledError:
            raise
        except Exception:
            # MCPManager 内部已经记录了单个服务器的失败原因；
            # 这里兜底，确保后台任务不会把异常抛到事件循环。
            return

    @property
    def history(self) -> list[dict[str, Any]]:
        """当前上下文快照（浅拷贝）。"""
        return list(self.messages)

    # =====================================================================
    # 新增 ②：上下文 Token 监控
    # =====================================================================

    @property
    def context_locked(self) -> bool:
        """普通用户输入是否已被暂停（等待 /compact）。"""
        return self._context_locked

    @property
    def is_compacting(self) -> bool:
        """压缩流水线是否正在执行。"""
        return self._compacting

    def _resolve_context_window(self) -> tuple[int, bool]:
        """窗口上限已在 Settings.load 中解析，这里只做转发。"""
        return self.settings.context_window, self.settings.context_window_is_default

    def get_context_stats(self, extra_text: str | None = None) -> ContextStats:
        """统计当前上下文的 Token 用量。

        优先使用 API usage 锚点：
            锚点 = 某次请求的 prompt_tokens + completion_tokens；
            当前值 = 锚点 + 锚点之后新增消息的本地估算。
        没有锚点时，对 System Prompt、历史消息和工具定义全部本地估算。
        """
        source = "estimate"
        anchor = self._api_token_anchor

        if anchor is not None and anchor[1] <= len(self.messages):
            anchor_tokens, anchor_index = anchor
            used = anchor_tokens + _estimate_messages_tokens(
                self.messages[anchor_index:]
            )
            source = "api"
        else:
            # 上下文被 reset / compact 替换，或消息回滚后，旧锚点失效
            self._api_token_anchor = None
            used = (
                _estimate_messages_tokens(self.messages)
                + _estimate_tools_tokens(self.tools)
            )

        # 用户刚输入、还没进入 messages 时，把新提示词也算进去
        if extra_text:
            used += estimate_text_tokens(extra_text) + 4

        window = self._context_window
        percent = (used / window * 100.0) if window > 0 else 0.0
        return ContextStats(
            used=used,
            window=window,
            percent=percent,
            window_is_default=self._window_is_default,
            source=source,
        )

    def _update_api_token_anchor(self, usage: dict[str, int] | None) -> None:
        """收到 API usage 后更新锚点。

        调用时机：assistant 消息刚写入 self.messages 之后。
        """
        if not usage:
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = prompt + completion
        if total <= 0:
            return
        self._api_token_anchor = (total, len(self.messages))

    def _finalize_context_stats(self) -> tuple[ContextStats, str]:
        """一轮结束后统计；首次跨过阈值时锁定并返回一次性警告。"""
        stats = self.get_context_stats()
        warning = ""
        threshold_percent = self._compact_ratio * 100
        if stats.percent >= threshold_percent and not self._context_locked:
            self._context_locked = True
            warning = (
                f"上下文占用已达到 {stats.percent:.1f}%（阈值 "
                f"{threshold_percent:.0f}%）。普通输入已暂停，"
                "请输入 /compact 压缩上下文；压缩成功后自动解除锁定。"
            )
        return stats, warning

    def _context_usage_event(
        self,
        stats: ContextStats,
        *,
        message: str = "",
        locked: bool | None = None,
    ) -> ContextUsage:
        """把 ContextStats 转换成界面层事件。"""
        return ContextUsage(
            used=stats.used,
            window=stats.window,
            percent=stats.percent,
            window_is_default=stats.window_is_default,
            locked=self._context_locked if locked is None else locked,
            message=message,
        )

    # =====================================================================
    # 新增 ②：/compact 压缩流水线
    # =====================================================================

    async def _generate_compact_summary(
        self, messages: list[dict[str, Any]]
    ) -> str:
        """调用 LLM 生成摘要。

        注意：使用独立的 messages 列表，绝不修改 self.messages。
        """
        transcript = _format_transcript(messages)
        summary_messages: list[dict[str, Any]] = [
            {"role": "system", "content": COMPACT_PROMPT},
            {
                "role": "user",
                "content": "以下是需要压缩的历史上下文：\n\n" + transcript,
            },
        ]

        parts: list[str] = []
        async for delta in self._client.stream(summary_messages, None):
            if delta.content:
                parts.append(delta.content)
        return "".join(parts).strip()

    async def compact(self) -> CompactResult:
        """执行一次手动压缩：归档 -> 摘要 -> 替换历史。

        三步顺序不能调换。任何一步失败都直接返回错误，
        self.messages 保持原样，因此不会丢失原始会话。
        """
        if self._compacting:
            return CompactResult(ok=False, error="已有压缩任务正在进行。")
        if len(self.messages) <= 1:
            return CompactResult(ok=False, error="当前没有可压缩的对话历史。")

        self._compacting = True
        try:
            # 快照：后续所有失败都以它为准，原上下文不受影响
            original = copy.deepcopy(self.messages)
            original_text = format_messages_for_display(original)

            # ---------- 步骤 1：完整归档 ----------
            try:
                archive_id, archive_path, archive_time = (
                    self.context_archive.archive_messages(original)
                )
            except ContextArchiveError as exc:
                return CompactResult(
                    ok=False, error=f"步骤 1（归档）失败：{exc}"
                )

            # ---------- 步骤 2：归档成功后生成摘要 ----------
            try:
                summary_body = await self._generate_compact_summary(original)
            except Exception as exc:
                return CompactResult(
                    ok=False,
                    error=f"步骤 2（生成摘要）失败：{exc}",
                    archive_id=archive_id,
                    archive_path=archive_path,
                    archive_time=archive_time,
                )

            if not summary_body:
                return CompactResult(
                    ok=False,
                    error="步骤 2（生成摘要）失败：模型返回了空摘要。",
                    archive_id=archive_id,
                    archive_path=archive_path,
                    archive_time=archive_time,
                )

            # 摘要末尾追加固定说明（归档时间 / 归档 id 已替换）
            footer = COMPACT_FOOTER_TEMPLATE.format(
                archive_time=archive_time, archive_id=archive_id
            )
            summary = summary_body.rstrip() + "\n\n" + footer

            # ---------- 步骤 3：摘要成功后替换会话历史 ----------
            # System Prompt 与 self.tools（全部工具定义）保持不变
            self.messages = [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": summary},
            ]
            self._api_token_anchor = None
            self._context_locked = False

            return CompactResult(
                ok=True,
                message=(
                    f"上下文压缩完成，原始消息已归档到 {archive_path}。"
                    "现在可以用 recallContext 检索历史。"
                ),
                archive_id=archive_id,
                archive_path=archive_path,
                archive_time=archive_time,
                original_text=original_text,
                summary=summary,
            )
        except Exception as exc:
            # 兜底：未预期异常也不能破坏原有会话
            return CompactResult(ok=False, error=f"压缩失败：{exc}")
        finally:
            self._compacting = False

    # =====================================================================
    # 新增 ②：recallContext 工具入口
    # =====================================================================

    def recall_context(self, text: str, id: str | None = None) -> str:
        """在 .agents/context/*.jsonl 中做纯字符串关键词检索。"""
        try:
            return self.context_archive.search(text, id)
        except ContextArchiveError as exc:
            return _context_json_error("recall_error", str(exc))
        except Exception as exc:
            return _context_json_error(
                "tool_error", f"recallContext 执行失败：{exc}"
            )

    async def aclose(self) -> None:
        # 先停止后台 MCP 加载任务，避免它继续持有连接
        if self._mcp_bg_task is not None and not self._mcp_bg_task.done():
            self._mcp_bg_task.cancel()
            try:
                await self._mcp_bg_task
            except BaseException:
                pass
        if self.mcp is not None:
            await self.mcp.aclose()
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

        # =====================================================================
        # 新增 ②：压缩期间 / 达到 60% 阈值后，拒绝普通输入。
        # 此时不会进入任何 Function Calling，用户只能执行 /compact。
        # =====================================================================
        if self._compacting:
            yield ErrorEvent("上下文正在压缩，暂时不能处理新的请求，请稍候。")
            return

        if self._context_locked:
            blocked_stats = self.get_context_stats(text_input)
            yield self._context_usage_event(blocked_stats, locked=True)
            yield ErrorEvent(
                f"上下文占用已达到 {blocked_stats.percent:.1f}%（阈值 "
                f"{self._compact_ratio * 100:.0f}%）。普通输入已暂停，"
                "请先输入 /compact 压缩上下文。"
            )
            return

        # MCP 工具必须在第一次调用模型前完成发现与注册。
        # 内部已做并发保护，重复调用不会重复连接。
        await self._ensure_mcp_started()

        # 快照用于异常/取消时回滚，避免留下半截 tool_calls 导致后续请求不合法
        snapshot = len(self.messages)
        self.messages.append({"role": "user", "content": text_input})

        # 用户每发送一条新提示词后统计一次（包含刚加入的用户消息）
        yield self._context_usage_event(self.get_context_stats())

        rounds_done = 0
        tools_used = 0

        try:
            for round_no in range(1, self.settings.max_tool_rounds + 1):
                rounds_done = round_no
                yield RoundStarted(round_no)

                content_parts: list[str] = []
                pending: dict[int, _PendingCall] = {}
                round_usage: dict[str, int] | None = None

                async for delta in self._client.stream(
                    self.messages, self.tools
                ):
                    if delta.usage:
                        round_usage = delta.usage

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
                    # 优先使用 API usage，并在一轮结束时做 60% 阈值检查
                    self._update_api_token_anchor(round_usage)
                    stats, warning = self._finalize_context_stats()
                    yield self._context_usage_event(stats, message=warning)
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
                # 本轮 API usage 以 assistant 消息写入后的消息数为锚点
                self._update_api_token_anchor(round_usage)

                for call in calls:
                    yield ToolCallStarted(
                        call_id=call.id,
                        name=call.name,
                        arguments=_safe_json_object(call.arguments),
                        raw_arguments=call.arguments,
                    )

                    # 安全上限：单轮最多执行 max_tool_calls 次工具。
                    # 超出的调用也要补一条 tool 消息，保持 OpenAI 协议中
                    # assistant.tool_calls 与 tool 消息一一对应。
                    if tools_used >= self.settings.max_tool_calls:
                        limit_output = self._error_json(
                            "tool_call_limit",
                            f"本轮工具调用次数已达到上限 "
                            f"{self.settings.max_tool_calls} 次，"
                            "该调用未执行。",
                        )
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": limit_output,
                            }
                        )
                        yield ToolCallFinished(
                            call_id=call.id,
                            name=call.name,
                            ok=False,
                            output=limit_output,
                            elapsed=0.0,
                        )
                        continue

                    execution = await self._dispatch_tool_async(
                        call.name, call.arguments
                    )
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

                # 工具调用达到安全上限：结束本轮，不再继续请求模型
                if tools_used >= self.settings.max_tool_calls:
                    notice = (
                        f"[已达到单轮最大工具调用次数 "
                        f"{self.settings.max_tool_calls}，本轮停止]"
                    )
                    self.messages.append({"role": "assistant", "content": notice})
                    yield ErrorEvent(
                        f"单轮工具调用次数过多（上限 "
                        f"{self.settings.max_tool_calls} 次），已停止本轮请求。"
                    )
                    stats, warning = self._finalize_context_stats()
                    yield self._context_usage_event(stats, message=warning)
                    yield TurnFinished(
                        final_text="",
                        rounds=rounds_done,
                        tool_calls=tools_used,
                        interrupted=True,
                    )
                    return

                if rounds_done < self.settings.max_tool_rounds and self.settings.request_interval:
                    await asyncio.sleep(self.settings.request_interval)

            # 达到工具轮数上限
            notice = f"[已达到最大工具调用轮数 {self.settings.max_tool_rounds}，本轮停止]"
            self.messages.append({"role": "assistant", "content": notice})
            yield ErrorEvent(
                f"工具调用轮数过多（上限 {self.settings.max_tool_rounds} 轮），已停止本轮请求。"
            )
            stats, warning = self._finalize_context_stats()
            yield self._context_usage_event(stats, message=warning)
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

    async def _dispatch_tool_async(
        self,
        name: str,
        raw_arguments: str | dict[str, Any] | None,
    ) -> ToolExecution:
        """异步分发入口：MCP 工具走客户端，本地工具复用 _dispatch_tool。

        为什么需要这一层：MCP SDK 的 call_tool 是协程，而 _dispatch_tool
        保持同步以兼容原有文件工具、联网工具和现有测试。这里按映射表
        精确识别 {server}__{tool}（而不是仅靠字符串前缀猜测），命中后
        路由到对应 MCP 服务；未命中则原样交给 _dispatch_tool。
        """
        if self.mcp is not None and self.mcp.is_mcp_tool(name):
            return await self.mcp.call_tool(name, raw_arguments)
        return self._dispatch_tool(name, raw_arguments)

    def _dispatch_tool(
        self,
        name: str,
        raw_arguments: str | dict[str, Any] | None,
    ) -> ToolExecution:
        """按工具名分发调用。

        - fetchUrl / searchWeb：显式分支调用联网工具；
        - RunCli：显式分支调用命令行工具；
        - 其余工具（10 个文件工具）：交给 ToolRegistry 原有逻辑。
        - MCP 工具：必须走异步入口 _dispatch_tool_async（见上）。
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

        # 防御性提示：正常流程下 MCP 工具已在 _dispatch_tool_async 中处理；
        # 这里识别双下划线前缀，避免同步调用方拿到“工具不存在”的模糊错误。
        if self.mcp is not None and self.mcp.is_mcp_tool(name):
            return ToolExecution(
                name,
                parsed,
                self._error_json(
                    "mcp_async_required",
                    f"MCP 工具 {name} 必须在异步流程中调用；"
                    "请通过 Agent.stream_turn（内部会自动路由）。",
                ),
                False,
                time.perf_counter() - start,
            )

        try:
            if name == "fetchUrl":
                output = fetch_url(**parsed)

            # =================================================================
            # 新增：fetchWeb 分发（课程任务 1）
            # 插入位置：fetchUrl 分支之后、searchWeb 分支之前
            # 与 fetchUrl / searchWeb 平级，走同一套 ToolExecution 返回规范
            # =================================================================
            elif name == "fetchWeb":
                output = fetch_web(**parsed)

            elif name == "searchWeb":
                output = search_web(**parsed)
            elif name == "RunCli":
                output = run_cli(**parsed)

            # =================================================================
            # 新增 ④：loadSkill 分发
            # 插入位置：RunCli 分支之后、else 之前
            # =================================================================
            elif name == "loadSkill":
                output = self.skills.load_skill(**parsed)

            # =================================================================
            # 新增 ⑤：createSkill 分发
            # 插入位置：loadSkill 分支之后、else 之前
            # =================================================================
            elif name == "createSkill":
                output = self.skills.create_skill(**parsed)

            # =================================================================
            # 新增 ②：recallContext 分发（与 loadSkill / createSkill 并列）
            # =================================================================
            elif name == "recallContext":
                output = self.recall_context(**parsed)

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


async def _console_repl() -> None:
    """控制台交互循环：逐事件消费 stream_turn，保持流式输出。"""
    configure_mcp_logging()
    agent = Agent()
    # 启动时异步加载 MCP：失败只打日志，不影响本地工具与交互
    await agent.astart()
    print("✅ Agent 就绪，输入问题，exit 退出")
    try:
        while True:
            try:
                user_q = input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if user_q.strip().lower() == "exit":
                break
            if not user_q.strip():
                continue

            line_open = False
            async for event in agent.stream_turn(user_q):
                if isinstance(event, TextDelta):
                    print(event.text, end="", flush=True)
                    line_open = True
                elif isinstance(event, ToolCallStarted):
                    if line_open:
                        print()
                        line_open = False
                    print(f"[调用] {event.name}({event.raw_arguments})")
                elif isinstance(event, ToolCallFinished):
                    if line_open:
                        print()
                        line_open = False
                    mark = "OK" if event.ok else "FAIL"
                    print(f"[{mark}] {event.output}")
                elif isinstance(event, ErrorEvent):
                    if line_open:
                        print()
                        line_open = False
                    print(f"[错误] {event.message}")
                elif isinstance(event, TurnFinished):
                    if line_open:
                        print()
                        line_open = False
                    print()
    finally:
        await agent.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(_console_repl())
    except KeyboardInterrupt:
        print("\n已退出。")
