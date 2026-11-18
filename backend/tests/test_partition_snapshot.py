"""分区计算快照回归测试：统一口径、幂等写入、事务回退、历史保留与血缘对比。

直接运行：.venv/bin/python tests/test_partition_snapshot.py
也可以用 pytest 跑：.venv/bin/python -m pytest tests/test_partition_snapshot.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.archive import ArchiveService
from app.services.partition import PARTITION_FIELDS, partition_service
from app.services.storage import StorageService
from app.store import store

storage_service = StorageService()
archive_service = ArchiveService()


def setup_function() -> None:
    """每个用例前恢复示例数据，保证互不污染。"""
    store.reset()


def _entry(config_code: str) -> dict:
    return next(row for row in store.rows("storage") if row["配置编号"] == config_code)


def test_snapshot_carries_config_version_and_batch_boundaries() -> None:
    snap = partition_service.find_snapshot("STOR-0001", "2026-10-25", "2026-10-31")
    assert snap is not None, "种子快照 SNAP-0001 应存在"
    assert snap["配置版本"] == 1 and snap["配置来源"] == "冻结版v1"
    assert snap["批次开始"] == "2026-10-25" and snap["批次结束"] == "2026-10-31"
    assert len(snap["区段"]) == 7 and all(seg["保存天数"] == 30 for seg in snap["区段"])


def test_three_entries_read_same_snapshot() -> None:
    rows, _ = storage_service.list_entries(filters={"配置编号": "STOR-0001"})
    assert rows and rows[0]["分区快照编号"] == "SNAP-0001", "列表入口应读取已提交快照"
    summary = partition_service.summary_for("STOR-0001")
    assert rows[0]["分区口径"] == summary["分区口径"], "列表与共用实现口径一致"
    manifest, total = archive_service.list_manifest(config_code="STOR-0001")
    assert total == 7 and {row["快照编号"] for row in manifest} == {"SNAP-0001"}, "归档入口应读取同一份快照"
    compare = storage_service.compare_partitions("STOR-0001", None, None)
    assert compare is not None and compare["口径一致"] is True


def test_idempotent_resubmit_reads_existing() -> None:
    entry = _entry("STOR-0003")
    before = len(store.rows("partition_snapshots"))
    snap1, msg1, ok1 = storage_service.submit_partition_snapshot(
        entry["id"], batch_start="2026-10-01", batch_end="2026-10-31", mode="冻结版")
    assert ok1 and snap1 is not None
    assert len(store.rows("partition_snapshots")) == before + 1, "首次送交应写入一份"
    snap2, msg2, ok2 = storage_service.submit_partition_snapshot(
        entry["id"], batch_start="2026-10-01", batch_end="2026-10-31", mode="最新版")
    assert ok2 and snap2 is not None
    assert snap2["快照编号"] == snap1["快照编号"], "重复送交应读取已存在结果"
    assert "已存在" in msg2
    assert len(store.rows("partition_snapshots")) == before + 1, "重复送交不能再写入"


def test_overlap_batch_rolls_back_transaction() -> None:
    entry = _entry("STOR-0001")
    snaps_before = len(store.rows("partition_snapshots"))
    manifest_before = len(store.rows("archive_manifest"))
    snap, message, ok = storage_service.submit_partition_snapshot(
        entry["id"], batch_start="2026-10-30", batch_end="2026-11-05", mode="冻结版")
    assert not ok and snap is None, "与既有批次重叠应被拒绝"
    assert "重叠" in message
    assert len(store.rows("partition_snapshots")) == snaps_before, "快照落库应整体回退"
    assert len(store.rows("archive_manifest")) == manifest_before, "归档批次清单不应读到半份数据"
    assert entry["最近快照编号"] == "SNAP-0001", "落地记录应一并回退"


def test_invalid_batch_boundary_rejected() -> None:
    entry = _entry("STOR-0002")
    before = len(store.rows("partition_snapshots"))
    snap, message, ok = storage_service.submit_partition_snapshot(
        entry["id"], batch_start="2026-11-10", batch_end="2026-11-01", mode="冻结版")
    assert not ok and snap is None and "批次边界" in message
    assert len(store.rows("partition_snapshots")) == before


def test_historical_segments_keep_original_retention() -> None:
    entry = _entry("STOR-0001")  # 当前配置 v2：按天/90 天；历史快照冻结 v1：按天/30 天
    snap, message, ok = storage_service.submit_partition_snapshot(
        entry["id"], batch_start="2026-11-01", batch_end="2026-11-03", mode="最新版")
    assert ok and snap is not None, message
    carried = [seg for seg in snap["区段"] if seg["来源"] == "历史保留"]
    fresh = [seg for seg in snap["区段"] if seg["来源"] == "本批次新算"]
    assert carried and all(seg["保存天数"] == 30 for seg in carried), "历史区段保存天数必须按原配置保留"
    assert fresh and all(seg["保存天数"] == 90 for seg in fresh), "本批次新区段才采用当前配置"
    assert snap["校验结论"]["历史区段保存天数未变"] is True
    origin = partition_service.find_snapshot("STOR-0001", "2026-10-25", "2026-10-31")
    assert origin is not None and all(seg["保存天数"] == 30 for seg in origin["区段"]), "既有快照不得被改动"


def test_conflict_downstream_uses_single_frozen_source() -> None:
    conflict = partition_service.conflict_note("STOR-0001")
    assert conflict["存在"] is True and conflict["下游采用"] == "冻结版v1"
    manifest, _ = archive_service.list_manifest(config_code="STOR-0001")
    assert manifest, "冲突场景下清单仍应可读"
    sources = {row["快照编号"]: row["配置来源"] for row in manifest}
    assert sources == {"SNAP-0001": "冻结版v1"}, "下游只允许采用一份保留配置来源的完整快照"
    for row in manifest:
        assert row["配置冲突"] != "否", "清单应带上配置冲突说明"


def test_frozen_vs_latest_lineage_differs() -> None:
    compare = storage_service.compare_partitions("STOR-0001", "2026-11-01", "2026-11-03")
    assert compare is not None
    frozen, latest = compare["血缘对比"]["冻结版"], compare["血缘对比"]["最新版"]
    assert frozen["配置来源"] == "冻结版v1" and frozen["保存天数"] == 30
    assert latest["配置来源"] == "最新版v2" and latest["保存天数"] == 90
    assert frozen["区段数"] == latest["区段数"] == 3


def test_manifest_rows_correspond_to_landing_records() -> None:
    rows, total = archive_service.list_manifest()
    assert total == 8, "种子台账应同步出 7+1 行清单"
    for row in rows:
        assert row["对应落地记录"] == "是"
        snap = next(s for s in store.rows("partition_snapshots") if s["快照编号"] == row["快照编号"])
        assert any(
            seg["区段"] == row["区段"] and seg["保存天数"] == row["保存天数"] for seg in snap["区段"]
        ), "清单区段必须与原落地记录对应"
        assert row["校验结论"] == snap["校验结论"]["结论"], "校验结论应同步到归档台账"


def test_manifest_skips_half_written_data() -> None:
    # 模拟半份数据：清单行落库了，但对应快照缺失（同事务回退后的残留）。
    store.rows("archive_manifest").append({
        "id": 999, "清单编号": "MANI-0999", "配置编号": "STOR-0002", "快照编号": "SNAP-9999",
        "批次开始": "2026-11-01", "批次结束": "2026-11-02", "区段": "2026-11-01",
        "区段开始": "2026-11-01", "区段结束": "2026-11-01", "保存天数": 30,
        "配置来源": "冻结版v1", "校验结论": "通过", "同步时间": "2026-11-02T00:00:00",
    })
    rows, _ = archive_service.list_manifest(config_code="STOR-0002")
    assert rows == [], "归档批次清单不应读取半份数据"


def test_list_and_export_share_partition_fields() -> None:
    rows, _ = storage_service.list_entries()
    assert rows and all(set(PARTITION_FIELDS) <= set(row) for row in rows), "列表行应携带分区结果字段"
    by_code = {row["配置编号"]: row for row in rows}
    assert by_code["STOR-0001"]["分区快照编号"] == "SNAP-0001"
    assert by_code["STOR-0002"]["分区快照编号"] == "", "未提交快照的配置应为空口径"
    assert "沿用冻结版v1" in by_code["STOR-0001"]["分区校验结论"], "冲突说明应体现在校验结论里"


def test_created_entry_contract_and_defaults() -> None:
    entry, missing = storage_service.create_entry(
        {"配置编号": "STOR-9001", "存储类型": "对象存储", "连接信息": "s3://x", "保存天数": "验收-6"})
    assert not missing and entry is not None
    assert entry["配置版本"] == 1 and entry["最近快照编号"] == ""
    snap, message, ok = storage_service.submit_partition_snapshot(
        entry["id"], batch_start="2026-11-01", batch_end="2026-11-02", mode="冻结版")
    assert ok and snap is not None, "保存天数不可解析时应按默认 30 天兜底"
    assert all(seg["保存天数"] == 30 for seg in snap["区段"])


def main() -> int:
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        setup_function()
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"{failed}/{len(tests)} 项失败")
        return 1
    print(f"{len(tests)} 项分区计算快照测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
