"""联网工具：fetchUrl（curl + trafilatura）与 searchWeb（百度千帆 AI 搜索）。

对外提供：
    fetch_url(url, timeout=20)          抓取网页并转 Markdown
    search_web(query, top_k=10)         百度千帆 AI 搜索
    WEB_TOOL_SCHEMAS                    两个工具的 function-calling 声明
    WebTools                            可指定 config.ini 路径的实例版本

约束：
- curl 通过 subprocess 列表参数调用，禁止 shell=True；
- 网页正文由 trafilatura 提取为 Markdown，自动丢弃导航、广告等模板内容；
- 搜索 API Key 只从 config.ini 的 [search] 读取，不在源码里硬编码；
- 超时、404、访问失败、解析失败、配置缺失、网络与 API 错误
  都收敛成结构化 JSON 字符串，交给 LLM 读取并自行纠正。

本模块不 import 任何界面库，可被 CLI / TUI / Web 复用。
"""

from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from ..config import DEFAULT_CONFIG_PATH
from .registry import ToolError

try:  # 延迟到调用时再报错；缺少依赖时原有文件工具仍可正常使用
    import requests
except ImportError:  # pragma: no cover - 取决于运行环境
    requests = None  # type: ignore[assignment]

try:  # pragma: no cover
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_SEARCH_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
DEFAULT_FETCH_TIMEOUT = 20.0
DEFAULT_SEARCH_TIMEOUT = 15.0
DEFAULT_TOP_K = 10
MAX_TOP_K = 20
MAX_HTML_BYTES = 5 * 1024 * 1024  # curl 下载上限 5 MiB
MAX_CONTENT_CHARS = 20_000  # 回灌给模型的正文上限
MAX_SNIPPET_CHARS = 500
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 xAgent/1.0"
)

# config.example.ini 里的占位符，命中时按“未配置”处理
_KEY_PLACEHOLDERS = {
    "",
    "your-baidu-qianfan-api-key",
    "your-api-key",
    "your-key-here",
    "changeme",
    "sk-xxx",
}

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    br"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.IGNORECASE
)
_STATUS_RE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})", re.MULTILINE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------------------
# 工具 schema（OpenAI function-calling 格式，直接追加到 Agent 的 tools 列表）
# ---------------------------------------------------------------------------

FETCH_URL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "fetchUrl",
        "description": (
            "抓取指定网页并提取正文。使用 curl 下载页面，trafilatura 过滤导航与广告后"
            "输出 Markdown；适合阅读文章、文档、新闻详情页。仅支持 http/https。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的完整网址，必须以 http:// 或 https:// 开头",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时秒数，默认 20，范围 1-120",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 120,
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}

SEARCH_WEB_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "searchWeb",
        "description": (
            "使用百度千帆 AI 搜索联网检索，返回标题、摘要和 url 列表。"
            "当需要最新信息、多个来源或不知道具体网址时先调用它。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或完整问题",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量，默认 10，范围 1-20",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

WEB_TOOL_SCHEMAS: list[dict[str, object]] = [FETCH_URL_SCHEMA, SEARCH_WEB_SCHEMA]


# ---------------------------------------------------------------------------
# 结构化错误 / 结果辅助
# ---------------------------------------------------------------------------


def _fail(error_type: str, message: str, **details: object) -> None:
    """抛出结构化错误。

    Agent 分发层会把 ToolError 文本原样回灌给模型，因此这里抛出的是
    带 error.type / error.message 的 JSON，而不是纯文本。
    """
    error: dict[str, object] = {"type": error_type, "message": message}
    error.update(details)
    payload = {"ok": False, "error": error}
    raise ToolError(json.dumps(payload, ensure_ascii=False))


def _ok(**payload: object) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _clamp_float(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _validate_http_url(raw: object, *, field: str = "url") -> str:
    if not isinstance(raw, str) or not raw.strip():
        _fail("invalid_url", f"{field} 不能为空。")
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        _fail("invalid_url", f"仅支持 http/https 链接：{url}")
    if not parsed.netloc:
        _fail("invalid_url", f"链接缺少主机名：{url}")
    return url


def _decode_html(raw: bytes, headers_text: str) -> str:
    """按响应头 / meta 声明解码，最后回退 utf-8、gb18030。"""
    candidates: list[str] = []
    match = _CHARSET_RE.search(headers_text or "")
    if match:
        candidates.append(match.group(1))
    meta = _META_CHARSET_RE.search(raw[:8192])
    if meta:
        candidates.append(meta.group(1).decode("ascii", errors="ignore"))
    candidates.extend(("utf-8", "gb18030"))

    seen: set[str] = set()
    for name in candidates:
        encoding = name.strip().lower()
        if not encoding or encoding in seen:
            continue
        seen.add(encoding)
        if encoding in {"gb2312", "gbk"}:
            encoding = "gb18030"
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _last_http_status(headers_text: str) -> int | None:
    matches = _STATUS_RE.findall(headers_text or "")
    return int(matches[-1]) if matches else None


def _html_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return re.sub(r"\s+", " ", unescape(match.group(1))).strip()


def _first_text(item: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _api_error_message(response: object) -> str:
    """从失败的 HTTP 响应里尽量取出可读错误信息。"""
    try:
        data = response.json()  # type: ignore[attr-defined]
    except ValueError:
        text = (getattr(response, "text", "") or "").strip()
        return text[:300] or "无错误详情"
    if isinstance(data, dict):
        message = _first_text(data, "error_msg", "message", "msg")
        if message:
            return message
    return json.dumps(data, ensure_ascii=False)[:300]


def _raise_for_api_error(data: object, status_code: int) -> None:
    if not isinstance(data, dict):
        _fail("api_error", "搜索接口响应不是 JSON 对象。", status_code=status_code)
    code = data.get("error_code", data.get("code"))
    if code in (None, 0, "0", 200, "200"):
        return
    message = _first_text(data, "error_msg", "message", "msg") or "未知错误"
    _fail(
        "api_error",
        f"搜索接口返回错误 [{code}]：{message}",
        status_code=status_code,
        api_code=code,
    )


def _extract_items(data: object) -> list[dict[str, object]]:
    """兼容 references / results / data.references 等常见返回结构。"""
    if not isinstance(data, dict):
        _fail("api_error", "搜索接口响应不是 JSON 对象。")

    candidates = data.get("references") or data.get("results") or data.get("items")
    if candidates is None:
        nested = data.get("data")
        if isinstance(nested, dict):
            candidates = (
                nested.get("references")
                or nested.get("results")
                or nested.get("items")
            )
        elif isinstance(nested, list):
            candidates = nested

    if candidates is None:
        _fail("api_error", "搜索接口响应中没有结果列表（references/results）。")
    if not isinstance(candidates, list):
        _fail("api_error", "搜索接口的结果列表格式异常。")
    return [item for item in candidates if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """从 config.ini 的 [search] 段读取的搜索配置。"""

    api_key: str
    endpoint: str = DEFAULT_SEARCH_ENDPOINT
    timeout: float = DEFAULT_SEARCH_TIMEOUT
    top_k: int = DEFAULT_TOP_K


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


class WebTools:
    """联网工具集合：绑定一个 config.ini 路径。"""

    def __init__(self, config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        self._config_path = Path(config_path).expanduser()

    # ---------- 配置读取 ----------

    def _load_search_config(self) -> SearchConfig:
        path = self._config_path
        if not path.is_file():
            _fail("config_missing", f"未找到配置文件：{path}")

        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError) as exc:
            _fail("config_invalid", f"读取配置文件失败：{exc}")

        if not parser.has_section("search"):
            _fail("config_missing", f"{path.name} 缺少 [search] 配置段。")

        api_key = parser.get("search", "baidu_qianfan_api_key", fallback="").strip()
        if api_key.lower() in _KEY_PLACEHOLDERS:
            _fail(
                "config_missing",
                f"请在 {path.name} 的 [search] baidu_qianfan_api_key 填写百度千帆 API Key。",
            )

        endpoint = (
            parser.get("search", "endpoint", fallback="").strip()
            or DEFAULT_SEARCH_ENDPOINT
        )
        endpoint = _validate_http_url(endpoint, field="endpoint")

        return SearchConfig(
            api_key=api_key,
            endpoint=endpoint,
            timeout=_clamp_float(
                parser.get("search", "timeout", fallback=None),
                DEFAULT_SEARCH_TIMEOUT,
                1.0,
                120.0,
            ),
            top_k=_clamp_int(
                parser.get("search", "top_k", fallback=None),
                DEFAULT_TOP_K,
                1,
                MAX_TOP_K,
            ),
        )

    # ---------- 1. fetchUrl ----------

    def fetch_url(self, url: str, timeout: float = DEFAULT_FETCH_TIMEOUT) -> str:
        """用 curl 下载网页，再用 trafilatura 提取正文 Markdown。

        失败时抛 ToolError，内容是 {"ok": false, "error": {...}} 的 JSON。
        """
        if trafilatura is None:
            _fail(
                "missing_dependency",
                "缺少 trafilatura，请执行 pip install -r requirements.txt。",
            )

        target = _validate_http_url(url)
        timeout_s = _clamp_float(timeout, DEFAULT_FETCH_TIMEOUT, 1.0, 120.0)

        curl = shutil.which("curl")
        if not curl:
            _fail("curl_not_found", "系统中未找到 curl 命令，请先安装 curl。")

        with tempfile.TemporaryDirectory(prefix="xagent_fetch_") as tmp:
            headers_path = Path(tmp) / "headers.txt"
            # 列表传参 + "--" 结束选项，杜绝 URL 被解释成 curl 参数；
            # 不使用 shell=True，也不做任何字符串拼接。
            args = [
                curl,
                "--silent",
                "--show-error",
                "--location",
                "--compressed",
                "--proto",
                "=http,https",
                "--proto-redir",
                "=http,https",
                "--connect-timeout",
                f"{min(10.0, timeout_s):g}",
                "--max-time",
                f"{timeout_s:g}",
                "--max-filesize",
                str(MAX_HTML_BYTES),
                "--user-agent",
                USER_AGENT,
                "--dump-header",
                str(headers_path),
                "--output",
                "-",
                "--",
                target,
            ]
            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    timeout=timeout_s + 5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                _fail("timeout", f"访问网页超时（>{timeout_s:g} 秒）：{target}")
            except OSError as exc:
                _fail("curl_error", f"无法执行 curl：{exc}")

            headers_text = ""
            try:
                if headers_path.is_file():
                    headers_text = headers_path.read_text(
                        encoding="iso-8859-1", errors="replace"
                    )
            except OSError:
                headers_text = ""

        # ---------- curl 失败分类 ----------
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            mapping = {
                6: ("dns_error", "域名解析失败"),
                7: ("connect_error", "无法连接到服务器"),
                28: ("timeout", f"访问网页超时（>{timeout_s:g} 秒）"),
                63: ("response_too_large", f"响应体超过 {MAX_HTML_BYTES} 字节上限"),
            }
            error_type, message = mapping.get(
                proc.returncode, ("fetch_failed", f"curl 退出码 {proc.returncode}")
            )
            detail = f"：{stderr}" if stderr else ""
            _fail(
                error_type,
                f"{message}{detail}",
                url=target,
                exit_code=proc.returncode,
            )

        # ---------- HTTP 状态（含 404） ----------
        status = _last_http_status(headers_text)
        if status is not None and status >= 400:
            _fail(
                "http_error",
                f"服务器返回 HTTP {status}：{target}",
                url=target,
                status_code=status,
            )

        raw = proc.stdout or b""
        if not raw:
            _fail("empty_response", f"服务器返回空响应：{target}", url=target)
        if len(raw) > MAX_HTML_BYTES:
            _fail(
                "response_too_large",
                f"响应体超过 {MAX_HTML_BYTES} 字节上限：{target}",
                url=target,
            )

        html = _decode_html(raw, headers_text)
        if not html.strip():
            _fail("empty_response", f"响应内容为空：{target}", url=target)

        # ---------- trafilatura 正文提取 ----------
        try:
            metadata = trafilatura.extract_metadata(html)
            content = trafilatura.extract(
                html,
                output_format="markdown",
                include_links=True,
                include_images=False,
                include_tables=True,
                include_comments=False,
                favor_precision=True,  # 偏向精确率，尽量滤掉导航与广告残留
                # deduplicate=True 使用跨调用的全局去重状态，同一进程内
                # 第二次抓取同一页面会返回 None，因此按单页处理关闭它。
                deduplicate=False,
            )
        except Exception as exc:  # 解析库内部异常统一收敛
            _fail("parse_failed", f"网页正文解析失败：{exc}", url=target)

        if not content or not content.strip():
            _fail(
                "parse_failed",
                "未能从页面提取到正文（可能是纯脚本渲染、登录页或反爬页面）。",
                url=target,
            )

        content = content.strip()
        title = ""
        if metadata is not None and getattr(metadata, "title", None):
            title = str(metadata.title).strip()
        if not title:
            title = _html_title(html) or target

        truncated = len(content) > MAX_CONTENT_CHARS
        if truncated:
            content = content[:MAX_CONTENT_CHARS]

        return _ok(
            url=target,
            title=title,
            content=content,
            content_chars=len(content),
            truncated=truncated,
        )

    # ---------- 2. searchWeb ----------

    def search_web(self, query: str, top_k: int | None = None) -> str:
        """调用百度千帆 AI 搜索，返回标题 / 摘要 / url 列表。"""
        if requests is None:
            _fail(
                "missing_dependency",
                "缺少 requests，请执行 pip install -r requirements.txt。",
            )

        if not isinstance(query, str):
            _fail("invalid_query", "搜索关键词必须是字符串。")
        keyword = query.strip()
        if not keyword:
            _fail("invalid_query", "搜索关键词不能为空。")

        config = self._load_search_config()
        limit = _clamp_int(
            config.top_k if top_k is None else top_k,
            config.top_k,
            1,
            MAX_TOP_K,
        )

        payload = {
            "messages": [{"role": "user", "content": keyword}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": limit}],
        }
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = requests.post(
                config.endpoint,
                headers=headers,
                json=payload,
                timeout=(5.0, config.timeout),
            )
        except requests.Timeout:
            _fail("timeout", f"搜索请求超时（>{config.timeout:g} 秒）。")
        except requests.ConnectionError as exc:
            _fail("network_error", f"无法连接搜索服务：{exc}")
        except requests.RequestException as exc:
            _fail("network_error", f"搜索请求失败：{exc}")

        if response.status_code >= 400:
            _fail(
                "api_error",
                f"搜索接口 HTTP {response.status_code}：{_api_error_message(response)}",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError:
            _fail(
                "api_error",
                f"搜索接口返回非 JSON 内容（HTTP {response.status_code}）。",
                status_code=response.status_code,
            )

        _raise_for_api_error(data, response.status_code)
        items = _extract_items(data)

        results = []
        for item in items[:limit]:
            results.append(
                {
                    "title": _first_text(item, "title", "name") or "（无标题）",
                    "snippet": _first_text(
                        item, "content", "snippet", "summary", "abstract"
                    )[:MAX_SNIPPET_CHARS],
                    "url": _first_text(item, "url", "link"),
                }
            )

        return _ok(query=keyword, count=len(results), results=results)


# ---------------------------------------------------------------------------
# 模块级默认实例：供 agent.py 直接 import 调用
# ---------------------------------------------------------------------------

_default_tools: WebTools | None = None


def _get_default_tools() -> WebTools:
    global _default_tools
    if _default_tools is None:
        _default_tools = WebTools()
    return _default_tools


def fetch_url(url: str, timeout: float = DEFAULT_FETCH_TIMEOUT) -> str:
    """fetchUrl 工具入口（使用项目根目录的 config.ini）。"""
    return _get_default_tools().fetch_url(url, timeout)


def search_web(query: str, top_k: int | None = None) -> str:
    """searchWeb 工具入口（使用项目根目录的 config.ini）。"""
    return _get_default_tools().search_web(query, top_k)


__all__ = [
    "FETCH_URL_SCHEMA",
    "SEARCH_WEB_SCHEMA",
    "WEB_TOOL_SCHEMAS",
    "SearchConfig",
    "WebTools",
    "fetch_url",
    "search_web",
]
