"""AnythingLLM MCP Server - 知识库向量搜索工具"""

from mcp.server import MCPServer
from client.anythingllm_client import AnythingLLMClient
from config import (
    ANYTHINGLLM_BASE_URL,
    ANYTHINGLLM_API_KEY,
    DEFAULT_WORKSPACE_SLUG,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    TOOL_TTL_MS,
    TOOL_CACHE_SCOPE,
    HTTP_HOST,
    HTTP_PORT,
    HTTP_PATH,
)

# 创建 MCP Server
server = MCPServer(MCP_SERVER_NAME, version=MCP_SERVER_VERSION)

# 初始化 AnythingLLM 客户端
client = AnythingLLMClient(ANYTHINGLLM_BASE_URL, ANYTHINGLLM_API_KEY)


@server.tool()
async def query_knowledge_base(
    query: str,
    limit: int = 4,
    workspace_slug: str | None = None,
) -> dict:
    """
    在 AnythingLLM 知识库中进行向量搜索，获取与问题相关的文档片段。

    参数说明：
    - query: 搜索查询/问题（必填）
    - limit: 返回结果数量，默认 4，范围 1-10
    - workspace_slug: 工作区标识符，默认使用配置中的工作区

    返回：
    - 包含搜索结果的文本，每条结果包含文档内容和来源信息
    """
    slug = workspace_slug or DEFAULT_WORKSPACE_SLUG

    # 验证 limit 范围
    limit = max(1, min(10, limit))

    try:
        # 执行向量搜索
        results = await client.vector_search(query, slug, limit)

        if not results:
            return {
                "resultType": "complete",
                "ttlMs": TOOL_TTL_MS,
                "cacheScope": TOOL_CACHE_SCOPE,
                "content": [{"type": "text", "text": "未找到相关结果。"}]
            }

        # 格式化结果
        formatted = []
        for i, r in enumerate(results, 1):
            text = r.get("text", "")
            source = r.get("source", "未知来源")
            formatted.append(f"{i}. [{source}] {text}")

        return {
            "resultType": "complete",
            "ttlMs": TOOL_TTL_MS,
            "cacheScope": TOOL_CACHE_SCOPE,
            "content": [{
                "type": "text",
                "text": f"搜索结果（共{len(results)}条）:\n\n" + "\n\n".join(formatted)
            }]
        }

    except Exception as e:
        return {
            "resultType": "complete",
            "ttlMs": TOOL_TTL_MS,
            "cacheScope": TOOL_CACHE_SCOPE,
            "content": [{"type": "text", "text": f"查询失败：{str(e)}"}]
        }


if __name__ == "__main__":
    import asyncio

    async def main():
        print(f"[INFO] AnythingLLM MCP Server starting...")
        print(f"   Listen: http://{HTTP_HOST}:{HTTP_PORT}{HTTP_PATH}")
        print(f"   Workspace: {DEFAULT_WORKSPACE_SLUG}")
        print(f"   Tool: query_knowledge_base")
        print(f"\nPress Ctrl+C to exit")

        # 使用 MCPServer 内置的 run_streamable_http_async 方法
        await server.run_streamable_http_async(
            host=HTTP_HOST,
            port=HTTP_PORT,
            streamable_http_path=HTTP_PATH,
        )

    asyncio.run(main())
