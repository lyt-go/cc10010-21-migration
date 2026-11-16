"""分区检测示例数据：历史数量规则 → 带日期键的规则数据快照。

示例数据、迁移脚本和部署环境都从这里读取 ``LEGACY_RULES`` / ``DATE_KEY_RULES`` /
``METASTORE_TABLES``，日期键统一由 :mod:`app.partition_rules` 提供，避免三处口径漂移。
"""
from __future__ import annotations

from typing import Any

from app import partition_rules as pr

# 历史数量规则：只有自由文本表名和期望分区数，没有日期键/分区字段。
LEGACY_RULES: list[dict[str, Any]] = [
    {
        "规则编号": "PART-0001",
        "数据表名": "dwd_order_daily",
        "期望分区数": 7,
        "实际分区": [
            "2026-11-10",
            "2026-11-11",
            "2026-11-12",
            "2026-11-13",
            "2026-11-14",
            "2026-11-15",
        ],
        "状态": "已启用",
        "分区延迟容忍": "30分钟",
        "缺失分区告警": "是",
        "检测间隔分钟": "60",
        "自动补齐开关": "开启",
        "补齐优先级": "高",
    },
    {
        "规则编号": "PART-0002",
        "数据表名": "dws_user_profile",
        "期望分区数": 30,
        "实际分区数": 25,
        "状态": "规则评审中",
        "分区延迟容忍": "2小时",
        "缺失分区告警": "是",
        "检测间隔分钟": "120",
        "自动补齐开关": "关闭",
        "补齐优先级": "中",
    },
    {
        "规则编号": "PART-0003",
        "数据表名": "dim_calendar",
        "期望分区数": 7,
        "实际分区数": 7,
        "状态": "草稿",
        "分区延迟容忍": "1天",
        "缺失分区告警": "否",
        "检测间隔分钟": "360",
        "自动补齐开关": "关闭",
        "补齐优先级": "低",
    },
    {
        "规则编号": "PART-0004",
        "数据表名": "dws_user_profile",
        "期望分区数": 7,
        "实际分区": [
            "2026-11-10",
            "2026-11-11",
            "2026-11-12",
            "2026-11-13",
            "2026-11-14",
            "2026-11-15",
        ],
        "状态": "已启用",
        "分区延迟容忍": "1小时",
        "缺失分区告警": "是",
        "检测间隔分钟": "60",
        "自动补齐开关": "开启",
        "补齐优先级": "中",
    },
]

# 已经带日期键的新规则：与历史数量规则共存，也是冲突判定的参照。
DATE_KEY_RULES: list[dict[str, Any]] = [
    {
        "规则编号": "PART-0101",
        "数据表名": "dws_user_profile",
        "日期键": pr.DATE_KEY,
        "分区字段": pr.PARTITION_FIELD,
        "检查窗口天数": 7,
        "期望分区数": 7,
        "实际分区": [
            "2026-11-10",
            "2026-11-11",
            "2026-11-12",
            "2026-11-13",
            "2026-11-14",
            "2026-11-15",
            "2026-11-16",
        ],
        "状态": "已启用",
        "分区延迟容忍": "1小时",
        "缺失分区告警": "是",
        "检测间隔分钟": "60",
        "自动补齐开关": "开启",
        "补齐优先级": "高",
    },
]

# 元数据存储里的表：日期键和分区字段必须与分区检测读到的值一致。
METASTORE_TABLES: list[dict[str, Any]] = [
    {
        "id": 1,
        "表编号": "META-0001",
        "表名": "dwd_order_daily",
        "所属数仓层": "DWD",
        "字段数量": 18,
        "日期键": pr.DATE_KEY,
        "分区字段": pr.PARTITION_FIELD,
        "主键字段": "order_id",
        "数据量级": "1.2亿",
        "最近更新时间": "2026-11-16",
        "status": "已确认",
        "pending": False,
        "abnormal": False,
    },
    {
        "id": 2,
        "表编号": "META-0002",
        "表名": "dws_user_profile",
        "所属数仓层": "DWS",
        "字段数量": 26,
        "日期键": pr.DATE_KEY,
        "分区字段": pr.PARTITION_FIELD,
        "主键字段": "user_id",
        "数据量级": "8600万",
        "最近更新时间": "2026-11-16",
        "status": "已确认",
        "pending": False,
        "abnormal": False,
    },
    {
        "id": 3,
        "表编号": "META-0003",
        "表名": "dim_region",
        "所属数仓层": "DIM",
        "字段数量": 12,
        "日期键": pr.DATE_KEY,
        "分区字段": pr.PARTITION_FIELD,
        "主键字段": "region_code",
        "数据量级": "3200",
        "最近更新时间": "2026-11-15",
        "status": "元数据评审中",
        "pending": True,
        "abnormal": False,
    },
]


def _metastore_by_table() -> dict[str, dict[str, Any]]:
    return {pr.text(row["表名"]): row for row in METASTORE_TABLES}


def build_snapshots() -> list[dict[str, Any]]:
    """按与迁移脚本相同的顺序生成快照：先新日期键规则，再历史数量规则。"""
    snapshots: list[dict[str, Any]] = []
    current_date_rules = list(DATE_KEY_RULES)
    metastore = _metastore_by_table()
    for index, rule in enumerate(DATE_KEY_RULES, start=1):
        snapshots.append(
            pr.build_manual_rule(rule, source_order=index)
        )
    for offset, legacy in enumerate(LEGACY_RULES, start=1):
        # 与迁移脚本相同的回填顺序：规则自带 > 元数据表 > 平台统一日期键。
        metastore_row = metastore.get(pr.text(legacy["数据表名"]), {})
        enriched = dict(legacy)
        enriched.setdefault("日期键", pr.text(metastore_row.get("日期键")) or pr.DATE_KEY)
        enriched.setdefault(
            "分区字段",
            pr.text(metastore_row.get("分区字段")) or enriched["日期键"],
        )
        snapshots.append(
            pr.build_legacy_snapshot(
                enriched,
                metastore_by_table=metastore,
                current_date_rules=current_date_rules,
                source_order=len(DATE_KEY_RULES) + offset,
            )
        )
    return snapshots


def build_partitioncheck_rows() -> list[dict[str, Any]]:
    """把快照装载成分区检测规则行，保留旧规则的展示字段与状态流转字段。"""
    rows: list[dict[str, Any]] = []
    legacy_by_code = {pr.text(rule["规则编号"]): rule for rule in LEGACY_RULES}
    manual_by_code = {pr.text(rule["规则编号"]): rule for rule in DATE_KEY_RULES}
    for index, snapshot in enumerate(build_snapshots(), start=1):
        rule_code = pr.text(snapshot["规则编号"])
        source = legacy_by_code.get(rule_code) or manual_by_code.get(rule_code) or {}
        row = {
            "id": index,
            "status": pr.text(source.get("状态") or snapshot.get("状态") or "草稿"),
            "pending": False,
            "abnormal": False,
        }
        row.update(
            {
                "规则编号": snapshot["规则编号"],
                "数据表名": snapshot["数据表名"],
                "期望分区数": snapshot["期望分区数"],
                "分区延迟容忍": pr.text(source.get("分区延迟容忍")),
                "缺失分区告警": pr.text(source.get("缺失分区告警")),
                "检测间隔分钟": pr.text(source.get("检测间隔分钟")),
                "自动补齐开关": pr.text(source.get("自动补齐开关")),
                "补齐优先级": pr.text(source.get("补齐优先级")),
                "日期键": snapshot["日期键"],
                "分区字段": snapshot["分区字段"],
                "历史分区标识": snapshot["历史分区标识"],
                "规则来源": snapshot["规则来源"],
                "来源规则编号": snapshot["来源规则编号"],
                "兼容标记": snapshot["兼容标记"],
                "口径说明": snapshot["口径说明"],
                "口径冲突": snapshot["口径冲突"],
                "检查窗口天数": snapshot["检查窗口天数"],
                "快照日期键": snapshot["快照日期键"],
                "快照版本": snapshot["快照版本"],
                "实际分区": snapshot["实际分区"],
                "实际分区数": snapshot["实际分区数"],
            }
        )
        rows.append(row)
    return rows


def build_metastore_rows() -> list[dict[str, Any]]:
    return [dict(row) for row in METASTORE_TABLES]
