"""数据集级访问授权（wire-dataset-acl）api 侧全矩阵测试。

覆盖 spec 场景（openspec/changes/wire-dataset-acl/specs/team-spaces/spec.md）：

- 受限数据集对未授权成员不可见（列表过滤 + 详情 403，detail 含「无访问授权」）；
- 授权后可见、撤销后即不可见（无缓存窗口）+ 审计两条变更记录；
- owner 恒可见（授权名单为空亦然）；
- 数据面同闸（console 轨道 query/aggregate/六算子读面；team 模式零变化；
  既有档位 403 文案零漂移）；``wdk-`` data-key 轨道不拦（D6）；
- 写面需 edit 授权（批量写/派生/修正/物化连接；全局角色门禁独立生效）；
- 队内 403 与跨企业 404 区分（含 restricted 者）；
- 零迁移兼容（默认 ``team`` 行为逐字节等价）；
- 管理面 owner-only + 留痕（editor/viewer 403；未知 sub 400 如实；
  每次成功 PUT 一条 append-only 审计行）；
- 实体对象面拦截、结构面（graph / 实体元数据列表）不拦。

环境：单引擎内存库（AiBase+EntitiesBase+DatasetsBase 合库——路由会话
覆写，照 test_entity_objects 先例）；平台库临时文件（console 身份 +
成员表）；引擎假件闭环（FakeEngine）。
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
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db, reset_datasets_runtime
from fd_wire_api.datasets.ingest import (
    TOOL_CORRECTION_ADD,
    TOOL_CORRECTION_LIST,
    TOOL_DELETE,
    TOOL_DERIVE,
    TOOL_LINK,
    TOOL_OVERVIEW,
    TOOL_PREVIEW,
    TOOL_QUERY,
    TOOL_WINDOW,
    TOOL_WRITE_BATCH,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset, DatasetAclEvent, DatasetGrant
from fd_wire_api.entities import service as entities_service
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db
from fd_wire_api.entities.models import Entity
from fd_wire_api.quality.service import TOOL_QUALITY

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

OWNER_SUB = "sub-acl-owner"
VIEWER_SUB = "sub-acl-viewer"
EDITOR_SUB = "sub-acl-editor"
OTHER_SUB = "sub-acl-other"

CUST = "cust-acl"
OTHER_CUST = "cust-acl-other"

OWNER = bearer(mint_id_token(OWNER_SUB))
VIEWER = bearer(mint_id_token(VIEWER_SUB))
EDITOR = bearer(mint_id_token(EDITOR_SUB))
OTHER = bearer(mint_id_token(OTHER_SUB))


class FakeEngine:
    """引擎假件：按工具返回契约形态（记录调用）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.quality_result = {
            "row_count": 2,
            "computed_at": "2026-10-31T00:00:00Z",
            "pk_uniqueness": {"columns": ["id"], "unique_ratio": 1.0},
            "pk_declared": True,
            "null_rates": [{"column": "id", "null_rate": 0.0, "sampled_rows": 2}],
            "sample_limit_hit": False,
        }

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        if tool == TOOL_QUERY:
            return {
                "columns": [{"name": "id", "type": "TEXT"}, {"name": "name", "type": "TEXT"}],
                "rows": [{"id": "1", "name": "a"}],
                "total": 1,
            }
        if tool == TOOL_PREVIEW:
            return {
                "columns": [{"name": "id", "type": "TEXT"}, {"name": "name", "type": "TEXT"}],
                "rows": [{"id": "1", "name": "a", "__row_id": "pk:1"}],
                "total": 1,
            }
        if tool == TOOL_OVERVIEW:
            return {
                "key": arguments["dataset_key"],
                "columns": [{"name": "id", "type": "TEXT"}],
                "rows": 2,
                "size_bytes": 100,
            }
        if tool == TOOL_WRITE_BATCH:
            return {"applied": len(arguments["ops"]), "correction_ids": [1]}
        if tool == TOOL_CORRECTION_ADD:
            return {"correction_id": 1}
        if tool == TOOL_CORRECTION_LIST:
            return {"corrections": []}
        if tool == TOOL_DERIVE:
            return {"dataset_key": arguments["child_key"], "rows": 2}
        if tool == TOOL_WINDOW:
            return {
                "dataset_key": arguments["child_key"],
                "rows": 2,
                "kind": "window",
            }
        if tool == TOOL_LINK:
            return {"rows": 2}
        if tool == TOOL_DELETE:
            return {"deleted": True}
        if tool == TOOL_QUALITY:
            return {"success": True, **dict(self.quality_result)}
        raise AssertionError(f"FakeEngine 未实现的工具: {tool}")


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
    """三库会话覆写 + 平台库（含成员）+ 引擎假件 + TestClient。"""
    monkeypatch.setenv("MATERIALIZATION_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db")
    monkeypatch.setenv("MATERIALIZATION_AUTO_CREATE_TABLES", "1")
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(OWNER_SUB, CUST), (OTHER_SUB, OTHER_CUST)],
        plan_code="basic",
    )
    # 团队成员（owner 不入成员表——owner 语义在解析层合成）
    from fd_wire_api.admin.db import get_session as platform_session

    with platform_session() as sess:
        customer = sess.execute(
            select(Customer).where(Customer.name == CUST)
        ).scalar_one()
        sess.add_all(
            [
                CustomerMember(
                    customer_id=customer.id, logto_sub=VIEWER_SUB, role="viewer"
                ),
                CustomerMember(
                    customer_id=customer.id, logto_sub=EDITOR_SUB, role="editor"
                ),
            ]
        )
        sess.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    reset_datasets_runtime()
    key_service.reset_runtime()
    app.dependency_overrides[get_ai_db] = override_get_db
    app.dependency_overrides[get_entities_db] = override_get_db
    app.dependency_overrides[get_datasets_db] = override_get_db

    fake = FakeEngine()
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: fake
    )
    with TestClient(app) as client:
        yield {
            "client": client,
            "db": db_session,
            "fake": fake,
        }
    app.dependency_overrides.clear()
    key_service.reset_runtime()
    reset_datasets_runtime()
    reset_ai_runtime()


# ------------------------------------------------------------------ 播种助手


def seed_dataset(
    db,
    *,
    key: str,
    customer: str = CUST,
    tier: str = "cell",
    acl_mode: str | None = None,
    name: str | None = None,
    columns: tuple[tuple[str, str], ...] = (("id", "TEXT"), ("name", "TEXT")),
) -> Dataset:
    dataset = Dataset(
        customer_id=customer,
        key=key,
        name=name or key,
        source_kind="upload",
        storage_tier=tier,
        columns_json=[{"name": c, "type": t} for c, t in columns],
        row_count=2,
    )
    if acl_mode is not None:
        dataset.acl_mode = acl_mode
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def add_grant(db, dataset: Dataset, sub: str, permission: str) -> None:
    db.add(
        DatasetGrant(
            dataset_id=dataset.id,
            logto_sub=sub,
            permission=permission,
            created_by_sub=OWNER_SUB,
        )
    )
    db.commit()


def seed_entity(db, dataset: Dataset, name: str = "客户", *, display: str = "name") -> Entity:
    entity = entities_service.create_entity(
        db,
        db,
        customer_id=dataset.customer_id,
        name=name,
        dataset_id=dataset.id,
        pk_columns=["id"],
        display_column=display,
    )
    db.commit()
    return entity


def seed_relation(db, source_entity: Entity, target_entity: Entity, *, from_column: str):
    rel = entities_service.create_relationship(
        db,
        db,
        customer_id=source_entity.customer_id,
        source_entity=source_entity,
        to_entity_id=target_entity.id,
        from_column=from_column,
        to_column="id",
        kind="n:1",
    )
    db.commit()
    return rel


def put_access(client, dataset_id: str, mode: str, grants: list[dict], *, headers=OWNER):
    return client.put(
        f"/api/v1/datasets/{dataset_id}/access",
        json={"mode": mode, "grants": grants},
        headers=headers,
    )


def events(db, dataset_id: str) -> list[DatasetAclEvent]:
    return list(
        db.execute(
            select(DatasetAclEvent)
            .where(DatasetAclEvent.dataset_id == dataset_id)
            .order_by(DatasetAclEvent.id)
        )
        .scalars()
        .all()
    )


# ------------------------------------------------------------------ 列表 / 详情


def test_restricted_hidden_from_list_and_detail_403(env):
    client, db = env["client"], env["db"]
    team = seed_dataset(db, key="team_ds")
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")

    # 未授权 viewer：列表不含 restricted 行；team 行照常
    listing = client.get("/api/v1/datasets", headers=VIEWER)
    assert listing.status_code == 200, listing.text
    ids = {row["id"] for row in listing.json()}
    assert restricted.id not in ids
    assert team.id in ids

    # 未授权 editor（有写角色但无授权）：同样列表过滤
    listing = client.get("/api/v1/datasets", headers=EDITOR)
    assert listing.status_code == 200
    assert restricted.id not in {row["id"] for row in listing.json()}

    # 详情 403，detail 含数据集名与「无访问授权」，不返回列/行
    detail = client.get(f"/api/v1/datasets/{restricted.id}", headers=VIEWER)
    assert detail.status_code == 403, detail.text
    assert "无访问授权" in detail.json()["detail"]
    assert "机密表" in detail.json()["detail"]
    assert "columns" not in detail.json()

    detail = client.get(f"/api/v1/datasets/{restricted.id}", headers=EDITOR)
    assert detail.status_code == 403


def test_owner_always_sees_restricted(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")

    listing = client.get("/api/v1/datasets", headers=OWNER)
    assert listing.status_code == 200
    assert restricted.id in {row["id"] for row in listing.json()}
    assert client.get(f"/api/v1/datasets/{restricted.id}", headers=OWNER).status_code == 200
    assert client.get(f"/api/v1/datasets/{restricted.id}/rows", headers=OWNER).status_code == 200
    assert client.get(f"/api/v1/datasets/{restricted.id}/export", headers=OWNER).status_code == 200

    # 授权名单为空时非 owner 成员全 403
    for headers in (VIEWER, EDITOR):
        assert client.get(f"/api/v1/datasets/{restricted.id}", headers=headers).status_code == 403


def test_granted_view_sees_list_and_detail(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")
    add_grant(db, restricted, VIEWER_SUB, "view")

    listing = client.get("/api/v1/datasets", headers=VIEWER)
    assert listing.status_code == 200
    assert restricted.id in {row["id"] for row in listing.json()}
    detail = client.get(f"/api/v1/datasets/{restricted.id}", headers=VIEWER)
    assert detail.status_code == 200, detail.text
    assert detail.json()["name"] == "机密表"


def test_revoke_takes_effect_immediately_and_audits(env):
    client, db = env["client"], env["db"]
    # 默认 team 起步：第一条 PUT 是模式切换，第二条是同模式换名单
    restricted = seed_dataset(db, key="secret_ds", name="机密表")

    first = put_access(client, restricted.id, "restricted", [{"logto_sub": VIEWER_SUB, "permission": "view"}])
    assert first.status_code == 200, first.text
    assert client.get(f"/api/v1/datasets/{restricted.id}", headers=VIEWER).status_code == 200

    # 撤销：下一次请求立即 403（无缓存窗口）
    second = put_access(client, restricted.id, "restricted", [])
    assert second.status_code == 200, second.text
    assert client.get(f"/api/v1/datasets/{restricted.id}", headers=VIEWER).status_code == 403
    assert restricted.id not in {
        row["id"] for row in client.get("/api/v1/datasets", headers=VIEWER).json()
    }
    # 审计两条变更记录可查
    rows = events(db, restricted.id)
    assert len(rows) == 2
    assert rows[0].action == "mode_change"
    assert rows[0].payload_json["new_grants"] == [
        {"logto_sub": VIEWER_SUB, "permission": "view"}
    ]
    assert rows[1].action == "grants_replace"
    assert rows[1].payload_json["new_grants"] == []


def test_switch_back_to_team_restores_everyone_and_clears_grants(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")
    put_access(client, restricted.id, "restricted", [{"logto_sub": VIEWER_SUB, "permission": "view"}])
    assert client.get(f"/api/v1/datasets/{restricted.id}", headers=EDITOR).status_code == 403

    back = put_access(client, restricted.id, "team", [])
    assert back.status_code == 200, back.text
    assert back.json() == {"mode": "team", "grants": []}

    # 全队恢复可见（含未授权 editor）
    assert client.get(f"/api/v1/datasets/{restricted.id}", headers=EDITOR).status_code == 200
    assert client.get(f"/api/v1/datasets/{restricted.id}", headers=VIEWER).status_code == 200
    assert restricted.id in {
        row["id"] for row in client.get("/api/v1/datasets", headers=VIEWER).json()
    }
    # 授权表清空
    remaining = db.execute(
        select(DatasetGrant).where(DatasetGrant.dataset_id == restricted.id)
    ).scalars().all()
    assert remaining == []
    # 审计第三条：mode_change（restricted → team），new_grants 空
    rows = events(db, restricted.id)
    assert len(rows) == 2
    assert rows[-1].action == "mode_change"
    assert rows[-1].payload_json["new_mode"] == "team"


# ------------------------------------------------------------------ 控制台读面


def test_console_read_surfaces_403_without_grant(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")

    for path in (
        f"/api/v1/datasets/{restricted.id}/rows",
        f"/api/v1/datasets/{restricted.id}/export",
        f"/api/v1/datasets/{restricted.id}/corrections",
        f"/api/v1/datasets/{restricted.id}/syncs",
        f"/api/v1/datasets/{restricted.id}/quality",
    ):
        resp = client.get(path, headers=EDITOR)
        assert resp.status_code == 403, (path, resp.text)
        assert "无访问授权" in resp.json()["detail"]

    # 授予 view 后各读面恢复
    add_grant(db, restricted, EDITOR_SUB, "view")
    for path in (
        f"/api/v1/datasets/{restricted.id}/rows",
        f"/api/v1/datasets/{restricted.id}/export",
        f"/api/v1/datasets/{restricted.id}/corrections",
        f"/api/v1/datasets/{restricted.id}/syncs",
        f"/api/v1/datasets/{restricted.id}/quality",
    ):
        resp = client.get(path, headers=EDITOR)
        assert resp.status_code == 200, (path, resp.text)


# ------------------------------------------------------------------ 数据面


def test_data_plane_console_channel_403_without_grant(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")

    query = client.post(
        f"/api/v1/datasets/{restricted.id}/query",
        json={"filters": [], "sort": [], "limit": 10},
        headers=EDITOR,
    )
    assert query.status_code == 403, query.text
    assert "无访问授权" in query.json()["detail"]

    aggregate = client.post(
        f"/api/v1/datasets/{restricted.id}/aggregate",
        json={"group_by": ["id"], "measures": [{"column": "id", "op": "count"}]},
        headers=EDITOR,
    )
    assert aggregate.status_code == 403

    # 六算子读面同闸（派生本身也是写面，需 edit——此处仅证明 403）
    derive = client.post(
        f"/api/v1/datasets/{restricted.id}/derive",
        json={"name": "derived", "filters": [], "sort": []},
        headers=EDITOR,
    )
    assert derive.status_code == 403

    # viewer 仍先被全局角色门禁拦（既有语义，非 ACL 文案）
    viewer_query = client.post(
        f"/api/v1/datasets/{restricted.id}/query",
        json={"filters": [], "sort": [], "limit": 10},
        headers=VIEWER,
    )
    assert viewer_query.status_code == 403
    assert "viewer 只读" in viewer_query.json()["detail"]


def test_data_plane_granted_view_reads(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")
    add_grant(db, restricted, EDITOR_SUB, "view")

    query = client.post(
        f"/api/v1/datasets/{restricted.id}/query",
        json={"filters": [], "sort": [], "limit": 10},
        headers=EDITOR,
    )
    assert query.status_code == 200, query.text
    assert query.json()["rows"][0]["id"] == "1"
    # view 授权可读但不可写
    derive = client.post(
        f"/api/v1/datasets/{restricted.id}/derive",
        json={"name": "derived", "filters": [], "sort": []},
        headers=EDITOR,
    )
    assert derive.status_code == 403
    assert "编辑（edit）" in derive.json()["detail"]


def test_data_key_channel_not_enforced(env):
    """D6：wdk- data-key 是客户级凭据、不承载成员身份 → 不拦。"""
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")

    minted = client.post(
        "/api/v1/data-plane-keys",
        json={"label": "app-1", "dataset_ids": []},
        headers=OWNER,
    )
    assert minted.status_code == 201, minted.text
    key_headers = {"Authorization": f"Bearer {minted.json()['key']}"}
    query = client.post(
        f"/api/v1/datasets/{restricted.id}/query",
        json={"filters": [], "sort": [], "limit": 10},
        headers=key_headers,
    )
    assert query.status_code == 200, query.text


WINDOW_BODY = {
    "name": "窗口派生",
    "partition_by": ["id"],
    "order_by": [{"column": "name", "dir": "asc"}],
    "window": [{"func": "row_number", "as": "rn"}],
}


def test_window_operator_needs_edit_grant(env):
    """window 产出派生数据集=写面：无授权 403、view 可读不可写、edit 才可写。"""
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")

    denied = client.post(
        f"/api/v1/datasets/{restricted.id}/window", json=dict(WINDOW_BODY), headers=EDITOR
    )
    assert denied.status_code == 403, denied.text
    assert "无访问授权" in denied.json()["detail"]
    assert env["fake"].calls == []  # 授权闸在引擎之前

    add_grant(db, restricted, EDITOR_SUB, "view")
    view_only = client.post(
        f"/api/v1/datasets/{restricted.id}/window", json=dict(WINDOW_BODY), headers=EDITOR
    )
    assert view_only.status_code == 403
    assert "编辑（edit）" in view_only.json()["detail"]

    # 同 (数据集, 成员) 只有一条授权行：升级为 edit（不新增第二行）
    grant = db.execute(
        select(DatasetGrant).where(
            DatasetGrant.dataset_id == restricted.id,
            DatasetGrant.logto_sub == EDITOR_SUB,
        )
    ).scalar_one()
    grant.permission = "edit"
    db.commit()
    granted = client.post(
        f"/api/v1/datasets/{restricted.id}/window", json=dict(WINDOW_BODY), headers=EDITOR
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()["source_kind"] == "derived"
    window_calls = [args for tool, args in env["fake"].calls if tool == TOOL_WINDOW]
    assert window_calls and window_calls[0]["parent_key"] == restricted.key


def test_window_operator_data_key_channel_not_enforced(env):
    """D6：wdk- 轨道不承载成员身份 → 受限数据集也可写（客户级凭据语义）。"""
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")
    minted = client.post(
        "/api/v1/data-plane-keys",
        json={"label": "app-1", "dataset_ids": []},
        headers=OWNER,
    )
    assert minted.status_code == 201, minted.text
    key_headers = {"Authorization": f"Bearer {minted.json()['key']}"}
    resp = client.post(
        f"/api/v1/datasets/{restricted.id}/window",
        json=dict(WINDOW_BODY),
        headers=key_headers,
    )
    assert resp.status_code == 201, resp.text


# ------------------------------------------------------------------ 实体对象面 / 结构面


def test_object_endpoints_403_but_structure_surfaces_allowed(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(
        db, key="customers", acl_mode="restricted", name="客户机密",
        columns=(("id", "TEXT"), ("name", "TEXT"), ("region_id", "TEXT")),
    )
    regions = seed_dataset(db, key="regions", columns=(("id", "TEXT"), ("name", "TEXT")))
    ent_c = seed_entity(db, restricted, name="客户")
    ent_r = seed_entity(db, regions, name="区域")
    rel = seed_relation(db, ent_c, ent_r, from_column="region_id")

    # 未授权 viewer：对象面全 403（含关系遍历）
    for path in (
        f"/api/v1/entities/{ent_c.id}/objects",
        f"/api/v1/entities/{ent_c.id}/objects/1",
        f"/api/v1/entities/{ent_c.id}/objects/1/relations/{rel.id}",
    ):
        resp = client.get(path, headers=VIEWER)
        assert resp.status_code == 403, (path, resp.text)
        assert "无访问授权" in resp.json()["detail"]

    # 结构面不拦：图谱与实体元数据列表正常
    graph = client.get("/api/v1/entities/graph", headers=VIEWER)
    assert graph.status_code == 200, graph.text
    listing = client.get("/api/v1/entities", headers=VIEWER)
    assert listing.status_code == 200, listing.text
    assert {e["name"] for e in listing.json()["entities"]} == {"客户", "区域"}


def test_object_endpoint_granted_view_allowed(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(
        db, key="customers", acl_mode="restricted", name="客户机密",
        columns=(("id", "TEXT"), ("name", "TEXT")),
    )
    ent = seed_entity(db, restricted, name="客户")
    add_grant(db, restricted, VIEWER_SUB, "view")

    resp = client.get(f"/api/v1/entities/{ent.id}/objects", headers=VIEWER)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["objects"]) == 1


# ------------------------------------------------------------------ 写面


def test_write_ops_require_edit(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted", name="机密表")

    # 无授权 editor：批量写/修正/派生全 403（ACL 文案）
    batch = client.post(
        f"/api/v1/datasets/{restricted.id}/rows:batchWrite",
        json={"ops": [{"op": "update", "anchor": "1", "values": {"name": "x"}}]},
        headers=EDITOR,
    )
    assert batch.status_code == 403, batch.text
    assert "无访问授权" in batch.json()["detail"]

    correction = client.post(
        f"/api/v1/datasets/{restricted.id}/corrections",
        json={"op": "update", "row_id": "1", "column": "name", "value": "x"},
        headers=EDITOR,
    )
    assert correction.status_code == 403

    derive = client.post(
        f"/api/v1/datasets/{restricted.id}/derive",
        json={"name": "derived", "filters": [], "sort": []},
        headers=EDITOR,
    )
    assert derive.status_code == 403

    # 授予 edit 后同一调用正常
    add_grant(db, restricted, EDITOR_SUB, "edit")
    batch = client.post(
        f"/api/v1/datasets/{restricted.id}/rows:batchWrite",
        json={"ops": [{"op": "update", "anchor": "1", "values": {"name": "x"}}]},
        headers=EDITOR,
    )
    assert batch.status_code == 200, batch.text
    correction = client.post(
        f"/api/v1/datasets/{restricted.id}/corrections",
        json={"op": "update", "row_id": "1", "column": "name", "value": "x"},
        headers=EDITOR,
    )
    assert correction.status_code == 201, correction.text
    derive = client.post(
        f"/api/v1/datasets/{restricted.id}/derive",
        json={"name": "derived", "filters": [], "sort": []},
        headers=EDITOR,
    )
    assert derive.status_code == 201, derive.text

    # viewer 写面仍先被全局角色门禁拦（独立生效）
    viewer_batch = client.post(
        f"/api/v1/datasets/{restricted.id}/rows:batchWrite",
        json={"ops": [{"op": "update", "anchor": "1", "values": {"name": "x"}}]},
        headers=VIEWER,
    )
    assert viewer_batch.status_code == 403
    assert "viewer 只读" in viewer_batch.json()["detail"]


def test_materialize_requires_edit(env):
    client, db = env["client"], env["db"]
    left = seed_dataset(
        db, key="orders", acl_mode="restricted", name="订单机密",
        columns=(("id", "TEXT"), ("region_id", "TEXT")),
    )
    right = seed_dataset(db, key="regions", columns=(("id", "TEXT"), ("name", "TEXT")))
    ent_l = seed_entity(db, left, name="订单", display="id")
    ent_r = seed_entity(db, right, name="区域", display="name")
    rel = seed_relation(db, ent_l, ent_r, from_column="region_id")

    resp = client.post(
        f"/api/v1/entities/{ent_l.id}/relationships/{rel.id}/materialize",
        json={},
        headers=EDITOR,
    )
    assert resp.status_code == 403, resp.text
    assert "无访问授权" in resp.json()["detail"]

    add_grant(db, left, EDITOR_SUB, "edit")
    resp = client.post(
        f"/api/v1/entities/{ent_l.id}/relationships/{rel.id}/materialize",
        json={},
        headers=EDITOR,
    )
    assert resp.status_code == 201, resp.text


# ------------------------------------------------------------------ 管理面


def test_management_owner_only(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted")
    for headers in (EDITOR, VIEWER):
        assert client.get(f"/api/v1/datasets/{restricted.id}/access", headers=headers).status_code == 403
        assert put_access(client, restricted.id, "team", [], headers=headers).status_code == 403


def test_access_get_returns_mode_and_grants(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted")
    add_grant(db, restricted, VIEWER_SUB, "view")
    add_grant(db, restricted, EDITOR_SUB, "edit")

    resp = client.get(f"/api/v1/datasets/{restricted.id}/access", headers=OWNER)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "mode": "restricted",
        "grants": [
            {"logto_sub": EDITOR_SUB, "permission": "edit"},
            {"logto_sub": VIEWER_SUB, "permission": "view"},
        ],
    }


def test_member_validation_400_honest_count(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds", acl_mode="restricted")

    resp = put_access(
        client,
        restricted.id,
        "restricted",
        [
            {"logto_sub": "sub-outsider-1", "permission": "view"},
            {"logto_sub": "sub-outsider-2", "permission": "view"},
        ],
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "2 个" in detail
    assert "sub-outsider-1" in detail
    # 零写入：模式与授权表未变，无审计行
    assert db.get(Dataset, restricted.id).acl_mode == "restricted"
    assert db.execute(
        select(DatasetGrant).where(DatasetGrant.dataset_id == restricted.id)
    ).scalars().all() == []
    assert events(db, restricted.id) == []

    # owner 本人可被授权（名单合法）
    ok = put_access(
        client,
        restricted.id,
        "restricted",
        [{"logto_sub": OWNER_SUB, "permission": "edit"}],
    )
    assert ok.status_code == 200, ok.text


def test_audit_event_payload_and_action(env):
    client, db = env["client"], env["db"]
    restricted = seed_dataset(db, key="secret_ds")

    # 同模式换名单 → grants_replace；payload 带旧/新快照
    first = put_access(
        client, restricted.id, "restricted", [{"logto_sub": VIEWER_SUB, "permission": "view"}]
    )
    assert first.status_code == 200
    second = put_access(
        client, restricted.id, "restricted", [{"logto_sub": EDITOR_SUB, "permission": "edit"}]
    )
    assert second.status_code == 200

    rows = events(db, restricted.id)
    assert len(rows) == 2
    assert rows[0].actor_sub == OWNER_SUB
    assert rows[0].action == "mode_change"  # team → restricted
    assert rows[0].payload_json["old_mode"] == "team"
    assert rows[0].payload_json["new_mode"] == "restricted"
    assert rows[0].payload_json["old_grants"] == []
    assert rows[1].action == "grants_replace"
    assert rows[1].payload_json["old_grants"] == [
        {"logto_sub": VIEWER_SUB, "permission": "view"}
    ]
    assert rows[1].payload_json["new_grants"] == [
        {"logto_sub": EDITOR_SUB, "permission": "edit"}
    ]


# ------------------------------------------------------------------ 隔离 / 顺序


def test_cross_tenant_stays_404(env):
    client, db = env["client"], env["db"]
    other = seed_dataset(db, key="other_ds", customer=OTHER_CUST, acl_mode="restricted")
    other_entity = seed_entity(db, other, name="他企实体")

    assert client.get(f"/api/v1/datasets/{other.id}", headers=OWNER).status_code == 404
    assert client.get(f"/api/v1/datasets/{other.id}/access", headers=OWNER).status_code == 404
    assert put_access(client, other.id, "team", []).status_code == 404
    assert client.post(
        f"/api/v1/datasets/{other.id}/query",
        json={"filters": [], "sort": [], "limit": 10},
        headers=OWNER,
    ).status_code == 404
    assert client.get(
        f"/api/v1/entities/{other_entity.id}/objects", headers=OWNER
    ).status_code == 404


def test_tier_gate_precedes_acl_and_text_unchanged(env):
    client, db = env["client"], env["db"]
    api_tier = seed_dataset(db, key="free_ds", tier="api", acl_mode="restricted")

    # 未授权 editor：档位闸 403 先于 ACL（既有文案零漂移）
    resp = client.post(
        f"/api/v1/datasets/{api_tier.id}/derive",
        json={"name": "derived", "filters": [], "sort": []},
        headers=EDITOR,
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "PLAN_TIER_REQUIRED"
    assert "无访问授权" not in detail["message"]

    # owner 同样只看到档位闸（ACL 不改变既有档位判定）
    resp = client.post(
        f"/api/v1/datasets/{api_tier.id}/derive",
        json={"name": "derived", "filters": [], "sort": []},
        headers=OWNER,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLAN_TIER_REQUIRED"


# ------------------------------------------------------------------ 零迁移兼容


def test_default_team_mode_is_byte_compatible(env):
    """默认 acl_mode=team（无授权行）：列表/详情/数据面/对象面行为不变。"""
    client, db = env["client"], env["db"]
    dataset = seed_dataset(db, key="default_ds")
    assert dataset.acl_mode == "team"  # ORM 默认值落库
    entity = seed_entity(db, dataset, name="客户")

    assert dataset.id in {
        row["id"] for row in client.get("/api/v1/datasets", headers=VIEWER).json()
    }
    assert client.get(f"/api/v1/datasets/{dataset.id}", headers=VIEWER).status_code == 200
    assert client.get(f"/api/v1/datasets/{dataset.id}/rows", headers=VIEWER).status_code == 200
    assert client.get(f"/api/v1/datasets/{dataset.id}/export", headers=VIEWER).status_code == 200
    assert client.post(
        f"/api/v1/datasets/{dataset.id}/query",
        json={"filters": [], "sort": [], "limit": 10},
        headers=EDITOR,
    ).status_code == 200
    assert client.get(f"/api/v1/entities/{entity.id}/objects", headers=VIEWER).status_code == 200
    # 未授权 editor 仍可写（team 直通，全局角色门禁独立）
    assert client.post(
        f"/api/v1/datasets/{dataset.id}/rows:batchWrite",
        json={"ops": [{"op": "update", "anchor": "1", "values": {"name": "x"}}]},
        headers=EDITOR,
    ).status_code == 200
    # 管理面读取如实呈现默认模式
    access = client.get(f"/api/v1/datasets/{dataset.id}/access", headers=OWNER)
    assert access.status_code == 200
    assert access.json() == {"mode": "team", "grants": []}
