"""AnythingLLM 的本地模拟上游，仅用于测试。

只实现本项目用到的 6 个接口，行为参考 AnythingLLM 官方 API 的返回结构。
"""

from __future__ import annotations

import json
import uuid

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

app = FastAPI(title="Mock AnythingLLM")

STATE = {
    "workspaces": [{"slug": "default", "name": "默认工作区", "documents": []}],
    "chunks": {},   # slug -> [location]
}


def _auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing api key")


@app.get("/api/v1/workspaces")
async def list_workspaces(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {"workspaces": STATE["workspaces"]}


@app.post("/api/v1/workspace/new")
async def new_workspace(payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    slug = f"ws-{uuid.uuid4().hex[:8]}"
    workspace = {"slug": slug, "name": payload.get("name", "未命名"), "documents": []}
    STATE["workspaces"].append(workspace)
    return {"workspace": workspace}


@app.post("/api/v1/document/upload")
async def upload(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    _auth(authorization)
    content = await file.read()
    location = f"custom-documents/{file.filename}-{uuid.uuid4().hex[:6]}.json"
    return {
        "success": True,
        "documents": [{"location": location, "size": len(content), "title": file.filename}],
    }


@app.post("/api/v1/workspace/{slug}/update-embeddings")
async def update_embeddings(slug: str, payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    adds = payload.get("adds") or []
    STATE["chunks"].setdefault(slug, []).extend(adds)
    for workspace in STATE["workspaces"]:
        if workspace["slug"] == slug:
            workspace["documents"] = STATE["chunks"][slug]
    return {"workspace": {"slug": slug, "documents": STATE["chunks"].get(slug, [])}}


@app.post("/api/v1/workspace/{slug}/chat")
async def chat(slug: str, payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    question = payload.get("message", "")
    return {
        "textResponse": f"（模拟回答）关于「{question}」，已引用 {len(STATE['chunks'].get(slug, []))} 篇文档。",
        "sources": [{"title": "宠物档案样例.txt", "url": "file:///documents/pets.txt"}],
        "mode": "chat",
    }


@app.post("/api/v1/workspace/{slug}/stream-chat")
async def stream_chat(slug: str, payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)

    async def events():
        for piece in ["模拟", "流式", "回答"]:
            yield "data: " + json.dumps({"textResponse": piece}, ensure_ascii=False) + "\n\n"
        yield "data: " + json.dumps({"textResponse": "", "close": True}) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=3911)
