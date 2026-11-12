"""AnythingLLM MCP Server。

把 AnythingLLM 的四类能力暴露给 Agent：
- 工作区管理：list_workspaces / create_workspace
- 文档入库：upload_and_vectorize（上传 + 立即向量化）
- 知识检索：query_knowledge_base（向量搜索）
- 对话问答：chat_with_workspace（带引用来源）

密钥从环境变量读取，缺失时工具返回可读提示而不是抛栈。
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server import MCPServer

from client.anythingllm_client import AnythingLLMClient, AnythingLLMError
from config import (
    ANYTHINGLLM_API_KEY,
    ANYTHINGLLM_BASE_URL,
    DEFAULT_WORKSPACE_SLUG,
    HTTP_HOST,
    HTTP_PATH,
    HTTP_PORT,
    MAX_UPLOAD_BYTES,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    REQUEST_TIMEOUT,
    TOOL_CACHE_SCOPE,
    TOOL_TTL_MS,
    missing_key_hint,
)

server = MCPServer(MCP_SERVER_NAME, version=MCP_SERVER_VERSION)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".docx", ".csv", ".json"}

_client: AnythingLLMClient | None = None


def _get_client() -> AnythingLLMClient:
    """按需创建客户端，避免缺密钥时在 import 阶段就崩。"""
    global _client
    if not ANYTHINGLLM_API_KEY:
        raise AnythingLLMError(missing_key_hint(), status=503)
    if _client is None:
        _client = AnythingLLMClient(ANYTHINGLLM_BASE_URL, ANYTHINGLLM_API_KEY, REQUEST_TIMEOUT)
    return _client


def _response(text: str) -> dict:
    return {
        "resultType": "complete",
        "ttlMs": TOOL_TTL_MS,
        "cacheScope": TOOL_CACHE_SCOPE,
        "content": [{"type": "text", "text": text}],
    }


def _failure(message: str) -> dict:
    return _response(f"调用失败：{message}")


def _resolve_slug(workspace_slug: str | None) -> str:
    slug = (workspace_slug or DEFAULT_WORKSPACE_SLUG or "").strip()
    if not slug:
        raise AnythingLLMError(
            "未指定工作区。请传入 workspace_slug，或设置环境变量 ANYTHINGLLM_WORKSPACE 作为默认值。"
        )
    return slug


@server.tool()
async def list_workspaces() -> dict:
    """列出 AnythingLLM 中的所有工作区及其文档数量。

    返回：工作区 slug、名称、文档数。
    """
    try:
        workspaces = await _get_client().list_workspaces()
        if not workspaces:
            return _response("当前没有任何工作区，可先调用 create_workspace 新建一个。")
        lines = [
            f"- {w.get('name', '未命名')}（slug: {w.get('slug')}，文档 {len(w.get('documents') or [])} 篇）"
            for w in workspaces
        ]
        return _response("工作区列表：\n" + "\n".join(lines))
    except AnythingLLMError as exc:
        return _failure(str(exc))


@server.tool()
async def create_workspace(name: str) -> dict:
    """新建一个工作区。

    参数：
    - name: 工作区名称，1–80 个字符
    """
    try:
        name = (name or "").strip()
        if not 1 <= len(name) <= 80:
            raise AnythingLLMError("工作区名称长度必须在 1–80 个字符之间。")
        workspace = await _get_client().create_workspace(name)
        return _response(f"已创建工作区「{workspace['name']}」，slug：{workspace['slug']}。")
    except AnythingLLMError as exc:
        return _failure(str(exc))


@server.tool()
async def upload_and_vectorize(file_path: str, workspace_slug: str | None = None) -> dict:
    """把一个本地文档上传到工作区并立即向量化，之后即可被检索或问答引用。

    参数：
    - file_path: 本地文件路径，支持 pdf / txt / md / docx / csv / json，单个不超过 20MB
    - workspace_slug: 目标工作区，缺省用配置里的默认工作区
    """
    try:
        slug = _resolve_slug(workspace_slug)
        path = Path((file_path or "").strip()).expanduser()
        if not path.is_file():
            raise AnythingLLMError(f"文件不存在：{path}")
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise AnythingLLMError(
                f"不支持的文件类型 {path.suffix}，可用：{', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )
        size = path.stat().st_size
        if size == 0:
            raise AnythingLLMError("文件是空的，未上传。")
        if size > MAX_UPLOAD_BYTES:
            raise AnythingLLMError(
                f"文件 {size / 1024 / 1024:.1f}MB 超过上限 {MAX_UPLOAD_BYTES / 1024 / 1024:.0f}MB。"
            )

        uploaded = await _get_client().upload_document(path.name, path.read_bytes())
        documents = uploaded.get("documents") or []
        if not documents:
            raise AnythingLLMError("上传成功但上游未返回文档位置，无法向量化。")

        location = documents[0].get("location") or documents[0].get("url")
        await _get_client().update_embeddings(slug, [location])
        return _response(
            f"《{path.name}》已上传到工作区 {slug} 并完成向量化（{size / 1024:.1f}KB）。"
            f"\n文档位置：{location}\n现在可以用 query_knowledge_base 或 chat_with_workspace 使用它。"
        )
    except AnythingLLMError as exc:
        return _failure(str(exc))
    except OSError as exc:
        return _failure(f"读取文件失败：{exc}")


@server.tool()
async def query_knowledge_base(
    query: str,
    limit: int = 4,
    workspace_slug: str | None = None,
) -> dict:
    """在工作区知识库里做向量检索，返回相关文档片段与来源。

    参数：
    - query: 检索问题（必填）
    - limit: 返回条数，1–10，默认 4
    - workspace_slug: 工作区标识符，缺省用默认工作区
    """
    try:
        query = (query or "").strip()
        if not query:
            raise AnythingLLMError("query 不能为空。")
        slug = _resolve_slug(workspace_slug)
        limit = max(1, min(10, limit))
        results = await _get_client().vector_search(query, slug, limit)
        if not results:
            return _response("未找到相关结果，请确认文档已上传并完成向量化。")
        formatted = [
            f"{index}. [{item.get('source', '未知来源')}] {item.get('text', '')}"
            for index, item in enumerate(results, 1)
        ]
        return _response(f"搜索结果（共 {len(results)} 条）：\n\n" + "\n\n".join(formatted))
    except AnythingLLMError as exc:
        return _failure(str(exc))


@server.tool()
async def chat_with_workspace(
    message: str,
    workspace_slug: str | None = None,
    mode: str = "chat",
) -> dict:
    """基于工作区文档回答问题，返回回答正文与引用来源。

    参数：
    - message: 用户问题（必填）
    - workspace_slug: 工作区标识符，缺省用默认工作区
    - mode: chat（默认）或 query
    """
    try:
        message = (message or "").strip()
        if not message:
            raise AnythingLLMError("message 不能为空。")
        if mode not in ("chat", "query"):
            raise AnythingLLMError("mode 只能是 chat 或 query。")
        slug = _resolve_slug(workspace_slug)
        answer = await _get_client().chat(slug, message, mode)
        text = answer["text"] or "（上游没有返回正文）"
        sources = answer.get("sources") or []
        if sources:
            titles = [source.get("title") or source.get("url") or "文档" for source in sources[:5]]
            text += "\n\n引用来源：" + "；".join(titles)
        return _response(text)
    except AnythingLLMError as exc:
        return _failure(str(exc))


if __name__ == "__main__":
    import asyncio

    async def main():
        print("[INFO] AnythingLLM MCP Server starting...")
        print(f"   Listen: http://{HTTP_HOST}:{HTTP_PORT}{HTTP_PATH}")
        print(f"   Upstream: {ANYTHINGLLM_BASE_URL}")
        print(f"   Default workspace: {DEFAULT_WORKSPACE_SLUG or '(未设置)'}")
        print("   Tools: list_workspaces / create_workspace / upload_and_vectorize / "
              "query_knowledge_base / chat_with_workspace")
        await server.run_streamable_http_async(
            host=HTTP_HOST,
            port=HTTP_PORT,
            streamable_http_path=HTTP_PATH,
        )

    asyncio.run(main())
