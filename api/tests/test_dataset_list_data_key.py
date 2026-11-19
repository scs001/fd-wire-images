"""数据集列表双轨凭据（add-data-key-dataset-list）api 侧测试。

覆盖 spec 场景（openspec/changes/add-data-key-dataset-list/specs）：

- ``wdk-`` key 轨道可列本客户数据集，且看不到别家；
- allowlist 非空收窄，restricted 不因成员维度被隐藏（D6 边界）；
- console 轨道零漂移（restricted 仍按成员授权过滤）；
- 无凭据 401（不是 200 空列表）。

环境照 test_dataset_acl 先例：单引擎内存库 + 平台库临时文件 + 假 JWKS。
"""

from __future__ import annotations

import os

os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app  # noqa: F401 —— 先经入口（跨切片环解析序先例）

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.billing.models import Customer
from fd_wire_api.console.models import CustomerMember
from fd_wire_api.data_plane import service as key_service
from fd_wire_api.datasets.db import (
    DatasetsBase,
    get_db as get_datasets_db,
    reset_datasets_runtime,
)
from fd_wire_api.datasets.ingest import get_engine_client_factory
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

OWNER_SUB = "sub-list-owner"
VIEWER_SUB = "sub-list-viewer"
OTHER_SUB = "sub-list-other"

CUST = "cust-list"
OTHER_CUST = "cust-list-other"

OWNER = bearer(mint_id_token(OWNER_SUB))
VIEWER = bearer(mint_id_token(VIEWER_SUB))


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AiBase.metadata.create_all(bind=engine)
    EntitiesBase.metadata.create_all(bind=engine)
    DatasetsBase.metadata.create_all(bind=engine)
    import fd_wire_api.ai.models  # noqa: F401 —— 注册全部 AiBase 映射

    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def env(monkeypatch, tmp_path, db_session):
    monkeypatch.setenv("MATERIALIZATION_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db")
    monkeypatch.setenv("MATERIALIZATION_AUTO_CREATE_TABLES", "1")
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(OWNER_SUB, CUST), (OTHER_SUB, OTHER_CUST)],
        plan_code="basic",
    )
    from fd_wire_api.admin.db import get_session as platform_session

    with platform_session() as sess:
        customer = sess.execute(
            select(Customer).where(Customer.name == CUST)
        ).scalar_one()
        sess.add(
            CustomerMember(customer_id=customer.id, logto_sub=VIEWER_SUB, role="viewer")
        )
        sess.commit()

    def override_get_db():
        yield db_session

    reset_ai_runtime()
    reset_datasets_runtime()
    key_service.reset_runtime()
    app.dependency_overrides[get_ai_db] = override_get_db
    app.dependency_overrides[get_entities_db] = override_get_db
    app.dependency_overrides[get_datasets_db] = override_get_db
    # 列表路径零引擎调用；工厂仍覆写以免默认实现被触达
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: None
    )
    with TestClient(app) as client:
        yield {"client": client, "db": db_session}
    app.dependency_overrides.clear()
    key_service.reset_runtime()
    reset_datasets_runtime()
    reset_ai_runtime()


def seed_dataset(db, *, key: str, customer: str = CUST, acl_mode: str | None = None) -> Dataset:
    dataset = Dataset(
        customer_id=customer,
        key=key,
        name=key,
        source_kind="upload",
        storage_tier="api",
        columns_json=[{"name": "id", "type": "TEXT"}],
        row_count=1,
    )
    if acl_mode is not None:
        dataset.acl_mode = acl_mode
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def mint_key(db, *, customer: str = CUST, dataset_ids: list[str] | None = None) -> str:
    _, plaintext = key_service.mint(
        db, customer_id=customer, label="list-test", dataset_ids=dataset_ids
    )
    return plaintext


def key_headers(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def list_ids(client, headers) -> set[str]:
    resp = client.get("/api/v1/datasets", headers=headers)
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


# ---------------------------------------------------------------- key 轨道


def test_key_lists_own_customer_only(env):
    client, db = env["client"], env["db"]
    mine = seed_dataset(db, key="mine")
    theirs = seed_dataset(db, key="theirs", customer=OTHER_CUST)

    plaintext = mint_key(db)
    ids = list_ids(client, key_headers(plaintext))

    assert mine.id in ids
    assert theirs.id not in ids


def test_key_allowlist_narrows(env):
    client, db = env["client"], env["db"]
    allowed = seed_dataset(db, key="allowed")
    excluded = seed_dataset(db, key="excluded")

    plaintext = mint_key(db, dataset_ids=[allowed.id])
    ids = list_ids(client, key_headers(plaintext))

    assert ids == {allowed.id}
    assert excluded.id not in ids


def test_key_sees_restricted_dataset(env):
    """D6 边界：key 无成员身份可判，restricted 不因成员维度被隐藏。"""
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret", acl_mode="restricted")

    plaintext = mint_key(db)
    ids = list_ids(client, key_headers(plaintext))

    assert restricted.id in ids


def test_empty_allowlist_means_all(env):
    client, db = env["client"], env["db"]
    a = seed_dataset(db, key="a")
    db_id = seed_dataset(db, key="b")

    plaintext = mint_key(db)
    ids = list_ids(client, key_headers(plaintext))

    assert ids == {a.id, db_id.id}


def test_revoked_key_401(env):
    client, db = env["client"], env["db"]
    seed_dataset(db, key="mine")
    plaintext = mint_key(db)
    row = db.execute(
        select(Dataset.customer_id).where(Dataset.customer_id == CUST)
    ).first()
    assert row is not None

    from fd_wire_api.datasets.models import DataPlaneKey

    key_row = db.execute(select(DataPlaneKey)).scalars().one()
    key_service.revoke(db, customer_id=CUST, key_id=key_row.id)

    resp = client.get("/api/v1/datasets", headers=key_headers(plaintext))
    assert resp.status_code == 401
    assert "已吊销" in resp.json()["detail"]


# ---------------------------------------------------------------- console 轨道


def test_console_restricted_still_filtered(env):
    """console 轨道零漂移：未授权 viewer 的列表不含 restricted 行。"""
    client, db = env["client"], env["db"]
    team = seed_dataset(db, key="team")
    restricted = seed_dataset(db, key="secret", acl_mode="restricted")

    ids = list_ids(client, VIEWER)
    assert team.id in ids
    assert restricted.id not in ids


def test_console_list_unaffected(env):
    client, db = env["client"], env["db"]
    mine = seed_dataset(db, key="mine")
    theirs = seed_dataset(db, key="theirs", customer=OTHER_CUST)

    ids = list_ids(client, OWNER)
    assert mine.id in ids
    assert theirs.id not in ids


# ---------------------------------------------------------------- 无凭据


def test_no_credential_is_401_not_empty(env):
    client = env["client"]
    resp = client.get("/api/v1/datasets")
    assert resp.status_code == 401


# ---------------------------------------------------------------- 行读面（rows）


def rows_resp(client, dataset_id, headers):
    return client.get(f"/api/v1/datasets/{dataset_id}/rows", headers=headers)


def test_key_reads_rows_of_own_dataset(env):
    """key 轨道能读本客户数据集的行（盘账取样的必经面）。"""
    client, db = env["client"], env["db"]
    ds = seed_dataset(db, key="rows-ok")
    key = mint_key(db, dataset_ids=[ds.id])

    resp = rows_resp(client, ds.id, key_headers(key))
    assert resp.status_code == 200, resp.text
    assert "rows" in resp.json()


def test_key_allowlist_blocks_rows_outside(env):
    """allowlist 未列入 → 403 如实说明（不是 404、不是空行集）。"""
    client, db = env["client"], env["db"]
    allowed = seed_dataset(db, key="rows-allowed")
    other = seed_dataset(db, key="rows-other")
    key = mint_key(db, dataset_ids=[allowed.id])

    ok = rows_resp(client, allowed.id, key_headers(key))
    assert ok.status_code == 200, ok.text

    denied = rows_resp(client, other.id, key_headers(key))
    assert denied.status_code == 403, denied.text
    assert "允许清单" in denied.json()["detail"]


def test_key_full_rows_without_allowlist(env):
    """空 allowlist = 全量 key：本客户数据集都可读行。"""
    client, db = env["client"], env["db"]
    ds = seed_dataset(db, key="rows-full")
    key = mint_key(db)

    resp = rows_resp(client, ds.id, key_headers(key))
    assert resp.status_code == 200, resp.text


def test_console_rows_unaffected(env):
    """console 轨道零漂移：owner 照旧可读行。"""
    client, db = env["client"], env["db"]
    ds = seed_dataset(db, key="rows-console")

    resp = rows_resp(client, ds.id, OWNER)
    assert resp.status_code == 200, resp.text


def test_rows_no_credential_401(env):
    client, db = env["client"], env["db"]
    ds = seed_dataset(db, key="rows-anon")

    resp = rows_resp(client, ds.id, {})
    assert resp.status_code == 401
