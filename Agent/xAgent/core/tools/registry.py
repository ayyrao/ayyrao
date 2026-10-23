"""工具注册表与调用分发。

新增工具时只需构造 ToolSpec 并 register，不需要改 Agent 主循环。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(Exception):
    """工具的可预期失败（路径越界、目录非空……）。

    这类错误会被转换为字符串回灌给模型，让模型自行纠正。
    """


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]

    def schema(self) -> dict[str, Any]:
        """OpenAI function-calling 格式的工具声明。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """一次工具调用的结构化结果。"""

    name: str
    arguments: dict[str, Any]
    output: str
    ok: bool
    elapsed: float


@dataclass(slots=True)
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict, init=False)

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., str],
    ) -> None:
        if name in self._tools:
            raise ValueError(f"工具 {name!r} 已注册，不能重复注册。")
        self._tools[name] = ToolSpec(name, description, parameters, handler)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._tools.values()]

    def invoke(self, name: str, arguments: str | dict[str, Any] | None = None) -> ToolExecution:
        """解析参数、执行工具，并把所有异常收敛成结构化结果。"""
        parsed, parse_error = self._parse_arguments(arguments)
        spec = self._tools.get(name)
        start = time.perf_counter()

        if parse_error is not None:
            return ToolExecution(name, parsed, parse_error, False, time.perf_counter() - start)
        if spec is None:
            message = f"工具不存在：{name}"
            return ToolExecution(name, parsed, message, False, time.perf_counter() - start)

        try:
            output = str(spec.handler(**parsed))
            ok, message = True, output
        except ToolError as exc:
            ok, message = False, str(exc)
        except TypeError as exc:  # 模型给出的参数与函数签名不匹配
            ok, message = False, f"工具参数错误：{exc}"
        except Exception as exc:  # 兜底：任何工具异常都不能让 Agent 崩溃
            ok, message = False, f"工具执行异常：{exc}"

        return ToolExecution(name, parsed, message, ok, time.perf_counter() - start)

    @staticmethod
    def _parse_arguments(
        arguments: str | dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None]:
        if arguments is None or arguments == "":
            return {}, None
        if isinstance(arguments, dict):
            return arguments, None
        if not isinstance(arguments, str):
            return {}, "工具参数必须是 JSON 对象。"
        try:
            data = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return {}, f"工具参数解析失败：{exc}"
        if not isinstance(data, dict):
            return {}, "工具参数必须是 JSON 对象。"
        return data, None
