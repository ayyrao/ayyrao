"""AnythingLLM 的模拟上游，仅用于 AnythingLLMMCP 冒烟测试。"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, File, Header, HTTPException, UploadFile

app = FastAPI(title="Mock AnythingLLM for MCP")

STATE = {
    "workspaces": [{"slug": "default", "name": "默认工作区", "documents": []}],
}


def _auth(authorization: str | None) -> None:
    if authorization != "Bearer test-key-123":
        raise HTTPException(status_code=401, detail="missing or wrong api key")


@app.get("/api/v1/workspaces")
async def list_workspaces(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {"workspaces": STATE["workspaces"]}


@app.post("/api/v1/workspace/new")
async def new_workspace(payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    slug = f"ws-{uuid.uuid4().hex[:8]}"
    workspace = {"slug": slug, "name": payload.get("name"), "documents": []}
    STATE["workspaces"].append(workspace)
    return {"workspace": workspace}


@app.post("/api/v1/document/upload")
async def upload(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    _auth(authorization)
    content = await file.read()
    return {"documents": [{"location": f"custom-documents/{file.filename}-{uuid.uuid4().hex[:6]}.json",
                           "size": len(content)}]}


@app.post("/api/v1/workspace/{slug}/update-embeddings")
async def update_embeddings(slug: str, payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    for workspace in STATE["workspaces"]:
        if workspace["slug"] == slug:
            workspace["documents"].extend(payload.get("adds") or [])
    return {"workspace": {"slug": slug}}


@app.post("/api/v1/workspace/{slug}/vector-search")
async def vector_search(slug: str, payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {
        "results": [
            {"text": f"与「{payload.get('query')}」最相关的片段", "source": "pets.txt"},
            {"text": "第二段相关片段", "source": "pets.txt"},
        ]
    }


@app.post("/api/v1/workspace/{slug}/chat")
async def chat(slug: str, payload: dict, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {
        "textResponse": f"基于工作区文档的回答：{payload.get('message')}",
        "sources": [{"title": "pets.txt"}],
        "mode": payload.get("mode", "chat"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=3913)
