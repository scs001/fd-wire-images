"""实体 v2 对象浏览与关系扩展测试（wire-entity-v2 任务组 2/3/6）。

覆盖：
- 声明矩阵：1:n 反向声明（归一化存储 + 响应/图谱按声明方向）、正反向 409、
  1:n 源列非主键 422；m:n 经中间集声明（缺中间集/列不在中间集 422）、
  m:n 物化 422 如实；
- 属性元数据：display_column 非法列 422 / column_notes 超 50 字 422 / 合法保存；
- 对象端点：列表（q→contains 显示列、api 档 403、引擎调用形状）、详情
  （eq 主键、零行 found=false、多列主键 422）、关系遍历（n:1 反查需先取
  本对象行、1:n 正查直查、m:n 两段、跨企业 404）；
- AI 注入 v2：显示列/列注记/m:n 中间集进块（entity_graph_prompt_block 直测）。

引擎假件：脚本化按工具名出队（对象端点多次查询的序列）。
"""

from __future__ import annotations

import os

os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db
from fd_wire_api.entities import service
from fd_wire_api.entities.models import Entity, EntityRelationship

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-eo-cust"
SUB_OTHER = "sub-eo-other"
CUST = "cust-eo"
OTHER_CUST = "cust-eo-other"

OWNER = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))


class ScriptedEngine:
    """按工具名出队的引擎假件（记录全部调用供形状断言）。"""

    def __init__(self, results: dict[str, list[dict]] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.results: dict[str, list[dict]] = results or {}

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        queue = self.results.get(tool)
        if queue:
            return queue.pop(0)
        return {"columns": [], "rows": [], "total": 0}


def query_result(columns: list[str], rows: list[list]) -> dict:
    return {
        "columns": [{"name": c, "type": "TEXT"} for c in columns],
        "rows": rows,
        "total": len(rows),
    }


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
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def engine_fake() -> ScriptedEngine:
    return ScriptedEngine()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session, engine_fake):
    """三库会话覆写 + 引擎假件（照 test_entities 先例）。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-eo=http://cell-eo/mcp")

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_ai_db] = override_get_db
    app.dependency_overrides[get_entities_db] = override_get_db
    app.dependency_overrides[get_datasets_db] = override_get_db
    from fd_wire_api.datasets.ingest import get_engine_client_factory

    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: engine_fake
    )
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUST, CUST), (SUB_OTHER, OTHER_CUST)],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


def seed_dataset(
    db_session,
    *,
    customer_id: str = CUST,
    key: str,
    columns: list | None = None,
    tier: str = "cell",
    row_count: int = 5,
) -> str:
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=key,
        source_kind="upload",
        storage_tier=tier,
        columns_json=columns
        or [{"name": "id", "type": "TEXT"}, {"name": "name", "type": "TEXT"}],
        row_count=row_count,
    )
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)
    return dataset.id


def make_entity(
    db_session,
    dataset_id: str,
    name: str = "客户",
    pk=("id",),
    display: str | None = None,
) -> Entity:
    entity = Entity(
        customer_id=CUST,
        name=name,
        dataset_id=dataset_id,
        pk_columns=list(pk),
        display_column=display,
    )
    db_session.add(entity)
    db_session.commit()
    db_session.refresh(entity)
    return entity


# ------------------------------------------------- 属性元数据（任务 1.1）


def test_display_column_invalid_rejected(client, db_session):
    ds = seed_dataset(db_session, key="orders")
    resp = client.post(
        "/api/v1/entities",
        json={
            "name": "订单",
            "dataset_id": ds,
            "pk_columns": ["id"],
            "display_column": "不存在的列",
        },
        headers=OWNER,
    )
    assert resp.status_code == 422, resp.text
    assert "显示列" in resp.json()["detail"]


def test_column_notes_validation(client, db_session):
    ds = seed_dataset(db_session, key="orders")
    # 合法保存
    resp = client.post(
        "/api/v1/entities",
        json={
            "name": "订单",
            "dataset_id": ds,
            "pk_columns": ["id"],
            "display_column": "name",
            "column_notes": {"name": "客户名", "id": "主键"},
        },
        headers=OWNER,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["display_column"] == "name"
    assert body["column_notes"] == {"name": "客户名", "id": "主键"}
    # 注记超 50 字
    resp = client.put(
        f"/api/v1/entities/{body['id']}",
        json={
            "name": "订单",
            "dataset_id": ds,
            "pk_columns": ["id"],
            "column_notes": {"name": "长" * 51},
        },
        headers=OWNER,
    )
    assert resp.status_code == 422
    assert "50 字" in resp.json()["detail"]


# ------------------------------------------------- 关系声明矩阵（任务 2.1）


def _make_two_entities(db_session):
    ds_orders = seed_dataset(
        db_session,
        key="orders",
        columns=[{"name": "id", "type": "TEXT"}, {"name": "customer_id", "type": "TEXT"}],
    )
    ds_customers = seed_dataset(db_session, key="customers")
    orders = make_entity(db_session, ds_orders, name="订单", pk=("id",))
    customers = make_entity(db_session, ds_customers, name="客户", pk=("id",))
    return orders, customers


def test_declare_1n_normalized_and_declared_direction(client, db_session):
    orders, customers = _make_two_entities(db_session)
    # 声明 客户 →(1:n) 订单（声明源列=客户主键；声明目标列=订单外键列）
    resp = client.post(
        f"/api/v1/entities/{customers.id}/relationships",
        json={
            "to_entity_id": orders.id,
            "from_column": "id",
            "to_column": "customer_id",
            "kind": "1:n",
        },
        headers=OWNER,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 响应按**声明方向**呈现
    assert body["kind"] == "1:n"
    assert body["from_entity_id"] == customers.id
    assert body["to_entity_id"] == orders.id
    # 存储为归一化反向 n:1
    rel = db_session.get(EntityRelationship, body["id"])
    assert rel.kind == "n:1"
    assert rel.declared_kind == "1:n"
    assert rel.from_entity_id == orders.id
    assert rel.to_entity_id == customers.id
    # 图谱 edge 同样按声明方向
    graph = client.get("/api/v1/entities/graph", headers=OWNER).json()
    edge = next(e for e in graph["edges"] if e["id"] == body["id"])
    assert edge["kind"] == "1:n"
    assert edge["from_entity_id"] == customers.id


def test_declare_1n_reverse_duplicate_409(client, db_session):
    orders, customers = _make_two_entities(db_session)
    resp = client.post(
        f"/api/v1/entities/{customers.id}/relationships",
        json={
            "to_entity_id": orders.id,
            "from_column": "id",
            "to_column": "customer_id",
            "kind": "1:n",
        },
        headers=OWNER,
    )
    assert resp.status_code == 201
    # 同四元组的 n:1 正向（归一化后同形）→ 409
    resp = client.post(
        f"/api/v1/entities/{orders.id}/relationships",
        json={
            "to_entity_id": customers.id,
            "from_column": "customer_id",
            "to_column": "id",
            "kind": "n:1",
        },
        headers=OWNER,
    )
    assert resp.status_code == 409, resp.text


def test_declare_1n_source_column_must_be_pk(client, db_session):
    orders, customers = _make_two_entities(db_session)
    resp = client.post(
        f"/api/v1/entities/{customers.id}/relationships",
        json={
            "to_entity_id": orders.id,
            "from_column": "name",  # 客户的非主键列
            "to_column": "customer_id",
            "kind": "1:n",
        },
        headers=OWNER,
    )
    assert resp.status_code == 422
    assert "主键列" in resp.json()["detail"]


def test_declare_m2m_with_junction_and_materialize_expands(
    client, db_session, engine_fake
):
    """m:n 声明 + 物化（wire-entity-mn-materialize 解禁）：桥展开路径成功、
    响应带诚实计数（估算=桥行数 / 实数=展开对数 / 孤儿桥）。"""
    from fd_wire_api.datasets.ingest import (
        TOOL_LINK,
        TOOL_OVERVIEW,
        get_engine_client_factory,
    )

    orders, customers = _make_two_entities(db_session)
    junction = seed_dataset(
        db_session,
        key="order_tags",
        columns=[{"name": "order_id", "type": "TEXT"}, {"name": "tag_id", "type": "TEXT"}],
    )
    resp = client.post(
        f"/api/v1/entities/{orders.id}/relationships",
        json={
            "to_entity_id": customers.id,
            "from_column": "id",
            "to_column": "id",
            "kind": "m:n",
            "junction_dataset_id": junction,
            "junction_source_column": "order_id",
            "junction_target_column": "tag_id",
        },
        headers=OWNER,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "m:n"
    assert body["junction_dataset_id"] == junction

    class BridgeEngine(ScriptedEngine):
        """junction 三要素回显 + link/overview 应答（物化成功路径）。"""

        def call(self, tool: str, arguments: dict) -> dict:
            self.calls.append((tool, dict(arguments)))
            if tool == TOOL_LINK:
                return {
                    "dataset_key": arguments.get("child_key"),
                    "rows": 4,
                    "orphan_dropped": 2,
                }
            if tool == TOOL_OVERVIEW:
                return {
                    "rows": 4,
                    "size_bytes": 4096,
                    "columns": [{"name": "id", "type": "TEXT"}],
                }
            return super().call(tool, arguments)

    bridge = BridgeEngine()
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: bridge
    )
    try:
        resp = client.post(
            f"/api/v1/entities/{orders.id}/relationships/{body['id']}/materialize",
            json={"name": "物化连接"},
            headers=OWNER,
        )
    finally:
        app.dependency_overrides.pop(get_engine_client_factory, None)
    assert resp.status_code == 201, resp.text
    # 引擎载荷带 junction 三要素（同一 link 工具单路径）
    link_args = [a for name, a in bridge.calls if name == TOOL_LINK]
    assert len(link_args) == 1
    assert link_args[0]["junction_key"]
    assert link_args[0]["junction_left_column"] == "order_id"
    assert link_args[0]["junction_right_column"] == "tag_id"
    # 诚实计数：估算=桥行数（5）、实数=展开对数（4）、孤儿桥（2）
    assert resp.json()["materialization"] == {
        "estimate_rows": 5,
        "rows": 4,
        "orphan_dropped": 2,
    }


def test_declare_m2m_requires_junction(client, db_session):
    orders, customers = _make_two_entities(db_session)
    resp = client.post(
        f"/api/v1/entities/{orders.id}/relationships",
        json={
            "to_entity_id": customers.id,
            "from_column": "id",
            "to_column": "id",
            "kind": "m:n",
        },
        headers=OWNER,
    )
    assert resp.status_code == 422
    assert "中间数据集" in resp.json()["detail"]
    # junction 列不在中间集列快照
    junction = seed_dataset(db_session, key="bad_junction")
    resp = client.post(
        f"/api/v1/entities/{orders.id}/relationships",
        json={
            "to_entity_id": customers.id,
            "from_column": "id",
            "to_column": "id",
            "kind": "m:n",
            "junction_dataset_id": junction,
            "junction_source_column": "不存在的列",
            "junction_target_column": "tag_id",
        },
        headers=OWNER,
    )
    assert resp.status_code == 422
    assert "junction_source_column" in resp.json()["detail"]


# ------------------------------------------------- 对象端点（任务 3.1）


def test_object_list_and_detail(client, db_session, engine_fake):
    ds = seed_dataset(db_session, key="customers")
    entity = make_entity(db_session, ds, name="客户", pk=("id",), display="name")
    engine_fake.results["customer_dataset_query"] = [
        query_result(["id", "name"], [["c1", "甲"], ["c2", "乙"]]),
        query_result(["id", "name"], [["c1", "甲"]]),
        query_result(["id", "name"], []),
    ]
    resp = client.get(f"/api/v1/entities/{entity.id}/objects", params={"q": "甲"}, headers=OWNER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_column"] == "name"
    assert body["objects"][0]["name"] == "甲"
    # 引擎调用形状：contains 在显示列上
    tool, args = engine_fake.calls[0]
    assert tool == "customer_dataset_query"
    assert args["filters"][-1] == {"column": "name", "op": "contains", "value": "甲"}

    detail = client.get(f"/api/v1/entities/{entity.id}/objects/c1", headers=OWNER)
    assert detail.status_code == 200
    assert detail.json()["found"] is True
    assert detail.json()["object"]["name"] == "甲"
    assert engine_fake.calls[1][1]["filters"] == [{"column": "id", "op": "eq", "value": "c1"}]

    miss = client.get(f"/api/v1/entities/{entity.id}/objects/ghost", headers=OWNER)
    assert miss.status_code == 200
    assert miss.json()["found"] is False


def test_object_endpoints_cell_tier_only(client, db_session):
    ds = seed_dataset(db_session, key="free_ds", tier="api")
    entity = make_entity(db_session, ds, name="免费实体", pk=("id",))
    resp = client.get(f"/api/v1/entities/{entity.id}/objects", headers=OWNER)
    assert resp.status_code == 403
    assert client.get(
        f"/api/v1/entities/{entity.id}/objects/x", headers=OWNER
    ).status_code == 403


def test_object_detail_multi_pk_422(client, db_session):
    ds = seed_dataset(db_session, key="comp")
    entity = make_entity(db_session, ds, name="复合主键", pk=("id", "name"))
    resp = client.get(f"/api/v1/entities/{entity.id}/objects/x", headers=OWNER)
    assert resp.status_code == 422
    assert "单列主键" in resp.json()["detail"]


def test_relation_traversal_n1_forward_uses_own_row(client, db_session, engine_fake):
    ds_orders = seed_dataset(
        db_session,
        key="orders",
        columns=[{"name": "id", "type": "TEXT"}, {"name": "customer_id", "type": "TEXT"}],
    )
    ds_customers = seed_dataset(db_session, key="customers")
    orders = make_entity(db_session, ds_orders, name="订单", pk=("id",))
    customers = make_entity(db_session, ds_customers, name="客户", pk=("id",))
    rel = client.post(
        f"/api/v1/entities/{orders.id}/relationships",
        json={
            "to_entity_id": customers.id,
            "from_column": "customer_id",
            "to_column": "id",
            "kind": "n:1",
        },
        headers=OWNER,
    ).json()
    engine_fake.results["customer_dataset_query"] = [
        query_result(["id", "customer_id"], [["o1", "c1"]]),  # 本对象行
        query_result(["id", "name"], [["c1", "甲"]]),  # 目标行
    ]
    resp = client.get(
        f"/api/v1/entities/{orders.id}/objects/o1/relations/{rel['id']}",
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["objects"][0]["name"] == "甲"
    # 第二次查询 = 目标集按 to_column eq 源列值
    assert engine_fake.calls[1][1]["filters"] == [
        {"column": "id", "op": "eq", "value": "c1"}
    ]


def test_relation_traversal_1n_direct_and_m2m_two_step(client, db_session, engine_fake):
    orders, customers = _make_two_entities(db_session)
    junction = seed_dataset(
        db_session,
        key="order_tags",
        columns=[{"name": "order_id", "type": "TEXT"}, {"name": "tag_id", "type": "TEXT"}],
    )
    # 1:n（客户 →(1:n) 订单）：声明 from 侧对象（客户，key=客户主键值）
    # → 存储反向 n:1 的 to 侧 → 反向遍历直查存储源集（订单）外键列
    rel_1n = client.post(
        f"/api/v1/entities/{customers.id}/relationships",
        json={
            "to_entity_id": orders.id,
            "from_column": "id",
            "to_column": "customer_id",
            "kind": "1:n",
        },
        headers=OWNER,
    ).json()
    engine_fake.results["customer_dataset_query"] = [
        query_result(["id", "customer_id"], [["o1", "c1"]])
    ]
    resp = client.get(
        f"/api/v1/entities/{customers.id}/objects/c1/relations/{rel_1n['id']}",
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "1:n"
    assert body["objects"][0]["id"] == "o1"
    assert engine_fake.calls[0][1]["filters"] == [
        {"column": "customer_id", "op": "eq", "value": "c1"}
    ]

    # m:n 两段：junction_source == order 键 → 目标集 in
    rel_m2m = client.post(
        f"/api/v1/entities/{orders.id}/relationships",
        json={
            "to_entity_id": customers.id,
            "from_column": "id",
            "to_column": "id",
            "kind": "m:n",
            "junction_dataset_id": junction,
            "junction_source_column": "order_id",
            "junction_target_column": "tag_id",
        },
        headers=OWNER,
    ).json()
    engine_fake.results["customer_dataset_query"] = [
        query_result(["order_id", "tag_id"], [["o1", "c1"]]),  # 中间集
        query_result(["id", "name"], [["c1", "甲"]]),  # 目标集
    ]
    resp = client.get(
        f"/api/v1/entities/{orders.id}/objects/o1/relations/{rel_m2m['id']}",
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["objects"][0]["id"] == "c1"
    # call[1]=中间集查询（junction_source eq o1）；call[2]=目标集 in
    assert engine_fake.calls[1][1]["filters"] == [
        {"column": "order_id", "op": "eq", "value": "o1"}
    ]
    assert engine_fake.calls[2][1]["filters"] == [
        {"column": "id", "op": "in", "value": ["c1"]}
    ]


def test_object_endpoints_cross_customer_404(client, db_session):
    ds = seed_dataset(db_session, key="theirs", customer_id=OTHER_CUST)
    entity = make_entity(db_session, ds, name="别家实体", pk=("id",))
    resp = client.get(f"/api/v1/entities/{entity.id}/objects", headers=OWNER)
    assert resp.status_code == 404


# ------------------------------------------------- AI 注入 v2（任务 2.2）


def test_prompt_block_v2_fields(db_session):
    ds = seed_dataset(
        db_session,
        key="customers",
        columns=[{"name": "id", "type": "TEXT"}, {"name": "cname", "type": "TEXT"}],
    )
    entity = make_entity(db_session, ds, name="客户", pk=("id",), display="cname")
    entity.column_notes = {"cname": "客户名称（含曾用名）"}
    db_session.commit()
    block = service.entity_graph_prompt_block(db_session, db_session, CUST)
    assert block is not None
    assert "显示列: cname" in block
    assert "cname(客户名称（含曾用名）)" in block


def test_declared_view_1n_flip(db_session):
    ds_orders = seed_dataset(db_session, key="dv_orders")
    ds_customers = seed_dataset(db_session, key="dv_customers")
    orders = make_entity(db_session, ds_orders, name="订单", pk=("id",))
    customers = make_entity(db_session, ds_customers, name="客户2", pk=("cid",))
    rel = EntityRelationship(
        customer_id=CUST,
        from_entity_id=orders.id,
        from_column="customer_id",
        to_entity_id=customers.id,
        to_column="cid",
        kind="n:1",
        declared_kind="1:n",
    )
    view = service.declared_view(rel)
    assert view["from_entity_id"] == customers.id
    assert view["to_entity_id"] == orders.id
    assert view["kind"] == "1:n"
