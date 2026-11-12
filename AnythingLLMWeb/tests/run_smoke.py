"""AnythingLLM 网页端冒烟测试。

启动内置模拟上游，再用 TestClient 走一遍真实路由：
健康检查 / 工作区列表 / 新建 / 上传 + 向量化 / 一次性对话 / 流式对话 / 密钥不下发。

运行：python tests/run_smoke.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PORT = 3911

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))


def start_mock() -> None:
    import uvicorn

    import mock_anythingllm

    config = uvicorn.Config(mock_anythingllm.app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            import httpx

            httpx.get(f"http://127.0.0.1:{PORT}/docs", timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError("模拟上游启动失败")


def main() -> int:
    os.environ.pop("ANYTHINGLLM_API_KEY", None)
    start_mock()

    from fastapi.testclient import TestClient

    import app as webapp

    client = TestClient(webapp.app)

    # 1. 未配置密钥时的三态
    health = client.get("/api/health").json()
    check("无密钥 /api/health 给出 config_error", health.get("code") == "config_error", str(health))
    workspace_resp = client.get("/api/workspaces")
    check("无密钥请求被 503 拦截", workspace_resp.status_code == 503, workspace_resp.text[:80])

    # 2. 配置密钥，指向模拟上游
    os.environ["ANYTHINGLLM_API_KEY"] = "test-key-123"
    os.environ["ANYTHINGLLM_BASE_URL"] = f"http://127.0.0.1:{PORT}"

    health = client.get("/api/health").json()
    check("有密钥 /api/health 返回 ok", health.get("ok") is True, str(health))
    check("健康检查不泄露密钥", "test-key-123" not in str(health), str(health))

    # 3. 工作区
    workspaces = client.get("/api/workspaces").json()["data"]
    check("工作区列表可读", len(workspaces) >= 1, str(workspaces))

    created = client.post("/api/workspaces", json={"name": "宠物档案库"}).json()["data"]
    check("新建工作区成功", created.get("slug", "").startswith("ws-"), str(created))
    slug = created["slug"]

    # 4. 上传 + 向量化
    upload = client.post(
        "/api/upload",
        data={"workspace": slug},
        files={"file": ("pets.txt", b"PET-000001 \xe5\xb0\x8f\xe7\x99\xbd", "text/plain")},
    )
    payload = upload.json()
    check("上传成功返回 ok", upload.status_code == 200 and payload.get("ok") is True, str(payload)[:200])
    check("上传后已完成向量化", payload["data"].get("embedded") is True, str(payload["data"])[:200])

    # 5. 一次性对话（含引用）
    chat = client.post("/api/chat", json={"workspace": slug, "message": "有几只猫？"}).json()
    check("一次性对话返回文本", "模拟回答" in chat["data"]["text"], chat["data"]["text"])
    check("对话带回引用来源", len(chat["data"]["sources"]) == 1, str(chat["data"]["sources"]))

    # 6. 流式对话
    import json as _json

    with client.stream("POST", "/api/chat/stream", json={"workspace": slug, "message": "流式测试"}) as response:
        body = "".join(chunk for chunk in response.iter_text())
    pieces = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            continue
        pieces.append(_json.loads(raw).get("text", ""))
    check("流式接口返回 SSE", "data:" in body, body[:120])
    check("流式增量可拼成完整回答", "".join(pieces) == "模拟流式回答", "".join(pieces))

    # 7. 静态页
    check("首页可访问", client.get("/").status_code == 200)
    check("前端脚本可访问", client.get("/static/app.js").status_code == 200)

    # 8. 前端拿不到密钥
    index = client.get("/").text
    check("首页不含 API Key", "test-key-123" not in index)
    check("首页不含 Authorization 注入逻辑", "Authorization" not in index)

    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print("AnythingLLM 网页端冒烟测试")
    print("=" * 60)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  -> {detail}"))
    print("-" * 60)
    print(f"总计：{passed} 通过，{len(results) - passed} 失败")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
