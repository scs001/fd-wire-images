"""Alembic 迁移链测试：upgrade head 产出计量表与唯一约束（临时 sqlite）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from fd_wire_api.metering.db import to_utc
from fd_wire_api.metering.models import MeteringAnomaly, MeteringRecord

from test_metering_support import make_event

from datetime import datetime

API_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def migrated_sqlite(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade head（DATABASE_URL 指路），返回连接串。"""
    db_url = f"sqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "head")
    return db_url


def _unique_index_columns(engine, table: str) -> list[set[str]]:
    """PRAGMA 层取该表全部唯一索引的列集合（sqlite 把 UNIQUE 约束实现为
    ``sqlite_autoindex_*``，inspector 不按约束名报告，须按列语义断言）。"""
    with engine.connect() as conn:
        indexes = conn.exec_driver_sql(f"PRAGMA index_list({table})").fetchall()
        result = []
        for _, name, is_unique, *_ in indexes:
            if not is_unique:
                continue
            cols = conn.exec_driver_sql(f"PRAGMA index_info({name})").fetchall()
            result.append({row[2] for row in cols})
        return result


def test_upgrade_creates_metering_tables(migrated_sqlite) -> None:
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"metering_records", "metering_anomalies", "alembic_version"} <= tables

    records_columns = {c["name"] for c in inspector.get_columns("metering_records")}
    assert records_columns == {
        "id", "event_timestamp", "request_id", "caller", "caller_principal",
        "credential_track", "server_name", "server_path", "tool_name",
        "rows_returned", "source", "created_at",
    }
    # 幂等唯一约束：request_id + server_name（sqlite 下为 autoindex，按列断言）
    assert {"request_id", "server_name"} in _unique_index_columns(engine, "metering_records")
    # 异常留痕幂等：request_id + server_name + reason
    assert {"request_id", "server_name", "reason"} in _unique_index_columns(
        engine, "metering_anomalies"
    )
    engine.dispose()


def test_unique_constraint_rejects_duplicate_request_and_server(migrated_sqlite) -> None:
    engine = create_engine(migrated_sqlite)
    from sqlalchemy.orm import Session

    event = make_event(request_id="dup-1", rows=10)
    ts = to_utc(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")))
    with Session(engine) as session:
        session.add(_record_from_event(event, ts))
        session.commit()
        session.add(_record_from_event(event, ts))  # 同 request_id + server
        with pytest.raises(IntegrityError):
            session.commit()
    with engine.connect() as conn:  # 唯一约束在库层生效
        assert conn.execute(text("SELECT COUNT(*) FROM metering_records")).scalar_one() == 1
    engine.dispose()


def test_downgrade_drops_tables(migrated_sqlite, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_sqlite)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.downgrade(cfg, "base")
    engine = create_engine(migrated_sqlite)
    assert "metering_records" not in inspect(engine).get_table_names()
    assert "metering_anomalies" not in inspect(engine).get_table_names()
    engine.dispose()


def _record_from_event(event: dict, ts) -> MeteringRecord:
    return MeteringRecord(
        event_timestamp=ts,
        request_id=event["request_id"],
        caller=event["identity"]["username"],
        credential_track="customer",
        server_name=event["mcp_server"]["name"],
        tool_name=event["mcp_request"]["tool_name"],
        rows_returned=10,
        source="customer",
    )
