"""宠物医院 MCP Server。

把本地宠物医院 REST 服务（默认 http://127.0.0.1:8080）封装为 MCP 工具。
统一约定：
- 成功返回 {"ok": true, "data": ..., "meta": {...}}
- 失败返回 {"ok": false, "error": {"code": ..., "message": ..., "retryable": ...}}
所有异常都在本层收敛成结构化错误，不向上游透传 HTML 错误页。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from mcp.server import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
log = logging.getLogger("pet-hospital-mcp")

BASE_URL = os.getenv("PET_HOSPITAL_URL", "http://127.0.0.1:8080").rstrip("/")
TIMEOUT = float(os.getenv("PET_HOSPITAL_TIMEOUT", "10"))
RETRIES = int(os.getenv("PET_HOSPITAL_RETRIES", "2"))
MAX_PAGE_SIZE = 100

# 上游完整枚举见 GET /api/v1/meta；这里只做前置校验，最终由上游判定
PET_STATUSES = ("待就诊", "就诊中", "住院中", "已康复", "慢性病随访")

MCP_SERVER_NAME = "pet-hospital"
MCP_SERVER_VERSION = "1.1.0"

server = MCPServer(MCP_SERVER_NAME, version=MCP_SERVER_VERSION)


class ToolFailure(Exception):
    """可预期的业务失败，带稳定错误码。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _ok(data: Any, **meta: Any) -> str:
    return json.dumps({"ok": True, "data": data, "meta": meta}, ensure_ascii=False, indent=2)


def _err(code: str, message: str, *, retryable: bool = False, **meta: Any) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {"code": code, "message": message, "retryable": retryable},
            "meta": meta,
        },
        ensure_ascii=False,
        indent=2,
    )


def _base_url() -> str:
    """每次请求读取环境变量，便于测试与运行时切换上游。"""
    return os.getenv("PET_HOSPITAL_URL", BASE_URL).rstrip("/")


async def _request(method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
    """带超时与指数退避重试的请求；只对网络错误和 5xx 重试。"""
    url = f"{_base_url()}{path}"
    last_error: Exception | None = None

    for attempt in range(RETRIES + 1):
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.request(method, url, params=params)
            elapsed = (time.perf_counter() - started) * 1000
            log.info(
                "request method=%s path=%s status=%s elapsed_ms=%.1f attempt=%d",
                method, path, response.status_code, elapsed, attempt + 1,
            )

            if response.status_code == 404:
                raise ToolFailure("NOT_FOUND", f"上游没有该资源：{path}")
            if response.status_code >= 500:
                raise ToolFailure(
                    "UPSTREAM_ERROR",
                    f"上游服务异常（HTTP {response.status_code}）",
                    retryable=True,
                )
            if response.status_code >= 400:
                detail = response.text[:200]
                raise ToolFailure(
                    "UPSTREAM_REJECTED",
                    f"上游拒绝请求（HTTP {response.status_code}）：{detail}",
                )

            try:
                return response.json()
            except ValueError as exc:
                raise ToolFailure("UPSTREAM_FORMAT", f"上游返回的不是合法 JSON：{exc}") from exc

        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,
        ) as exc:
            elapsed = (time.perf_counter() - started) * 1000
            log.warning(
                "request_failed path=%s error=%s elapsed_ms=%.1f attempt=%d",
                path, type(exc).__name__, elapsed, attempt + 1,
            )
            last_error = exc
            if attempt < RETRIES:
                await asyncio.sleep(0.3 * (2 ** attempt))
                continue
            raise ToolFailure(
                "UPSTREAM_UNAVAILABLE",
                f"连不上宠物医院服务（{_base_url()}）：{type(exc).__name__}。"
                "请先启动 PetHospitalServer，或用 PET_HOSPITAL_URL 指向正确地址。",
                retryable=True,
            ) from exc

        except ToolFailure as exc:
            if exc.retryable and attempt < RETRIES:
                await asyncio.sleep(0.3 * (2 ** attempt))
                continue
            raise

    raise ToolFailure("UPSTREAM_UNAVAILABLE", f"重试 {RETRIES} 次后仍失败：{last_error}")


def _validate_page(page: int, page_size: int) -> None:
    if page < 1:
        raise ToolFailure("VALIDATION_ERROR", "page 必须 ≥ 1")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ToolFailure("VALIDATION_ERROR", f"page_size 必须在 1–{MAX_PAGE_SIZE} 之间")


@server.tool()
async def list_pets(
    species: str | None = None,
    name: str | None = None,
    doctor: str | None = None,
    disease: str | None = None,
    status: str | None = None,
    owner_name: str | None = None,
    owner_phone: str | None = None,
    min_cost: int | None = None,
    max_cost: int | None = None,
    sort_by: str | None = None,
    order: str = "desc",
    page: int = 1,
    page_size: int = 10,
) -> str:
    """查询宠物档案列表，支持按种类、医生、状态、疾病、主人、花费区间筛选，支持排序与分页。

    参数：
    - species: 种类，如「犬」「猫」
    - name: 宠物姓名模糊匹配
    - doctor / disease / status: 主治医生、疾病、就诊状态
    - owner_name / owner_phone: 主人姓名、电话
    - min_cost / max_cost: 总花费区间（元）
    - sort_by: 排序字段（name / totalCost / ageMonths 等）
    - order: asc 或 desc
    - page: 页码，从 1 开始
    - page_size: 每页条数，1–100
    """
    try:
        _validate_page(page, page_size)
        if order not in ("asc", "desc"):
            raise ToolFailure("VALIDATION_ERROR", "order 只能是 asc 或 desc")
        if status and status not in PET_STATUSES:
            raise ToolFailure(
                "VALIDATION_ERROR", f"status 不在允许值内：{', '.join(PET_STATUSES)}"
            )
        if min_cost is not None and min_cost < 0:
            raise ToolFailure("VALIDATION_ERROR", "min_cost 不能为负")
        if max_cost is not None and max_cost < 0:
            raise ToolFailure("VALIDATION_ERROR", "max_cost 不能为负")
        if min_cost is not None and max_cost is not None and min_cost > max_cost:
            raise ToolFailure("VALIDATION_ERROR", "min_cost 不能大于 max_cost")

        params: dict[str, Any] = {"order": order, "page": page, "pageSize": page_size}
        for value, remote in (
            (species, "species"),
            (name, "name"),
            (doctor, "doctor"),
            (disease, "disease"),
            (status, "status"),
            (owner_name, "ownerName"),
            (owner_phone, "ownerPhone"),
            (min_cost, "min"),
            (max_cost, "max"),
            (sort_by, "sortBy"),
        ):
            if value is not None:
                params[remote] = value

        data = await _request("GET", "/api/v1/pets", params=params)
        return _ok(data, page=page, page_size=page_size)
    except ToolFailure as exc:
        return _err(exc.code, exc.message, retryable=exc.retryable)


@server.tool()
async def get_pet_detail(pet_id: str) -> str:
    """按档案编号查询单只宠物的完整信息、历史病历与费用汇总。

    参数：
    - pet_id: 档案编号，形如 PET-000001
    """
    try:
        pet_id = (pet_id or "").strip()
        if not pet_id:
            raise ToolFailure("VALIDATION_ERROR", "pet_id 不能为空")
        detail = await _request("GET", f"/api/v1/pets/{pet_id}")
        try:
            summary = await _request("GET", f"/api/v1/pets/{pet_id}/summary")
        except ToolFailure:
            summary = None  # 汇总缺失不影响主流程
        return _ok({"pet": detail, "summary": summary})
    except ToolFailure as exc:
        return _err(exc.code, exc.message, retryable=exc.retryable)


@server.tool()
async def search_pets(query: str, page: int = 1, page_size: int = 10) -> str:
    """跨字段全文检索宠物档案（含病历全文），空格分词按 AND 匹配。

    参数：
    - query: 关键词，如「猫 疫苗」
    - page / page_size: 分页，page_size 1–100
    """
    try:
        query = (query or "").strip()
        if not query:
            raise ToolFailure("VALIDATION_ERROR", "query 不能为空")
        _validate_page(page, page_size)
        data = await _request(
            "GET",
            "/api/v1/pets/search",
            params={"q": query, "page": page, "pageSize": page_size},
        )
        return _ok(data, query=query)
    except ToolFailure as exc:
        return _err(exc.code, exc.message, retryable=exc.retryable)


@server.tool()
async def get_hospital_stats() -> str:
    """获取经营统计：档案数、总收入、客单价、种类与医生排行等。"""
    try:
        data = await _request("GET", "/api/v1/stats")
        return _ok(data)
    except ToolFailure as exc:
        return _err(exc.code, exc.message, retryable=exc.retryable)


if __name__ == "__main__":
    async def main() -> None:
        log.info("Pet Hospital MCP Server starting")
        log.info("upstream=%s timeout=%ss retries=%d", BASE_URL, TIMEOUT, RETRIES)
        log.info("tools=list_pets, get_pet_detail, search_pets, get_hospital_stats")
        await server.run_streamable_http_async(
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "9091")),
            streamable_http_path="/mcp",
        )

    asyncio.run(main())
