"""配置加载。

优先级：环境变量 > config.ini > 内置默认值。

环境变量：
    XAGENT_API_KEY / DEEPSEEK_API_KEY
    XAGENT_BASE_URL
    XAGENT_MODEL
    XAGENT_WORKSPACE
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

# xAgent 项目根目录（core 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_workspace(raw: str, anchor: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = anchor / path
    return path.resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    """Agent 运行配置，全局只读。"""

    api_key: str
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-flash"
    stream: bool = True
    temperature: float = 0.0
    max_tool_rounds: int = 10
    request_interval: float = 0.8
    workspace: Path = PROJECT_ROOT / "workspace"
    config_path: Path = DEFAULT_CONFIG_PATH

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        path = Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH
        parser = configparser.ConfigParser()
        if path.exists():
            parser.read(path, encoding="utf-8")

        llm = parser["llm"] if parser.has_section("llm") else {}
        agent = parser["agent"] if parser.has_section("agent") else {}

        api_key = (
            os.getenv("XAGENT_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or llm.get("api_key", "")
        ).strip()
        if not api_key:
            raise ConfigError(
                f"未配置 API Key。请编辑 {path} 的 [llm] api_key，"
                "或设置环境变量 XAGENT_API_KEY。"
            )

        base_url = (
            os.getenv("XAGENT_BASE_URL") or llm.get("base_url", cls.base_url)
        ).strip()
        model = (os.getenv("XAGENT_MODEL") or llm.get("model_name", cls.model)).strip()

        raw_workspace = os.getenv("XAGENT_WORKSPACE") or agent.get("workspace", "workspace")
        workspace = _resolve_workspace(raw_workspace, path.parent)

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            stream=_to_bool(llm.get("stream"), cls.stream),
            temperature=float(llm.get("temperature", cls.temperature)),
            max_tool_rounds=max(1, int(agent.get("max_tool_rounds", cls.max_tool_rounds))),
            request_interval=max(0.0, float(agent.get("request_interval", cls.request_interval))),
            workspace=workspace,
            config_path=path,
        )
