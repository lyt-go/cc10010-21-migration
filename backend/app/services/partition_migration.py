"""分区检测规则迁移：历史数量规则 → 带日期键的规则数据快照。

设计要点（与需求逐条对应）：

* 旧规则只存表名 + 期望分区数，迁移时补齐日期键、分区字段、历史分区标识；
* ``规则编号`` 原样保留为主键，快照里另有 ``来源规则编号``，缺失分区清单与
  完整性结论都带着它，迁移后仍可追溯原规则；
* 同一历史规则重复迁移只生成一个版本：``rule_code`` 唯一 + 内容指纹去重；
* 每条规则一个事务并写检查点，中途失败后再次运行会从最后一条完整规则继续；
* 只依赖标准库 ``sqlite3``，能读旧表名/旧列名，也能在当前 Python 环境直接跑。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from app import partition_rules as pr
from app import partition_repository as repo

SNAPSHOT_TABLE = "partition_rule_snapshot"
LOG_TABLE = "partition_rule_migration_log"
STATE_TABLE = "partition_rule_migration_state"
FAILURE_TABLE = "partition_rule_migration_failure"

STATE_LAST_RULE = "last_completed_rule_code"
STATE_LAST_INDEX = "last_completed_index"
STATE_TOTAL = "legacy_rule_total"
STATE_STATUS = "status"
STATE_UPDATED_AT = "updated_at"
STATE_RESUMED_FROM = "resumed_from_rule_code"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"

SNAPSHOT_DDL = f"""
CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
    rule_code TEXT PRIMARY KEY,
    source_rule_code TEXT NOT NULL,
    rule_source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    expected_partitions INTEGER NOT NULL DEFAULT 0,
    date_key TEXT,
    partition_field TEXT,
    legacy_partition_id TEXT,
    compat_mode TEXT NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 7,
    snapshot_date_key TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL DEFAULT 1,
    conflict_flag INTEGER NOT NULL DEFAULT 0,
    content_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    migrated_at TEXT NOT NULL
)
"""

LOG_DDL = f"""
CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
    rule_code TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    fingerprint TEXT,
    snapshot_version INTEGER NOT NULL DEFAULT 1,
    message TEXT,
    migrated_at TEXT NOT NULL
)
"""

STATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

FAILURE_DDL = f"""
CREATE TABLE IF NOT EXISTS {FAILURE_TABLE} (
    rule_code TEXT PRIMARY KEY,
    message TEXT,
    failed_at TEXT NOT NULL
)
"""


@dataclass
class MigrationResult:
    db_path: str
    snapshot_table: str = SNAPSHOT_TABLE
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    resumed_from: str | None = None
    last_completed_rule: str | None = None
    status: str = STATUS_FINISHED
    dry_run: bool = False
    messages: list[str] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "snapshot_table": self.snapshot_table,
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "resumed_from": self.resumed_from,
            "last_completed_rule": self.last_completed_rule,
            "status": self.status,
            "dry_run": self.dry_run,
            "messages": list(self.messages),
        }


def ensure_schema(connection: sqlite3.Connection) -> None:
    for ddl in (SNAPSHOT_DDL, LOG_DDL, STATE_DDL, FAILURE_DDL):
        connection.execute(ddl)
    connection.commit()


def _state_get(connection: sqlite3.Connection, key: str) -> str | None:
    try:
        row = connection.execute(
            f"SELECT value FROM {STATE_TABLE} WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return None if row is None else str(row[0])


def _state_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        f"INSERT INTO {STATE_TABLE}(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "" if value is None else str(value)),
    )


def _existing_snapshot(
    connection: sqlite3.Connection, rule_code: str
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT * FROM {SNAPSHOT_TABLE} WHERE rule_code = ?", (rule_code,)
    ).fetchone()
    return None if row is None else dict(row)


def _completed_rule_codes(connection: sqlite3.Connection) -> set[str]:
    try:
        rows = connection.execute(
            f"SELECT rule_code FROM {LOG_TABLE} WHERE status = ?", (STATUS_OK,)
        ).fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(row[0]) for row in rows}


def _write_snapshot(
    connection: sqlite3.Connection, snapshot: dict[str, Any]
) -> str:
    """写入或更新单个快照，返回 inserted / updated / skipped。"""
    rule_code = pr.text(snapshot["规则编号"])
    existing = _existing_snapshot(connection, rule_code)
    payload_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    migrated_at = pr.now_iso()
    if existing is not None:
        if pr.text(existing.get("content_fingerprint")) == snapshot["内容指纹"]:
            return "skipped"
        connection.execute(
            f"""
            UPDATE {SNAPSHOT_TABLE}
               SET source_rule_code = ?, rule_source = ?, table_name = ?,
                   expected_partitions = ?, date_key = ?, partition_field = ?,
                   legacy_partition_id = ?, compat_mode = ?, window_days = ?,
                   snapshot_date_key = ?, snapshot_version = 1, conflict_flag = ?,
                   content_fingerprint = ?, payload_json = ?, migrated_at = ?
             WHERE rule_code = ?
            """,
            (
                snapshot["来源规则编号"],
                snapshot["规则来源"],
                snapshot["数据表名"],
                pr.to_int(snapshot["期望分区数"], 0),
                snapshot["日期键"],
                snapshot["分区字段"],
                snapshot["历史分区标识"],
                snapshot["兼容标记"],
                pr.to_int(snapshot["检查窗口天数"], pr.WINDOW_DAYS),
                snapshot["快照日期键"],
                1 if snapshot["口径冲突"] else 0,
                snapshot["内容指纹"],
                payload_json,
                migrated_at,
                rule_code,
            ),
        )
        return "updated"

    connection.execute(
        f"""
        INSERT INTO {SNAPSHOT_TABLE}(
            rule_code, source_rule_code, rule_source, table_name,
            expected_partitions, date_key, partition_field, legacy_partition_id,
            compat_mode, window_days, snapshot_date_key, snapshot_version,
            conflict_flag, content_fingerprint, payload_json, migrated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            rule_code,
            snapshot["来源规则编号"],
            snapshot["规则来源"],
            snapshot["数据表名"],
            pr.to_int(snapshot["期望分区数"], 0),
            snapshot["日期键"],
            snapshot["分区字段"],
            snapshot["历史分区标识"],
            snapshot["兼容标记"],
            pr.to_int(snapshot["检查窗口天数"], pr.WINDOW_DAYS),
            snapshot["快照日期键"],
            1 if snapshot["口径冲突"] else 0,
            snapshot["内容指纹"],
            payload_json,
            migrated_at,
        ),
    )
    return "inserted"


def _record_log(
    connection: sqlite3.Connection,
    rule_code: str,
    status: str,
    snapshot: dict[str, Any] | None,
    message: str,
) -> None:
    connection.execute(
        f"""
        INSERT INTO {LOG_TABLE}(rule_code, status, fingerprint, snapshot_version, message, migrated_at)
        VALUES(?, ?, ?, 1, ?, ?)
        ON CONFLICT(rule_code) DO UPDATE SET
            status = excluded.status,
            fingerprint = excluded.fingerprint,
            message = excluded.message,
            migrated_at = excluded.migrated_at
        """,
        (
            rule_code,
            status,
            snapshot["内容指纹"] if snapshot else None,
            message,
            pr.now_iso(),
        ),
    )


def _record_failure(
    connection: sqlite3.Connection, rule_code: str, message: str
) -> None:
    connection.execute(
        f"""
        INSERT INTO {FAILURE_TABLE}(rule_code, message, failed_at)
        VALUES(?, ?, ?)
        ON CONFLICT(rule_code) DO UPDATE SET
            message = excluded.message, failed_at = excluded.failed_at
        """,
        (rule_code, message, pr.now_iso()),
    )


def _clear_failure(connection: sqlite3.Connection, rule_code: str) -> None:
    connection.execute(
        f"DELETE FROM {FAILURE_TABLE} WHERE rule_code = ?", (rule_code,)
    )


def discover_legacy_rules(
    connection: sqlite3.Connection,
    legacy_tables: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """按稳定顺序读取旧规则；``partitioncheck`` 没有日期键列时也算旧表。"""
    tables = repo.legacy_source_tables(connection, legacy_tables)
    rules: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    for table in tables:
        for row in repo.fetch_table(connection, table):
            rule_code = pr.text(repo.pick(row, repo.RULE_CODE_COLUMNS))
            if not rule_code:
                continue
            # 同一规则编号在同一旧表里重复出现时只保留第一条，避免重复版本。
            if rule_code in seen_rows:
                continue
            seen_rows.add(rule_code)
            rule = repo.legacy_row_to_rule({**row, "__source_table": table}, len(rules) + 1)
            rule["源表"] = table
            rules.append(rule)
    rules.sort(key=lambda item: (pr.text(item.get("规则编号")), pr.to_int(item.get("id"), 0)))
    return rules


def migrate(
    db_path: str | Path,
    *,
    snapshot_date_key: str = pr.SNAPSHOT_DATE_KEY,
    date_key: str | None = None,
    partition_field: str | None = None,
    legacy_tables: Iterable[str] | None = None,
    stop_after: int | None = None,
    fail_rule_codes: Iterable[str] | None = None,
    fail_hook: Callable[[dict[str, Any]], None] | None = None,
    dry_run: bool = False,
) -> MigrationResult:
    """迁移历史分区检测规则并回填日期键快照。

    ``stop_after`` 用来模拟中途失败，便于验证"从最后完整规则继续"；
    ``fail_hook`` 供调用方注入失败，抛出的异常会被记录为该条规则失败。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = MigrationResult(db_path=str(path), dry_run=dry_run)
    failing = {pr.text(code) for code in (fail_rule_codes or [])}

    connection = repo.connect(path)
    try:
        ensure_schema(connection)
        legacy_rules = discover_legacy_rules(connection, legacy_tables)
        result.total = len(legacy_rules)
        if not legacy_rules:
            result.status = STATUS_FINISHED
            result.messages.append("旧数据库中没有找到历史分区检测规则，无需迁移")
            if not dry_run:
                _state_set(connection, STATE_STATUS, STATUS_FINISHED)
                _state_set(connection, STATE_TOTAL, 0)
                _state_set(connection, STATE_UPDATED_AT, pr.now_iso())
                connection.commit()
            return result

        completed = _completed_rule_codes(connection)
        resumed_from = _state_get(connection, STATE_LAST_RULE)
        if resumed_from and resumed_from not in {pr.text(r["规则编号"]) for r in legacy_rules}:
            resumed_from = None
        result.resumed_from = resumed_from
        result.last_completed_rule = resumed_from
        if not dry_run:
            _state_set(connection, STATE_STATUS, STATUS_RUNNING)
            _state_set(connection, STATE_TOTAL, len(legacy_rules))
            _state_set(connection, STATE_RESUMED_FROM, resumed_from or "")
            connection.commit()

        # 当前日期键规则与元数据表用于判断旧规则迁移后应该采用哪种兼容标记。
        current_date_rules = repo.load_current_date_rules(connection)
        metastore_by_table = repo.load_metastore_lookup(connection)
        resolved_date_key = pr.text(date_key) or pr.DATE_KEY
        resolved_partition_field = pr.text(partition_field) or pr.PARTITION_FIELD

        processed = 0
        for index, legacy_rule in enumerate(legacy_rules, start=1):
            rule_code = pr.text(legacy_rule.get("规则编号"))
            # 已经完整迁移过的规则直接跳过——重复迁移不会产生第二个版本。
            if rule_code in completed:
                result.skipped += 1
                result.snapshots.append(
                    {
                        "规则编号": rule_code,
                        "status": "skipped",
                        "reason": "该规则已完整迁移，保留现有快照版本",
                    }
                )
                continue

            if stop_after is not None and processed >= stop_after:
                result.status = STATUS_RUNNING
                result.messages.append(
                    f"已按计划在处理 {processed} 条规则后停止，下次从规则 {rule_code} 继续"
                )
                break

            enriched = dict(legacy_rule)
            metastore_row = metastore_by_table.get(pr.text(enriched.get("数据表名")), {})
            # 回填顺序：规则自带 > 元数据表 > 统一常量；总是能补出日期键，
            # 只有检测任务读到尚未迁移的旧库记录时才会出现"待确认"。
            if not pr.text(enriched.get("日期键")):
                enriched["日期键"] = pr.text(metastore_row.get("日期键")) or resolved_date_key
            if not pr.text(enriched.get("分区字段")):
                enriched["分区字段"] = (
                    pr.text(metastore_row.get("分区字段"))
                    or pr.text(enriched["日期键"])
                    or resolved_partition_field
                )

            snapshot: dict[str, Any] | None = None
            try:
                if fail_hook is not None:
                    fail_hook(enriched)
                if rule_code in failing:
                    raise RuntimeError("模拟迁移失败：规则内容无法转换")
                snapshot = pr.build_legacy_snapshot(
                    enriched,
                    metastore_by_table=metastore_by_table,
                    current_date_rules=current_date_rules,
                    snapshot_date_key=snapshot_date_key,
                )
                if dry_run:
                    outcome = "planned"
                else:
                    with connection:  # 单条规则一个事务，失败只回滚这一条
                        outcome = _write_snapshot(connection, snapshot)
                        _record_log(connection, rule_code, STATUS_OK, snapshot, "迁移完成")
                        _clear_failure(connection, rule_code)
                        _state_set(connection, STATE_LAST_RULE, rule_code)
                        _state_set(connection, STATE_LAST_INDEX, index)
                        _state_set(connection, STATE_STATUS, STATUS_RUNNING)
                        _state_set(connection, STATE_UPDATED_AT, pr.now_iso())
                if outcome == "inserted":
                    result.inserted += 1
                elif outcome == "updated":
                    result.updated += 1
                elif outcome == "skipped":
                    result.skipped += 1
                else:
                    result.inserted += 1
                result.last_completed_rule = rule_code
                result.snapshots.append(
                    {
                        "规则编号": rule_code,
                        "数据表名": snapshot["数据表名"],
                        "日期键": snapshot["日期键"],
                        "分区字段": snapshot["分区字段"],
                        "兼容标记": snapshot["兼容标记"],
                        "状态": outcome,
                    }
                )
            except Exception as error:  # noqa: BLE001 - 迁移需要记录任何单条失败
                connection.rollback()
                result.failed += 1
                message = f"{type(error).__name__}: {error}"
                if not dry_run:
                    with connection:
                        _record_log(connection, rule_code, STATUS_FAILED, snapshot, message)
                        _record_failure(connection, rule_code, message)
                        _state_set(connection, STATE_STATUS, STATUS_RUNNING)
                        _state_set(connection, STATE_UPDATED_AT, pr.now_iso())
                result.messages.append(f"规则 {rule_code} 迁移失败：{message}")
                result.snapshots.append(
                    {"规则编号": rule_code, "status": "failed", "reason": message}
                )
                # 失败后停在这里，检查点保持最后一条完整规则，便于补做。
                result.status = STATUS_RUNNING
                result.messages.append(
                    f"检查点停留在 {result.last_completed_rule or '迁移起点'}，"
                    f"补做将从规则 {rule_code} 继续"
                )
                break
            processed += 1

        if result.status != STATUS_RUNNING and not dry_run:
            with connection:
                _state_set(connection, STATE_STATUS, STATUS_FINISHED)
                _state_set(connection, STATE_LAST_RULE, result.last_completed_rule or "")
                _state_set(connection, STATE_UPDATED_AT, pr.now_iso())
        return result
    finally:
        connection.close()


def verify(db_path: str | Path) -> dict[str, Any]:
    """迁移后自检：快照是否保留原规则编号与来源，日期键是否齐全。"""
    path = Path(db_path)
    report: dict[str, Any] = {
        "db_path": str(path),
        "ok": True,
        "snapshot_count": 0,
        "missing_date_key": [],
        "missing_source": [],
        "rule_code_mismatch": [],
        "duplicate_versions": [],
    }
    if not path.exists():
        report["ok"] = False
        report["messages"] = ["数据库文件不存在"]
        return report
    connection = repo.connect(path)
    try:
        if SNAPSHOT_TABLE not in repo.existing_tables(connection):
            report["ok"] = False
            report["messages"] = ["快照表尚未创建，请先执行迁移"]
            return report
        rows = repo.fetch_table(connection, SNAPSHOT_TABLE)
        report["snapshot_count"] = len(rows)
        versions: dict[str, set[int]] = {}
        for row in rows:
            rule_code = pr.text(row.get("rule_code"))
            if not pr.text(row.get("date_key")):
                report["missing_date_key"].append(rule_code)
            if pr.text(row.get("source_rule_code")) != rule_code:
                report["rule_code_mismatch"].append(rule_code)
            if not pr.text(row.get("rule_source")):
                report["missing_source"].append(rule_code)
            versions.setdefault(rule_code, set()).add(
                pr.to_int(row.get("snapshot_version"), 1)
            )
        report["duplicate_versions"] = [
            code for code, values in versions.items() if len(values) > 1
        ]
        report["ok"] = not (
            report["missing_date_key"]
            or report["missing_source"]
            or report["rule_code_mismatch"]
            or report["duplicate_versions"]
        )
        return report
    finally:
        connection.close()
