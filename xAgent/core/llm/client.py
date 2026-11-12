"""大模型客户端：对 OpenAI SDK 做一层很薄的归一化封装。

- 支持流式（stream=True）与非流式两种模式，对上层暴露统一的 ChatDelta
- 不包含任何界面代码
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from ..config import Settings


@dataclass(slots=True)
class ToolCallDelta:
    """流式工具调用片段；index 用于把同一调用的多个分片拼起来。"""

    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(slots=True)
class ChatDelta:
    """一次模型增量输出。

    usage 是 API 返回的 Token 用量（prompt_tokens / completion_tokens /
    total_tokens）。服务端没有返回时为 None，上层可退化为本地估算。
    """

    content: str = ""
    tool_calls: list[ToolCallDelta] = field(default_factory=list)
    usage: dict[str, int] | None = None


def _usage_dict(usage: Any) -> dict[str, int] | None:
    """把 OpenAI SDK 的 usage 对象转换成普通 dict，缺失时返回 None。"""
    if usage is None:
        return None
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or (prompt + completion))
    if prompt <= 0 and completion <= 0 and total <= 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ChatDelta]:
        """向模型发起一次请求，逐段产出 ChatDelta。"""
        request: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": self._settings.temperature,
        }
        # max_tokens 来自 config.ini；None 时不传，交给服务端默认值
        max_tokens = getattr(self._settings, "max_tokens", None)
        if isinstance(max_tokens, int) and max_tokens > 0:
            request["max_tokens"] = max_tokens
        if tools:
            request["tools"] = tools

        if self._settings.stream:
            async for delta in self._stream_chat(request):
                yield delta
        else:
            response = await self._client.chat.completions.create(**request)
            yield self._from_response(response)

    async def _stream_chat(self, request: dict[str, Any]) -> AsyncIterator[ChatDelta]:
        """流式请求。
        改动：只累积，流结束后一次性输出完整工具调用，避免分片叠加bug
        """
        payload = dict(request)
        payload["stream_options"] = {"include_usage": True}
        try:
            stream = await self._client.chat.completions.create(
                stream=True, **payload
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status not in (400, 422):
                raise
            payload.pop("stream_options", None)
            stream = await self._client.chat.completions.create(
                stream=True, **payload
            )
        tool_deltas: dict[int, ToolCallDelta] = {}
        final_usage: Any = None
        content_buffer = ""
        async for chunk in stream:
            if chunk.usage is not None:
                final_usage = chunk.usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            text = delta.content or ""
            content_buffer += text

            # 只累积工具分片，不往外发
            for tc in delta.tool_calls or []:
                idx = tc.index
                if idx not in tool_deltas:
                    tool_deltas[idx] = ToolCallDelta(index=idx)
                td = tool_deltas[idx]
                if tc.id:
                    td.id = tc.id
                if tc.function:
                    if tc.function.name:
                        td.name = tc.function.name
                    if tc.function.arguments:
                        td.arguments += tc.function.arguments
        # 流全部读完之后，一次性输出
        yield ChatDelta(
            content=content_buffer,
            tool_calls=list(tool_deltas.values()),
        )
        # 补发usage
        usage_dict = _usage_dict(final_usage)
        if usage_dict is not None:
            yield ChatDelta(usage=usage_dict)

    @staticmethod
    def _from_response(response: Any) -> ChatDelta:
        usage_dict = _usage_dict(response.usage)
        tool_calls: list[ToolCallDelta] = []
        message = response.choices[0].message
        for tc in message.tool_calls or []:
            tool_calls.append(ToolCallDelta(
                index=tc.index,
                id=tc.id,
                name=tc.function.name if tc.function else None,
                arguments=tc.function.arguments if tc.function else "",
            ))
        return ChatDelta(
            content=message.content or "",
            tool_calls=tool_calls,
            usage=usage_dict,
        )

    async def aclose(self) -> None:
        await self._client.close()
