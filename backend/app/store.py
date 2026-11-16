"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
分区检测与元数据存储额外支持从 ``PARTITION_DB_PATH`` 指向的旧数据库/迁移产物
加载：检测任务读到的是日期键快照，元数据存储跟随同一快照更新分区字段。
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.partition_analysis import apply_to_metastore, evaluated_rules
from app.partition_repository import load_metastore_rows, load_partitioncheck_rows
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._partition_source = "内置示例数据"
        self._load_partition_database()
        self.refresh_partition_conclusions()

    def _load_partition_database(self) -> None:
        """有旧库/迁移产物时优先读取；读不到就继续用内置示例数据。"""
        db_path = settings.partition_db_path
        if not db_path:
            return
        partition_rows = load_partitioncheck_rows(db_path)
        if partition_rows:
            self._tables["partitioncheck"] = partition_rows
            self._partition_source = f"数据库 {db_path}"
        metastore_rows = load_metastore_rows(db_path)
        if metastore_rows:
            existing = {str(row.get("表名")): row for row in metastore_rows}
            merged: list[dict[str, Any]] = []
            for row in self._tables.get("metastore", []):
                table_name = str(row.get("表名"))
                db_row = existing.pop(table_name, None)
                if db_row is not None:
                    # 旧库只提供表结构口径，非空字段覆盖，其余保留示例展示值。
                    for key, value in db_row.items():
                        if key in {"id", "status", "pending", "abnormal"}:
                            continue
                        if value not in (None, "", 0):
                            row[key] = value
                merged.append(row)
            merged.extend(existing.values())
            self._tables["metastore"] = merged

    def partition_source(self) -> str:
        return self._partition_source

    def refresh_partition_conclusions(self) -> list[dict[str, Any]]:
        """重算分区检测结论，并同步到分区检测行和元数据存储清单。

        分区检测与元数据存储都从这里取数，保证两个模块读同一日期键和分区字段。
        """
        partition_rows = self._tables.setdefault("partitioncheck", [])
        metastore_rows = self._tables.setdefault("metastore", [])
        conclusions = evaluated_rules(partition_rows)
        by_rule = {
            str(item.get("规则编号")): item for item in conclusions
        }
        for row in partition_rows:
            conclusion = by_rule.get(str(row.get("规则编号")))
            if conclusion is None:
                continue
            row["日期键"] = conclusion.get("日期键") or row.get("日期键")
            row["分区字段"] = conclusion.get("分区字段") or row.get("分区字段")
            row["缺失分区清单"] = conclusion.get("缺失分区清单") or []
            row["缺失分区数"] = conclusion.get("缺失分区数", 0)
            row["分区完整性结论"] = conclusion.get("分区完整性结论")
            row["结论版本"] = conclusion.get("结论版本") or conclusion.get(
                "快照版本", 1
            )
        apply_to_metastore(metastore_rows, conclusions)
        return conclusions

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        self.refresh_partition_conclusions()
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
