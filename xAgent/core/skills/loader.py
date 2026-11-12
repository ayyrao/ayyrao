"""Skill 发现、System Prompt 注入与 loadSkill 读取逻辑。

目录约定（以**程序启动时的工作目录**为项目根）：

    <project_root>/.agents/skills/
        <skill-dir>/
            SKILL.md            # 必需，YAML Frontmatter 必须有 name / description
            references/...      # 可选附件
            scripts/...         # 可选附件

关键原则：
1. 初始化时只读取每个 SKILL.md 的 Frontmatter（name / description），
   正文要等 LLM 主动调用 loadSkill 才进入上下文；
2. loadSkill 的读取范围严格限制在当前 Skill 目录内，路径解析后必须
   满足 is_relative_to(skill_dir)，符号链接越界同样会被拒绝；
3. 所有失败都抛 ToolError，内容为 {"ok": false, "error": {...}} JSON，
   与 web_tools / cli_tools 的错误格式保持一致。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools.registry import ToolError
from .frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    read_frontmatter_block,
)

# Skill 根目录：<project_root>/.agents/skills
SKILLS_DIR_PARTS: tuple[str, ...] = (".agents", "skills")
SKILL_FILENAME = "SKILL.md"

# 单次 loadSkill 读取上限，防止把超大附件灌进模型上下文
MAX_READ_BYTES = 1024 * 1024  # 1 MiB
MAX_CONTENT_CHARS = 100_000  # 回灌给模型的字符上限
TRUNCATION_MARKER = "【内容已截断】"

# System Prompt 中每条 description 的最大展示长度（折叠为单行后）
MAX_PROMPT_DESCRIPTION_CHARS = 500


# ---------------------------------------------------------------------------
# 结构化错误 / 结果（与 web_tools / cli_tools 保持一致）
# ---------------------------------------------------------------------------


def _fail(error_type: str, message: str, **details: object) -> None:
    """抛出结构化 ToolError，Agent 分发层会把 JSON 文本原样回灌给模型。"""
    error: dict[str, object] = {"type": error_type, "message": message}
    error.update(details)
    payload = {"ok": False, "error": error}
    raise ToolError(json.dumps(payload, ensure_ascii=False))


def _ok(**payload: object) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _one_line(text: str, limit: int = MAX_PROMPT_DESCRIPTION_CHARS) -> str:
    """把多行 description 折叠成单行，避免破坏 System Prompt 的列表结构。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """一个已发现的 Skill。

    name / description 来自 SKILL.md 的 Frontmatter；
    directory 与 skill_file 都是 resolve() 之后的绝对路径。
    """

    name: str
    description: str
    directory: Path
    skill_file: Path
    directory_name: str


# ---------------------------------------------------------------------------
# SkillLoader
# ---------------------------------------------------------------------------


class SkillLoader:
    """扫描并加载项目根目录下的 .agents/skills。

    参数：
        project_root —— 项目根目录，默认取构造时的进程工作目录（即程序启动
                        目录）。显式传入便于测试或多项目复用。
        skills_dir   —— 可选，直接指定 skills 根目录（覆盖默认的
                        <project_root>/.agents/skills）。
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

        # name -> SkillInfo；保持发现顺序（目录名排序），便于稳定输出
        self.skills: dict[str, SkillInfo] = {}
        # 扫描过程中的非致命问题（缺字段、重复名、YAML 错误等）
        self.warnings: list[str] = []
        # 本次对话中已经加载过 SKILL.md 的技能名（小写归一化后）
        self._loaded: set[str] = set()

        self.discover()

    # ---------- 发现 ----------

    def discover(self) -> None:
        """扫描 skills 根目录，只解析每个 SKILL.md 的 Frontmatter。"""
        self.skills = {}
        self.warnings = []
        self._loaded.clear()

        root = self.skills_root
        if not root.is_dir():
            # 没有 skills 目录是正常情况，不报错也不阻断 Agent 启动
            return

        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            self.warnings.append(f"无法读取 skills 目录 {root}：{exc}")
            return

        for entry in children:
            # 只扫描一级子目录；隐藏目录（.git 等）不视为 Skill
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            self._register(entry, root)

    def _register(self, entry: Path, root: Path) -> None:
        """校验并注册单个 Skill 目录。任何异常都只记录 warning，不中断启动。"""
        try:
            directory = entry.resolve()
        except OSError as exc:
            self.warnings.append(f"跳过 {entry.name}：目录无法解析（{exc}）。")
            return

        # 防止符号链接 / junction 把 Skill 目录指向 skills 根目录之外
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
            block = read_frontmatter_block(skill_file)
            metadata = parse_frontmatter(block)
        except FrontmatterError as exc:
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

        # 换行/控制字符会破坏 System Prompt 列表；反引号会破坏 Markdown 标记
        if any(ch in "\r\n\t`" for ch in name):
            self.warnings.append(
                f"跳过 {entry.name}：name 含有非法字符（换行、制表符或反引号）。"
            )
            return

        # 名称按大小写不敏感去重：loadSkill 的查找也遵循这一规则
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
        """定位 SKILL.md，并兼容大小写（Linux 上 skill.md 也能识别）。

        返回的路径必须仍位于 Skill 目录内，避免 skill.md 是指向外部的符号链接。
        """
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
        """按名称排序返回已发现的技能，保证 System Prompt 稳定。"""
        return [self.skills[name] for name in sorted(self.skills, key=str.casefold)]

    def prompt_section(self) -> str:
        """生成注入 System Prompt 的 Skill 清单；没有技能时返回空字符串。"""
        skills = self.available_skills()
        if not skills:
            return ""

        lines = ["You have access to the following skills:"]
        for skill in skills:
            lines.append(f"- `{skill.name}`: {_one_line(skill.description)}")
        lines.append("")
        lines.append(
            "Use the `loadSkill` tool to load the full instructions of a skill "
            "when needed. You can also load additional files referenced by a "
            "skill by passing the `filename` argument."
        )
        return "\n".join(lines)

    # ---------- 加载状态 ----------

    def reset_loaded(self) -> None:
        """清空“已加载”记录（Agent.reset / /clear 时调用）。"""
        self._loaded.clear()

    def is_loaded(self, name: str) -> bool:
        info = self._find_skill(name)
        return info is not None and info.name.casefold() in self._loaded

    # ---------- loadSkill 入口 ----------

    def load_skill(self, name: str, filename: str | None = None) -> str:
        """loadSkill 工具实现。

        - 仅 name：返回 SKILL.md 全文；同一技能不可重复加载（返回
          already_loaded=true 且不再回传正文）；
        - name + filename：只读取附件，不加载 SKILL.md，也不改变加载状态；
        - filename 支持 skill 相对路径、项目相对路径、绝对路径，最终必须
          落在当前 Skill 目录内；只给文件名时在该目录内递归搜索。
        """
        skill = self._get_skill(name)

        # filename 为空字符串时按“仅传 name”处理，避免模型传空值导致误解
        if filename is None or (
            isinstance(filename, str) and not filename.strip()
        ):
            return self._load_skill_md(skill)

        if not isinstance(filename, str):
            _fail("invalid_arguments", "filename 必须是字符串。")

        return self._load_attachment(skill, filename.strip())

    def _find_skill(self, raw_name: object) -> SkillInfo | None:
        """按名称查找技能（不抛错）：先精确匹配，再大小写不敏感匹配。"""
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
        """按名称查找技能；找不到时抛出结构化错误。"""
        if not isinstance(raw_name, str) or not raw_name.strip():
            _fail("invalid_arguments", "name 不能为空，必须是系统提示中列出的技能名。")

        info = self._find_skill(raw_name)
        if info is not None:
            return info

        _fail(
            "skill_not_found",
            f"未找到 Skill：{raw_name.strip()}。请只使用系统提示中列出的技能名。",
            available=sorted(self.skills),
        )

    def _load_skill_md(self, skill: SkillInfo) -> str:
        """读取 SKILL.md 全文；已加载过则拒绝重复读取。"""
        key = skill.name.casefold()
        if key in self._loaded:
            return _ok(
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
        return _ok(
            skill=skill.name,
            filename=SKILL_FILENAME,
            path=SKILL_FILENAME,
            content=content,
            content_chars=len(content),
            truncated=truncated,
        )

    def _load_attachment(self, skill: SkillInfo, filename: str) -> str:
        """读取技能目录内的附件文件。"""
        target = self._resolve_attachment(skill, filename)

        # 显式请求 SKILL.md（例如 filename="SKILL.md"）时复用重复加载规则
        if target == skill.skill_file:
            return self._load_skill_md(skill)

        content, truncated = self._read_text(target)
        relative = self._display_path(skill, target)
        return _ok(
            skill=skill.name,
            filename=relative,
            path=relative,
            content=content,
            content_chars=len(content),
            truncated=truncated,
        )

    # ---------- 附件路径解析 ----------

    def _resolve_attachment(self, skill: SkillInfo, filename: str) -> Path:
        """把 filename 解析成 Skill 目录内的真实文件路径。

        规则与优先级：
        1. 含路径分隔符时，依次尝试 skill 相对路径、项目相对路径、绝对路径；
           三者都必须解析到 Skill 目录内部；
        2. 只给文件名时，先尝试 Skill 目录下的同名文件，再递归搜索；
           递归搜索找到多个同名文件时返回候选列表，交由 LLM 选择。
        """
        if "\x00" in filename:
            _fail("invalid_filename", "filename 中包含非法空字符。")

        # 统一分隔符：模型可能给出 Windows 风格的 .agents\skills\...
        normalized = filename.replace("\\", "/").strip()
        if not normalized:
            _fail("invalid_filename", "filename 不能为空。")

        path = Path(normalized).expanduser()

        if "/" not in normalized:
            # ---- 仅文件名：精确匹配 -> 递归搜索 ----
            exact = self._inside(path.name, skill.directory)
            if exact is not None and exact.is_file():
                return exact

            matches = self._search_by_name(skill.directory, path.name)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                _fail(
                    "ambiguous_filename",
                    f"在 Skill {skill.name!r} 中找到 {len(matches)} 个同名文件"
                    f" {path.name!r}，请从 candidates 中选择一个完整路径，"
                    "再次调用 loadSkill 并传入 filename。",
                    candidates=[
                        self._display_path(skill, item) for item in matches
                    ],
                )
            _fail(
                "file_not_found",
                f"Skill {skill.name!r} 中没有找到文件：{path.name}",
                skill=skill.name,
            )

        # ---- 含目录：skill 相对 / 项目相对 / 绝对，精确匹配 ----
        if path.is_absolute():
            candidates = [path]
        else:
            candidates = [
                skill.directory / path,  # skill 相对路径
                self.project_root / path,  # 项目相对路径
            ]

        blocked: list[str] = []
        for candidate in candidates:
            resolved = self._inside(candidate, skill.directory)
            if resolved is None:
                # 记录越界候选；若所有候选都不可用，给出明确的安全错误
                blocked.append(str(candidate))
                continue
            if resolved.is_file():
                return resolved

        if blocked and not any(
            self._inside(candidate, skill.directory) is not None
            for candidate in candidates
        ):
            _fail(
                "path_outside_skill",
                "拒绝读取：文件必须位于当前 Skill 目录内部"
                f"（{self._display_path(skill, skill.directory)}）。",
                skill=skill.name,
                requested=filename,
            )

        _fail(
            "file_not_found",
            f"Skill {skill.name!r} 中不存在该路径：{filename}。"
            "含目录的路径需要精确匹配；如果只记得文件名，可以只传文件名让工具递归搜索。",
            skill=skill.name,
        )

    @staticmethod
    def _inside(candidate: str | Path, directory: Path) -> Path | None:
        """解析路径并检查是否位于 Skill 目录内；越界或无法解析返回 None。

        使用 resolve()（会展开符号链接 / Windows junction）后再做
        is_relative_to()，而不是字符串前缀匹配，避免 ../ 与链接绕过。
        """
        try:
            resolved = Path(candidate).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        return resolved if resolved.is_relative_to(directory) else None

    @staticmethod
    def _search_by_name(directory: Path, basename: str) -> list[Path]:
        """在 Skill 目录内递归查找同名文件（大小写不敏感）。

        - 不跟随符号链接目录（os.walk 默认 followlinks=False）；
        - 隐藏目录（.git 等）不扫描；
        - 结果去重并按路径排序，保证候选列表稳定。
        """
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
                # 再次确认没有通过符号链接越界
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
        """读取 UTF-8 文本并限制大小；返回 (内容, 是否截断)。"""
        try:
            size = path.stat().st_size
            if size > MAX_READ_BYTES:
                _fail(
                    "file_too_large",
                    f"文件过大（{size} 字节），超过 {MAX_READ_BYTES} 字节上限："
                    f"{path.name}",
                )
            data = path.read_bytes()
        except OSError as exc:
            _fail("read_error", f"读取文件失败：{exc}")

        if b"\x00" in data:
            _fail("binary_file", f"非文本文件，禁止读取：{path.name}")

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            _fail(
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
        """返回相对 Skill 目录的 POSIX 风格路径，用于回显。"""
        try:
            return path.relative_to(skill.directory).as_posix() or "."
        except ValueError:
            return str(path)


__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_READ_BYTES",
    "SKILL_FILENAME",
    "SKILLS_DIR_PARTS",
    "SkillInfo",
    "SkillLoader",
]
