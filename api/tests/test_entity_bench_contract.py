"""知识档位一等字段 + 关系更新面 + 实体读面机器通道
（add-entity-bench-contract 全场景测试）。

覆盖 spec 三 requirement：

- 档位字段：PUT 带合法 tier/sensitivity → 落库+出参回显+事件；词表外 422；
  v0 请求（不带新键）零行为变化（出参显式 null）；版本 manifest 键恒出现
  + digest 确定 + 跨键集边界 null 归一；回滚执行恢复两字段；
- 关系更新面：label/tier/sensitivity 更新生效+op=update 事件；结构键携带
  422（extra=forbid 拒绝制）；缺键 422（整量语义）；rid 不存在/跨客户 404；
- 机器通道：白名单内形状与 console 面一致；无/坏 token 401；scope 外 404
  不泄露；缺 customer_id 422；缺配置 503；机器面 GET-only（无写端点）。

迁移 0050 的回填/幂等走 alembic 真链（test_migration_backfill*——先升到
0049 造袋数据，再升 0050 断言）。

环境：ai / entities / datasets 三库合库内存引擎（照 test_entities 先例）。
"""

from __future__ import annotations

import json
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

from fd_wire_api.ai.db import AiBase, reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db
from fd_wire_api.entities.models import Entity, EntityRelationship
from fd_wire_api.entities import service, versioning

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-bench-001"
SUB_OTHER = "sub-bench-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))
CUSTOMER = "cust-001"
OTHER_CUSTOMER = "cust-002"

ORDER_COLUMNS = [
    {"name": "order_id", "type": "TEXT"},
    {"name": "customer_id", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
]
CUSTOMER_COLUMNS = [{"name": "customer_id", "type": "TEXT"}]

MACHINE_TOKEN = "bench-contract-machine-token"


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
def client(monkeypatch, tmp_path, db_session):
    """覆写 ai/entities/datasets 三库会话 + 机器通道 env 的 TestClient。"""
    monkeypatch.setenv("FD_WIRE_INTERNAL_ENTITY_CUSTOMERS", CUSTOMER)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_entities_db] = override_get_db
    app.dependency_overrides[get_datasets_db] = override_get_db
    from fd_wire_api.packs.db import get_db as get_packs_db

    app.dependency_overrides[get_packs_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUST, CUSTOMER), (SUB_OTHER, OTHER_CUSTOMER)],
        internal_token=MACHINE_TOKEN,
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_ai_runtime()


def _seed_dataset(db, *, dataset_id="ds-1", columns=None, customer=CUSTOMER):
    row = Dataset(
        id=dataset_id,
        customer_id=customer,
        key=f"key-{dataset_id}",
        name=f"数据集 {dataset_id}",
        source_kind="upload",
        storage_tier="api",
        columns_json=columns if columns is not None else ORDER_COLUMNS,
        row_count=10,
    )
    db.add(row)
    db.commit()
    return row


def _create_entity(client, *, name="订单", tier=None, sensitivity=None, dataset="ds-1"):
    body = {
        "name": name,
        "dataset_id": dataset,
        "pk_columns": ["order_id"],
    }
    if tier is not None:
        body["tier"] = tier
    if sensitivity is not None:
        body["sensitivity"] = sensitivity
    return client.post("/api/v1/entities", json=body, headers=CUST)


def _create_relationship(client, *, from_id, to_id, column="customer_id", label="下单客户"):
    return client.post(
        f"/api/v1/entities/{from_id}/relationships",
        json={
            "to_entity_id": to_id,
            "from_column": column,
            "to_column": "customer_id",
            "kind": "n:1",
            "label": label,
        },
        headers=CUST,
    )


# ------------------------------------------------------------------ 档位字段


def test_entity_put_with_valid_tier_stores_and_echoes(client, monkeypatch):
    _seed_dataset(next(app.dependency_overrides[get_entities_db]()))
    created = _create_entity(client)
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]

    events_seen = []
    import fd_wire_api.entities.service as svc

    original = svc.versioning.record_event

    def _spy(db, **kwargs):
        events_seen.append(kwargs)
        return original(db, **kwargs)

    monkeypatch.setattr(svc.versioning, "record_event", _spy)
    updated = client.put(
        f"/api/v1/entities/{entity_id}",
        json={
            "name": "订单",
            "dataset_id": "ds-1",
            "pk_columns": ["order_id"],
            "tier": "gold",
            "sensitivity": "confidential",
        },
        headers=CUST,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["tier"] == "gold"
    assert body["sensitivity"] == "confidential"
    assert any(
        e["op"] == "update" and e["target_kind"] == "entity"
        and e["after"]["tier"] == "gold"
        for e in events_seen
    )


def test_entity_vocab_violation_rejected_422(client):
    _seed_dataset(next(app.dependency_overrides[get_entities_db]()))
    created = _create_entity(client)
    entity_id = created.json()["id"]
    for field, bad in (("tier", "diamond"), ("sensitivity", "secret")):
        resp = client.put(
            f"/api/v1/entities/{entity_id}",
            json={
                "name": "订单",
                "dataset_id": "ds-1",
                "pk_columns": ["order_id"],
                field: bad,
            },
            headers=CUST,
        )
        assert resp.status_code == 422, (field, resp.text)
        assert bad in resp.json()["detail"]
        assert "raw/bronze/silver/gold" in resp.json()["detail"] or field == "sensitivity"


def test_entity_v0_request_zero_behavior_change(client):
    """v0 请求（不带新键）行为逐字不变——出参两字段显式 null。"""
    _seed_dataset(next(app.dependency_overrides[get_entities_db]()))
    created = _create_entity(client)
    assert created.status_code == 201
    body = created.json()
    assert body["tier"] is None
    assert body["sensitivity"] is None
    # "tier" 键必须在出参中恒出现（不是缺省省略）
    assert "tier" in body and "sensitivity" in body
    # 图谱节点同形
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    node = graph["nodes"][0]
    assert node["tier"] is None and node["sensitivity"] is None


def test_relationship_create_v0_zero_behavior_change(client):
    """关系声明面不收档位字段（raw 语义）——出参显式 null。"""
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    _seed_dataset(
        entities_db,
        dataset_id="ds-2",
        columns=CUSTOMER_COLUMNS,
    )
    a = _create_entity(client, name="订单").json()["id"]
    b = client.post(
        "/api/v1/entities",
        json={"name": "客户", "dataset_id": "ds-2", "pk_columns": ["customer_id"]},
        headers=CUST,
    ).json()["id"]
    rel = _create_relationship(client, from_id=a, to_id=b)
    assert rel.status_code == 201, rel.text
    body = rel.json()
    assert body["tier"] is None and body["sensitivity"] is None


# ------------------------------------------------------------------ 关系更新面


def test_relationship_update_label_and_tier(client, monkeypatch):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    _seed_dataset(entities_db, dataset_id="ds-2", columns=CUSTOMER_COLUMNS)
    a = _create_entity(client, name="订单").json()["id"]
    b = client.post(
        "/api/v1/entities",
        json={"name": "客户", "dataset_id": "ds-2", "pk_columns": ["customer_id"]},
        headers=CUST,
    ).json()["id"]
    rel = _create_relationship(client, from_id=a, to_id=b).json()["id"]

    events_seen = []
    import fd_wire_api.entities.service as svc

    original = svc.versioning.record_event

    def _spy(db, **kwargs):
        events_seen.append(kwargs)
        return original(db, **kwargs)

    monkeypatch.setattr(svc.versioning, "record_event", _spy)
    updated = client.put(
        f"/api/v1/entities/{a}/relationships/{rel}",
        json={"label": "归属于", "tier": "gold", "sensitivity": "internal"},
        headers=CUST,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["label"] == "归属于"
    assert body["tier"] == "gold"
    assert body["sensitivity"] == "internal"
    assert any(
        e["op"] == "update" and e["target_kind"] == "relationship"
        and e["after"]["tier"] == "gold"
        for e in events_seen
    )


def test_relationship_structural_keys_rejected_422(client):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    _seed_dataset(entities_db, dataset_id="ds-2", columns=CUSTOMER_COLUMNS)
    a = _create_entity(client, name="订单").json()["id"]
    b = client.post(
        "/api/v1/entities",
        json={"name": "客户", "dataset_id": "ds-2", "pk_columns": ["customer_id"]},
        headers=CUST,
    ).json()["id"]
    rel = _create_relationship(client, from_id=a, to_id=b).json()["id"]
    for structural in ("kind", "from_column", "to_entity_id", "junction_dataset_id"):
        resp = client.put(
            f"/api/v1/entities/{a}/relationships/{rel}",
            json={"label": "x", "tier": None, "sensitivity": None, structural: "y"},
            headers=CUST,
        )
        assert resp.status_code == 422, (structural, resp.text)
    # 原样未动
    graph = client.get("/api/v1/entities/graph", headers=CUST).json()
    edge = graph["edges"][0]
    assert edge["kind"] == "n:1"


def test_relationship_update_missing_keys_422(client):
    """整量语义：三键必传，缺键 422。"""
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    _seed_dataset(entities_db, dataset_id="ds-2", columns=CUSTOMER_COLUMNS)
    a = _create_entity(client, name="订单").json()["id"]
    b = client.post(
        "/api/v1/entities",
        json={"name": "客户", "dataset_id": "ds-2", "pk_columns": ["customer_id"]},
        headers=CUST,
    ).json()["id"]
    rel = _create_relationship(client, from_id=a, to_id=b).json()["id"]
    resp = client.put(
        f"/api/v1/entities/{a}/relationships/{rel}",
        json={"label": "只改label"},
        headers=CUST,
    )
    assert resp.status_code == 422


def test_relationship_update_404_not_leaking(client):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    _seed_dataset(entities_db, dataset_id="ds-2", columns=CUSTOMER_COLUMNS)
    a = _create_entity(client, name="订单").json()["id"]
    b = client.post(
        "/api/v1/entities",
        json={"name": "客户", "dataset_id": "ds-2", "pk_columns": ["customer_id"]},
        headers=CUST,
    ).json()["id"]
    rel = _create_relationship(client, from_id=a, to_id=b).json()["id"]
    # rid 不存在
    resp = client.put(
        f"/api/v1/entities/{a}/relationships/99999",
        json={"label": None, "tier": None, "sensitivity": None},
        headers=CUST,
    )
    assert resp.status_code == 404
    # 他企（OTHER bearer = 他客户）
    resp = client.put(
        f"/api/v1/entities/{a}/relationships/{rel}",
        json={"label": None, "tier": None, "sensitivity": None},
        headers=OTHER,
    )
    assert resp.status_code == 404


# ------------------------------------------------------------------ 版本化纳管


def test_manifest_keys_constant_and_digest_deterministic(client):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    ent = service.create_entity(
        entities_db,
        entities_db,
        customer_id=CUSTOMER,
        name="订单",
        dataset_id="ds-1",
        pk_columns=["order_id"],
        tier="silver",
        sensitivity="internal",
    )
    entities_db.commit()
    payload = versioning.entity_payload(ent)
    assert payload["tier"] == "silver"
    assert payload["sensitivity"] == "internal"
    digest_a = versioning.manifest_digest({"entities": [payload], "relationships": []})
    digest_b = versioning.manifest_digest({"entities": [payload], "relationships": []})
    assert digest_a == digest_b

    # 跨键集边界：v1 形态（缺两键）归一为 null——与「显式 null」逐值等价
    # （design D5：缺键 ≡ null，diff/回滚 plan 不产生假差异）
    legacy = dict(payload)
    legacy.pop("tier")
    legacy.pop("sensitivity")
    explicit_null = {**payload, "tier": None, "sensitivity": None}
    norm_legacy = versioning.normalize_entity(legacy)
    norm_null = versioning.normalize_entity(explicit_null)
    assert norm_legacy == norm_null


def test_rollback_restores_tier_fields(client):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    ent = service.create_entity(
        entities_db,
        entities_db,
        customer_id=CUSTOMER,
        name="订单",
        dataset_id="ds-1",
        pk_columns=["order_id"],
        tier="gold",
        sensitivity="confidential",
    )
    entities_db.commit()
    manifest = {"entities": [versioning.entity_payload(ent)], "relationships": []}
    # 当前态改掉档位（模拟升格链回退场景）
    ent.tier = "raw"
    entities_db.commit()
    plan = versioning.rollback_plan(
        versioning.build_manifest(entities_db, customer_id=CUSTOMER), manifest
    )
    result = versioning.execute_rollback(
        entities_db, entities_db, customer_id=CUSTOMER, plan=plan
    )
    assert result["executed"], result
    entities_db.refresh(ent)
    assert ent.tier == "gold"
    assert ent.sensitivity == "confidential"


# ------------------------------------------------------------------ 机器通道


def test_machine_face_shape_matches_console(client):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    _seed_dataset(entities_db, dataset_id="ds-2", columns=CUSTOMER_COLUMNS)
    a = _create_entity(client, name="订单", tier="silver", sensitivity="internal").json()["id"]
    b = client.post(
        "/api/v1/entities",
        json={"name": "客户", "dataset_id": "ds-2", "pk_columns": ["customer_id"]},
        headers=CUST,
    ).json()["id"]
    _create_relationship(client, from_id=a, to_id=b)

    machine_list = client.get(
        "/api/v1/internal/entities",
        params={"customer_id": CUSTOMER},
        headers={"X-Internal-Token": MACHINE_TOKEN},
    )
    console_list = client.get("/api/v1/entities", headers=CUST)
    assert machine_list.status_code == 200
    assert machine_list.json() == console_list.json()

    machine_graph = client.get(
        "/api/v1/internal/entities/graph",
        params={"customer_id": CUSTOMER},
        headers={"X-Internal-Token": MACHINE_TOKEN},
    )
    console_graph = client.get("/api/v1/entities/graph", headers=CUST)
    assert machine_graph.status_code == 200
    assert machine_graph.json() == console_graph.json()
    node = machine_graph.json()["nodes"][0]
    assert node["tier"] == "silver"


def test_machine_face_auth_matrix(client, monkeypatch):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    # 无 token → 401
    resp = client.get("/api/v1/internal/entities", params={"customer_id": CUSTOMER})
    assert resp.status_code == 401
    # 坏 token → 401
    resp = client.get(
        "/api/v1/internal/entities",
        params={"customer_id": CUSTOMER},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 401
    # 缺配置 → 503 报精确缺项
    monkeypatch.delenv("FD_WIRE_INTERNAL_TOKEN", raising=False)
    resp = client.get(
        "/api/v1/internal/entities",
        params={"customer_id": CUSTOMER},
        headers={"X-Internal-Token": MACHINE_TOKEN},
    )
    assert resp.status_code == 503
    assert "FD_WIRE_INTERNAL_TOKEN" in resp.json()["detail"]


def test_machine_face_out_of_scope_404_not_leaking(client):
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    resp = client.get(
        "/api/v1/internal/entities",
        params={"customer_id": OTHER_CUSTOMER},
        headers={"X-Internal-Token": MACHINE_TOKEN},
    )
    assert resp.status_code == 404
    # 缺 customer_id → 422
    resp = client.get(
        "/api/v1/internal/entities", headers={"X-Internal-Token": MACHINE_TOKEN}
    )
    assert resp.status_code == 422


def test_machine_face_is_read_only():
    """硬边界：机器面路由注册面无任何写方法。"""
    from fd_wire_api.entities.internal import router

    for route in router.routes:
        methods = getattr(route, "methods", set())
        assert methods <= {"GET"}, f"机器面出现非只读路由: {route.path} {methods}"
    assert len(router.routes) == 2  # list + graph


# ------------------------------------------------------------------ 迁移 0050


@pytest.fixture()
def alembic_to_0049(tmp_path):
    """alembic 真链升到 0049（entities 表 + 袋数据播种基座）。"""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    api_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    db_url = f"sqlite:///{tmp_path}/bench_0050.db"
    os.environ["DATABASE_URL"] = db_url
    # 停在 0050 前一档（0049）——先建表，再播种袋数据，由测试升级到 head 触发回填
    command.upgrade(cfg, "0049_ontology_versioning")
    return cfg, db_url


def _seed_bag_rows(db_url: str) -> None:
    from sqlalchemy import text

    engine = create_engine(db_url)
    stamp = (
        "platform=knowledge-bench; tier=gold; sensitivity=confidential; "
        "idempotency_key=abc; reviewer=op-1; review_at=2026-11-05T00:00:00"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO entities (customer_id, name, dataset_id, pk_columns,"
                " column_notes, created_at, updated_at)"
                " VALUES (:c, :n, :d, :p, :notes, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            [
                {
                    "c": "cust-x",
                    "n": "带袋订单",
                    "d": "ds-1",
                    "p": '["order_id"]',
                    "notes": json.dumps(
                        {"order_id": "金额", "_bench": stamp}
                    ),
                },
                {
                    "c": "cust-x",
                    "n": "无袋实体",
                    "d": "ds-2",
                    "p": '["order_id"]',
                    "notes": None,
                },
                {
                    "c": "cust-x",
                    "n": "词表外袋",
                    "d": "ds-3",
                    "p": '["order_id"]',
                    "notes": json.dumps(
                        {"_bench": "platform=knowledge-bench; tier=diamond"}
                    ),
                },
            ],
        )
    engine.dispose()


def test_migration_backfill_and_idempotent(alembic_to_0049):
    """回填：袋标记升一等（词表内）；词表外跳过；袋逐字保留；重跑零写。"""
    from alembic import command

    cfg, db_url = alembic_to_0049
    _seed_bag_rows(db_url)
    command.upgrade(cfg, "head")

    from sqlalchemy import inspect, text

    engine = create_engine(db_url)
    inspector = inspect(engine)
    ent_cols = {c["name"] for c in inspector.get_columns("entities")}
    rel_cols = {c["name"] for c in inspector.get_columns("entity_relationships")}
    assert {"tier", "sensitivity"} <= ent_cols
    assert {"tier", "sensitivity"} <= rel_cols

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT name, tier, sensitivity, column_notes FROM entities")
        ).mappings().all()
    by_name = {row["name"]: row for row in rows}
    stamped = by_name["带袋订单"]
    assert stamped["tier"] == "gold"
    assert stamped["sensitivity"] == "confidential"
    # 袋逐字保留
    notes = json.loads(stamped["column_notes"])
    assert notes["_bench"].startswith("platform=knowledge-bench")
    assert notes["order_id"] == "金额"
    # 无袋 / 词表外 → 不造假
    assert by_name["无袋实体"]["tier"] is None
    assert by_name["词表外袋"]["tier"] is None
    engine.dispose()

    # 幂等：重跑 head 零写（值已在位 → skip 分支）
    before = {row["name"]: (row["tier"], row["sensitivity"]) for row in rows}
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        rows_after = conn.execute(
            text("SELECT name, tier, sensitivity FROM entities")
        ).mappings().all()
    after = {row["name"]: (row["tier"], row["sensitivity"]) for row in rows_after}
    assert before == after
    engine.dispose()


# ------------------------------------------------------------------ 袋键豁免（实施期缺陷回归）


def test_bench_bag_key_bypasses_column_snapshot_check(client):
    """_bench 元数据袋键不参与列快照校验（2026-11-05 真回写 422 缺陷回归）。

    实体 PUT 携带 column_notes._bench（值含 reviewer/idempotency 溯源串、
    超 50 字）应通过；真实列名缺失仍拒。
    """
    entities_db = next(app.dependency_overrides[get_entities_db]())
    _seed_dataset(entities_db)
    created = _create_entity(client)
    entity_id = created.json()["id"]
    long_stamp = "platform=knowledge-bench; tier=gold; sensitivity=internal; reviewer=op@x; review_at=2026-11-05T00:00:00"
    resp = client.put(
        f"/api/v1/entities/{entity_id}",
        json={
            "name": "订单",
            "dataset_id": "ds-1",
            "pk_columns": ["order_id"],
            "column_notes": {"_bench": long_stamp},
        },
        headers=CUST,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["column_notes"]["_bench"] == long_stamp
    # 普通列名不在快照仍拒（豁免只限下划线前缀）
    resp = client.put(
        f"/api/v1/entities/{entity_id}",
        json={
            "name": "订单",
            "dataset_id": "ds-1",
            "pk_columns": ["order_id"],
            "column_notes": {"not_a_column": "x"},
        },
        headers=CUST,
    )
    assert resp.status_code == 422
