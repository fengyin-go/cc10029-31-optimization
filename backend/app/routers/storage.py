"""存储落地接口：维护存储配置，覆盖提交校验、启用配置、停用配置等动作。

分区计算快照沿用既有返回结构：列表仍是 {items,total,page,size}，动作仍是 {ok,message}；
列表、导出与归档入口读取同一份已提交快照，不再各自计算分区。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult, PartitionSnapshotPayload
from app.services.partition import PARTITION_FIELDS, PartitionError
from app.services.storage import StorageService

router = APIRouter(prefix="/api/storage", tags=["存储落地"])

service = StorageService()

LIST_FIELDS = ["配置编号", "存储类型", "连接信息", "分区策略", "压缩格式", "保存天数", "备份副本", "清理策略"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、配置编号关键字与状态过滤存储落地列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status", *PARTITION_FIELDS])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", ""),
                      *(item.get(field, "") for field in PARTITION_FIELDS)]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="storage.csv"'})


@router.get("/partition-compare")
def partition_compare(
    config_code: str = Query(alias="配置编号", description="按配置编号对比"),
    batch_start: str | None = Query(default=None, alias="批次开始"),
    batch_end: str | None = Query(default=None, alias="批次结束"),
) -> dict:
    """对比列表、导出、归档三个入口使用的分区结果；必须声明在 /{entry_id} 之前。"""
    try:
        result = service.compare_partitions(config_code, batch_start, batch_end)
    except PartitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"存储配置 {config_code} 不存在或已归档")
    return result


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条存储配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"存储配置 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/partition-snapshots", response_model=PageResult[dict])
def list_partition_snapshots(
    entry_id: int,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """读取该配置已提交的分区计算快照：配置版本、批次边界、区段与校验结论。"""
    result = service.list_partition_snapshots(entry_id, page=page, size=size)
    if result is None:
        raise HTTPException(status_code=404, detail=f"存储配置 {entry_id} 不存在或已归档")
    items, total = result
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条存储配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="存储配置已登记", entry=entry)


@router.post("/{entry_id}/partition-snapshots", response_model=ActionResult)
def submit_partition_snapshot(entry_id: int, payload: PartitionSnapshotPayload) -> ActionResult:
    """送交分区计算快照：同一批次只写入一份，落库失败时同一事务整体回退。"""
    snapshot, message, ok = service.submit_partition_snapshot(
        entry_id, batch_start=payload.批次开始, batch_end=payload.批次结束, mode=payload.版本模式)
    if not ok:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条存储配置执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
