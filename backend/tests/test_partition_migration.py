"""分区规则迁移与跨模块口径的回归测试（只依赖标准库 unittest）。

运行：``cd backend && .venv/bin/python -m unittest discover -s tests -v``
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import partition_rules as pr  # noqa: E402
from app import partition_repository as repo  # noqa: E402
from app.partition_analysis import (  # noqa: E402
    apply_to_metastore,
    consistency_report,
)
from app.partition_seed import (  # noqa: E402
    DATE_KEY_RULES,
    LEGACY_RULES,
    METASTORE_TABLES,
    build_partitioncheck_rows,
)
from app.services import partition_migration as migration  # noqa: E402


def seed_legacy_db(path: Path) -> None:
    """旧库：只有自由文本表名 + 期望分区数，没有日期键。"""
    connection = sqlite3.connect(str(path))
    try:
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
                "INSERT INTO partition_check_rules VALUES(?, ?, ?, ?, ?, ?, ?)",
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
        connection.execute(
            """
            CREATE TABLE metastore (
                id INTEGER PRIMARY KEY,
                table_code TEXT,
                table_name TEXT,
                layer TEXT,
                column_count INTEGER,
                date_key TEXT,
                partition_field TEXT
            )
            """
        )
        for row in METASTORE_TABLES:
            connection.execute(
                "INSERT INTO metastore VALUES(?, ?, ?, ?, ?, ?, ?)",
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
        connection.execute(
            """
            CREATE TABLE partitioncheck (
                id INTEGER PRIMARY KEY,
                rule_code TEXT,
                table_name TEXT,
                expected_partitions INTEGER,
                date_key TEXT,
                partition_field TEXT,
                window_days INTEGER,
                actual_partitions TEXT,
                status TEXT
            )
            """
        )
        for index, rule in enumerate(DATE_KEY_RULES, start=1):
            connection.execute(
                "INSERT INTO partitioncheck VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    index,
                    rule["规则编号"],
                    rule["数据表名"],
                    rule["期望分区数"],
                    rule["日期键"],
                    rule["分区字段"],
                    rule["检查窗口天数"],
                    json.dumps(rule["实际分区"], ensure_ascii=False),
                    rule["状态"],
                ),
            )
        connection.commit()
    finally:
        connection.close()


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "etl.sqlite3"
        seed_legacy_db(self.db)

    def snapshot_rows(self) -> list[dict]:
        connection = repo.connect(self.db)
        try:
            return repo.fetch_table(connection, migration.SNAPSHOT_TABLE)
        finally:
            connection.close()

    def test_snapshot_keeps_original_rule_code_and_source(self) -> None:
        result = migration.migrate(self.db)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.inserted, len(LEGACY_RULES))
        rows = self.snapshot_rows()
        codes = {row["rule_code"] for row in rows}
        self.assertEqual(codes, {rule["规则编号"] for rule in LEGACY_RULES})
        for row in rows:
            self.assertEqual(row["source_rule_code"], row["rule_code"])
            self.assertEqual(row["rule_source"], pr.LEGACY_SOURCE)
            self.assertEqual(row["date_key"], pr.DATE_KEY)
            self.assertEqual(row["partition_field"], pr.PARTITION_FIELD)
            self.assertTrue(row["legacy_partition_id"])

    def test_conflicting_legacy_rule_uses_count_compat_mode(self) -> None:
        migration.migrate(self.db)
        rows = {row["rule_code"]: row for row in self.snapshot_rows()}
        # dws_user_profile 上已有 7 天窗口的日期键规则，30 天期望值与之冲突。
        self.assertEqual(rows["PART-0002"]["compat_mode"], pr.COMPAT_COUNT)
        self.assertEqual(int(rows["PART-0002"]["conflict_flag"]), 1)
        # 7 天期望值与窗口一致，采用等价标记。
        self.assertEqual(rows["PART-0001"]["compat_mode"], pr.COMPAT_EQUIVALENT)
        self.assertEqual(int(rows["PART-0001"]["conflict_flag"]), 0)

    def test_compat_mode_changes_missing_partition_calculation(self) -> None:
        base = {
            "规则编号": "PART-X",
            "数据表名": "dwd_order_daily",
            "期望分区数": 30,
            "实际分区数": 25,
            "日期键": pr.DATE_KEY,
            "分区字段": pr.PARTITION_FIELD,
            "快照日期键": pr.SNAPSHOT_DATE_KEY,
            "检查窗口天数": 7,
        }
        date_mode = pr.evaluate_rule({**base, "兼容标记": pr.COMPAT_DATE_KEY})
        count_mode = pr.evaluate_rule({**base, "兼容标记": pr.COMPAT_COUNT})
        self.assertEqual(date_mode["缺失分区数"], 0)
        self.assertEqual(count_mode["缺失分区数"], 5)
        self.assertNotEqual(date_mode["缺失分区数"], count_mode["缺失分区数"])

    def test_missing_partitions_carry_source_rule_code(self) -> None:
        migration.migrate(self.db)
        rows = repo.load_partitioncheck_rows(self.db)
        conclusions = {item["规则编号"]: item for item in pr.build_conclusions(rows)}
        conclusion = conclusions["PART-0002"]
        self.assertEqual(conclusion["来源规则编号"], "PART-0002")
        self.assertEqual(conclusion["规则来源"], pr.LEGACY_SOURCE)
        self.assertEqual(conclusion["缺失分区数"], 5)
        self.assertTrue(conclusion["缺失分区清单"])
        for item in conclusion["缺失分区清单"]:
            self.assertEqual(item["来源规则编号"], "PART-0002")

    def test_repeated_migration_creates_single_version(self) -> None:
        migration.migrate(self.db)
        second = migration.migrate(self.db)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.updated, 0)
        self.assertEqual(second.skipped, len(LEGACY_RULES))
        rows = self.snapshot_rows()
        self.assertEqual(len(rows), len(LEGACY_RULES))
        self.assertEqual(
            len({(row["rule_code"], row["snapshot_version"]) for row in rows}),
            len(LEGACY_RULES),
        )
        self.assertTrue(migration.verify(self.db)["ok"])

    def test_resume_after_failure_starts_from_last_complete_rule(self) -> None:
        first = migration.migrate(self.db, fail_rule_codes=["PART-0003"])
        self.assertEqual(first.failed, 1)
        self.assertEqual(first.last_completed_rule, "PART-0002")
        self.assertEqual(first.inserted, 2)
        self.assertEqual({row["rule_code"] for row in self.snapshot_rows()}, {"PART-0001", "PART-0002"})

        resumed = migration.migrate(self.db)
        self.assertEqual(resumed.resumed_from, "PART-0002")
        self.assertEqual(resumed.failed, 0)
        self.assertEqual(resumed.skipped, 2)
        self.assertEqual(resumed.inserted, len(LEGACY_RULES) - 2)
        self.assertEqual(len(self.snapshot_rows()), len(LEGACY_RULES))
        self.assertTrue(migration.verify(self.db)["ok"])

    def test_interrupted_migration_still_readable_by_check_task(self) -> None:
        migration.migrate(self.db, stop_after=1)
        rows = repo.load_partitioncheck_rows(self.db)
        by_code = {row["规则编号"]: row for row in rows}
        self.assertEqual(by_code["PART-0001"]["兼容标记"], pr.COMPAT_EQUIVALENT)
        # 未迁移的旧记录仍被读到，并以待确认/数量口径兜底。
        self.assertEqual(by_code["PART-0002"]["兼容标记"], pr.COMPAT_PENDING)
        self.assertEqual(by_code["PART-0002"]["迁移状态"], "待迁移")
        results = {item["规则编号"]: item for item in pr.build_conclusions(rows)}
        self.assertEqual(results["PART-0002"]["缺失分区数"], 5)
        self.assertEqual(results["PART-0002"]["分区完整性结论"], pr.INTEGRITY_MISSING)

    def test_metastore_syncs_partition_field_from_snapshot(self) -> None:
        migration.migrate(self.db)
        partition_rows = repo.load_partitioncheck_rows(self.db)
        metastore_rows = repo.load_metastore_rows(self.db)
        conclusions = pr.build_conclusions(partition_rows)
        apply_to_metastore(metastore_rows, conclusions)
        by_table = {row["表名"]: row for row in metastore_rows}
        self.assertEqual(by_table["dwd_order_daily"]["分区字段"], pr.PARTITION_FIELD)
        self.assertEqual(by_table["dwd_order_daily"]["日期键"], pr.DATE_KEY)
        self.assertEqual(by_table["dwd_order_daily"]["缺失分区数"], 1)
        self.assertIn("PART-0001", by_table["dwd_order_daily"]["分区规则编号"])
        report = consistency_report(partition_rows, metastore_rows)
        self.assertTrue(report["ok"], report["mismatches"])

    def test_consistency_report_detects_field_mismatch(self) -> None:
        migration.migrate(self.db)
        partition_rows = repo.load_partitioncheck_rows(self.db)
        metastore_rows = repo.load_metastore_rows(self.db)
        metastore_rows[0]["日期键"] = "ds"
        report = consistency_report(partition_rows, metastore_rows)
        self.assertFalse(report["ok"])
        self.assertEqual(report["mismatches"][0]["数据表名"], "dwd_order_daily")

    def test_sample_data_and_migration_share_date_key(self) -> None:
        migration.migrate(self.db)
        sample = {row["规则编号"]: row for row in build_partitioncheck_rows()}
        migrated = {row["rule_code"]: row for row in self.snapshot_rows()}
        for code in migrated:
            self.assertEqual(migrated[code]["date_key"], sample[code]["日期键"])
            self.assertEqual(migrated[code]["partition_field"], sample[code]["分区字段"])
            self.assertEqual(migrated[code]["compat_mode"], sample[code]["兼容标记"])


class LegacyCompatibilityTest(unittest.TestCase):
    def test_date_key_expands_window_values(self) -> None:
        values = pr.date_key_values("2026-11-16", 7)
        self.assertEqual(len(values), 7)
        self.assertEqual(values[0], "2026-11-10")
        self.assertEqual(values[-1], "2026-11-16")

    def test_legacy_rule_without_date_key_is_pending(self) -> None:
        rule = {
            "规则编号": "PART-OLD",
            "数据表名": "unknown_table",
            "期望分区数": 7,
            "实际分区数": 2,
        }
        snapshot = pr.build_legacy_snapshot(
            rule, metastore_by_table={}, current_date_rules=[]
        )
        self.assertEqual(snapshot["兼容标记"], pr.COMPAT_PENDING)
        conclusion = pr.evaluate_rule(snapshot)
        self.assertEqual(conclusion["缺失分区数"], 5)
        self.assertEqual(conclusion["来源规则编号"], "PART-OLD")


if __name__ == "__main__":
    unittest.main()
