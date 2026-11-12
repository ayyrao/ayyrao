"""AnythingLLM API 客户端。

覆盖四类能力：工作区管理、文档上传与向量化、向量检索、对话问答。
所有请求统一超时；非 2xx 收敛成 AnythingLLMError，带上游错误正文。
"""

from __future__ import annotations

import httpx


class AnythingLLMError(RuntimeError):
    """上游调用失败，message 是可直接展示给模型的中文说明。"""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class AnythingLLMClient:
    """AnythingLLM REST API 异步客户端"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.client = httpx.AsyncClient(timeout=timeout)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            response = await self.client.request(method, url, headers=self.headers, **kwargs)
        except httpx.RequestError as exc:
            raise AnythingLLMError(
                f"连接 AnythingLLM 失败（{self.base_url}）：{type(exc).__name__}。"
                "请确认服务已启动，或检查 ANYTHINGLLM_BASE_URL。"
            ) from exc

        if response.status_code >= 400:
            detail = response.text[:200]
            raise AnythingLLMError(f"AnythingLLM 返回 {response.status_code}：{detail}")

        try:
            return response.json()
        except ValueError as exc:
            raise AnythingLLMError(f"AnythingLLM 返回的不是 JSON：{exc}") from exc

    async def vector_search(self, query: str, workspace_slug: str, limit: int = 4) -> list[dict]:
        """在工作区中进行向量搜索。

        参数：
        - query: 搜索查询/问题
        - workspace_slug: 工作区标识符
        - limit: 返回结果数量（1-10）
        """
        data = await self._request(
            "POST",
            f"/api/v1/workspace/{workspace_slug}/vector-search",
            json={"query": query, "limit": limit},
        )
        return data.get("results", [])

    async def get_workspace(self, workspace_slug: str) -> dict:
        """获取工作区详情（name、documents、threads 等）。"""
        data = await self._request("GET", f"/api/v1/workspace/{workspace_slug}")
        workspaces = data.get("workspace", [])
        return workspaces[0] if workspaces else {}

    async def list_workspaces(self) -> list[dict]:
        """获取所有工作区列表。"""
        data = await self._request("GET", "/api/v1/workspaces")
        return data.get("workspaces", [])

    async def create_workspace(self, name: str) -> dict:
        """新建工作区，返回 {slug, name}。"""
        data = await self._request("POST", "/api/v1/workspace/new", json={"name": name})
        workspace = data.get("workspace", {})
        return {"slug": workspace.get("slug"), "name": workspace.get("name", name)}

    async def upload_document(self, filename: str, content: bytes) -> dict:
        """上传单个文档，返回上游的 documents 列表（含 location）。"""
        files = {"file": (filename, content, "application/octet-stream")}
        data = await self._request("POST", "/api/v1/document/upload", files=files)
        return data

    async def update_embeddings(self, workspace_slug: str, adds: list[str]) -> dict:
        """把已上传文档加入工作区向量库。"""
        return await self._request(
            "POST",
            f"/api/v1/workspace/{workspace_slug}/update-embeddings",
            json={"adds": adds, "deletes": []},
        )

    async def chat(self, workspace_slug: str, message: str, mode: str = "chat") -> dict:
        """在工作区内对话，返回 textResponse 与 sources。"""
        data = await self._request(
            "POST",
            f"/api/v1/workspace/{workspace_slug}/chat",
            json={"message": message, "mode": mode},
        )
        return {
            "text": data.get("textResponse", ""),
            "sources": data.get("sources", []),
            "mode": data.get("mode", mode),
        }

    async def close(self):
        """关闭 HTTP 客户端"""
        await self.client.aclose()
