"""配置加载与首次启动配置生成。

优先级：环境变量 > config.ini > 内置默认值。

设计原则：
    1. 所有硬编码参数都集中到本文件，业务代码只读 Settings 字段；
    2. 任何配置项缺失、写错类型都不会让程序崩溃，而是回退到默认值；
    3. 只有 API Key 缺失（或仍是模板占位符）才会抛出 ConfigError，
       因为此时无法真正调用大模型。

环境变量：
    XAGENT_CONFIG         指定 config.ini 路径
    XAGENT_API_KEY / DEEPSEEK_API_KEY
    XAGENT_BASE_URL
    XAGENT_MODEL
    XAGENT_WORKSPACE
    XAGENT_CONTEXT_WINDOW
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# xAgent 项目根目录（core 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"
CONFIG_FILENAME = "config.ini"

# 模板中的占位 Key：命中这些值说明用户还没有修改配置文件
API_KEY_PLACEHOLDERS = {
    "",
    "your_api_key_here",
    "your-api-key-here",
    "your-key-here",
    "sk-your-key-here",
    "sk-xxx",
    "changeme",
}

# 首次启动生成的配置文件模板（所有敏感字段都是 mock 占位值）
CONFIG_TEMPLATE = """\
# =============================================================================
# xAgent 配置文件
#
# 首次启动时自动生成。请至少修改 [llm] api_key，然后重新启动程序。
# 所有配置项都可以删除：删除后程序会自动使用内置默认值。
# 相对路径均以本文件所在目录为基准。
# =============================================================================

[llm]
# 大模型 API Key。建议改用环境变量 XAGENT_API_KEY，避免把密钥写进文件。
api_key = your_api_key_here
# OpenAI 兼容接口地址
base_url = https://api.deepseek.com/v1
# 模型名称
model_name = deepseek-flash
# 是否使用流式输出
stream = true
# 采样温度（0.0 更稳定，适合工具调用）
temperature = 0.0
# 单次回复最大 token 数；留空或填 0 表示交给服务端默认值
max_tokens = 4096

[agent]
# 单轮对话最多允许的模型往返次数（防止死循环）
max_tool_rounds = 10
# 每轮模型请求之间的限流缓冲（秒）
request_interval = 0.8
# 上下文窗口大小（token）。不知道模型上限时保持默认 128000
context_window = 128000
# 上下文占用达到该比例后暂停普通输入，只允许 /compact（0~1）
compact_threshold = 0.6
# 是否在会话初始化时自动注入 AGENTS.md 项目规范
agents_md = true

[paths]
# 文件操作沙箱根目录
workspace = workspace
# Agent Skills 目录
skills_dir = .agents/skills
# 会话历史保存目录（每个会话一个 JSON 文件）
session_dir = .agents/sessions
# 上下文压缩归档目录（.jsonl）
context_archive_dir = .agents/context
# MCP 服务器配置文件
mcp_config = .mcp.json

[session]
# 每轮对话结束后是否自动保存会话（/save、/quit、/new 始终会保存）
auto_save = true
# 自动生成会话标题时截取的最大字符数
title_max_chars = 30

[tui]
# 左侧边栏宽度（列）
sidebar_width = 34
# 界面主题：textual-dark / textual-light / nord / gruvbox 等
theme = textual-dark
# 是否显示上下文状态条与底部状态栏
show_status_bar = true

[security]
# 文件操作沙箱根目录（优先级高于 [paths] workspace）
sandbox_root = workspace
# 单轮对话允许执行的最大工具调用次数
max_tool_calls = 20

[search]
# 百度千帆 AI 搜索 API Key；不使用联网搜索时留空即可
baidu_qianfan_api_key =
# 千帆 AI 搜索接口地址
endpoint = https://qianfan.baidubce.com/v2/ai_search/web_search
# 单次搜索超时（秒）
timeout = 15
# 默认返回条数（1-20）
top_k = 10
"""


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def _settings_default(name: str) -> Any:
    """读取 Settings 字段的内置默认值。

    Settings 使用 slots=True，类属性是 member_descriptor 而不是默认值，
    因此不能用 ``cls.field`` 读取，必须通过 dataclasses.fields() 获取。
    """
    for item in fields(Settings):
        if item.name == name:
            return item.default
    raise KeyError(f"未知的配置字段：{name}")


# =============================================================================
# 通用解析工具：任何非法值都回退到默认值，绝不抛异常
# =============================================================================


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | None, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _to_float(
    value: str | None,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _resolve_path(raw: str, anchor: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = anchor / path
    return path.resolve()


def _read(parser: configparser.ConfigParser, section: str, key: str) -> str | None:
    """读取字符串配置项；缺失或空串统一返回 None。"""
    if not parser.has_section(section):
        return None
    value = parser.get(section, key, fallback=None)
    if value is None:
        return None
    value = value.strip()
    return value or None


# =============================================================================
# 配置文件路径与首次生成
# =============================================================================


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    """确定本次运行使用的 config.ini 路径。

    顺序：显式参数 > XAGENT_CONFIG > 当前工作目录。
    任务要求以「运行 xAgent 时的当前工作目录」为准，因此不再隐式回退到
    源码目录；从项目根目录启动时两者本来就是同一个文件。
    """
    if config_path:
        return Path(config_path).expanduser().resolve()

    env_path = os.getenv("XAGENT_CONFIG", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    return (Path.cwd() / CONFIG_FILENAME).resolve()


def ensure_config(config_path: str | Path | None = None) -> tuple[Path, bool]:
    """检查配置文件是否存在；不存在时生成带注释的模板。

    返回 (配置路径, 是否为本次新生成)。
    """
    path = resolve_config_path(config_path)
    if path.exists():
        return path, False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # "x" 模式：并发启动时也不会覆盖别人刚创建的文件
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(CONFIG_TEMPLATE)
    except FileExistsError:
        return path, False
    except OSError as exc:
        raise ConfigError(f"无法生成默认配置文件 {path}：{exc}") from exc
    return path, True


# =============================================================================
# Settings
# =============================================================================


@dataclass(frozen=True, slots=True)
class Settings:
    """Agent 运行配置，全局只读。

    *_is_default 字段用于区分「配置文件里明确写了」和「使用内置默认值」：
    例如 skills_dir 默认为空时，SkillLoader 会基于当前工作目录推导，
    这样测试可以把项目根目录指向临时目录。
    """

    api_key: str

    # ---------- 大模型 ----------
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-flash"
    stream: bool = True
    temperature: float = 0.0
    max_tokens: int | None = 4096

    # ---------- Agent / 上下文 ----------
    max_tool_rounds: int = 10
    max_tool_calls: int = 20
    request_interval: float = 0.8
    context_window: int = 128_000
    context_window_is_default: bool = True
    compact_threshold: float = 0.6
    agents_md_enabled: bool = True

    # ---------- 路径 ----------
    workspace: Path = PROJECT_ROOT / "workspace"
    skills_dir: Path = PROJECT_ROOT / ".agents" / "skills"
    skills_dir_is_default: bool = True
    session_dir: Path = PROJECT_ROOT / ".agents" / "sessions"
    session_dir_is_default: bool = True
    context_archive_dir: Path = PROJECT_ROOT / ".agents" / "context"
    context_archive_dir_is_default: bool = True
    mcp_config_path: Path = PROJECT_ROOT / ".mcp.json"
    mcp_config_path_is_default: bool = True

    # ---------- 会话 ----------
    auto_save_sessions: bool = True
    session_title_max_chars: int = 30

    # ---------- TUI ----------
    sidebar_width: int = 34
    tui_theme: str = "textual-dark"
    show_status_bar: bool = True

    config_path: Path = DEFAULT_CONFIG_PATH

    @property
    def sandbox_root(self) -> Path:
        """文件操作沙箱根目录（与 workspace 保持同一值）。"""
        return self.workspace

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        path = resolve_config_path(config_path)
        parser = configparser.ConfigParser(interpolation=None)
        if path.exists():
            try:
                parser.read(path, encoding="utf-8")
            except (configparser.Error, OSError, UnicodeDecodeError):
                # 配置文件损坏时退化为全部默认值，不让程序崩溃
                parser = configparser.ConfigParser(interpolation=None)

        # ---------- LLM ----------
        api_key = (
            os.getenv("XAGENT_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or _read(parser, "llm", "api_key")
            or ""
        ).strip()
        if api_key.lower() in API_KEY_PLACEHOLDERS:
            raise ConfigError(
                f"尚未配置可用的 API Key。请编辑 {path} 的 [llm] api_key，"
                "或设置环境变量 XAGENT_API_KEY。"
            )

        base_url = (
            os.getenv("XAGENT_BASE_URL")
            or _read(parser, "llm", "base_url")
            or _settings_default("base_url")
        ).strip()
        model = (
            os.getenv("XAGENT_MODEL")
            or _read(parser, "llm", "model_name")
            or _read(parser, "llm", "model")
            or _settings_default("model")
        ).strip()
        stream = _to_bool(_read(parser, "llm", "stream"), _settings_default("stream"))
        temperature = _to_float(
            _read(parser, "llm", "temperature"), _settings_default("temperature"), 0.0, 2.0
        )
        if not parser.has_option("llm", "max_tokens"):
            # 配置项缺失：使用内置默认值
            max_tokens: int | None = _settings_default("max_tokens")
        else:
            # 显式留空 / 填 0 / 填了非法值：交给服务端默认值
            raw_max_tokens = _read(parser, "llm", "max_tokens")
            parsed_max = _to_int(raw_max_tokens, 0, 0)
            max_tokens = parsed_max if parsed_max > 0 else None

        # ---------- Agent / 上下文 ----------
        max_tool_rounds = _to_int(
            _read(parser, "agent", "max_tool_rounds"), _settings_default("max_tool_rounds"), 1
        )
        max_tool_calls = _to_int(
            _read(parser, "security", "max_tool_calls"), _settings_default("max_tool_calls"), 1
        )
        request_interval = _to_float(
            _read(parser, "agent", "request_interval"), _settings_default("request_interval"), 0.0
        )
        agents_md_enabled = _to_bool(
            _read(parser, "agent", "agents_md"), _settings_default("agents_md_enabled")
        )

        raw_window = (
            os.getenv("XAGENT_CONTEXT_WINDOW", "").strip()
            or _read(parser, "agent", "context_window")
        )
        context_window_is_default = raw_window is None
        context_window = _to_int(
            raw_window, _settings_default("context_window"), 1
        )

        raw_threshold = _read(parser, "agent", "compact_threshold")
        threshold = _to_float(raw_threshold, _settings_default("compact_threshold"), 0.05, 100.0)
        # 允许用户写 60（表示 60%）或 0.6（表示 60%）
        if threshold > 1.0:
            threshold /= 100.0
        compact_threshold = min(max(threshold, 0.05), 1.0)

        # ---------- 路径 ----------
        # workspace 优先级：环境变量 > [security] sandbox_root > [paths] workspace
        #                   > [agent] workspace > 默认 workspace
        raw_workspace = (
            os.getenv("XAGENT_WORKSPACE")
            or _read(parser, "security", "sandbox_root")
            or _read(parser, "paths", "workspace")
            or _read(parser, "agent", "workspace")
        )
        workspace = _resolve_path(raw_workspace or "workspace", path.parent)

        raw_skills = _read(parser, "paths", "skills_dir")
        skills_dir = _resolve_path(
            raw_skills or ".agents/skills", path.parent
        )
        raw_sessions = _read(parser, "paths", "session_dir")
        session_dir = _resolve_path(
            raw_sessions or ".agents/sessions", path.parent
        )
        raw_archives = _read(
            parser, "paths", "context_archive_dir"
        )
        context_archive_dir = _resolve_path(
            raw_archives or ".agents/context", path.parent
        )
        raw_mcp = _read(parser, "paths", "mcp_config") or _read(
            parser, "paths", "mcp_config_path"
        )
        mcp_config_path = _resolve_path(
            raw_mcp or ".mcp.json", path.parent
        )

        # ---------- 会话 ----------
        auto_save_sessions = _to_bool(
            _read(parser, "session", "auto_save"), _settings_default("auto_save_sessions")
        )
        session_title_max_chars = _to_int(
            _read(parser, "session", "title_max_chars"),
            _settings_default("session_title_max_chars"),
            8,
        )

        # ---------- TUI ----------
        sidebar_width = min(
            max(
                _to_int(
                    _read(parser, "tui", "sidebar_width"),
                    _settings_default("sidebar_width"),
                    16,
                ),
                16,
            ),
            80,
        )
        tui_theme = (
            _read(parser, "tui", "theme") or _settings_default("tui_theme")
        ).strip()
        show_status_bar = _to_bool(
            _read(parser, "tui", "show_status_bar"), _settings_default("show_status_bar")
        )

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            max_tool_rounds=max_tool_rounds,
            max_tool_calls=max_tool_calls,
            request_interval=request_interval,
            context_window=context_window,
            context_window_is_default=context_window_is_default,
            compact_threshold=compact_threshold,
            agents_md_enabled=agents_md_enabled,
            workspace=workspace,
            skills_dir=skills_dir,
            skills_dir_is_default=raw_skills is None,
            session_dir=session_dir,
            session_dir_is_default=raw_sessions is None,
            context_archive_dir=context_archive_dir,
            context_archive_dir_is_default=raw_archives is None,
            mcp_config_path=mcp_config_path,
            mcp_config_path_is_default=raw_mcp is None,
            auto_save_sessions=auto_save_sessions,
            session_title_max_chars=session_title_max_chars,
            sidebar_width=sidebar_width,
            tui_theme=tui_theme,
            show_status_bar=show_status_bar,
            config_path=path,
        )
