"""历史归档业务规则：状态流转、字段校验与筛选口径都收在这里。

归档批次清单不在本模块重新计算分区，而是读取存储落地已提交的分区计算快照，
清单中的区段与原落地记录一一对应。
"""
from __future__ import annotations

from typing import Any

from app.services.partition import partition_service
from app.store import store

MODULE = "archive"
LIST_FIELDS = ["策略编号", "数据来源", "归档周期", "归档格式", "保留年限", "压缩级别", "索引规则", "清理策略"]
REQUIRED_FIELDS = ["策略编号", "数据来源", "归档周期"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class ArchiveService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("策略编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

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
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"归档策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于历史归档可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"归档策略已{action}"

    # ---------------------------------------------------------------- 归档批次清单
    def list_manifest(
        self, *, config_code: str | None = None, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        """读取分区计算快照同步来的归档批次清单；半份数据与对不上落地记录的行不展示。"""
        rows = partition_service.manifest_rows(config_code or None)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total
