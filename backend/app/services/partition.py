"""分区计算快照：列表、导出、归档三个入口共用的分区判定实现。

优化前，存储落地的分区判断散在列表、导出、归档三个入口各自计算，批次结果容易不一致。
本模块把分区计算抽取成一份共用实现：三个入口统一读取已提交的分区计算快照，不再各自判定。

按既有数据流梳理的处理步骤：
1. 读取存储配置（分区策略、保存天数、配置版本）与该配置已提交的既有快照（既有落地区段）。
2. 选定保留配置来源：冻结版沿用历史批次冻结的配置，最新版采用当前存储配置；
   一份快照只采用一份保留配置来源，当前配置与历史批次配置冲突时不混用。
3. 按分区策略把批次边界切分成区段，并计算每个区段的保存天数。
4. 历史区段按原配置保留（保存天数逐字沿用），统一计算不悄悄改动已落地数据。
5. 生成校验结论，把分区计算快照与归档批次清单在同一事务落库；任一步失败整体回退，
   归档批次清单不会读到半份数据。
6. 列表、导出、归档三个入口读取同一份已提交快照；重复送交同一批次时只写入一份，
   后续请求直接读取已存在结果。
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

MODULE = "storage"
SNAPSHOT_TABLE = "partition_snapshots"
MANIFEST_TABLE = "archive_manifest"

DEFAULT_RETENTION_DAYS = 30
DEFAULT_STRATEGY = "按天"
VERSION_MODES = ("冻结版", "最新版")
# 列表与导出共用的分区结果字段：两个入口都从同一份快照派生，保证口径一致。
PARTITION_FIELDS = ["分区快照编号", "分区口径", "分区校验结论"]


class PartitionError(ValueError):
    """分区计算快照提交校验失败：批次边界不合法、区段重叠、历史保存天数被改动等。"""


def _parse_day(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PartitionError(f"{field}「{value}」不是合法日期（YYYY-MM-DD）") from exc


def parse_retention(value: Any) -> int:
    """保存天数兜底解析：登记值不是正整数时按默认 30 天计。"""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return days if days > 0 else DEFAULT_RETENTION_DAYS


def parse_strategy(value: Any) -> str:
    text = str(value or "")
    if "周" in text:
        return "按周"
    if "月" in text:
        return "按月"
    return DEFAULT_STRATEGY


def cut_segments(strategy: str, start: date, end: date) -> list[tuple[str, date, date]]:
    """把批次边界按分区策略切成区段，返回 (区段名, 区段开始, 区段结束) 列表。"""
    segments: list[tuple[str, date, date]] = []
    cursor = start
    while cursor <= end:
        if strategy == "按月":
            month_end = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
            seg_end = min(month_end, end)
            segments.append((cursor.strftime("%Y-%m"), cursor, seg_end))
        elif strategy == "按周":
            seg_end = min(cursor + timedelta(days=6 - cursor.weekday()), end)
            iso_year, iso_week, _ = cursor.isocalendar()
            segments.append((f"{iso_year}-W{iso_week:02d}", cursor, seg_end))
        else:
            seg_end = cursor
            segments.append((cursor.isoformat(), cursor, seg_end))
        cursor = seg_end + timedelta(days=1)
    return segments


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start <= b_end and b_start <= a_end


class PartitionService:
    """分区计算快照的共用实现：计算、校验、落库、读取都收在这一处。"""

    # ---------------------------------------------------------------- 基础读取
    def snapshots_for(self, config_code: str) -> list[dict[str, Any]]:
        """该配置已提交的全部分区计算快照，按提交顺序排列。"""
        return [row for row in store.rows(SNAPSHOT_TABLE) if row.get("配置编号") == config_code]

    def latest_snapshot(self, config_code: str) -> dict[str, Any] | None:
        snapshots = self.snapshots_for(config_code)
        return snapshots[-1] if snapshots else None

    def find_snapshot(self, config_code: str, batch_start: str, batch_end: str) -> dict[str, Any] | None:
        for row in self.snapshots_for(config_code):
            if row.get("批次开始") == batch_start and row.get("批次结束") == batch_end:
                return row
        return None

    def _entry_for(self, config_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("配置编号") or "") == config_code:
                return row
        return None

    @staticmethod
    def config_of(entry: dict[str, Any]) -> dict[str, Any]:
        """当前存储配置的保留口径：配置版本、分区策略、保存天数。"""
        return {
            "配置版本": int(entry.get("配置版本") or 1),
            "分区策略": parse_strategy(entry.get("分区策略")),
            "保存天数": parse_retention(entry.get("保存天数")),
        }

    def frozen_source(self, config_code: str) -> dict[str, Any] | None:
        """历史批次冻结的保留配置来源；还没有快照时返回 None。"""
        snapshots = self.snapshots_for(config_code)
        if not snapshots:
            return None
        frozen = snapshots[0]
        return {
            "配置版本": int(frozen.get("配置版本") or 1),
            "分区策略": str(frozen.get("分区策略") or DEFAULT_STRATEGY),
            "保存天数": parse_retention(frozen.get("保存天数")),
        }

    def conflict_note(self, config_code: str) -> dict[str, Any]:
        """当前存储配置与最新快照的保留配置来源是否冲突；冲突时下游只沿用一份完整快照。"""
        entry = self._entry_for(config_code)
        snapshot = self.latest_snapshot(config_code)
        if entry is None or snapshot is None:
            return {"存在": False, "说明": "尚无已提交快照，不存在配置冲突"}
        current = self.config_of(entry)
        if current["分区策略"] == snapshot.get("分区策略") and current["保存天数"] == parse_retention(snapshot.get("保存天数")):
            return {"存在": False, "说明": "当前配置与最新快照的保留配置来源一致"}
        source = str(snapshot.get("配置来源") or "冻结版")
        return {
            "存在": True,
            "下游采用": source,
            "说明": (
                f"当前配置{current['分区策略']}/保存{current['保存天数']}天(v{current['配置版本']}) "
                f"与快照{source}的{snapshot.get('分区策略')}/保存{parse_retention(snapshot.get('保存天数'))}天不一致，"
                f"下游只采用{source}的完整分区计算快照"
            ),
        }

    # ---------------------------------------------------------------- 统一计算
    def compute_lineage(self, entry: dict[str, Any], start: date, end: date, mode: str) -> dict[str, Any]:
        """按选定的保留配置来源试算本批次区段（不落库），冻结版与最新版会得到不同血缘。"""
        config_code = str(entry.get("配置编号") or "")
        current = self.config_of(entry)
        if mode == "最新版":
            source = current
        else:
            source = self.frozen_source(config_code) or current
        label = f"{mode}v{source['配置版本']}"
        segments = [
            {
                "区段": name,
                "开始": seg_start.isoformat(),
                "结束": seg_end.isoformat(),
                "保存天数": source["保存天数"],
                "来源": "本批次新算",
                "配置来源": label,
            }
            for name, seg_start, seg_end in cut_segments(source["分区策略"], start, end)
        ]
        return {
            "配置来源": label,
            "配置版本": source["配置版本"],
            "分区策略": source["分区策略"],
            "保存天数": source["保存天数"],
            "区段": segments,
        }

    def historical_segments(self, config_code: str) -> list[dict[str, Any]]:
        """既有落地区段：从已提交快照逐字沿用，保存天数保持原配置不变。"""
        segments: dict[str, dict[str, Any]] = {}
        for snapshot in self.snapshots_for(config_code):
            for seg in snapshot.get("区段", []):
                segments.setdefault(
                    str(seg.get("区段")),
                    {
                        "区段": seg.get("区段"),
                        "开始": seg.get("开始"),
                        "结束": seg.get("结束"),
                        "保存天数": parse_retention(seg.get("保存天数")),
                        "来源": "历史保留",
                        "配置来源": seg.get("配置来源") or snapshot.get("配置来源"),
                    },
                )
        return list(segments.values())

    # ---------------------------------------------------------------- 校验
    def _validate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """生成校验结论：批次不重叠、历史区段保存天数未变；失败时由调用方回退事务。"""
        config_code = str(snapshot.get("配置编号"))
        committed = [s for s in self.snapshots_for(config_code) if s.get("快照编号") != snapshot.get("快照编号")]
        for other in committed:
            if _overlaps(str(snapshot["批次开始"]), str(snapshot["批次结束"]), str(other["批次开始"]), str(other["批次结束"])):
                return {
                    "结论": "失败：区段重叠",
                    "历史区段保存天数未变": True,
                    "说明": (
                        f"批次 {snapshot['批次开始']}~{snapshot['批次结束']} 与已提交快照 "
                        f"{other['快照编号']}（{other['批次开始']}~{other['批次结束']}）区段重叠，本次落库已回退"
                    ),
                }
        origin: dict[str, int] = {}
        for other in committed:
            for seg in other.get("区段", []):
                origin.setdefault(str(seg.get("区段")), parse_retention(seg.get("保存天数")))
        changed = [
            str(seg.get("区段"))
            for seg in snapshot.get("区段", [])
            if seg.get("来源") == "历史保留" and str(seg.get("区段")) in origin and origin[str(seg.get("区段"))] != parse_retention(seg.get("保存天数"))
        ]
        if changed:
            return {
                "结论": "失败：历史保存天数被改动",
                "历史区段保存天数未变": False,
                "说明": f"历史区段 {'、'.join(changed)} 的保存天数与原落地记录不一致，本次落库已回退",
            }
        carried = sum(1 for seg in snapshot.get("区段", []) if seg.get("来源") == "历史保留")
        fresh = len(snapshot.get("区段", [])) - carried
        return {
            "结论": "通过",
            "历史区段保存天数未变": True,
            "说明": f"批次 {snapshot['批次开始']}~{snapshot['批次结束']} 新算区段 {fresh} 个，历史区段 {carried} 个按原配置保留",
        }

    # ---------------------------------------------------------------- 落库（同一事务）
    def get_or_create_snapshot(
        self,
        entry: dict[str, Any],
        batch_start: Any,
        batch_end: Any,
        mode: str,
    ) -> tuple[dict[str, Any], bool]:
        """送交分区计算快照：同一批次只写入一份，重复送交直接读取已存在结果。"""
        config_code = str(entry.get("配置编号") or "").strip()
        if not config_code:
            raise PartitionError("存储配置缺少配置编号，无法送交分区计算快照")
        if mode not in VERSION_MODES:
            raise PartitionError(f"版本模式「{mode}」不支持，只能是冻结版或最新版")
        start = _parse_day(batch_start, "批次开始")
        end = _parse_day(batch_end, "批次结束")
        if start > end:
            raise PartitionError(f"批次开始 {start.isoformat()} 晚于批次结束 {end.isoformat()}，批次边界不合法")
        existing = self.find_snapshot(config_code, start.isoformat(), end.isoformat())
        if existing is not None:
            return existing, False

        lineage = self.compute_lineage(entry, start, end, mode)
        snapshot: dict[str, Any] = {
            "配置编号": config_code,
            "配置版本": lineage["配置版本"],
            "版本模式": mode,
            "配置来源": lineage["配置来源"],
            "分区策略": lineage["分区策略"],
            "保存天数": lineage["保存天数"],
            "批次开始": start.isoformat(),
            "批次结束": end.isoformat(),
            "区段": [*self.historical_segments(config_code), *lineage["区段"]],
            "提交时间": datetime.now().isoformat(timespec="seconds"),
        }
        # 快照、归档批次清单、落地记录在同一事务写入；校验失败整体回退，清单读不到半份数据。
        with store.transaction():
            snapshot["id"] = self._next_id(SNAPSHOT_TABLE)
            snapshot["快照编号"] = f"SNAP-{snapshot['id']:04d}"
            store.rows(SNAPSHOT_TABLE).append(snapshot)
            conclusion = self._validate(snapshot)
            snapshot["校验结论"] = conclusion
            if str(conclusion["结论"]).startswith("失败"):
                raise PartitionError(str(conclusion["说明"]))
            self._sync_manifest(snapshot)
            entry["最近快照编号"] = snapshot["快照编号"]
        return snapshot, True

    @staticmethod
    def _next_id(table: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1

    def _sync_manifest(self, snapshot: dict[str, Any]) -> None:
        """把校验结论同步到历史归档的归档批次清单（另一份业务台账），与快照同一事务。"""
        rows = store.rows(MANIFEST_TABLE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        for seg in snapshot.get("区段", []):
            rows.append(
                {
                    "id": next_id,
                    "清单编号": f"MANI-{next_id:04d}",
                    "配置编号": snapshot["配置编号"],
                    "快照编号": snapshot["快照编号"],
                    "批次开始": snapshot["批次开始"],
                    "批次结束": snapshot["批次结束"],
                    "区段": seg.get("区段"),
                    "区段开始": seg.get("开始"),
                    "区段结束": seg.get("结束"),
                    "保存天数": parse_retention(seg.get("保存天数")),
                    "配置来源": seg.get("配置来源") or snapshot.get("配置来源"),
                    "校验结论": snapshot["校验结论"]["结论"],
                    "同步时间": snapshot["提交时间"],
                }
            )
            next_id += 1

    # ---------------------------------------------------------------- 三个入口的读取口径
    def summary_for(self, config_code: str) -> dict[str, str]:
        """列表与导出入口共用的分区结果：同一份已提交快照派生，保证两个入口口径一致。"""
        snapshot = self.latest_snapshot(config_code)
        if snapshot is None:
            return {"分区快照编号": "", "分区口径": "", "分区校验结论": ""}
        conclusion = str(snapshot.get("校验结论", {}).get("结论", ""))
        conflict = self.conflict_note(config_code)
        if conflict.get("存在"):
            conclusion = f"{conclusion}（当前配置已变更，沿用{conflict['下游采用']}）"
        return {
            "分区快照编号": str(snapshot.get("快照编号") or ""),
            "分区口径": f"{snapshot.get('分区策略')}·保存{parse_retention(snapshot.get('保存天数'))}天·{len(snapshot.get('区段', []))}区段",
            "分区校验结论": conclusion,
        }

    def manifest_rows(self, config_code: str | None = None) -> list[dict[str, Any]]:
        """归档批次清单：只读已完整提交的快照台账，清单区段必须与原落地记录对应。"""
        snapshots = {str(row.get("快照编号")): row for row in store.rows(SNAPSHOT_TABLE)}
        result: list[dict[str, Any]] = []
        for row in store.rows(MANIFEST_TABLE):
            if config_code and row.get("配置编号") != config_code:
                continue
            snapshot = snapshots.get(str(row.get("快照编号")))
            if snapshot is None:
                continue  # 快照未完整提交（半份数据），清单不读取
            matched = any(
                str(seg.get("区段")) == str(row.get("区段")) and parse_retention(seg.get("保存天数")) == parse_retention(row.get("保存天数"))
                for seg in snapshot.get("区段", [])
            )
            if not matched:
                continue  # 清单区段与原落地记录对不上，宁可不展示
            conflict = self.conflict_note(str(row.get("配置编号")))
            result.append({**row, "对应落地记录": "是", "配置冲突": conflict["说明"] if conflict.get("存在") else "否"})
        return result

    # ---------------------------------------------------------------- 操作者对比
    def compare(self, entry: dict[str, Any], batch_start: Any, batch_end: Any) -> dict[str, Any]:
        """对比列表、导出、归档使用的分区结果，并给出冻结版/最新版血缘与历史保存天数核对。"""
        config_code = str(entry.get("配置编号") or "")
        latest = self.latest_snapshot(config_code)
        if batch_start and batch_end:
            start, end = _parse_day(batch_start, "批次开始"), _parse_day(batch_end, "批次结束")
            if start > end:
                raise PartitionError(f"批次开始 {start.isoformat()} 晚于批次结束 {end.isoformat()}，批次边界不合法")
        elif latest is not None:
            start, end = _parse_day(latest["批次开始"], "批次开始"), _parse_day(latest["批次结束"], "批次结束")
        else:
            end = date.today()
            start = end - timedelta(days=6)

        summary = self.summary_for(config_code)
        manifest = self.manifest_rows(config_code)
        entries = {
            "列表": dict(summary),
            "导出": dict(summary),
            "归档": {**summary, "清单行数": len(manifest)},
        }
        consistent = (
            entries["列表"] == entries["导出"]
            and entries["归档"]["分区快照编号"] == entries["列表"]["分区快照编号"]
        )
        lineage = {
            mode: {**self.compute_lineage(entry, start, end, mode)}
            for mode in VERSION_MODES
        }
        for item in lineage.values():
            item["区段数"] = len(item["区段"])
        return {
            "配置编号": config_code,
            "批次开始": start.isoformat(),
            "批次结束": end.isoformat(),
            "入口口径": entries,
            "口径一致": consistent,
            "血缘对比": lineage,
            "历史区段保存天数核对": self._retention_check(config_code),
            "配置冲突": self.conflict_note(config_code),
        }

    def _retention_check(self, config_code: str) -> dict[str, Any]:
        """核对历史区段保存天数：同一区段在多份快照中出现时，保存天数必须保持首落值。"""
        first_seen: dict[str, int] = {}
        latest_seen: dict[str, int] = {}
        for snapshot in self.snapshots_for(config_code):
            for seg in snapshot.get("区段", []):
                label = str(seg.get("区段"))
                first_seen.setdefault(label, parse_retention(seg.get("保存天数")))
                latest_seen[label] = parse_retention(seg.get("保存天数"))
        detail = [
            {"区段": label, "原保存天数": first_seen[label], "当前保存天数": latest_seen[label], "未变": first_seen[label] == latest_seen[label]}
            for label in first_seen
        ]
        return {"未变": all(item["未变"] for item in detail), "区段": detail}


partition_service = PartitionService()
