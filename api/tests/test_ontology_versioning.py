"""本体版本机制测试（wire-ontology-versioning 任务 1.3/2.x/3.x/4.x）。

覆盖（spec 场景）：
- 迁移 0049：两表 + 索引建立、可升降（SQLite 双方言自测）。
- 事件流（2.2）：控制台建/改/删实体与关系各留一条事件（before/after 全量）；
  删实体级联删掉的关系逐条留 delete 事件；空转 PUT 不落事件。
- 闸血缘（2.3）：内置声明经 executor 执行 → via=action_gate、source_ref 与
  source_instance_id 与动作实例一致。
- 事件写失败整体失败（2.4）：事件插入抛错 → 本体行不落（同事务）。
- 无改删事件接口（2.5）：本体版本路由集合里没有改/删事件端点。
- 快照（3.1/3.2/3.4/3.5）：版本号单调、同 digest 幂等、无本体不出快照、
  日循环零变更跳过/当日已出跳过/存量本体首版即基线、日版保留清理、
  快照不改变当前态。
- diff（4.1）：字段级 before→after、增删三组、manifest↔当前态互比。
- 端点（4.2/4.3/4.5）：版本列表+未发布计数、diff、导出 json/yaml/出处、
  跨企业 404、回滚 plan 预览、回滚执行（digest 对齐）、回滚再回滚、
  完整性自检（含裸 SQL 逃逸检测）。
- digest 规范（2.1）：键序/条序/类型归一稳定，同内容同 digest。

环境：ai / entities / datasets 三库合库内存引擎（照 test_entities 先例——
生产分库拓扑由路由依赖签名与各自 override 保持）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）；
# 关闭日快照后台循环（个别用例直接调 tick_once 驱动）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")
os.environ.setdefault("ONTOLOGY_SNAPSHOT_ENABLED", "0")

from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.ingest import get_engine_client_factory
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities import service as entities_service
from fd_wire_api.entities import snapshots as snapshot_service
from fd_wire_api.entities import versioning
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db
from fd_wire_api.entities.models import (
    Entity,
    EntityRelationship,
    OntologyEvent,
    OntologySnapshot,
)

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

API_DIR = Path(__file__).resolve().parent.parent

SUB_CUST = "sub-onto-001"
SUB_MEMBER = "sub-onto-002"
SUB_OTHER = "sub-onto-003"
CUST = bearer(mint_id_token(SUB_CUST))
MEMBER = bearer(mint_id_token(SUB_MEMBER))
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


class RecordingEngine:
    """引擎假件：版本机制纯元数据，引擎应零调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        return {"ok": True}


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
def engine_fake() -> RecordingEngine:
    return RecordingEngine()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session, engine_fake):
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
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: engine_fake
    )
    install_fake_jwks(monkeypatch)
    env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUST, "cust-onto"), (SUB_OTHER, "cust-onto-other")],
    )
    # 同队 editor（回滚 owner-only 用例）：成员行而非第二个客户行
    from fd_wire_api.console.models import CustomerMember

    with env.session() as sess:
        sess.add(
            CustomerMember(
                customer_id=env.subs[SUB_CUST],
                logto_sub=SUB_MEMBER,
                role="editor",
            )
        )
        sess.commit()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


# ------------------------------------------------------------------ 助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = "cust-onto",
    key: str = "orders",
    columns: list | None = None,
    tier: str = "api",
) -> str:
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=f"{key} 数据源",
        source_kind="upload",
        storage_tier=tier,
        columns_json=columns if columns is not None else ORDER_COLUMNS,
        row_count=10,
    )
    db_session.add(dataset)
    db_session.commit()
    return dataset.id


def post_entity(client, *, dataset_id: str, name: str = "订单", pk=None, headers=CUST):
    return client.post(
        "/api/v1/entities",
        json={
            "name": name,
            "dataset_id": dataset_id,
            "pk_columns": pk or ["order_id"],
        },
        headers=headers,
    )


def seed_graph(client, db_session):
    """订单(customer_id) →(n:1) 客户(customer_id) 两实体一关系。"""
    orders_dataset = seed_dataset(db_session, key="orders")
    customers_dataset = seed_dataset(
        db_session, key="customers", columns=CUSTOMER_COLUMNS
    )
    order = post_entity(client, dataset_id=orders_dataset, name="订单")
    customer = post_entity(
        client, dataset_id=customers_dataset, name="客户", pk=["customer_id"]
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
    return order.json(), customer.json(), rel.json()


def events(db_session, customer_id: str = "cust-onto") -> list[OntologyEvent]:
    return versioning.list_events(db_session, customer_id=customer_id)


def _rewrite_event_times(db_session, moment: datetime) -> None:
    """把本企业事件时间钉到模拟时间线（假时钟下 created_at 决定「已发布」判定）。"""
    for event in db_session.query(OntologyEvent).all():
        event.created_at = moment
    db_session.commit()


# ------------------------------------------------------- 6.3 事件语义键


def test_events_carry_a_semantic_key_that_matches_the_rows(client, db_session) -> None:
    """基础设施换底 6.3：事件写当时的语义键，且能从 ``target_id`` 反解一致。

    ``target_key`` 是跨环境身份（自增 id 只在本库有意义）；写进去的必须是
    **当时**的名字——事后反解只能得到今天的名字，那就成了会撒谎的台账。
    """
    order, customer, rel = seed_graph(client, db_session)
    db_session.expire_all()

    creates = [e for e in events(db_session) if e.op == versioning.OP_CREATE]
    # seed_graph 建了两个实体一条关系：按 target_id 取，别用 (kind, op) 当键
    # ——那会把「订单」和「客户」两条 create 事件压成一条。
    entity_event = [
        e for e in creates
        if e.target_kind == versioning.KIND_ENTITY and e.target_id == order["id"]
    ][0]
    rel_event = [
        e for e in creates if e.target_kind == versioning.KIND_RELATIONSHIP
    ][0]

    # 实体：语义键 = 实体名，且与 target_id 指向的行一致
    assert entity_event.target_key == "订单"
    assert entity_event.target_id == order["id"]

    # 关系：`{from}->{kind}:{to}`（存储方向），两端名字都对得上
    assert rel_event.target_key == "订单->n:1:客户"
    stored = db_session.get(EntityRelationship, rel_event.target_id)
    names = versioning._entity_names(
        db_session,
        customer_id=stored.customer_id,
        ids=(stored.from_entity_id, stored.to_entity_id),
    )
    assert rel_event.target_key == (
        f"{names[stored.from_entity_id]}->{stored.kind}:{names[stored.to_entity_id]}"
    )


def test_a_rename_keeps_old_keys_and_writes_the_new_one(client, db_session) -> None:
    """改名不改历史：旧事件留着旧名字，新事件写新名字（两条都在，各说各的当时）。"""
    order, _customer, _rel = seed_graph(client, db_session)
    response = client.put(
        f"/api/v1/entities/{order['id']}",
        json={
            "name": "订单主表",
            "dataset_id": order["dataset_id"],
            "pk_columns": order["pk_columns"],
        },
        headers=CUST,
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()

    entity_events = [
        e for e in events(db_session)
        if e.target_kind == versioning.KIND_ENTITY and e.target_id == order["id"]
    ]
    assert [e.op for e in entity_events] == [versioning.OP_CREATE, versioning.OP_UPDATE]
    assert entity_events[0].target_key == "订单"
    assert entity_events[1].target_key == "订单主表"


def test_cascade_deleted_relationship_events_still_get_a_key(client, db_session) -> None:
    """删实体的级联删关系：关系事件也必须拿到语义键（实体行还没删时就写好）。"""
    order, _customer, _rel = seed_graph(client, db_session)
    response = client.delete(f"/api/v1/entities/{order['id']}", headers=CUST)
    assert response.status_code == 200, response.text
    db_session.expire_all()

    deletes = [e for e in events(db_session) if e.op == versioning.OP_DELETE]
    assert deletes, "级联删必须留事件"
    rel_delete = [e for e in deletes if e.target_kind == versioning.KIND_RELATIONSHIP][0]
    assert rel_delete.target_key == "订单->n:1:客户"


# ---------------------------------------------------------------- 1.3 迁移


def _alembic_config() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


def _assert_migration_shape(db_url: str) -> None:
    """两表 + 索引 + 唯一约束的形状断言（双言语共用）。"""
    inspector = inspect(create_engine(db_url))
    tables = set(inspector.get_table_names())
    assert {"ontology_events", "ontology_snapshots"} <= tables

    event_columns = {c["name"] for c in inspector.get_columns("ontology_events")}
    assert event_columns == {
        "id",
        "customer_id",
        "seq",
        "op",
        "target_kind",
        "target_id",
        # 0054（establish-ontology-kernel 6.3）：事件信封的语义键
        "target_key",
        "before_json",
        "after_json",
        "actor",
        "via",
        "source_ref",
        "source_instance_id",
        "created_at",
    }
    snapshot_columns = {c["name"] for c in inspector.get_columns("ontology_snapshots")}
    assert snapshot_columns == {
        "id",
        "customer_id",
        "version",
        "digest",
        "manifest_json",
        "trigger",
        "label",
        "created_by",
        "created_at",
    }
    event_indexes = {ix["name"] for ix in inspector.get_indexes("ontology_events")}
    assert "ix_ontology_events_customer_seq" in event_indexes
    snapshot_indexes = {
        ix["name"] for ix in inspector.get_indexes("ontology_snapshots")
    }
    assert "ix_ontology_snapshots_customer_created" in snapshot_indexes
    unique_names = {
        uq["name"] for uq in inspector.get_unique_constraints("ontology_snapshots")
    }
    assert "uq_ontology_snapshots_customer_version" in unique_names


def test_migration_0049_upgrade_downgrade_sqlite(tmp_path, monkeypatch) -> None:
    """迁移可升可降：两表 + 索引在位，downgrade 回 0048 后清零。"""
    db_url = f"sqlite:///{tmp_path}/onto_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(_alembic_config(), "head")
    _assert_migration_shape(db_url)

    command.downgrade(_alembic_config(), "0048_dag_executor_schema")
    inspector = inspect(create_engine(db_url))
    tables = set(inspector.get_table_names())
    assert "ontology_events" not in tables
    assert "ontology_snapshots" not in tables


@pytest.mark.skipif(
    not os.getenv("ONTOLOGY_TEST_PG_URL"),
    reason="PG 腿需显式给 ONTOLOGY_TEST_PG_URL（一次性真库；缺省跳过）",
)
def test_migration_0049_upgrade_downgrade_postgres(monkeypatch) -> None:
    """PG 腿（双方言自测）：同一迁移在真实 PG 上可升可降、索引/唯一约束生效。

    跑法（本地一次性集群）：
    ``ONTOLOGY_TEST_PG_URL=postgresql+psycopg://u@host:port/db pytest -k postgres``
    ——库需为**空库**（迁移从 base 全链跑到 head）。
    """
    db_url = os.environ["ONTOLOGY_TEST_PG_URL"]
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(_alembic_config(), "head")
    _assert_migration_shape(db_url)
    command.downgrade(_alembic_config(), "0048_dag_executor_schema")
    inspector = inspect(create_engine(db_url))
    tables = set(inspector.get_table_names())
    assert "ontology_events" not in tables
    assert "ontology_snapshots" not in tables
    command.upgrade(_alembic_config(), "head")
    _assert_migration_shape(db_url)


# ---------------------------------------------------------------- 2.2 事件流


def test_console_writes_leave_events(client, db_session) -> None:
    """建/改/删实体与关系各留一条事件（before/after 全量）。"""
    order, customer, rel = seed_graph(client, db_session)

    created = [e for e in events(db_session)]
    assert [e.op for e in created] == ["create", "create", "create"]
    assert [e.target_kind for e in created] == [
        "entity",
        "entity",
        "relationship",
    ]
    assert created[0].target_id == order["id"]
    assert created[0].before_json is None
    assert created[0].after_json["name"] == "订单"
    assert created[0].after_json["pk_columns"] == ["order_id"]
    assert created[0].via == "console"
    assert created[0].actor == SUB_CUST
    assert created[0].seq == 1
    assert [e.seq for e in created] == [1, 2, 3]

    # 改实体（显示列）→ op=update，before/after 字段级可辨
    response = client.put(
        f"/api/v1/entities/{order['id']}",
        json={
            "name": "订单",
            "dataset_id": order["dataset_id"],
            "pk_columns": ["order_id"],
            "display_column": "order_id",
            "description": "下单记录",
        },
        headers=CUST,
    )
    assert response.status_code == 200, response.text
    update_event = events(db_session)[-1]
    assert update_event.op == "update"
    assert update_event.before_json["display_column"] is None
    assert update_event.after_json["display_column"] == "order_id"
    assert update_event.before_json["description"] is None
    assert update_event.after_json["description"] == "下单记录"

    # 空转 PUT（同值）不落事件
    again = client.put(
        f"/api/v1/entities/{order['id']}",
        json={
            "name": "订单",
            "dataset_id": order["dataset_id"],
            "pk_columns": ["order_id"],
            "display_column": "order_id",
            "description": "下单记录",
        },
        headers=CUST,
    )
    assert again.status_code == 200, again.text
    assert len(events(db_session)) == 4

    # 删关系 → op=delete、after 为 null、before 全量
    deleted_rel = client.delete(
        f"/api/v1/entities/{order['id']}/relationships/{rel['id']}", headers=CUST
    )
    assert deleted_rel.status_code == 200, deleted_rel.text
    rel_delete = events(db_session)[-1]
    assert rel_delete.op == "delete"
    assert rel_delete.target_kind == "relationship"
    assert rel_delete.target_id == rel["id"]
    assert rel_delete.after_json is None
    assert rel_delete.before_json["from_column"] == "customer_id"
    assert rel_delete.before_json["kind"] == "n:1"


def test_delete_entity_leaves_cascade_relationship_events(client, db_session) -> None:
    """删实体级联删掉的关系逐条留 delete 事件（折叠回放才不失真）。"""
    order, customer, rel = seed_graph(client, db_session)
    response = client.delete(f"/api/v1/entities/{order['id']}", headers=CUST)
    assert response.status_code == 200, response.text
    assert response.json()["deleted_relationships"] == 1

    tail = events(db_session)[-2:]
    assert [(e.target_kind, e.op) for e in tail] == [
        ("relationship", "delete"),
        ("entity", "delete"),
    ]
    assert tail[0].target_id == rel["id"]
    assert tail[1].target_id == order["id"]
    assert tail[1].before_json["name"] == "订单"


def test_event_write_failure_fails_whole_transaction(db_session, monkeypatch) -> None:
    """2.4：事件插入失败 → 本体行也不落（无「本体变了没事件」窗口）。"""
    seed_dataset(db_session)
    dataset = db_session.query(Dataset).first()

    def boom(*args, **kwargs):
        raise RuntimeError("events table unavailable")

    monkeypatch.setattr(versioning, "record_event", boom)
    with pytest.raises(RuntimeError):
        entities_service.create_entity(
            db_session,
            db_session,
            customer_id="cust-onto",
            name="订单",
            dataset_id=dataset.id,
            pk_columns=["order_id"],
        )
    db_session.rollback()
    assert db_session.query(Entity).count() == 0
    assert db_session.query(OntologyEvent).count() == 0


def test_no_event_mutation_endpoints() -> None:
    """2.5：本体版本面没有任何改/删事件端点（只读 + 追加）。"""
    from fd_wire_api.app import app as real_app

    for route in real_app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1/ontology"):
            continue
        methods = set(getattr(route, "methods", set()) or set())
        assert methods <= {"GET", "POST"}, (path, methods)
        assert "events" not in path, path


# ---------------------------------------------------------------- 2.3 闸血缘


def test_action_gate_events_carry_lineage(client, db_session) -> None:
    """经闸声明 → via=action_gate，source_ref/source_instance_id 与实例一致。"""
    from fd_wire_api.domain_actions import builtins as da_builtins

    dataset_id = seed_dataset(db_session)
    db_session.commit()

    class _Sessions:
        def __init__(self, session):
            self.ai = session
            self.entities = session
            self.datasets = session

    class _Instance:
        def __init__(self, session):
            self.id = 77
            self.source_ref = "fleet=/post=/title=订单域"
            self.args_json = {
                "values": {
                    "business_name": "订单",
                    "dataset_key": "orders",
                    "pk_columns": "order_id",
                }
            }

    bt = da_builtins.get_builtin(-1, "cust-onto")
    da_builtins.execute_metadata_effect(
        _Sessions(db_session),
        bt=bt,
        instance=_Instance(db_session),
        customer_id="cust-onto",
    )
    created = events(db_session)
    assert len(created) == 1
    event = created[0]
    assert event.op == "create"
    assert event.via == "action_gate"
    assert event.source_ref == "fleet=/post=/title=订单域"
    assert event.source_instance_id == 77
    assert event.actor == "domain-action:-1:77"
    assert event.after_json["dataset_id"] == dataset_id


# ---------------------------------------------------------------- 3.1 快照


def test_snapshot_version_monotonic_and_idempotent(client, db_session) -> None:
    """版本号单调 +1；同 digest 不重复落（响应如实指认与 vN 一致）。"""
    seed_graph(client, db_session)
    first = client.post("/api/v1/ontology/versions", json={}, headers=CUST)
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1
    assert first.json()["trigger"] == "manual"
    assert first.json()["unchanged"] is False
    assert len(first.json()["digest"]) == 64

    repeat = client.post("/api/v1/ontology/versions", json={}, headers=CUST)
    assert repeat.status_code == 201, repeat.text
    assert repeat.json()["unchanged"] is True
    assert repeat.json()["version"] == 1
    assert "v1" in repeat.json()["message"]
    assert db_session.query(OntologySnapshot).count() == 1

    # 改一条 → 新 digest → v2
    order = db_session.query(Entity).filter(Entity.name == "订单").one()
    response = client.put(
        f"/api/v1/entities/{order.id}",
        json={
            "name": "订单",
            "dataset_id": order.dataset_id,
            "pk_columns": ["order_id"],
            "display_column": "order_id",
        },
        headers=CUST,
    )
    assert response.status_code == 200, response.text
    second = client.post(
        "/api/v1/ontology/versions", json={"label": "冒烟本体定稿"}, headers=CUST
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2
    assert second.json()["label"] == "冒烟本体定稿"
    assert second.json()["created_by"] == SUB_CUST
    assert second.json()["digest"] != first.json()["digest"]


def test_manual_label_length_limit(client, db_session) -> None:
    seed_graph(client, db_session)
    too_long = client.post(
        "/api/v1/ontology/versions",
        json={"label": "甲" * 81},
        headers=CUST,
    )
    assert too_long.status_code == 422
    assert "80" in too_long.json()["detail"]


def test_empty_ontology_has_no_snapshot(client, db_session) -> None:
    """无本体 → 不落快照（手动 422、日任务跳过）。"""
    seed_dataset(db_session)  # 有数据集但没有任何实体
    response = client.post("/api/v1/ontology/versions", json={}, headers=CUST)
    assert response.status_code == 422
    assert "本体为空" in response.json()["detail"]
    assert db_session.query(OntologySnapshot).count() == 0

    outcomes = snapshot_service.tick_once(
        now=datetime(2026, 11, 1, 3, 30),
        session_factory=lambda: db_session,
    )
    assert outcomes == []
    assert db_session.query(OntologySnapshot).count() == 0


def test_daily_tick_skips_without_changes_and_dedupes_day(client, db_session) -> None:
    """3.2：零变更跳过；当日已有日版跳过；有事件才出新版。"""
    seed_graph(client, db_session)
    day1 = datetime(2026, 11, 1, 3, 30)
    # 钉住事件时间到模拟时间线（假时钟下 created_at 是「快照之后」的唯一判据）
    _rewrite_event_times(db_session, day1 - timedelta(hours=6))
    first = snapshot_service.tick_once(now=day1, session_factory=lambda: db_session)
    assert first[0]["status"] == "created"
    assert first[0]["version"] == 1

    # 同一天再跑 → 当日已有日版
    same_day = snapshot_service.tick_once(
        now=day1 + timedelta(hours=5), session_factory=lambda: db_session
    )
    assert same_day[0]["status"] == "skipped"
    assert same_day[0]["reason"] == "already_daily_today"

    # 次日、但零变更 → 跳过
    day2 = day1 + timedelta(days=1)
    no_changes = snapshot_service.tick_once(
        now=day2, session_factory=lambda: db_session
    )
    assert no_changes[0]["status"] == "skipped"
    assert no_changes[0]["reason"] == "no_changes"
    assert db_session.query(OntologySnapshot).count() == 1

    # 有变更 → 次日出一版
    order = db_session.query(Entity).filter(Entity.name == "订单").one()
    changed = client.put(
        f"/api/v1/entities/{order.id}",
        json={
            "name": "订单",
            "dataset_id": order.dataset_id,
            "pk_columns": ["order_id"],
            "description": "改了点东西",
        },
        headers=CUST,
    )
    assert changed.status_code == 200, changed.text
    _rewrite_event_times(db_session, day2 - timedelta(minutes=30))
    day2_retry = snapshot_service.tick_once(
        now=day2 + timedelta(minutes=1), session_factory=lambda: db_session
    )
    assert day2_retry[0]["status"] == "created"
    assert day2_retry[0]["version"] == 2


def test_first_snapshot_is_baseline_without_events(client, db_session) -> None:
    """3.2/design D6：存量本体零事件也出基线 v1（不虚构历史）。"""
    seed_graph(client, db_session)
    # 模拟「机制启用即上已有本体」：清掉事件，只剩存量行
    for event in db_session.query(OntologyEvent).all():
        db_session.delete(event)
    db_session.commit()
    assert db_session.query(OntologyEvent).count() == 0

    outcomes = snapshot_service.tick_once(
        now=datetime(2026, 11, 1, 3, 30), session_factory=lambda: db_session
    )
    assert outcomes[0]["status"] == "created"
    assert outcomes[0]["version"] == 1
    snapshot = db_session.query(OntologySnapshot).one()
    assert len(snapshot.manifest_json["entities"]) == 2
    assert len(snapshot.manifest_json["relationships"]) == 1


def test_snapshot_does_not_mutate_current_state(client, db_session) -> None:
    """3.5：快照是纯读后落档——两表行逐字节不变。"""
    seed_graph(client, db_session)
    before_entities = [
        versioning.entity_payload(row) for row in db_session.query(Entity).all()
    ]
    before_rels = [
        versioning.relationship_payload(row)
        for row in db_session.query(EntityRelationship).all()
    ]
    before_manifest = versioning.build_manifest(
        db_session, customer_id="cust-onto"
    )
    response = client.post("/api/v1/ontology/versions", json={}, headers=CUST)
    assert response.status_code == 201, response.text
    db_session.expire_all()
    assert [
        versioning.entity_payload(row) for row in db_session.query(Entity).all()
    ] == before_entities
    assert [
        versioning.relationship_payload(row)
        for row in db_session.query(EntityRelationship).all()
    ] == before_rels
    assert (
        versioning.build_manifest(db_session, customer_id="cust-onto")
        == before_manifest
    )


def test_daily_retention_keeps_manual(client, db_session) -> None:
    """3.4：日版只留最近 N 份；手动版永久。"""
    seed_graph(client, db_session)
    manual = client.post(
        "/api/v1/ontology/versions", json={"label": "发布版"}, headers=CUST
    )
    assert manual.status_code == 201, manual.text
    order = db_session.query(Entity).filter(Entity.name == "订单").one()

    # 造 35 份日版（每次先改一条让 digest 变化，再直插日版）
    base = datetime(2026, 9, 1, 3, 30)
    for index in range(35):
        response = client.put(
            f"/api/v1/entities/{order.id}",
            json={
                "name": "订单",
                "dataset_id": order.dataset_id,
                "pk_columns": ["order_id"],
                "description": f"第 {index} 次",
            },
            headers=CUST,
        )
        assert response.status_code == 200, response.text
        snapshot, state = snapshot_service.create_snapshot(
            db_session,
            customer_id="cust-onto",
            trigger=versioning.TRIGGER_DAILY,
            now=base + timedelta(days=index),
        )
        assert state == "created", state
    db_session.commit()
    assert (
        db_session.query(OntologySnapshot)
        .filter(OntologySnapshot.trigger == "daily")
        .count()
        == 35
    )

    removed = snapshot_service.prune_daily_snapshots(
        db_session, customer_id="cust-onto", keep=30
    )
    db_session.commit()
    assert len(removed) == 5
    assert (
        db_session.query(OntologySnapshot)
        .filter(OntologySnapshot.trigger == "daily")
        .count()
        == 30
    )
    assert (
        db_session.query(OntologySnapshot)
        .filter(OntologySnapshot.trigger == "manual")
        .count()
        == 1
    )


# ---------------------------------------------------------------- 2.1 digest


def test_digest_is_canonical_and_stable(client, db_session) -> None:
    """键序/条序/类型归一后同内容同 digest（不同输入顺序亦同）。"""
    seed_graph(client, db_session)
    manifest = versioning.build_manifest(db_session, customer_id="cust-onto")
    digest = versioning.manifest_digest(manifest)

    # 条序反转 + 键序无关 + 数值形态归一 → 同 digest
    shuffled = {
        "relationships": list(manifest["relationships"]),
        "entities": list(reversed(manifest["entities"])),
    }
    assert versioning.manifest_digest(shuffled) == digest
    # 类型归一：pk_columns 元组/字符串、column_notes 缺省 → 同 digest
    coerced = versioning.canonical_manifest(
        (
            {
                "id": item["id"],
                "name": item["name"],
                "dataset_id": item["dataset_id"],
                "pk_columns": tuple(item["pk_columns"]),
                "description": item["description"],
                "display_column": item["display_column"],
            }
            for item in manifest["entities"]
        ),
        manifest["relationships"],
    )
    assert versioning.manifest_digest(coerced) == digest
    # 空 manifest digest 稳定且 ≠ 非空
    assert versioning.manifest_digest({}) == versioning.manifest_digest(
        versioning.empty_manifest()
    )
    assert versioning.manifest_digest({}) != digest


def test_digest_matches_the_golden_values_from_before_the_kernel_swap() -> None:
    """尽力保住已发布的每一个 digest（establish-ontology-kernel 6.2）。

    下面六个数是**换底之前**由本模块原实现算出来的（2026-11-12 捕获）。换底把
    ``canonical_json``/``manifest_digest``/``normalize_*`` 交给内核后，这些值
    必须逐字节不变——digest 进了 ``ontology_snapshots`` 与导出物，改一个字节
    就等于作废所有已发布版本，且历史快照的 ``digest`` 与重算值会对不上。
    """
    from datetime import date, datetime

    cases = {
        "empty": ({}, "e1bb444bbbd2ebe89a032be6b851a7b5125285ed146915fb65568a10ab2a89a5"),
        "entity_full": (
            {"entities": [{
                "id": 7, "name": "orders", "dataset_id": "ds-1", "pk_columns": ["order_id"],
                "description": "订单", "display_column": "order_id",
                "column_notes": {"order_id": "主键", "amount": "元"},
                "tier": "gold", "sensitivity": "internal",
            }], "relationships": []},
            "18ee236c173d3830af1437bd65559be8958303015247cdf294a615b13b3d0f5e",
        ),
        "entity_minimal_pre_0050": (
            {"entities": [
                {"id": 3, "name": "customers", "dataset_id": "ds-2", "pk_columns": [],
                 "description": None, "display_column": None, "column_notes": None}
            ], "relationships": []},
            "61fcd15edb85f70d330ed4489a0c493aa41fdf540b36732938c1bb77fb55718f",
        ),
        "relationship_full": (
            {"entities": [{"id": 1, "name": "orders"}, {"id": 2, "name": "customers"}],
             "relationships": [{
                 "id": 11, "from_entity_id": 1, "from_column": "customer_id",
                 "to_entity_id": 2, "to_column": "id", "kind": "many_to_one",
                 "declared_kind": "belongs_to", "label": "所属客户",
                 "junction_dataset_id": None, "junction_source_column": None,
                 "junction_target_column": None, "tier": None, "sensitivity": None,
             }]},
            "5a4a3b5777842a6ef6a223dd4066a73e9cea95518faf060fc5d80987d72ba765",
        ),
        "type_coercion": (
            {"entities": [
                {"id": "5", "name": "t", "dataset_id": "d", "pk_columns": "a",
                 "column_notes": ["x"], "tier": None, "sensitivity": None}
            ], "relationships": []},
            "22b5d189483857d9aff97d88f703c604aff1eb767c9aabdd43848ea4e6e99da4",
        ),
        "datetime_default": (
            {"entities": [
                {"id": 9, "name": "t", "dataset_id": "d", "pk_columns": [],
                 "extra_day": date(2026, 11, 12),
                 "extra_ts": datetime(2026, 11, 12, 1, 2, 3, 4000)}
            ], "relationships": []},
            "b85c7e06fcd96e51e43cb37a504b3f47f86c5948871a265f403aa007c9193bd3",
        ),
    }
    for name, (manifest, want) in cases.items():
        assert versioning.manifest_digest(manifest) == want, name


def test_canonical_json_is_byte_identical_after_the_kernel_swap() -> None:
    """规范 JSON 文本本身也不许变（导出物与签名面按它对齐）。"""
    text = versioning.canonical_json({"b": 1, "a": "中文", "c": [2, {"z": None}]})
    assert text == '{"a":"中文","b":1,"c":[2,{"z":null}]}'


# ---------------------------------------------------------------- 4.1 diff


def test_diff_reports_three_groups(client, db_session) -> None:
    """4.1：+1 实体、-1 关系、字段级改各归其组。"""
    order, customer, rel = seed_graph(client, db_session)
    base = versioning.build_manifest(db_session, customer_id="cust-onto")

    # 改字段
    response = client.put(
        f"/api/v1/entities/{order['id']}",
        json={
            "name": "订单",
            "dataset_id": order["dataset_id"],
            "pk_columns": ["order_id"],
            "display_column": "order_id",
        },
        headers=CUST,
    )
    assert response.status_code == 200, response.text
    # 删关系
    assert (
        client.delete(
            f"/api/v1/entities/{order['id']}/relationships/{rel['id']}",
            headers=CUST,
        ).status_code
        == 200
    )
    # 加实体（新数据集）
    new_dataset = seed_dataset(db_session, key="staff", columns=[{"name": "emp_id", "type": "TEXT"}])
    added = post_entity(client, dataset_id=new_dataset, name="员工", pk=["emp_id"])
    assert added.status_code == 201, added.text

    target = versioning.build_manifest(db_session, customer_id="cust-onto")
    diff = versioning.diff_manifests(base, target)
    assert diff["has_changes"] is True
    assert diff["counts"]["entities"] == {
        "added": 1,
        "removed": 0,
        "changed": 1,
        "total": 2,
    }
    assert diff["counts"]["relationships"] == {
        "added": 0,
        "removed": 1,
        "changed": 0,
        "total": 1,
    }
    changed = diff["entities"]["changed"][0]
    assert changed["id"] == order["id"]
    assert changed["fields"]["display_column"] == {"before": None, "after": "order_id"}
    assert diff["relationships"]["removed"][0]["id"] == rel["id"]


# ---------------------------------------------------------------- 4.2 端点


def test_diff_carries_provenance_when_gate_written(client, db_session) -> None:
    """4.1/spec：改动条目可携带出处——经闸建的实体在 diff 里带 source_ref。"""
    from fd_wire_api.domain_actions import builtins as da_builtins

    class _Sessions:
        def __init__(self, session):
            self.ai = session
            self.entities = session
            self.datasets = session

    class _Instance:
        id = 55
        source_ref = "fleet=/post=/title=本体版"

        def __init__(self, values):
            self.args_json = {"values": values}

    seed_dataset(db_session)
    seed_dataset(db_session, key="customers", columns=CUSTOMER_COLUMNS)
    db_session.commit()
    da_builtins.execute_metadata_effect(
        _Sessions(db_session),
        bt=da_builtins.get_builtin(-1, "cust-onto"),
        instance=_Instance(
            {
                "business_name": "订单",
                "dataset_key": "orders",
                "pk_columns": "order_id",
            }
        ),
        customer_id="cust-onto",
    )
    assert client.post("/api/v1/ontology/versions", json={}, headers=CUST).status_code == 201
    da_builtins.execute_metadata_effect(
        _Sessions(db_session),
        bt=da_builtins.get_builtin(-1, "cust-onto"),
        instance=_Instance(
            {
                "business_name": "客户",
                "dataset_key": "customers",
                "pk_columns": "customer_id",
            }
        ),
        customer_id="cust-onto",
    )

    diff = client.get("/api/v1/ontology/versions/1/diff?against=current", headers=CUST)
    assert diff.status_code == 200, diff.text
    added = diff.json()["entities"]["added"]
    assert len(added) == 1
    assert added[0]["name"] == "客户"
    assert added[0]["source_ref"] == "fleet=/post=/title=本体版"


def test_versions_list_and_unpublished_count(client, db_session) -> None:
    seed_graph(client, db_session)
    assert client.post("/api/v1/ontology/versions", json={}, headers=CUST).status_code == 201
    order = db_session.query(Entity).filter(Entity.name == "订单").one()
    for index in range(2):
        response = client.put(
            f"/api/v1/entities/{order.id}",
            json={
                "name": "订单",
                "dataset_id": order.dataset_id,
                "pk_columns": ["order_id"],
                "description": f"v{index}",
            },
            headers=CUST,
        )
        assert response.status_code == 200, response.text

    listing = client.get("/api/v1/ontology/versions", headers=CUST)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["unpublished"] == {"count": 2, "since_version": 1}
    assert len(body["versions"]) == 1
    assert body["versions"][0]["entity_count"] == 2
    assert body["versions"][0]["relationship_count"] == 1
    assert body["versions"][0]["trigger"] == "manual"
    # 时间线条目也带 manifest 形状版本（老消费者按未知键忽略）
    assert body["versions"][0]["manifest_schema_version"] == 1

    # 空本体客户：200 + 空时间线（不是 404）
    empty = client.get("/api/v1/ontology/versions", headers=OTHER)
    assert empty.status_code == 200
    assert empty.json()["versions"] == []
    assert empty.json()["unpublished"]["count"] == 0


def test_diff_endpoint_and_cross_tenant_404(client, db_session) -> None:
    seed_graph(client, db_session)
    assert client.post("/api/v1/ontology/versions", json={}, headers=CUST).status_code == 201
    order = db_session.query(Entity).filter(Entity.name == "订单").one()
    assert (
        client.put(
            f"/api/v1/entities/{order.id}",
            json={
                "name": "订单",
                "dataset_id": order.dataset_id,
                "pk_columns": ["order_id"],
                "display_column": "order_id",
            },
            headers=CUST,
        ).status_code
        == 200
    )

    diff = client.get(
        "/api/v1/ontology/versions/1/diff?against=current", headers=CUST
    )
    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert body["base"] == {"kind": "version", "version": 1, "digest": body["base"]["digest"]}
    assert body["target"]["kind"] == "current"
    assert body["counts"]["entities"]["changed"] == 1

    # 跨企业：他企看不到 v1
    assert (
        client.get("/api/v1/ontology/versions/1/diff", headers=OTHER).status_code == 404
    )
    assert client.get("/api/v1/ontology/versions/99/diff", headers=CUST).status_code == 404
    assert (
        client.get("/api/v1/ontology/versions/1/diff?against=abc", headers=CUST).status_code
        == 422
    )


def test_export_json_yaml_and_provenance(client, db_session) -> None:
    seed_graph(client, db_session)
    published = client.post(
        "/api/v1/ontology/versions", json={"label": "定稿"}, headers=CUST
    )
    assert published.status_code == 201, published.text
    version = published.json()["version"]

    current = client.get("/api/v1/ontology/export", headers=CUST)
    assert current.status_code == 200, current.text
    document = json.loads(current.text)
    assert document["schema_version"] == 2  # 0050：manifest 增档位两键
    # establish-ontology-kernel 6.5：manifest 形状版本（与 schema_version 不同轴）
    assert document["manifest_schema_version"] == 1
    assert document["customer_id"] == "cust-onto"
    assert document["version"] == "current"
    assert document["digest"] == versioning.manifest_digest(
        {
            "entities": document["entities"],
            "relationships": document["relationships"],
        }
    )
    assert len(document["entities"]) == 2
    assert len(document["relationships"]) == 1
    # 默认无出处
    assert "source_ref" not in document["entities"][0]

    as_yaml = client.get("/api/v1/ontology/export?format=yaml", headers=CUST)
    assert as_yaml.status_code == 200, as_yaml.text
    assert as_yaml.headers["content-type"].startswith("application/yaml")
    yaml_document = yaml.safe_load(as_yaml.text)
    assert yaml_document["entities"] == document["entities"]
    assert yaml_document["digest"] == document["digest"]

    historical = client.get(
        f"/api/v1/ontology/export?version={version}&format=json", headers=CUST
    )
    historical_document = json.loads(historical.text)
    assert historical_document["version"] == version
    assert historical_document["digest"] == published.json()["digest"]

    # 跨企业 404 / 空本体 200 空数组
    assert (
        client.get(f"/api/v1/ontology/export?version={version}", headers=OTHER).status_code
        == 404
    )
    empty = client.get("/api/v1/ontology/export", headers=OTHER)
    assert empty.status_code == 200
    assert json.loads(empty.text)["entities"] == []


def test_export_provenance_only_for_gate_writes(client, db_session) -> None:
    """4.2：provenance=true 时只给有经闸血缘的条目加 source_ref。"""
    from fd_wire_api.domain_actions import builtins as da_builtins

    seed_dataset(db_session)
    db_session.commit()

    class _Sessions:
        def __init__(self, session):
            self.ai = session
            self.entities = session
            self.datasets = session

    class _Instance:
        id = 91
        source_ref = "fleet=/post=/title=供应链"

        def __init__(self):
            self.args_json = {
                "values": {
                    "business_name": "订单",
                    "dataset_key": "orders",
                    "pk_columns": "order_id",
                }
            }

    da_builtins.execute_metadata_effect(
        _Sessions(db_session),
        bt=da_builtins.get_builtin(-1, "cust-onto"),
        instance=_Instance(),
        customer_id="cust-onto",
    )
    # 再建一条控制台实体（无血缘）
    second_dataset = seed_dataset(db_session, key="customers", columns=CUSTOMER_COLUMNS)
    assert (
        post_entity(
            client, dataset_id=second_dataset, name="客户", pk=["customer_id"]
        ).status_code
        == 201
    )

    document = json.loads(
        client.get("/api/v1/ontology/export?provenance=true", headers=CUST).text
    )
    by_name = {item["name"]: item for item in document["entities"]}
    assert by_name["订单"]["source_ref"] == "fleet=/post=/title=供应链"
    assert "source_ref" not in by_name["客户"]  # 无经闸血缘 → 键缺席


# ---------------------------------------------------------------- 4.3 回滚


def test_rollback_plan_preview_and_execute(client, db_session) -> None:
    """4.3：plan 预览 + 执行；执行后当前态与 v1 digest 一致。"""
    order, customer, _rel = seed_graph(client, db_session)
    published = client.post("/api/v1/ontology/versions", json={}, headers=CUST)
    assert published.status_code == 201, published.text
    v1 = published.json()["version"]
    v1_digest = published.json()["digest"]

    # 改一条字段（回滚方向 = update）
    changed = client.put(
        f"/api/v1/entities/{order['id']}",
        json={
            "name": "订单",
            "dataset_id": order["dataset_id"],
            "pk_columns": ["order_id"],
            "display_column": "order_id",
            "description": "改坏了",
        },
        headers=CUST,
    )
    assert changed.status_code == 200, changed.text

    plan = client.post(
        f"/api/v1/ontology/versions/{v1}/rollback/plan", headers=CUST
    )
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert body["empty"] is False
    assert body["plan"]["summary"]["entities"]["update"] == 1
    assert body["plan"]["update_entities"][0]["fields"]["description"] == {
        "before": "改坏了",
        "after": None,
    }

    executed = client.post(f"/api/v1/ontology/versions/{v1}/rollback", headers=CUST)
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["digest_match"] is True
    assert result["current_digest"] == v1_digest
    assert result["identity_changed"] == []
    # 回滚事件落库且 via=rollback
    rollback_events = [e for e in events(db_session) if e.via == "rollback"]
    assert len(rollback_events) == 1
    assert rollback_events[0].op == "update"
    assert rollback_events[0].actor == SUB_CUST

    # 再打版 → 内容与 v1 一致：同 digest 不重复落（响应如实指认）
    republished = client.post("/api/v1/ontology/versions", json={}, headers=CUST)
    assert republished.status_code == 201, republished.text
    assert republished.json()["unchanged"] is True
    assert republished.json()["version"] == v1
    assert republished.json()["digest"] == v1_digest
    assert db_session.query(OntologySnapshot).count() == 1


def test_rollback_restores_deleted_entity_identity(client, db_session) -> None:
    """4.3：删掉实体再回滚 → 沿 manifest id 重建，digest 对齐。"""
    order, customer, rel = seed_graph(client, db_session)
    published = client.post("/api/v1/ontology/versions", json={}, headers=CUST)
    assert published.status_code == 201, published.text
    v1_digest = published.json()["digest"]

    deleted = client.delete(f"/api/v1/entities/{customer['id']}", headers=CUST)
    assert deleted.status_code == 200, deleted.text
    plan = client.post(
        "/api/v1/ontology/versions/1/rollback/plan", headers=CUST
    ).json()
    assert plan["plan"]["summary"]["entities"]["create"] == 1
    assert plan["plan"]["summary"]["relationships"]["create"] == 1

    executed = client.post("/api/v1/ontology/versions/1/rollback", headers=CUST)
    assert executed.status_code == 200, executed.text
    assert executed.json()["digest_match"] is True
    restored = db_session.get(Entity, customer["id"])
    assert restored is not None
    assert restored.name == "客户"
    assert db_session.get(EntityRelationship, rel["id"]) is not None


def test_rollback_is_reversible(client, db_session) -> None:
    """4.4：回滚本身可被再回滚（append-only 下方向可逆）。"""
    order, customer, rel = seed_graph(client, db_session)
    v1 = client.post("/api/v1/ontology/versions", json={}, headers=CUST).json()
    changed = client.put(
        f"/api/v1/entities/{order['id']}",
        json={
            "name": "订单",
            "dataset_id": order["dataset_id"],
            "pk_columns": ["order_id"],
            "description": "第二版",
        },
        headers=CUST,
    )
    assert changed.status_code == 200, changed.text
    v2 = client.post("/api/v1/ontology/versions", json={}, headers=CUST).json()

    back_to_v1 = client.post(
        f"/api/v1/ontology/versions/{v1['version']}/rollback", headers=CUST
    )
    assert back_to_v1.status_code == 200, back_to_v1.text
    assert back_to_v1.json()["digest_match"] is True

    back_to_v2 = client.post(
        f"/api/v1/ontology/versions/{v2['version']}/rollback", headers=CUST
    )
    assert back_to_v2.status_code == 200, back_to_v2.text
    assert back_to_v2.json()["digest_match"] is True
    assert back_to_v2.json()["current_digest"] == v2["digest"]
    # 历史只增不减：每次回滚都留了事件
    assert len([e for e in events(db_session) if e.via == "rollback"]) == 2


def test_rollback_requires_owner(client, db_session) -> None:
    """4.3/design D8：回滚执行 owner-only（editor 403）。"""
    seed_graph(client, db_session)
    assert client.post("/api/v1/ontology/versions", json={}, headers=CUST).status_code == 201
    plan = client.post("/api/v1/ontology/versions/1/rollback/plan", headers=MEMBER)
    assert plan.status_code == 200, plan.text
    executed = client.post("/api/v1/ontology/versions/1/rollback", headers=MEMBER)
    assert executed.status_code == 403
    assert "owner" in executed.json()["detail"]


def test_rollback_conflict_reports_honestly(client, db_session) -> None:
    """4.3：回滚中途校验失败 → 422 带已执行条目（不假装成功）。"""
    order, customer, _rel = seed_graph(client, db_session)
    assert client.post("/api/v1/ontology/versions", json={}, headers=CUST).status_code == 201
    # 让 v1 里的实体在回滚目标里需要一个已不存在的数据集：删掉客户实体后
    # 把它的数据集换成别企的（模拟漂移），再从快照回滚
    assert client.delete(f"/api/v1/entities/{customer['id']}", headers=CUST).status_code == 200
    dataset_row = db_session.get(Dataset, customer["dataset_id"])
    dataset_row.customer_id = "cust-onto-other"
    db_session.commit()

    response = client.post("/api/v1/ontology/versions/1/rollback", headers=CUST)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["executed"] == []
    assert "回滚在第 1 步失败" in detail["message"]


# ---------------------------------------------------------------- 4.5 完整性


def test_integrity_consistent_after_normal_writes(client, db_session) -> None:
    seed_graph(client, db_session)
    assert client.post("/api/v1/ontology/versions", json={}, headers=CUST).status_code == 201
    report = client.get("/api/v1/ontology/integrity", headers=CUST)
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["consistent"] is True
    assert body["baseline"]["kind"] == "snapshot"
    assert body["baseline"]["version"] == 1
    assert body["differences"]["has_changes"] is False


def test_integrity_detects_escape_write(client, db_session) -> None:
    """4.5：裸 SQL 直改（绕过 service）→ 自检报告差异条目。"""
    seed_graph(client, db_session)
    assert client.post("/api/v1/ontology/versions", json={}, headers=CUST).status_code == 201
    # 演练：绕过 service 直接改库（运维事故形态）
    db_session.execute(
        text("UPDATE entities SET name = '订单（直改）' WHERE name = '订单'")
    )
    db_session.commit()
    report = client.get("/api/v1/ontology/integrity", headers=CUST).json()
    assert report["consistent"] is False
    changed = report["differences"]["entities"]["changed"]
    assert len(changed) == 1
    assert changed[0]["fields"]["name"] == {
        "before": "订单",
        "after": "订单（直改）",
    }
    assert "裸 SQL" in report["escape_hint"]
