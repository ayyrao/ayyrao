import os
import json
import time
import configparser
import re
import shutil
import subprocess
import tempfile
from html import unescape
from urllib.parse import urlparse

from openai import OpenAI

try:
    import requests
except ImportError:
    requests = None

try:
    import trafilatura
except ImportError:
    trafilatura = None

# 项目根目录，防止越界访问外部文件夹
BASE_DIR = os.path.abspath(".")

# 路径安全校验：只能在当前项目文件夹内操作
def safe_path(path):
    full_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not full_path.startswith(BASE_DIR):
        return None, "禁止访问项目以外的路径！"
    return full_path, ""

# ---------------------- 10个文件工具函数 ----------------------
# 1. 获取当前工作目录
def get_cwd():
    return os.getcwd()

# 2. 列出目录内容
def list_dir(path="."):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    try:
        return str(os.listdir(full_path))
    except Exception as e:
        return str(e)

# 3. 获取文件类型（后缀名）
def get_file_type(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    ext = os.path.splitext(full_path)[1]
    return f"文件后缀：{ext}"

# 4. 读取纯文本文件，二进制文件拒绝读取
def read_text_file(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isfile(full_path):
        return "文件不存在"
    # 只允许文本格式
    suffix = os.path.splitext(full_path)[1]
    binary_suffix = [".jpg", ".png", ".gif", ".exe", ".zip", ".rar"]
    if suffix in binary_suffix:
        return "非文本文件，禁止读取"
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return str(e)

# 5. 创建文件，自动创建父文件夹
def create_file(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    parent_dir = os.path.dirname(full_path)
    os.makedirs(parent_dir, exist_ok=True)
    try:
        with open(full_path, "w", encoding="utf-8") as f:
            pass
        return "文件创建成功"
    except Exception as e:
        return str(e)

# 6. 编辑文本文件内容
def edit_text_file(path, content):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isfile(full_path):
        return "文件不存在"
    try:
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return "内容修改成功"
    except Exception as e:
        return str(e)

# 7. 创建文件夹
def make_dir(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    os.makedirs(full_path, exist_ok=True)
    return "目录创建成功"

# 8. 删除空目录，非空目录不让删
def remove_dir(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isdir(full_path):
        return "目录不存在"
    if len(os.listdir(full_path)) > 0:
        return "目录不为空，禁止删除"
    os.rmdir(full_path)
    return "空目录删除成功"

# 9. 删除文件
def remove_file(path):
    full_path, msg = safe_path(path)
    if not full_path:
        return msg
    if not os.path.isfile(full_path):
        return "文件不存在"
    os.remove(full_path)
    return "文件删除成功"

# 10. 重命名文件/文件夹（加异常捕获）
def rename_item(old_path, new_path):
    old_full, msg = safe_path(old_path)
    new_full, msg2 = safe_path(new_path)
    if not old_full or not new_full:
        return "路径非法"
    try:
        os.rename(old_full, new_full)
        return "重命名完成"
    except Exception as e:
        return f"重命名失败：{str(e)}"

# ---------------------- 联网工具：fetchUrl / searchWeb ----------------------
# 配置文件固定为 chat.py 同目录的 config.ini
WEB_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
WEB_SEARCH_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
WEB_FETCH_TIMEOUT = 20
WEB_SEARCH_TIMEOUT = 15
WEB_MAX_TOP_K = 20
WEB_MAX_HTML_BYTES = 5 * 1024 * 1024        # curl 下载上限 5 MiB
WEB_MAX_CONTENT_CHARS = 20000               # 回灌给模型的正文上限
WEB_MAX_SUMMARY_CHARS = 500
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 chatAgent/1.0"
)

# config.ini 中常见的占位符，命中时按“未配置”处理
WEB_KEY_PLACEHOLDERS = {
    "",
    "your-baidu-qianfan-api-key",
    "your-api-key",
    "your-key-here",
    "changeme",
    "sk-xxx",
}

_WEB_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_WEB_META_CHARSET_RE = re.compile(
    br"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.IGNORECASE
)
_WEB_STATUS_RE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})", re.MULTILINE)
_WEB_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class WebToolError(Exception):
    """联网工具的可预期失败，最终会转成结构化 JSON 返回给 LLM。"""

    def __init__(self, error_type, message, **details):
        super().__init__(message)
        self.error_type = error_type
        self.details = details


def _web_fail(error_type, message, **details):
    """内部抛出可预期错误，由 fetch_url / search_web 统一转 JSON。"""
    raise WebToolError(error_type, message, **details)


def _web_error_json(error_type, message, **details):
    """结构化错误：LLM 可以读取 error.type / error.message 后自行纠正。"""
    error = {"type": error_type, "message": message}
    error.update(details)
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


def _web_ok(**payload):
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _web_clamp_float(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _web_clamp_int(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _web_validate_url(raw, field="url"):
    """只允许 http/https，防止 file:// 等协议被 curl 读取。"""
    if not isinstance(raw, str) or not raw.strip():
        _web_fail("invalid_url", f"{field} 不能为空。")
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        _web_fail("invalid_url", f"仅支持 http/https 链接：{url}")
    if not parsed.netloc:
        _web_fail("invalid_url", f"链接缺少主机名：{url}")
    return url


def _web_decode_html(raw, headers_text):
    """按响应头 / meta 声明解码，最后回退 utf-8、gb18030。"""
    candidates = []
    match = _WEB_CHARSET_RE.search(headers_text or "")
    if match:
        candidates.append(match.group(1))
    meta = _WEB_META_CHARSET_RE.search(raw[:8192])
    if meta:
        candidates.append(meta.group(1).decode("ascii", errors="ignore"))
    candidates.extend(("utf-8", "gb18030"))

    seen = set()
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


def _web_last_http_status(headers_text):
    matches = _WEB_STATUS_RE.findall(headers_text or "")
    return int(matches[-1]) if matches else None


def _web_html_title(html):
    match = _WEB_TITLE_RE.search(html)
    if not match:
        return ""
    return re.sub(r"\s+", " ", unescape(match.group(1))).strip()


def _web_first_text(item, *keys):
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _web_api_error_message(response):
    """从失败的 HTTP 响应中尽量取出可读错误信息。"""
    try:
        data = response.json()
    except ValueError:
        text = (getattr(response, "text", "") or "").strip()
        return text[:300] or "无错误详情"
    if isinstance(data, dict):
        message = _web_first_text(data, "error_msg", "message", "msg")
        if message:
            return message
    return json.dumps(data, ensure_ascii=False)[:300]


def _web_check_api_error(data, status_code):
    """检查千帆返回的业务错误码（HTTP 200 也可能带 code != 0）。"""
    if not isinstance(data, dict):
        _web_fail("api_error", "搜索接口响应不是 JSON 对象。", status_code=status_code)
    code = data.get("error_code", data.get("code"))
    if code in (None, 0, "0", 200, "200"):
        return
    message = _web_first_text(data, "error_msg", "message", "msg") or "未知错误"
    _web_fail(
        "api_error",
        f"搜索接口返回错误 [{code}]：{message}",
        status_code=status_code,
        api_code=code,
    )


def _web_extract_items(data):
    """兼容 references / results / data.references 等常见返回结构。"""
    if not isinstance(data, dict):
        _web_fail("api_error", "搜索接口响应不是 JSON 对象。")

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
        _web_fail("api_error", "搜索接口响应中没有结果列表（references/results）。")
    if not isinstance(candidates, list):
        _web_fail("api_error", "搜索接口的结果列表格式异常。")
    return [item for item in candidates if isinstance(item, dict)]


def _web_load_search_config():
    """只从 config.ini 的 [search] 段读取配置，密钥不硬编码。"""
    if not os.path.isfile(WEB_CONFIG_PATH):
        _web_fail("config_missing", f"未找到配置文件：{WEB_CONFIG_PATH}")

    parser = configparser.ConfigParser()
    try:
        parser.read(WEB_CONFIG_PATH, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError) as exc:
        _web_fail("config_invalid", f"读取配置文件失败：{exc}")

    if not parser.has_section("search"):
        _web_fail("config_missing", "config.ini 缺少 [search] 配置段。")

    api_key = parser.get("search", "baidu_qianfan_api_key", fallback="").strip()
    if api_key.lower() in WEB_KEY_PLACEHOLDERS:
        _web_fail(
            "config_missing",
            "请在 config.ini 的 [search] baidu_qianfan_api_key 填写百度千帆 API Key。",
        )

    endpoint = (
        parser.get("search", "endpoint", fallback="").strip()
        or WEB_SEARCH_ENDPOINT
    )
    endpoint = _web_validate_url(endpoint, field="endpoint")

    return {
        "api_key": api_key,
        "endpoint": endpoint,
        "timeout": _web_clamp_float(
            parser.get("search", "timeout", fallback=None),
            WEB_SEARCH_TIMEOUT,
            1.0,
            120.0,
        ),
        "top_k": _web_clamp_int(
            parser.get("search", "top_k", fallback=None),
            10,
            1,
            WEB_MAX_TOP_K,
        ),
    }


# 1. fetchUrl：subprocess 调用 curl + trafilatura 提取正文
def fetch_url(url, timeout=WEB_FETCH_TIMEOUT):
    """抓取网页并提取正文 Markdown。

    成功返回 {"ok": true, ...}；任何失败都返回 {"ok": false, "error": {...}}，
    不会向主循环抛异常。
    """
    try:
        if trafilatura is None:
            _web_fail(
                "missing_dependency",
                "缺少 trafilatura，请执行 pip install trafilatura。",
            )

        target = _web_validate_url(url)
        timeout_s = _web_clamp_float(timeout, WEB_FETCH_TIMEOUT, 1.0, 120.0)

        curl = shutil.which("curl")
        if not curl:
            _web_fail("curl_not_found", "系统中未找到 curl 命令，请先安装 curl。")

        with tempfile.TemporaryDirectory(prefix="chat_fetch_") as tmp:
            headers_path = os.path.join(tmp, "headers.txt")
            # 列表传参 + "--" 结束选项，URL 不会被解释成 curl 参数；
            # 不使用 shell=True，也不做任何命令字符串拼接。
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
                str(WEB_MAX_HTML_BYTES),
                "--user-agent",
                WEB_USER_AGENT,
                "--dump-header",
                headers_path,
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
                    shell=False,  # 明确禁止 shell，避免命令注入
                )
            except subprocess.TimeoutExpired:
                _web_fail("timeout", f"访问网页超时（>{timeout_s:g} 秒）：{target}")
            except OSError as exc:
                _web_fail("curl_error", f"无法执行 curl：{exc}")

            headers_text = ""
            try:
                if os.path.isfile(headers_path):
                    with open(
                        headers_path, "r", encoding="iso-8859-1", errors="replace"
                    ) as f:
                        headers_text = f.read()
            except OSError:
                headers_text = ""

        # ---------- curl 退出码分类 ----------
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            mapping = {
                6: ("dns_error", "域名解析失败"),
                7: ("connect_error", "无法连接到服务器"),
                28: ("timeout", f"访问网页超时（>{timeout_s:g} 秒）"),
                63: ("response_too_large", f"响应体超过 {WEB_MAX_HTML_BYTES} 字节上限"),
            }
            error_type, message = mapping.get(
                proc.returncode, ("fetch_failed", f"curl 退出码 {proc.returncode}")
            )
            detail = f"：{stderr}" if stderr else ""
            _web_fail(
                error_type,
                f"{message}{detail}",
                url=target,
                exit_code=proc.returncode,
            )

        # ---------- HTTP 状态（404 等） ----------
        status = _web_last_http_status(headers_text)
        if status is not None and status >= 400:
            _web_fail(
                "http_error",
                f"服务器返回 HTTP {status}：{target}",
                url=target,
                status_code=status,
            )

        raw = proc.stdout or b""
        if not raw:
            _web_fail("empty_response", f"服务器返回空响应：{target}", url=target)
        if len(raw) > WEB_MAX_HTML_BYTES:
            _web_fail(
                "response_too_large",
                f"响应体超过 {WEB_MAX_HTML_BYTES} 字节上限：{target}",
                url=target,
            )

        html = _web_decode_html(raw, headers_text)
        if not html.strip():
            _web_fail("empty_response", f"响应内容为空：{target}", url=target)

        # ---------- trafilatura 提取正文（自动过滤导航 / 广告） ----------
        try:
            metadata = trafilatura.extract_metadata(html)
            content = trafilatura.extract(
                html,
                output_format="markdown",
                include_links=True,
                include_images=False,
                include_tables=True,
                include_comments=False,
                favor_precision=True,  # 偏向精确率，减少导航/广告残留
                # 注意：deduplicate=True 使用跨调用的全局去重状态，
                # 同一进程内第二次抓取同一页面会返回 None，这里关闭。
                deduplicate=False,
            )
        except Exception as exc:
            _web_fail("parse_failed", f"网页正文解析失败：{exc}", url=target)

        if not content or not content.strip():
            _web_fail(
                "parse_failed",
                "未能从页面提取到正文（可能是纯脚本渲染、登录页或反爬页面）。",
                url=target,
            )

        content = content.strip()
        title = ""
        if metadata is not None and getattr(metadata, "title", None):
            title = str(metadata.title).strip()
        if not title:
            title = _web_html_title(html) or target

        truncated = len(content) > WEB_MAX_CONTENT_CHARS
        if truncated:
            content = content[:WEB_MAX_CONTENT_CHARS]

        return _web_ok(
            url=target,
            title=title,
            content=content,
            content_chars=len(content),
            truncated=truncated,
        )
    except WebToolError as exc:
        return _web_error_json(exc.error_type, str(exc), **exc.details)
    except Exception as exc:  # 兜底：任何意外都转结构化错误，主循环不崩溃
        return _web_error_json("unexpected_error", f"fetchUrl 执行异常：{exc}")


# 2. searchWeb：读取 config.ini [search]，调用百度千帆 AI 搜索
def search_web(query, top_k=10):
    """联网搜索，返回 title / summary / url 列表。

    成功返回 {"ok": true, "results": [...]}；任何失败都返回结构化 JSON。
    """
    try:
        if requests is None:
            _web_fail(
                "missing_dependency",
                "缺少 requests，请执行 pip install requests。",
            )

        if not isinstance(query, str):
            _web_fail("invalid_query", "搜索关键词必须是字符串。")
        keyword = query.strip()
        if not keyword:
            _web_fail("invalid_query", "搜索关键词不能为空。")

        config = _web_load_search_config()
        limit = _web_clamp_int(
            config["top_k"] if top_k is None else top_k,
            config["top_k"],
            1,
            WEB_MAX_TOP_K,
        )

        payload = {
            "messages": [{"role": "user", "content": keyword}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": limit}],
        }
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = requests.post(
                config["endpoint"],
                headers=headers,
                json=payload,
                timeout=(5.0, config["timeout"]),
            )
        except requests.Timeout:
            _web_fail("timeout", f"搜索请求超时（>{config['timeout']:g} 秒）。")
        except requests.ConnectionError as exc:
            _web_fail("network_error", f"无法连接搜索服务：{exc}")
        except requests.RequestException as exc:
            _web_fail("network_error", f"搜索请求失败：{exc}")

        if response.status_code >= 400:
            _web_fail(
                "api_error",
                f"搜索接口 HTTP {response.status_code}："
                f"{_web_api_error_message(response)}",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError:
            _web_fail(
                "api_error",
                f"搜索接口返回非 JSON 内容（HTTP {response.status_code}）。",
                status_code=response.status_code,
            )

        _web_check_api_error(data, response.status_code)
        items = _web_extract_items(data)

        results = []
        for item in items[:limit]:
            results.append(
                {
                    "title": _web_first_text(item, "title", "name") or "（无标题）",
                    "summary": _web_first_text(
                        item, "content", "snippet", "summary", "abstract"
                    )[:WEB_MAX_SUMMARY_CHARS],
                    "url": _web_first_text(item, "url", "link"),
                }
            )

        return _web_ok(query=keyword, count=len(results), results=results)
    except WebToolError as exc:
        return _web_error_json(exc.error_type, str(exc), **exc.details)
    except Exception as exc:  # 兜底：任何意外都转结构化错误，主循环不崩溃
        return _web_error_json("unexpected_error", f"searchWeb 执行异常：{exc}")

# ---------------------- 工具定义（function call格式） ----------------------
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_cwd",
            "description": "获取当前工作目录",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出指定目录下所有文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_type",
            "description": "获取指定文件的文件类型",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "读取纯文本文件，二进制文件拒绝读取",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "新建文件，自动创建父目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_text_file",
            "description": "编辑纯文本文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "make_dir",
            "description": "创建新目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_dir",
            "description": "只删除空目录，非空目录禁止删除",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_file",
            "description": "删除指定文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_item",
            "description": "重命名文件或者目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {"type": "string"},
                    "new_path": {"type": "string"}
                },
                "required": ["old_path", "new_path"]
            }
        }
    }
]

# 追加两个联网工具的 schema，与原有 10 个文件工具共存
tools.extend([
    {
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
                        "description": "要抓取的完整网址，必须以 http:// 或 https:// 开头"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "超时秒数，默认 20，范围 1-120",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 120
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
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
                        "description": "搜索关键词或完整问题"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量，默认 10，范围 1-20",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 20
                    }
                },
                "required": ["query"]
            }
        }
    }
])

# 工具分发调用
def run_tool(name, arguments):
    args = json.loads(arguments)
    if name == "get_cwd":
        return get_cwd()
    elif name == "list_dir":
        return list_dir(args.get("path", "."))
    elif name == "get_file_type":
        return get_file_type(args["path"])
    elif name == "read_text_file":
        return read_text_file(args["path"])
    elif name == "create_file":
        return create_file(args["path"])
    elif name == "edit_text_file":
        return edit_text_file(args["path"], args["content"])
    elif name == "make_dir":
        return make_dir(args["path"])
    elif name == "remove_dir":
        return remove_dir(args["path"])
    elif name == "remove_file":
        return remove_file(args["path"])
    elif name == "rename_item":
        return rename_item(args["old_path"], args["new_path"])
    elif name == "fetchUrl":
        # 工具名 fetchUrl -> 联网抓取函数 fetch_url
        return fetch_url(args.get("url", ""), args.get("timeout", WEB_FETCH_TIMEOUT))
    elif name == "searchWeb":
        # 工具名 searchWeb -> 联网搜索函数 search_web
        return search_web(args.get("query", ""), args.get("top_k", 10))
    else:
        return "工具不存在"

# 主Agent对话逻辑
if __name__ == "__main__":
    client = OpenAI(
        api_key="sk-d27d4449de5c47acb39f09bfeecb41f0",
        base_url="https://api.deepseek.com"
    )
    messages = []
    while True:
        user_msg = input("请输入指令：")
        messages.append({"role": "user", "content": user_msg})
        try:
            # 工具调用可能有多轮，循环直到模型给出最终回答
            for _ in range(10):
                resp = client.chat.completions.create(
                    model="deepseek-flash",
                    messages=messages,
                    tools=tools
                )
                time.sleep(0.8) # 限流缓冲
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    print(msg.content)
                    # 把最终回答也存入历史，保证多轮对话上下文完整
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    break
                # 必须先把带 tool_calls 的 assistant 消息放回历史，
                # 否则后续 tool 消息会被接口判定为无对应调用
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments
                            }
                        }
                        for call in msg.tool_calls
                    ]
                })
                for call in msg.tool_calls:
                    res = run_tool(call.function.name, call.function.arguments)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": res
                    })
                time.sleep(0.8)
            else:
                print("工具调用轮数过多，已停止本轮请求")
        except Exception as err:
            print(f"请求出错：{err}")
