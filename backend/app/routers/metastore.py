"""元数据存储接口：维护元数据表，覆盖提交元数据评审、确认元数据、标记过期等动作。

元数据表清单会同步分区检测结论（缺失分区数/完整性）并跟随迁移快照里的
日期键更新 ``分区字段``；``/partition-conclusions`` 与 ``/consistency``
让相邻业务面直接核对日期键是否一致。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app import partition_rules as pr
from app.partition_analysis import aggregate_by_table
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.metastore import MetastoreService

router = APIRouter(prefix="/api/metastore", tags=["元数据存储"])

service = MetastoreService()

LIST_FIELDS = [
    "表编号",
    "表名",
    "所属数仓层",
    "字段数量",
    "日期键",
    "分区字段",
    "主键字段",
    "数据量级",
    "最近更新时间",
    "分区规则编号",
    "缺失分区数",
    "分区完整性结论",
]
STATUSES = ["草稿", "元数据评审中", "已确认", "已过期"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出元数据存储清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "metastore", "total": total, "items": items}


@router.get("/partition-conclusions", response_model=dict)
def partition_conclusions() -> dict[str, Any]:
    """元数据存储侧的分区结论：按表汇总，保留原规则编号与规则来源。"""
    conclusions = service.conclusions()
    grouped = aggregate_by_table(conclusions)
    return {
        "module": "metastore",
        "date_key": pr.DATE_KEY,
        "partition_field": pr.PARTITION_FIELD,
        "total": len(grouped),
        "items": list(grouped.values()),
    }


@router.get("/consistency", response_model=dict)
def consistency() -> dict[str, Any]:
    """自检：元数据存储与分区检测读取同一日期键和分区字段。"""
    return service.consistency()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表编号或表名检索"),
    status: str | None = Query(default=None, description="草稿、元数据评审中、已确认、已过期"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表编号/表名与状态过滤元数据存储列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条元数据表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条元数据表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="元数据表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条元数据表执行提交元数据评审、确认元数据、标记过期；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
