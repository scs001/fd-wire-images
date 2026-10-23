"""catalog 迁移链测试：upgrade head 产出 source_db/unit 列与按源索引（临时 sqlite）。

镜像表换源 list_registry_entries（indicator-caliber-unification 任务 2.1）：
source_db 独立成列（按源过滤与 stats GROUP BY，不解析 ID）、unit 直映落列；
存量旧行可空起步，由换源后的首次全量重刷一次性退役。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

API_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def migrated_sqlite(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade head（DATABASE_URL 指路），返回连接串。"""
    db_url = f"sqlite:///{tmp_path}/catalog_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "head")
    return db_url


def test_upgrade_head_adds_source_db_and_unit(migrated_sqlite) -> None:
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("indicator_snapshots")}
    assert "source_db" in columns
    assert "unit" in columns
    # 可空起步（存量旧行不回填）
    assert columns["source_db"]["nullable"] is True
    assert columns["unit"]["nullable"] is True
    # source_db 独立索引（按源过滤 / stats GROUP BY）
    index_names = {ix["name"] for ix in inspector.get_indexes("indicator_snapshots")}
    assert "ix_indicator_snapshots_source_db" in index_names
    engine.dispose()


def test_chain_head_follows_catalog_source_db(migrated_sqlite) -> None:
    """revision 顺接链头：0010 之后由 0011_datasets 接续（wire-customer-local-data 2.1）。"""
    engine = create_engine(migrated_sqlite)
    with engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    # 链头断言随最新迁移推进（0038_artifact_drafts：doc 草稿层表，
    # wire-rich-text-docs——单链线性追加）
    assert version == "0040_artifact_source_ref"
    engine.dispose()


def test_downgrade_removes_source_db_and_unit(migrated_sqlite, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_sqlite)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    # 明确回退到 0010 的下游（链头之上后续迁移只增不改，-1 已不再是 0010）
    command.downgrade(cfg, "0009_materialization")
    engine = create_engine(migrated_sqlite)
    columns = {c["name"] for c in inspect(engine).get_columns("indicator_snapshots")}
    assert "source_db" not in columns
    assert "unit" not in columns
    engine.dispose()
