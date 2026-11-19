"""敏感度闭包传播测试（wire-sensitivity-propagation 任务 5.1/1.1/1.3/3.x）。

覆盖（design D6 兼容矩阵 + spec 场景）：
- 父集边登记：derive 单亲 / link 双亲（extra_parents）/ 自环去重（任务 1.2）；
- 闭包 helper：三代链取严、join/link 双亲可达、dangling → incomplete、
  环 → incomplete、深度上限 → incomplete（任务 1.3）；
- 设档闸闭包化：敏感祖先链 market 422 指名链、自身敏感既有文案零回归、
  private 恒放行（任务 3.1，兼容矩阵六行）；
- fail-closed：父集不全 → directed/market 拒 + 修复路径文案（任务 3.3）；
- 向下传播：敏感化连带撤回 market 后代、响应如实列出、幂等（重复触发零
  重复）、祖先改回常规后代不自动恢复（任务 3.2）；
- 删除清理：删子集清 child 边；父集删除 → 后代 incomplete（任务 1.1 注记）；
- 存量回填 SQL：与迁移 0041 的 BACKFILL_SQL 逐字一致（任务 1.1）；
- 目录列表：有效敏感度徽标三字段 + 敏感筛选命中继承行（任务 4.1）。

环境：datasets 内存库（挂 DatasetsBase——资产三表 + datasets）+ 平台库
（console_fixture，owner/editor/viewer 三角色种子）——照 test_assets_api 先例。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（照 test_assets_api 先例）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

import fd_wire_api.assets.routes as assets_routes
from fd_wire_api.assets.models import AssetMeta, DatasetParentRef
from fd_wire_api.assets.sensitivity import MAX_CLOSURE_DEPTH, effective_sensitivity_bulk
from fd_wire_api.console.models import CustomerMember
from fd_wire_api.data_plane.auth import DataPlanePrincipal
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.market import service as market_service

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token


def _backfill_sql() -> str:
    """从迁移 0041 文件原样加载 BACKFILL_SQL（保证测试与迁移逐字一致）。"""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "20261029_0041_dataset_parent_refs.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0041", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BACKFILL_SQL

SUB_OWNER = "sub-sens-owner"
SUB_OTHER = "sub-sens-other"
SUB_EDITOR = "sub-sens-editor"

CUST = "sens-cust"
OTHER_CUST = "sens-other"

OWNER = bearer(mint_id_token(SUB_OWNER))


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    DatasetsBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session):
    """datasets 库会话覆写 + 角色种子（照 test_assets_api 先例）。"""
    from fd_wire_api.catalog import catalog_settings

    monkeypatch.setattr(catalog_settings, "catalog_sync_enabled", False)
    monkeypatch.setattr(
        assets_routes, "_lookup_registry_cadence", lambda dataset_key: None
    )

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    install_fake_jwks(monkeypatch)
    env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_OWNER, CUST), (SUB_OTHER, OTHER_CUST)],
    )
    with env.session() as sess:
        sess.add(
            CustomerMember(
                customer_id=env.subs[SUB_OWNER], logto_sub=SUB_EDITOR, role="editor"
            )
        )
        sess.commit()

    app.dependency_overrides[get_datasets_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ 助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = CUST,
    key: str,
    name: str | None = None,
    source_kind: str = "upload",
    row_count: int = 10,
) -> Dataset:
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name or key,
        source_kind=source_kind,
        storage_tier="cell",
        row_count=row_count,
    )
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)
    return dataset


def register_child(
    db_session, parent: Dataset, *, extra=None, key="child", origin="derive"
) -> Dataset:
    """直调登记函数（任务 1.2 的单测入口；零引擎调用）。"""
    from fd_wire_api.data_plane.data_routes import _register_derived_child

    principal = DataPlanePrincipal(customer_id=CUST, channel="console", actor_id=CUST)
    return _register_derived_child(
        db_session,
        principal=principal,
        source=parent,
        name=key,
        engine_result={"dataset_key": key, "rows": 1},
        extra_parents=extra,
        origin=origin,
    )


def refs_of(db_session, dataset_id: str) -> list[tuple[str, str]]:
    rows = (
        db_session.query(DatasetParentRef)
        .filter(DatasetParentRef.child_dataset_id == dataset_id)
        .all()
    )
    return sorted((r.parent_dataset_id, r.origin) for r in rows)


# ------------------------------------------------- 登记矩阵（任务 1.2）


def test_register_derive_single_parent(db_session):
    parent = seed_dataset(db_session, key="orders")
    child = register_child(db_session, parent, key="orders_filtered")
    assert refs_of(db_session, child.id) == [(parent.id, "derive")]


def test_register_link_double_parent_and_self_dedup(db_session):
    left = seed_dataset(db_session, key="orders")
    right = seed_dataset(db_session, key="customers")
    child = register_child(db_session, left, extra=[right], key="joined", origin="link")
    assert set(refs_of(db_session, child.id)) == {
        (left.id, "link"),
        (right.id, "link"),
    }
    # 自环（link 左=右）去重为一条
    child2 = register_child(db_session, left, extra=[left], key="self_link", origin="link")
    assert set(refs_of(db_session, child2.id)) == {(left.id, "link")}


# ------------------------------------------------- 闭包 helper（任务 1.3/2.1）


def test_closure_three_generations_takes_max(db_session):
    grand = seed_dataset(db_session, key="g", name="祖集")
    grand_meta = None  # 敏感祖先不建 meta 行也可（默认常规）——用 meta 行驱动
    from fd_wire_api.assets.models import AssetMeta

    grand_meta = AssetMeta(dataset_id=grand.id, sensitivity="敏感")
    db_session.add(grand_meta)
    db_session.commit()
    mid = register_child(db_session, grand, key="m")
    leaf = register_child(db_session, mid, key="l")

    out = effective_sensitivity_bulk(db_session, CUST, [leaf.id])
    info = out[leaf.id]
    assert info["level"] == "敏感"
    assert info["own"] == "常规"
    assert info["inherited"] is True
    assert info["incomplete"] is False
    assert [c["name"] for c in info["chain"]] == ["祖集"]


def test_closure_join_double_parent_reachable(db_session):
    a = seed_dataset(db_session, key="a")
    b = seed_dataset(db_session, key="b")
    from fd_wire_api.assets.models import AssetMeta

    db_session.add(AssetMeta(dataset_id=b.id, sensitivity="内部"))
    db_session.commit()
    child = register_child(db_session, a, extra=[b], key="ab", origin="link")
    info = effective_sensitivity_bulk(db_session, CUST, [child.id])[child.id]
    assert info["level"] == "内部"  # 双亲闭包均可达
    assert info["inherited"] is True


def test_closure_dangling_reference_incomplete(db_session):
    parent = seed_dataset(db_session, key="p")
    child = register_child(db_session, parent, key="c")
    db_session.delete(parent)  # 父集删除 → dangling（刻意不清 parent 边）
    db_session.commit()
    info = effective_sensitivity_bulk(db_session, CUST, [child.id])[child.id]
    assert info["incomplete"] is True
    assert "父集引用不可解析" in info["incomplete_reason"]


def test_closure_cycle_incomplete(db_session):
    a = seed_dataset(db_session, key="ca")
    b = register_child(db_session, a, key="cb")
    db_session.add(
        DatasetParentRef(
            customer_id=CUST, child_dataset_id=a.id, parent_dataset_id=b.id, origin="legacy"
        )
    )
    db_session.commit()  # A→B→A 环
    info = effective_sensitivity_bulk(db_session, CUST, [a.id])[a.id]
    assert info["incomplete"] is True
    assert "环" in info["incomplete_reason"]


def test_closure_depth_cap_incomplete(db_session):
    prev = seed_dataset(db_session, key="d0")
    for i in range(MAX_CLOSURE_DEPTH + 2):
        prev = register_child(db_session, prev, key=f"d{i + 1}")
    info = effective_sensitivity_bulk(db_session, CUST, [prev.id])[prev.id]
    assert info["incomplete"] is True
    assert "深度上限" in info["incomplete_reason"]


def test_closure_other_customer_ref_incomplete(db_session):
    theirs = seed_dataset(db_session, customer_id=OTHER_CUST, key="x")
    mine = seed_dataset(db_session, key="mine")
    db_session.add(
        DatasetParentRef(
            customer_id=CUST,
            child_dataset_id=mine.id,
            parent_dataset_id=theirs.id,
            origin="legacy",
        )
    )
    db_session.commit()
    info = effective_sensitivity_bulk(db_session, CUST, [mine.id])[mine.id]
    assert info["incomplete"] is True


# ------------------------------------------------- 设档闸闭包（任务 3.1）


def test_gate_sensitive_ancestor_blocks_market_with_chain(db_session):
    grand = seed_dataset(db_session, key="pg", name="敏感祖集")
    from fd_wire_api.assets.models import AssetMeta

    db_session.add(AssetMeta(dataset_id=grand.id, sensitivity="敏感"))
    db_session.commit()
    leaf = register_child(db_session, grand, key="pl")
    with pytest.raises(market_service.VisibilityError) as exc:
        market_service.set_visibility(
            db_session, dataset=leaf, visibility="market", actor_sub=SUB_OWNER
        )
    assert "敏感祖集" in str(exc.value.detail)
    assert "继承自敏感祖先链" in str(exc.value.detail)
    # private 恒放行（兼容矩阵：既有语义零回归）
    market_service.set_visibility(
        db_session, dataset=leaf, visibility="private", actor_sub=SUB_OWNER
    )


def test_gate_own_sensitive_message_unchanged(db_session):
    """兼容矩阵行 1：自身敏感既有文案与档位禁发集合零回归（link 也禁）。"""
    ds = seed_dataset(db_session, key="own_sens")
    from fd_wire_api.assets.models import AssetMeta

    db_session.add(AssetMeta(dataset_id=ds.id, sensitivity="敏感"))
    db_session.commit()
    for vis in ("link", "directed", "market"):
        with pytest.raises(market_service.VisibilityError) as exc:
            market_service.set_visibility(
                db_session, dataset=ds, visibility=vis, actor_sub=SUB_OWNER
            )
        assert "只允许 private" in str(exc.value.detail)


def test_gate_regular_no_ancestor_allows_market(db_session):
    """兼容矩阵行 4：自身常规、无祖先 → 放行（既有行为零漂移）。"""
    ds = seed_dataset(db_session, key="plain")
    market_service.set_visibility(
        db_session, dataset=ds, visibility="market", actor_sub=SUB_OWNER
    )
    meta = db_session.get(AssetMeta, ds.id)
    assert meta.visibility == "market"


def test_gate_incomplete_fail_closed(db_session):
    """fail-closed：父集不全 → directed/market 拒 + 修复路径文案。"""
    parent = seed_dataset(db_session, key="gone")
    child = register_child(db_session, parent, key="orphan_child")
    db_session.delete(parent)
    db_session.commit()
    for vis in ("directed", "market"):
        with pytest.raises(market_service.VisibilityError) as exc:
            market_service.set_visibility(
                db_session, dataset=child, visibility=vis, actor_sub=SUB_OWNER
            )
        assert "父集不全" in str(exc.value.detail)
        assert "重派生" in str(exc.value.detail)


# ------------------------------------------------- 向下传播（任务 3.2）


def test_propagation_reverts_market_descendant(db_session):
    root = seed_dataset(db_session, key="root", name="根集")
    child = register_child(db_session, root, key="child_d")
    market_service.set_visibility(
        db_session, dataset=child, visibility="market", actor_sub=SUB_OWNER
    )
    reverted = market_service.propagate_downward_reverts(
        db_session, dataset=root, actor_sub=SUB_OWNER
    )
    assert reverted == [
        {"dataset_id": child.id, "name": child.name, "from": "market"}
    ]
    child_meta = db_session.get(AssetMeta, child.id)
    assert child_meta.visibility == "private"
    # 幂等：重复触发零重复撤回
    assert (
        market_service.propagate_downward_reverts(
            db_session, dataset=root, actor_sub=SUB_OWNER
        )
        == []
    )


def test_propagation_no_auto_restore_when_ancestor_reverted(db_session):
    """祖先改回常规 → 后代保持 private（撤回是终态动作，spec 硬要求）。

    时序：祖先常规时后代发到 market → 祖先敏感化（传播连带撤回）→ 祖先改回
    常规 → 后代不自动恢复（重新开放由客户显式执行）。
    """
    root = seed_dataset(db_session, key="r2")
    child = register_child(db_session, root, key="c2")
    market_service.set_visibility(
        db_session, dataset=child, visibility="market", actor_sub=SUB_OWNER
    )
    # 祖先敏感化（模拟 assets PATCH 的触发时刻——传播由 PATCH 路径调用）
    root_meta = AssetMeta(dataset_id=root.id, sensitivity="敏感")
    db_session.add(root_meta)
    db_session.commit()
    reverted = market_service.propagate_downward_reverts(
        db_session, dataset=root, actor_sub=SUB_OWNER
    )
    assert {r["dataset_id"] for r in reverted} == {child.id}
    # 祖先改回常规
    root_meta.sensitivity = "常规"
    db_session.commit()
    child_meta = db_session.get(AssetMeta, child.id)
    assert child_meta.visibility == "private"  # 不自动恢复


def test_propagation_multi_level_chain(db_session):
    root = seed_dataset(db_session, key="r3")
    mid = register_child(db_session, root, key="m3")
    leaf = register_child(db_session, mid, key="l3")
    market_service.set_visibility(
        db_session, dataset=leaf, visibility="market", actor_sub=SUB_OWNER
    )
    reverted = market_service.propagate_downward_reverts(
        db_session, dataset=root, actor_sub=SUB_OWNER
    )
    assert {r["dataset_id"] for r in reverted} == {leaf.id}


# ------------------------------------------------- 删除清理（任务 1.1 注记）


def test_delete_dataset_cleans_child_refs(client, db_session):
    parent = seed_dataset(db_session, key="del_p")
    child = register_child(db_session, parent, key="del_c")
    child.storage_tier = "api"  # 本地删除路径（免引擎调用）
    db_session.commit()
    resp = client.delete(f"/api/v1/datasets/{child.id}", headers=OWNER)
    assert resp.status_code == 200, resp.text
    assert refs_of(db_session, child.id) == []  # child 边清理


# ------------------------------------------------- 存量回填（任务 1.1）


def test_backfill_sql_from_source_ref(db_session):
    """与迁移 0041 的 BACKFILL_SQL 逐字一致的语义验证（文件原样加载）。"""
    backfill_sql = _backfill_sql()

    parent = seed_dataset(db_session, key="bp")
    derived = Dataset(
        customer_id=CUST,
        key="bp_child",
        name="bp_child",
        source_kind="derived",
        source_ref=parent.key,
        row_count=1,
    )
    other = seed_dataset(db_session, key="bp_plain")
    db_session.add(derived)
    db_session.commit()
    db_session.execute(text(backfill_sql))
    db_session.commit()
    assert refs_of(db_session, derived.id) == [(parent.id, "legacy")]
    assert refs_of(db_session, other.id) == []  # 非 derived 行不回填
    # 幂等：重复执行零重复行
    db_session.execute(text(backfill_sql))
    db_session.commit()
    assert refs_of(db_session, derived.id) == [(parent.id, "legacy")]


# ------------------------------------------------- HTTP 面（任务 4.1）


def test_visibility_http_blocked_with_chain(client, db_session):
    grand = seed_dataset(db_session, key="hg", name="链祖集")
    from fd_wire_api.assets.models import AssetMeta

    db_session.add(AssetMeta(dataset_id=grand.id, sensitivity="敏感"))
    db_session.commit()
    leaf = register_child(db_session, grand, key="hl")
    resp = client.put(
        f"/api/v1/datasets/{leaf.id}/visibility",
        json={"visibility": "market", "access_mode": "open"},
        headers=OWNER,
    )
    assert resp.status_code == 422, resp.text
    assert "链祖集" in resp.json()["detail"]


def test_assets_list_effective_fields_and_filter(client, db_session):
    grand = seed_dataset(db_session, key="lg", name="列祖集")
    from fd_wire_api.assets.models import AssetMeta

    db_session.add(AssetMeta(dataset_id=grand.id, sensitivity="敏感"))
    db_session.commit()
    plain = seed_dataset(db_session, key="lplain")
    leaf = register_child(db_session, grand, key="lleaf")

    resp = client.get("/api/v1/assets", headers=OWNER)
    assert resp.status_code == 200, resp.text
    items = {item["id"]: item for item in resp.json()["items"]}
    leaf_asset = items[leaf.id]["asset"]
    assert leaf_asset["sensitivity_effective"] == "敏感"
    assert leaf_asset["sensitivity_inherited"] is True
    assert leaf_asset["sensitivity_incomplete"] is False
    assert items[plain.id]["asset"]["sensitivity_effective"] == "常规"
    assert items[plain.id]["asset"]["sensitivity_inherited"] is False

    # 敏感筛选命中继承行（SQL 层不做，Python 合并视图口径）
    resp = client.get("/api/v1/assets", params={"sensitivity": "敏感"}, headers=OWNER)
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {grand.id, leaf.id}


def test_patch_meta_propagates_and_reports(client, db_session):
    root = seed_dataset(db_session, key="pr", name="传播根")
    child = register_child(db_session, root, key="pc")
    market_service.set_visibility(
        db_session, dataset=child, visibility="market", actor_sub=SUB_OWNER
    )
    resp = client.patch(
        f"/api/v1/datasets/{root.id}/asset-meta",
        json={"sensitivity": "敏感"},
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sensitivity_effective"] == "敏感"
    assert body["propagated_reverts"] == [
        {"dataset_id": child.id, "name": child.name, "from": "market"}
    ]
