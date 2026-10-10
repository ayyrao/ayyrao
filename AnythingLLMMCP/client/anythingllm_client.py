"""AnythingLLM API 客户端"""

import httpx
from typing import Optional


class AnythingLLMClient:
    """AnythingLLM REST API 异步客户端"""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.client = httpx.AsyncClient(timeout=30.0)

    async def vector_search(
        self,
        query: str,
        workspace_slug: str,
        limit: int = 4
    ) -> list[dict]:
        """
        在工作区中进行向量搜索。

        参数：
        - query: 搜索查询/问题
        - workspace_slug: 工作区标识符
        - limit: 返回结果数量（1-10）

        返回：
        - 搜索结果列表，每项包含 text、source 等字段
        """
        url = f"{self.base_url}/api/v1/workspace/{workspace_slug}/vector-search"
        response = await self.client.post(
            url,
            headers=self.headers,
            json={"query": query, "limit": limit}
        )
        response.raise_for_status()
        data = response.json()
        return data.get("results", [])

    async def get_workspace(self, workspace_slug: str) -> dict:
        """
        获取工作区详情。

        参数：
        - workspace_slug: 工作区标识符

        返回：
        - 工作区信息字典，包含 name、documents、threads 等
        """
        url = f"{self.base_url}/api/v1/workspace/{workspace_slug}"
        response = await self.client.get(url, headers=self.headers)
        response.raise_for_status()
        data = response.json()
        workspaces = data.get("workspace", [])
        return workspaces[0] if workspaces else {}

    async def list_workspaces(self) -> list[dict]:
        """
        获取所有工作区列表。

        返回：
        - 工作区列表
        """
        url = f"{self.base_url}/api/v1/workspaces"
        response = await self.client.get(url, headers=self.headers)
        response.raise_for_status()
        data = response.json()
        return data.get("workspaces", [])

    async def close(self):
        """关闭 HTTP 客户端"""
        await self.client.aclose()
