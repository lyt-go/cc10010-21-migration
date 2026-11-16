"""分区检测结论聚合：跨模块（分区检测 ↔ 元数据存储）共用同一份计算口径。

只做纯函数计算，不碰内存存储，避免 ``store`` 与 ``services`` 循环依赖。
"""
from __future__ import annotations

from typing import Any, Iterable

from app import partition_rules as pr


def evaluated_rules(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return pr.build_conclusions(rows)


def aggregate_by_table(
    conclusions: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """按数据表汇总分区检测结论，供元数据存储清单同步展示。"""
    grouped: dict[str, dict[str, Any]] = {}
    for conclusion in conclusions:
        table_name = pr.text(conclusion.get("数据表名"))
        if not table_name:
            continue
        entry = grouped.setdefault(
            table_name,
            {
                "数据表名": table_name,
                "日期键": pr.text(conclusion.get("日期键")),
                "分区字段": pr.text(conclusion.get("分区字段")),
                "分区规则编号": [],
                "规则来源": [],
                "来源规则编号": [],
                "缺失分区清单": [],
                "缺失分区数": 0,
                "分区完整性结论": pr.INTEGRITY_COMPLETE,
                "检测批次": conclusion.get("计算批次") or pr.SNAPSHOT_DATE_KEY,
                "口径冲突规则": [],
                "兼容标记": [],
                "结论版本": 0,
            },
        )
        entry["分区规则编号"].append(conclusion.get("规则编号"))
        entry["规则来源"].append(conclusion.get("规则来源"))
        entry["来源规则编号"].append(conclusion.get("来源规则编号"))
        entry["兼容标记"].append(conclusion.get("兼容标记"))
        if conclusion.get("口径冲突") or conclusion.get("兼容标记") == pr.COMPAT_PENDING:
            entry["口径冲突规则"].append(conclusion.get("规则编号"))
        entry["缺失分区数"] += pr.to_int(conclusion.get("缺失分区数"), 0)
        entry["缺失分区清单"].extend(conclusion.get("缺失分区清单") or [])
        entry["结论版本"] = max(
            entry["结论版本"],
            pr.to_int(conclusion.get("结论版本"), 1),
        )
        if pr.severity(conclusion.get("分区完整性结论", "")) >= pr.severity(
            entry["分区完整性结论"]
        ):
            entry["分区完整性结论"] = conclusion.get("分区完整性结论")
        if not entry["日期键"]:
            entry["日期键"] = pr.text(conclusion.get("日期键"))
        if not entry["分区字段"]:
            entry["分区字段"] = pr.text(conclusion.get("分区字段"))

    for entry in grouped.values():
        entry["分区规则编号"] = sorted(
            {code for code in entry["分区规则编号"] if code}
        )
        entry["规则来源"] = sorted({source for source in entry["规则来源"] if source})
        entry["来源规则编号"] = sorted(
            {code for code in entry["来源规则编号"] if code}
        )
        entry["兼容标记"] = sorted({mode for mode in entry["兼容标记"] if mode})
        # 同一张表的所有规则必须读同一个日期键/分区字段，否则给出显式告警。
        entry["日期键一致"] = len(
            {
                pr.text(item.get("日期键"))
                for item in entry["缺失分区清单"]
                if pr.text(item.get("日期键"))
            }
        ) <= 1
        entry["分区完整性结论"] = entry["分区完整性结论"] or pr.INTEGRITY_UNCHECKED
    return grouped


def apply_to_metastore(
    metastore_rows: list[dict[str, Any]],
    conclusions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把分区检测结论同步到元数据存储清单，并核对日期键/分区字段。"""
    grouped = aggregate_by_table(conclusions)
    for row in metastore_rows:
        table_name = pr.text(row.get("表名") or row.get("table_name"))
        summary = grouped.get(table_name)
        if summary is None:
            row.setdefault("日期键", pr.DATE_KEY)
            row.setdefault("分区字段", pr.PARTITION_FIELD)
            row["分区规则编号"] = []
            row["缺失分区数"] = 0
            row["缺失分区清单"] = []
            row["分区完整性结论"] = pr.INTEGRITY_UNCHECKED
            row["检测批次"] = pr.SNAPSHOT_DATE_KEY
            row["分区字段一致"] = pr.text(row.get("分区字段")) == pr.text(
                row.get("日期键")
            )
            continue
        if not pr.text(row.get("日期键")):
            row["日期键"] = summary["日期键"] or pr.DATE_KEY
        if not pr.text(row.get("分区字段")):
            row["分区字段"] = summary["分区字段"] or summary["日期键"] or pr.PARTITION_FIELD
        else:
            # 快照携带的分区字段是检测口径的事实来源，相邻业务面跟随更新。
            row["分区字段"] = summary["分区字段"] or row["分区字段"]
        row["分区规则编号"] = summary["分区规则编号"]
        row["规则来源"] = summary["规则来源"]
        row["来源规则编号"] = summary["来源规则编号"]
        row["兼容标记"] = summary["兼容标记"]
        row["缺失分区数"] = summary["缺失分区数"]
        row["缺失分区清单"] = summary["缺失分区清单"]
        row["分区完整性结论"] = summary["分区完整性结论"]
        row["检测批次"] = summary["检测批次"]
        row["结论版本"] = summary["结论版本"]
        row["口径冲突规则"] = summary["口径冲突规则"]
        row["分区字段一致"] = pr.text(row.get("分区字段")) in {
            pr.text(summary["分区字段"]),
            pr.text(summary["日期键"]),
        }
    return metastore_rows


def consistency_report(
    partition_rows: Iterable[dict[str, Any]],
    metastore_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """跨模块自检：元数据存储与分区检测是否读同一日期键和分区字段。"""
    conclusions = pr.build_conclusions(partition_rows)
    grouped = aggregate_by_table(conclusions)
    metastore = {
        pr.text(row.get("表名") or row.get("table_name")): row
        for row in metastore_rows
    }
    mismatches: list[dict[str, Any]] = []
    missing_tables: list[str] = []
    checked: list[dict[str, Any]] = []
    for table_name, summary in grouped.items():
        meta = metastore.get(table_name)
        if meta is None:
            # 未登记到元数据存储的表没东西可同步，单独列出而不是算冲突。
            missing_tables.append(table_name)
            continue
        meta_date_key = pr.text(meta.get("日期键"))
        meta_field = pr.text(meta.get("分区字段"))
        rule_date_key = summary["日期键"]
        rule_field = summary["分区字段"]
        ok = (
            (not rule_date_key or meta_date_key == rule_date_key)
            and (not rule_field or meta_field in {rule_field, rule_date_key})
        )
        item = {
            "数据表名": table_name,
            "元数据日期键": meta_date_key,
            "分区检测日期键": rule_date_key,
            "元数据分区字段": meta_field,
            "分区检测分区字段": rule_field,
            "一致": ok,
        }
        checked.append(item)
        if not ok:
            mismatches.append({"数据表名": table_name, "原因": "日期键或分区字段不一致", **item})
    return {
        "ok": not mismatches,
        "checked": checked,
        "mismatches": mismatches,
        "missing_in_metastore": missing_tables,
        "date_key": pr.DATE_KEY,
        "partition_field": pr.PARTITION_FIELD,
        "检测批次": pr.SNAPSHOT_DATE_KEY,
    }
