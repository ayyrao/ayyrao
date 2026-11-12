"""MCP 客户端管理：读取 .mcp.json、发现工具、转发调用。

为什么单独放在 core/mcp_client.py，而不是全部塞进 core/agent.py：

1. MCP SDK 是可选依赖。单独成模块后，未安装 mcp 时 Agent 仍可正常启动，
   只会在日志里提示“MCP 已跳过”；
2. 一个 MCP Server 需要常驻连接、后台任务、请求队列和生命周期管理，
   放在 agent.py 会让主循环文件迅速膨胀；
3. agent.py 只需要接入一个很薄的异步分发入口，原有 _dispatch_tool 的
   本地工具逻辑完全不用改。

设计要点：
- 配置从“项目运行根目录”（进程 cwd）读取，而不是源码目录；
- 每个 Server 一个后台 asyncio.Task：连接、initialize、list_tools 后常驻；
  所有 call_tool 都投递到该 Task 内串行执行，避免跨 Task 使用 SDK 会话；
- 单个 Server 连接/调用失败只记录日志，不影响其他 Server 与本地工具；
- 对外工具名：{server_name}__{tool_name}，内部维护映射表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .tools import ToolExecution

try:  # 可选依赖：未安装 MCP SDK 时 Agent 仍可启动
    import httpx2
    import mcp.types as mcp_types
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    _MCP_IMPORT_ERROR: Exception | None = None
    _MCP_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - 取决于运行环境
    httpx2 = None  # type: ignore[assignment]
    mcp_types = None  # type: ignore[assignment]
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]
    _MCP_IMPORT_ERROR = exc
    _MCP_AVAILABLE = False


LOGGER = logging.getLogger("xagent.mcp")

MCP_CONFIG_FILENAME = ".mcp.json"
DEFAULT_TIMEOUT = 60.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 600.0
MAX_TOOL_PAGES = 50

# OpenAI / DeepSeek function calling 对工具名的限制
_FUNCTION_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_ENV_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)


def configure_mcp_logging(level: int = logging.INFO) -> None:
    """让 MCP 启动日志在 CLI 中可见。

    只给 xagent.mcp 这个 logger 安装独立的 stderr handler，避免影响
    httpx / mcp SDK 自身的日志，也避免把全局日志级别改乱。
    """
    logger = logging.getLogger("xagent.mcp")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


# =============================================================================
# 配置读取与工具 Schema 转换
# =============================================================================


def _expand_env(value: str) -> str:
    """展开 ${VAR} / ${VAR:-default} / $VAR；未定义的变量展开为空串。"""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3)
        default = match.group(2) or ""
        return os.environ.get(name, default)

    return _ENV_PATTERN.sub(replace, value)


def _describe_exception(exc: BaseException) -> str:
    """把异常链压成一段可读文本，保留真实原因而不是只写“连接失败”。

    MCP 2.x 的 HTTP 传输在连接失败时会抛 ExceptionGroup，真正原因
    （ConnectionRefusedError / SSLError 等）藏在子异常里，所以这里要
    同时展开 ExceptionGroup.exceptions 和 __cause__/__context__。
    """
    parts: list[str] = []
    seen_ids: set[int] = set()
    seen_texts: set[str] = set()
    queue: list[BaseException] = [exc]

    while queue and len(parts) < 10:
        current = queue.pop(0)
        if id(current) in seen_ids:
            continue
        seen_ids.add(id(current))

        text = str(current).strip()
        normalized = text.lower()
        noise = (
            normalized.startswith("cancelled via cancel scope")
            or normalized.startswith("unhandled errors in a taskgroup")
            or type(current).__name__ in {"WouldBlock", "CancelledError"}
        )
        if text and normalized not in seen_texts and not noise:
            seen_texts.add(normalized)
            parts.append(f"{type(current).__name__}: {text}")
        elif not text and type(current).__name__ not in {"WouldBlock", "CancelledError"}:
            parts.append(type(current).__name__)

        sub_exceptions = getattr(current, "exceptions", None)
        if isinstance(sub_exceptions, (list, tuple)):
            queue.extend(sub_exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None and cause is not current:
            queue.append(cause)

    if not parts:
        parts.append(type(exc).__name__)
    detail = " <- ".join(parts)
    if len(detail) > 800:
        detail = detail[:800] + "…"
    return detail


def _exception_hint(exc: BaseException) -> str:
    """针对 SSL / 连接拒绝给出可操作的提示；绝不建议关闭证书校验。"""
    detail = _describe_exception(exc).lower()
    if isinstance(exc, ssl.SSLError) or "certificate" in detail or "ssl" in detail:
        return (
            "\n[MCP] 提示：SSL 证书校验失败。请检查系统根证书、"
            "HTTPS_PROXY / HTTP_PROXY / NO_PROXY 环境变量，或为内网服务配置"
            "可信证书。本程序不会使用 verify=False 绕过证书校验。"
        )
    if "connection refused" in detail or "connecterror" in detail or "all connection attempts failed" in detail:
        return "\n[MCP] 提示：请确认该 MCP 服务已启动，且 host/port 与配置文件一致。"
    return ""


def _host_in_no_proxy(host: str) -> bool:
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    host = host.strip().lower().strip("[]")
    if not host:
        return False

    for entry in raw.split(","):
        item = entry.strip().lower()
        if not item:
            continue
        if item == "*":
            return True
        # 去掉可选的端口，例如 127.0.0.1:8090
        if item.startswith("["):
            item = item.split("]", 1)[0].lstrip("[")
        elif ":" in item:
            item = item.rsplit(":", 1)[0]
        item = item.lstrip(".")
        if host == item or host.endswith("." + item):
            return True
    return False


def _proxy_for_url(url: str) -> str | None:
    """按目标 URL 的协议读取 HTTPS_PROXY / HTTP_PROXY，并遵守 NO_PROXY。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if _host_in_no_proxy(host):
        return None

    if parsed.scheme.lower() == "https":
        keys = ("HTTPS_PROXY", "https_proxy")
    else:
        keys = ("HTTP_PROXY", "http_proxy")

    for key in keys + ("ALL_PROXY", "all_proxy"):
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def _normalize_timeout(raw: object, default: float = DEFAULT_TIMEOUT) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, MIN_TIMEOUT), MAX_TIMEOUT)


def _normalize_transport(config: dict[str, Any]) -> str:
    raw = config.get("type") or config.get("transport") or ""
    transport = str(raw).strip().lower().replace("-", "_")
    if transport in {"streamablehttp", "streamable_http", "http", "https"}:
        return "streamable_http"
    if transport in {"stdio", "command"}:
        return "stdio"
    if not transport:
        return "stdio" if config.get("command") else "streamable_http"
    return transport


def _tool_schema(
    exposed_name: str, tool: Any, server_name: str
) -> dict[str, Any] | None:
    """把 MCP Tool 转成 OpenAI function calling schema。"""
    parameters = getattr(tool, "input_schema", None)
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    else:
        parameters = dict(parameters)
        # 顶层 $schema 对 function calling 没有意义，部分服务端会拒绝
        parameters.pop("$schema", None)
        if "type" not in parameters:
            parameters["type"] = "object"
        if parameters.get("type") == "object" and "properties" not in parameters:
            parameters["properties"] = {}

    description = getattr(tool, "description", None)
    if not isinstance(description, str) or not description.strip():
        description = f"{server_name} 提供的 MCP 工具 {getattr(tool, 'name', '')}"

    if not _FUNCTION_NAME_RE.fullmatch(exposed_name):
        return None

    return {
        "type": "function",
        "function": {
            "name": exposed_name,
            "description": description,
            "parameters": parameters,
        },
    }


def _error_payload(
    server: str, tool: str, error_type: str, message: str
) -> dict[str, Any]:
    return {
        "ok": False,
        "server": server,
        "tool": tool,
        "error": {"type": error_type, "message": message},
    }


def _content_bytes(item: Any) -> int:
    data = getattr(item, "data", None) or getattr(item, "blob", None)
    return len(data) if isinstance(data, (str, bytes)) else 0


def _result_payload(
    result: Any, server_name: str, tool_name: str
) -> dict[str, Any]:
    """把 CallToolResult 统一封装成 LLM 易读的 JSON。

    - TextContent 按顺序拼成 text；
    - structured_content 原样放进 structured；
    - 图片/音频/资源只返回元信息，避免把 base64 塞进上下文；
    - is_error=true 时同时给出 error.type / error.message。
    """
    content = getattr(result, "content", None) or []
    texts: list[str] = []
    extras: list[dict[str, Any]] = []

    for item in content:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            text = getattr(item, "text", "")
            if isinstance(text, str):
                texts.append(text)
        elif item_type == "image":
            extras.append(
                {
                    "type": "image",
                    "mime_type": getattr(item, "mime_type", None),
                    "bytes": _content_bytes(item),
                }
            )
        elif item_type == "audio":
            extras.append(
                {
                    "type": "audio",
                    "mime_type": getattr(item, "mime_type", None),
                    "bytes": _content_bytes(item),
                }
            )
        elif item_type == "resource":
            resource = getattr(item, "resource", None)
            entry: dict[str, Any] = {
                "type": "resource",
                "uri": str(getattr(resource, "uri", "")),
                "mime_type": getattr(resource, "mime_type", None),
            }
            resource_text = getattr(resource, "text", None)
            if isinstance(resource_text, str):
                entry["text"] = resource_text
            else:
                entry["bytes"] = len(getattr(resource, "blob", None) or b"")
            extras.append(entry)
        elif item_type == "resource_link":
            extras.append(
                {
                    "type": "resource_link",
                    "uri": str(getattr(item, "uri", "")),
                    "name": getattr(item, "name", None),
                    "description": getattr(item, "description", None),
                }
            )
        else:
            extras.append({"type": item_type or "unknown"})

    text = "\n".join(part for part in texts if part)
    is_error = bool(getattr(result, "is_error", False))
    structured = getattr(result, "structured_content", None)

    payload: dict[str, Any] = {
        "ok": not is_error,
        "server": server_name,
        "tool": tool_name,
        "text": text,
    }
    if structured is not None:
        payload["structured"] = structured
    if extras:
        payload["content"] = extras
    if is_error:
        payload["error"] = {
            "type": "mcp_tool_error",
            "message": text or "MCP 工具返回 is_error=true，但没有提供文本错误信息。",
        }
    return payload


# =============================================================================
# 单个 MCP Server 的长连接
# =============================================================================


@dataclass(slots=True)
class MCPServerConnection:
    """一个 MCP Server 的常驻连接。

    连接、initialize、list_tools 以及后续所有 call_tool 都在同一个
    asyncio.Task（_serve）里执行。调用方只通过 asyncio.Queue 投递请求，
    这样不会跨 Task 直接操作 SDK 的会话对象。
    """

    name: str
    config: dict[str, Any]
    default_timeout: float = DEFAULT_TIMEOUT

    transport: str = ""
    timeout: float = DEFAULT_TIMEOUT
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    status: str = "pending"  # pending / connecting / connected / failed / stopped
    error: str | None = None
    tools: list[Any] = field(default_factory=list)

    _session: Any = None
    _task: asyncio.Task[None] | None = None
    _queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _connected: bool = False
    _stopping: bool = False

    def __post_init__(self) -> None:
        self.transport = _normalize_transport(self.config)
        self.timeout = _normalize_timeout(
            self.config.get("timeout"), self.default_timeout
        )
        self.url = str(self.config.get("url") or "").strip()

        raw_headers = self.config.get("headers")
        if isinstance(raw_headers, dict):
            self.headers = {
                str(key): _expand_env(str(value))
                for key, value in raw_headers.items()
            }

    # ---------- 生命周期 ----------

    async def connect(self) -> bool:
        """启动后台任务并等待 ready；失败只返回 False，不抛给调用方。"""
        if not _MCP_AVAILABLE:
            self.status = "failed"
            self.error = f"MCP SDK 未安装：{_MCP_IMPORT_ERROR}"
            return False

        self.status = "connecting"
        self._task = asyncio.create_task(
            self._serve(), name=f"xagent-mcp-{self.name}"
        )

        try:
            # 留 2 秒余量，优先拿到 _serve 内部的真实错误信息
            await asyncio.wait_for(self._ready.wait(), self.timeout + 2)
        except asyncio.TimeoutError:
            self.status = "failed"
            self.error = f"连接超时（>{self.timeout:g} 秒）"
            LOGGER.error(
                "[MCP] %s 连接失败（%s），已跳过，不影响其他工具",
                self.name,
                self.error,
            )
            await self.shutdown()
            return False
        except asyncio.CancelledError:
            await self.shutdown()
            raise

        if not self._connected:
            await self.shutdown()
            return False
        return True

    async def _serve(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                streams = await self._open_transport(stack)
                read_stream, write_stream = self._split_streams(streams)

                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self.timeout,
                ) as session:
                    self._session = session
                    await asyncio.wait_for(session.initialize(), self.timeout)
                    self.tools = await asyncio.wait_for(
                        self._list_tools(session), self.timeout
                    )

                    self._connected = True
                    self.status = "connected"
                    self.error = None
                    prefixed = ", ".join(
                        f"{self.name}__{getattr(tool, 'name', '?')}"
                        for tool in self.tools
                    )
                    LOGGER.info(
                        "[MCP] %s 连接成功，工具 %d 个：%s",
                        self.name,
                        len(self.tools),
                        prefixed or "（无工具）",
                    )
                    self._ready.set()

                    await self._run_requests(session)

        except asyncio.CancelledError:
            self.status = "stopped"
            raise
        except Exception as exc:
            self.status = "failed"
            self.error = _describe_exception(exc)
            self._ready.set()
            if not self._stopping:
                LOGGER.error(
                    "[MCP] %s 连接失败（%s），已跳过，不影响其他工具%s",
                    self.name,
                    self.error,
                    _exception_hint(exc),
                )
        finally:
            self._connected = False
            self._session = None
            self._ready.set()
            self._fail_pending(f"MCP 服务 {self.name} 连接已关闭")
            if self.status not in {"failed", "stopped"}:
                self.status = "stopped"

    async def _open_transport(self, stack: AsyncExitStack) -> Any:
        if self.transport == "stdio":
            command = self.config.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("stdio 类型缺少 command 配置。")

            raw_args = self.config.get("args")
            args = (
                [_expand_env(str(item)) for item in raw_args]
                if isinstance(raw_args, list)
                else []
            )
            raw_env = self.config.get("env")
            env = (
                {
                    str(key): _expand_env(str(value))
                    for key, value in raw_env.items()
                }
                if isinstance(raw_env, dict)
                else None
            )
            cwd = self.config.get("cwd")
            params = StdioServerParameters(
                command=_expand_env(command),
                args=args,
                env=env,
                cwd=str(Path(_expand_env(str(cwd))).expanduser()) if cwd else None,
            )
            return await stack.enter_async_context(stdio_client(params))

        if self.transport != "streamable_http":
            raise ValueError(
                f"不支持的 MCP 传输类型：{self.transport!r}"
                "（只支持 stdio / streamable_http）"
            )

        if not self.url:
            raise ValueError("streamable_http 类型缺少 url 配置。")
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"非法的 MCP URL：{self.url!r}")

        timeout = httpx2.Timeout(
            connect=min(30.0, self.timeout),
            read=self.timeout,
            write=self.timeout,
            pool=self.timeout,
        )
        proxy = _proxy_for_url(self.url)
        if proxy:
            LOGGER.info("[MCP] %s 使用代理：%s", self.name, proxy)
        else:
            LOGGER.debug("[MCP] %s 未配置代理或命中 NO_PROXY", self.name)

        # 不传 verify=False；证书问题必须暴露给用户修复。
        http_client = httpx2.AsyncClient(
            headers=self.headers or None,
            timeout=timeout,
            proxy=proxy,
        )
        await stack.enter_async_context(http_client)
        return await stack.enter_async_context(
            streamable_http_client(self.url, http_client=http_client)
        )

    @staticmethod
    def _split_streams(streams: Any) -> tuple[Any, Any]:
        """兼容 SDK 返回 tuple 或带 read_stream/write_stream 字段的对象。"""
        if isinstance(streams, tuple) and len(streams) >= 2:
            return streams[0], streams[1]

        read_stream = getattr(streams, "read_stream", None) or getattr(
            streams, "read", None
        )
        write_stream = getattr(streams, "write_stream", None) or getattr(
            streams, "write", None
        )
        if read_stream is None or write_stream is None:
            raise TypeError(
                "MCP transport 返回值无法解析为 (read_stream, write_stream)："
                f"{type(streams)!r}"
            )
        return read_stream, write_stream

    async def _list_tools(self, session: Any) -> list[Any]:
        tools: list[Any] = []
        cursor: str | None = None

        for _ in range(MAX_TOOL_PAGES):
            if cursor:
                result = await session.list_tools(
                    params=mcp_types.PaginatedRequestParams(cursor=cursor)
                )
            else:
                result = await session.list_tools()

            page = getattr(result, "tools", None) or []
            tools.extend(page)
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                return tools

        LOGGER.warning(
            "[MCP] %s 的工具分页超过 %d 页，停止继续拉取",
            self.name,
            MAX_TOOL_PAGES,
        )
        return tools

    # ---------- 请求转发 ----------

    async def _run_requests(self, session: Any) -> None:
        """在连接所在 Task 内串行执行 call_tool，直到 shutdown。"""
        while not self._stopping:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if item is None:
                break

            tool_name, arguments, future = item
            if future.done():
                continue

            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=arguments),
                    self.timeout,
                )
                if not future.done():
                    future.set_result(
                        _result_payload(result, self.name, tool_name)
                    )
            except asyncio.TimeoutError:
                if not future.done():
                    future.set_result(
                        _error_payload(
                            self.name,
                            tool_name,
                            "mcp_timeout",
                            f"MCP 工具调用超时（>{self.timeout:g} 秒）。",
                        )
                    )
            except asyncio.CancelledError:
                if not future.done():
                    future.set_result(
                        _error_payload(
                            self.name,
                            tool_name,
                            "mcp_cancelled",
                            "MCP 工具调用已取消。",
                        )
                    )
                raise
            except Exception as exc:
                detail = _describe_exception(exc)
                if not future.done():
                    future.set_result(
                        _error_payload(
                            self.name,
                            tool_name,
                            "mcp_call_failed",
                            f"调用 MCP 工具失败：{detail}",
                        )
                    )
                LOGGER.error(
                    "[MCP] %s 工具 %s 调用失败：%s",
                    self.name,
                    tool_name,
                    detail,
                )

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        """把一次调用投递给后台连接任务，并等待结果。"""
        if not self._connected or self._session is None:
            reason = f"（{self.error}）" if self.error else ""
            return False, _error_payload(
                self.name,
                tool_name,
                "mcp_server_unavailable",
                f"MCP 服务 {self.name} 未连接或已断开{reason}。",
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        await self._queue.put((tool_name, arguments, future))

        try:
            payload = await asyncio.wait_for(future, self.timeout + 2)
        except asyncio.TimeoutError:
            payload = _error_payload(
                self.name,
                tool_name,
                "mcp_timeout",
                f"MCP 工具调用超时（>{self.timeout:g} 秒），已放弃等待。",
            )
        except asyncio.CancelledError:
            raise

        return bool(payload.get("ok")), payload

    # ---------- 收尾 ----------

    def _fail_pending(self, message: str) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is None:
                continue
            tool_name, _arguments, future = item
            if not future.done():
                future.set_result(
                    _error_payload(
                        self.name,
                        tool_name,
                        "mcp_disconnected",
                        message,
                    )
                )

    async def shutdown(self) -> None:
        self._stopping = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover - 默认队列无上限
            pass

        task = self._task
        if task is None:
            return
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except BaseException:  # 取消/关闭都不应影响其他服务
                    pass
            except BaseException:  # 取消/关闭都不应影响其他服务
                pass

        self._connected = False
        self._session = None
        if self.status not in {"failed", "stopped"}:
            self.status = "stopped"

    @property
    def connected(self) -> bool:
        return self._connected

    def status_line(self) -> str:
        line = f"{self.name}: {self.status}，工具 {len(self.tools)} 个"
        if self.error:
            line += f"（{self.error}）"
        return line


# =============================================================================
# 映射表与 MCP 管理器
# =============================================================================


@dataclass(frozen=True, slots=True)
class MCPToolBinding:
    exposed_name: str
    server_name: str
    tool_name: str
    schema: dict[str, Any]


class MCPManager:
    """读取 .mcp.json，异步加载所有服务，并维护工具映射表。"""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        default_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        raw_path = (
            config_path
            or os.getenv("XAGENT_MCP_CONFIG")
            or (Path.cwd() / MCP_CONFIG_FILENAME)
        )
        self.config_path = Path(raw_path).expanduser().resolve(strict=False)
        self.default_timeout = _normalize_timeout(default_timeout)

        self.servers: dict[str, MCPServerConnection] = {}
        self._tool_map: dict[str, MCPToolBinding] = {}
        self.schemas: list[dict[str, Any]] = []
        self.warnings: list[str] = []

        self.started = False
        self.config_error: str | None = None
        self._lock = asyncio.Lock()

    # ---------- 启动 ----------

    @property
    def sdk_available(self) -> bool:
        """MCP Python SDK 是否可用（供 TUI 边栏显示提示）。"""
        return _MCP_AVAILABLE

    async def start(
        self,
        reserved_names: set[str] | None = None,
        *,
        on_update: Callable[[], None] | None = None,
        sequential: bool = False,
    ) -> list[dict[str, Any]]:
        """连接所有服务；任何单个服务失败都不会影响其余服务。

        sequential=True 时逐个连接，每完成一个就调用 on_update()，
        供 TUI 边栏实时刷新 MCP 区域；False 时保持原有并发行为。
        """
        async with self._lock:
            if self.started:
                return list(self.schemas)
            self.started = True

            def notify() -> None:
                if on_update is None:
                    return
                try:
                    on_update()
                except Exception:  # 界面回调异常不能影响连接流程
                    LOGGER.exception("[MCP] on_update 回调执行失败")

            if not _MCP_AVAILABLE:
                message = (
                    "未安装 MCP Python SDK（pip install mcp），"
                    f"所有 MCP 服务已跳过：{_MCP_IMPORT_ERROR}"
                )
                self.config_error = message
                LOGGER.warning("[MCP] %s", message)
                notify()
                return []

            try:
                servers_config = self._load_config()
            except Exception as exc:
                message = _describe_exception(exc)
                self.config_error = message
                LOGGER.error(
                    "[MCP] 配置文件读取失败（%s），已跳过所有 MCP 服务", message
                )
                notify()
                return []

            if not servers_config:
                notify()
                return []

            for name, raw_config in servers_config.items():
                if not isinstance(raw_config, dict):
                    warning = f"{name}: 配置必须是 JSON 对象，已跳过"
                    self.warnings.append(warning)
                    LOGGER.warning("[MCP] %s", warning)
                    continue
                if raw_config.get("enabled") is False:
                    LOGGER.info("[MCP] %s 在配置中被禁用，已跳过", name)
                    continue
                self.servers[name] = MCPServerConnection(
                    name=name,
                    config=raw_config,
                    default_timeout=self.default_timeout,
                )

            if not self.servers:
                notify()
                return []

            reserved = reserved_names or set()

            if sequential:
                # 逐个连接：先渲染界面，再在后台慢慢连，不阻塞用户输入。
                for name, server in self.servers.items():
                    try:
                        await server.connect()
                    except asyncio.CancelledError:
                        await server.shutdown()
                        raise
                    except Exception as exc:
                        # connect() 内部通常已收敛异常，这里只做兜底
                        server.status = "failed"
                        server.error = _describe_exception(exc)
                        await server.shutdown()
                    self._register_tools(reserved, only=server)
                    notify()
            else:
                names = list(self.servers)
                results = await asyncio.gather(
                    *(self.servers[name].connect() for name in names),
                    return_exceptions=True,
                )

                for name, result in zip(names, results):
                    if isinstance(result, BaseException):
                        detail = _describe_exception(result)
                        if not isinstance(result, asyncio.CancelledError):
                            LOGGER.error(
                                "[MCP] %s 初始化异常（%s），已跳过，不影响其他工具",
                                name,
                                detail,
                            )
                        await self.servers[name].shutdown()

                self._register_tools(reserved)

            connected = sum(1 for srv in self.servers.values() if srv.connected)
            failed = len(self.servers) - connected
            LOGGER.info(
                "[MCP] 加载完成：%d 个服务连接成功，%d 个失败；"
                "注册 MCP 工具 %d 个",
                connected,
                failed,
                len(self.schemas),
            )
            # 并发模式只在全部结束时通知一次
            if not sequential:
                notify()
            return list(self.schemas)

    def _load_config(self) -> dict[str, Any]:
        path = self.config_path
        LOGGER.info("[MCP] 读取配置：%s", path)
        if not path.is_file():
            self.config_error = f"未找到配置文件：{path}"
            LOGGER.warning(
                "[MCP] 未找到 %s，本次不加载 MCP 工具", path
            )
            return {}

        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("配置文件顶层必须是 JSON 对象。")

        servers = raw.get("mcpServers", raw.get("mcp_servers"))
        if servers is None:
            # 兼容直接把 server 名放在顶层的写法
            servers = {
                key: value
                for key, value in raw.items()
                if isinstance(value, dict)
                and ("url" in value or "command" in value)
            }
        if not isinstance(servers, dict):
            raise ValueError("mcpServers 必须是 JSON 对象。")
        return servers

    # ---------- 工具注册 ----------

    def _register_tools(
        self,
        reserved_names: set[str],
        *,
        only: MCPServerConnection | None = None,
    ) -> None:
        # 增量注册时 taken 必须包含已注册的 MCP 工具，避免重名
        taken = set(reserved_names) | set(self._tool_map)
        targets = [only] if only is not None else list(self.servers.values())

        for server in targets:
            if not server.connected:
                continue
            for tool in server.tools:
                original = getattr(tool, "name", None)
                if not isinstance(original, str) or not original.strip():
                    self.warnings.append(f"{server.name}: 跳过无名工具")
                    continue
                original = original.strip()
                exposed = f"{server.name}__{original}"

                if exposed in self._tool_map or exposed in taken:
                    warning = f"工具名冲突，已跳过：{exposed}"
                    self.warnings.append(warning)
                    LOGGER.warning("[MCP] %s", warning)
                    continue

                schema = _tool_schema(exposed, tool, server.name)
                if schema is None:
                    warning = (
                        f"工具名不符合 function calling 规范"
                        f"（最长 64，仅字母/数字/下划线/连字符），已跳过：{exposed}"
                    )
                    self.warnings.append(warning)
                    LOGGER.warning("[MCP] %s", warning)
                    continue

                self._tool_map[exposed] = MCPToolBinding(
                    exposed_name=exposed,
                    server_name=server.name,
                    tool_name=original,
                    schema=schema,
                )
                self.schemas.append(schema)
                taken.add(exposed)

    # ---------- 调用 ----------

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._tool_map

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._tool_map)

    async def call_tool(
        self,
        exposed_name: str,
        raw_arguments: str | dict[str, Any] | None,
    ) -> ToolExecution:
        """解析参数、路由到对应服务，并把所有异常收敛成 ToolExecution。"""
        start = time.perf_counter()
        parsed, parse_error = self._parse_arguments(raw_arguments)
        if parse_error is not None:
            return ToolExecution(
                exposed_name,
                parsed,
                json.dumps(
                    {
                        "ok": False,
                        "server": None,
                        "tool": exposed_name,
                        "error": {
                            "type": "invalid_arguments",
                            "message": parse_error,
                        },
                    },
                    ensure_ascii=False,
                ),
                False,
                time.perf_counter() - start,
            )

        binding = self._tool_map.get(exposed_name)
        if binding is None:
            return ToolExecution(
                exposed_name,
                parsed,
                json.dumps(
                    _error_payload(
                        "",
                        exposed_name,
                        "mcp_tool_not_found",
                        f"未找到 MCP 工具：{exposed_name}",
                    ),
                    ensure_ascii=False,
                ),
                False,
                time.perf_counter() - start,
            )

        server = self.servers.get(binding.server_name)
        if server is None:
            payload = _error_payload(
                binding.server_name,
                binding.tool_name,
                "mcp_server_not_found",
                f"未找到 MCP 服务实例：{binding.server_name}",
            )
            ok = False
        else:
            try:
                ok, payload = await server.call_tool(
                    binding.tool_name, parsed
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ok = False
                payload = _error_payload(
                    binding.server_name,
                    binding.tool_name,
                    "mcp_call_failed",
                    f"调用 MCP 工具异常：{_describe_exception(exc)}",
                )

        return ToolExecution(
            exposed_name,
            parsed,
            json.dumps(payload, ensure_ascii=False, default=str),
            ok,
            time.perf_counter() - start,
        )

    @staticmethod
    def _parse_arguments(
        raw_arguments: str | dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None]:
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

    # ---------- 状态与关闭 ----------

    def status_lines(self) -> list[str]:
        if not self.servers:
            return [f"未加载 MCP 服务（配置：{self.config_path}）"]
        lines = [f"配置文件：{self.config_path}"]
        lines.extend(server.status_line() for server in self.servers.values())
        lines.append(f"已注册 MCP 工具：{len(self._tool_map)} 个")
        return lines

    async def aclose(self) -> None:
        if not self.servers:
            return
        await asyncio.gather(
            *(server.shutdown() for server in self.servers.values()),
            return_exceptions=True,
        )
