"""航空器牵引接口：维护牵引任务，覆盖安排牵引、确认完成、取消任务与批量顺延。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, PostponeResult
from app.services.towing import TowingService

router = APIRouter(prefix="/api/towing", tags=["航空器牵引"])

service = TowingService()

LIST_FIELDS = ["牵引编号", "关联航班", "牵引车号", "起点机位", "终点机位", "牵引人员", "生效开始", "生效结束", "完成时刻", "牵引状态"]
STATUSES = ["待牵引", "牵引中", "已完成", "已取消"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按牵引编号检索"),
    status: str | None = Query(default=None, description="待牵引、牵引中、已完成、已取消"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按牵引编号与状态过滤航空器牵引列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/statistics", response_model=dict)
def statistics() -> dict[str, Any]:
    """牵引统计口径：与任务列表同源，顺延后的时段与机位口径同步刷新。"""
    return service.statistics()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出航空器牵引清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "towing", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条牵引任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"牵引任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条牵引任务，缺字段或违反时段/机位把关时说明原因而不是静默丢弃。"""
    entry, problems = service.create_entry(payload.values)
    if problems:
        return ActionResult(ok=False, message="；".join(problems))
    return ActionResult(ok=True, message="牵引任务已登记", entry=entry)


@router.post("/postpone", response_model=PostponeResult)
def postpone_entries(payload: EntryPayload) -> PostponeResult:
    """按生效时段批量顺延一批牵引任务，冲突的几条单独列出，其余照常顺延。"""
    values = payload.values
    result, message = service.postpone_entries(
        start_raw=values.get("start"),
        end_raw=values.get("end"),
        delta_minutes=values.get("delta_minutes"),
    )
    if result is None:
        raise HTTPException(status_code=400, detail=message)
    return PostponeResult(**result)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条牵引任务执行安排牵引、确认完成、取消任务；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
