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
import json
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
    ErrorEvent,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from .llm.client import LLMClient
from .security import PathSandbox
from .tools import (
    ToolError,
    ToolExecution,
    ToolRegistry,
    build_default_registry,
    run_cli,
)
from .tools.web_tools import WEB_TOOL_SCHEMAS, fetch_url, search_web


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
        self.skills_root: Path = (
            Path(skills_dir).expanduser().resolve()
            if skills_dir is not None
            else self.project_root.joinpath(*SKILLS_DIR_PARTS)
        )

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

    # ---------- loadSkill 入口 ----------

    def load_skill(self, name: str, filename: str | None = None) -> str:
        """仅 name 读 SKILL.md；name+filename 读附件；附件不改变加载状态。"""
        skill = self._get_skill(name)

        if filename is None or (
            isinstance(filename, str) and not filename.strip()
        ):
            return self._load_skill_md(skill)

        if not isinstance(filename, str):
            _skill_fail("invalid_arguments", "filename 必须是字符串。")

        return self._load_attachment(skill, filename.strip())

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
    "已经存在，本工具只返回错误提示，绝不会覆盖任何已有文件。references/、"
    "scripts/ 等附件不在本工具中创建；创建成功后请继续使用 create_file / "
    "make_dir 等 fileOps 工具。"
)

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


SYSTEM_PROMPT = """你是一个运行在本地沙箱中的文件管理与联网助手。

工作目录：{workspace}

规则：
1. 你只能操作工作目录以内的文件与目录，所有路径都使用相对于工作目录的路径。
2. 需要了解文件现状时先调用工具（get_cwd / list_dir 等）确认，不要凭空猜测。
3. 创建文件前先确认是否已存在；修改内容使用 edit_text_file。
4. remove_dir 只能删除空目录；rename_item 不会覆盖已存在的目标。
5. 需要最新信息或不知道具体网址时，先调用 searchWeb；拿到链接后用 fetchUrl 阅读正文。
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
        self.skills = SkillLoader(project_root=None)
        self.tools = self.tools + [LOAD_SKILL_SCHEMA, CREATE_SKILL_SCHEMA]
        skill_section = self.skills.prompt_section()
        if skill_section:
            self._system_prompt = f"{self._system_prompt}\n\n{skill_section}"

        self.messages: list[dict[str, Any]] = []
        self.reset()

    # ---------- 生命周期 ----------

    def reset(self) -> None:
        """清空上下文，仅保留 system prompt。"""
        self.messages = [{"role": "system", "content": self._system_prompt}]

        # =====================================================================
        # 新增 ③：/clear 或 reset 后允许重新加载 SKILL.md
        # 插入位置：self.messages 重置语句之后
        # =====================================================================
        self.skills.reset_loaded()

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
        - RunCli：显式分支调用命令行工具；
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
    agent = Agent()
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
