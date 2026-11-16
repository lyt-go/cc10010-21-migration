"""分区检测规则的日期键口径、兼容标记与缺失分区计算。

历史规则只保存自由文本表名和期望分区数；新规则额外记录日期键（date key）、
分区字段（partition field）和历史分区标识。两种口径同时存在时，兼容标记决定
缺失分区清单怎么算：

* ``数量口径``：沿用旧规则语义，缺失数 = 期望分区数 - 实际分区数。
* ``日期键口径``：按日期键窗口展开应存在的分区值，与实际分区值比对。
* ``双口径等价``：旧期望分区数刚好等于日期键窗口天数，两种算法结果一致。
* ``待确认``：旧规则连日期键/分区字段都无法补齐，只能先沿用数量口径。

本模块只依赖标准库，迁移脚本和运行中的服务共用同一套常量与计算逻辑，
避免示例数据、迁移脚本、部署环境读到不同的日期键。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

# 单一日期键事实来源：示例数据、迁移脚本、接口、元数据存储都从这里读取。
DATE_KEY = "dt"
PARTITION_FIELD = "dt"
SNAPSHOT_DATE_KEY = "2026-11-16"
WINDOW_DAYS = 7

COMPAT_COUNT = "数量口径"
COMPAT_DATE_KEY = "日期键口径"
COMPAT_EQUIVALENT = "双口径等价"
COMPAT_PENDING = "待确认"

LEGACY_SOURCE = "历史分区检测规则"
MANUAL_SOURCE = "分区检测登记"

INTEGRITY_COMPLETE = "完整"
INTEGRITY_MISSING = "缺失"
INTEGRITY_PENDING = "待确认"
INTEGRITY_UNCHECKED = "未检测"

INTEGRITY_RANK = {
    INTEGRITY_UNCHECKED: 0,
    INTEGRITY_COMPLETE: 1,
    INTEGRITY_PENDING: 2,
    INTEGRITY_MISSING: 3,
}


def text(value: Any) -> str:
    """把自由文本字段归一化成去空白字符串，None 视为空。"""
    return "" if value is None else str(value).strip()


def to_int(value: Any, default: int = 0) -> int:
    """兼容旧库里的数字字符串；无法解析时退回默认值而不是抛错。"""
    if isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_date(value: Any) -> date | None:
    """解析日期键值，支持 ``YYYY-MM-DD``、``YYYYMMDD`` 和带时间戳的写法。"""
    raw = text(value)
    if not raw:
        return None
    raw = raw.split("T", 1)[0].split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def date_key_values(end_date_key: Any, window_days: int) -> list[str]:
    """按日期键窗口展开应存在的分区值，升序返回。"""
    end = parse_date(end_date_key)
    if end is None:
        return []
    days = max(to_int(window_days, WINDOW_DAYS), 1)
    return [(end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(payload: Any) -> str:
    """内容指纹：同一条历史规则重复迁移时用它判断是否已生成过版本。"""
    return hashlib.sha1(canonical_json(payload).encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_legacy_compat(
    legacy_rule: dict[str, Any],
    metastore_by_table: dict[str, dict[str, Any]],
    current_date_rules: Sequence[dict[str, Any]],
    *,
    default_window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    """根据旧规则与同表的新日期键规则，决定迁移后的兼容标记。

    新日期键规则代表目标语义：如果旧期望分区数刚好等于新规则的日期键窗口，
    两种口径等价；否则视为冲突，沿用旧数量口径并保留人工确认标记。
    """
    table_name = text(legacy_rule.get("数据表名") or legacy_rule.get("table_name"))
    metastore = metastore_by_table.get(table_name, {})
    date_key = text(
        legacy_rule.get("日期键")
        or legacy_rule.get("date_key")
        or metastore.get("日期键")
        or metastore.get("date_key")
    )
    partition_field = text(
        legacy_rule.get("分区字段")
        or legacy_rule.get("partition_field")
        or metastore.get("分区字段")
        or metastore.get("partition_field")
        or date_key
    )
    current_rules = [
        rule
        for rule in current_date_rules
        if text(rule.get("数据表名") or rule.get("table_name")) == table_name
    ]
    reference_window = to_int(
        (
            current_rules[0].get("检查窗口天数")
            or current_rules[0].get("window_days")
            if current_rules
            else 0
        )
        or legacy_rule.get("检查窗口天数")
        or legacy_rule.get("window_days")
        or metastore.get("检查窗口天数")
        or default_window_days,
        default_window_days,
    )
    window_days = max(reference_window, 1)
    expected_count = to_int(
        legacy_rule.get("期望分区数") or legacy_rule.get("expected_partitions"),
        0,
    )

    if not date_key or not partition_field:
        return {
            "日期键": "",
            "分区字段": "",
            "兼容标记": COMPAT_PENDING,
            "检查窗口天数": window_days,
            "口径冲突": False,
            "口径说明": "旧规则缺少日期键或分区字段，沿用数量口径并标记待确认",
            "同表日期键规则": [text(rule.get("规则编号")) for rule in current_rules],
        }

    if expected_count == window_days:
        return {
            "日期键": date_key,
            "分区字段": partition_field,
            "兼容标记": COMPAT_EQUIVALENT,
            "检查窗口天数": window_days,
            "口径冲突": False,
            "口径说明": (
                f"旧期望分区数 {expected_count} 与日期键窗口 {window_days} 天一致，"
                "按日期键口径回填缺失分区"
            ),
            "同表日期键规则": [text(rule.get("规则编号")) for rule in current_rules],
        }

    conflict_note = (
        f"旧期望分区数 {expected_count} 与日期键窗口 {window_days} 天冲突，沿用数量口径"
    )
    if current_rules:
        conflict_note += "；同表已存在日期键规则，待人工确认后再切换口径"
    else:
        conflict_note += "；同表暂无日期键规则，按旧数量语义继续计算"
    return {
        "日期键": date_key,
        "分区字段": partition_field,
        "兼容标记": COMPAT_COUNT,
        "检查窗口天数": window_days,
        # 只有同表确实存在新日期键规则时才记为口径冲突。
        "口径冲突": bool(current_rules),
        "口径说明": conflict_note,
        "同表日期键规则": [text(rule.get("规则编号")) for rule in current_rules],
    }


def build_legacy_snapshot(
    legacy_rule: dict[str, Any],
    *,
    metastore_by_table: dict[str, dict[str, Any]],
    current_date_rules: Sequence[dict[str, Any]],
    snapshot_date_key: str = SNAPSHOT_DATE_KEY,
    source_order: int = 0,
) -> dict[str, Any]:
    """把一条历史数量规则转换成带日期键的分区规则数据快照。

    规则编号原样保留（``规则编号`` 与 ``来源规则编号`` 都指向旧编号），
    同时记录规则来源，后续缺失分区清单与完整性结论可以追溯到原规则。
    """
    rule_code = text(legacy_rule.get("规则编号") or legacy_rule.get("rule_code"))
    table_name = text(legacy_rule.get("数据表名") or legacy_rule.get("table_name"))
    expected = to_int(legacy_rule.get("期望分区数") or legacy_rule.get("expected_partitions"), 0)
    compat = resolve_legacy_compat(
        legacy_rule,
        metastore_by_table,
        current_date_rules,
    )
    actual_values = [
        text(value)
        for value in (legacy_rule.get("实际分区") or legacy_rule.get("actual_partitions") or [])
        if text(value)
    ]
    actual_count = legacy_rule.get("实际分区数", legacy_rule.get("actual_partitions_count"))
    if actual_count is None and actual_values:
        actual_count = len(actual_values)
    actual_count = None if actual_count is None else to_int(actual_count, 0)

    snapshot = {
        "id": source_order,
        "规则编号": rule_code,
        "来源规则编号": rule_code,
        "规则来源": LEGACY_SOURCE,
        "数据表名": table_name,
        "期望分区数": expected,
        "日期键": compat["日期键"],
        "分区字段": compat["分区字段"],
        "历史分区标识": text(
            legacy_rule.get("历史分区标识")
            or legacy_rule.get("legacy_partition_id")
            or f"LEGACY-{rule_code}"
        ),
        "兼容标记": compat["兼容标记"],
        "口径说明": compat["口径说明"],
        "口径冲突": compat["口径冲突"],
        "检查窗口天数": compat["检查窗口天数"],
        "快照日期键": snapshot_date_key,
        "快照版本": 1,
        "当前版本": True,
        "实际分区": actual_values,
        "实际分区数": actual_count,
        "状态": text(legacy_rule.get("状态") or legacy_rule.get("status") or "草稿"),
        "同表日期键规则": compat["同表日期键规则"],
        # 英文别名：便于迁移产物和外部部署直接消费。
        "rule_code": rule_code,
        "source_rule_code": rule_code,
        "rule_source": LEGACY_SOURCE,
        "table_name": table_name,
        "expected_partitions": expected,
        "date_key": compat["日期键"],
        "partition_field": compat["分区字段"],
        "legacy_partition_id": text(
            legacy_rule.get("历史分区标识")
            or legacy_rule.get("legacy_partition_id")
            or f"LEGACY-{rule_code}"
        ),
        "compat_mode": compat["兼容标记"],
        "window_days": compat["检查窗口天数"],
        "snapshot_date_key": snapshot_date_key,
        "snapshot_version": 1,
    }
    snapshot["内容指纹"] = fingerprint(
        {
            "规则编号": rule_code,
            "数据表名": table_name,
            "期望分区数": expected,
            "日期键": compat["日期键"],
            "分区字段": compat["分区字段"],
            "兼容标记": compat["兼容标记"],
            "检查窗口天数": compat["检查窗口天数"],
            "快照日期键": snapshot_date_key,
            "历史分区标识": snapshot["历史分区标识"],
            "实际分区": actual_values,
            "实际分区数": actual_count,
        }
    )
    return snapshot


def build_manual_rule(
    rule: dict[str, Any],
    *,
    source_order: int = 0,
    snapshot_date_key: str = SNAPSHOT_DATE_KEY,
) -> dict[str, Any]:
    """登记口径的规则：本身就带日期键，不需要从旧数量规则迁移。"""
    rule_code = text(rule.get("规则编号") or rule.get("rule_code"))
    table_name = text(rule.get("数据表名") or rule.get("table_name"))
    date_key = text(rule.get("日期键") or rule.get("date_key") or DATE_KEY)
    partition_field = text(rule.get("分区字段") or rule.get("partition_field") or PARTITION_FIELD)
    window_days = max(
        to_int(rule.get("检查窗口天数") or rule.get("window_days"), WINDOW_DAYS),
        1,
    )
    expected = to_int(
        rule.get("期望分区数") or rule.get("expected_partitions"),
        window_days,
    )
    actual_values = [
        text(value)
        for value in (rule.get("实际分区") or rule.get("actual_partitions") or [])
        if text(value)
    ]
    actual_count = rule.get("实际分区数", rule.get("actual_partitions_count"))
    if actual_count is None and actual_values:
        actual_count = len(actual_values)
    actual_count = None if actual_count is None else to_int(actual_count, 0)

    return {
        "id": source_order,
        "规则编号": rule_code,
        "来源规则编号": rule_code,
        "规则来源": MANUAL_SOURCE,
        "数据表名": table_name,
        "期望分区数": expected,
        "日期键": date_key,
        "分区字段": partition_field,
        "历史分区标识": text(rule.get("历史分区标识") or rule.get("legacy_partition_id")),
        "兼容标记": COMPAT_DATE_KEY,
        "口径说明": "登记时已带日期键，按日期键口径计算缺失分区",
        "口径冲突": False,
        "检查窗口天数": window_days,
        "快照日期键": snapshot_date_key,
        "快照版本": 1,
        "当前版本": True,
        "实际分区": actual_values,
        "实际分区数": actual_count,
        "状态": text(rule.get("状态") or rule.get("status") or "已启用"),
        "同表日期键规则": [],
        "rule_code": rule_code,
        "source_rule_code": rule_code,
        "rule_source": MANUAL_SOURCE,
        "table_name": table_name,
        "expected_partitions": expected,
        "date_key": date_key,
        "partition_field": partition_field,
        "legacy_partition_id": text(rule.get("历史分区标识") or rule.get("legacy_partition_id")),
        "compat_mode": COMPAT_DATE_KEY,
        "window_days": window_days,
        "snapshot_date_key": snapshot_date_key,
        "snapshot_version": 1,
    }


def _missing_entry(
    *,
    value: str,
    date_key: str,
    partition_field: str,
    rule_code: str,
    source_rule_code: str,
    compat_mode: str,
) -> dict[str, Any]:
    return {
        "分区值": value,
        "日期键": date_key,
        "分区字段": partition_field,
        "来源规则编号": source_rule_code,
        "规则编号": rule_code,
        "兼容标记": compat_mode,
        "partition_value": value,
        "date_key": date_key,
        "partition_field": partition_field,
        "source_rule_code": source_rule_code,
        "compat_mode": compat_mode,
    }


def evaluate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """按规则上的兼容标记计算缺失分区清单与完整性结论。

    兼容标记不是装饰字段：``数量口径`` 和 ``日期键口径`` 会走到两套不同的
    计算分支，因此迁移时选错标记会直接改变缺失分区数量。
    """
    rule_code = text(rule.get("规则编号") or rule.get("rule_code"))
    source_rule_code = text(rule.get("来源规则编号") or rule_code)
    rule_source = text(rule.get("规则来源") or rule.get("rule_source"))
    table_name = text(rule.get("数据表名") or rule.get("table_name"))
    date_key = text(rule.get("日期键") or rule.get("date_key"))
    partition_field = text(rule.get("分区字段") or rule.get("partition_field"))
    compat_mode = text(rule.get("兼容标记") or rule.get("compat_mode"))
    expected = to_int(rule.get("期望分区数") or rule.get("expected_partitions"), 0)
    window_days = max(
        to_int(rule.get("检查窗口天数") or rule.get("window_days"), WINDOW_DAYS),
        1,
    )
    snapshot_date_key = text(rule.get("快照日期键") or rule.get("snapshot_date_key"))
    actual_values = [
        text(value)
        for value in (rule.get("实际分区") or rule.get("actual_partitions") or [])
        if text(value)
    ]
    actual_count = rule.get("实际分区数", rule.get("actual_partitions_count"))
    if actual_count is None and actual_values:
        actual_count = len(actual_values)
    actual_count = None if actual_count is None else to_int(actual_count, 0)

    missing: list[dict[str, Any]] = []
    mode_note = ""
    if compat_mode in (COMPAT_DATE_KEY, COMPAT_EQUIVALENT) and date_key:
        expected_keys = date_key_values(snapshot_date_key, window_days)
        if actual_values:
            present = set(actual_values)
            missing_values = [value for value in expected_keys if value not in present]
        elif actual_count is not None:
            missing_count = max(len(expected_keys) - actual_count, 0)
            missing_values = expected_keys[len(expected_keys) - missing_count:] if missing_count else []
        else:
            missing_values = []
        missing = [
            _missing_entry(
                value=value,
                date_key=date_key,
                partition_field=partition_field or date_key,
                rule_code=rule_code,
                source_rule_code=source_rule_code,
                compat_mode=compat_mode,
            )
            for value in missing_values
        ]
        mode_note = f"按日期键 {date_key} 展开 {len(expected_keys)} 个应存在分区"
    else:
        if actual_count is not None:
            missing_count = max(expected - actual_count, 0)
            missing = [
                _missing_entry(
                    value=f"缺失分区{index + 1:02d}",
                    date_key=date_key,
                    partition_field=partition_field,
                    rule_code=rule_code,
                    source_rule_code=source_rule_code,
                    compat_mode=compat_mode or COMPAT_COUNT,
                )
                for index in range(missing_count)
            ]
        mode_note = f"按数量口径比较期望 {expected} 与实际 {actual_count}"

    if actual_count is None and not actual_values:
        integrity = INTEGRITY_PENDING
        mode_note += "；缺少实际分区数据，结论待确认"
    elif missing:
        integrity = INTEGRITY_MISSING
    else:
        integrity = INTEGRITY_COMPLETE

    if compat_mode == COMPAT_PENDING:
        mode_note += "；兼容标记为待确认，缺失分区仍按数量口径保留"

    return {
        "规则编号": rule_code,
        "来源规则编号": source_rule_code,
        "规则来源": rule_source,
        "数据表名": table_name,
        "日期键": date_key,
        "分区字段": partition_field,
        "兼容标记": compat_mode or COMPAT_COUNT,
        "期望分区数": expected,
        "实际分区数": actual_count,
        "检查窗口天数": window_days,
        "快照日期键": snapshot_date_key,
        "快照版本": to_int(rule.get("快照版本") or rule.get("snapshot_version"), 1),
        "口径冲突": bool(rule.get("口径冲突") or rule.get("conflict_flag")),
        "缺失分区清单": missing,
        "缺失分区数": len(missing),
        "分区完整性结论": integrity,
        "口径说明": mode_note,
        "计算批次": snapshot_date_key,
        # 英文别名
        "rule_code": rule_code,
        "source_rule_code": source_rule_code,
        "rule_source": rule_source,
        "table_name": table_name,
        "date_key": date_key,
        "partition_field": partition_field,
        "compat_mode": compat_mode or COMPAT_COUNT,
        "expected_partitions": expected,
        "actual_partitions_count": actual_count,
        "conflict_flag": bool(rule.get("口径冲突") or rule.get("conflict_flag")),
        "missing_partitions": missing,
        "missing_partition_count": len(missing),
        "integrity": integrity,
        "snapshot_version": to_int(rule.get("快照版本") or rule.get("snapshot_version"), 1),
    }


def build_conclusions(rules: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """为当前生效的规则生成结论；同一规则编号只保留一个结论版本。"""
    latest: dict[str, dict[str, Any]] = {}
    for rule in rules:
        rule_code = text(rule.get("规则编号") or rule.get("rule_code"))
        if not rule_code:
            continue
        version = to_int(rule.get("快照版本") or rule.get("snapshot_version"), 1)
        existing = latest.get(rule_code)
        if existing is None or version >= to_int(
            existing.get("快照版本") or existing.get("snapshot_version"), 1
        ):
            latest[rule_code] = rule

    conclusions: list[dict[str, Any]] = []
    for index, rule_code in enumerate(sorted(latest), start=1):
        conclusion = evaluate_rule(latest[rule_code])
        conclusion["id"] = index
        conclusion["结论版本"] = conclusion["快照版本"]
        conclusion["conclusion_version"] = conclusion["快照版本"]
        conclusion["状态"] = "已生成"
        conclusion["pending"] = conclusion["分区完整性结论"] == INTEGRITY_PENDING
        conclusion["abnormal"] = conclusion["分区完整性结论"] == INTEGRITY_MISSING
        conclusions.append(conclusion)
    return conclusions


def severity(integrity: str) -> int:
    return INTEGRITY_RANK.get(integrity, 0)
