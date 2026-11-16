#!/usr/bin/env python3
"""分区检测规则迁移脚本：历史数量规则 → 带日期键的规则数据快照。

用法（在 backend 目录下执行）::

    python scripts/partition_rule_migration.py status  --db ./var/etl.sqlite3
    python scripts/partition_rule_migration.py migrate --db ./var/etl.sqlite3
    python scripts/partition_rule_migration.py migrate --db ./var/etl.sqlite3 --stop-after 2
    python scripts/partition_rule_migration.py migrate --db ./var/etl.sqlite3 --fail-rule PART-0003
    python scripts/partition_rule_migration.py verify  --db ./var/etl.sqlite3
    python scripts/partition_rule_migration.py seed-legacy --db ./var/etl.sqlite3

兼容说明：

* 只依赖标准库 ``sqlite3``/``argparse``，可直接用当前 ``.venv`` 或系统 Python 跑；
* 旧库表名/列名通过实际表结构探测（``partition_check_rules``、``partitioncheck``、
  ``rule_code``/``规则编号`` 等），没有日期键列的老表按旧数量规则处理；
* 日期键与分区字段默认取 :mod:`app.partition_rules` 里的常量，与示例数据、
  部署环境完全一致；可用 ``--date-key``/``--partition-field`` 显式覆盖。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import partition_rules as pr  # noqa: E402
from app import partition_repository as repo  # noqa: E402
from app.partition_seed import LEGACY_RULES, METASTORE_TABLES  # noqa: E402
from app.services import partition_migration as migration  # noqa: E402


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def command_seed_legacy(args: argparse.Namespace) -> int:
    """造一个只含旧表和旧列名的示例库，便于本地验证迁移。"""
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("DROP TABLE IF EXISTS partition_check_rules")
        connection.execute(
            """
            CREATE TABLE partition_check_rules (
                id INTEGER PRIMARY KEY,
                rule_code TEXT NOT NULL,
                table_name TEXT NOT NULL,
                expected_partitions INTEGER NOT NULL,
                actual_partitions_count INTEGER,
                actual_partitions TEXT,
                status TEXT
            )
            """
        )
        for index, rule in enumerate(LEGACY_RULES, start=1):
            connection.execute(
                "INSERT INTO partition_check_rules"
                "(id, rule_code, table_name, expected_partitions, actual_partitions_count,"
                " actual_partitions, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    index,
                    rule["规则编号"],
                    rule["数据表名"],
                    rule["期望分区数"],
                    rule.get("实际分区数"),
                    json.dumps(rule.get("实际分区") or [], ensure_ascii=False),
                    rule.get("状态") or "已启用",
                ),
            )
        connection.execute("DROP TABLE IF EXISTS metastore")
        connection.execute(
            """
            CREATE TABLE metastore (
                id INTEGER PRIMARY KEY,
                table_code TEXT NOT NULL,
                table_name TEXT NOT NULL,
                layer TEXT,
                column_count INTEGER,
                date_key TEXT,
                partition_field TEXT
            )
            """
        )
        for row in METASTORE_TABLES:
            connection.execute(
                "INSERT INTO metastore"
                "(id, table_code, table_name, layer, column_count, date_key, partition_field)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["表编号"],
                    row["表名"],
                    row["所属数仓层"],
                    row["字段数量"],
                    pr.DATE_KEY,
                    pr.PARTITION_FIELD,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    _print({"ok": True, "db_path": str(path), "message": "已写入旧表结构的示例数据"})
    return 0


def command_status(args: argparse.Namespace) -> int:
    path = Path(args.db)
    if not path.exists():
        _print({"ok": False, "db_path": str(path), "message": "数据库不存在"})
        return 1
    connection = repo.connect(path)
    try:
        tables = sorted(repo.existing_tables(connection))
        state: dict[str, str] = {}
        if migration.STATE_TABLE in tables:
            for row in repo.fetch_table(connection, migration.STATE_TABLE):
                state[str(row.get("key"))] = str(row.get("value") or "")
        snapshots = (
            len(repo.fetch_table(connection, migration.SNAPSHOT_TABLE))
            if migration.SNAPSHOT_TABLE in tables
            else 0
        )
        legacy = migration.discover_legacy_rules(connection)
        failed: list[dict[str, str]] = []
        if migration.FAILURE_TABLE in tables:
            failed = [
                {
                    "rule_code": str(row.get("rule_code")),
                    "message": str(row.get("message")),
                }
                for row in repo.fetch_table(connection, migration.FAILURE_TABLE)
            ]
        _print(
            {
                "ok": True,
                "db_path": str(path),
                "tables": tables,
                "legacy_rule_total": len(legacy),
                "snapshot_count": snapshots,
                "migration_state": state,
                "unfinished_rules": failed,
                "date_key": pr.DATE_KEY,
                "partition_field": pr.PARTITION_FIELD,
            }
        )
        return 0
    finally:
        connection.close()


def command_migrate(args: argparse.Namespace) -> int:
    def fail_hook(rule: dict) -> None:
        if args.fail_once_flag and Path(args.fail_once_flag).exists():
            if pr.text(rule.get("规则编号")) == args.fail_rule:
                Path(args.fail_once_flag).unlink()
                raise RuntimeError("模拟迁移中途失败（一次性开关）")

    result = migration.migrate(
        args.db,
        snapshot_date_key=args.snapshot_date_key,
        date_key=args.date_key,
        partition_field=args.partition_field,
        legacy_tables=args.legacy_table or None,
        stop_after=args.stop_after,
        fail_rule_codes=args.fail_rule if args.fail_rule else None,
        fail_hook=fail_hook if args.fail_once_flag else None,
        dry_run=args.dry_run,
    )
    _print(result.as_dict())
    return 0 if result.failed == 0 else 2


def command_verify(args: argparse.Namespace) -> int:
    report = migration.verify(args.db)
    _print(report)
    return 0 if report.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="历史分区检测规则迁移（回填日期键快照）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--db", required=True, help="旧数据库/迁移产物路径（SQLite）")

    seed = sub.add_parser("seed-legacy", help="生成旧表结构的示例库")
    add_common(seed)
    seed.set_defaults(func=command_seed_legacy)

    status = sub.add_parser("status", help="查看旧规则数量与迁移检查点")
    add_common(status)
    status.set_defaults(func=command_status)

    migrate = sub.add_parser("migrate", help="执行迁移并回填日期键快照")
    add_common(migrate)
    migrate.add_argument("--date-key", default=pr.DATE_KEY, help=f"日期键，默认 {pr.DATE_KEY}")
    migrate.add_argument(
        "--partition-field",
        default=pr.PARTITION_FIELD,
        help=f"分区字段，默认 {pr.PARTITION_FIELD}",
    )
    migrate.add_argument(
        "--snapshot-date-key",
        default=pr.SNAPSHOT_DATE_KEY,
        help=f"快照日期键，默认 {pr.SNAPSHOT_DATE_KEY}",
    )
    migrate.add_argument(
        "--legacy-table",
        action="append",
        default=[],
        help="显式指定旧表名，可重复；默认自动探测",
    )
    migrate.add_argument(
        "--stop-after",
        type=int,
        default=None,
        help="处理 N 条规则后停止，模拟中途失败",
    )
    migrate.add_argument(
        "--fail-rule",
        action="append",
        default=[],
        help="指定规则迁移失败，可重复，用于验证断点续做",
    )
    migrate.add_argument(
        "--fail-once-flag",
        default=None,
        help="一次性失败开关文件：第一次遇到 --fail-rule 时删掉它并抛错",
    )
    migrate.add_argument("--dry-run", action="store_true", help="只演练不写库")
    migrate.set_defaults(func=command_migrate)

    verify = sub.add_parser("verify", help="校验快照保留原规则编号、日期键齐全且无重复版本")
    add_common(verify)
    verify.set_defaults(func=command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
