"""运行配置：端口、跨域、运行环境与旧数据库位置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _partition_db_path() -> str | None:
    raw = os.environ.get("PARTITION_DB_PATH") or os.environ.get("APP_DB_PATH")
    return raw.strip() if raw and raw.strip() else None


@dataclass(frozen=True)
class Settings:
    app_name: str = "数据仓库ETL任务编排平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 旧数据库/迁移产物位置；为空时使用内置示例数据。
    partition_db_path: str | None = field(default_factory=_partition_db_path)


settings = Settings()
