"""实体语义层 API 测试（wire-entity-layer 任务 1.x/2.x）。

覆盖：
- 实体校验矩阵：pk 越列/空/重复、name 空/超长、dataset 不存在/他企
  （统一 422 不泄露）、重复 name 409、重复绑定数据集 409（detail 带
  冲突方）、零半写；
- 关系纪律：目标列必须 ∈ 目标实体主键（violation 文案指明）、源列 ∈
  源集列、kind 枚举、四元组唯一 409、目标实体他企/不存在 404、
  自环（同一实体）允许；
- 级联删除：先删两端关系再删实体（计数进响应），图谱同步消失；
- 客户域隔离：他企一律 404（不泄露）、列表/图谱互不可见；
- 图谱形状：nodes 含数据集摘要 {id,key,name,row_count,storage_tier} 与
  pk_columns；edges 四元组+kind+label；空图谱 200 空集；
- AI 注入三态：有图谱→捕获的 system 含实体行与关系行；空图谱→与
  SYSTEM_PROMPT 常量逐字节相等（零漂移）；>50 实体→截断标注在；
- 免费档零引擎：实体/关系全流程引擎工厂零调用（纯元数据声明层）。

环境：ai / entities / datasets 三库合库内存引擎（生产分库拓扑由路由
依赖签名保证——get_entities_db 与 get_datasets_db 各自注入，照
test_form_artifacts 先例）。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.ai.loop import SYSTEM_PROMPT
from fd_wire_api.ai.protocols import LlmAssistantMessage
from fd_wire_api.ai.routes import get_llm_client, get_tool_executor
from fd_wire_api.app import app
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.ingest import get_engine_client_factory
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db
from fd_wire_api.entities.models import Entity
from fd_wire_api.entities.service import entity_graph_prompt_block

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-ent-001"
SUB_OTHER = "sub-ent-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

ORDER_COLUMNS = [
    {"name": "order_id", "type": "TEXT"},
    {"name": "customer_id", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
]
CUSTOMER_COLUMNS = [
    {"name": "customer_id", "type": "TEXT"},
    {"name": "name", "type": "TEXT"},
]
STAFF_COLUMNS = [
    {"name": "emp_id", "type": "TEXT"},
    {"name": "manager_id", "type": "TEXT"},
]


class RecordingEngine:
    """引擎假件：记录 tools/call 调用（实体全流程应为零调用）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        return {"ok": True}


class FakeLlm:
    """脚本化 LlmClient：按序弹出结果；记录收到的 wire（system 断言用）。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools):
        self.calls.append(messages)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, tool_name, args):
        self.calls.append((tool_name, args))
        return {"ok": True, "rows": 0}


@pytest.fixture()
def db_session():
    """独立内存库（ai + entities + datasets 三套表同引擎——夹具合库，
    生产分库拓扑由路由依赖签名与各自 override 保持）。"""
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
def engine_fake() -> RecordingEngine:
    return RecordingEngine()


@pytest.fixture()
def fake_llm() -> FakeLlm:
    return FakeLlm([LlmAssistantMessage(content="构建完成。")])


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session, engine_fake, fake_llm):
    """覆写 ai/entities/datasets 三库会话（各自 override——依赖签名与生产
    分库拓扑一致）+ 引擎工厂/LLM/工具执行假件的 TestClient。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_ai_db] = override_get_db
    app.dependency_overrides[get_entities_db] = override_get_db
    app.dependency_overrides[get_datasets_db] = override_get_db
    from fd_wire_api.packs.db import get_db as get_packs_db
    app.dependency_overrides[get_packs_db] = override_get_db
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    app.dependency_overrides[get_tool_executor] = lambda: FakeExecutor()
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: engine_fake
    )
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUST, "cust-001"), (SUB_OTHER, "cust-002")],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


# ------------------------------------------------------------------ 助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = "cust-001",
    key: str = "orders",
    name: str | None = None,
    columns: list | None = None,
    row_count: int = 42,
    tier: str = "api",
) -> str:
    """直插一条数据集登记，返回 dataset id。"""
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name or f"{key} 数据源",
        source_kind="upload",
        storage_tier=tier,
        columns_json=columns if columns is not None else ORDER_COLUMNS,
        row_count=row_count,
    )
    db_session.add(dataset)
    db_session.commit()
    return dataset.id


def create_entity(
    client,
    *,
    dataset_id: str,
    name: str = "订单",
    pk: list[str] | None = None,
    headers: dict = CUST,
    description: str | None = None,
):
    return client.post(
        "/api/v1/entities",
        json={
            "name": name,
            "dataset_id": dataset_id,
            "pk_columns": pk or ["order_id"],
            "description": description,
        },
        headers=headers,
    )


def seed_graph(db_session, client):
    """标准两实体一关系：订单.customer_id →(n:1) 客户.customer_id。"""
    orders_id = seed_dataset(db_session, key="orders", name="订单表")
    customers_id = seed_dataset(
        db_session,
        key="customers",
        name="客户表",
        columns=CUSTOMER_COLUMNS,
        row_count=7,
    )
    order = create_entity(client, dataset_id=orders_id, name="订单")
    customer = create_entity(
        client,
        dataset_id=customers_id,
        name="客户",
        pk=["customer_id"],
        description="下单主体",
    )
    assert order.status_code == 201, order.text
    assert customer.status_code == 201, customer.text
    rel = client.post(
        f"/api/v1/entities/{order.json()['id']}/relationships",
        json={
            "to_entity_id": customer.json()["id"],
            "from_column": "customer_id",
            "to_column": "customer_id",
            "kind": "n:1",
        },
        headers=CUST,
    )
    assert rel.status_code == 201, rel.text
    return {
        "orders_dataset": orders_id,
        "customers_dataset": customers_id,
        "order": order.json(),
        "customer": customer.json(),
        "rel": rel.json(),
    }


# ------------------------------------------------------------ 实体校验矩阵


def test_entity_create_and_list_with_dataset_summary(client, db_session) -> None:
    """声明实体 201；列表项含数据集摘要 {id,key,name,row_count,storage_tier}。"""
    dataset_id = seed_dataset(
        db_session, key="orders", name="订单表", row_count=42, tier="api"
    )
    resp = create_entity(
        client, dataset_id=dataset_id, name="订单", description="下单事实表"
    )
    assert resp.status_code == 201, resp.text
    entity = resp.json()
    assert entity["name"] == "订单"
    assert entity["pk_columns"] == ["order_id"]
    assert entity["description"] == "下单事实表"
    assert entity["dataset"] == {
        "id": dataset_id,
        "key": "orders",
        "name": "订单表",
        "row_count": 42,
        "storage_tier": "api",
    }

    listed = client.get("/api/v1/entities", headers=CUST).json()["entities"]
    assert len(listed) == 1
    assert listed[0]["id"] == entity["id"]
    assert listed[0]["dataset"]["key"] == "orders"


def test_entity_validation_matrix_422_zero_half_write(client, db_session) -> None:
    """结构/校验矩阵：pk 越列/空/重复、name 空/超长、dataset 缺——逐条 422
    且零半写。"""
    dataset_id = seed_dataset(db_session, key="orders")
    cases = [
        # pk 越列（列名级指名）
        (
            {"name": "订单", "dataset_id": dataset_id, "pk_columns": ["order_id", "nope"]},
            "nope",
        ),
        # pk 空
        (
            {"name": "订单", "dataset_id": dataset_id, "pk_columns": []},
            "主键列",
        ),
        # pk 重复
        (
            {"name": "订单", "dataset_id": dataset_id, "pk_columns": ["a", "a"]},
            "重复",
        ),
        # pk 空字符串项
        (
            {"name": "订单", "dataset_id": dataset_id, "pk_columns": [""]},
            "主键列",
        ),
        # name 空白
        (
            {"name": "   ", "dataset_id": dataset_id, "pk_columns": ["order_id"]},
            "实体名",
        ),
        # name 超长
        (
            {"name": "长" * 101, "dataset_id": dataset_id, "pk_columns": ["order_id"]},
            "100",
        ),
        # dataset 不存在
        (
            {"name": "订单", "dataset_id": "ds-ghost", "pk_columns": ["order_id"]},
            "不存在",
        ),
    ]
    for payload, needle in cases:
        resp = client.post("/api/v1/entities", json=payload, headers=CUST)
        assert resp.status_code == 422, payload
        assert needle in resp.json()["detail"], payload
    # 零半写
    assert client.get("/api/v1/entities", headers=CUST).json() == {"entities": []}


def test_entity_dataset_other_customer_422_uniform(client, db_session) -> None:
    """dataset 指向他企：422 且与不存在同文案（不泄露属主差异）。"""
    other_dataset = seed_dataset(db_session, customer_id="cust-002", key="secret")
    resp = create_entity(client, dataset_id=other_dataset)
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"数据集 {other_dataset} 不存在"
    assert client.get("/api/v1/entities", headers=CUST).json() == {"entities": []}


def test_entity_uniqueness_conflicts_409(client, db_session) -> None:
    """重复业务名 / 重复绑定数据集 → 409，detail 带冲突方。"""
    ds_a = seed_dataset(db_session, key="orders")
    ds_b = seed_dataset(db_session, key="customers", columns=CUSTOMER_COLUMNS)
    assert create_entity(client, dataset_id=ds_a, name="订单").status_code == 201

    # 重复业务名（不同数据集）
    dup_name = create_entity(
        client, dataset_id=ds_b, name="订单", pk=["customer_id"]
    )
    assert dup_name.status_code == 409
    detail = dup_name.json()["detail"]
    assert "订单" in detail and "冲突实体" in detail

    # 重复绑定（同名不同名实体）
    dup_binding = create_entity(
        client, dataset_id=ds_a, name="订单二", pk=["customer_id"]
    )
    assert dup_binding.status_code == 409
    detail = dup_binding.json()["detail"]
    assert "已绑定" in detail and "订单" in detail
    # 失败零半写：仍只有一个实体
    assert len(client.get("/api/v1/entities", headers=CUST).json()["entities"]) == 1


def test_entity_update_revalidation_and_zero_half_write(client, db_session) -> None:
    """PUT 重校验：合法更新生效；重名 409；pk 越列 422 且实体保持原样。"""
    ds_a = seed_dataset(db_session, key="orders")
    ds_b = seed_dataset(db_session, key="customers", columns=CUSTOMER_COLUMNS)
    entity = create_entity(client, dataset_id=ds_a, name="订单").json()
    assert create_entity(
        client, dataset_id=ds_b, name="客户", pk=["customer_id"]
    ).status_code == 201

    # 合法更新（改名 + 换主键列）
    ok = client.put(
        f"/api/v1/entities/{entity['id']}",
        json={"name": "订单事实", "dataset_id": ds_a, "pk_columns": ["order_id", "customer_id"]},
        headers=CUST,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "订单事实"
    assert ok.json()["pk_columns"] == ["order_id", "customer_id"]

    # 重名 → 409
    dup = client.put(
        f"/api/v1/entities/{entity['id']}",
        json={"name": "客户", "dataset_id": ds_a, "pk_columns": ["order_id"]},
        headers=CUST,
    )
    assert dup.status_code == 409

    # pk 越列 → 422 且零半写（实体保持上一次成功态）
    bad = client.put(
        f"/api/v1/entities/{entity['id']}",
        json={"name": "订单事实", "dataset_id": ds_a, "pk_columns": ["nope"]},
        headers=CUST,
    )
    assert bad.status_code == 422
    assert "nope" in bad.json()["detail"]
    current = client.get("/api/v1/entities", headers=CUST).json()["entities"]
    mine = [e for e in current if e["id"] == entity["id"]][0]
    assert mine["name"] == "订单事实"
    assert mine["pk_columns"] == ["order_id", "customer_id"]

    # 换绑他企数据集 → 422
    other_ds = seed_dataset(db_session, customer_id="cust-002", key="secret")
    hijack = client.put(
        f"/api/v1/entities/{entity['id']}",
        json={"name": "订单事实", "dataset_id": other_ds, "pk_columns": ["x"]},
        headers=CUST,
    )
    assert hijack.status_code == 422


# ------------------------------------------------------------- 关系纪律


def test_relationship_target_pk_discipline(client, db_session) -> None:
    """目标列不在目标实体主键集合 → 422 指明纪律文案；不落关系行。"""
    seeded = seed_graph(db_session, client)
    violation = client.post(
        f"/api/v1/entities/{seeded['order']['id']}/relationships",
        json={
            "to_entity_id": seeded["customer"]["id"],
            "from_column": "amount",
            "to_column": "name",  # 客户集有 name 列但不在主键集合
            "kind": "n:1",
        },
        headers=CUST,
    )
    assert violation.status_code == 422
    assert "关系目标必须是目标实体的主键列" in violation.json()["detail"]
    assert "name" in violation.json()["detail"]
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    assert len(graph["edges"]) == 1  # 仅 seed 的那条


def test_relationship_validation_matrix(client, db_session) -> None:
    """kind 枚举 / 源列越列 / 目标实体他企或不存在 / 四元组重复。"""
    seeded = seed_graph(db_session, client)
    order_id = seeded["order"]["id"]
    customer_id = seeded["customer"]["id"]

    # kind 越枚举
    bad_kind = client.post(
        f"/api/v1/entities/{order_id}/relationships",
        json={
            "to_entity_id": customer_id,
            "from_column": "customer_id",
            "to_column": "customer_id",
            "kind": "n:m",
        },
        headers=CUST,
    )
    assert bad_kind.status_code == 422
    assert "n:1" in bad_kind.json()["detail"]

    # 源列不在源数据集列
    bad_source = client.post(
        f"/api/v1/entities/{order_id}/relationships",
        json={
            "to_entity_id": customer_id,
            "from_column": "ghost_col",
            "to_column": "customer_id",
            "kind": "1:1",
        },
        headers=CUST,
    )
    assert bad_source.status_code == 422
    assert "ghost_col" in bad_source.json()["detail"]

    # 目标实体不存在
    ghost = client.post(
        f"/api/v1/entities/{order_id}/relationships",
        json={
            "to_entity_id": 99999,
            "from_column": "customer_id",
            "to_column": "customer_id",
            "kind": "n:1",
        },
        headers=CUST,
    )
    assert ghost.status_code == 404

    # 四元组重复 → 409
    dup = client.post(
        f"/api/v1/entities/{order_id}/relationships",
        json={
            "to_entity_id": customer_id,
            "from_column": "customer_id",
            "to_column": "customer_id",
            "kind": "n:1",
        },
        headers=CUST,
    )
    assert dup.status_code == 409
    assert "冲突关系" in dup.json()["detail"]

    # 零半写：仍只有 seed 的关系
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    assert len(graph["edges"]) == 1


def test_relationship_self_loop_allowed(client, db_session) -> None:
    """自环（同一实体）允许：员工.manager_id →(n:1) 员工.emp_id。"""
    staff_ds = seed_dataset(db_session, key="staff", columns=STAFF_COLUMNS)
    staff = create_entity(
        client, dataset_id=staff_ds, name="员工", pk=["emp_id"]
    ).json()
    rel = client.post(
        f"/api/v1/entities/{staff['id']}/relationships",
        json={
            "to_entity_id": staff["id"],
            "from_column": "manager_id",
            "to_column": "emp_id",
            "kind": "n:1",
            "label": "上级",
        },
        headers=CUST,
    )
    assert rel.status_code == 201, rel.text
    assert rel.json()["label"] == "上级"
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    assert graph["edges"][0]["from_entity_id"] == staff["id"]
    assert graph["edges"][0]["to_entity_id"] == staff["id"]


def test_relationship_delete_endpoint_semantics(client, db_session) -> None:
    """删关系：非该实体端点 → 404；以端点关系可删（任一端操作均可）。"""
    seeded = seed_graph(db_session, client)
    order_id = seeded["order"]["id"]
    customer_id = seeded["customer"]["id"]
    rel_id = seeded["rel"]["id"]

    # 用第三方实体删别人的关系 → 404（须以该实体为端点）
    staff_ds = seed_dataset(db_session, key="staff", columns=STAFF_COLUMNS)
    staff = create_entity(
        client, dataset_id=staff_ds, name="员工", pk=["emp_id"]
    ).json()
    not_endpoint = client.delete(
        f"/api/v1/entities/{staff['id']}/relationships/{rel_id}", headers=CUST
    )
    assert not_endpoint.status_code == 404

    # 目标端（客户）删该关系同样合法
    ok = client.delete(
        f"/api/v1/entities/{customer_id}/relationships/{rel_id}", headers=CUST
    )
    assert ok.status_code == 200
    assert ok.json() == {"id": rel_id, "deleted": True}
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    assert graph["edges"] == []
    assert {n["name"] for n in graph["nodes"]} == {"订单", "客户", "员工"}

    # 已删再删 → 404；order 端删同一条 → 404
    assert (
        client.delete(
            f"/api/v1/entities/{order_id}/relationships/{rel_id}", headers=CUST
        ).status_code
        == 404
    )


# ------------------------------------------------------------- 级联删除


def test_cascade_delete_counts_relationships(client, db_session) -> None:
    """删实体：同事务级联删两端关系，计数进响应，图谱同步消失。"""
    ds_orders = seed_dataset(db_session, key="orders")
    ds_customers = seed_dataset(
        db_session, key="customers", columns=CUSTOMER_COLUMNS
    )
    ds_staff = seed_dataset(db_session, key="staff", columns=STAFF_COLUMNS)
    order = create_entity(client, dataset_id=ds_orders, name="订单").json()
    customer = create_entity(
        client, dataset_id=ds_customers, name="客户", pk=["customer_id"]
    ).json()
    staff = create_entity(
        client, dataset_id=ds_staff, name="员工", pk=["emp_id"]
    ).json()
    # 三条关系都以 customer 为端点（两条入 + 一条出）
    rels = [
        (order, "customer_id", customer, "customer_id"),
        (staff, "manager_id", customer, "customer_id"),
        (customer, "customer_id", order, "order_id"),
    ]
    for src, fcol, dst, tcol in rels:
        resp = client.post(
            f"/api/v1/entities/{src['id']}/relationships",
            json={
                "to_entity_id": dst["id"],
                "from_column": fcol,
                "to_column": tcol,
                "kind": "n:1",
            },
            headers=CUST,
        )
        assert resp.status_code == 201, resp.text

    removed = client.delete(f"/api/v1/entities/{customer['id']}", headers=CUST)
    assert removed.status_code == 200
    assert removed.json() == {
        "id": customer["id"],
        "deleted": True,
        "deleted_relationships": 3,
    }
    # 图谱同步消失：节点少一、边全清（三条都挂着 customer）
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    assert {n["name"] for n in graph["nodes"]} == {"订单", "员工"}
    assert graph["edges"] == []
    # 删除幂等语义：再删 404
    assert (
        client.delete(f"/api/v1/entities/{customer['id']}", headers=CUST).status_code
        == 404
    )


# ------------------------------------------------------------- 隔离


def test_customer_isolation_404_no_leak(client, db_session) -> None:
    """他企一律 404（不泄露存在性）；列表/图谱互不可见。"""
    ds_other = seed_dataset(db_session, customer_id="cust-002", key="secret2")
    ds_own = seed_dataset(db_session, key="orders")
    own = create_entity(client, dataset_id=ds_own, name="订单").json()
    other = client.post(
        "/api/v1/entities",
        json={
            "name": "机密",
            "dataset_id": ds_other,
            "pk_columns": ["order_id"],
        },
        headers=OTHER,
    )
    assert other.status_code == 201, other.text

    # 双向互不可见：他企只见自己的实体，本企只见自己的
    assert [
        e["name"] for e in client.get("/api/v1/entities", headers=OTHER).json()["entities"]
    ] == ["机密"]
    assert {n["name"] for n in client.get("/api/v1/entities/graph", headers=OTHER).json()["nodes"]} == {"机密"}
    assert [
        e["name"] for e in client.get("/api/v1/entities", headers=CUST).json()["entities"]
    ] == ["订单"]
    assert {n["name"] for n in client.get("/api/v1/entities/graph", headers=CUST).json()["nodes"]} == {"订单"}

    # 他企对本企实体 PUT/DELETE/建关系一律 404
    assert (
        client.put(
            f"/api/v1/entities/{own['id']}",
            json={"name": "劫持", "dataset_id": ds_own, "pk_columns": ["order_id"]},
            headers=OTHER,
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/entities/{own['id']}", headers=OTHER).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/entities/{own['id']}/relationships",
            json={
                "to_entity_id": own["id"],
                "from_column": "order_id",
                "to_column": "order_id",
                "kind": "1:1",
            },
            headers=OTHER,
        ).status_code
        == 404
    )
    # 本企实体安然无恙
    assert len(client.get("/api/v1/entities", headers=CUST).json()["entities"]) == 1


# ------------------------------------------------------------- 图谱形状


def test_graph_shape_with_dataset_summary(client, db_session) -> None:
    """图谱同源：nodes 含数据集摘要与主键；edges 四元组+kind+label。"""
    seeded = seed_graph(db_session, client)
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    nodes = {n["name"]: n for n in graph["nodes"]}
    assert set(nodes) == {"订单", "客户"}
    assert nodes["订单"]["pk_columns"] == ["order_id"]
    assert nodes["订单"]["description"] is None
    assert nodes["客户"]["description"] == "下单主体"
    assert nodes["订单"]["dataset"] == {
        "id": seeded["orders_dataset"],
        "key": "orders",
        "name": "订单表",
        "row_count": 42,
        "storage_tier": "api",
    }
    assert nodes["客户"]["dataset"]["row_count"] == 7

    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge == {
        "id": edge["id"],
        "from_entity_id": seeded["order"]["id"],
        "from_column": "customer_id",
        "to_entity_id": seeded["customer"]["id"],
        "to_column": "customer_id",
        "kind": "n:1",
        "label": None,
        # v2：m:n 中间集三键（n:1 关系恒为空）
        "junction_dataset_id": None,
        "junction_source_column": None,
        "junction_target_column": None,
    }


def test_graph_empty_returns_empty_sets(client) -> None:
    """空图谱：200 {nodes: [], edges: []}（不 404）。"""
    resp = client.get("/api/v1/entities/graph", headers=CUST)
    assert resp.status_code == 200
    assert resp.json() == {"nodes": [], "edges": []}


# ------------------------------------------------------- AI 注入三态


def _captured_system(fake_llm) -> str:
    assert fake_llm.calls, "LLM 未被调用"
    wire = fake_llm.calls[0]
    assert wire[0]["role"] == "system"
    return wire[0]["content"]


def test_ai_injection_with_graph(client, db_session, fake_llm) -> None:
    """有图谱：system 在固定文案后追加图谱块（实体行 + 关系行）。"""
    seed_graph(db_session, client)
    resp = client.post(
        "/api/v1/ai/conversations",
        json={"title": "销售分析", "message": "按客户统计订单金额"},
        headers=CUST,
    )
    assert resp.status_code == 201, resp.text
    system = _captured_system(fake_llm)
    assert system.startswith(SYSTEM_PROMPT)
    block = system[len(SYSTEM_PROMPT) :]
    assert block.startswith("\n【本企业实体图谱】（业务语义；列名以此为准）")
    assert "实体「订单」= 数据集 订单表（主键: order_id）" in block
    assert "实体「客户」= 数据集 客户表（主键: customer_id）" in block
    assert "订单.customer_id →(n:1) 客户.customer_id" in block


def test_ai_injection_empty_graph_byte_identical(client, db_session, fake_llm) -> None:
    """空图谱：system 与 SYSTEM_PROMPT 常量逐字节相等（零漂移）。"""
    # 有数据集但无实体声明（确保"空"判定在实体面而非数据集面）
    seed_dataset(db_session, key="orders")
    resp = client.post(
        "/api/v1/ai/conversations",
        json={"message": "帮我分析数据"},
        headers=CUST,
    )
    assert resp.status_code == 201, resp.text
    assert _captured_system(fake_llm) == SYSTEM_PROMPT

    # 续聊（append_and_run 路径）同样零漂移
    conversation_id = resp.json()["id"]
    follow = client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"content": "继续"},
        headers=CUST,
    )
    assert follow.status_code == 200, follow.text
    assert _captured_system(fake_llm) == SYSTEM_PROMPT


def test_ai_injection_truncation_over_50_entities(
    client, db_session, fake_llm
) -> None:
    """>50 实体：注入块截断到 50 并如实标注（不撑爆上下文）。"""
    # 直插 51 个实体（各绑不同 dataset_id 弱引用——服务层直插不经校验）
    for n in range(1, 52):
        db_session.add(
            Entity(
                customer_id="cust-001",
                name=f"实体{n:03d}",
                dataset_id=f"ds-fake-{n:03d}",
                pk_columns=["id"],
            )
        )
    db_session.commit()
    assert entity_graph_prompt_block(db_session, db_session, "cust-001") is not None

    resp = client.post(
        "/api/v1/ai/conversations",
        json={"message": "全量分析"},
        headers=CUST,
    )
    assert resp.status_code == 201, resp.text
    system = _captured_system(fake_llm)
    assert "【本企业实体图谱】" in system
    assert "实体「实体050」" in system  # 前 50 在
    assert "实体「实体051」" not in system  # 第 51 截断
    assert "实体仅列前 50" in system
    assert "完整见控制台实体页" in system


def test_ai_injection_other_customer_isolated(
    client, db_session, fake_llm, monkeypatch
) -> None:
    """图谱注入按客户域隔离：他企对话不见本企实体块（给他企开 cell 使
    LLM 真被调用——否则未开通护栏零 LLM 调用，断言无意义）。"""
    monkeypatch.setenv(
        "AI_CELL_ENDPOINT_MAP",
        "cust-001=http://cell-001/mcp,cust-002=http://cell-002/mcp",
    )
    reset_ai_runtime()  # 端点映射解析有惰性缓存——改 env 后必须清（照 test_ai_loop）
    seed_graph(db_session, client)
    resp = client.post(
        "/api/v1/ai/conversations",
        json={"message": "分析一下"},
        headers=OTHER,
    )
    assert resp.status_code == 201, resp.text
    assert _captured_system(fake_llm) == SYSTEM_PROMPT  # 他企零注入


# ------------------------------------------------------------- 免费档零引擎


def test_free_tier_full_flow_zero_engine_calls(client, db_session, engine_fake) -> None:
    """免费档全程零引擎：实体/关系 CRUD + 图谱全流程引擎工厂零调用。"""
    seeded = seed_graph(db_session, client)
    client.get("/api/v1/entities", headers=CUST)
    client.get("/api/v1/entities/graph", headers=CUST)
    client.put(
        f"/api/v1/entities/{seeded['order']['id']}",
        json={
            "name": "订单",
            "dataset_id": seeded["orders_dataset"],
            "pk_columns": ["order_id"],
            "description": "改述",
        },
        headers=CUST,
    )
    client.delete(
        f"/api/v1/entities/{seeded['order']['id']}/relationships/"
        f"{seeded['rel']['id']}",
        headers=CUST,
    )
    client.delete(f"/api/v1/entities/{seeded['customer']['id']}", headers=CUST)
    client.delete(f"/api/v1/entities/{seeded['order']['id']}", headers=CUST)
    assert client.get("/api/v1/entities", headers=CUST).json() == {"entities": []}
    assert engine_fake.calls == []
