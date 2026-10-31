"""datasets 迁移链测试（任务 2.1）：干净库 upgrade/downgrade 往返。

- upgrade head → 六表 + api 层两表（wire-free-tier-data-gate 0020）齐全、
  列与索引/唯一约束按契约 §3.2；
- downgrade 到 0010 → 全部数据集表清空、链头回到 0010。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

API_DIR = Path(__file__).resolve().parent.parent

TABLES = {
    "connectors",
    "connector_keys",
    "connector_pairing_tokens",
    "datasets",
    "dataset_syncs",
    "uploads",
    "dataset_columns",
    "dataset_rows",
}


def _alembic_config() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


@pytest.fixture
def migrated_sqlite(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/datasets_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(_alembic_config(), "head")
    return db_url


def test_upgrade_head_creates_dataset_tables(migrated_sqlite) -> None:
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert TABLES <= tables

    assert {c["name"] for c in inspector.get_columns("connectors")} == {
        "id",
        "customer_id",
        "name",
        "version",
        "status",
        "last_heartbeat_at",
        "created_at",
        "revoked_at",
    }
    assert {c["name"] for c in inspector.get_columns("connector_keys")} == {
        "id",
        "connector_id",
        "key_hash",
        "created_at",
        "revoked_at",
    }
    assert {c["name"] for c in inspector.get_columns("connector_pairing_tokens")} == {
        "id",
        "customer_id",
        "token_hash",
        "created_at",
        "expires_at",
        "used_at",
    }
    assert {c["name"] for c in inspector.get_columns("datasets")} == {
        "id",
        "customer_id",
        "key",
        "name",
        "source_kind",
        "storage_tier",
        "connector_id",
        "source_name",
        "source_ref",
        "source_config_json",
        "columns_json",
        "row_count",
        "size_bytes",
        "sync_cron",
        "sync_status",
        "paused_reason",
        "provenance_tier",
        "sync_mode",
        "watermark_column",
        "watermark_value",
        "created_at",
        "updated_at",
    }
    assert {c["name"] for c in inspector.get_columns("dataset_syncs")} == {
        "id",
        "dataset_id",
        "started_at",
        "finished_at",
        "status",
        "rows",
        "error",
        "mode",
    }
    assert {c["name"] for c in inspector.get_columns("uploads")} == {
        "id",
        "customer_id",
        "filename",
        "columns_json",
        "row_count",
        "size_bytes",
        "stored_path",
        "content_sha256",
        "created_at",
        "consumed_at",
    }
    # api 层轻量行存储（wire-free-tier-data-gate 0020）
    assert {c["name"] for c in inspector.get_columns("dataset_columns")} == {
        "id",
        "dataset_id",
        "name",
        "dtype",
        "ordinal",
    }
    assert {c["name"] for c in inspector.get_columns("dataset_rows")} == {
        "id",
        "dataset_id",
        "row_index",
        "data",
    }
    # 关键索引
    assert "ix_datasets_customer_id" in {
        ix["name"] for ix in inspector.get_indexes("datasets")
    }
    assert "ix_dataset_syncs_dataset_id" in {
        ix["name"] for ix in inspector.get_indexes("dataset_syncs")
    }
    assert "ix_connector_keys_key_hash" in {
        ix["name"] for ix in inspector.get_indexes("connector_keys")
    }
    assert "ix_dataset_rows_dataset_id" in {
        ix["name"] for ix in inspector.get_indexes("dataset_rows")
    }
    assert "ix_dataset_columns_dataset_id" in {
        ix["name"] for ix in inspector.get_indexes("dataset_columns")
    }
    # datasets(customer_id, key) 唯一（sqlite 以 autoindex 实现，PRAGMA 按列断言）
    with engine.connect() as conn:
        unique_sets = []
        for _, name, unique, *_ in conn.exec_driver_sql(
            "PRAGMA index_list(datasets)"
        ).fetchall():
            if not unique:
                continue
            cols = conn.exec_driver_sql(f"PRAGMA index_info({name})").fetchall()
            unique_sets.append({row[2] for row in cols})
    assert {"customer_id", "key"} in unique_sets
    # dataset_rows(dataset_id, row_index) 唯一（幂等写保护，0020）
    with engine.connect() as conn:
        unique_sets = []
        for _, name, unique, *_ in conn.exec_driver_sql(
            "PRAGMA index_list(dataset_rows)"
        ).fetchall():
            if not unique:
                continue
            cols = conn.exec_driver_sql(f"PRAGMA index_info({name})").fetchall()
            unique_sets.append({row[2] for row in cols})
    assert {"dataset_id", "row_index"} in unique_sets
    # storage_tier 存量缺省 cell（server_default；「存量不回填」语义的落地形态）
    with engine.connect() as conn:
        tier_default = conn.exec_driver_sql(
            "SELECT dflt_value FROM pragma_table_info('datasets')"
            " WHERE name='storage_tier'"
        ).scalar()
    assert tier_default == "'cell'"
    # 增量同步字段存量缺省（wire-connector-incremental-sync 0025）：sync_mode
    # 与 dataset_syncs.mode 落 snapshot（=现状全量语义）；水位两列可空。
    with engine.connect() as conn:
        mode_default = conn.exec_driver_sql(
            "SELECT dflt_value FROM pragma_table_info('datasets')"
            " WHERE name='sync_mode'"
        ).scalar()
        sync_mode_default = conn.exec_driver_sql(
            "SELECT dflt_value FROM pragma_table_info('dataset_syncs')"
            " WHERE name='mode'"
        ).scalar()
        # pragma_table_info 列序：cid, name, type, notnull, dflt_value, pk
        watermark_notnull = {
            row[1]: bool(row[3])
            for row in conn.exec_driver_sql(
                "SELECT * FROM pragma_table_info('datasets')"
                " WHERE name IN ('watermark_column','watermark_value')"
            ).fetchall()
        }
    assert mode_default == "'snapshot'"
    assert sync_mode_default == "'snapshot'"
    assert watermark_notnull == {"watermark_column": False, "watermark_value": False}
    engine.dispose()


def test_chain_head_is_provenance_foundation(migrated_sqlite) -> None:
    engine = create_engine(migrated_sqlite)
    with engine.connect() as conn:
        version = conn.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar()
    # 链头断言随最新迁移前进（0038_artifact_drafts：doc 草稿层表，
    # wire-rich-text-docs——单链线性追加）
    assert version == "0046_xingyan_content_supply"
    engine.dispose()


def test_incremental_sync_migration_roundtrip(migrated_sqlite, monkeypatch) -> None:
    """0025 幂等可重跑：0024 → 0025 降/升往返后三列一字段仍在、缺省不变。"""
    monkeypatch.setenv("DATABASE_URL", migrated_sqlite)
    command.downgrade(_alembic_config(), "0024_demand_source_public")
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    assert not ({"sync_mode", "watermark_column", "watermark_value"} & {
        c["name"] for c in inspector.get_columns("datasets")
    })
    assert "mode" not in {c["name"] for c in inspector.get_columns("dataset_syncs")}
    engine.dispose()
    command.upgrade(_alembic_config(), "head")
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    assert {"sync_mode", "watermark_column", "watermark_value"} <= {
        c["name"] for c in inspector.get_columns("datasets")
    }
    assert "mode" in {c["name"] for c in inspector.get_columns("dataset_syncs")}
    engine.dispose()


def test_free_tier_migration_roundtrip(migrated_sqlite, monkeypatch) -> None:
    """0020 幂等可重跑（任务 1.1）：0019 → 0020 降/升往返后两表与列仍在。"""
    monkeypatch.setenv("DATABASE_URL", migrated_sqlite)
    command.downgrade(_alembic_config(), "0019_demand_three_state")
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    assert "storage_tier" not in {
        c["name"] for c in inspector.get_columns("datasets")
    }
    assert not ({"dataset_columns", "dataset_rows"} & set(inspector.get_table_names()))
    engine.dispose()
    command.upgrade(_alembic_config(), "0020_free_tier_datasets")
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    assert "storage_tier" in {c["name"] for c in inspector.get_columns("datasets")}
    assert {"dataset_columns", "dataset_rows"} <= set(inspector.get_table_names())
    engine.dispose()


def test_downgrade_removes_dataset_tables(migrated_sqlite, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_sqlite)
    command.downgrade(_alembic_config(), "0010_catalog_source_db")
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    assert not (TABLES & set(inspector.get_table_names()))
    with engine.connect() as conn:
        version = conn.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar()
    assert version == "0010_catalog_source_db"
    engine.dispose()