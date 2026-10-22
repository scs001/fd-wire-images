"""PG 方言冒烟矩阵（wire-dedicated-postgres 任务 3.4）。

两档运行（design D5——双方言靠测试矩阵保证，不靠开发也用 PG）：

1. ``PG_TEST_URL`` 环境变量存在（指向可弃测试库，例如
   ``postgres://wire:wire@localhost:5433/wire_smoke``）：真连 PostgreSQL，
   对全部模块 metadata 做 create_all + 代表表 CRUD 回读 + drop_all。
2. 未设：对全部模型做 create/drop 的 PG 方言**编译断言**（SQLAlchemy
   ``postgresql`` 方言），外加双方言周期表达式（strftime/to_char）编译
   断言——不连任何数据库。

导入 datasets/ai 等并行切片模型仅为读取其 metadata（编译断言覆盖其新表；
源码不由本测试维护）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable, DropTable

from fd_wire_api.config import normalize_database_url

# ---- 全部模块的 metadata（导入即注册 ORM 映射）--------------------------
from fd_wire_api.metering.db import MeteringBase  # noqa: F401
from fd_wire_api.metering import models as _metering_models  # noqa: F401
from fd_wire_api.catalog.models import Base as CatalogBase  # noqa: F401
from fd_wire_api.demands.db import Base as DemandsBase  # noqa: F401
from fd_wire_api.billing.models import Base as BillingBase  # noqa: F401
from fd_wire_api.admin.db import AdminBase  # noqa: F401
from fd_wire_api.console.models import ConsoleBase  # noqa: F401
from fd_wire_api.materialization.db import MatBase  # noqa: F401
from fd_wire_api.ai.db import AiBase  # noqa: F401
from fd_wire_api.ai import models as _ai_models  # noqa: F401
from fd_wire_api.datasets.db import DatasetsBase  # noqa: F401
from fd_wire_api.datasets import models as _datasets_models  # noqa: F401
from fd_wire_api.billing.metering_source import metering_records  # 共享读取契约表

ALL_METADATA: list[tuple[str, object]] = [
    ("metering", MeteringBase.metadata),
    ("catalog", CatalogBase.metadata),
    ("demands", DemandsBase.metadata),
    ("billing", BillingBase.metadata),
    ("admin", AdminBase.metadata),
    ("console", ConsoleBase.metadata),
    ("materialization", MatBase.metadata),
    ("ai", AiBase.metadata),
    ("datasets", DatasetsBase.metadata),
    ("billing.metering_records", metering_records.metadata),
]

PG_TEST_URL = os.getenv("PG_TEST_URL")


def _pg_dialect():
    return postgresql.dialect()


# ---------------------------------------------------------------------------
# 档 2（默认）：PG 方言编译断言——create/drop 全模型
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "metadata"), ALL_METADATA, ids=[m[0] for m in ALL_METADATA])
def test_create_drop_compiles_on_postgres(label: str, metadata) -> None:
    """全部模型的 CreateTable/DropTable 在 postgresql 方言下可编译。"""
    dialect = _pg_dialect()
    tables = metadata.sorted_tables
    assert tables, f"{label} metadata 无表——导入注册可能失败"
    for table in tables:
        create_sql = str(CreateTable(table).compile(dialect=dialect)).strip()
        assert create_sql.upper().startswith("CREATE TABLE"), table.name
        assert "AUTOINCREMENT" not in create_sql.upper(), (
            f"{table.name}: SQLite 专有 AUTOINCREMENT 漏进 PG 方言"
        )
        drop_sql = str(DropTable(table).compile(dialect=dialect)).strip()
        assert drop_sql.upper().startswith("DROP TABLE"), table.name


def test_period_expression_compiles_on_both_dialects() -> None:
    """周期派生双方言：SQLite strftime / PG to_char（dbcompat 审计项回归）。"""
    from fd_wire_api.dbcompat import period_expression
    from fd_wire_api.metering.models import MeteringRecord

    pg_engine = create_engine("postgresql+psycopg://u:p@localhost/smfake")
    sqlite_engine = create_engine("sqlite://")

    pg_sql = str(
        select(period_expression(pg_engine, MeteringRecord.event_timestamp)).compile(
            dialect=pg_engine.dialect
        )
    )
    assert "to_char(" in pg_sql and "strftime" not in pg_sql

    sqlite_sql = str(
        select(period_expression(sqlite_engine, MeteringRecord.event_timestamp)).compile(
            dialect=sqlite_engine.dialect
        )
    )
    assert "strftime(" in sqlite_sql and "to_char" not in sqlite_sql


def test_pg_url_normalization_from_env_shape() -> None:
    """部署常见 PG URL 形态经 normalize 后可创建 psycopg3 引擎（不连库）。"""
    for raw in (
        "postgres://u:p@localhost:5432/wire_platform",
        "postgresql://u:p@localhost:5432/wire_platform",
    ):
        engine = create_engine(normalize_database_url(raw))
        assert isinstance(engine, Engine)
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"


# ---------------------------------------------------------------------------
# 档 1（PG_TEST_URL 存在）：真连建表 + 代表表 CRUD + drop
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PG_TEST_URL, reason="PG_TEST_URL 未配置——仅方言编译断言")
@pytest.mark.parametrize(("label", "metadata"), ALL_METADATA, ids=[m[0] for m in ALL_METADATA])
def test_pg_live_create_crud_drop(label: str, metadata) -> None:
    """真连 PG：create_all → 代表表 CRUD 回读 → drop_all（测试库可弃）。"""
    engine = create_engine(normalize_database_url(PG_TEST_URL))
    try:
        metadata.create_all(engine)
    finally:
        metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.skipif(not PG_TEST_URL, reason="PG_TEST_URL 未配置——仅方言编译断言")
def test_pg_live_representative_crud_roundtrip() -> None:
    """代表表 CRUD：覆盖类型空间（timestamptz/int/str/bool/JSON/Numeric/Text）。

    - metering.MeteringRecord：timestamptz + 唯一约束 + 索引
    - billing.Plan：Boolean + JSON 列（布尔值经 SQLAlchemy Boolean 抽象，
      规避 PG 无布尔聚合/字面量差异）
    - demands.Demand：JSON list/dict 列 + Numeric + Text
    """
    from fd_wire_api.billing.models import Plan
    from fd_wire_api.demands.models import Demand
    from fd_wire_api.metering.models import MeteringRecord

    engine = create_engine(normalize_database_url(PG_TEST_URL))
    try:
        MeteringBase.metadata.create_all(engine)
        DemandsBase.metadata.create_all(engine)
        BillingBase.metadata.create_all(engine)

        stamp = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        with engine.begin() as conn:
            rec_id = conn.execute(
                insert(MeteringRecord).returning(MeteringRecord.id),
                {
                    "event_timestamp": stamp,
                    "request_id": "req-pg-smoke",
                    "caller": "pg-smoke",
                    "credential_track": "customer",
                    "server_name": "pg-smoke-server",
                    "rows_returned": 7,
                    "source": "customer",
                },
            ).scalar_one()

            demand_id = conn.execute(
                insert(Demand),
                {
                    "code": "DE-PGSMOKE-01",
                    "customer_id": "pg-smoke-cust",
                    "title": "PG 冒烟",
                    "intent": "双方言 CRUD 冒烟",
                    "linked_indicators": ["ind-1", "ind-2"],
                    "status": "submitted",
                    "status_history": [{"status": "submitted", "at": "2026-10-01T00:00:00Z"}],
                    "entities_hint": {"region": "cn"},
                    "quote_amount": None,
                },
            ).inserted_primary_key[0]

            plan_id = conn.execute(
                insert(Plan),
                {
                    "code": "pg-smoke-plan",
                    "name": "PG 冒烟档",
                    "monthly_price_micro": 0,
                    "included_lines": 100,
                    "overage_unit_price_micro": 4000,
                    "is_active": True,
                    "is_public": False,
                    "limits_json": {"storage_bytes": 1024, "max_dataset_rows": 10},
                },
            ).inserted_primary_key[0]

        with engine.begin() as conn:
            # 回读断言（JSON 列经 SQLAlchemy 抽象往返等值）
            rec = conn.execute(
                select(MeteringRecord).where(MeteringRecord.id == rec_id)
            ).scalar_one()
            assert rec.rows_returned == 7
            assert rec.event_timestamp == stamp

            demand = conn.execute(
                select(Demand).where(Demand.id == demand_id)
            ).scalar_one()
            assert demand.linked_indicators == ["ind-1", "ind-2"]
            assert demand.status_history[0]["status"] == "submitted"
            assert demand.entities_hint == {"region": "cn"}

            plan = conn.execute(select(Plan).where(Plan.id == plan_id)).scalar_one()
            assert plan.is_active is True
            assert plan.limits_json == {"storage_bytes": 1024, "max_dataset_rows": 10}

        with engine.begin() as conn:
            # U/D
            conn.execute(
                update(MeteringRecord).where(MeteringRecord.id == rec_id).values(rows_returned=9)
            )
            assert (
                conn.execute(
                    delete(MeteringRecord).where(MeteringRecord.id == rec_id)
                ).rowcount
                == 1
            )
            conn.execute(delete(Demand).where(Demand.id == demand_id))
            conn.execute(delete(Plan).where(Plan.id == plan_id))
    finally:
        # 反向 drop（依赖顺序）
        for metadata in (
            DemandsBase.metadata,
            BillingBase.metadata,
            MeteringBase.metadata,
        ):
            metadata.drop_all(engine)
        engine.dispose()
