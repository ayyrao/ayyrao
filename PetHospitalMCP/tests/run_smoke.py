"""宠物医院 MCP 冒烟测试。

启动本地模拟上游，直接调用四个工具函数，验证：
正常查询 / 筛选 / 详情 / 检索 / 统计 / 参数校验 / 404 收敛 / 上游不可达重试。

运行：python tests/run_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PORT = 3912
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))


def start_mock() -> None:
    import uvicorn

    import mock_pet_server

    server = uvicorn.Server(
        uvicorn.Config(mock_pet_server.app, host="127.0.0.1", port=PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    import httpx

    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError("模拟上游启动失败")


async def main() -> int:
    os.environ["PET_HOSPITAL_URL"] = f"http://127.0.0.1:{PORT}"
    os.environ["PET_HOSPITAL_RETRIES"] = "2"
    start_mock()

    import main as pet_mcp

    # 工具注册
    tools = [tool.name for tool in await pet_mcp.server.list_tools()]
    check("注册 4 个业务工具", tools == ["list_pets", "get_pet_detail", "search_pets", "get_hospital_stats"], str(tools))

    # 列表
    data = json.loads(await pet_mcp.list_pets(page_size=5))
    check("列表返回 ok", data["ok"] is True, str(data)[:120])
    check("列表带总数与分页元信息", data["data"].get("total") == 3 and data["meta"]["page_size"] == 5, str(data["meta"]))

    # 筛选
    data = json.loads(await pet_mcp.list_pets(species="猫", sort_by="totalCost", order="desc"))
    check("按种类筛选生效", all(p["species"] == "猫" for p in data["data"]["items"]), str(data["data"])[:120])

    data = json.loads(await pet_mcp.list_pets(owner_name="任媛媛"))
    check("按主人筛选生效", data["data"]["total"] == 2, str(data["data"]["total"]))

    # 参数校验
    data = json.loads(await pet_mcp.list_pets(page_size=500))
    check("page_size 超限被拦", data["error"]["code"] == "VALIDATION_ERROR", str(data["error"]))
    data = json.loads(await pet_mcp.list_pets(status="不存在的状态"))
    check("非法状态被拦", data["error"]["code"] == "VALIDATION_ERROR", str(data["error"]))
    data = json.loads(await pet_mcp.list_pets(min_cost=900, max_cost=100))
    check("花费区间倒置被拦", data["error"]["code"] == "VALIDATION_ERROR", str(data["error"]))
    data = json.loads(await pet_mcp.list_pets(order="random"))
    check("非法排序方向被拦", data["error"]["code"] == "VALIDATION_ERROR", str(data["error"]))

    # 详情
    data = json.loads(await pet_mcp.get_pet_detail("PET-000002"))
    check("详情返回宠物 + 汇总", data["ok"] and data["data"]["summary"]["totalCost"] == 2400, str(data)[:160])

    data = json.loads(await pet_mcp.get_pet_detail("PET-999999"))
    check("不存在的档案返回 NOT_FOUND", data["error"]["code"] == "NOT_FOUND", str(data["error"]))

    data = json.loads(await pet_mcp.get_pet_detail("  "))
    check("空编号被拦", data["error"]["code"] == "VALIDATION_ERROR", str(data["error"]))

    # 检索
    data = json.loads(await pet_mcp.search_pets("CKD"))
    check("全文检索命中病历", data["ok"] and data["data"]["total"] == 1, str(data["data"])[:120])

    data = json.loads(await pet_mcp.search_pets("   "))
    check("空检索词被拦", data["error"]["code"] == "VALIDATION_ERROR", str(data["error"]))

    # 统计
    data = json.loads(await pet_mcp.get_hospital_stats())
    check("经营统计字段齐全", data["ok"] and "averageCost" in data["data"], str(data["data"])[:120])

    # 上游不可达：重试后给结构化错误
    os.environ["PET_HOSPITAL_URL"] = "http://127.0.0.1:3999"
    started = time.perf_counter()
    data = json.loads(await pet_mcp.get_hospital_stats())
    elapsed = (time.perf_counter() - started) * 1000
    check("上游不可达返回 UPSTREAM_UNAVAILABLE", data["error"]["code"] == "UPSTREAM_UNAVAILABLE", str(data["error"]))
    check("错误标记为可重试", data["error"]["retryable"] is True, str(data["error"]))
    check("确实做了退避重试（>0.6s）", elapsed > 600, f"{elapsed:.0f}ms")

    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print("宠物医院 MCP 冒烟测试")
    print("=" * 60)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  -> {detail}"))
    print("-" * 60)
    print(f"总计：{passed} 通过，{len(results) - passed} 失败")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
