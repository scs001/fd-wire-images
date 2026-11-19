"""内置声明动作——路由级测试（wire-entity-write-surface 任务 3.4/3.5 的 HTTP 面）。

覆盖薄适配层（service/gate 级见 test_entity_write_surface.py）：
- GET /domain-actions：内置两行注入（builtin=true、负 id）；
- POST /domain-actions/propose：内置负 id 跳过行查找直达闸（200 即时回执）；
  非法草案 422（preflight 即拦、不落实例）；未知负 id 404（未注册语义）；
- POST /api/v1/ai/actions/{id}/approve：owner 确认→实体落库→图谱端点可见；
- viewer 拒提议 403（既有角色门语义零变化）。

环境：console_fixture（假 JWKS + 平台库双席）+ 单引擎内存库（ai/datasets/
entities 会话束同库，照 test_preferences 路由先例）。
"""

from __future__ import annotations

import os

os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db
from fd_wire_api.ai.routes import get_gate_sessions
from fd_wire_api.app import app
from fd_wire_api.console.models import CustomerMember
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db

CUST = "cust-ewr"
SUB_OWNER = "sub-owner-ewr"
SUB_VIEWER = "sub-viewer-ewr"
OWNER_HDR: dict = {}
VIEWER_HDR: dict = {}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(
        tmp_path, monkeypatch, customers=[(SUB_OWNER, CUST)]
    )
    with console_env.session() as sess:
        sess.add(CustomerMember(
            customer_id=console_env.subs[SUB_OWNER], logto_sub=SUB_VIEWER,
            role="viewer", invited_by_sub=SUB_OWNER,
        ))
        sess.commit()

    global OWNER_HDR, VIEWER_HDR
    OWNER_HDR = bearer(mint_id_token(SUB_OWNER))
    VIEWER_HDR = bearer(mint_id_token(SUB_VIEWER))

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AiBase.metadata.create_all(bind=engine)
    EntitiesBase.metadata.create_all(bind=engine)
    DatasetsBase.metadata.create_all(bind=engine)
    import fd_wire_api.ai.models  # noqa: F401 —— 注册全部 AiBase 映射

    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    from fd_wire_api.ai.actions import GateSessions

    bundle = GateSessions(
        ai=session, datasets=session, demands=session,
        materialization=session, packs=session, entities=session,
    )

    def override_db():
        yield session

    def override_gate_sessions():
        return bundle

    app.dependency_overrides[get_ai_db] = override_db
    app.dependency_overrides[get_datasets_db] = override_db
    app.dependency_overrides[get_entities_db] = override_db
    app.dependency_overrides[get_gate_sessions] = override_gate_sessions

    # 播种 cell 数据集（免费档亦可声明；这里给 cell 档与对象面语义对齐）
    dataset = Dataset(
        customer_id=CUST, key="customers", name="customers", source_kind="upload",
        storage_tier="cell",
        columns_json=[{"name": "id", "type": "TEXT"}, {"name": "name", "type": "TEXT"}],
        row_count=3,
    )
    session.add(dataset)
    session.commit()

    client = TestClient(app)
    try:
        yield client, session
    finally:
        for dep in (get_ai_db, get_datasets_db, get_entities_db, get_gate_sessions):
            app.dependency_overrides.pop(dep, None)
        session.close()
        client.close()
        console_env.engine.dispose()


ENTITY_VALUES = {
    "business_name": "客户",
    "dataset_key": "customers",
    "pk_columns": "id",
    "display_column": "name",
}


def test_list_injects_builtin_rows(env):
    client, _ = env
    resp = client.get("/api/v1/domain-actions", headers=OWNER_HDR)
    assert resp.status_code == 200, resp.text
    actions = resp.json()["actions"]
    builtins = [a for a in actions if a.get("builtin")]
    assert {a["id"] for a in builtins} == {-1, -2}
    assert all(a["entity_id"] is None and a["enabled"] for a in builtins)
    relation = next(a for a in builtins if a["id"] == -2)
    kind_field = next(f for f in relation["form_schema"]["fields"] if f["name"] == "kind")
    assert kind_field["options"] == ["n:1", "1:1", "1:n", "m:n"]


def test_propose_confirm_graph_full_chain(env):
    client, session = env
    resp = client.post(
        "/api/v1/domain-actions/propose",
        json={"action_type_id": -1, "values": ENTITY_VALUES,
              "source_ref": "fleet=f1;post=deployer;title=声明提案员;target=cell:drillcust001"},
        headers=OWNER_HDR,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instance_id"] and body["state"] in ("pending_confirm", "awaiting_confirmation")
    assert "声明实体「客户」" in body["impact"]

    approve = client.post(
        f"/api/v1/ai/actions/{body['proposal_id']}/approve",
        headers=OWNER_HDR,
    )
    assert approve.status_code == 200, approve.text

    graph = client.get("/api/v1/entities/graph", headers=OWNER_HDR)
    assert graph.status_code == 200, graph.text
    nodes = graph.json()["nodes"]
    assert [n["name"] for n in nodes] == ["客户"]
    assert nodes[0]["pk_columns"] == ["id"]

    # 台账：result_ref 回填、免费角色门后 viewer 不可见他人提案？隔离另测
    instances = client.get(
        "/api/v1/domain-actions/instances", headers=OWNER_HDR
    ).json()["instances"]
    assert instances[0]["result_ref"] == str(nodes[0]["id"])
    assert instances[0]["correction_id"] is None
    assert instances[0]["source_ref"].startswith("fleet=f1")


def test_propose_invalid_draft_422_not_pending(env):
    client, _ = env
    resp = client.post(
        "/api/v1/domain-actions/propose",
        json={"action_type_id": -1,
              "values": {**ENTITY_VALUES, "pk_columns": "ghost"}},
        headers=OWNER_HDR,
    )
    assert resp.status_code == 422, resp.text
    assert "ghost" in resp.json()["detail"]
    instances = client.get(
        "/api/v1/domain-actions/instances", headers=OWNER_HDR
    ).json()["instances"]
    assert instances == []  # 非法草案不进 pending


def test_propose_unknown_negative_id_404(env):
    client, _ = env
    resp = client.post(
        "/api/v1/domain-actions/propose",
        json={"action_type_id": -99, "values": ENTITY_VALUES},
        headers=OWNER_HDR,
    )
    assert resp.status_code == 404


def test_viewer_propose_403(env):
    """既有角色门语义零变化：内置通道不改变 viewer 只读边界。"""
    client, _ = env
    resp = client.post(
        "/api/v1/domain-actions/propose",
        json={"action_type_id": -1, "values": ENTITY_VALUES},
        headers=VIEWER_HDR,
    )
    assert resp.status_code == 403


def test_builtin_type_endpoints_not_editable(env):
    """编辑/停用端点对内置负 id 404（无 DB 行可寻；内置名保留与 metadata
    效果的 409/422 面在 service 级测试覆盖）。"""
    client, session = env
    from fd_wire_api.entities import service as entities_service
    from fd_wire_api.entities.models import Entity

    ds = session.query(Dataset).filter_by(key="customers").one()
    ent = entities_service.create_entity(
        session, session, customer_id=CUST, name="区域",
        dataset_id=ds.id, pk_columns=["id"],
    )
    session.commit()
    body = {
        "name": "x", "risk": "write",
        "form_schema": {"fields": [{"name": "id", "input": "text", "required": True}]},
        "effect": {"kind": "insert", "value_mapping": {"name": {"field": "id"}}},
    }
    put_resp = client.put(
        f"/api/v1/entities/{ent.id}/actions/-1", json=body, headers=OWNER_HDR
    )
    assert put_resp.status_code == 404, put_resp.text
    del_resp = client.delete(
        f"/api/v1/entities/{ent.id}/actions/-1", headers=OWNER_HDR
    )
    assert del_resp.status_code == 404, del_resp.text
    del Entity  # noqa: F841 —— 导入即注册映射
