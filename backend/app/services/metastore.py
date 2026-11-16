"""元数据存储业务规则：状态流转、字段校验、筛选口径与分区字段同步都收在这里。

元数据存储清单会读取分区检测的 MISSING/完整性结论，并跟随迁移快照里的
``日期键`` 同步 ``分区字段``；相邻业务面因此和分区检测读同一套日期键。
"""
from __future__ import annotations

from typing import Any

from app import partition_rules as pr
from app.partition_analysis import consistency_report, evaluated_rules
from app.store import store

MODULE = "metastore"
REQUIRED_FIELDS = ["表编号", "表名", "所属数仓层"]
STATUS_ORDER = ["草稿", "元数据评审中", "已确认", "已过期"]
ACTION_RULES = {"提交元数据评审": "元数据评审中", "确认元数据": "已确认", "标记过期": "已过期"}
NEGATIVE_ACTIONS = ["标记过期"]


class MetastoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        store.refresh_partition_conclusions()
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("表编号", ""))
                or keyword in str(row.get("表名", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        store.refresh_partition_conclusions()
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        date_key = str(values.get("日期键") or pr.DATE_KEY).strip() or pr.DATE_KEY
        entry["日期键"] = date_key
        entry["分区字段"] = str(values.get("分区字段") or date_key).strip() or date_key
        for field in ["字段数量", "主键字段", "数据量级", "最近更新时间"]:
            if field in values:
                entry[field] = values[field]
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        store.refresh_partition_conclusions()
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"元数据表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于元数据存储可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.refresh_partition_conclusions()
        return entry, f"元数据表已{action}"

    def conclusions(self) -> list[dict[str, Any]]:
        """元数据存储视角的分区检测结论：来源仍保留原规则编号。"""
        store.refresh_partition_conclusions()
        return evaluated_rules(store.rows("partitioncheck"))

    def consistency(self) -> dict[str, Any]:
        store.refresh_partition_conclusions()
        return consistency_report(store.rows("partitioncheck"), store.rows(MODULE))
