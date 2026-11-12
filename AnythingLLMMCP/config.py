"""AnythingLLM MCP Server 配置。

所有可变项都从环境变量读取（可写进 .env，或用系统环境变量注入），
密钥不再硬编码在源码里。
"""

import os


def _flag(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


ANYTHINGLLM_BASE_URL = _flag("ANYTHINGLLM_BASE_URL", "http://localhost:3001")
ANYTHINGLLM_API_KEY = _flag("ANYTHINGLLM_API_KEY")

# 默认工作区 slug；留空时由调用方显式传入
DEFAULT_WORKSPACE_SLUG = _flag("ANYTHINGLLM_WORKSPACE")

# MCP Server 配置
MCP_SERVER_NAME = _flag("MCP_SERVER_NAME", "anythingllm-knowledge")
MCP_SERVER_VERSION = "1.1.0"

# 缓存策略（SEP-2549）
TOOL_TTL_MS = int(_flag("TOOL_TTL_MS", "60000") or 60000)  # 1 分钟
TOOL_CACHE_SCOPE = "user"

# HTTP 传输配置
HTTP_PORT = int(_flag("MCP_PORT", "9090") or 9090)
HTTP_HOST = _flag("MCP_HOST", "127.0.0.1")
HTTP_PATH = _flag("MCP_PATH", "/mcp")

# 请求超时与上传上限
REQUEST_TIMEOUT = float(_flag("ANYTHINGLLM_TIMEOUT", "30") or 30)
MAX_UPLOAD_BYTES = int(
    _flag("ANYTHINGLLM_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)) or 20 * 1024 * 1024
)


def missing_key_hint() -> str:
    return (
        "未配置 ANYTHINGLLM_API_KEY。请在 AnythingLLM 的 Settings → API Keys 生成密钥，"
        "再写入环境变量或 .env（模板见 .env.example）。"
    )
