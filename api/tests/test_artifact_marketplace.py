"""产物市场 API 测试（wire-artifact-marketplace 任务 6.1–6.3）。

覆盖：

- **市场闸矩阵（6.1）**：默认私有（SQL 层过滤——未开放产物不出现在任何
  列表/检索/详情/订阅/渲染面）；四档往返（private/market/directed/link）；
  撤回即时失效（原订阅者渲染 403 如实说明、无关客户 404 不泄露）；kind
  白名单（form/alert 不可设档 422）；draft/archived 拒设非 private 409；
  幂等（同值零留痕）；viewer 403；敏感度双点闸（Q5：发布时 422 + 事后
  改敏感渲染拒 403，含 Q13 数据集侧互锁）；
- **复制三态（6.2）**：全绑定（bound=n/unbound=0）/部分未绑/全未绑；
  **快照剥离递归断言（Q4）**（新 spec 不存在任何 snapshot 键，数据值
  零残留）；allow_copy=false 403；未订阅 403；**溯源指针（Q7）**；
  复制后独立性（编辑互不影响）；
- **渲染路（6.3）**：订阅者 200（含快照 + refreshed_at 标注素材）；
  未订阅 403；approval 档申请→批准→渲染；定向档 grant 名单渲染；
  **详情结构摘要（Q8：无 config/snapshot/query）**；未鉴权 401；
- **Q9**：刷新路径写 spec refreshed_at（契约路径端点级验证）；
- **Q13**：数据集敏感度互锁（assets PATCH 改敏感 → 自动撤回 private
  + 留痕 + 响应如实告知）。

环境：ai 内存库（artifacts 两表 + 产物市场三表，AiBase）+ datasets 内存库
（datasets/asset_meta——敏感度与绑定判定走 ``get_session`` 直连，测试用
自己的 datasets 引擎会话工厂覆写）；三角色种子（owner/editor/viewer）。
"""

from __future__ import annotations

import copy
import os

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.admin.db import get_db as get_platform_db
from fd_wire_api.ai.db import AiBase, get_db, reset_ai_runtime
from fd_wire_api.ai.models import AIDraftArtifact
from fd_wire_api.ai.schemas import ConversationCreate
from fd_wire_api.ai.service import create_conversation as create_ai_conversation
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import Artifact, ArtifactShareGrant
from fd_wire_api.assets.models import AssetMeta, DatasetParentRef
from fd_wire_api.assets.routes import require_asset_editor
from fd_wire_api.console.models import CustomerMember
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.market import DatasetShareGrant  # noqa: F401 - 注册映射

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_OWNER = "sub-am-owner"
SUB_CONSUMER = "sub-am-consumer"
SUB_STRANGER = "sub-am-stranger"
SUB_EDITOR = "sub-am-editor"
SUB_VIEWER = "sub-am-viewer"

CUST = "am-owner"
CONSUMER = "am-consumer"
STRANGER = "am-stranger"

OWNER = bearer(mint_id_token(SUB_OWNER))
CONSUMER_H = bearer(mint_id_token(SUB_CONSUMER))
STRANGER_H = bearer(mint_id_token(SUB_STRANGER))
EDITOR = bearer(mint_id_token(SUB_EDITOR))
VIEWER = bearer(mint_id_token(SUB_VIEWER))


PANEL_WITH_SNAPSHOT = {
    "id": "p1",
    "type": "kpi",
    "title": "中国 PMI",
    "span": 1,
    "source": "PMI",
    "query": {"dataset_id": "ds-bound", "limit": 1},
    "snapshot": {"columns": ["月份", "PMI"], "rows": [["2026-09", 49.8]]},
    "config": {"unit": "点"},
}

PANEL_UNBOUND = {
    "id": "p2",
    "type": "line",
    "title": "GDP 趋势",
    "span": 2,
    "source": "macro_trend",
    "query": {"dataset_id": "ds-other", "limit": 10},
    "snapshot": {"columns": ["年份"], "rows": [["2025"]]},
}

DASH_SPEC = {
    "title": "宏观景气月报",
    "spec": {"panels": [copy.deepcopy(PANEL_WITH_SNAPSHOT), copy.deepcopy(PANEL_UNBOUND)]},
}

REPORT_SPEC = {
    "title": "新能源产业研究",
    "spec": {
        "content": "# 新能源\n\n报告正文……",
        "data_scope": "全国新能源装机",
        "data_range": "2015-2025",
    },
}


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def ai_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AiBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def ds_session():
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
def client(monkeypatch, tmp_path, ai_session, ds_session):
    """覆写双库依赖 + 四角色种子；``get_session``（敏感度/读权判定直连）
    同样指向测试 datasets 引擎。"""

    def override_ai():
        try:
            yield ai_session
        finally:
            pass

    def override_ds():
        try:
            yield ds_session
        finally:
            pass

    # market/artifact_service 的直连判定走 datasets.db.get_session——
    # 测试内把会话工厂换成 ds_session 同引擎（同生产语义：直连数据集库）
    from fd_wire_api.datasets import db as ds_db

    monkeypatch.setattr(
        ds_db, "_session_factory", lambda: (lambda: ds_session)  # noqa: ARG005
    )

    reset_ai_runtime()
    app.dependency_overrides[get_db] = override_ai
    app.dependency_overrides[get_datasets_db] = override_ds
    install_fake_jwks(monkeypatch)
    env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[
            (SUB_OWNER, CUST),
            (SUB_CONSUMER, CONSUMER),
            (SUB_STRANGER, STRANGER),
        ],
    )
    with env.session() as sess:
        sess.add(
            CustomerMember(
                customer_id=env.subs[SUB_OWNER], logto_sub=SUB_EDITOR, role="editor"
            )
        )
        sess.add(
            CustomerMember(
                customer_id=env.subs[SUB_OWNER], logto_sub=SUB_VIEWER, role="viewer"
            )
        )
        sess.commit()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


# ------------------------------------------------------------------ 助手


def make_artifact(client, ai_session, *, spec=None, kind="dashboard", publish=True) -> dict:
    conv = create_ai_conversation(
        ai_session, customer_id=CUST, payload=ConversationCreate(title="t")
    )
    draft = AIDraftArtifact(
        conversation_id=conv.id,
        customer_id=CUST,
        kind=kind,
        spec=spec or DASH_SPEC,
        status="draft",
    )
    ai_session.add(draft)
    ai_session.commit()
    artifact = client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conv.id, "draft_id": draft.id},
        headers=OWNER,
    ).json()
    if publish:
        resp = client.post(f"/api/v1/artifacts/{artifact['id']}/publish", headers=OWNER)
        assert resp.status_code == 200, resp.text
    return artifact


def put_visibility(client, artifact_id: int, body: dict, headers=OWNER):
    return client.put(
        f"/api/v1/artifacts/{artifact_id}/visibility", json=body, headers=headers
    )


def seed_dataset(ds_session, *, dataset_id: str, customer_id: str = CUST) -> str:
    ds_session.add(
        Dataset(
            id=dataset_id,
            customer_id=customer_id,
            key=dataset_id,
            name=dataset_id,
            source_kind="upload",
            storage_tier="api",
            columns_json=[{"name": "id", "type": "TEXT"}],
            row_count=3,
        )
    )
    ds_session.commit()
    return dataset_id


def set_sensitivity(ds_session, dataset_id: str, value: str) -> None:
    meta = ds_session.get(AssetMeta, dataset_id)
    if meta is None:
        meta = AssetMeta(dataset_id=dataset_id)
        ds_session.add(meta)
    meta.sensitivity = value
    ds_session.commit()


def market_ids(client, headers=CONSUMER_H, **params) -> list[int]:
    resp = client.get("/api/v1/market/artifacts", headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


# ------------------------------------------- 6.1 闸矩阵：默认私有与四档往返


def test_default_private_absent_everywhere(client, ai_session) -> None:
    """默认私有（红线）：未开放产物在列表/检索/详情/订阅/渲染/复制面全无。"""
    artifact = make_artifact(client, ai_session)  # 发布但从未设可见性
    assert market_ids(client) == []
    assert market_ids(client, q="宏观") == []
    assert client.get(
        f"/api/v1/market/artifacts/{artifact['id']}", headers=CONSUMER_H
    ).status_code == 404
    assert client.post(
        f"/api/v1/market/artifacts/{artifact['id']}/subscribe", headers=CONSUMER_H
    ).status_code == 404
    assert client.get(
        f"/api/v1/market/artifacts/{artifact['id']}/render", headers=CONSUMER_H
    ).status_code == 404
    assert client.post(
        f"/api/v1/market/artifacts/{artifact['id']}/copy", headers=CONSUMER_H
    ).status_code == 404


def test_visibility_roundtrip_four_tiers(client, ai_session) -> None:
    """四档往返：market → 列表可见；link → 市场消失；directed → 名单制；
    回 private → 全消失（撤回即时失效）。每次变更留痕。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]

    # market：列表可见 + 卡片字段
    resp = put_visibility(client, aid, {"visibility": "market", "access_mode": "open"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["visibility"] == "market"
    assert body["published_version"] == 1  # 版本锚点
    assert body["published_at"]
    assert aid in market_ids(client)
    card = next(i for i in client.get(
        "/api/v1/market/artifacts", headers=CONSUMER_H
    ).json()["items"] if i["id"] == aid)
    assert card["is_mine"] is False
    assert card["publisher_customer_id"] == CUST
    assert card["allow_copy"] is False

    # link：市场消失（签名链接语义不经市场面）
    assert put_visibility(client, aid, {"visibility": "link"}).status_code == 200
    assert aid not in market_ids(client)

    # directed + grant 名单：市场列表不出现，grant 客户可见详情
    resp = put_visibility(
        client, aid, {"visibility": "directed", "grants": [CONSUMER]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["grants"] == [CONSUMER]
    assert aid not in market_ids(client)
    assert client.get(
        f"/api/v1/market/artifacts/{aid}", headers=CONSUMER_H
    ).status_code == 200
    assert client.get(
        f"/api/v1/market/artifacts/{aid}", headers=STRANGER_H
    ).status_code == 404  # 非名单内不泄露

    # 回 private：全消失，锚点清空
    resp = put_visibility(client, aid, {"visibility": "private"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["published_at"] is None
    assert resp.json()["published_version"] is None
    assert aid not in market_ids(client)
    assert client.get(
        f"/api/v1/market/artifacts/{aid}", headers=CONSUMER_H
    ).status_code == 404

    # 留痕：status_history 追加 visibility 事件（含 before/after 八字段）
    detail = client.get(f"/api/v1/artifacts/{aid}", headers=OWNER).json()
    events = [e for e in detail["status_history"] if e.get("note") == "visibility"]
    assert len(events) == 4
    assert events[0]["before"]["visibility"] == "private"
    assert events[0]["after"]["visibility"] == "market"
    assert events[-1]["after"]["visibility"] == "private"
    assert "grants" in events[1]["after"]  # directed 事件的名单快照


def test_visibility_idempotent_no_noise(client, ai_session) -> None:
    """幂等：重复设同值零留痕（重复点击不留噪声）。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})
    put_visibility(client, aid, {"visibility": "market"})
    detail = client.get(f"/api/v1/artifacts/{aid}", headers=OWNER).json()
    events = [e for e in detail["status_history"] if e.get("note") == "visibility"]
    assert len(events) == 1


def test_visibility_gates_draft_and_kind(client, ai_session) -> None:
    """draft 拒设非 private 409；非 dashboard/report kind 422；viewer 403。"""
    artifact = make_artifact(client, ai_session, publish=False)
    aid = artifact["id"]
    resp = put_visibility(client, aid, {"visibility": "market"})
    assert resp.status_code == 409, resp.text
    assert "先发布" in resp.json()["detail"]
    # draft 设 private 放行（幂等返回）
    assert put_visibility(client, aid, {"visibility": "private"}).status_code == 200

    form_artifact = make_artifact(
        client,
        ai_session,
        kind="form",
        spec={"title": "表单", "spec": {"fields": [{"name": "x"}]}},
    )
    resp = put_visibility(client, form_artifact["id"], {"visibility": "market"})
    assert resp.status_code == 422, resp.text
    assert "不支持市场可见性" in resp.json()["detail"]

    dash = make_artifact(client, ai_session, spec=copy.deepcopy(DASH_SPEC))
    resp = put_visibility(client, dash["id"], {"visibility": "market"}, headers=VIEWER)
    assert resp.status_code == 403  # viewer 不可设
    resp = put_visibility(client, dash["id"], {"visibility": "market"}, headers=EDITOR)
    assert resp.status_code == 200  # editor 可设


def test_directed_and_market_semantics_subscribe_404(
    client, ai_session
) -> None:
    """定向档不可订阅（访问只认 grant）；market 档订阅→渲染；approval 档
    订阅转申请。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    # directed：订阅 404（不在市场档）
    put_visibility(client, aid, {"visibility": "directed", "grants": []})
    assert client.post(
        f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H
    ).status_code == 404

    # market/open：订阅即得 → 渲染
    put_visibility(client, aid, {"visibility": "market", "access_mode": "open"})
    resp = client.post(
        f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H
    )
    assert resp.status_code == 200 and resp.json()["subscribed"] is True
    assert client.post(
        f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H
    ).json()["created"] is False  # 幂等

    # approval：新客户订阅 → 转申请（不直接订阅）
    put_visibility(client, aid, {"visibility": "market", "access_mode": "approval"})
    resp = client.post(
        f"/api/v1/market/artifacts/{aid}/subscribe", headers=STRANGER_H
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscribed"] is False
    assert resp.json()["access_request"]["status"] == "pending"


def test_approval_flow_approve_then_render(client, ai_session) -> None:
    """审批流：申请 → 发布方批准 → 订阅建立 → 渲染通过；拒绝可重提。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market", "access_mode": "approval"})

    # 未申请渲染 403
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H
    ).status_code == 403

    # 申请 + 发布方列表可见 + 批准
    resp = client.post(
        f"/api/v1/market/artifacts/{aid}/access-requests",
        json={},
        headers=CONSUMER_H,
    )
    assert resp.status_code == 200 and resp.json()["created"] is True
    req_id = resp.json()["access_request"]["id"]
    listing = client.get(
        f"/api/v1/artifacts/{aid}/access-requests", headers=OWNER
    ).json()
    assert [r["status"] for r in listing["items"]] == ["pending"]
    resp = client.post(
        f"/api/v1/artifacts/{aid}/access-requests/{req_id}/approve", headers=OWNER
    )
    assert resp.status_code == 200 and resp.json()["status"] == "approved"

    # 批准即订阅 → 渲染 200
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H
    ).status_code == 200

    # 拒绝可重提（fresh 客户）
    resp = client.post(
        f"/api/v1/market/artifacts/{aid}/access-requests",
        json={},
        headers=STRANGER_H,
    )
    req2 = resp.json()["access_request"]["id"]
    client.post(
        f"/api/v1/artifacts/{aid}/access-requests/{req2}/reject", headers=OWNER
    )
    resp = client.post(
        f"/api/v1/market/artifacts/{aid}/access-requests",
        json={},
        headers=STRANGER_H,
    )
    assert resp.json()["created"] is False
    assert resp.json()["access_request"]["status"] == "pending"  # 重提成功


def test_withdrawal_immediate_effect_honest_403(client, ai_session) -> None:
    """撤回即时失效：原订阅者渲染 403 如实说明；无关客户 404 不泄露。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})
    client.post(f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H)
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H
    ).status_code == 200

    put_visibility(client, aid, {"visibility": "private"})
    resp = client.get(f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H)
    assert resp.status_code == 403
    assert "撤回" in resp.json()["detail"]
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=STRANGER_H
    ).status_code == 404
    assert aid not in market_ids(client)
    # 原订阅者复制也如实 403（不静默 404）
    resp = client.post(
        f"/api/v1/market/artifacts/{aid}/copy", headers=CONSUMER_H
    )
    assert resp.status_code == 403


def test_archived_disappears_from_market(client, ai_session) -> None:
    """归档即下架：市场列表/详情/渲染立即消失（可见性档位不动，在架态说了算）。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})
    client.post(f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H)
    assert aid in market_ids(client)

    client.post(f"/api/v1/artifacts/{aid}/archive", headers=OWNER)
    assert aid not in market_ids(client)
    assert client.get(
        f"/api/v1/market/artifacts/{aid}", headers=CONSUMER_H
    ).status_code == 404
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H
    ).status_code == 403  # 原订阅者：如实「已下架」


def test_sensitivity_hard_gate_publish_422(client, ai_session, ds_session) -> None:
    """敏感度双点闸①：绑定数据集含敏感档 → 设 market 422（可 link/directed）。"""
    seed_dataset(ds_session, dataset_id="ds-bound")
    set_sensitivity(ds_session, "ds-bound", "敏感")
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    resp = put_visibility(client, aid, {"visibility": "market"})
    assert resp.status_code == 422, resp.text
    assert "敏感" in resp.json()["detail"] and "ds-bound" in resp.json()["detail"]
    # link/directed 放行（敏感闸只拦 market）
    assert put_visibility(client, aid, {"visibility": "link"}).status_code == 200


def test_sensitivity_render_recheck_after_change(
    client, ai_session, ds_session
) -> None:
    """敏感度双点闸②：发布后绑定数据集改敏感 → 订阅者渲染拒 403（Q10 实时）。"""
    seed_dataset(ds_session, dataset_id="ds-bound")
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})
    client.post(f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H)
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H
    ).status_code == 200

    set_sensitivity(ds_session, "ds-bound", "敏感")
    resp = client.get(f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H)
    assert resp.status_code == 403
    assert "敏感" in resp.json()["detail"]
    # 自有者不受此闸（发布方自己看自己的板）
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=OWNER
    ).status_code == 200


def test_sensitivity_closure_ancestor_blocks_publish(client, ai_session, ds_session):
    """有效敏感度闭包（wire-sensitivity-market-closure）：敏感祖先链上的
    派生子集绑进看板 → 设 market 422（堵数据集面闸的产物面旁路）。"""
    seed_dataset(ds_session, dataset_id="ds-anc")
    seed_dataset(ds_session, dataset_id="ds-bound")
    set_sensitivity(ds_session, "ds-anc", "敏感")
    ds_session.add(
        DatasetParentRef(
            customer_id=CUST,
            child_dataset_id="ds-bound",
            parent_dataset_id="ds-anc",
            origin="legacy",
        )
    )
    ds_session.commit()
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    resp = put_visibility(client, aid, {"visibility": "market"})
    assert resp.status_code == 422, resp.text
    assert "ds-bound" in resp.json()["detail"]
    # link/directed 放行（敏感闸只拦 market——口径与自身敏感一致）
    assert put_visibility(client, aid, {"visibility": "link"}).status_code == 200


def test_sensitivity_closure_incomplete_fail_closed(client, ai_session, ds_session):
    """父集不全（dangling 引用）→ 有效敏感度不可考 → 按敏感 fail-closed。"""
    seed_dataset(ds_session, dataset_id="ds-bound")
    ds_session.add(
        DatasetParentRef(
            customer_id=CUST,
            child_dataset_id="ds-bound",
            parent_dataset_id="ds-ghost-parent",
            origin="legacy",
        )
    )
    ds_session.commit()
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    resp = put_visibility(client, aid, {"visibility": "market"})
    assert resp.status_code == 422, resp.text
    assert "ds-bound" in resp.json()["detail"]


def test_sensitivity_closure_render_recheck_after_ancestor_change(
    client, ai_session, ds_session
):
    """发布后祖先改敏感 → 派生绑定集有效敏感度升严 → 订阅者渲染拒（闭包实时）。"""
    seed_dataset(ds_session, dataset_id="ds-anc2")
    seed_dataset(ds_session, dataset_id="ds-bound")
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})
    client.post(f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H)
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H
    ).status_code == 200

    ds_session.add(
        DatasetParentRef(
            customer_id=CUST,
            child_dataset_id="ds-bound",
            parent_dataset_id="ds-anc2",
            origin="legacy",
        )
    )
    set_sensitivity(ds_session, "ds-anc2", "敏感")
    resp = client.get(f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H)
    assert resp.status_code == 403
    assert "敏感" in resp.json()["detail"]
    # 祖先改回常规 → 下一次渲染即恢复（读时计算无陈旧值）
    set_sensitivity(ds_session, "ds-anc2", "常规")
    assert client.get(
        f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H
    ).status_code == 200


def test_unauthenticated_401(client, ai_session) -> None:
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})
    for method, path in [
        ("get", "/api/v1/market/artifacts"),
        ("get", f"/api/v1/market/artifacts/{aid}"),
        ("get", f"/api/v1/market/artifacts/{aid}/render"),
        ("post", f"/api/v1/market/artifacts/{aid}/subscribe"),
        ("post", f"/api/v1/market/artifacts/{aid}/copy"),
        ("put", f"/api/v1/artifacts/{aid}/visibility"),
    ]:
        kwargs = {"json": {}} if method in ("post", "put") else {}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401, (method, path, resp.status_code)


# ------------------------------------------- 6.2 复制三态 + 快照剥离 + 溯源


def _any_snapshot_key(node) -> bool:
    """递归探测 spec 中是否存在任何 snapshot 键。"""
    if isinstance(node, dict):
        return "snapshot" in node or any(_any_snapshot_key(v) for v in node.values())
    if isinstance(node, list):
        return any(_any_snapshot_key(v) for v in node)
    return False


def _collect_snapshot_values(node) -> list:
    if isinstance(node, dict):
        out = []
        for k, v in node.items():
            if k == "snapshot":
                out.append(v)
            else:
                out.extend(_collect_snapshot_values(v))
        return out
    if isinstance(node, list):
        out = []
        for v in node:
            out.extend(_collect_snapshot_values(v))
        return out
    return []


def test_copy_strips_snapshots_and_reports_bindings(client, ai_session, ds_session) -> None:
    """复制三态 + 快照剥离递归断言 + 溯源指针 + 复制后独立。

    caller（CONSUMER）自有 ds-bound（有读权）→ 该面板 bound；
    ds-other 属发布方且未订阅 → unbound。"""
    seed_dataset(ds_session, dataset_id="ds-bound", customer_id=CONSUMER)
    seed_dataset(ds_session, dataset_id="ds-other", customer_id=CUST)
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(
        client, aid, {"visibility": "market", "allow_copy": True}, headers=OWNER
    )
    client.post(f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H)

    resp = client.post(f"/api/v1/market/artifacts/{aid}/copy", headers=CONSUMER_H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "dashboard"
    assert body["copied_from_artifact_id"] == aid  # Q7 溯源指针
    assert body["binding_report"]["bound"] == 1
    assert body["binding_report"]["unbound"] == 1
    assert body["binding_report"]["unbound_datasets"] == ["ds-other"]

    # 新产物：draft v1，属 caller
    new_id = body["artifact_id"]
    detail = client.get(f"/api/v1/artifacts/{new_id}", headers=CONSUMER_H).json()
    assert detail["status"] == "draft"
    assert detail["version"] == 1
    new_spec = detail["spec"]

    # Q4 快照剥离递归断言：任何位置都没有 snapshot 键，原快照数据值零残留
    assert not _any_snapshot_key(new_spec)
    assert _collect_snapshot_values(new_spec) == []
    serialized = str(new_spec)
    assert "49.8" not in serialized  # 原 snapshot 数据值不得残留
    assert "2026-09" not in serialized

    # 绑定重映射：bound 面板查询保留；unbound 面板标 unbound
    panels = new_spec["spec"]["panels"]
    assert panels[0]["query"]["dataset_id"] == "ds-bound"
    assert "binding_status" not in panels[0]
    assert panels[1].get("binding_status") == "unbound"
    assert panels[1]["query"]["dataset_id"] == "ds-other"  # 引用保留，仅标注

    # 复制后独立：编辑自己那份不影响原产物
    edited = copy.deepcopy(new_spec)
    edited["title"] = "我的改版"
    resp = client.put(
        f"/api/v1/artifacts/{new_id}", json={"spec": edited}, headers=CONSUMER_H
    )
    assert resp.status_code == 200, resp.text
    origin = client.get(f"/api/v1/artifacts/{aid}", headers=OWNER).json()
    assert origin["spec"]["title"] == DASH_SPEC["title"]


def test_copy_gates_allow_copy_and_subscription(client, ai_session) -> None:
    """复制闸：allow_copy=false 403；未订阅 403；自有产物可复制。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})  # allow_copy 缺省 false
    client.post(f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H)
    resp = client.post(f"/api/v1/market/artifacts/{aid}/copy", headers=CONSUMER_H)
    assert resp.status_code == 403
    assert "未允许复制" in resp.json()["detail"]

    put_visibility(client, aid, {"visibility": "market", "allow_copy": True})
    resp = client.post(f"/api/v1/market/artifacts/{aid}/copy", headers=STRANGER_H)
    assert resp.status_code == 403
    assert "订阅" in resp.json()["detail"]

    # 自有产物可复制（自己的内容换一份独立 spec）
    resp = client.post(f"/api/v1/market/artifacts/{aid}/copy", headers=OWNER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["copied_from_artifact_id"] == aid


def test_copy_directed_grantee(client, ai_session) -> None:
    """定向档：名单内客户可复制（grant 即准入）；名单外 403（可发现但无授权）。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(
        client,
        aid,
        {"visibility": "directed", "grants": [CONSUMER], "allow_copy": True},
    )
    resp = client.post(f"/api/v1/market/artifacts/{aid}/copy", headers=CONSUMER_H)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/v1/market/artifacts/{aid}/copy", headers=STRANGER_H)
    assert resp.status_code == 403
    assert "订阅" in resp.json()["detail"]


# ------------------------------------------- 6.3 渲染路 + 详情结构摘要


def test_render_subscriber_gets_snapshot_and_time(client, ai_session) -> None:
    """订阅者渲染：返回版本 spec（含发布时刻快照）+ refreshed_at 素材。

    无 refreshed_at 的旧版本 → null（前端显「未知」）；有则如实透传。"""
    from fd_wire_api.artifacts.models import ArtifactVersion

    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    # 直写 refreshed_at 进 v1 spec（模拟刷新路径产物）
    refresh_time = "2026-10-13T08:00:00+00:00"
    version = ai_session.execute(
        select(ArtifactVersion).where(ArtifactVersion.artifact_id == aid)
    ).scalar_one()
    spec = dict(version.spec)
    spec["refreshed_at"] = refresh_time
    version.spec = spec
    ai_session.commit()

    put_visibility(client, aid, {"visibility": "market"})
    client.post(f"/api/v1/market/artifacts/{aid}/subscribe", headers=CONSUMER_H)
    resp = client.get(f"/api/v1/market/artifacts/{aid}/render", headers=CONSUMER_H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refreshed_at"] == refresh_time
    assert body["access_source"] == "market_subscription"
    # 渲染素材含快照（订阅者可见内容 = 发布时刻数据快照——Q1 已披露）
    assert body["spec"]["spec"]["panels"][0]["snapshot"]["rows"] == [
        ["2026-09", 49.8]
    ]


def test_render_refreshed_at_from_refresh_path(
    client, ai_session, monkeypatch
) -> None:
    """Q9 端点级：刷新（契约/AI 两路共用落盘）后 spec.refreshed_at 在位。

    LLM 与引擎依赖全假件（照 test_artifacts_refresh 手法）。"""
    from fd_wire_api.ai.protocols import LlmAssistantMessage
    from fd_wire_api.ai.routes import get_llm_client, get_tool_executor

    class _Llm:
        def complete(self, messages, tools):
            import json as _json

            return LlmAssistantMessage(
                content="```json\n"
                + _json.dumps(
                    {
                        "title": "宏观景气月报",
                        "spec": {"panels": [copy.deepcopy(PANEL_UNBOUND)]},
                    },
                    ensure_ascii=False,
                )
                + "\n```"
            )

    class _Executor:
        def execute(self, tool_name, args):  # pragma: no cover - 无工具调用场景
            return {"ok": True}

    app.dependency_overrides[get_llm_client] = lambda: _Llm()
    app.dependency_overrides[get_tool_executor] = lambda: _Executor()
    try:
        # 无 query 面板 → 走 AI 路（与契约路共用同一落盘点，Q9 两种刷新
        # 路径同源；契约路的 refreshed_at 断言在 test_artifacts_refresh）
        no_query = {
            "title": "宏观景气月报",
            "spec": {
                "panels": [
                    {
                        "id": "p1",
                        "type": "line",
                        "title": "趋势",
                        "span": 2,
                        "source": "macro_trend",
                        "snapshot": {"columns": ["年份"], "rows": [["2025"]]},
                    }
                ]
            },
        }
        artifact = make_artifact(client, ai_session, spec=no_query)
        aid = artifact["id"]
        resp = client.post(f"/api/v1/artifacts/{aid}/refresh", headers=OWNER)
        assert resp.status_code == 200, resp.text
        detail = client.get(f"/api/v1/artifacts/{aid}", headers=OWNER).json()
        assert detail["spec"]["refreshed_at"] == resp.json()["refreshed_at"]
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
        app.dependency_overrides.pop(get_tool_executor, None)


def test_detail_structure_summary_no_leak(client, ai_session) -> None:
    """详情结构摘要（Q8）：面板清单仅 {id,type,title}——无 config/snapshot/query；
    报告返回 data_scope/data_range。未订阅也可看详情（发现面）。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})

    resp = client.get(f"/api/v1/market/artifacts/{aid}", headers=CONSUMER_H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["panel_count"] == 2
    for panel in body["panels"]:
        assert set(panel.keys()) == {"id", "type", "title"}
    flat = str(body)
    assert "snapshot" not in flat
    assert "49.8" not in flat
    assert "query" not in flat and "config" not in flat
    assert body["access_source"] is None  # 未订阅（详情不要求订阅）


def test_detail_summary_excludes_v2_fields(client, ai_session) -> None:
    """v2 字段不进市场详情（wire-chart-contract-v2 任务 7.2）：encoding/
    panes/overlays/height/raw_option 一律不出现在摘要里——摘要口径不变。"""
    spec = {
        "title": "v2 看板",
        "spec": {
            "panels": [
                {
                    "id": "p1",
                    "type": "candlestick",
                    "title": "K 线",
                    "span": 3,
                    "height": 420,
                    "encoding": {"x": "日期", "xType": "time"},
                    "panes": [{"title": "主"}, {"title": "量"}],
                    "overlays": {"markLine": [{"value": 2.1}]},
                    "raw_option": {"series": [{"type": "line", "data": [1]}]},
                }
            ]
        },
    }
    artifact = make_artifact(client, ai_session, spec=spec)
    aid = artifact["id"]
    put_visibility(client, aid, {"visibility": "market"})

    resp = client.get(f"/api/v1/market/artifacts/{aid}", headers=CONSUMER_H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["panels"] == [{"id": "p1", "type": "candlestick", "title": "K 线"}]
    flat = str(body)
    for leaked in ["encoding", "panes", "overlays", "raw_option", '"height"']:
        assert leaked not in flat, f"v2 字段泄露到市场详情：{leaked}"

    # 报告：data_scope/data_range 元信息（Q11），无正文数据值摘要泄露
    report = make_artifact(client, ai_session, spec=REPORT_SPEC, kind="research_report")
    put_visibility(client, report["id"], {"visibility": "market"})
    resp = client.get(
        f"/api/v1/market/artifacts/{report['id']}", headers=CONSUMER_H
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_scope"] == "全国新能源装机"
    assert body["data_range"] == "2015-2025"
    assert "panels" not in body


def test_market_search_kind_and_q_filter(client, ai_session) -> None:
    """检索：kind 筛选只回对应类型；q 标题子串命中。"""
    dash = make_artifact(client, ai_session)
    report = make_artifact(client, ai_session, spec=REPORT_SPEC, kind="research_report")
    put_visibility(client, dash["id"], {"visibility": "market"})
    put_visibility(client, report["id"], {"visibility": "market"})

    assert market_ids(client, kind="dashboard") == [dash["id"]]
    assert market_ids(client, kind="research_report") == [report["id"]]
    assert market_ids(client, q="新能源") == [report["id"]]
    assert market_ids(client, q="不存在的标题") == []


def test_visibility_get_endpoint(client, ai_session) -> None:
    """GET visibility：缺省 private；设 directed 后带名单；跨客户 404。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    resp = client.get(f"/api/v1/artifacts/{aid}/visibility", headers=OWNER)
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "private"
    assert resp.json()["allow_copy"] is False

    put_visibility(client, aid, {"visibility": "directed", "grants": [CONSUMER]})
    resp = client.get(f"/api/v1/artifacts/{aid}/visibility", headers=OWNER)
    assert resp.json()["grants"] == [CONSUMER]

    assert client.get(
        f"/api/v1/artifacts/{aid}/visibility", headers=CONSUMER_H
    ).status_code == 404  # 他企 404 不泄露


def test_grants_incremental_endpoints(client, ai_session) -> None:
    """定向名单增量端点：POST 追加 / DELETE 移除；非 directed 档 422。"""
    artifact = make_artifact(client, ai_session)
    aid = artifact["id"]
    # 非 directed 档调用 → 422
    put_visibility(client, aid, {"visibility": "market"})
    resp = client.post(
        f"/api/v1/artifacts/{aid}/grants", json={"customer_id": CONSUMER}, headers=OWNER
    )
    assert resp.status_code == 422

    put_visibility(client, aid, {"visibility": "directed", "grants": []})
    resp = client.post(
        f"/api/v1/artifacts/{aid}/grants", json={"customer_id": CONSUMER}, headers=OWNER
    )
    assert resp.status_code == 200
    assert resp.json()["grants"] == [CONSUMER]

    resp = client.delete(
        f"/api/v1/artifacts/{aid}/grants/{CONSUMER}", headers=OWNER
    )
    assert resp.status_code == 200
    assert resp.json()["grants"] == []


# ------------------------------------------- Q13 数据集敏感度互锁


def test_q13_sensitivity_interlock_auto_withdraw(
    client, ai_session, ds_session
) -> None:
    """Q13：数据集在架（非 private）被改敏感 → assets PATCH 自动撤回 private
    + 留痕 + 响应如实告知（绝不静默改档）。"""
    ds_id = seed_dataset(ds_session, dataset_id="ds-live")
    # 先走数据集市场发布（directed + grant，非 private 档）
    resp = client.put(
        f"/api/v1/datasets/{ds_id}/visibility",
        json={"visibility": "directed", "grants": [CONSUMER]},
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["visibility"] == "directed"

    # assets PATCH 改成敏感 → 互锁撤回
    resp = client.patch(
        f"/api/v1/datasets/{ds_id}/asset-meta",
        json={"sensitivity": "敏感"},
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sensitivity"] == "敏感"
    assert body["auto_reverted_visibility"] == {"from": "directed", "to": "private"}
    # 可见性读取面证实已撤回且 grant 清空
    resp = client.get(f"/api/v1/datasets/{ds_id}/visibility", headers=OWNER)
    assert resp.json()["visibility"] == "private"
    assert resp.json()["published_at"] is None
    grants = client.get(f"/api/v1/datasets/{ds_id}/grants", headers=OWNER)
    assert grants.json()["grants"] == []

    # 留痕事件齐备：先 visibility=directed 变更，再 sensitivity+撤回
    events = client.get(
        f"/api/v1/datasets/{ds_id}/asset-meta/events", headers=OWNER
    ).json()["events"]
    # 事件新→旧：最近一条是互锁撤回（sensitivity 敏感 + visibility private）
    latest = events[0]
    assert latest["after"]["sensitivity"] == "敏感"
    assert latest["after"]["visibility"] == "private"
    assert latest["before"]["visibility"] == "directed"


def test_q13_private_dataset_no_interlock(client, ai_session, ds_session) -> None:
    """Q13 边界：private 档数据集改敏感无互锁（本就私有，零变更零噪声）。"""
    ds_id = seed_dataset(ds_session, dataset_id="ds-private")
    resp = client.patch(
        f"/api/v1/datasets/{ds_id}/asset-meta",
        json={"sensitivity": "敏感"},
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    assert "auto_reverted_visibility" not in resp.json()


def test_q13_non_sensitive_change_no_interlock(client, ai_session, ds_session) -> None:
    """Q13 边界：非「敏感」值变更（内部）不触发撤回。"""
    ds_id = seed_dataset(ds_session, dataset_id="ds-live2")
    client.put(
        f"/api/v1/datasets/{ds_id}/visibility",
        json={"visibility": "market"},
        headers=OWNER,
    )
    resp = client.patch(
        f"/api/v1/datasets/{ds_id}/asset-meta",
        json={"sensitivity": "内部"},
        headers=OWNER,
    )
    assert resp.status_code == 200
    assert "auto_reverted_visibility" not in resp.json()
    view = client.get(f"/api/v1/datasets/{ds_id}/visibility", headers=OWNER).json()
    assert view["visibility"] == "market"  # 档位未动
