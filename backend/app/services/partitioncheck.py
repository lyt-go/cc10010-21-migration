"""分区检测业务规则：状态流转、字段校验、日期键兼容与筛选口径都收在这里。

规则现在可以带日期键（``日期键``/``分区字段``/``历史分区标识``）；旧的数量
规则仍然可读、可流转，带 :data:`app.partition_rules.COMPAT_PENDING` 标记的
记录按数量口径计算，不会被检测任务丢弃。
"""
from __future__ import annotations

from typing import Any

from app import partition_rules as pr
from app.partition_analysis import consistency_report, evaluated_rules
from app.store import store

MODULE = "partitioncheck"
REQUIRED_FIELDS = ["规则编号", "数据表名", "期望分区数"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已暂停"]
ACTION_RULES = {"提交规则评审": "规则评审中", "启用规则": "已启用", "暂停规则": "已暂停"}
NEGATIVE_ACTIONS = []
COMPAT_MODES = [
    pr.COMPAT_COUNT,
    pr.COMPAT_DATE_KEY,
    pr.COMPAT_EQUIVALENT,
    pr.COMPAT_PENDING,
]


class PartitioncheckService:
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
                if keyword in str(row.get("规则编号", ""))
                or keyword in str(row.get("数据表名", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        store.refresh_partition_conclusions()
        return store.find(MODULE, entry_id)

    def _normalize_rule_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """兼容旧调用方：没给日期键时使用统一常量，而不是各模块各写一份。"""
        normalized = dict(values)
        normalized.setdefault("日期键", pr.DATE_KEY)
        normalized.setdefault("分区字段", normalized.get("日期键") or pr.PARTITION_FIELD)
        normalized.setdefault("检查窗口天数", pr.WINDOW_DAYS)
        if str(normalized.get("日期键") or "").strip() == "":
            normalized["日期键"] = pr.DATE_KEY
        if str(normalized.get("分区字段") or "").strip() == "":
            normalized["分区字段"] = normalized["日期键"]
        return normalized

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        normalized = self._normalize_rule_values(values)
        rows = store.rows(MODULE)
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rule = pr.build_manual_rule(
            {
                **normalized,
                "规则编号": normalized.get("规则编号"),
                "数据表名": normalized.get("数据表名"),
                "期望分区数": normalized.get("期望分区数"),
                "状态": STATUS_ORDER[0],
            },
            source_order=entry_id,
        )
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
        }
        entry.update(rule)
        entry["id"] = entry_id
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 保持登记时的可选展示字段。
        for field in [
            "分区延迟容忍",
            "缺失分区告警",
            "检测间隔分钟",
            "自动补齐开关",
            "补齐优先级",
        ]:
            if field in values:
                entry[field] = values[field]
        if normalized.get("历史分区标识"):
            entry["历史分区标识"] = normalized["历史分区标识"]
        rows.append(entry)
        store.refresh_partition_conclusions()
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检测规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分区检测可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.refresh_partition_conclusions()
        return entry, f"检测规则已{action}"

    def conclusions(self) -> list[dict[str, Any]]:
        store.refresh_partition_conclusions()
        return evaluated_rules(store.rows(MODULE))

    def consistency(self) -> dict[str, Any]:
        store.refresh_partition_conclusions()
        return consistency_report(store.rows(MODULE), store.rows("metastore"))

    def compatibility_summary(self) -> dict[str, Any]:
        """迁移进度与兼容标记分布：旧规则是否都已回填日期键一目了然。"""
        rows = store.rows(MODULE)
        counts = {mode: 0 for mode in COMPAT_MODES}
        pending_rules: list[str] = []
        conflict_rules: list[str] = []
        legacy_sources: list[str] = []
        for row in rows:
            mode = str(row.get("兼容标记") or pr.COMPAT_COUNT)
            counts[mode] = counts.get(mode, 0) + 1
            if mode == pr.COMPAT_PENDING:
                pending_rules.append(str(row.get("规则编号")))
            if row.get("口径冲突"):
                conflict_rules.append(str(row.get("规则编号")))
            if row.get("规则来源") == pr.LEGACY_SOURCE:
                legacy_sources.append(str(row.get("规则编号")))
        return {
            "date_key": pr.DATE_KEY,
            "partition_field": pr.PARTITION_FIELD,
            "source": store.partition_source(),
            "compat_counts": counts,
            "pending_rules": pending_rules,
            "conflict_rules": conflict_rules,
            "legacy_rules": legacy_sources,
            "total": len(rows),
        }
