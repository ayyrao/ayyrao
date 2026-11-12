"""宠物医院 REST 服务的本地模拟上游，仅用于测试。

接口形态对齐 PetHospitalServer/README.md 的公开约定。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Mock Pet Hospital")

PETS = [
    {
        "id": "PET-000001",
        "name": "小白",
        "species": "犬",
        "doctor": "林医生",
        "disease": "肠胃炎",
        "status": "已康复",
        "ownerName": "任媛媛",
        "ownerPhone": "13800000001",
        "totalCost": 860,
        "records": [{"date": "2026-03-02", "diagnosis": "急性肠胃炎"}],
    },
    {
        "id": "PET-000002",
        "name": "橘子",
        "species": "猫",
        "doctor": "周医生",
        "disease": "慢性肾病",
        "status": "慢性病随访",
        "ownerName": "范语菲",
        "ownerPhone": "13800000002",
        "totalCost": 2400,
        "records": [{"date": "2026-04-11", "diagnosis": "CKD 二期"}],
    },
    {
        "id": "PET-000003",
        "name": "豆豆",
        "species": "猫",
        "doctor": "周医生",
        "disease": "疫苗反应",
        "status": "待就诊",
        "ownerName": "任媛媛",
        "ownerPhone": "13800000001",
        "totalCost": 120,
        "records": [],
    },
]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/v1/pets")
async def list_pets(
    name: str | None = None,
    species: str | None = None,
    doctor: str | None = None,
    status: str | None = None,
    ownerName: str | None = None,
    min: int | None = None,
    max: int | None = None,
    sortBy: str | None = None,
    order: str = "desc",
    page: int = 1,
    pageSize: int = 10,
):
    items = PETS
    if name:
        items = [p for p in items if name in p["name"]]
    if species:
        items = [p for p in items if p["species"] == species]
    if doctor:
        items = [p for p in items if p["doctor"] == doctor]
    if status:
        items = [p for p in items if p["status"] == status]
    if ownerName:
        items = [p for p in items if ownerName in p["ownerName"]]
    if min is not None:
        items = [p for p in items if p["totalCost"] >= min]
    if max is not None:
        items = [p for p in items if p["totalCost"] <= max]
    if sortBy == "totalCost":
        items = sorted(items, key=lambda p: p["totalCost"], reverse=(order == "desc"))

    start = (page - 1) * pageSize
    return {
        "items": items[start:start + pageSize],
        "total": len(items),
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/api/v1/pets/search")
async def search(q: str = "", page: int = 1, pageSize: int = 10):
    hits = [p for p in PETS if q in p["name"] or any(q in r.get("diagnosis", "") for r in p["records"])]
    return {"items": hits, "total": len(hits), "q": q}


@app.get("/api/v1/pets/{pet_id}")
async def get_pet(pet_id: str):
    for pet in PETS:
        if pet["id"] == pet_id:
            return pet
    raise HTTPException(status_code=404, detail="pet not found")


@app.get("/api/v1/pets/{pet_id}/summary")
async def get_summary(pet_id: str):
    for pet in PETS:
        if pet["id"] == pet_id:
            return {"id": pet_id, "totalCost": pet["totalCost"], "records": len(pet["records"])}
    raise HTTPException(status_code=404, detail="pet not found")


@app.get("/api/v1/stats")
async def stats():
    return {
        "pets": len(PETS),
        "totalRevenue": sum(p["totalCost"] for p in PETS),
        "averageCost": round(sum(p["totalCost"] for p in PETS) / len(PETS), 2),
        "bySpecies": {"犬": 1, "猫": 2},
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=3912)
