#!/usr/bin/env python3
"""xAgent 工具一键验收脚本。

覆盖范围：
    原有工具   10 个文件工具的正常 CRUD 流程与典型错误（越界、重复创建、非空目录等）
    fetchUrl   正常抓取、超时、参数错误、HTTP 404、长正文截断
    searchWeb  正常搜索、超时、参数错误、API 错误、配置缺失（本地 mock 端点）
    RunCli     正常执行、超时、参数错误、异常退出、程序不存在、输出截断、无 shell 注入

用法：
    python acceptance_test.py             # 输出验收报告
    python acceptance_test.py -v          # 显示每个用例的详情与异常堆栈
    python acceptance_test.py --strict    # SKIP 也视为验收失败
    python acceptance_test.py --no-color  # 关闭 ANSI 颜色

说明：
- fetchUrl / searchWeb 使用本地 HTTP mock 服务，不访问外网，不消耗搜索 API 配额；
- 文件工具使用临时沙箱，验收结束后自动删除，不污染项目工作目录；
- RunCli 以当前用户权限执行，只运行脚本内固定的无害命令。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.security import PathSandbox  # noqa: E402
from core.tools import build_default_registry, cli_tools  # noqa: E402
from core.tools.file_schemas import FILE_TOOL_DEFINITIONS, register_file_tools  # noqa: E402
from core.tools.file_tools import FileTools  # noqa: E402
from core.tools.registry import ToolError, ToolRegistry  # noqa: E402
from core.tools import web_tools  # noqa: E402
from core.tools.web_tools import WebTools, fetch_url  # noqa: E402


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SECTION_FILE = "原有工具"
SECTION_FETCH = "fetchUrl"
SECTION_SEARCH = "searchWeb"
SECTION_CLI = "RunCli"
SECTION_ORDER = (SECTION_FILE, SECTION_FETCH, SECTION_SEARCH, SECTION_CLI)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

SLOW_SECONDS = 2.0  # mock 慢接口的响应延迟
FETCH_TIMEOUT = 1.0  # fetchUrl 超时用例的秒数
SEARCH_TIMEOUT = 1.0  # searchWeb 超时用例的秒数
CLI_TIMEOUT = 1.0  # RunCli 超时用例的秒数
MAX_CLI_OUTPUT_CHARS = 20_000  # 与 cli_tools.MAX_OUTPUT_CHARS 保持一致

_ANSI = {
    PASS: "\033[32m",
    FAIL: "\033[31m",
    SKIP: "\033[33m",
    "HEADER": "\033[1;36m",
    "BOLD": "\033[1m",
    "DIM": "\033[2m",
    "RESET": "\033[0m",
}


# ---------------------------------------------------------------------------
# 断言与结果记录
# ---------------------------------------------------------------------------


class CheckFailure(RuntimeError):
    """验收断言失败。"""


class SkipCase(RuntimeError):
    """环境缺失导致用例跳过。"""


def _check(condition: object, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


@dataclass(slots=True)
class CaseResult:
    section: str
    name: str
    status: str
    elapsed: float
    detail: str = ""
    traceback_text: str = ""


@dataclass(slots=True)
class Acceptance:
    strict: bool = False
    verbose: bool = False
    color: bool = True
    results: list[CaseResult] = field(default_factory=list)

    # ---------- 用例执行 ----------

    def run(self, section: str, name: str, func) -> None:
        start = time.perf_counter()
        try:
            detail = func() or ""
        except SkipCase as exc:
            self._record(section, name, SKIP, time.perf_counter() - start, str(exc))
        except CheckFailure as exc:
            self._record(section, name, FAIL, time.perf_counter() - start, str(exc))
        except Exception as exc:  # 兜底：未预期异常也计入失败而不是中断脚本
            detail = f"{type(exc).__name__}: {exc}"
            self._record(
                section,
                name,
                FAIL,
                time.perf_counter() - start,
                detail,
                traceback.format_exc(),
            )
        else:
            self._record(section, name, PASS, time.perf_counter() - start, str(detail))

    def skip(self, section: str, name: str, reason: str) -> None:
        self._record(section, name, SKIP, 0.0, reason)

    def _record(
        self,
        section: str,
        name: str,
        status: str,
        elapsed: float,
        detail: str = "",
        traceback_text: str = "",
    ) -> None:
        result = CaseResult(section, name, status, elapsed, detail, traceback_text)
        self.results.append(result)
        self._print_case(result)

    # ---------- 输出 ----------

    def _color(self, text: str, key: str) -> str:
        if not self.color:
            return text
        return f"{_ANSI.get(key, '')}{text}{_ANSI['RESET']}"

    def _print_case(self, result: CaseResult) -> None:
        tag = self._color(f"[{result.status}]", result.status)
        duration = f"{result.elapsed * 1000:7.1f} ms"
        print(f"{tag} {result.section:<8} :: {result.name}  ({duration})")

        if result.status == FAIL:
            self._print_detail(result.detail)
            if self.verbose and result.traceback_text:
                self._print_detail(result.traceback_text.rstrip())
        elif result.status == SKIP:
            self._print_detail(f"跳过：{result.detail}")
        elif self.verbose and result.detail:
            self._print_detail(result.detail)

    @staticmethod
    def _print_detail(detail: str) -> None:
        for line in str(detail).splitlines():
            print(f"       {line}")

    def is_success(self) -> bool:
        total_fail = sum(r.status == FAIL for r in self.results)
        total_skip = sum(r.status == SKIP for r in self.results)
        return total_fail == 0 and (not self.strict or total_skip == 0)

    def print_summary(self) -> None:
        print()
        print("=" * 68)
        print(self._color("按模块汇总", "HEADER"))
        print(f"{'模块':<12}{'通过':>6}{'失败':>6}{'跳过':>6}{'用时':>10}")

        for section in SECTION_ORDER:
            items = [r for r in self.results if r.section == section]
            passed = sum(r.status == PASS for r in items)
            failed = sum(r.status == FAIL for r in items)
            skipped = sum(r.status == SKIP for r in items)
            elapsed = sum(r.elapsed for r in items)
            print(f"{section:<12}{passed:>6}{failed:>6}{skipped:>6}{elapsed:>9.2f}s")

        total_pass = sum(r.status == PASS for r in self.results)
        total_fail = sum(r.status == FAIL for r in self.results)
        total_skip = sum(r.status == SKIP for r in self.results)
        total_elapsed = sum(r.elapsed for r in self.results)

        print("-" * 68)
        print(
            f"总计：{total_pass} 通过，{total_fail} 失败，{total_skip} 跳过，"
            f"用时 {total_elapsed:.2f}s"
        )
        success = self.is_success()
        if success:
            print(self._color("结论：验收通过", PASS))
        else:
            print(self._color("结论：验收未通过", FAIL))

        print()
        print(f"环境提示：{agent_integration_hint()}")
        try:
            print(f"环境提示：{agent_tools_snapshot()}")
        except Exception as exc:
            print(f"环境提示：无法读取 Agent 工具列表（{type(exc).__name__}: {exc}）")


# ---------------------------------------------------------------------------
# 本地 mock HTTP 服务（fetchUrl / searchWeb）
# ---------------------------------------------------------------------------

_FETCH_OK_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>验收页面</title></head>
<body>
<nav>首页 / 导航 / 广告</nav>
<article>
<h1>验收页面</h1>
<p>这是 xAgent fetchUrl 工具的本地验收页面，用于验证 curl 下载链路是否正常。</p>
<p>正文经过 trafilatura 提取后应当以 Markdown 返回，并丢弃导航和页脚。</p>
</article>
<footer>版权所有 页脚内容</footer>
</body></html>"""

_LARGE_TEXT = "这是用于验证 fetchUrl 输出截断的长文本。" * 2000
_FETCH_LARGE_HTML = (
    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<title>长文页面</title></head><body><article><h1>长文页面</h1>"
    f"<p>{_LARGE_TEXT}</p></article></body></html>"
)
_FETCH_404_HTML = "<html><body><h1>404 Not Found</h1></body></html>"

_SEARCH_OK = {
    "references": [
        {
            "title": "验收结果 A",
            "url": "https://example.com/a",
            "content": "这是本地 mock 返回的摘要 A。",
        },
        {
            "title": "验收结果 B",
            "url": "https://example.com/b",
            "content": "这是本地 mock 返回的摘要 B。",
        },
    ]
}


class _MockHandler(BaseHTTPRequestHandler):
    """为 fetchUrl / searchWeb 提供确定性的本地响应。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:  # 保持终端输出干净
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:  # 客户端超时断开后写回失败，属于预期情况
            pass

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path == "/fetch/ok":
            self._send(200, _FETCH_OK_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/fetch/large":
            self._send(
                200, _FETCH_LARGE_HTML.encode("utf-8"), "text/html; charset=utf-8"
            )
        elif self.path == "/fetch/slow":
            time.sleep(SLOW_SECONDS)
            self._send(200, _FETCH_OK_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/fetch/404":
            self._send(404, _FETCH_404_HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        if self.path == "/search/ok":
            self._send_json(200, _SEARCH_OK)
        elif self.path == "/search/slow":
            time.sleep(SLOW_SECONDS)
            self._send_json(200, _SEARCH_OK)
        elif self.path == "/search/error":
            self._send_json(200, {"error_code": 500, "error_msg": "mock api failure"})
        else:
            self._send_json(404, {"message": "not found"})


@contextlib.contextmanager
def mock_server():
    """启动本地 mock 服务，返回 http://127.0.0.1:<port>。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, name="xagent-acceptance-mock", daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 通用断言辅助
# ---------------------------------------------------------------------------


def _expect_tool_error(func, expected_type: str) -> dict:
    """断言工具抛出 ToolError，且 error.type 符合预期。"""
    try:
        result = func()
    except ToolError as exc:
        try:
            payload = json.loads(str(exc))
        except json.JSONDecodeError:
            raise CheckFailure(f"ToolError 不是结构化 JSON：{exc}") from exc

        _check(payload.get("ok") is False, f"错误结果缺少 ok=false：{payload}")
        error = payload.get("error") or {}
        actual = error.get("type")
        _check(
            actual == expected_type,
            f"期望 error.type={expected_type!r}，实际 {actual!r}：{payload}",
        )
        return payload

    raise CheckFailure(
        f"期望抛出 ToolError({expected_type})，实际成功返回：{str(result)[:300]}"
    )


def _invoke_expect_fail(
    registry: ToolRegistry, name: str, payload: dict, keyword: str
) -> str:
    result = registry.invoke(name, payload)
    _check(not result.ok, f"{name} 期望失败，实际成功：{result.output}")
    _check(
        keyword in result.output,
        f"{name} 错误信息应包含 {keyword!r}，实际：{result.output}",
    )
    return result.output


# ---------------------------------------------------------------------------
# 1. 原有文件工具
# ---------------------------------------------------------------------------


def _file_tools_normal(registry: ToolRegistry) -> str:
    def ok(name: str, payload: dict, label: str) -> str:
        result = registry.invoke(name, payload)
        _check(result.ok, f"{label} 失败：{result.output}")
        return result.output

    ok("get_cwd", {}, "get_cwd")
    ok("make_dir", {"path": "docs"}, "make_dir")
    ok("create_file", {"path": "docs/note.txt"}, "create_file")
    ok(
        "edit_text_file",
        {"path": "docs/note.txt", "content": "hello 验收"},
        "edit_text_file",
    )

    content = ok("read_text_file", {"path": "docs/note.txt"}, "read_text_file")
    _check(content == "hello 验收", f"读回内容不一致：{content!r}")

    kind = ok("get_file_type", {"path": "docs/note.txt"}, "get_file_type")
    _check(".txt" in kind, f"get_file_type 结果异常：{kind}")

    listing = ok("list_dir", {"path": "docs"}, "list_dir")
    _check("note.txt" in listing, f"list_dir 未列出 note.txt：{listing}")

    ok(
        "rename_item",
        {"old_path": "docs/note.txt", "new_path": "docs/renamed.txt"},
        "rename_item",
    )
    ok("remove_file", {"path": "docs/renamed.txt"}, "remove_file")
    ok("remove_dir", {"path": "docs"}, "remove_dir")
    return "10 个文件工具的正常流程全部通过"


def build_file_cases(acc: Acceptance, registry: ToolRegistry, sandbox: PathSandbox) -> None:
    def registry_check() -> str:
        declared = {name for name, _, _ in FILE_TOOL_DEFINITIONS}
        _check(
            len(registry) == 10,
            f"注册表应有 10 个文件工具，实际 {len(registry)} 个：{registry.names}",
        )
        _check(
            set(registry.names) == declared,
            f"注册工具与声明不一致：{set(registry.names) ^ declared}",
        )
        return f"{len(registry)} 个文件工具注册完整"

    acc.run(SECTION_FILE, "工具注册完整性", registry_check)
    acc.run(SECTION_FILE, "10 个工具正常 CRUD 流程", lambda: _file_tools_normal(registry))

    def path_escape() -> str:
        return _invoke_expect_fail(
            registry, "read_text_file", {"path": "../outside.txt"}, "工作目录以外"
        )

    def duplicate_create() -> str:
        first = registry.invoke("create_file", {"path": "dup.txt"})
        _check(first.ok, f"准备用例失败：{first.output}")
        return _invoke_expect_fail(
            registry, "create_file", {"path": "dup.txt"}, "已存在"
        )

    def remove_non_empty_dir() -> str:
        _check(registry.invoke("make_dir", {"path": "nonempty"}).ok, "创建目录失败")
        _check(
            registry.invoke("create_file", {"path": "nonempty/x.txt"}).ok,
            "创建文件失败",
        )
        return _invoke_expect_fail(
            registry, "remove_dir", {"path": "nonempty"}, "不为空"
        )

    def remove_root() -> str:
        return _invoke_expect_fail(registry, "remove_dir", {"path": "."}, "工作目录本身")

    def read_binary() -> str:
        (sandbox.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
        return _invoke_expect_fail(
            registry, "read_text_file", {"path": "image.png"}, "非文本"
        )

    def rename_overwrite() -> str:
        _check(registry.invoke("create_file", {"path": "a.txt"}).ok, "创建 a.txt 失败")
        _check(registry.invoke("create_file", {"path": "b.txt"}).ok, "创建 b.txt 失败")
        return _invoke_expect_fail(
            registry,
            "rename_item",
            {"old_path": "a.txt", "new_path": "b.txt"},
            "禁止覆盖",
        )

    acc.run(SECTION_FILE, "路径越界拦截", path_escape)
    acc.run(SECTION_FILE, "重复创建拦截", duplicate_create)
    acc.run(SECTION_FILE, "删除非空目录拦截", remove_non_empty_dir)
    acc.run(SECTION_FILE, "删除工作目录拦截", remove_root)
    acc.run(SECTION_FILE, "二进制文件拒读", read_binary)
    acc.run(SECTION_FILE, "重命名覆盖拦截", rename_overwrite)


# ---------------------------------------------------------------------------
# 2. fetchUrl
# ---------------------------------------------------------------------------


def build_fetch_cases(acc: Acceptance, base_url: str) -> None:
    if shutil.which("curl") is None:
        acc.skip(SECTION_FETCH, "全部用例", "系统中未找到 curl")
        return
    if web_tools.trafilatura is None:
        acc.skip(SECTION_FETCH, "全部用例", "未安装 trafilatura")
        return

    def normal() -> str:
        payload = json.loads(fetch_url(f"{base_url}/fetch/ok", timeout=5))
        _check(payload.get("ok") is True, f"返回失败：{payload}")
        _check(
            "本地验收页面" in payload.get("content", ""),
            f"正文未包含预期内容：{payload.get('content', '')[:200]}",
        )
        return f"title={payload.get('title')!r}, chars={payload.get('content_chars')}"

    def timeout() -> str:
        payload = _expect_tool_error(
            lambda: fetch_url(f"{base_url}/fetch/slow", FETCH_TIMEOUT), "timeout"
        )
        return f"error.type={payload['error']['type']}"

    def invalid_url() -> str:
        payload = _expect_tool_error(
            lambda: fetch_url("ftp://example.com"), "invalid_url"
        )
        return f"error.type={payload['error']['type']}"

    def http_404() -> str:
        payload = _expect_tool_error(
            lambda: fetch_url(f"{base_url}/fetch/404"), "http_error"
        )
        return f"status_code={payload['error'].get('status_code')}"

    def truncation() -> str:
        payload = json.loads(fetch_url(f"{base_url}/fetch/large", timeout=10))
        _check(payload.get("ok") is True, f"返回失败：{payload}")
        _check(payload.get("truncated") is True, "长正文未被标记为截断")
        _check(
            payload.get("content_chars") == 20_000,
            f"截断后字符数异常：{payload.get('content_chars')}",
        )
        return "truncated=true, content_chars=20000"

    acc.run(SECTION_FETCH, "正常抓取本地页面", normal)
    acc.run(SECTION_FETCH, "超时（timeout=1）", timeout)
    acc.run(SECTION_FETCH, "参数错误：非 http/https", invalid_url)
    acc.run(SECTION_FETCH, "HTTP 404 错误", http_404)
    acc.run(SECTION_FETCH, "长正文截断", truncation)


# ---------------------------------------------------------------------------
# 3. searchWeb（本地 mock 端点）
# ---------------------------------------------------------------------------


def _make_search_tools(
    tmp_dir: Path,
    base_url: str,
    route: str,
    *,
    api_key: str = "acceptance-test-key",
    timeout: float = SEARCH_TIMEOUT,
) -> WebTools:
    """为单个用例生成临时 config.ini，并绑定 WebTools。"""
    config_path = tmp_dir / f"search_{route.strip('/').replace('/', '_')}.ini"
    config_path.write_text(
        "[search]\n"
        f"baidu_qianfan_api_key = {api_key}\n"
        f"endpoint = {base_url}{route}\n"
        f"timeout = {timeout:g}\n"
        "top_k = 5\n",
        encoding="utf-8",
    )
    return WebTools(config_path)


def build_search_cases(acc: Acceptance, base_url: str, tmp_dir: Path) -> None:
    if web_tools.requests is None:
        acc.skip(SECTION_SEARCH, "全部用例", "未安装 requests")
        return

    def normal() -> str:
        tools = _make_search_tools(tmp_dir, base_url, "/search/ok")
        payload = json.loads(tools.search_web("xAgent 验收", 2))
        _check(payload.get("ok") is True, f"返回失败：{payload}")
        _check(payload.get("count") == 2, f"结果数量异常：{payload.get('count')}")
        titles = [item.get("title") for item in payload.get("results", [])]
        _check(titles == ["验收结果 A", "验收结果 B"], f"结果标题异常：{titles}")
        return "mock 返回 2 条结果，标题与摘要解析正常"

    def timeout() -> str:
        tools = _make_search_tools(tmp_dir, base_url, "/search/slow")
        payload = _expect_tool_error(lambda: tools.search_web("超时测试"), "timeout")
        return f"error.type={payload['error']['type']}"

    def invalid_query() -> str:
        tools = _make_search_tools(tmp_dir, base_url, "/search/ok")
        payload = _expect_tool_error(lambda: tools.search_web("   "), "invalid_query")
        return f"error.type={payload['error']['type']}"

    def api_error() -> str:
        tools = _make_search_tools(tmp_dir, base_url, "/search/error")
        payload = _expect_tool_error(lambda: tools.search_web("接口错误"), "api_error")
        return f"error.type={payload['error']['type']}, api_code={payload['error'].get('api_code')}"

    def config_missing() -> str:
        tools = _make_search_tools(
            tmp_dir, base_url, "/search/ok", api_key="your-key-here"
        )
        payload = _expect_tool_error(lambda: tools.search_web("未配置"), "config_missing")
        return f"error.type={payload['error']['type']}"

    acc.run(SECTION_SEARCH, "正常搜索（mock）", normal)
    acc.run(SECTION_SEARCH, "超时（timeout=1）", timeout)
    acc.run(SECTION_SEARCH, "参数错误：空关键词", invalid_query)
    acc.run(SECTION_SEARCH, "API 业务错误", api_error)
    acc.run(SECTION_SEARCH, "配置缺失", config_missing)


# ---------------------------------------------------------------------------
# 4. RunCli
# ---------------------------------------------------------------------------


def _run_cli_json(command: str, args: list[str], timeout: float = 30.0) -> dict:
    payload = json.loads(cli_tools.run_cli(command, args, timeout=timeout))
    _check(payload.get("ok") is True, f"返回失败：{payload}")
    return payload


def build_cli_cases(acc: Acceptance, tmp_dir: Path) -> None:
    python = sys.executable

    def registry_dispatch() -> str:
        registry = build_default_registry(PathSandbox(tmp_dir / "cli_registry"))
        _check("RunCli" in registry, "RunCli 未注册进 ToolRegistry")
        schema_names = {
            item.get("function", {}).get("name") for item in registry.schemas()
        }
        _check("RunCli" in schema_names, "注册表 schemas 中没有 RunCli")
        result = registry.invoke(
            "RunCli",
            {"command": python, "args": ["-c", "print('registry-ok')"]},
        )
        _check(result.ok, f"注册表分发 RunCli 失败：{result.output}")
        payload = json.loads(result.output)
        _check(payload.get("exit_code") == 0, f"exit_code 异常：{payload}")
        _check("registry-ok" in payload.get("stdout", ""), "stdout 未包含预期输出")
        return "RunCli 已注册，registry.invoke 分发正常"

    def schema_check() -> str:
        function = cli_tools.RUN_CLI_SCHEMA.get("function", {})
        _check(function.get("name") == "RunCli", "schema 工具名不是 RunCli")
        params = function.get("parameters", {})
        props = params.get("properties", {})
        _check(set(props) == {"command", "args"}, f"schema 入参异常：{set(props)}")
        _check(params.get("required") == ["command"], "command 必须为必填项")
        _check(props["command"].get("type") == "string", "command 应为 string")
        _check(
            props["args"].get("type") == "array"
            and props["args"].get("items", {}).get("type") == "string",
            "args 应为 string 数组",
        )
        _check(
            params.get("additionalProperties") is False,
            "schema 应禁止额外参数",
        )
        return "name / 入参 / 类型 / additionalProperties 校验通过"

    def normal() -> str:
        payload = _run_cli_json(python, ["-c", "print('cli-ok')"])
        _check(payload.get("exit_code") == 0, f"exit_code 异常：{payload.get('exit_code')}")
        _check("cli-ok" in payload.get("stdout", ""), "stdout 未包含预期输出")
        _check(payload.get("stderr") == "", f"stderr 应为空：{payload.get('stderr')!r}")
        return "exit_code=0，stdout 正常，stderr 为空"

    def stderr_capture() -> str:
        payload = _run_cli_json(
            python, ["-c", "import sys; print('boom', file=sys.stderr)"]
        )
        _check("boom" in payload.get("stderr", ""), "stderr 未捕获到输出")
        return "stderr 捕获正常"

    def abnormal_exit() -> str:
        payload = _run_cli_json(
            python, ["-c", "import sys; print('bye'); sys.exit(7)"]
        )
        _check(payload.get("exit_code") == 7, f"exit_code 异常：{payload.get('exit_code')}")
        _check("bye" in payload.get("stdout", ""), "异常退出前的 stdout 丢失")
        return "exit_code=7，仍返回 ok=true 与完整输出"

    def timeout() -> str:
        payload = _expect_tool_error(
            lambda: cli_tools.run_cli(
                python,
                ["-c", "import time; print('start', flush=True); time.sleep(5)"],
                timeout=CLI_TIMEOUT,
            ),
            "timeout",
        )
        error = payload["error"]
        _check(error.get("exit_code") is None, "超时结果的 exit_code 应为 null")
        _check("start" in error.get("stdout", ""), "超时未返回已产生的部分输出")
        return "error.type=timeout，带部分 stdout，exit_code=null"

    def invalid_args() -> str:
        payload = _expect_tool_error(
            lambda: cli_tools.run_cli(python, "--version"), "invalid_arguments"
        )
        return f"error.type={payload['error']['type']}"

    def empty_command() -> str:
        payload = _expect_tool_error(lambda: cli_tools.run_cli(""), "invalid_arguments")
        return f"error.type={payload['error']['type']}"

    def command_not_found() -> str:
        payload = _expect_tool_error(
            lambda: cli_tools.run_cli("xagent_no_such_command_xyz"), "exec_error"
        )
        return f"error.type={payload['error']['type']}"

    def truncation() -> str:
        payload = _run_cli_json(python, ["-c", "print('x' * 50000)"])
        _check(payload.get("stdout_truncated") is True, "stdout 未标记截断")
        _check(
            "【输出已截断】" in payload.get("stdout", ""),
            "stdout 缺少【输出已截断】标记",
        )
        _check(
            len(payload.get("stdout", "")) <= MAX_CLI_OUTPUT_CHARS + 100,
            f"截断后长度异常：{len(payload.get('stdout', ''))}",
        )
        return "stdout_truncated=true，含【输出已截断】标记"

    def no_shell_injection() -> str:
        payload = _run_cli_json(
            python,
            ["-c", "import sys; print(repr(sys.argv[1:]))", "&&", "echo", "injected"],
        )
        parsed = ast.literal_eval(payload.get("stdout", "").strip())
        _check(
            parsed == ["&&", "echo", "injected"],
            f"参数没有原样传递，可能被 shell 解析：{parsed!r}",
        )
        return "&& / echo 等 shell 元字符被当作普通参数原样传递"

    acc.run(SECTION_CLI, "注册表注册与分发", registry_dispatch)
    acc.run(SECTION_CLI, "schema 定义校验", schema_check)
    acc.run(SECTION_CLI, "正常执行", normal)
    acc.run(SECTION_CLI, "stderr 捕获", stderr_capture)
    acc.run(SECTION_CLI, "异常退出（exit 7）", abnormal_exit)
    acc.run(SECTION_CLI, "超时（timeout=1）", timeout)
    acc.run(SECTION_CLI, "参数错误：args 不是列表", invalid_args)
    acc.run(SECTION_CLI, "参数错误：command 为空", empty_command)
    acc.run(SECTION_CLI, "程序不存在", command_not_found)
    acc.run(SECTION_CLI, "输出截断", truncation)
    acc.run(SECTION_CLI, "无 shell 注入", no_shell_injection)


# ---------------------------------------------------------------------------
# 环境提示
# ---------------------------------------------------------------------------


def agent_integration_hint() -> str:
    """检查 RunCli 是否已接入 Agent（只读源码，不实例化 Agent）。"""
    agent_path = PROJECT_ROOT / "core" / "agent.py"
    tools_init = PROJECT_ROOT / "core" / "tools" / "__init__.py"
    try:
        source = agent_path.read_text(encoding="utf-8")
        init_source = tools_init.read_text(encoding="utf-8")
    except OSError as exc:
        return f"无法读取接入文件：{exc}"

    registered = "register_cli_tools" in init_source
    dispatched = "RunCli" in source and "run_cli" in source
    if registered and dispatched:
        return "Agent 已接入 RunCli（注册表 schema + _dispatch_tool 分支），可通过 function calling 调用。"
    missing = []
    if not registered:
        missing.append("注册表注册")
    if not dispatched:
        missing.append("Agent 分发分支")
    return f"RunCli 接入不完整，缺少：{'、'.join(missing)}。"


def agent_tools_snapshot() -> str:
    """只读方式列出 Agent 将发送给模型的工具名（不请求 LLM）。"""
    from core.agent import Agent
    from core.config import Settings

    agent = Agent(settings=Settings.load())
    try:
        names = [item["function"]["name"] for item in agent.tools]
        return f"Agent 工具（{len(names)} 个）：{', '.join(names)}"
    finally:
        import asyncio

        asyncio.run(agent.aclose())


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _print_header(color: bool) -> None:
    def paint(text: str, key: str) -> str:
        if not color:
            return text
        return f"{_ANSI[key]}{text}{_ANSI['RESET']}"

    print(paint("xAgent 工具验收报告", "HEADER"))
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python：{sys.version.split()[0]}（{sys.executable}）")
    print(f"项目：{PROJECT_ROOT}")
    print("模式：fetchUrl / searchWeb 本地 mock；文件工具临时沙箱；RunCli 当前用户权限")
    print("=" * 68)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="xAgent 工具一键验收：文件工具 / fetchUrl / searchWeb / RunCli",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="显示用例详情与异常堆栈")
    parser.add_argument(
        "--strict", action="store_true", help="将 SKIP 也视为验收失败"
    )
    parser.add_argument("--no-color", action="store_true", help="关闭 ANSI 颜色")
    args = parser.parse_args(argv)

    color = (
        not args.no_color
        and sys.stdout.isatty()
        and os.getenv("NO_COLOR") is None
    )
    acc = Acceptance(strict=args.strict, verbose=args.verbose, color=color)
    _print_header(color)

    with tempfile.TemporaryDirectory(prefix="xagent_acceptance_") as tmp:
        tmp_dir = Path(tmp)
        sandbox = PathSandbox(tmp_dir / "sandbox")
        registry = ToolRegistry()
        register_file_tools(registry, FileTools(sandbox))

        build_file_cases(acc, registry, sandbox)

        with mock_server() as base_url:
            build_fetch_cases(acc, base_url)
            build_search_cases(acc, base_url, tmp_dir)

        build_cli_cases(acc, tmp_dir)

    acc.print_summary()
    return 0 if acc.is_success() else 1


if __name__ == "__main__":
    raise SystemExit(main())
