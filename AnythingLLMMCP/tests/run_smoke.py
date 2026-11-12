"""AnythingLLM MCP 冒烟测试。

启动模拟上游，验证五类工具：工作区列表 / 新建 / 上传并向量化 / 向量检索 / 对话问答，
以及缺密钥时的可读报错。

运行：python tests/run_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PORT = 3913
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))


def start_mock() -> None:
    import uvicorn

    import mock_anythingllm

    server = uvicorn.Server(
        uvicorn.Config(mock_anythingllm.app, host="127.0.0.1", port=PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    import httpx

    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/docs", timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError("模拟上游启动失败")


def text_of(payload: dict) -> str:
    return payload["content"][0]["text"]


async def main() -> int:
    start_mock()

    # 先验证「没有密钥」时的表现
    os.environ.pop("ANYTHINGLLM_API_KEY", None)
    import main as anything_mcp

    no_key = await anything_mcp.list_workspaces()
    check("缺密钥返回可读提示而非抛栈", "ANYTHINGLLM_API_KEY" in text_of(no_key), text_of(no_key)[:80])

    # 配置密钥后重新指向模拟上游
    anything_mcp.ANYTHINGLLM_API_KEY = "test-key-123"
    anything_mcp.ANYTHINGLLM_BASE_URL = f"http://127.0.0.1:{PORT}"
    anything_mcp._client = None

    tools = [tool.name for tool in await anything_mcp.server.list_tools()]
    check(
        "注册 5 个工具，覆盖四类能力",
        tools == ["list_workspaces", "create_workspace", "upload_and_vectorize",
                  "query_knowledge_base", "chat_with_workspace"],
        str(tools),
    )

    listed = await anything_mcp.list_workspaces()
    check("工作区列表可读", "默认工作区" in text_of(listed), text_of(listed)[:80])

    created = await anything_mcp.create_workspace("宠物病历库")
    check("新建工作区成功", "已创建工作区" in text_of(created), text_of(created)[:80])
    slug = text_of(created).split("slug：")[-1].strip("。")

    created_bad = await anything_mcp.create_workspace("   ")
    check("空名称被拦", "1–80" in text_of(created_bad), text_of(created_bad)[:80])

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
        handle.write("PET-000001 小白 犬 肠胃炎 已康复")
        sample = handle.name

    uploaded = await anything_mcp.upload_and_vectorize(sample, slug)
    check("上传并向量化成功", "完成向量化" in text_of(uploaded), text_of(uploaded)[:100])

    missing = await anything_mcp.upload_and_vectorize("/tmp/不存在.txt", slug)
    check("不存在文件被拦", "文件不存在" in text_of(missing), text_of(missing)[:80])

    bad_type = await anything_mcp.upload_and_vectorize(__file__, slug)
    check("不支持的类型被拦", "不支持的文件类型" in text_of(bad_type), text_of(bad_type)[:80])

    searched = await anything_mcp.query_knowledge_base("疫苗", 4, slug)
    check("向量检索有结果", "搜索结果（共 2 条）" in text_of(searched), text_of(searched)[:120])

    empty_query = await anything_mcp.query_knowledge_base("  ", 4, slug)
    check("空检索词被拦", "query 不能为空" in text_of(empty_query), text_of(empty_query)[:80])

    answered = await anything_mcp.chat_with_workspace("这周有几只猫？", slug)
    check("对话返回正文", "基于工作区文档的回答" in text_of(answered), text_of(answered)[:100])
    check("对话带引用来源", "pets.txt" in text_of(answered), text_of(answered)[:120])

    bad_mode = await anything_mcp.chat_with_workspace("你好", slug, "completion")
    check("非法 mode 被拦", "chat 或 query" in text_of(bad_mode), text_of(bad_mode)[:80])

    os.unlink(sample)

    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print("AnythingLLM MCP 冒烟测试")
    print("=" * 60)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  -> {detail}"))
    print("-" * 60)
    print(f"总计：{passed} 通过，{len(results) - passed} 失败")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
