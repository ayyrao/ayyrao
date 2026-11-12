"""AnythingLLM 后端客户端。

密钥只在这里读取（环境变量），前端永远拿不到。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

import httpx


class ConfigError(RuntimeError):
    """缺少必要配置时抛出，由路由层转成 503 + 可读信息。"""


class AnythingLLMError(RuntimeError):
    """上游 AnythingLLM 返回非 2xx 或网络失败。"""

    def __init__(self, message: str, *, status: int = 502, code: str = "upstream_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _load_dotenv() -> None:
    """极简 .env 读取：只补充尚未设置的环境变量，不覆盖已有值。"""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings() -> dict[str, str]:
    _load_dotenv()
    base_url = os.getenv("ANYTHINGLLM_BASE_URL", "http://localhost:3001").rstrip("/")
    api_key = os.getenv("ANYTHINGLLM_API_KEY", "").strip()
    workspace = os.getenv("ANYTHINGLLM_WORKSPACE", "").strip()
    if not api_key:
        raise ConfigError(
            "未配置 ANYTHINGLLM_API_KEY。请复制 .env.example 为 .env 并填入密钥，"
            "或直接导出环境变量后重启服务。"
        )
    return {"base_url": base_url, "api_key": api_key, "workspace": workspace}


class AnythingLLMClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout)

    def _raise(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = response.text[:300]
        raise AnythingLLMError(
            f"AnythingLLM 返回 {response.status_code}：{detail}",
            status=502,
            code="upstream_http_error",
        )

    async def list_workspaces(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/api/v1/workspaces", headers=self._headers
                )
            except httpx.RequestError as exc:
                raise AnythingLLMError(f"连接 AnythingLLM 失败：{exc}", code="upstream_unreachable")
            self._raise(response)
            return response.json().get("workspaces", [])

    async def create_workspace(self, name: str) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/workspace/new",
                    headers=self._headers,
                    json={"name": name},
                )
            except httpx.RequestError as exc:
                raise AnythingLLMError(f"连接 AnythingLLM 失败：{exc}", code="upstream_unreachable")
            self._raise(response)
            return response.json().get("workspace", {})

    async def upload_document(self, filename: str, content: bytes, mime: str) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/document/upload",
                    headers=self._headers,
                    files={"file": (filename, content, mime or "application/octet-stream")},
                )
            except httpx.RequestError as exc:
                raise AnythingLLMError(f"上传失败：{exc}", code="upstream_unreachable")
            self._raise(response)
            return response.json()

    async def update_embeddings(self, workspace: str, adds: list[str]) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/workspace/{workspace}/update-embeddings",
                    headers=self._headers,
                    json={"adds": adds, "deletes": []},
                )
            except httpx.RequestError as exc:
                raise AnythingLLMError(f"向量化失败：{exc}", code="upstream_unreachable")
            self._raise(response)
            return response.json()

    async def chat(self, workspace: str, message: str) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/workspace/{workspace}/chat",
                    headers=self._headers,
                    json={"message": message, "mode": "chat"},
                )
            except httpx.RequestError as exc:
                raise AnythingLLMError(f"对话失败：{exc}", code="upstream_unreachable")
            self._raise(response)
            data = response.json()
            return {
                "text": data.get("textResponse", ""),
                "sources": data.get("sources", []),
                "mode": data.get("mode", "chat"),
            }

    async def stream_chat(self, workspace: str, message: str) -> AsyncIterator[str]:
        """把 AnythingLLM 的 SSE 增量转成纯文本块。"""
        payload = {"message": message, "mode": "chat"}
        async with self._client() as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/v1/workspace/{workspace}/stream-chat",
                    headers=self._headers,
                    json=payload,
                ) as response:
                    if not response.is_success:
                        body = (await response.aread()).decode("utf-8", "ignore")[:300]
                        raise AnythingLLMError(
                            f"AnythingLLM 流式接口返回 {response.status_code}：{body}",
                            code="upstream_http_error",
                        )
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw in ("", "[DONE]"):
                            continue
                        try:
                            chunk = json.loads(raw)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            raise AnythingLLMError(str(chunk.get("errorMessage") or "流式对话出错"))
                        text = chunk.get("textResponse") or ""
                        if text:
                            yield text
            except httpx.RequestError as exc:
                raise AnythingLLMError(f"连接 AnythingLLM 失败：{exc}", code="upstream_unreachable")
