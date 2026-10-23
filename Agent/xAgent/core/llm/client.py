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
    """一次模型增量输出。"""

    content: str = ""
    tool_calls: list[ToolCallDelta] = field(default_factory=list)


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
        if tools:
            request["tools"] = tools

        if self._settings.stream:
            async for delta in self._stream_chat(request):
                yield delta
        else:
            response = await self._client.chat.completions.create(**request)
            yield self._from_response(response)

    async def _stream_chat(self, request: dict[str, Any]) -> AsyncIterator[ChatDelta]:
        stream = await self._client.chat.completions.create(stream=True, **request)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            calls: list[ToolCallDelta] = []
            for call in delta.tool_calls or []:
                function = call.function
                calls.append(
                    ToolCallDelta(
                        index=call.index or 0,
                        id=call.id,
                        name=function.name if function else None,
                        arguments=(function.arguments or "") if function else "",
                    )
                )

            content = delta.content or ""
            if content or calls:
                yield ChatDelta(content=content, tool_calls=calls)

    @staticmethod
    def _from_response(response: Any) -> ChatDelta:
        message = response.choices[0].message
        calls = [
            ToolCallDelta(
                index=index,
                id=call.id,
                name=call.function.name if call.function else None,
                arguments=(call.function.arguments or "") if call.function else "",
            )
            for index, call in enumerate(message.tool_calls or [])
        ]
        return ChatDelta(content=message.content or "", tool_calls=calls)

    async def aclose(self) -> None:
        await self._client.close()
