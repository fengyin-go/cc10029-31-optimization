"""存储落地业务规则：状态流转、字段校验与筛选口径都收在这里。

分区判断不再在列表、导出、归档入口各自计算，统一走 app.services.partition
的分区计算快照：三个入口读取同一份已提交快照，保证批次结果一致。
"""
from __future__ import annotations

from typing import Any

from app.services.partition import PartitionError, partition_service
from app.store import store

MODULE = "storage"
LIST_FIELDS = ["配置编号", "存储类型", "连接信息", "分区策略", "压缩格式", "保存天数", "备份副本", "清理策略"]
REQUIRED_FIELDS = ["配置编号", "存储类型", "连接信息"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class StorageService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("配置编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        # 列表与导出共用同一份分区计算快照派生口径，避免两个入口结果不一致。
        page_rows = [
            {**row, **partition_service.summary_for(str(row.get("配置编号") or ""))}
            for row in rows[start:start + size]
        ]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["配置版本"] = 1
        entry["最近快照编号"] = ""
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"存储配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于存储落地可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"存储配置已{action}"

    # ---------------------------------------------------------------- 分区计算快照
    def list_partition_snapshots(
        self, entry_id: int, *, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        snapshots = partition_service.snapshots_for(str(entry.get("配置编号") or ""))
        total = len(snapshots)
        start = (page - 1) * size
        return snapshots[start:start + size], total

    def submit_partition_snapshot(
        self, entry_id: int, *, batch_start: Any, batch_end: Any, mode: str
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """送交分区计算快照：同一批次只写入一份，失败时同一事务整体回退。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"存储配置 {entry_id} 不存在或已归档", False
        try:
            snapshot, created = partition_service.get_or_create_snapshot(
                entry, batch_start, batch_end, mode
            )
        except PartitionError as exc:
            return None, str(exc), False
        if not created:
            return (
                snapshot,
                f"批次 {snapshot['批次开始']}~{snapshot['批次结束']} 已存在分区计算快照 "
                f"{snapshot['快照编号']}，直接读取既有结果",
                True,
            )
        return (
            snapshot,
            f"分区计算快照 {snapshot['快照编号']} 已写入，"
            f"归档批次清单已同步 {len(snapshot['区段'])} 个区段",
            True,
        )

    def compare_partitions(
        self, config_code: str, batch_start: str | None, batch_end: str | None
    ) -> dict[str, Any] | None:
        """对比列表、导出、归档三个入口的分区结果，并核对历史区段保存天数。"""
        entry = next(
            (row for row in store.rows(MODULE) if str(row.get("配置编号") or "") == config_code),
            None,
        )
        if entry is None:
            return None
        return partition_service.compare(entry, batch_start, batch_end)
