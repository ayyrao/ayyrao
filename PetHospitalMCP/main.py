"""Pet Hospital MCP Server - list_pets tool"""

from mcp.server import MCPServer
import httpx
import json

# 配置
PET_HOSPITAL_URL = "http://127.0.0.1:8080"
MCP_SERVER_NAME = "pet-hospital"
MCP_SERVER_VERSION = "1.0.0"

# 创建 MCP Server
server = MCPServer(MCP_SERVER_NAME, version=MCP_SERVER_VERSION)

# HTTP 客户端
http_client = httpx.AsyncClient(timeout=30.0)


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
    """
    查询宠物档案列表，支持按种类、医生、状态、疾病、主人等多种参数筛选，支持排序和分页。

    参数说明：
    - species: 按种类筛选（如"犬"、"猫"）
    - name: 按宠物姓名模糊匹配
    - doctor: 按主治医生筛选
    - disease: 按疾病筛选
    - status: 按就诊状态筛选（待就诊/就诊中/住院中/已康复/慢性病随访）
    - owner_name: 按主人姓名筛选
    - owner_phone: 按主人电话筛选
    - min_cost / max_cost: 按总花费区间筛选
    - sort_by: 排序字段（name/totalCost/ageMonths 等）
    - order: 排序方向（asc/desc）
    - page / page_size: 分页参数
    """
    params = {}
    if species:
        params["species"] = species
    if name:
        params["name"] = name
    if doctor:
        params["doctor"] = doctor
    if disease:
        params["disease"] = disease
    if status:
        params["status"] = status
    if owner_name:
        params["ownerName"] = owner_name
    if owner_phone:
        params["ownerPhone"] = owner_phone
    if min_cost is not None:
        params["min"] = min_cost
    if max_cost is not None:
        params["max"] = max_cost
    if sort_by:
        params["sortBy"] = sort_by
    params["order"] = order
    params["page"] = page
    params["pageSize"] = page_size

    response = await http_client.get(
        f"{PET_HOSPITAL_URL}/api/v1/pets",
        params=params,
    )
    response.raise_for_status()
    data = response.json()
    return json.dumps(data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import asyncio

    async def main():
        print(f"[INFO] Pet Hospital MCP Server starting...")
        print(f"   Listen: http://127.0.0.1:9091/mcp")
        print(f"   Tool: list_pets")
        print(f"\nPress Ctrl+C to exit")

        await server.run_streamable_http_async(
            host="127.0.0.1",
            port=9091,
            streamable_http_path="/mcp",
        )

    asyncio.run(main())
