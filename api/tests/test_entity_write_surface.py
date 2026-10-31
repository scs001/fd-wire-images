"""内置声明动作类型测试（wire-entity-write-surface 任务组 3）。

覆盖（spec 场景）：
- 声明提案全链（3.1）：propose→pending→owner 确认→实体/关系落库→列表通道
  可见；与控制台直声明逐字段同构；m:n 经中间集；**零引擎调用**（metadata 通道
  不触修正层/数据行）；台账 result_ref 回填、correction_id 恒 None。
- 校验矩阵（3.2）：主键列不在列快照/关系目标不在主键集/重复业务名/他企引用/
  m:n 缺中间集三键/中间集列不在快照——各自 PreflightError 且**不进 pending**
  （提议行与实例行都不落）。
- 漂移复校（3.3）：propose 后删列→执行 failed 零半写；撞名竞态→一成一败如实。
- 内置定义纪律（3.4）：内置名保留 409；客户 metadata 效果 422；负 id 无 DB 行
  （get_owned_type 404 语义）。
- 档位与隔离（3.5）：免费档数据集可声明（声明非付费能力）；跨企业引用按
  「不存在」拒（不泄露存在性差异）。
- 读面零漂移（3.6）：既有 test_entities/test_entity_objects/test_domain_actions
  全量作回归网（本文件只补内置通道增量）。

环境：单引擎内存库（AiBase+EntitiesBase+DatasetsBase 合库——闸会话束同库
注入，照 test_domain_actions 先例）；metadata 执行体不经引擎，engine_fake
仅作「未被调用」的反证。
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

from fd_wire_api.app import app  # noqa: F401 —— 先经入口（跨切片环解析序先例）

import fd_wire_api.datasets.ingest as datasets_ingest
from fd_wire_api.ai import actions as gate
from fd_wire_api.ai.actions import ActionNotRegistered, ActorContext, GateSessions, PreflightError
from fd_wire_api.ai.models import AIActionProposal
from fd_wire_api.datasets.db import DatasetsBase
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.domain_actions import builtins as da_builtins
from fd_wire_api.domain_actions import service as domain_service
from fd_wire_api.domain_actions.models import DomainActionInstance
from fd_wire_api.entities import service as entities_service
from fd_wire_api.entities.db import EntitiesBase

OWNER = ActorContext(customer_id="cust-ew", sub="sub-ew-owner", role="owner")
OTHERS = ActorContext(customer_id="cust-ew-other", sub="sub-y", role="owner")


@pytest.fixture()
def db():
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
    return GateSessions(
        ai=db, datasets=db, demands=db, materialization=db, packs=db, entities=db
    )


@pytest.fixture(autouse=True)
def _clear_proposed_args():
    gate._PROPOSED_ARGS.clear()
    yield
    gate._PROPOSED_ARGS.clear()


@pytest.fixture()
def engine_fake(monkeypatch):
    """引擎假件：metadata 通道**不得**触之（记录调用作反证）。"""

    class FakeEngine:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, tool: str, arguments: dict) -> dict:
            self.calls.append((tool, dict(arguments)))
            return {"correction_id": 1}

    fake = FakeEngine()
    monkeypatch.setattr(
        datasets_ingest, "get_engine_client_factory", lambda: lambda cid: fake
    )
    return fake


# ------------------------------------------------------------------ 播种


def seed_dataset(db, *, key: str, customer: str = "cust-ew", tier: str = "cell",
                 columns: tuple[str, ...] = ("id", "name", "region_id")) -> Dataset:
    dataset = Dataset(
        customer_id=customer,
        key=key,
        name=key,
        source_kind="upload",
        storage_tier=tier,
        columns_json=[{"name": c, "type": "TEXT"} for c in columns],
        row_count=5,
    )
    db.add(dataset)
    db.commit()
    return dataset


def propose_builtin(sess, type_id: int, values: dict, *, ctx=OWNER, source_ref=None):
    return gate.propose(
        sess.ai,
        sess,
        ctx,
        action_id=f"domain.{type_id}",
        args={"values": values, "source_ref": source_ref, "_channel": "mcp"},
    )


ENTITY_VALUES = {
    "business_name": "客户",
    "dataset_key": "customers",
    "pk_columns": "id",
    "display_column": "name",
    "column_notes": "id=主键;name=客户名",
}


def _entity_values_on(dataset_key: str) -> dict:
    """同构实体值换数据集（要求该集含 id/name/region_id 三列）。"""
    return {**ENTITY_VALUES, "dataset_key": dataset_key}

RELATION_VALUES = {
    "source_entity": "客户",
    "source_column": "region_id",
    "target_entity": "区域",
    "target_column": "id",
    "kind": "n:1",
}


def seed_two_entities(db, datasets_db=None):
    """客户+区域两实体（控制台直声明路径=对照真源）。"""
    customers = seed_dataset(db, key="customers",
                             columns=("id", "name", "region_id"))
    regions = seed_dataset(db, key="regions", columns=("id", "rname"))
    ent_c = entities_service.create_entity(
        db, db, customer_id="cust-ew", name="客户", dataset_id=customers.id,
        pk_columns=["id"], display_column="name",
    )
    ent_r = entities_service.create_entity(
        db, db, customer_id="cust-ew", name="区域", dataset_id=regions.id,
        pk_columns=["id"],
    )
    db.commit()
    return customers, regions, ent_c, ent_r


# ------------------------------------------------------------------ 3.1 全链


def test_entity_declare_propose_confirm_lands(db, sess, engine_fake):
    seed_dataset(db, key="customers", columns=("id", "name", "region_id"))
    proposal, _ = propose_builtin(
        sess, da_builtins.BUILTIN_ENTITY_DECLARE_ID, ENTITY_VALUES,
        source_ref="fleet=f1;post=deployer;title=声明提案员;target=cell:drillcust001",
    )
    assert proposal.state in ("pending_confirm", "awaiting_confirmation")
    instance = domain_service.instance_by_proposal(
        sess.ai, customer_id="cust-ew", proposal_id=proposal.id
    )
    assert instance is not None and instance.status == "awaiting_confirmation"

    gate.approve(sess.ai, sess, OWNER, proposal.id)

    entities = entities_service.list_entities(db, customer_id="cust-ew")
    assert [e.name for e in entities] == ["客户"]
    landed = entities[0]
    assert landed.pk_columns == ["id"]
    assert landed.display_column == "name"
    assert landed.column_notes == {"id": "主键", "name": "客户名"}
    # 台账：result_ref 回填实体 id、correction_id 恒 None（不进修正层）
    assert instance.status == "executed"
    assert instance.result_ref == str(landed.id)
    assert instance.correction_id is None
    assert instance.source_ref == "fleet=f1;post=deployer;title=声明提案员;target=cell:drillcust001"
    # metadata 通道零引擎调用（不触修正层/数据行）
    assert engine_fake.calls == []


def test_entity_declare_isomorphic_with_console_declare(db, sess):
    seed_dataset(db, key="customers")
    proposal, _ = propose_builtin(sess, -1, ENTITY_VALUES)
    gate.approve(sess.ai, sess, OWNER, proposal.id)
    via_proposal = entities_service.list_entities(db, customer_id="cust-ew")[0]

    ds2 = seed_dataset(db, key="customers2")
    direct = entities_service.create_entity(
        sess.entities, sess.datasets, customer_id="cust-ew", name="客户二",
        dataset_id=ds2.id, pk_columns=["id"], display_column="name",
        column_notes={"id": "主键", "name": "客户名"},
    )
    sess.entities.commit()
    # 同构断言：提案落库与控制台直声明逐字段一致（无「来源」差异暴露）
    for fieldname in ("pk_columns", "display_column", "column_notes"):
        assert getattr(via_proposal, fieldname) == getattr(direct, fieldname)


def test_relation_declare_confirm_lands_and_graph(db, sess, engine_fake):
    _, _, ent_c, ent_r = seed_two_entities(db)
    proposal, _ = propose_builtin(
        sess, da_builtins.BUILTIN_RELATION_DECLARE_ID, RELATION_VALUES
    )
    gate.approve(sess.ai, sess, OWNER, proposal.id)
    rels = entities_service.list_relationships(db, customer_id="cust-ew")
    assert len(rels) == 1
    rel = rels[0]
    assert (rel.from_entity_id, rel.to_entity_id) == (ent_c.id, ent_r.id)
    assert rel.kind == "n:1"
    assert engine_fake.calls == []
    instance = domain_service.instance_by_proposal(
        sess.ai, customer_id="cust-ew", proposal_id=proposal.id
    )
    assert instance.result_ref == str(rel.id)


def test_relation_declare_mn_via_junction(db, sess):
    seed_two_entities(db)
    seed_dataset(db, key="cust_regions", columns=("cust_id", "region_id"))
    values = {
        "source_entity": "客户", "source_column": "id",
        "target_entity": "区域", "target_column": "id", "kind": "m:n",
        "junction_dataset_key": "cust_regions",
        "junction_source_column": "cust_id",
        "junction_target_column": "region_id",
    }
    proposal, _ = propose_builtin(
        sess, da_builtins.BUILTIN_RELATION_DECLARE_ID, values
    )
    gate.approve(sess.ai, sess, OWNER, proposal.id)
    rels = entities_service.list_relationships(db, customer_id="cust-ew")
    assert [r.kind for r in rels] == ["m:n"]
    assert rels[0].junction_dataset_id is not None


# ------------------------------------------------------------------ 3.2 校验矩阵（不进 pending）


def _assert_not_pending(sess):
    assert sess.ai.query(DomainActionInstance).count() == 0
    assert sess.ai.query(AIActionProposal).count() == 0


def test_preflight_pk_column_not_in_snapshot(db, sess):
    seed_dataset(db, key="customers", columns=("id", "name"))
    with pytest.raises(PreflightError) as exc:
        propose_builtin(sess, -1, {**ENTITY_VALUES, "pk_columns": "ghost"})
    assert "ghost" in str(exc.value)
    _assert_not_pending(sess)


def test_preflight_relation_target_not_pk(db, sess):
    seed_two_entities(db)
    with pytest.raises(PreflightError) as exc:
        propose_builtin(sess, -2, {**RELATION_VALUES, "target_column": "rname"})
    assert "主键" in str(exc.value)
    _assert_not_pending(sess)


def test_preflight_duplicate_business_name(db, sess):
    seed_dataset(db, key="customers")
    seed_dataset(db, key="orders", columns=("id", "name", "region_id"))
    # 控制台已声明同名 → 提案 preflight 即拒（业务名唯一）
    p, _ = propose_builtin(sess, -1, ENTITY_VALUES)
    gate.approve(sess.ai, sess, OWNER, p.id)
    before = sess.ai.query(AIActionProposal).count()
    with pytest.raises(PreflightError) as exc:
        propose_builtin(sess, -1, _entity_values_on("orders"))
    assert "已存在" in str(exc.value) or "冲突" in str(exc.value)
    assert sess.ai.query(AIActionProposal).count() == before


def test_preflight_other_company_dataset_not_leaked(db, sess):
    seed_dataset(db, key="customers", customer="cust-ew-other")
    with pytest.raises(PreflightError) as exc:
        propose_builtin(sess, -1, ENTITY_VALUES)
    # 他企与不存在同文案，不泄露属主差异
    assert "不存在" in str(exc.value)
    _assert_not_pending(sess)


def test_preflight_mn_requires_junction_triple(db, sess):
    seed_two_entities(db)
    with pytest.raises(PreflightError):
        propose_builtin(sess, -2, {
            "source_entity": "客户", "source_column": "id",
            "target_entity": "区域", "target_column": "id", "kind": "m:n",
        })
    _assert_not_pending(sess)


def test_preflight_mn_rejects_extra_on_non_mn(db, sess):
    seed_two_entities(db)
    seed_dataset(db, key="cust_regions", columns=("cust_id", "region_id"))
    with pytest.raises(PreflightError):
        propose_builtin(sess, -2, {
            **RELATION_VALUES,
            "junction_dataset_key": "cust_regions",
        })
    _assert_not_pending(sess)


def test_preflight_required_field_missing(db, sess):
    seed_dataset(db, key="customers")
    with pytest.raises(PreflightError):
        propose_builtin(sess, -1, {"dataset_key": "customers", "pk_columns": "id"})
    _assert_not_pending(sess)


# ------------------------------------------------------------------ 3.3 漂移复校


def test_drift_after_propose_execute_fails_zero_halfwrite(db, sess):
    seed_dataset(db, key="customers", columns=("id", "name", "region_id"))
    proposal, _ = propose_builtin(sess, -1, ENTITY_VALUES)
    # propose 后列漂移：显示列被同步删除（主键列保留——否则 preflight 就拦了；
    # 执行复校走 create_entity 全链重跑）
    dataset = db.query(Dataset).filter(Dataset.key == "customers").one()
    dataset.columns_json = [{"name": "id", "type": "TEXT"}]
    db.commit()

    # 执行失败形态沿闸管线：approve 不上抛，实例置 failed+原因（test_domain_actions
    # engine-fail 同形态）
    gate.approve(sess.ai, sess, OWNER, proposal.id)
    instance = domain_service.instance_by_proposal(
        sess.ai, customer_id="cust-ew", proposal_id=proposal.id
    )
    assert instance.status == "failed"
    assert instance.error and "name" in instance.error  # 报的是显示列漂移
    assert entities_service.list_entities(db, customer_id="cust-ew") == []  # 零半写


def test_conflict_race_one_ok_one_failed(db, sess):
    seed_dataset(db, key="customers")
    seed_dataset(db, key="orders", columns=("id", "name", "region_id"))
    pa, _ = propose_builtin(sess, -1, ENTITY_VALUES)
    # 同业务名、异数据集：pa 确认前 pb 合法在途（preflight 时实体未落库）
    pb, _ = propose_builtin(sess, -1, _entity_values_on("orders"))
    gate.approve(sess.ai, sess, OWNER, pa.id)
    gate.approve(sess.ai, sess, OWNER, pb.id)
    ia = domain_service.instance_by_proposal(
        sess.ai, customer_id="cust-ew", proposal_id=pa.id)
    ib = domain_service.instance_by_proposal(
        sess.ai, customer_id="cust-ew", proposal_id=pb.id)
    assert ia.status == "executed" and ib.status == "failed"
    assert entities_service.list_entities(db, customer_id="cust-ew") and \
        len(entities_service.list_entities(db, customer_id="cust-ew")) == 1  # 只成一个


# ------------------------------------------------------------------ 3.4 内置定义纪律


def test_customer_type_metadata_effect_rejected(db):
    _, _, ent_c, _ = seed_two_entities(db)
    with pytest.raises(domain_service.InvalidActionType) as exc:
        domain_service.create_type(
            db, db, db, customer_id="cust-ew", entity_id=ent_c.id,
            name="我的声明", risk="write",
            form_schema={"fields": [{"name": "id", "input": "text", "required": True}]},
            effect={"kind": "metadata", "op": "entity.create"},
            actor_sub="sub",
        )
    assert "仅平台内置" in str(exc.value)


def test_builtin_names_reserved(db):
    _, _, ent_c, _ = seed_two_entities(db)
    with pytest.raises(domain_service.ActionTypeConflict) as exc:
        domain_service.create_type(
            db, db, db, customer_id="cust-ew", entity_id=ent_c.id,
            name="entity-declare", risk="write",
            form_schema={"fields": [{"name": "id", "input": "text", "required": True}]},
            effect={"kind": "insert", "value_mapping": {"name": {"field": "id"}}},
            actor_sub="sub",
        )
    assert "保留名" in str(exc.value)


def test_builtin_negative_id_has_no_db_row(db):
    with pytest.raises(domain_service.ActionTypeNotFound):
        domain_service.get_owned_type(
            db, type_id=da_builtins.BUILTIN_ENTITY_DECLARE_ID, customer_id="cust-ew"
        )


def test_builtin_resolve_via_gate_not_registered_for_wrong_shape(db, sess):
    # 未识别的负 id → 闸「未注册」拒绝（不泄露）
    with pytest.raises(ActionNotRegistered):
        domain_service.build_domain_action_def(sess, "cust-ew", -99)


# ------------------------------------------------------------------ 3.5 档位与隔离


def test_free_tier_dataset_declare_ok(db, sess):
    seed_dataset(db, key="customers", tier="api",
                 columns=("id", "name", "region_id"))
    proposal, _ = propose_builtin(sess, -1, ENTITY_VALUES)
    gate.approve(sess.ai, sess, OWNER, proposal.id)
    assert len(entities_service.list_entities(db, customer_id="cust-ew")) == 1


def test_builtin_visible_in_list_channel(db):
    rows = domain_service.list_enabled_for_customer(db, customer_id="cust-ew")
    assert rows == []  # 无 DB 行（客户域自声明为空）
    payloads = [da_builtins.builtin_payload(bt) for bt in da_builtins.builtin_types("cust-ew")]
    assert {p["id"] for p in payloads} == {-1, -2}
    assert all(p["builtin"] is True and p["enabled"] is True for p in payloads)
    # relation 的 kind 字段是字面量 select（v0 选项源纪律不破坏）
    kind_field = next(
        f for f in payloads[1]["form_schema"]["fields"] if f["name"] == "kind"
    )
    assert kind_field["options"] == ["n:1", "1:1", "1:n", "m:n"]
