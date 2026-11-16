"""分区规则数据访问：内存示例数据与旧 SQLite 数据库共用同一套读取口径。

* 旧数据库里可能只有 ``partition_check_rules`` / ``partitioncheck`` 这种只存
  表名和期望分区数的老表；读取时按 :data:`app.partition_rules.COMPAT_PENDING`
  处理，迁移未完成的记录仍可被检测任务读取。
* 新表 ``partition_rule_snapshot`` 由迁移脚本写入，读取后直接覆盖同编号旧记录。
* 日期键只在 :mod:`app.partition_rules` 里定义一次，示例数据和部署读取不会漂移。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from app import partition_rules as pr
from app.partition_seed import METASTORE_TABLES as SEED_METASTORE_TABLES

LEGACY_TABLES = (
    "partition_check_rule_legacy",
    "partition_check_rules",
    "partition_check_rule",
    "partition_rules",
    "partitioncheck_legacy",
)
CURRENT_TABLES = ("partitioncheck", "partition_check")
SNAPSHOT_TABLES = ("partition_rule_snapshot",)
METASTORE_TABLES = ("metastore", "metadata_store")
PARTITIONCHECK_MODULE = "partitioncheck"
METASTORE_MODULE = "metastore"

RULE_CODE_COLUMNS = ("规则编号", "rule_code", "rule_no", "code")
TABLE_NAME_COLUMNS = ("数据表名", "table_name", "table", "source_table")
EXPECTED_COLUMNS = (
    "期望分区数",
    "expected_partitions",
    "expected_partition_count",
    "partition_count",
    "expected_count",
)
ACTUAL_COUNT_COLUMNS = ("实际分区数", "actual_partitions_count", "actual_count")
ACTUAL_VALUES_COLUMNS = ("实际分区", "actual_partitions", "actual_values")
STATUS_COLUMNS = ("状态", "status")
DATE_KEY_COLUMNS = ("日期键", "date_key")
PARTITION_FIELD_COLUMNS = ("分区字段", "partition_field")
WINDOW_COLUMNS = ("检查窗口天数", "window_days", "check_window_days")
LEGACY_PARTITION_COLUMNS = ("历史分区标识", "legacy_partition_id", "legacy_partition")
LAYER_COLUMNS = ("所属数仓层", "layer", "warehouse_layer")
METASTORE_CODE_COLUMNS = ("表编号", "table_code", "table_id")


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def existing_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(row["name"]) for row in rows}


def pick(row: dict[str, Any], columns: tuple[str, ...]) -> Any:
    for column in columns:
        if column in row and row[column] not in (None, ""):
            return row[column]
    return None


def fetch_table(
    connection: sqlite3.Connection,
    table: str,
) -> list[dict[str, Any]]:
    if table not in existing_tables(connection):
        return []
    cursor = connection.execute(f'SELECT * FROM "{table}"')
    return [dict(row) for row in cursor.fetchall()]


def legacy_source_tables(
    connection: sqlite3.Connection,
    legacy_tables: Iterable[str] | None = None,
) -> list[str]:
    """找出所有"旧口径"来源表。

    显式传入优先；否则按 :data:`LEGACY_TABLES` 顺序找，再补上存在但没有日期键
    列的 ``partitioncheck``/``partition_check``——旧库经常直接叫这个名字。
    """
    tables = existing_tables(connection)
    ordered: list[str] = []
    if legacy_tables is not None:
        for table in legacy_tables:
            if table in tables and table not in ordered:
                ordered.append(table)
        return ordered
    for table in LEGACY_TABLES:
        if table in tables and table not in ordered:
            ordered.append(table)
    for table in CURRENT_TABLES:
        if table not in tables or table in ordered:
            continue
        columns = table_columns(connection, table)
        if not any(column in columns for column in DATE_KEY_COLUMNS):
            ordered.append(table)
    return ordered


def legacy_row_to_rule(row: dict[str, Any], index: int) -> dict[str, Any]:
    return _legacy_row_to_rule(row, index)


def load_current_date_rules(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """当前带日期键的规则：用来判断同表新规则是否与旧数量规则冲突。"""
    rules: list[dict[str, Any]] = []
    tables = existing_tables(connection)
    for table in CURRENT_TABLES:
        if table not in tables:
            continue
        columns = table_columns(connection, table)
        if not any(column in columns for column in DATE_KEY_COLUMNS):
            continue
        for index, raw in enumerate(fetch_table(connection, table), start=1):
            date_key = pr.text(pick(raw, DATE_KEY_COLUMNS))
            if not date_key:
                continue
            rules.append(
                {
                    "规则编号": pr.text(pick(raw, RULE_CODE_COLUMNS)),
                    "数据表名": pr.text(pick(raw, TABLE_NAME_COLUMNS)),
                    "日期键": date_key,
                    "分区字段": pr.text(pick(raw, PARTITION_FIELD_COLUMNS)) or date_key,
                    "检查窗口天数": pr.to_int(
                        pick(raw, WINDOW_COLUMNS), pr.WINDOW_DAYS
                    ),
                    "期望分区数": pr.to_int(pick(raw, EXPECTED_COLUMNS), 0),
                }
            )
    for raw in _snapshot_rows(connection):
        if pr.text(raw.get("规则来源")) == pr.MANUAL_SOURCE:
            rules.append(
                {
                    "规则编号": pr.text(raw.get("规则编号")),
                    "数据表名": pr.text(raw.get("数据表名")),
                    "日期键": pr.text(raw.get("日期键")),
                    "分区字段": pr.text(raw.get("分区字段")),
                    "检查窗口天数": pr.to_int(raw.get("检查窗口天数"), pr.WINDOW_DAYS),
                    "期望分区数": pr.to_int(raw.get("期望分区数"), 0),
                }
            )
    return rules


def load_metastore_lookup(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """表名 → 元数据行，用于补齐旧规则缺失的日期键/分区字段。"""
    lookup: dict[str, dict[str, Any]] = {}
    for row in load_metastore_rows_from_connection(connection):
        table_name = pr.text(row.get("表名"))
        if table_name:
            lookup[table_name] = row
    return lookup


def load_metastore_rows_from_connection(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    tables = existing_tables(connection)
    for table in METASTORE_TABLES:
        if table not in tables:
            continue
        columns = table_columns(connection, table)
        if "表名" not in columns and "table_name" not in columns:
            continue
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(fetch_table(connection, table), start=1):
            date_key = pr.text(pick(raw, DATE_KEY_COLUMNS))
            rows.append(
                {
                    "id": index,
                    "表编号": pr.text(pick(raw, METASTORE_CODE_COLUMNS))
                    or f"META-{index:04d}",
                    "表名": pr.text(pick(raw, TABLE_NAME_COLUMNS)),
                    "所属数仓层": pr.text(pick(raw, LAYER_COLUMNS)),
                    "日期键": date_key,
                    "分区字段": pr.text(pick(raw, PARTITION_FIELD_COLUMNS)) or date_key,
                }
            )
        return rows
    return []


def _legacy_row_to_rule(row: dict[str, Any], index: int) -> dict[str, Any]:
    rule_code = pr.text(pick(row, RULE_CODE_COLUMNS))
    table_name = pr.text(pick(row, TABLE_NAME_COLUMNS))
    actual_values = pick(row, ACTUAL_VALUES_COLUMNS)
    if isinstance(actual_values, str):
        raw = actual_values.strip()
        if raw.startswith("["):
            try:
                actual_values = [str(item) for item in json.loads(raw)]
            except (TypeError, ValueError):
                actual_values = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            actual_values = [item.strip() for item in raw.split(",") if item.strip()]
    actual_values = [pr.text(item) for item in (actual_values or []) if pr.text(item)]
    actual_count = pick(row, ACTUAL_COUNT_COLUMNS)
    if actual_count is None and actual_values:
        actual_count = len(actual_values)
    return {
        "id": index,
        "status": pr.text(pick(row, STATUS_COLUMNS)) or "已启用",
        "pending": False,
        "abnormal": False,
        "规则编号": rule_code,
        "数据表名": table_name,
        "期望分区数": pr.to_int(pick(row, EXPECTED_COLUMNS), 0),
        "日期键": "",  # 旧表没有日期键
        "分区字段": "",
        "历史分区标识": pr.text(pick(row, LEGACY_PARTITION_COLUMNS))
        or (f"LEGACY-{rule_code}" if rule_code else ""),
        "规则来源": pr.LEGACY_SOURCE,
        "来源规则编号": rule_code,
        "兼容标记": pr.COMPAT_PENDING,
        "口径说明": "旧数据库记录尚未完成迁移，检测任务暂按数量口径读取",
        "口径冲突": False,
        "检查窗口天数": pr.to_int(pick(row, WINDOW_COLUMNS), pr.WINDOW_DAYS),
        "快照日期键": pr.SNAPSHOT_DATE_KEY,
        "快照版本": 0,
        "迁移状态": "待迁移",
        "实际分区": actual_values,
        "实际分区数": None if actual_count is None else pr.to_int(actual_count, 0),
    }


def _current_row_to_rule(row: dict[str, Any], index: int) -> dict[str, Any]:
    rule_code = pr.text(pick(row, RULE_CODE_COLUMNS))
    table_name = pr.text(pick(row, TABLE_NAME_COLUMNS))
    date_key = pr.text(pick(row, DATE_KEY_COLUMNS))
    partition_field = pr.text(pick(row, PARTITION_FIELD_COLUMNS)) or date_key
    actual_values = pick(row, ACTUAL_VALUES_COLUMNS)
    if isinstance(actual_values, str) and actual_values.strip().startswith("["):
        try:
            actual_values = [pr.text(item) for item in json.loads(actual_values)]
        except (TypeError, ValueError):
            actual_values = []
    actual_values = actual_values or []
    actual_count = pick(row, ACTUAL_COUNT_COLUMNS)
    if actual_count is None and actual_values:
        actual_count = len(actual_values)
    return {
        "id": index,
        "status": pr.text(pick(row, STATUS_COLUMNS)) or "已启用",
        "pending": False,
        "abnormal": False,
        "规则编号": rule_code,
        "数据表名": table_name,
        "期望分区数": pr.to_int(pick(row, EXPECTED_COLUMNS), 0),
        "日期键": date_key,
        "分区字段": partition_field,
        "历史分区标识": pr.text(pick(row, LEGACY_PARTITION_COLUMNS)),
        "规则来源": "分区检测登记",
        "来源规则编号": rule_code,
        "兼容标记": pr.COMPAT_DATE_KEY if date_key else pr.COMPAT_COUNT,
        "口径说明": "当前规则表读取，按规则自带日期键计算",
        "口径冲突": False,
        "检查窗口天数": pr.to_int(pick(row, WINDOW_COLUMNS), pr.WINDOW_DAYS),
        "快照日期键": pr.SNAPSHOT_DATE_KEY,
        "快照版本": 1,
        "迁移状态": "无需迁移",
        "实际分区": actual_values,
        "实际分区数": None if actual_count is None else pr.to_int(actual_count, 0),
    }


def _snapshot_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in SNAPSHOT_TABLES:
        for raw in fetch_table(connection, table):
            payload: dict[str, Any] = {}
            payload_json = pr.text(raw.get("payload_json") or raw.get("payload"))
            if payload_json:
                try:
                    decoded = json.loads(payload_json)
                    if isinstance(decoded, dict):
                        payload = decoded
                except (TypeError, ValueError):
                    payload = {}
            rule_code = pr.text(raw.get("rule_code")) or pr.text(payload.get("规则编号"))
            table_name = pr.text(raw.get("table_name")) or pr.text(payload.get("数据表名"))
            row = {
                "规则编号": rule_code,
                "来源规则编号": pr.text(raw.get("source_rule_code")) or rule_code,
                "规则来源": pr.text(raw.get("rule_source")) or pr.LEGACY_SOURCE,
                "数据表名": table_name,
                "期望分区数": pr.to_int(
                    raw.get("expected_partitions") or payload.get("期望分区数"), 0
                ),
                "日期键": pr.text(raw.get("date_key")) or pr.text(payload.get("日期键")),
                "分区字段": pr.text(raw.get("partition_field"))
                or pr.text(payload.get("分区字段")),
                "历史分区标识": pr.text(raw.get("legacy_partition_id"))
                or pr.text(payload.get("历史分区标识")),
                "兼容标记": pr.text(raw.get("compat_mode"))
                or pr.text(payload.get("兼容标记")),
                "口径说明": pr.text(payload.get("口径说明")),
                "口径冲突": bool(raw.get("conflict_flag", payload.get("口径冲突"))),
                "检查窗口天数": pr.to_int(
                    raw.get("window_days") or payload.get("检查窗口天数"),
                    pr.WINDOW_DAYS,
                ),
                "快照日期键": pr.text(raw.get("snapshot_date_key"))
                or pr.text(payload.get("快照日期键"))
                or pr.SNAPSHOT_DATE_KEY,
                "快照版本": pr.to_int(
                    raw.get("snapshot_version") or payload.get("快照版本"), 1
                ),
                "迁移状态": "已迁移",
                "实际分区": payload.get("实际分区") or [],
                "实际分区数": payload.get("实际分区数"),
            }
            row.update({key: value for key, value in payload.items() if key not in row})
            rows.append(row)
    return rows


def snapshot_dicts_from_db(db_path: str | Path) -> list[dict[str, Any]]:
    """只读取迁移产出的日期键快照，供元数据存储加载分区字段。"""
    path = Path(db_path)
    if not path.exists() or not path.is_file():
        return []
    try:
        connection = connect(path)
    except sqlite3.DatabaseError:
        return []
    try:
        return _snapshot_rows(connection)
    finally:
        connection.close()


def load_partitioncheck_rows(db_path: str | Path) -> list[dict[str, Any]]:
    """旧表未迁移记录 + 当前日期键规则 + 迁移快照，合并成检测任务可读的行。

    同一规则编号以快照为准：迁移完成后检测任务自然读到新口径，
    迁移期间尚未完成的旧记录仍以 ``待确认/数量口径`` 出现。
    """
    path = Path(db_path)
    if not path.exists() or not path.is_file():
        return []
    try:
        connection = connect(path)
    except sqlite3.DatabaseError:
        return []
    try:
        tables = existing_tables(connection)
        snapshots = _snapshot_rows(connection)
        snapshot_codes = {pr.text(row.get("规则编号")) for row in snapshots}
        legacy_tables = set(legacy_source_tables(connection))

        current: list[dict[str, Any]] = []
        for table in CURRENT_TABLES:
            if table not in tables or table in legacy_tables:
                continue
            columns = table_columns(connection, table)
            if not any(column in columns for column in DATE_KEY_COLUMNS):
                continue
            for raw in fetch_table(connection, table):
                if pr.text(pick(raw, RULE_CODE_COLUMNS)) in snapshot_codes:
                    continue
                current.append(_current_row_to_rule(raw, 0))

        # 旧库的 partitioncheck 表可能根本没有日期键列；这类表也算旧口径，
        # 迁移期间检测任务仍要能读到（按待确认/数量口径兜底）。
        legacy: list[dict[str, Any]] = []
        for table in legacy_source_tables(connection):
            for raw in fetch_table(connection, table):
                if pr.text(pick(raw, RULE_CODE_COLUMNS)) in snapshot_codes:
                    continue
                legacy.append(_legacy_row_to_rule(raw, 0))

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        # 优先级：迁移快照 > 当前日期键表 > 旧表；同一规则编号只出现一次。
        for row in snapshots + current + legacy:
            rule_code = pr.text(row.get("规则编号"))
            if rule_code and rule_code in seen:
                continue
            seen.add(rule_code)
            rows.append(row)
        for index, row in enumerate(rows, start=1):
            row["id"] = index
            row.setdefault("status", "已启用")
            row.setdefault("pending", False)
            row.setdefault("abnormal", False)
        return rows
    finally:
        connection.close()


def load_metastore_rows(db_path: str | Path) -> list[dict[str, Any]]:
    path = Path(db_path)
    if not path.exists() or not path.is_file():
        return []
    try:
        connection = connect(path)
    except sqlite3.DatabaseError:
        return []
    try:
        for table in METASTORE_TABLES:
            if table not in existing_tables(connection):
                continue
            columns = table_columns(connection, table)
            if "表名" not in columns and "table_name" not in columns:
                continue
            rows: list[dict[str, Any]] = []
            for index, raw in enumerate(fetch_table(connection, table), start=1):
                date_key = pr.text(pick(raw, DATE_KEY_COLUMNS))
                partition_field = pr.text(pick(raw, PARTITION_FIELD_COLUMNS)) or date_key
                rows.append(
                    {
                        "id": index,
                        "status": pr.text(pick(raw, STATUS_COLUMNS)) or "已确认",
                        "pending": False,
                        "abnormal": False,
                        "表编号": pr.text(pick(raw, METASTORE_CODE_COLUMNS))
                        or f"META-{index:04d}",
                        "表名": pr.text(pick(raw, TABLE_NAME_COLUMNS)),
                        "所属数仓层": pr.text(pick(raw, LAYER_COLUMNS)),
                        "字段数量": pr.to_int(raw.get("字段数量") or raw.get("column_count"), 0),
                        "日期键": date_key,
                        "分区字段": partition_field,
                        "主键字段": pr.text(raw.get("主键字段") or raw.get("primary_key")),
                        "数据量级": pr.text(raw.get("数据量级") or raw.get("row_scale")),
                        "最近更新时间": pr.text(
                            raw.get("最近更新时间") or raw.get("updated_at")
                        ),
                    }
                )
            return rows
        return []
    finally:
        connection.close()


def seed_metastore_rows() -> list[dict[str, Any]]:
    return [dict(row) for row in SEED_METASTORE_TABLES]
