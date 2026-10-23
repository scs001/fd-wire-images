"""产物生命周期 API 测试（wire-artifacts-persistence）。

覆盖（tasks 2.1–2.5 / 4.1）：
- 会话草稿导入 → 两次编辑（版本追加）→ 发布 → 归档 → 恢复 → 版本历史
  完整的 E2E，全程跨客户隔离（他企一律 404）；
- 列表 kind/status 过滤与更新倒序；
- archived 拒改 409（提示先恢复）与恢复后再编辑；
- 非法迁移 409（说明合法路径）；状态迁移操作记录（status_history）；
- append-only：历史版本内容不可变、无改写端点；
- spec 最小校验 422；鉴权 401；
- 空白建板（wire-dashboard-blank-create 1.1）：免费档第一块板的出生
  路径——{kind,title} 建最小空板 draft v1（kind 白名单仅 dashboard、
  title 必填非空 422、审计同款、他企列表隔离）；
- 模型约束（kind 五类 / status 三态，与 ai.tools 值域对表）；
- 迁移链 0021（0020 未落盘时以临时 no-op stub 接链验证，集成后自动走真链）。
"""

from __future__ import annotations

import os
from pathlib import Path

# 在导入 app 前把默认引擎指向内存库：TestClient 启动钩子里的
# init_ai_db() 不至于在测试机上落下 fd_wire_ai.db 文件。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db, reset_ai_runtime
from fd_wire_api.ai.models import AIDraftArtifact
from fd_wire_api.ai.service import create_conversation as create_ai_conversation
from fd_wire_api.ai.schemas import ConversationCreate
from fd_wire_api.ai.tools import ARTIFACT_TOOL_KINDS
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import ARTIFACT_KINDS, ArtifactStatus
from fd_wire_api.market.platform_publisher import PLATFORM_PUBLISHER_NAME

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-art-001"
SUB_OTHER = "sub-art-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

API_DIR = Path(__file__).resolve().parent.parent

DASHBOARD_SPEC = {
    "title": "中美日人均 GDP 对比",
    "spec": {"indicators": ["IND-GDP-US", "IND-GDP-CN"], "chart": "line"},
}


@pytest.fixture()
def db_session():
    """独立内存库（含 artifacts 两表——import 即注册到 AiBase）的会话。"""
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
def client(monkeypatch, tmp_path, db_session):
    """覆写 get_db（含 artifacts 路由——同一依赖对象）的 TestClient。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path, monkeypatch,
        customers=[(SUB_CUST, "cust-001"), (SUB_OTHER, "cust-002")],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


def stage_draft(db_session, *, customer_id: str = "cust-001", spec=None, kind="dashboard"):
    """直插一条会话草稿（模拟构建循环暂存产物），返回 (conversation, draft)。"""
    conversation = create_ai_conversation(
        db_session, customer_id=customer_id, payload=ConversationCreate(title="GDP 分析")
    )
    draft = AIDraftArtifact(
        conversation_id=conversation.id,
        customer_id=customer_id,
        kind=kind,
        spec=spec or DASHBOARD_SPEC,
        status="draft",
    )
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)
    return conversation, draft


def import_draft(client, *, conversation_id: int, draft_id: int, headers=CUST) -> dict:
    resp = client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conversation_id, "draft_id": draft_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------ E2E（4.1）


def test_e2e_import_edit_publish_archive_restore_versions(client, db_session) -> None:
    """全链路：草稿导入→两次编辑→发布→归档→恢复→版本历史完整。"""
    conversation, draft = stage_draft(db_session)
    artifact = import_draft(
        client, conversation_id=conversation.id, draft_id=draft.id
    )

    # 导入即 v1 draft 产物：spec 原样拷贝 + 来源会话指针 + 初始状态留痕
    assert artifact["status"] == "draft"
    assert artifact["version"] == 1
    assert artifact["kind"] == "dashboard"
    assert artifact["title"] == DASHBOARD_SPEC["title"]
    assert artifact["spec"] == DASHBOARD_SPEC
    assert artifact["origin_conversation_id"] == conversation.id
    assert artifact["status_history"][0]["status"] == "draft"

    # 两次编辑：版本递增到 3，title 随 spec 投影
    spec_v2 = {
        "title": "中美日人均 GDP 对比（修订）",
        "spec": {"indicators": ["IND-GDP-US", "IND-GDP-CN", "IND-GDP-JP"], "chart": "line"},
    }
    put2 = client.put(
        f"/api/v1/artifacts/{artifact['id']}",
        json={"spec": spec_v2},
        headers=CUST,
    )
    assert put2.status_code == 200, put2.text
    assert put2.json()["version"] == 2
    assert put2.json()["title"] == spec_v2["title"]

    spec_v3 = {
        "title": "中美日人均 GDP 对比（终版）",
        "spec": {"indicators": ["IND-GDP-CN"], "chart": "bar"},
    }
    put3 = client.put(
        f"/api/v1/artifacts/{artifact['id']}",
        json={"spec": spec_v3},
        headers=CUST,
    )
    assert put3.status_code == 200, put3.text
    assert put3.json()["version"] == 3
    assert put3.json()["spec"] == spec_v3

    # 发布 → 归档 → 恢复（每次迁移更新 updated_at 并留操作记录）
    published = client.post(
        f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    archived = client.post(
        f"/api/v1/artifacts/{artifact['id']}/archive", headers=CUST
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    restored = client.post(
        f"/api/v1/artifacts/{artifact['id']}/restore", headers=CUST
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "draft"

    detail = client.get(f"/api/v1/artifacts/{artifact['id']}", headers=CUST)
    assert detail.status_code == 200
    body = detail.json()
    # 状态操作记录完整：导入 + publish + archive + restore（append-only）
    assert [entry["status"] for entry in body["status_history"]] == [
        "draft", "published", "archived", "draft",
    ]
    assert all(entry["by"] == "console:cust-001" for entry in body["status_history"])

    # 版本历史完整：v3 → v2 → v1（新在前），任一版本内容可查且不可变
    versions = client.get(
        f"/api/v1/artifacts/{artifact['id']}/versions", headers=CUST
    )
    assert versions.status_code == 200
    assert [v["version"] for v in versions.json()] == [3, 2, 1]
    assert all(v["operator"] == "console:cust-001" for v in versions.json())

    v1 = client.get(
        f"/api/v1/artifacts/{artifact['id']}/versions/1", headers=CUST
    )
    assert v1.status_code == 200
    assert v1.json()["spec"] == DASHBOARD_SPEC  # 历史版本保持初始内容
    v2 = client.get(
        f"/api/v1/artifacts/{artifact['id']}/versions/2", headers=CUST
    )
    assert v2.json()["spec"] == spec_v2

    # 不存在的版本 404；历史版本无任何改写端点（PUT 到版本路径 = 405/404）
    assert client.get(
        f"/api/v1/artifacts/{artifact['id']}/versions/99", headers=CUST
    ).status_code == 404
    assert client.put(
        f"/api/v1/artifacts/{artifact['id']}/versions/1",
        json={"spec": spec_v2},
        headers=CUST,
    ).status_code in (404, 405)


def test_archived_rejects_edit_until_restored(client, db_session) -> None:
    """archived 拒改 409（明示先恢复）；恢复后可继续编辑。"""
    conversation, draft = stage_draft(db_session)
    artifact = import_draft(client, conversation_id=conversation.id, draft_id=draft.id)
    client.post(f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST)
    client.post(f"/api/v1/artifacts/{artifact['id']}/archive", headers=CUST)

    rejected = client.put(
        f"/api/v1/artifacts/{artifact['id']}",
        json={"spec": {"title": "偷改", "spec": {"x": 1}}},
        headers=CUST,
    )
    assert rejected.status_code == 409
    assert "恢复" in rejected.json()["detail"]

    client.post(f"/api/v1/artifacts/{artifact['id']}/restore", headers=CUST)
    ok = client.put(
        f"/api/v1/artifacts/{artifact['id']}",
        json={"spec": {"title": "恢复后修订", "spec": {"x": 1}}},
        headers=CUST,
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2


# ------------------------------------------------------------ 列表/过滤/排序


def test_list_filters_and_ordering(client, db_session) -> None:
    """列表：kind/status 过滤 + 更新时间倒序；跨客户互不可见。"""
    conv, dash = stage_draft(db_session, spec={"title": "看盘", "spec": {"a": 1}})
    alert_conv, alert = stage_draft(
        db_session,
        spec={"title": "告警", "spec": {"indicator": "X", "threshold": 1}},
        kind="alert_rule",
    )
    first = import_draft(client, conversation_id=conv.id, draft_id=dash.id)
    second = import_draft(client, conversation_id=alert_conv.id, draft_id=alert.id)
    client.post(f"/api/v1/artifacts/{first['id']}/publish", headers=CUST)

    # 全量（更新倒序：publish 触碰过 first 的 updated_at）
    listing = client.get("/api/v1/artifacts", headers=CUST)
    assert listing.status_code == 200
    ids = [a["id"] for a in listing.json()]
    assert ids == [first["id"], second["id"]]

    # kind 过滤
    only_alert = client.get("/api/v1/artifacts?kind=alert_rule", headers=CUST)
    assert [a["kind"] for a in only_alert.json()] == ["alert_rule"]

    # status 过滤
    only_published = client.get("/api/v1/artifacts?status=published", headers=CUST)
    assert [a["status"] for a in only_published.json()] == ["published"]

    # 组合过滤无匹配 → 空列表
    assert client.get(
        "/api/v1/artifacts?kind=alert_rule&status=published", headers=CUST
    ).json() == []


# ------------------------------------------------------------- 非法状态迁移


def test_illegal_transitions_409_with_legal_path(client, db_session) -> None:
    """非法迁移 409 且说明合法路径：重复发布 / 归档态发布 / 草稿态恢复。"""
    conv, draft = stage_draft(db_session)
    artifact = import_draft(client, conversation_id=conv.id, draft_id=draft.id)

    # published --publish--> ✗（合法路径提示 draft → published）
    client.post(f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST)
    republish = client.post(f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST)
    assert republish.status_code == 409
    assert "draft --publish--> published" in republish.json()["detail"]

    # archived --publish--> ✗（提示先恢复）
    client.post(f"/api/v1/artifacts/{artifact['id']}/archive", headers=CUST)
    publish_archived = client.post(
        f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST
    )
    assert publish_archived.status_code == 409
    assert "恢复" in publish_archived.json()["detail"]

    # draft --restore--> ✗（仅归档可恢复）
    conv2, draft2 = stage_draft(db_session, spec={"title": "另一份", "spec": {"b": 2}})
    artifact2 = import_draft(client, conversation_id=conv2.id, draft_id=draft2.id)
    restore_draft = client.post(
        f"/api/v1/artifacts/{artifact2['id']}/restore", headers=CUST
    )
    assert restore_draft.status_code == 409
    assert "archived --restore--> draft" in restore_draft.json()["detail"]

    # draft 可直接归档（draft --archive--> archived 合法）
    archive_draft = client.post(
        f"/api/v1/artifacts/{artifact2['id']}/archive", headers=CUST
    )
    assert archive_draft.status_code == 200
    assert archive_draft.json()["status"] == "archived"


# ----------------------------------------------------------------- 草稿导入


def test_from_draft_not_found_variants(client, db_session) -> None:
    """草稿导入 404：会话不存在 / 非本人会话 / 草稿不在该会话。"""
    conv, draft = stage_draft(db_session)
    other_conv, other_draft = stage_draft(
        db_session, customer_id="cust-002", spec={"title": "别家", "spec": {"c": 3}}
    )

    # 非本人会话（别家草稿）：不泄露存在性，一律 404
    assert client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": other_conv.id, "draft_id": other_draft.id},
        headers=CUST,
    ).status_code == 404

    # 会话不存在
    assert client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": 99999, "draft_id": draft.id},
        headers=CUST,
    ).status_code == 404

    # 草稿不属于该会话
    assert client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conv.id, "draft_id": other_draft.id},
        headers=CUST,
    ).status_code == 404

    # 本人正常导入仍通（前面的 404 不产生副作用）
    assert client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conv.id, "draft_id": draft.id},
        headers=CUST,
    ).status_code == 201


# ------------------------------------------------------------------ 空白建板


def test_blank_create_dashboard_success(client, db_session) -> None:
    """空白建板：201 draft v1 + 最小空板 spec + 审计同款 + 无来源会话指针。"""
    resp = client.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "我的第一块板"},
        headers=CUST,
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    assert artifact["kind"] == "dashboard"
    assert artifact["title"] == "我的第一块板"
    assert artifact["status"] == "draft"
    assert artifact["version"] == 1
    # v1 = 最小空板 spec（frozen 契约：{"title", "spec":{"title","panels":[]}}）
    assert artifact["spec"] == {
        "title": "我的第一块板",
        "spec": {"title": "我的第一块板", "panels": []},
    }
    # 直接创建：无来源会话指针（区别于 from-draft）
    assert artifact["origin_conversation_id"] is None
    # 初始状态留痕 + operator 审计同款（console:<customer_id>）
    assert artifact["status_history"][0]["status"] == "draft"
    assert artifact["status_history"][0]["by"] == "console:cust-001"
    assert "空白" in artifact["status_history"][0]["note"]

    # 详情可读；版本链 v1 唯一，operator 审计同款
    detail = client.get(f"/api/v1/artifacts/{artifact['id']}", headers=CUST)
    assert detail.status_code == 200
    assert detail.json()["spec"] == artifact["spec"]
    versions = client.get(
        f"/api/v1/artifacts/{artifact['id']}/versions", headers=CUST
    )
    assert [v["version"] for v in versions.json()] == [1]
    assert versions.json()[0]["operator"] == "console:cust-001"

    # 出生即接入既有编辑路径（免费档自助 BI 闭环）：PUT 追加 v2
    edited = client.put(
        f"/api/v1/artifacts/{artifact['id']}",
        json={
            "spec": {
                "title": "我的第一块板",
                "spec": {"title": "我的第一块板", "panels": [{"id": "p1"}]},
            }
        },
        headers=CUST,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2

    # 他企不可见（列表隔离——产物域既有隔离语义覆盖新出生路径）
    assert client.get("/api/v1/artifacts", headers=OTHER).json() == []


TWO_STEP_CASES = [
    (
        "dashboard",
        {"title": "两步建板", "spec": {"title": "两步建板", "panels": [{"id": "p1"}]}},
    ),
    (
        "doc",
        {"title": "两步建文", "spec": {"format": "markdown", "text": "# 标题\n\n正文"}},
    ),
]

TWO_STEP_BAD_CASES = [
    # dashboard 走通用层：缺 spec 键即 422
    ("dashboard", {"title": "坏看板"}),
    # doc 走正文契约层：format 非 markdown 即 422
    ("doc", {"title": "坏文档", "spec": {"format": "html", "text": "<p>x</p>"}}),
]


@pytest.mark.parametrize("kind,spec", TWO_STEP_CASES)
def test_two_step_create_dashboard_and_doc(client, kind, spec) -> None:
    """产物构建 MCP 的两步路径（空白建档 → PUT 完整 spec）对 dashboard/doc
    同样成立：v1=空 spec、v2=给定 spec、全程 draft。"""
    created = client.post(
        "/api/v1/artifacts", json={"kind": kind, "title": spec["title"]}, headers=CUST
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]
    assert created.json()["version"] == 1

    put = client.put(f"/api/v1/artifacts/{aid}", json={"spec": spec}, headers=CUST)
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2
    assert put.json()["status"] == "draft"
    assert put.json()["spec"] == spec


@pytest.mark.parametrize("kind,bad_spec", TWO_STEP_BAD_CASES)
def test_two_step_rejection_leaves_no_version(client, kind, bad_spec) -> None:
    """第二步被拒时零半写：不落 v2 版本行，产物停在 v1 空 spec
    （补偿归档由 MCP 层执行——见 wire-build-mcp 的补偿用例）。"""
    created = client.post(
        "/api/v1/artifacts", json={"kind": kind, "title": bad_spec["title"]}, headers=CUST
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]

    put = client.put(f"/api/v1/artifacts/{aid}", json={"spec": bad_spec}, headers=CUST)
    assert put.status_code == 422, put.text

    versions = client.get(f"/api/v1/artifacts/{aid}/versions", headers=CUST).json()
    assert [v["version"] for v in versions] == [1]
    assert client.get(f"/api/v1/artifacts/{aid}", headers=CUST).json()["version"] == 1


def test_source_ref_recorded_on_versions(client) -> None:
    """生产来源指针（wire-content-fleet D6）：带 source_ref 的创建/更新把它记在
    对应版本行上；不带则留空。来源不参与鉴权，也不进 spec。"""
    created = client.post(
        "/api/v1/artifacts",
        json={
            "kind": "dashboard",
            "title": "舰队建的板",
            "source_ref": "deploy=ops-cell;post=wire-fleet-content;turn=42",
        },
        headers=CUST,
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]
    # 来源不进 spec（spec 仍是干净的最小空板）
    assert created.json()["spec"]["spec"] == {"title": "舰队建的板", "panels": []}

    v1 = client.get(f"/api/v1/artifacts/{aid}/versions/1", headers=CUST).json()
    assert v1["source_ref"] == "deploy=ops-cell;post=wire-fleet-content;turn=42"

    # 更新带来源 → v2 记新来源；不带 → 留空
    client.put(
        f"/api/v1/artifacts/{aid}",
        json={
            "spec": {"title": "舰队建的板", "spec": {"title": "舰队建的板", "panels": [{"id": "p1"}]}},
            "source_ref": "deploy=ops-cell;post=wire-fleet-content;turn=43",
        },
        headers=CUST,
    )
    v2 = client.get(f"/api/v1/artifacts/{aid}/versions/2", headers=CUST).json()
    assert v2["source_ref"] == "deploy=ops-cell;post=wire-fleet-content;turn=43"

    # 人工创建：来源为空（既有行为逐字节不变）
    manual = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "人工建的板"}, headers=CUST
    ).json()
    mv1 = client.get(f"/api/v1/artifacts/{manual['id']}/versions/1", headers=CUST).json()
    assert mv1["source_ref"] is None


def test_blank_create_kind_whitelist_422(client) -> None:
    """kind 白名单=dashboard+form+alert_rule+record-view+doc：白名单外 kind
    422 如实拒，零半写。"""
    resp = client.post(
        "/api/v1/artifacts",
        json={"kind": "research_report", "title": "报告"},
        headers=CUST,
    )
    assert resp.status_code == 422
    assert (
        resp.json()["detail"]
        == "空白创建仅支持 dashboard、form、alert_rule、record-view、doc（其他 kind 由 AI 构建产生）"
    )
    # 拒收零半写：不落任何产物行
    assert client.get("/api/v1/artifacts", headers=CUST).json() == []


def test_blank_create_alert_rule_template(client) -> None:
    """alert_rule 空白出生（wire-alert-evaluation）：模板壳 v1 直接落、
    首次 PUT 真实 spec 才过规则契约层（出生版本不过契约）。"""
    resp = client.post(
        "/api/v1/artifacts",
        json={"kind": "alert_rule", "title": "我的告警"},
        headers=CUST,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "alert_rule" and body["status"] == "draft"
    inner = body["spec"]["spec"]
    assert inner["aggregate"]["func"] == "count"
    assert inner["condition"] == {"op": "gt", "value": 0}
    assert inner["intervalMinutes"] == 30
    # 占位 dataset_id 出厂即存（draft 不评估；PUT 真实规则才校验/武装）
    assert inner["query"]["dataset_id"] == "<数据集id>"


def test_blank_create_title_required_422(client) -> None:
    """title 必填非空：缺/空串/纯空白/非字符串 422；合法输入 strip 归一。"""
    # 缺 title（模型边界如实 422）
    assert client.post(
        "/api/v1/artifacts", json={"kind": "dashboard"}, headers=CUST
    ).status_code == 422
    # 空串 / 纯空白（service 统一口径）
    empty = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": ""}, headers=CUST
    )
    assert empty.status_code == 422
    assert "title" in empty.json()["detail"]
    assert client.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "   "},
        headers=CUST,
    ).status_code == 422
    # 非字符串（模型边界如实 422）
    assert client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": 123}, headers=CUST
    ).status_code == 422
    # 拒收零半写
    assert client.get("/api/v1/artifacts", headers=CUST).json() == []

    # 合法输入 strip 归一：title 投影与 spec 内外层一致去边空白
    ok = client.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "  空白板  "},
        headers=CUST,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["title"] == "空白板"
    assert ok.json()["spec"]["title"] == "空白板"


# ------------------------------------------------- 带内容创建 + slug 幂等


SUB_PLAT = "sub-art-plat"
PLAT = bearer(mint_id_token(SUB_PLAT))


@pytest.fixture()
def client_platform(monkeypatch, tmp_path, db_session):
    """同 client，另加平台自营发布方（finddata-official）的身份映射——
    带内容创建（add-artifact-create-endpoint）的平台租户测试身份。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[
            (SUB_CUST, "cust-001"),
            (SUB_OTHER, "cust-002"),
            (SUB_PLAT, PLATFORM_PUBLISHER_NAME),
        ],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


def _content_envelope(
    slug: str | None = "province-test",
    *,
    title: str = "省级面板测试板",
    panels: list | None = None,
) -> dict:
    """带内容创建的 spec 信封（showcase_slug 约定在 inner 层）。"""
    inner: dict = {"title": title, "panels": panels or [{"id": "kpi-1", "type": "kpi"}]}
    if slug is not None:
        inner["showcase_slug"] = slug
    return {"title": title, "spec": inner}


def test_create_with_spec_platform_201(client_platform) -> None:
    """平台租户带内容创建：201 draft v1=给定 spec，留痕如实区分出生方式。"""
    resp = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "省级面板测试板",
              "spec": _content_envelope()},
        headers=PLAT,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft" and body["version"] == 1
    assert body["customer_id"] == PLATFORM_PUBLISHER_NAME
    assert body["origin_conversation_id"] is None
    # v1 = 给定 spec 原样（含 showcase_slug 幂等键）
    assert body["spec"] == _content_envelope()
    # 留痕语义同空白创建（draft 初始留痕 + operator 同款），文案如实区分
    assert body["status_history"][0]["status"] == "draft"
    assert body["status_history"][0]["by"] == f"console:{PLATFORM_PUBLISHER_NAME}"
    assert "带内容创建" in body["status_history"][0]["note"]


def test_create_with_spec_non_platform_403_zero_halfwrite(client) -> None:
    """红线：非平台租户带内容创建 403 零半写；空白创建不受影响（201）。"""
    resp = client.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "越权板", "spec": _content_envelope()},
        headers=CUST,
    )
    assert resp.status_code == 403
    assert "平台自营发布方" in resp.json()["detail"]
    assert client.get("/api/v1/artifacts", headers=CUST).json() == []
    # 同一租户不带 spec 的空白创建照常（既有行为零变化）
    blank = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "空白板"},
        headers=CUST,
    )
    assert blank.status_code == 201


def test_create_with_spec_validation_422_zero_halfwrite(client_platform) -> None:
    """校验失败 422 零半写：缺 spec.spec / 白名单外 kind，均不落任何行。"""
    bad_inner = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "坏板",
              "spec": {"title": "坏板"}},
        headers=PLAT,
    )
    assert bad_inner.status_code == 422
    assert "spec.spec" in bad_inner.json()["detail"]
    bad_kind = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "research_report", "title": "报告",
              "spec": _content_envelope()},
        headers=PLAT,
    )
    assert bad_kind.status_code == 422
    assert client_platform.get("/api/v1/artifacts", headers=PLAT).json() == []


def test_slug_replay_appends_version_200(client_platform) -> None:
    """同 slug 重放：不建重、追加版本、200 返回既有产物（新建 201 区分）。"""
    first = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "省级面板测试板",
              "spec": _content_envelope(panels=[{"id": "kpi-1"}])},
        headers=PLAT,
    )
    assert first.status_code == 201
    created = first.json()

    replay = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "省级面板测试板",
              "spec": _content_envelope(
                  panels=[{"id": "kpi-1"}, {"id": "chart-1"}])},
        headers=PLAT,
    )
    assert replay.status_code == 200, replay.text
    updated = replay.json()
    assert updated["id"] == created["id"]
    assert updated["version"] == 2
    assert updated["spec"]["spec"]["panels"] == [
        {"id": "kpi-1"}, {"id": "chart-1"}]
    # 版本链 append-only：v1 原内容仍在
    v1 = client_platform.get(
        f"/api/v1/artifacts/{created['id']}/versions/1", headers=PLAT)
    assert v1.json()["spec"]["spec"]["panels"] == [{"id": "kpi-1"}]


def test_slug_replay_identical_no_redundant_version(client_platform) -> None:
    """同 slug 重放且内容一致：零操作（不追加冗余版本），200 返回现状。"""
    spec = _content_envelope()
    first = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "省级面板测试板", "spec": spec},
        headers=PLAT,
    )
    assert first.status_code == 201
    # 键序不同=同一内容（规范化语义比对，不制造假版本）
    reordered = {
        "spec": {"showcase_slug": "province-test",
                 "panels": spec["spec"]["panels"], "title": spec["spec"]["title"]},
        "title": spec["title"],
    }
    replay = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "省级面板测试板",
              "spec": reordered},
        headers=PLAT,
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == 1
    versions = client_platform.get(
        f"/api/v1/artifacts/{replay.json()['id']}/versions", headers=PLAT)
    assert [v["version"] for v in versions.json()] == [1]


def test_slug_replay_archived_409(client_platform) -> None:
    """slug 重放命中 archived 产物：409 与 PUT 同口径，版本链不动。"""
    created = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "省级面板测试板",
              "spec": _content_envelope()},
        headers=PLAT,
    ).json()
    assert client_platform.post(
        f"/api/v1/artifacts/{created['id']}/archive", headers=PLAT
    ).status_code == 200
    replay = client_platform.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "省级面板测试板",
              "spec": _content_envelope(panels=[{"id": "new-1"}])},
        headers=PLAT,
    )
    assert replay.status_code == 409
    versions = client_platform.get(
        f"/api/v1/artifacts/{created['id']}/versions", headers=PLAT)
    assert [v["version"] for v in versions.json()] == [1]


def test_slug_cross_tenant_isolation(db_session) -> None:
    """幂等查重只在请求方租户域内：他租户同 slug 不命中（service 层直证）。

    v0 带内容创建仅平台租户可用，跨租户场景由 service 层直接锁定——
    将来放开权限时语义不漂移。"""
    from fd_wire_api.artifacts.service import create_or_replay_artifact

    envelope_a = {"title": "A 板", "spec": {"title": "A 板",
                                            "panels": [],
                                            "showcase_slug": "shared-slug"}}
    artifact_a, created_a = create_or_replay_artifact(
        db_session, customer_id="finddata-official", kind="dashboard",
        title="A 板", spec=envelope_a,
    )
    assert created_a is True
    # 租户 B 用同一 slug：不被拒、不被合并——建自己的产物
    envelope_b = {"title": "B 板", "spec": {"title": "B 板",
                                            "panels": [],
                                            "showcase_slug": "shared-slug"}}
    artifact_b, created_b = create_or_replay_artifact(
        db_session, customer_id="cust-002", kind="dashboard",
        title="B 板", spec=envelope_b,
    )
    assert created_b is True
    assert artifact_b.id != artifact_a.id
    assert artifact_b.customer_id == "cust-002"
    # 租户 B 再重放：命中自己的（不是 A 的）
    _, created_b2 = create_or_replay_artifact(
        db_session, customer_id="cust-002", kind="dashboard",
        title="B 板", spec=envelope_b,
    )
    assert created_b2 is False


# ---------------------------------------------------------------- 跨客户隔离


def test_tenant_isolation_all_endpoints(client, db_session) -> None:
    """跨客户隔离：他企访问我的产物（详情/改/版本/三迁移）一律 404。"""
    conv, draft = stage_draft(db_session)
    artifact = import_draft(client, conversation_id=conv.id, draft_id=draft.id)
    aid = artifact["id"]

    assert client.get(f"/api/v1/artifacts/{aid}", headers=OTHER).status_code == 404
    assert client.put(
        f"/api/v1/artifacts/{aid}",
        json={"spec": {"title": "偷改", "spec": {"x": 1}}},
        headers=OTHER,
    ).status_code == 404
    assert client.get(f"/api/v1/artifacts/{aid}/versions", headers=OTHER).status_code == 404
    assert client.get(
        f"/api/v1/artifacts/{aid}/versions/1", headers=OTHER
    ).status_code == 404
    assert client.post(f"/api/v1/artifacts/{aid}/publish", headers=OTHER).status_code == 404
    assert client.post(f"/api/v1/artifacts/{aid}/archive", headers=OTHER).status_code == 404
    assert client.post(f"/api/v1/artifacts/{aid}/restore", headers=OTHER).status_code == 404
    # 列表互不可见
    assert client.get("/api/v1/artifacts", headers=OTHER).json() == []
    # 不存在的产物同样 404
    assert client.get("/api/v1/artifacts/99999", headers=CUST).status_code == 404


# ------------------------------------------------------------ 校验与鉴权


def test_put_invalid_spec_422(client, db_session) -> None:
    """spec 最小校验（沿用工具侧口径）：缺 title / 空 spec 对象 / 空 body 422。"""
    conv, draft = stage_draft(db_session)
    artifact = import_draft(client, conversation_id=conv.id, draft_id=draft.id)
    aid = artifact["id"]

    no_title = client.put(
        f"/api/v1/artifacts/{aid}", json={"spec": {"spec": {"a": 1}}}, headers=CUST
    )
    assert no_title.status_code == 422
    assert "title" in no_title.json()["detail"]

    empty_inner = client.put(
        f"/api/v1/artifacts/{aid}", json={"spec": {"title": "空规格"}}, headers=CUST
    )
    assert empty_inner.status_code == 422

    empty_body = client.put(f"/api/v1/artifacts/{aid}", json={"spec": {}}, headers=CUST)
    assert empty_body.status_code == 422

    # 全部拒收后版本不动
    assert client.get(f"/api/v1/artifacts/{aid}", headers=CUST).json()["version"] == 1


def test_auth_required(client) -> None:
    """缺 Bearer ID token 一律 401。"""
    assert client.get("/api/v1/artifacts").status_code == 401
    assert client.get("/api/v1/artifacts/1").status_code == 401
    assert client.put(
        "/api/v1/artifacts/1", json={"spec": {"title": "x", "spec": {"a": 1}}}
    ).status_code == 401
    assert client.post("/api/v1/artifacts/1/publish").status_code == 401
    assert client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": 1, "draft_id": 1},
    ).status_code == 401
    assert client.post(
        "/api/v1/artifacts",
        json={"kind": "dashboard", "title": "x"},
    ).status_code == 401


# --------------------------------------------------------------- 模型约束（1.2）


def test_model_enums_match_ai_tool_kinds() -> None:
    """kind 限五类（与 ai.tools 产物工具值域一致）；status 三态。"""
    assert set(ARTIFACT_KINDS) == set(ARTIFACT_TOOL_KINDS.values())
    assert ARTIFACT_KINDS == (
        "dashboard",
        "composite_indicator",
        "research_report",
        "alert_rule",
        "refresh_schedule",
    )
    assert {s.value for s in ArtifactStatus} == {"draft", "published", "archived"}


def test_version_unique_constraint_db_level(db_session) -> None:
    """(artifact_id, version) 唯一：库层拒绝重复版本号（append-only 兜底）。"""
    from sqlalchemy.exc import IntegrityError

    from fd_wire_api.artifacts.models import Artifact, ArtifactVersion

    artifact = Artifact(
        customer_id="cust-001",
        kind="dashboard",
        title="看盘",
        status="draft",
        version=1,
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactVersion(artifact_id=artifact.id, version=1, spec={"title": "x", "spec": {}})
    )
    db_session.commit()
    db_session.add(
        ArtifactVersion(
            artifact_id=artifact.id, version=1, spec={"title": "y", "spec": {}}
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --------------------------------------------------------------- 迁移链（1.1）

_STUB_0020 = '''"""stub: 0020_free_tier_datasets（并行批次预占位；仅本地测试接链用）"""
revision = "0020_free_tier_datasets"
down_revision = "0019_demand_three_state"
branch_labels = None
depends_on = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
'''


def _alembic_copy_with_stub(tmp_path: Path) -> Path:
    """复制 alembic 脚本目录到临时处；0020 未落盘时补 no-op stub 接链
    （真 0021 的 down_revision 指向它——集成后 stub 自动让位真链）。"""
    import shutil

    script_dir = tmp_path / "alembic"
    (script_dir / "versions").mkdir(parents=True)
    shutil.copy(API_DIR / "alembic" / "env.py", script_dir / "env.py")
    for path in (API_DIR / "alembic" / "versions").glob("*.py"):
        shutil.copy(path, script_dir / "versions" / path.name)
    has_0020 = any(
        'revision: str = "0020_free_tier_datasets"' in p.read_text()
        for p in (script_dir / "versions").glob("*.py")
    )
    if not has_0020:
        (script_dir / "versions" / "stub_0020.py").write_text(_STUB_0020, encoding="utf-8")
    return script_dir


@pytest.fixture()
def migrated_0021(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade 至 0021（0020 缺席时以 stub 接链）。"""
    script_dir = _alembic_copy_with_stub(tmp_path)
    db_url = f"sqlite:///{tmp_path}/artifacts_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(script_dir))
    command.upgrade(cfg, "0021_artifact_lifecycle")
    return cfg, db_url


@pytest.fixture()
def migrated_head(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade 至链头（drift check 用：ORM 含全链增量列）。"""
    script_dir = _alembic_copy_with_stub(tmp_path)
    db_url = f"sqlite:///{tmp_path}/artifacts_migration_head.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(script_dir))
    command.upgrade(cfg, "head")
    return cfg, db_url


def test_migration_creates_artifact_tables(migrated_0021) -> None:
    """0021 建两表 + 关键索引 + (artifact_id, version) 唯一；幂等重跑无恙。"""
    from sqlalchemy import create_engine, inspect

    cfg, db_url = migrated_0021
    command.upgrade(cfg, "0021_artifact_lifecycle")  # 幂等重跑

    engine = create_engine(db_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"artifacts", "artifact_versions"} <= tables

    assert {c["name"] for c in inspector.get_columns("artifacts")} == {
        "id", "customer_id", "kind", "title", "status", "version",
        "origin_conversation_id", "status_history", "created_at", "updated_at",
    }
    assert {c["name"] for c in inspector.get_columns("artifact_versions")} == {
        "id", "artifact_id", "version", "spec", "operator", "created_at",
    }
    index_names = {ix["name"] for ix in inspector.get_indexes("artifacts")}
    assert {
        "ix_artifacts_customer_id", "ix_artifacts_kind", "ix_artifacts_status",
        "ix_artifacts_origin_conversation_id",
    } <= index_names

    # (artifact_id, version) 唯一（sqlite autoindex，PRAGMA 按列断言）
    with engine.connect() as conn:
        unique_sets = []
        for _, name, unique, *_ in conn.exec_driver_sql(
            "PRAGMA index_list(artifact_versions)"
        ).fetchall():
            if not unique:
                continue
            cols = conn.exec_driver_sql(f"PRAGMA index_info({name})").fetchall()
            unique_sets.append({row[2] for row in cols})
    assert {"artifact_id", "version"} in unique_sets
    engine.dispose()


def test_migration_schema_matches_orm(migrated_head) -> None:
    """drift check：迁移产出列集与 ORM 声明（AiBase.metadata）逐表一致。

    在**链头**上对账（0021 之后 artifacts 还经历了 0036 产物市场增量列；
    只看 0021 会把后续增量列误报为漂移）。"""
    from sqlalchemy import create_engine, inspect

    _, db_url = migrated_head
    engine = create_engine(db_url)
    inspector = inspect(engine)
    for table in ("artifacts", "artifact_versions"):
        migrated = {c["name"] for c in inspector.get_columns(table)}
        declared = set(AiBase.metadata.tables[table].columns.keys())
        # artifact_versions 无后续增量列，链头与 0021 同形；artifacts 的
        # 0036 六列（visibility/access_mode/published_at/published_version/
        # allow_copy/copied_from_artifact_id）经迁移在位即通过。
        assert migrated == declared, f"{table} 迁移与 ORM 列漂移: {migrated ^ declared}"
    engine.dispose()


def test_migration_downgrade_drops_tables(migrated_0021, monkeypatch, tmp_path) -> None:
    """降级回 0019：两表全清。"""
    from sqlalchemy import create_engine, inspect

    cfg, db_url = migrated_0021
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.downgrade(cfg, "0019_demand_three_state")
    engine = create_engine(db_url)
    assert not ({"artifacts", "artifact_versions"} & set(inspect(engine).get_table_names()))
    engine.dispose()
