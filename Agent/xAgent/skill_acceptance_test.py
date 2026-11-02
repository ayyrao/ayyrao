#!/usr/bin/env python3
"""Agent Skills 动态加载的一键验收脚本（完全离线，不调用 LLM）。

覆盖范围：
    发现与注册      SKILL.md 大小写、缺少 frontmatter、缺少 name/description、
                    同名技能、非法 YAML、无 SKILL.md 的目录
    Frontmatter     YAML 解析（PyYAML 与内置最小解析器）、块标量、只读头部
    Prompt 注入     只包含 name/description，不泄露 SKILL.md 正文
    loadSkill       SKILL.md 去重加载、附件读取、相对/绝对/项目路径、
                    仅文件名递归搜索、同名多文件候选、路径穿越拦截
    Agent 集成      system prompt、tools 列表、_dispatch_tool 分发、reset 清空状态

用法：
    python skill_acceptance_test.py
    python skill_acceptance_test.py -v     # 失败时显示异常堆栈
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent import Agent  # noqa: E402
from core.config import Settings  # noqa: E402
from core.skills import (  # noqa: E402
    FrontmatterError,
    SkillLoader,
    parse_frontmatter,
    read_frontmatter_block,
)
from core.tools.registry import ToolError  # noqa: E402


# ---------------------------------------------------------------------------
# 极简测试框架
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"

_results: list[tuple[str, str, str]] = []


def check(name: str, function) -> None:
    try:
        function()
    except Exception as exc:  # noqa: BLE001 - 验收脚本需要收集所有失败
        _results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))
        if VERBOSE:
            traceback.print_exc()
    else:
        _results.append((PASS, name, ""))


def expect(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_json(raw: str) -> dict:
    data = json.loads(raw)
    expect(isinstance(data, dict), f"返回值不是 JSON 对象：{raw[:120]}")
    return data


def error_type(raw: str) -> str:
    data = load_json(raw)
    expect(data.get("ok") is False, f"期望失败结果，实际：{raw[:200]}")
    return data["error"]["type"]


def call(loader: SkillLoader, *args) -> str:
    """调用 loadSkill 并把 ToolError 转成 JSON 字符串（与 Agent 分发层一致）。"""
    try:
        return loader.load_skill(*args)
    except ToolError as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# 测试用技能目录
# ---------------------------------------------------------------------------

ALPHA_SKILL = """---
name: alpha
description: >-
  第一行描述
  第二行描述
license: MIT
---
# Alpha Skill

ALPHA-BODY
"""

BETA_SKILL = """---
name: beta
description: "Beta skill."
---
# Beta

BETA-BODY
"""


def build_fixture(root: Path) -> None:
    """在临时目录里构造覆盖各种边界情况的 skills 树。"""
    skills = root / ".agents" / "skills"
    skills.mkdir(parents=True)

    alpha = skills / "alpha"
    (alpha / "references").mkdir(parents=True)
    (alpha / "deep" / "nested").mkdir(parents=True)
    (alpha / "docs").mkdir()
    (alpha / "SKILL.md").write_text(ALPHA_SKILL, encoding="utf-8")
    (alpha / "references" / "rules.md").write_text("# rules v1", encoding="utf-8")
    (alpha / "deep" / "nested" / "rules.md").write_text(
        "# rules v2", encoding="utf-8"
    )
    (alpha / "docs" / "Notes.MD").write_text("notes content", encoding="utf-8")

    beta = skills / "beta"
    (beta / "assets").mkdir(parents=True)
    # 小写文件名：验证 Linux 上也能识别 SKILL.md
    (beta / "skill.md").write_text(BETA_SKILL, encoding="utf-8")
    (beta / "assets" / "data.json").write_text('{"x": 1}', encoding="utf-8")

    # 缺少 description -> 应被跳过并产生 warning
    missing_desc = skills / "missing-desc"
    missing_desc.mkdir()
    (missing_desc / "SKILL.md").write_text(
        "---\nname: no-desc\n---\nbody\n", encoding="utf-8"
    )

    # 没有 SKILL.md -> 应被跳过
    no_skill = skills / "no-skill-md"
    no_skill.mkdir()
    (no_skill / "readme.md").write_text("hello", encoding="utf-8")

    # 与 alpha 同名 -> 应被跳过
    duplicate = skills / "dup-name"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: duplicate\n---\nDUP\n", encoding="utf-8"
    )

    # frontmatter 缺少结束分隔符 -> 应被跳过
    broken = skills / "broken-frontmatter"
    broken.mkdir()
    (broken / "SKILL.md").write_text(
        "---\nname: broken\ndescription: no end\n", encoding="utf-8"
    )

    # 技能目录之外的“秘密”文件，用于路径穿越测试
    (root / "outside_secret.txt").write_text("TOP-SECRET", encoding="utf-8")


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def run(root: Path) -> None:
    skills = root / ".agents" / "skills"

    # ---------- 发现与注册 ----------

    def discovery() -> None:
        loader = SkillLoader(project_root=root)
        expect(set(loader.skills) == {"alpha", "beta"},
               f"应只发现 alpha/beta，实际 {sorted(loader.skills)}")
        expect(loader.skills["alpha"].description == "第一行描述 第二行描述",
               f"description 解析错误：{loader.skills['alpha'].description!r}")
        expect(loader.skills["beta"].skill_file.name.casefold() == "skill.md",
               "未能识别小写 skill.md")
        joined = "\n".join(loader.warnings)
        for keyword in ("missing-desc", "no-skill-md", "dup-name", "broken-frontmatter"):
            expect(keyword in joined, f"缺少对 {keyword} 的 warning：{loader.warnings}")

    check("发现：只注册合法技能并跳过异常目录", discovery)

    def metadata_only() -> None:
        loader = SkillLoader(project_root=root)
        prompt = loader.prompt_section()
        expect("alpha" in prompt and "beta" in prompt, "System Prompt 缺少技能名")
        expect("第一行描述 第二行描述" in prompt, "System Prompt 缺少 description")
        expect("ALPHA-BODY" not in prompt, "初始化阶段泄露了 SKILL.md 正文")
        expect("loadSkill" in prompt, "System Prompt 未提示 loadSkill")

    check("注入：只包含 name/description，不读取正文", metadata_only)

    def no_skills_dir() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loader = SkillLoader(project_root=tmp)
            expect(loader.skills == {}, "空目录不应发现技能")
            expect(loader.prompt_section() == "", "空目录不应生成 Prompt 片段")

    check("发现：没有 .agents/skills 时静默降级", no_skills_dir)

    # ---------- Frontmatter ----------

    def frontmatter_only_header() -> None:
        block = read_frontmatter_block(skills / "alpha" / "SKILL.md")
        expect("name: alpha" in block, "未读到 name")
        expect("ALPHA-BODY" not in block, "read_frontmatter_block 读到了正文")

    check("Frontmatter：只按行读取头部，不读正文", frontmatter_only_header)

    def frontmatter_fallback() -> None:
        text = (
            "name: demo-skill\n"
            "description: >-\n"
            "  第一段\n"
            "  第二段\n"
            "tags: [a, b]\n"
        )
        data = parse_frontmatter(text, use_pyyaml=False)
        expect(data["name"] == "demo-skill", f"name 解析错误：{data}")
        expect(data["description"] == "第一段 第二段", f"折叠解析错误：{data}")

        quoted = parse_frontmatter(
            "name: 'it''s-ok'\ndescription: \"带: 冒号\"\n", use_pyyaml=False
        )
        expect(quoted["name"] == "it's-ok", f"单引号解析错误：{quoted}")
        expect(quoted["description"] == "带: 冒号", f"双引号解析错误：{quoted}")

    check("Frontmatter：内置最小解析器（无 PyYAML 环境）", frontmatter_fallback)

    def frontmatter_missing_end() -> None:
        try:
            read_frontmatter_block(skills / "broken-frontmatter" / "SKILL.md")
        except FrontmatterError:
            return
        raise AssertionError("缺少结束分隔符时应抛 FrontmatterError")

    check("Frontmatter：缺少结束分隔符时报错", frontmatter_missing_end)

    # ---------- loadSkill：SKILL.md ----------

    def load_skill_md() -> None:
        loader = SkillLoader(project_root=root)
        first = load_json(loader.load_skill("alpha"))
        expect(first["ok"] is True, "首次加载应成功")
        expect("ALPHA-BODY" in first["content"], "未返回 SKILL.md 全文")
        expect(first["truncated"] is False, "小文件不应截断")

        second = load_json(loader.load_skill("alpha"))
        expect(second["already_loaded"] is True, "第二次加载应标记 already_loaded")
        expect("content" not in second, "重复加载不应再次返回正文")
        expect(loader.is_loaded("alpha"), "加载状态未记录")

    check("loadSkill：SKILL.md 首次加载 + 去重", load_skill_md)

    def reset_loaded() -> None:
        loader = SkillLoader(project_root=root)
        loader.load_skill("alpha")
        loader.reset_loaded()
        result = load_json(loader.load_skill("alpha"))
        expect(result.get("already_loaded") is not True, "reset 后应允许重新加载")
        expect("content" in result, "reset 后应返回正文")

    check("loadSkill：reset_loaded 后可重新加载", reset_loaded)

    def attachment_does_not_mark_loaded() -> None:
        loader = SkillLoader(project_root=root)
        attachment = load_json(loader.load_skill("alpha", "references/rules.md"))
        expect(attachment["content"] == "# rules v1", "附件内容错误")
        expect(not loader.is_loaded("alpha"), "读附件不应把 SKILL.md 标记为已加载")
        skill_md = load_json(loader.load_skill("alpha"))
        expect("content" in skill_md, "读附件后仍应能加载 SKILL.md")

    check("loadSkill：附件读取不影响 SKILL.md 加载状态", attachment_does_not_mark_loaded)

    def explicit_skill_md() -> None:
        loader = SkillLoader(project_root=root)
        first = load_json(loader.load_skill("alpha", "SKILL.md"))
        expect("ALPHA-BODY" in first.get("content", ""), "filename=SKILL.md 应返回正文")
        second = load_json(loader.load_skill("alpha", "SKILL.md"))
        expect(second.get("already_loaded") is True, "filename=SKILL.md 也应去重")

    check("loadSkill：显式 filename=SKILL.md 同样去重", explicit_skill_md)

    # ---------- loadSkill：路径解析 ----------

    def attachment_relative() -> None:
        loader = SkillLoader(project_root=root)
        result = load_json(loader.load_skill("alpha", "references/rules.md"))
        expect(result["filename"] == "references/rules.md",
               f"相对路径回显错误：{result}")

    check("路径：skill 相对路径", attachment_relative)

    def attachment_project_relative() -> None:
        loader = SkillLoader(project_root=root)
        result = load_json(
            loader.load_skill(
                "alpha", ".agents/skills/alpha/references/rules.md"
            )
        )
        expect(result["content"] == "# rules v1", "项目相对路径读取失败")

    check("路径：项目相对路径", attachment_project_relative)

    def attachment_absolute() -> None:
        loader = SkillLoader(project_root=root)
        absolute = (skills / "alpha" / "references" / "rules.md").resolve()
        result = load_json(loader.load_skill("alpha", str(absolute)))
        expect(result["content"] == "# rules v1", "绝对路径读取失败")

    check("路径：技能目录内的绝对路径", attachment_absolute)

    def bare_filename_recursive() -> None:
        loader = SkillLoader(project_root=root)
        result = load_json(loader.load_skill("alpha", "Notes.MD"))
        expect(result["filename"] == "docs/Notes.MD",
               f"递归搜索结果错误：{result}")

    check("路径：仅文件名时递归搜索", bare_filename_recursive)

    def bare_filename_case_insensitive() -> None:
        loader = SkillLoader(project_root=root)
        result = load_json(loader.load_skill("alpha", "notes.md"))
        expect(result["filename"] == "docs/Notes.MD", "文件名大小写不敏感搜索失败")

    check("路径：文件名大小写不敏感", bare_filename_case_insensitive)

    def ambiguous_filename() -> None:
        loader = SkillLoader(project_root=root)
        raw = call(loader, "alpha", "rules.md")
        data = load_json(raw)
        expect(data["ok"] is False, "同名文件应返回候选而不是直接读取")
        expect(data["error"]["type"] == "ambiguous_filename", f"错误类型：{data}")
        candidates = set(data["error"]["candidates"])
        expect(candidates == {"references/rules.md", "deep/nested/rules.md"},
               f"候选列表错误：{candidates}")

    check("路径：同名多文件返回候选列表", ambiguous_filename)

    def file_not_found() -> None:
        loader = SkillLoader(project_root=root)
        expect(error_type(call(loader, "alpha", "nope.md")) == "file_not_found",
               "不存在的文件应返回 file_not_found")
        expect(
            error_type(call(loader, "alpha", "docs/nope.md")) == "file_not_found",
            "不存在且含目录的路径应返回 file_not_found",
        )

    check("路径：文件不存在", file_not_found)

    def unknown_skill() -> None:
        loader = SkillLoader(project_root=root)
        expect(error_type(call(loader, "gamma")) == "skill_not_found",
               "未知技能应返回 skill_not_found")
        expect(error_type(call(loader, "")) == "invalid_arguments",
               "空技能名应返回 invalid_arguments")

    check("参数：未知技能 / 空技能名", unknown_skill)

    def skill_name_case_insensitive() -> None:
        loader = SkillLoader(project_root=root)
        result = load_json(loader.load_skill("ALPHA"))
        expect("ALPHA-BODY" in result.get("content", ""), "技能名应大小写不敏感")

    check("参数：技能名大小写不敏感", skill_name_case_insensitive)

    # ---------- 安全 ----------

    def traversal_relative() -> None:
        loader = SkillLoader(project_root=root)
        for filename in ("../beta/SKILL.md", "../../outside_secret.txt"):
            expect(
                error_type(call(loader, "alpha", filename))
                == "path_outside_skill",
                f"路径穿越未被拦截：{filename}",
            )

    check("安全：../ 路径穿越被拦截", traversal_relative)

    def traversal_absolute() -> None:
        loader = SkillLoader(project_root=root)
        secret = str((root / "outside_secret.txt").resolve())
        expect(
            error_type(call(loader, "alpha", secret)) == "path_outside_skill",
            "技能目录外的绝对路径未被拦截",
        )

    check("安全：目录外绝对路径被拦截", traversal_absolute)

    def traversal_backslash() -> None:
        loader = SkillLoader(project_root=root)
        # Windows 风格分隔符也要被识别并拦截
        expect(
            error_type(call(loader, "alpha", "..\\beta\\SKILL.md"))
            == "path_outside_skill",
            "反斜杠路径穿越未被拦截",
        )

    check("安全：反斜杠路径穿越被拦截", traversal_backslash)

    # ---------- Agent 集成 ----------

    def agent_integration() -> None:
        workspace = root / "workspace"
        settings = Settings(api_key="test-only", workspace=workspace)
        agent = Agent(settings=settings, project_root=root)
        try:
            prompt = agent.messages[0]["content"]
            expect("alpha" in prompt and "loadSkill" in prompt,
                   "Agent system prompt 未注入技能")
            expect("ALPHA-BODY" not in prompt, "Agent system prompt 泄露技能正文")

            names = {tool["function"]["name"] for tool in agent.tools}
            expect("loadSkill" in names, f"tools 中缺少 loadSkill：{sorted(names)}")
            expect({"fetchUrl", "searchWeb", "RunCli"} <= names,
                   "原有工具被破坏")

            execution = agent._dispatch_tool("loadSkill", '{"name": "alpha"}')
            expect(execution.ok, f"分发 loadSkill 失败：{execution.output}")
            expect("ALPHA-BODY" in execution.output, "分发结果缺少正文")

            outside = agent._dispatch_tool(
                "loadSkill", '{"name": "alpha", "filename": "../beta/SKILL.md"}'
            )
            expect(not outside.ok, "分发层未拦截越界路径")
            expect(
                json.loads(outside.output)["error"]["type"] == "path_outside_skill",
                f"越界错误类型错误：{outside.output}",
            )

            agent.reset()
            expect(not agent.skills.is_loaded("alpha"), "Agent.reset 未清空加载状态")
        finally:
            asyncio.run(agent.aclose())

    check("集成：Agent prompt / tools / 分发 / reset", agent_integration)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

VERBOSE = False


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="xAgent Skills 验收脚本")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示异常堆栈")
    args = parser.parse_args()
    VERBOSE = args.verbose

    with tempfile.TemporaryDirectory(prefix="xagent_skills_") as tmp:
        root = Path(tmp)
        build_fixture(root)
        run(root)

    # Windows 控制台默认 GBK，统一改成 UTF-8 以便输出中文用例名
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    failed = [item for item in _results if item[0] == FAIL]
    for status, name, detail in _results:
        print(f"[{status}] {name}")
        if detail:
            print(f"        {detail}")

    print(f"\n共 {len(_results)} 项：{len(_results) - len(failed)} PASS，{len(failed)} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
