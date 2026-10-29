"""命令行工具：RunCli（subprocess 列表参数，禁用 shell）。

对外提供：
    run_cli(command, args=None)     执行本地程序，返回 stdout / stderr / exit_code
    RUN_CLI_SCHEMA                  RunCli 的 function-calling 声明
    CLI_TOOL_SCHEMAS                工具声明列表（可追加到 Agent 的 tools）
    register_cli_tools(registry)    把 RunCli 注册进 ToolRegistry
    CliTools                        可指定工作目录的实例版本

约束：
- 命令与参数组装成列表传给 subprocess，shell=False，禁止拼接 shell 字符串；
- 不实现命令白名单 / 黑名单 / 审批确认（教学实验环境），使用运行 xAgent 的
  当前用户权限执行；
- 固定超时，输出过长时截断并标注【输出已截断】；
- 超时、程序不存在等异常收敛成结构化 JSON 字符串，交给 LLM 读取并自行纠正。

本模块不 import 任何界面库，可被 CLI / TUI / Web 复用。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .registry import ToolError, ToolRegistry


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_CLI_TIMEOUT = 30.0  # 单次命令默认超时（秒）
MAX_CLI_TIMEOUT = 300.0  # 内部允许的最大超时，防止调用方传入过大值
MAX_OUTPUT_CHARS = 20_000  # stdout / stderr 各自回灌给模型的字符上限
TRUNCATION_MARKER = "【输出已截断】"


# ---------------------------------------------------------------------------
# 工具 schema（OpenAI function-calling 格式，直接追加到 Agent 的 tools 列表）
# ---------------------------------------------------------------------------

RUN_CLI_DESCRIPTION = (
    "在当前系统中以 xAgent 进程的用户权限执行一个命令行程序，返回 stdout、"
    "stderr 和 exit_code。command 为程序名称或路径，args 为参数列表，"
    "例如 command='git', args=['status', '--short']。"
    "命令不经过 shell，管道、重定向、通配符不会生效；"
    "没有参数时 args 传 []。exit_code 非 0 表示程序自身报告失败，"
    "输出仍会完整返回，不是工具调用错误。单次执行默认超时 30 秒。"
)

RUN_CLI_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": (
                "要执行的程序名称或绝对路径，例如 'python'、'git'、"
                "'C:/Windows/System32/ipconfig.exe'"
            ),
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "传给程序的参数列表，按顺序每个参数一个元素；"
                "没有参数时传 []"
            ),
            "default": [],
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}

RUN_CLI_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "RunCli",
        "description": RUN_CLI_DESCRIPTION,
        "parameters": RUN_CLI_PARAMETERS,
    },
}

CLI_TOOL_SCHEMAS: list[dict[str, object]] = [RUN_CLI_SCHEMA]


# ---------------------------------------------------------------------------
# 结构化错误 / 结果辅助（与 web_tools 保持相同格式）
# ---------------------------------------------------------------------------


def _fail(error_type: str, message: str, **details: object) -> None:
    """抛出结构化错误，Agent 分发层会把 JSON 文本原样回灌给模型。"""
    error: dict[str, object] = {"type": error_type, "message": message}
    error.update(details)
    payload = {"ok": False, "error": error}
    raise ToolError(json.dumps(payload, ensure_ascii=False))


def _ok(**payload: object) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _clamp_timeout(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = DEFAULT_CLI_TIMEOUT
    return max(1.0, min(MAX_CLI_TIMEOUT, number))


def _normalize_args(raw: object) -> list[str]:
    """把模型给出的参数列表统一成字符串列表。

    数字会被转成字符串（模型偶尔把端口号写成整数）；dict / list / bool
    等无法安全传给 subprocess 的类型直接拒绝。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        _fail("invalid_arguments", "args 必须是字符串列表，例如 [\"--version\"]。")

    normalized: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            normalized.append(item)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            normalized.append(str(item))
        else:
            _fail(
                "invalid_arguments",
                f"args[{index}] 必须是字符串，收到 {type(item).__name__}。",
            )
    return normalized


def _decode_output(raw: bytes | str | None) -> str:
    """解码子进程输出：UTF-8 优先，Windows 中文环境回退 GB18030。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _truncate_output(text: str) -> tuple[str, bool]:
    """超过上限时截断，并在末尾明确标注【输出已截断】。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    kept = text[:MAX_OUTPUT_CHARS]
    note = (
        f"\n{TRUNCATION_MARKER}原始输出 {len(text)} 字符，"
        f"仅保留前 {MAX_OUTPUT_CHARS} 字符。"
    )
    return kept + note, True


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


class CliTools:
    """命令行工具集合：可选绑定一个工作目录。"""

    def __init__(self, working_dir: str | Path | None = None) -> None:
        # None 表示继承 xAgent 进程的当前工作目录
        self._working_dir = (
            Path(working_dir).expanduser() if working_dir is not None else None
        )

    def run_cli(
        self,
        command: str,
        args: object = None,
        timeout: float = DEFAULT_CLI_TIMEOUT,
    ) -> str:
        """执行命令行程序，返回 stdout / stderr / exit_code。

        失败（参数非法、程序不存在、超时）时抛 ToolError，内容是
        {"ok": false, "error": {...}} 的 JSON。
        """
        if not isinstance(command, str) or not command.strip():
            _fail("invalid_arguments", "command 不能为空，必须是程序名称或路径。")
        program = command.strip()
        if "\x00" in program:
            _fail("invalid_arguments", "command 中不能包含空字符。")

        normalized_args = _normalize_args(args)
        if any("\x00" in item for item in normalized_args):
            _fail("invalid_arguments", "args 中不能包含空字符。")

        # 列表传参 + shell=False：参数不会被 shell 解析，杜绝命令注入。
        argv = [program, *normalized_args]
        timeout_s = _clamp_timeout(timeout)

        try:
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                cwd=self._working_dir,
            )
        except subprocess.TimeoutExpired as exc:
            # 超时也把已产生的部分输出回灌给模型，便于判断进度
            stdout, stdout_truncated = _truncate_output(_decode_output(exc.stdout))
            stderr, stderr_truncated = _truncate_output(_decode_output(exc.stderr))
            _fail(
                "timeout",
                f"命令执行超时（>{timeout_s:g} 秒）：{program}",
                command=program,
                args=normalized_args,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        except OSError as exc:
            _fail(
                "exec_error",
                f"无法执行命令 {program!r}：{exc}",
                command=program,
                args=normalized_args,
                exit_code=None,
                stdout="",
                stderr=str(exc),
            )
        except ValueError as exc:  # 例如参数中含无法编码的字符
            _fail(
                "invalid_arguments",
                f"命令参数不合法：{exc}",
                command=program,
                args=normalized_args,
                exit_code=None,
                stdout="",
                stderr=str(exc),
            )

        stdout, stdout_truncated = _truncate_output(_decode_output(proc.stdout))
        stderr, stderr_truncated = _truncate_output(_decode_output(proc.stderr))

        return _ok(
            command=program,
            args=normalized_args,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


# ---------------------------------------------------------------------------
# 模块级默认实例：供 agent.py 直接 import 调用
# ---------------------------------------------------------------------------

_default_tools: CliTools | None = None


def _get_default_tools() -> CliTools:
    global _default_tools
    if _default_tools is None:
        _default_tools = CliTools()
    return _default_tools


def run_cli(
    command: str,
    args: object = None,
    timeout: float = DEFAULT_CLI_TIMEOUT,
) -> str:
    """RunCli 工具入口（在 xAgent 进程当前工作目录执行）。"""
    return _get_default_tools().run_cli(command, args, timeout)


def register_cli_tools(registry: ToolRegistry) -> None:
    """把 RunCli 注册进 ToolRegistry。

    schema 与函数共用 RUN_CLI_PARAMETERS，保证模型看到的入参声明和
    注册表里的声明始终一致。
    """
    registry.register(
        name="RunCli",
        description=RUN_CLI_DESCRIPTION,
        parameters=RUN_CLI_PARAMETERS,
        handler=run_cli,
    )


__all__ = [
    "CLI_TOOL_SCHEMAS",
    "RUN_CLI_PARAMETERS",
    "RUN_CLI_SCHEMA",
    "CliTools",
    "register_cli_tools",
    "run_cli",
]
