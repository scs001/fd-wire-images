"""关系物化端点测试（wire-entity-link 任务 2.2，design D2）。

覆盖：
- 成功物化：201 DatasetOut（source_kind=derived、source_ref=左集 key、
  列/体量以引擎 overview 回填——撞名 right_ 前缀列自然入 columns）；
  引擎 link 载荷四元组 + actor/channel 契约逐字段断言；
- 端点参数只是定位入口：从 to 端实体路径触发，左/右仍按关系方向；
- 档位闸：任一端数据集 api 档（免费档）→ 403 PLAN_TIER_REQUIRED、零引擎；
- 404 矩阵：关系失存 / 非端点 / 他企 / 数据集失存（drift）；
- 列漂移：from_column/to_column 已不在数据集当前列 → 422 指名、零引擎；
- 旧引擎（未滚动 cell 镜像）unknown tool → 502 带引擎原文；
- EngineUnavailable → 502 fail-closed；引擎域错误 → 400 如实；
- 零半写：引擎失败/配额满时不落子集行（child 登记仅在引擎成功后）；
- 配额满 → 429（quota: 前缀，零引擎调用）。

环境：ai / entities / datasets 三库合库内存引擎（生产分库拓扑由路由依赖
签名保证——get_entities_db 与 get_datasets_db 各自注入，照 test_entities
先例）；引擎假件脚本化 link/overview 应答与故障注入。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.ai.loop import SYSTEM_PROMPT
from fd_wire_api.ai.protocols import LlmAssistantMessage
from fd_wire_api.ai.routes import get_llm_client, get_tool_executor
from fd_wire_api.app import app
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    EngineUnavailable,
    TOOL_LINK,
    TOOL_OVERVIEW,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db

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

#: 引擎 LEFT JOIN 后的连接列快照（右集 customer_id 撞名 → right_ 前缀，
#: name 未撞名保留原名）——overview 回填以此为准
LINKED_COLUMNS = [
    {"name": "order_id", "type": "TEXT"},
    {"name": "customer_id", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
    {"name": "right_customer_id", "type": "TEXT"},
    {"name": "name", "type": "TEXT"},
]


class LinkEngine:
    """引擎假件：记录调用；link/overview 应答脚本化 + 故障注入。

    overview 对「link 刚产出的 child key」返回 link_rows/link_columns
    （模拟引擎 overview 列真相回填——撞名前缀列由引擎侧产出）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.link_result: dict = {"rows": 3}
        self.link_rows: int = 5
        self.link_columns: list = [dict(c) for c in LINKED_COLUMNS]
        self.fail_tools: dict[str, Exception] = {}
        self._child_key: str | None = None

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        if tool in self.fail_tools:
            raise self.fail_tools[tool]
        if tool == TOOL_LINK:
            self._child_key = str(arguments.get("child_key"))
            return dict(self.link_result)
        if tool == TOOL_OVERVIEW:
            key = arguments.get("dataset_key")
            if key == self._child_key:
                return {
                    "rows": self.link_rows,
                    "size_bytes": 4096,
                    "columns": [dict(c) for c in self.link_columns],
                }
            return {"rows": 0, "size_bytes": 0, "columns": []}
        return {"ok": True}

    def calls_for(self, tool: str) -> list[dict]:
        return [args for name, args in self.calls if name == tool]


class FakeLlm:
    """脚本化 LlmClient（app 依赖覆写用，本文件不触发真实对话）。"""

    def __init__(self) -> None:
        self.script = [LlmAssistantMessage(content="构建完成。")]
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools):
        self.calls.append(messages)
        return self.script.pop(0)


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
def engine_fake() -> LinkEngine:
    return LinkEngine()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session, engine_fake):
    """覆写 ai/entities/datasets 三库会话 + 引擎工厂假件的 TestClient。"""
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
    app.dependency_overrides[get_llm_client] = lambda: FakeLlm()
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
    tier: str = "cell",
    size_bytes: int = 0,
) -> str:
    """直插一条数据集登记（物化两端默认 cell 档），返回 dataset id。"""
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name or f"{key} 数据源",
        source_kind="upload",
        storage_tier=tier,
        columns_json=columns if columns is not None else ORDER_COLUMNS,
        row_count=row_count,
        size_bytes=size_bytes,
    )
    db_session.add(dataset)
    db_session.commit()
    return dataset.id


def create_entity(
    client,
    *,
    dataset_id: str,
    name: str,
    pk: list[str],
    headers: dict = CUST,
):
    return client.post(
        "/api/v1/entities",
        json={
            "name": name,
            "dataset_id": dataset_id,
            "pk_columns": pk,
            "description": None,
        },
        headers=headers,
    )


def seed_graph(db_session, client):
    """标准两实体一关系（两端 cell 档）：订单.customer_id →(n:1) 客户.customer_id。"""
    orders_id = seed_dataset(db_session, key="orders", name="订单表")
    customers_id = seed_dataset(
        db_session,
        key="customers",
        name="客户表",
        columns=CUSTOMER_COLUMNS,
        row_count=7,
    )
    order = create_entity(
        client, dataset_id=orders_id, name="订单", pk=["order_id"]
    )
    customer = create_entity(
        client, dataset_id=customers_id, name="客户", pk=["customer_id"]
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


def materialize(
    client, seeded, *, entity_id=None, rel_id=None, headers=CUST, json_body=None
):
    return client.post(
        f"/api/v1/entities/{entity_id if entity_id is not None else seeded['order']['id']}"
        f"/relationships/{rel_id if rel_id is not None else seeded['rel']['id']}"
        "/materialize",
        headers=headers,
        json=json_body if json_body is not None else {},
    )


def derived_datasets(db_session, customer_id: str = "cust-001") -> list[Dataset]:
    return list(
        db_session.execute(
            select(Dataset).where(
                Dataset.customer_id == customer_id,
                Dataset.source_kind == "derived",
            )
        ).scalars()
    )


# ------------------------------------------------------------------ 成功路径


def test_materialize_success_payload_contract_and_backfill(
    client, db_session, engine_fake
) -> None:
    """成功物化：201 DatasetOut；link 载荷四元组+actor 契约逐字段；
    列/体量以引擎 overview 回填（撞名 right_ 前缀列、行数取回填值）。"""
    seeded = seed_graph(db_session, client)

    resp = materialize(client, seeded)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_kind"] == "derived"
    assert body["source_ref"] == "orders"  # 左集 key（管线图左父边）
    assert body["name"] == "订单×客户 连接"  # 缺省名：左实体名×右实体名 连接
    assert body["storage_tier"] == "cell"
    assert body["row_count"] == engine_fake.link_rows  # 引擎 overview 回填
    assert body["size_bytes"] == 4096
    assert [c["name"] for c in body["columns"]] == [
        "order_id",
        "customer_id",
        "amount",
        "right_customer_id",  # 右集 customer_id 撞名 → right_ 前缀
        "name",  # 未撞名右列保留原名
    ]

    # link 载荷契约：四元组（左集 key/左列/右集 key/右列）+ child/actor/channel
    calls = engine_fake.calls_for(TOOL_LINK)
    assert len(calls) == 1
    args = calls[0]
    assert args == {
        "child_key": body["key"],
        "left_key": "orders",
        "left_column": "customer_id",
        "right_key": "customers",
        "right_column": "customer_id",
        "actor": "customer:cust-001",
        "channel": "console",
    }
    # 登记落在 datasets 库（子集行 source_ref=左集 key）
    children = derived_datasets(db_session)
    assert len(children) == 1
    assert children[0].key == body["key"]
    assert children[0].source_ref == "orders"


def test_materialize_custom_name_and_to_side_entrypoint(
    client, db_session, engine_fake
) -> None:
    """自定义名生效；从 to 端实体路径触发同样成立（端点参数只是定位入口，
    左/右仍按关系 from→to 方向）。"""
    seeded = seed_graph(db_session, client)
    resp = materialize(
        client,
        seeded,
        entity_id=seeded["customer"]["id"],  # 右端实体路径
        json_body={"name": "订单宽表"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "订单宽表"
    args = engine_fake.calls_for(TOOL_LINK)[0]
    assert args["left_key"] == "orders" and args["right_key"] == "customers"
    assert args["left_column"] == "customer_id" and args["right_column"] == "customer_id"


def test_materialize_name_over_200_rejected(client, db_session, engine_fake) -> None:
    """name 超 200 上限 → 422（结构校验，零引擎零半写）。"""
    seeded = seed_graph(db_session, client)
    resp = materialize(client, seeded, json_body={"name": "长" * 201})
    assert resp.status_code == 422
    assert engine_fake.calls == []
    assert derived_datasets(db_session) == []


# ------------------------------------------------------------------ 档位闸


def test_materialize_free_tier_either_side_403(client, db_session, engine_fake) -> None:
    """任一端数据集 api 档（免费档）→ 403 PLAN_TIER_REQUIRED、零引擎、零半写。"""
    # 左端 api 档
    seeded = seed_graph(db_session, client)
    db_session.get(Dataset, seeded["orders_dataset"]).storage_tier = "api"
    db_session.commit()
    resp = materialize(client, seeded)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLAN_TIER_REQUIRED"
    assert engine_fake.calls == []
    assert derived_datasets(db_session) == []

    # 右端 api 档（左端恢复 cell）
    db_session.get(Dataset, seeded["orders_dataset"]).storage_tier = "cell"
    db_session.get(Dataset, seeded["customers_dataset"]).storage_tier = "api"
    db_session.commit()
    right = materialize(client, seeded)
    assert right.status_code == 403
    assert right.json()["detail"]["code"] == "PLAN_TIER_REQUIRED"
    assert engine_fake.calls == []
    assert derived_datasets(db_session) == []


# ------------------------------------------------------------------ 404 矩阵


def test_materialize_resolution_404_matrix(client, db_session, engine_fake) -> None:
    """关系失存 / 非端点实体 / 他企 / 数据集失存（drift）→ 一律 404 不泄露。"""
    seeded = seed_graph(db_session, client)

    # 关系不存在
    assert materialize(client, seeded, rel_id=99999).status_code == 404

    # 端点实体不是该关系的端点（第三方实体路径）
    staff_id = seed_dataset(db_session, key="staff", columns=STAFF_COLUMNS)
    staff = create_entity(
        client, dataset_id=staff_id, name="员工", pk=["emp_id"]
    ).json()
    not_endpoint = materialize(client, seeded, entity_id=staff["id"])
    assert not_endpoint.status_code == 404

    # 他企对本企关系物化 → 404
    assert materialize(client, seeded, headers=OTHER).status_code == 404

    # 右端数据集失存（drift：实体还在、数据集行已删）
    db_session.delete(db_session.get(Dataset, seeded["customers_dataset"]))
    db_session.commit()
    drifted = materialize(client, seeded)
    assert drifted.status_code == 404
    assert "不存在" in drifted.json()["detail"]

    assert engine_fake.calls == []
    assert derived_datasets(db_session) == []


# ------------------------------------------------------------------ 列漂移


def test_materialize_column_drift_422(client, db_session, engine_fake) -> None:
    """from_column/to_column 已不在数据集当前列 → 422 指名（防声明后的
    漂移窗口）、零引擎、零半写。"""
    seeded = seed_graph(db_session, client)

    # 左列漂移：orders 集重建列快照（去掉 customer_id）
    left = db_session.get(Dataset, seeded["orders_dataset"])
    left.columns_json = [c for c in ORDER_COLUMNS if c["name"] != "customer_id"]
    db_session.commit()
    drifted_left = materialize(client, seeded)
    assert drifted_left.status_code == 422
    assert "customer_id" in drifted_left.json()["detail"]
    assert "列漂移" in drifted_left.json()["detail"]

    # 右列漂移：customers 集去掉 customer_id
    left.columns_json = list(ORDER_COLUMNS)
    right = db_session.get(Dataset, seeded["customers_dataset"])
    right.columns_json = [{"name": "name", "type": "TEXT"}]
    db_session.commit()
    drifted_right = materialize(client, seeded)
    assert drifted_right.status_code == 422
    assert "customer_id" in drifted_right.json()["detail"]
    assert "列漂移" in drifted_right.json()["detail"]

    assert engine_fake.calls == []
    assert derived_datasets(db_session) == []


# ------------------------------------------------------------------ 引擎错误矩阵


def test_materialize_old_engine_unknown_tool_502_with_original_text(
    client, db_session, engine_fake
) -> None:
    """旧 cell 镜像（未含 link 工具）：unknown tool → 502，detail 带引擎
    原文（如实不伪造），零半写。"""
    seeded = seed_graph(db_session, client)
    engine_fake.fail_tools[TOOL_LINK] = EngineCallError(
        "引擎tools/call customer_dataset_link 返回 JSON-RPC 错误: -32602 "
        "Unknown tool: customer_dataset_link"
    )
    resp = materialize(client, seeded)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "Unknown tool: customer_dataset_link" in detail  # 引擎原文
    assert "customer_dataset_link" in detail
    assert derived_datasets(db_session) == []  # 零半写


def test_materialize_engine_unavailable_502(client, db_session, engine_fake) -> None:
    """EngineUnavailable → 502 fail-closed（engine_unavailable 前缀），零半写。"""
    seeded = seed_graph(db_session, client)
    engine_fake.fail_tools[TOOL_LINK] = EngineUnavailable(
        "customer cust-001 的 cell 未开通（AI_CELL_ENDPOINT_MAP 无映射且约定派生失败）"
    )
    resp = materialize(client, seeded)
    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("engine_unavailable")
    assert derived_datasets(db_session) == []


def test_materialize_engine_domain_error_400_zero_half_write(
    client, db_session, engine_fake
) -> None:
    """引擎域错误（行超限）→ 400 如实带原文；child 只在引擎成功后登记。"""
    seeded = seed_graph(db_session, client)
    engine_fake.fail_tools[TOOL_LINK] = EngineCallError(
        "引擎工具报错: 行数 600000 超过单数据集上限 500000——整批拒绝，子集保持原状"
    )
    resp = materialize(client, seeded)
    assert resp.status_code == 400
    assert "引擎拒绝" in resp.json()["detail"]
    assert "整批拒绝" in resp.json()["detail"]  # 引擎原文如实
    assert derived_datasets(db_session) == []  # 零半写


# ------------------------------------------------------------------ 配额


def test_materialize_quota_full_429(client, db_session, engine_fake) -> None:
    """存储配额满 → 429（quota: 前缀），引擎零调用、零半写。"""
    seeded = seed_graph(db_session, client)
    # 塞一个占满存储配额的数据集（默认上限 1GB——free 档无 storage_bytes
    # 分层配置，回落全局缺省；2GB 必然越限）
    seed_dataset(
        db_session,
        key="filler",
        columns=[{"name": "x", "type": "TEXT"}],
        size_bytes=2 * 1024**3,
    )
    resp = materialize(client, seeded)
    assert resp.status_code == 429
    assert resp.json()["detail"].startswith("quota:")
    assert engine_fake.calls == []
    assert derived_datasets(db_session) == []


# ------------------------------------------------------------------ m:n 桥展开
# （wire-entity-mn-materialize：物化端点放开 m:n，经中间数据集展开关系对）


def seed_mn_graph(db_session, client, *, junction_tier: str = "cell",
                  junction_row_count: int = 6):
    """m:n 标准图：订单 ↔ 客户 经中间集 order_customers（三端默认 cell 档）。"""
    orders_id = seed_dataset(db_session, key="orders", name="订单表")
    customers_id = seed_dataset(
        db_session, key="customers", name="客户表", columns=CUSTOMER_COLUMNS
    )
    junction_id = seed_dataset(
        db_session,
        key="order_customers",
        name="订单客户桥",
        columns=[
            {"name": "order_id", "type": "TEXT"},
            {"name": "customer_id", "type": "TEXT"},
        ],
        row_count=junction_row_count,
        tier=junction_tier,
    )
    order = create_entity(client, dataset_id=orders_id, name="订单", pk=["order_id"])
    customer = create_entity(
        client, dataset_id=customers_id, name="客户", pk=["customer_id"]
    )
    assert order.status_code == 201 and customer.status_code == 201
    rel = client.post(
        f"/api/v1/entities/{order.json()['id']}/relationships",
        json={
            "to_entity_id": customer.json()["id"],
            "from_column": "order_id",
            "to_column": "customer_id",
            "kind": "m:n",
            "junction_dataset_id": junction_id,
            "junction_source_column": "order_id",
            "junction_target_column": "customer_id",
        },
        headers=CUST,
    )
    assert rel.status_code == 201, rel.text
    return {
        "orders_dataset": orders_id,
        "customers_dataset": customers_id,
        "junction_dataset": junction_id,
        "order": order.json(),
        "customer": customer.json(),
        "rel": rel.json(),
    }


def test_materialize_mn_success_bridge_counts_and_parents(
    client, db_session, engine_fake
) -> None:
    """m:n 物化成功：junction 三要素随调用下发；响应带诚实计数（估算/实数/
    孤儿桥）；中间集入父集边（三父）。"""
    seeded = seed_mn_graph(db_session, client)
    engine_fake.link_result = {"rows": 4, "orphan_dropped": 2}

    resp = materialize(client, seeded)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_ref"] == "orders"
    assert body["name"] == "订单×客户 连接"
    # 诚实计数：估算=桥行数（6）、实数=展开对数（4）、孤儿桥（2）
    assert body["materialization"] == {
        "estimate_rows": 6,
        "rows": 4,
        "orphan_dropped": 2,
    }

    # 引擎载荷：junction 三要素逐字段
    calls = engine_fake.calls_for(TOOL_LINK)
    assert len(calls) == 1
    args = calls[0]
    assert args["left_column"] == "order_id"
    assert args["right_column"] == "customer_id"
    assert args["junction_left_column"] == "order_id"
    assert args["junction_right_column"] == "customer_id"
    assert args["junction_key"]  # 中间集 key（allocate 生成）

    # 父集边：左 + 右 + 中间集（sensitivity 闭包数据源完整性）
    from fd_wire_api.assets.models import DatasetParentRef

    parents = list(
        db_session.execute(
            select(DatasetParentRef).where(
                DatasetParentRef.child_dataset_id == body["id"]
            )
        ).scalars()
    )
    assert len(parents) == 3
    parent_ids = {p.parent_dataset_id for p in parents}
    assert seeded["junction_dataset"] in parent_ids


def test_materialize_mn_junction_free_tier_403(client, db_session, engine_fake) -> None:
    """三端档位闸：中间集免费档 → 403 如实（两端 cell 也拦）。"""
    seeded = seed_mn_graph(db_session, client, junction_tier="api")
    resp = materialize(client, seeded)
    assert resp.status_code == 403, resp.text
    assert engine_fake.calls_for(TOOL_LINK) == []  # 零半写


def test_materialize_mn_estimate_gate_422(client, db_session, engine_fake) -> None:
    """估算闸：桥行数超 datasets 切片行数上限单一真源 → 422 给数、零写。"""
    seeded = seed_mn_graph(db_session, client, junction_row_count=600_000)
    resp = materialize(client, seeded)
    assert resp.status_code == 422, resp.text
    assert "600000" in resp.json()["detail"] and "估算" in resp.json()["detail"]
    assert engine_fake.calls_for(TOOL_LINK) == []


def test_materialize_mn_junction_column_drift_422(
    client, db_session, engine_fake
) -> None:
    """桥列漂移：junction_source_column 被同步删除 → 422 指名列名、零半写。"""
    seeded = seed_mn_graph(db_session, client)
    junction = db_session.get(Dataset, seeded["junction_dataset"])
    junction.columns_json = [{"name": "customer_id", "type": "TEXT"}]
    db_session.commit()

    resp = materialize(client, seeded)
    assert resp.status_code == 422, resp.text
    assert "order_id" in resp.json()["detail"]
    assert engine_fake.calls_for(TOOL_LINK) == []


def test_materialize_mn_junction_missing_404(client, db_session, engine_fake) -> None:
    """中间集失存（声明后删除）→ 404 如实。"""
    seeded = seed_mn_graph(db_session, client)
    junction = db_session.get(Dataset, seeded["junction_dataset"])
    db_session.delete(junction)
    db_session.commit()

    resp = materialize(client, seeded)
    assert resp.status_code == 404, resp.text
    assert "中间数据集" in resp.json()["detail"]
