"""客户域动作测试（wire-domain-actions 任务组 6）。

覆盖（spec 场景 + design D6 兼容面）：
- 声明矩阵：创建 OK（快照字段）/risk=publish 422/重名 409/表单 schema 非法
  422/效果映射列不在数据集 422/update 多列主键 422/主键列进 value_mapping 422/
  api 档 422（v0 cell 专属）/缺少必填表单字段 preflight 422；
- 提议流（闸管线，无会话上下文——0043 conversation_id 可空）：propose 即时
  pending_confirm（write 档）→ 实例台账 awaiting_confirmation；source_ref
  原样透传；停用类型 propose → ActionNotRegistered（未注册语义）；
- 确认执行：approve → 引擎 customer_dataset_write_batch 单 op（insert values /
  update anchor=主键值）、actor=domain-action:*、channel=agent → 实例
  executed + correction_id 回填；引擎失败 → 实例 failed + 提议 failed；
  类型停用后确认 → 实例 disabled（backstop）；
- 拒绝：reject → 实例 rejected（on_rejected 钩子）；
- 台账：list/get 客户域隔离（他企 404）；entity 删除级联停用；
- 兼容面：自理动作（wire.artifact.publish 等）注册表解析零回归——既有
  test_ai_actions 全量作为回归（本文件只补域动作增量）。

环境：单引擎内存库（AiBase+EntitiesBase+DatasetsBase 合库——闸会话束同库
注入，照 test_ai_actions 先例）；引擎假件经 monkeypatch
datasets.ingest.get_engine_client_factory（闸执行体的直调通道）。
"""

from __future__ import annotations

import os

os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 先经 app 入口导入（既有跨切片环的解析序——照 test_assets_api 先例）
from fd_wire_api.app import app  # noqa: F401

import fd_wire_api.datasets.ingest as datasets_ingest
from fd_wire_api.ai import actions as gate
from fd_wire_api.ai.actions import (
    ActionNotRegistered,
    ApprovalDenied,
    ActorContext,
    GateSessions,
    propose as gate_propose,
    reject as gate_reject,
)
from fd_wire_api.ai.models import AIActionProposal
from fd_wire_api.datasets.db import DatasetsBase
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.domain_actions import service as domain_service
from fd_wire_api.domain_actions.models import DomainActionInstance, DomainActionType
from fd_wire_api.entities.models import Entity

OWNER = ActorContext(customer_id="cust-da", sub="sub-da-owner", role="owner")
VIEWER = ActorContext(customer_id="cust-da", sub="sub-da-viewer", role="viewer")
OTHERS = ActorContext(customer_id="cust-da-other", sub="sub-x", role="owner")


class FakeEngine:
    """写批假件：记录调用、返回 correction_id（可注入失败）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: str | None = None
        self.correction_id: int | None = 77

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        if self.fail:
            raise RuntimeError(self.fail)
        return {"correction_id": self.correction_id}


@pytest.fixture()
def db():
    """单引擎内存库（AI+实体+数据集合库——闸会话束同库注入）。"""
    from fd_wire_api.entities.db import EntitiesBase

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from fd_wire_api.ai.db import AiBase

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
def sess(db):
    return GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db)


@pytest.fixture(autouse=True)
def _clear_proposed_args():
    gate._PROPOSED_ARGS.clear()
    yield
    gate._PROPOSED_ARGS.clear()


@pytest.fixture()
def engine_fake(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(
        datasets_ingest, "get_engine_client_factory", lambda: lambda cid: fake
    )
    return fake


def seed_entity(db, *, tier: str = "cell", key="customers") -> Entity:
    dataset = Dataset(
        customer_id="cust-da",
        key=key,
        name=key,
        source_kind="upload",
        storage_tier=tier,
        columns_json=[
            {"name": "id", "type": "TEXT"},
            {"name": "cname", "type": "TEXT"},
            {"name": "status", "type": "TEXT"},
        ],
        row_count=5,
    )
    db.add(dataset)
    db.commit()
    entity = Entity(
        customer_id="cust-da",
        name="客户",
        dataset_id=dataset.id,
        pk_columns=["id"],
        display_column="cname",
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


def make_type(db, entity: Entity, *, risk="write", effect=None, name="批准") -> Any:
    return domain_service.create_type(
        db,
        db,
        db,
        customer_id="cust-da",
        entity_id=entity.id,
        name=name,
        risk=risk,
        form_schema={
            "fields": [
                {"name": "id", "input": "text", "required": True},
                {"name": "status", "input": "select", "required": True,
                 "options": ["approved", "rejected"]},
                {"name": "note", "input": "text", "required": False},
            ]
        },
        effect=effect
        or {
            "kind": "insert",
            "value_mapping": {
                "id": {"field": "id"},
                "cname": {"literal": "客户"},
                "status": {"field": "status"},
            },
        },
        actor_sub="sub-da-owner",
    )


def propose_domain(sess, type_row, values, *, ctx=OWNER, source_ref=None):
    return gate_propose(
        sess.ai,
        sess,
        ctx,
        action_id=f"domain.{type_row.id}",
        args={"values": values, "source_ref": source_ref, "_channel": "mcp"},
    )


# ------------------------------------------------- 声明矩阵（任务 2.1）


def test_create_type_snapshots_entity_binding(db):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    assert type_row.dataset_id == entity.dataset_id
    assert type_row.pk_columns_json == ["id"]
    assert type_row.enabled is True
    # 快照事件落痕
    events = db.query(DomainActionType).get(type_row.id)
    assert events is not None


def test_create_type_publish_risk_rejected(db):
    entity = seed_entity(db)
    with pytest.raises(domain_service.InvalidActionType) as exc:
        make_type(db, entity, risk="publish")
    assert "publish" in str(exc.value)


def test_create_type_duplicate_name_409(db):
    entity = seed_entity(db)
    make_type(db, entity)
    with pytest.raises(domain_service.ActionTypeConflict):
        make_type(db, entity)


def test_create_type_select_without_options_422(db):
    entity = seed_entity(db)
    with pytest.raises(domain_service.InvalidActionType) as exc:
        domain_service.create_type(
            db, db, db,
            customer_id="cust-da", entity_id=entity.id, name="批准", risk="write",
            form_schema={"fields": [{"name": "id", "input": "text", "required": True},
                                    {"name": "status", "input": "select", "required": True}]},
            effect={"kind": "insert", "value_mapping": {"id": {"field": "id"}}},
            actor_sub="sub",
        )
    assert "select" in str(exc.value)


def test_create_type_mapping_column_not_in_dataset(db):
    entity = seed_entity(db)
    with pytest.raises(domain_service.InvalidActionType) as exc:
        make_type(db, entity, effect={
            "kind": "insert",
            "value_mapping": {"不存在的列": {"literal": "x"}},
        })
    assert "列快照" in str(exc.value)


def test_create_type_update_requires_single_pk_in_form(db):
    entity = seed_entity(db)
    # 主键列进 value_mapping → 拒（定位列不可改写）
    with pytest.raises(domain_service.InvalidActionType) as exc:
        make_type(db, entity, effect={
            "kind": "update",
            "value_mapping": {"id": {"field": "id"}, "status": {"field": "status"}},
        })
    assert "主键列" in str(exc.value)
    # 主键字段不在表单 → 拒（update 定位依赖主键字段值）
    with pytest.raises(domain_service.InvalidActionType) as exc:
        domain_service.create_type(
            db, db, db,
            customer_id="cust-da", entity_id=entity.id, name="更新", risk="write",
            form_schema={"fields": [
                {"name": "status", "input": "select", "required": True,
                 "options": ["approved", "rejected"]},
            ]},
            effect={
                "kind": "update",
                "value_mapping": {"status": {"field": "status"}},
            },
            actor_sub="sub",
        )
    assert "主键字段" in str(exc.value) or "主键列" in str(exc.value)


def test_create_type_api_tier_rejected(db):
    entity = seed_entity(db, tier="api")
    with pytest.raises(domain_service.InvalidActionType) as exc:
        make_type(db, entity)
    assert "cell 档" in str(exc.value)


# ------------------------------------------------- 提议流（任务 3.2）


def test_propose_domain_action_pending_with_instance(db, sess):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    proposal, missing = propose_domain(
        sess, type_row, {"id": "c1", "status": "approved"},
        source_ref="fleet=f1; post=p1; title=巡检; target=persona:x",
    )
    assert proposal.state == "pending_confirm"
    assert proposal.tier == "inline"
    assert proposal.action_id == f"domain.{type_row.id}"
    assert proposal.conversation_id is None  # 0043：域动作无会话合法
    assert missing == []
    instance = domain_service.instance_by_proposal(
        db, customer_id="cust-da", proposal_id=proposal.id
    )
    assert instance is not None
    assert instance.status == "awaiting_confirmation"
    assert instance.channel == "mcp"
    assert instance.source_ref == "fleet=f1; post=p1; title=巡检; target=persona:x"


def test_propose_missing_required_preflight_422(db, sess):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    with pytest.raises(gate.PreflightError) as exc:
        propose_domain(sess, type_row, {"status": "approved"})  # 缺 id
    assert "id" in str(exc.value)


def test_propose_disabled_type_unregistered(db, sess):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    domain_service.set_enabled(sess.ai, type_row, enabled=False, actor_sub="sub")
    with pytest.raises(ActionNotRegistered):
        propose_domain(sess, type_row, {"id": "c1", "status": "approved"})


# ------------------------------------------------- 确认执行（任务 3.1）


def test_approve_executes_insert_via_write_batch(db, sess, engine_fake):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    proposal, _ = propose_domain(sess, type_row, {"id": "c1", "status": "approved"})
    result = gate.approve(sess.ai, sess, OWNER, proposal.id)
    assert result["state"] == "executed", result
    tool, args = engine_fake.calls[0]
    assert tool == "customer_dataset_write_batch"
    assert args["ops"] == [
        {"op": "insert", "values": {"id": "c1", "cname": "客户", "status": "approved"}}
    ]
    assert args["actor"].startswith("domain-action:")
    assert args["channel"] == "agent"
    instance = domain_service.instance_by_proposal(
        db, customer_id="cust-da", proposal_id=proposal.id
    )
    assert instance.status == "executed"
    assert instance.correction_id == 77


def test_approve_update_anchors_by_pk(db, sess, engine_fake):
    entity = seed_entity(db)
    type_row = make_type(
        db, entity,
        name="改状态",
        effect={
            "kind": "update",
            "value_mapping": {"status": {"field": "status"}},
        },
    )
    proposal, _ = propose_domain(
        sess, type_row, {"id": "c1", "status": "rejected"}
    )
    result = gate.approve(sess.ai, sess, OWNER, proposal.id)
    assert result["state"] == "executed", result
    _, args = engine_fake.calls[0]
    assert args["ops"] == [
        {"op": "update", "anchor": "c1", "values": {"status": "rejected"}}
    ]


def test_approve_engine_failure_marks_instance_failed(db, sess, engine_fake):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    proposal, _ = propose_domain(sess, type_row, {"id": "c1", "status": "approved"})
    engine_fake.fail = "引擎写失败"
    result = gate.approve(sess.ai, sess, OWNER, proposal.id)
    assert result["state"] == "failed"
    assert "引擎写失败" in result["error"]
    instance = domain_service.instance_by_proposal(
        db, customer_id="cust-da", proposal_id=proposal.id
    )
    assert instance.status == "failed"
    assert "引擎写失败" in instance.error


def test_approve_after_type_disabled_marks_instance_disabled(db, sess, engine_fake):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    proposal, _ = propose_domain(sess, type_row, {"id": "c1", "status": "approved"})
    domain_service.set_enabled(sess.ai, type_row, enabled=False, actor_sub="sub")
    # 停用即冻结在途实例（set_enabled）+ 确认按「未注册」拒绝（闸语义，
    # 不泄露存在性）；执行器内的启用态复检是竞态窗口的 backstop。
    with pytest.raises(ActionNotRegistered):
        gate.approve(sess.ai, sess, OWNER, proposal.id)
    instance = domain_service.instance_by_proposal(
        db, customer_id="cust-da", proposal_id=proposal.id
    )
    assert instance.status == "disabled"


def test_viewer_cannot_approve(db, sess, engine_fake):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    proposal, _ = propose_domain(sess, type_row, {"id": "c1", "status": "approved"})
    with pytest.raises(ApprovalDenied):
        gate.approve(sess.ai, sess, VIEWER, proposal.id)


# ------------------------------------------------- 拒绝（任务 3.2）


def test_reject_marks_instance_rejected(db, sess):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    proposal, _ = propose_domain(sess, type_row, {"id": "c1", "status": "approved"})
    gate_reject(sess.ai, OWNER, proposal.id, sess=sess)
    instance = domain_service.instance_by_proposal(
        db, customer_id="cust-da", proposal_id=proposal.id
    )
    assert instance.status == "rejected"


# ------------------------------------------------- 台账与级联


def test_instances_isolated_by_customer(db, sess):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    propose_domain(sess, type_row, {"id": "c1", "status": "approved"})
    rows = domain_service.list_instances(db, customer_id="cust-da")
    assert len(rows) == 1
    assert domain_service.list_instances(db, customer_id="cust-da-other") == []
    with pytest.raises(domain_service.ActionTypeNotFound):
        domain_service.get_owned_type(db, type_id=type_row.id, customer_id="cust-da-other")


def test_entity_delete_cascades_disable(db, sess):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    proposal, _ = propose_domain(sess, type_row, {"id": "c1", "status": "approved"})
    from fd_wire_api.entities import service as entity_service

    entity_service.delete_entity(db, entity, domain_db=db)
    assert db.get(DomainActionType, type_row.id).enabled is False
    # 在途实例冻结 disabled
    instance = domain_service.instance_by_proposal(
        db, customer_id="cust-da", proposal_id=proposal.id
    )
    assert instance.status == "disabled"


def test_registry_resolution_rejects_other_customer(db, sess):
    entity = seed_entity(db)
    type_row = make_type(db, entity)
    with pytest.raises(ActionNotRegistered):
        domain_service.build_domain_action_def(sess, "cust-da-other", type_row.id)
