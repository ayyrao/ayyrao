"""AnythingLLM 网页端后端。

职责：托管前端静态页 + 代理 AnythingLLM API。
密钥只从环境变量读取，不下发到浏览器。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from anythingllm import AnythingLLMClient, AnythingLLMError, ConfigError, load_settings

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

app = FastAPI(title="AnythingLLM 知识库控制台", version="1.0.0")


class ChatRequest(BaseModel):
    workspace: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=4000)


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _client() -> AnythingLLMClient:
    settings = load_settings()
    return AnythingLLMClient(settings["base_url"], settings["api_key"])


@app.exception_handler(ConfigError)
async def _config_error(_request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"ok": False, "code": "config_error", "message": str(exc)},
    )


@app.exception_handler(AnythingLLMError)
async def _upstream_error(_request, exc: AnythingLLMError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "code": exc.code, "message": str(exc)},
    )


@app.get("/api/health")
async def health() -> dict:
    """前端启动时探活：同时判断密钥是否就绪。"""
    try:
        settings = load_settings()
    except ConfigError as exc:
        return {"ok": False, "code": "config_error", "message": str(exc)}
    return {
        "ok": True,
        "base_url": settings["base_url"],
        "default_workspace": settings["workspace"],
    }


@app.get("/api/workspaces")
async def list_workspaces() -> dict:
    client = _client()
    items = await client.list_workspaces()
    return {
        "ok": True,
        "data": [
            {"slug": w.get("slug"), "name": w.get("name"), "documents": len(w.get("documents") or [])}
            for w in items
        ],
    }


@app.post("/api/workspaces")
async def create_workspace(body: WorkspaceRequest) -> dict:
    client = _client()
    workspace = await client.create_workspace(body.name.strip())
    return {"ok": True, "data": {"slug": workspace.get("slug"), "name": workspace.get("name")}}


@app.post("/api/upload")
async def upload(
    workspace: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """上传文档并立即向量化，返回可读的进度阶段。"""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空。")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 20MB 上限。")

    client = _client()
    uploaded = await client.upload_document(
        file.filename or "upload.txt", content, file.content_type or "text/plain"
    )
    documents = uploaded.get("documents") or []
    if not documents:
        raise AnythingLLMError("上传成功但未返回文档位置，无法向量化。", code="upload_no_location")

    location = documents[0].get("location") or documents[0].get("url")
    result = await client.update_embeddings(workspace, [location])
    return {
        "ok": True,
        "data": {
            "filename": file.filename,
            "location": location,
            "embedded": True,
            "message": f"《{file.filename}》已上传并完成向量化。",
            "raw": result,
        },
    }


@app.post("/api/chat")
async def chat(body: ChatRequest) -> dict:
    client = _client()
    answer = await client.chat(body.workspace, body.message)
    return {"ok": True, "data": answer}


@app.post("/api/chat/stream")
async def chat_stream(body: ChatRequest) -> StreamingResponse:
    """SSE 流式返回；前端逐块渲染，失败时退回 /api/chat。"""
    client = _client()

    async def event_source():
        try:
            async for chunk in client.stream_chat(body.workspace, body.message):
                payload = json.dumps({"text": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001 - 前端需要看到失败原因
            payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8092, reload=False)
