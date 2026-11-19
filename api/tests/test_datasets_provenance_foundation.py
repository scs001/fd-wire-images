"""不可变登记基线的行为层验证（wire-provenance-ledger 任务组 1）。

覆盖契约 wire-customer-datasets 增量「不可变登记基线」的同步历史侧：

- 触发器（迁移 0013）在真库上的行为：``syncing`` 行允许 UPDATE 到终态
  （syncing→aborted 是唯一放行的改写），终态行 UPDATE 被阻断、任何行
  DELETE 被阻断（历史 append-only）；
- 删除数据集后同步历史行**保留**（孤儿化由设计接受——行内无客户数据；
  若仍走 DELETE 会被触发器 RAISE）。

哈希侧（uploads.content_sha256）见 test_datasets_uploads.py。
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.models import Dataset, DatasetSync
from test_datasets_common import (
    CUSTOMER_A,
    create_connector_dataset,
    datasets_env,  # noqa: F401 - pytest fixture
    pair_connector,
    push_sync,
)

API_DIR = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


@pytest.fixture
def migrated_sqlite(tmp_path, monkeypatch):
    """干净库跑全链迁移（含 0013 终态触发器）——触发器行为按真库验。"""
    db_url = f"sqlite:///{tmp_path}/datasets_trigger.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(_alembic_config(), "head")
    return db_url


def _insert_sync_row(db_url: str, *, status: str) -> str:
    """直插一行 dataset + 一行 dataset_syncs（绕开 ORM 默认，指定 status）。"""
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO datasets (id, customer_id, key, name, source_kind,"
                " columns_json, row_count, size_bytes, sync_status,"
                " provenance_tier, created_at, updated_at)"
                " VALUES ('ds-1', 'cust-001', 't1', 'T', 'connector', '[]',"
                " 0, 0, 'idle', 'chain_only',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO dataset_syncs (id, dataset_id, status, rows,"
                " started_at)"
                " VALUES ('sync-1', 'ds-1', :status, 0, CURRENT_TIMESTAMP)"
            ),
            {"status": status},
        )
    engine.dispose()
    return "sync-1"


def test_trigger_allows_syncing_row_to_reach_terminal_state(migrated_sqlite) -> None:
    """syncing → aborted（含原因/时间）是触发器放行的唯一改写路径。"""
    _insert_sync_row(migrated_sqlite, status="syncing")
    engine = create_engine(migrated_sqlite)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dataset_syncs SET status = 'aborted',"
                " error = '同步被连接器中止（abort）',"
                " finished_at = CURRENT_TIMESTAMP"
                " WHERE id = 'sync-1'"
            )
        )
        row = conn.execute(
            text("SELECT status, error FROM dataset_syncs WHERE id = 'sync-1'")
        ).fetchone()
    assert row.status == "aborted"
    assert "中止" in row.error
    engine.dispose()


def test_trigger_blocks_update_of_terminal_rows(migrated_sqlite) -> None:
    _insert_sync_row(migrated_sqlite, status="aborted")
    engine = create_engine(migrated_sqlite)
    with pytest.raises(IntegrityError, match="immutable"):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE dataset_syncs SET error = 'x' WHERE id = 'sync-1'")
            )
    engine.dispose()


def test_trigger_blocks_delete_of_any_sync_row(migrated_sqlite) -> None:
    """历史行禁删（含在飞行——删除本身即消除失败记录的路径）。"""
    _insert_sync_row(migrated_sqlite, status="syncing")
    engine = create_engine(migrated_sqlite)
    with pytest.raises(IntegrityError, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM dataset_syncs WHERE id = 'sync-1'"))
    engine.dispose()


def test_delete_dataset_keeps_sync_history_rows(datasets_env) -> None:
    """删数据集：datasets 行删、dataset_syncs 行保留（孤儿化由设计接受）。"""
    paired = pair_connector(datasets_env)
    dataset = create_connector_dataset(datasets_env, connector_id=paired["connector_id"])
    result = push_sync(
        datasets_env,
        connector_key=paired["connector_key"],
        dataset_key=dataset["key"],
        columns=[{"name": "id", "type": "INTEGER"}],
        rows=[{"id": 1}],
    )
    assert result["commit"].status_code == 200, result["commit"].text

    deleted = datasets_env.client.delete(
        f"/api/v1/datasets/{dataset['id']}", headers=CUSTOMER_A
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}

    session = get_session()
    try:
        assert session.get(Dataset, dataset["id"]) is None
        rows = (
            session.query(DatasetSync)
            .filter(DatasetSync.dataset_id == dataset["id"])
            .all()
        )
        # 同步历史保留（含 ok 终态行）——不再随数据集删除
        assert [r.status for r in rows] == ["ok"]
    finally:
        session.close()
