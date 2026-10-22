"""签名分享链接 API 测试（wire-dashboard-share 任务 1.1–1.3）。

覆盖：
- 生成→打开 happy path：url 形状（/api/v1/share/dashboard/{id}?t=…&v=…）、
  令牌三段格式白盒钉死（mac=HMAC-SHA256(secret, "id|version|exp")）、
  公开打开返回钉死版本 spec + expires_at（无 shared_at——令牌无 iat）；
- 生成闸：draft/非 dashboard 403（detail 明确）、跨客户与不存在 404、
  缺 Bearer 401；
- 打开闸（一律 404 detail 统一 "not found"，不泄露差异）：缺 t / 缺 v /
  v 与令牌内版本不一致 / 篡改 mac / 换产物重放 / 过期（TTL 负值生成即过期）/
  结构畸形；
- 版本钉死：编辑出新版本后旧链接仍看旧版本（artifact_versions 历史行
  spec，非当前指针）；重新生成返回新版本；
- 归档即失效（published 兜底）；
- TTL 三态（wire-share-ttl-options）：缺省 30 / 自选 7|90 天（链接可打开、
  exp 按所选窗口）；非法值 422 detail 列合法值；显式选择压过 env 缺省；
- FD_WIRE_SHARE_SECRET 未配置：两端点 503 detail 带原因（惰性 env，逐
  请求生效）。
"""

from __future__ import annotations

import hashlib
import hmac
import os

# 在导入 app 前把默认引擎指向内存库：TestClient 启动钩子里的
# init_ai_db() 不至于在测试机上落下 fd_wire_ai.db 文件。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db, reset_ai_runtime
from fd_wire_api.ai.models import AIDraftArtifact
from fd_wire_api.ai.service import create_conversation as create_ai_conversation
from fd_wire_api.ai.schemas import ConversationCreate
from fd_wire_api.app import app

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-share-001"
SUB_OTHER = "sub-share-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

SHARE_SECRET = "test-share-secret-32bytes"

SPEC_V1 = {
    "title": "中美日人均 GDP 对比",
    "spec": {
        "title": "中美日人均 GDP 对比",
        "panels": [
            {
                "id": "p1",
                "type": "kpi",
                "title": "最新人均 GDP",
                "span": 1,
                "source": "IND-GDP-CN",
                "snapshot": {"columns": ["value"], "rows": [[129.4]]},
                "config": {"unit": "万元", "subtitle": "2023"},
            }
        ],
    },
}

SPEC_V2 = {
    "title": "中美日人均 GDP 对比（修订）",
    "spec": {
        "title": "中美日人均 GDP 对比（修订）",
        "panels": [
            {
                "id": "p1",
                "type": "table",
                "title": "人均 GDP 明细",
                "span": 3,
                "snapshot": {"columns": ["年份", "GDP"], "rows": [["2023", 129.4]]},
            }
        ],
    },
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
    """覆写 get_db 的 TestClient；FD_WIRE_SHARE_SECRET 已配置（默认态）。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", SHARE_SECRET)

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


def _make_artifact(
    db_session,
    client,
    *,
    spec=None,
    kind="dashboard",
    publish=True,
    customer_id: str = "cust-001",
) -> dict:
    """直插草稿 → 导入 →（可选）发布，返回产物详情 dict。"""
    conversation = create_ai_conversation(
        db_session, customer_id=customer_id, payload=ConversationCreate(title="GDP 分析")
    )
    draft = AIDraftArtifact(
        conversation_id=conversation.id,
        customer_id=customer_id,
        kind=kind,
        spec=spec or SPEC_V1,
        status="draft",
    )
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)
    resp = client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conversation.id, "draft_id": draft.id},
        headers=CUST if customer_id == "cust-001" else OTHER,
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    if publish:
        published = client.post(
            f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST
        )
        assert published.status_code == 200, published.text
        artifact = published.json()
    return artifact


def _share(client, artifact_id: int, headers=CUST, *, ttl_days: int | None = None) -> dict:
    """生成分享链接的测试捷径；ttl_days 显式给出时带 body。"""
    json = None if ttl_days is None else {"ttl_days": ttl_days}
    resp = client.post(
        f"/api/v1/artifacts/{artifact_id}/share", headers=headers, json=json
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _parse_token(url: str) -> tuple[int, int, str]:
    """从 url 里拆 (version, exp, mac) 三段令牌。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    assert set(query) == {"t", "v"}
    version_tok, exp, mac = query["t"][0].split(".")
    assert int(query["v"][0]) == int(version_tok)  # url 的 v 与令牌内版本一致
    return int(version_tok), int(exp), mac


# ------------------------------------------------------------- 生成 → 打开


def test_create_and_open_happy_path(client, db_session) -> None:
    """生成→打开：url 形状 + 令牌格式白盒 + 公开返回钉死版本 spec。"""
    artifact = _make_artifact(db_session, client)
    link = _share(client, artifact["id"])

    assert link["version"] == artifact["version"] == 1
    assert link["url"].startswith(f"/api/v1/share/dashboard/{artifact['id']}?t=")
    assert "&v=1" in link["url"]
    # expires_at 为 UTC ISO-8601 且在未来（默认 30 天）
    assert link["expires_at"].startswith("20") and "+" in link["expires_at"]

    # 令牌白盒钉死：t = "{version}.{exp}.{mac}"，
    # mac = HMAC-SHA256(secret, "{artifact_id}|{version}|{exp}")
    version_tok, exp, mac = _parse_token(link["url"])
    expected = hmac.new(
        SHARE_SECRET.encode("utf-8"),
        f"{artifact['id']}|{version_tok}|{exp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(expected, mac)

    # 公开打开：无任何鉴权头，返回钉死版本快照（无 shared_at——令牌无 iat）
    opened = client.get(link["url"])
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert set(body) == {"title", "spec", "version", "expires_at"}
    assert body["title"] == SPEC_V1["title"]
    assert body["spec"] == SPEC_V1
    assert body["version"] == 1
    assert body["expires_at"] == link["expires_at"]


def test_ttl_env_override_controls_expiry_window(client, db_session, monkeypatch) -> None:
    """FD_WIRE_SHARE_TTL_SECONDS 覆盖默认 30 天：exp 按短窗生成。"""
    import time

    artifact = _make_artifact(db_session, client)
    link = _share(client, artifact["id"])
    _, exp, _ = _parse_token(link["url"])
    # 默认值钉死 30 天（容许生成与断言间跨秒 ±2s）
    assert 30 * 24 * 3600 - 2 <= exp - int(time.time()) <= 30 * 24 * 3600

    # 覆盖为 60 秒（env 惰性读——同进程内逐请求生效）
    monkeypatch.setenv("FD_WIRE_SHARE_TTL_SECONDS", "60")
    short = _share(client, artifact["id"])
    _, exp_short, _ = _parse_token(short["url"])
    assert 58 <= exp_short - int(time.time()) <= 60


def test_ttl_days_option_three_states(client, db_session, monkeypatch) -> None:
    """有效期三态（wire-share-ttl-options 1.1）：缺省 30 / 7 天 / 90 天；
    非法值 422 detail 列合法值；显式选择压过 env 缺省。"""
    import time

    artifact = _make_artifact(db_session, client)

    # 缺省（无 body）：30 天
    default_link = _share(client, artifact["id"])
    _, exp_default, _ = _parse_token(default_link["url"])
    assert 30 * 24 * 3600 - 2 <= exp_default - int(time.time()) <= 30 * 24 * 3600

    # 显式 7 天：链接可打开（令牌按所选有效期验签）
    link_7 = _share(client, artifact["id"], ttl_days=7)
    _, exp_7, _ = _parse_token(link_7["url"])
    assert 7 * 24 * 3600 - 2 <= exp_7 - int(time.time()) <= 7 * 24 * 3600
    assert client.get(link_7["url"]).status_code == 200

    # 显式 90 天：链接可打开
    link_90 = _share(client, artifact["id"], ttl_days=90)
    _, exp_90, _ = _parse_token(link_90["url"])
    assert 90 * 24 * 3600 - 2 <= exp_90 - int(time.time()) <= 90 * 24 * 3600
    assert client.get(link_90["url"]).status_code == 200

    # 非法值（不在枚举）：422，detail 列出全部合法值（0 档自
    # wire-record-view D4 起合法——长效令牌，其行为在 test_record_view.py
    # 的 0 档用例锚定）
    for bad in (14, 1, 365, -7):
        rejected = client.post(
            f"/api/v1/artifacts/{artifact['id']}/share",
            headers=CUST,
            json={"ttl_days": bad},
        )
        assert rejected.status_code == 422, bad
        detail = rejected.json()["detail"]
        for legal in ("0", "7", "30", "90"):
            assert legal in detail

    # 显式选择压过 env 缺省（FD_WIRE_SHARE_TTL_SECONDS 不再生效）
    monkeypatch.setenv("FD_WIRE_SHARE_TTL_SECONDS", "60")
    override = _share(client, artifact["id"], ttl_days=90)
    _, exp_override, _ = _parse_token(override["url"])
    assert 90 * 24 * 3600 - 2 <= exp_override - int(time.time()) <= 90 * 24 * 3600

    # 空 body（仅 {} 无字段）= 缺省 30（撤掉 env 覆盖后回到默认窗口）
    monkeypatch.delenv("FD_WIRE_SHARE_TTL_SECONDS", raising=False)
    empty = client.post(
        f"/api/v1/artifacts/{artifact['id']}/share", headers=CUST, json={}
    )
    assert empty.status_code == 200
    _, exp_empty, _ = _parse_token(empty.json()["url"])
    assert 30 * 24 * 3600 - 2 <= exp_empty - int(time.time()) <= 30 * 24 * 3600


# ----------------------------------------------------------------- 生成闸


def test_create_rejects_draft_and_non_dashboard(client, db_session) -> None:
    """draft 403（提示先发布）；非 dashboard kind 403（说明仅仪表盘）。"""
    draft_artifact = _make_artifact(db_session, client, publish=False)
    rejected = client.post(f"/api/v1/artifacts/{draft_artifact['id']}/share", headers=CUST)
    assert rejected.status_code == 403
    assert "published" in rejected.json()["detail"]

    alert = _make_artifact(
        db_session, client,
        spec={"title": "告警", "spec": {"indicator": "X", "threshold": 1}},
        kind="alert_rule",
    )
    wrong_kind = client.post(f"/api/v1/artifacts/{alert['id']}/share", headers=CUST)
    assert wrong_kind.status_code == 403
    assert "dashboard" in wrong_kind.json()["detail"]

    # archived 同样 403（归档后不再可分享）
    artifact = _make_artifact(db_session, client)
    client.post(f"/api/v1/artifacts/{artifact['id']}/archive", headers=CUST)
    archived = client.post(f"/api/v1/artifacts/{artifact['id']}/share", headers=CUST)
    assert archived.status_code == 403
    assert "published" in archived.json()["detail"]


def test_create_tenant_isolation_and_auth(client, db_session) -> None:
    """跨客户 404（不泄露他企存在性）；不存在 404；缺 Bearer 401。"""
    artifact = _make_artifact(db_session, client)
    aid = artifact["id"]
    assert client.post(f"/api/v1/artifacts/{aid}/share", headers=OTHER).status_code == 404
    assert client.post("/api/v1/artifacts/99999/share", headers=CUST).status_code == 404
    assert client.post(f"/api/v1/artifacts/{aid}/share").status_code == 401


# ----------------------------------------------------------------- 打开闸


def test_open_uniform_404_for_all_failures(client, db_session) -> None:
    """打开闸全量：缺 t/缺 v/v 不一致/篡改/换产物重放/畸形——404 且 detail 统一。"""
    artifact = _make_artifact(db_session, client)
    link = _share(client, artifact["id"])
    base, _, _ = link["url"].partition("?")
    token = link["url"].split("t=")[1].split("&")[0]

    other = _make_artifact(db_session, client)  # 同客户第二份 published 仪表盘
    other_link = _share(client, other["id"])
    other_token = other_link["url"].split("t=")[1].split("&")[0]

    cases = [
        base,                                   # 缺 t 缺 v
        f"{base}?t={token}",                    # 缺 v
        f"{base}?t={token}&v=2",                # v 与令牌内版本不一致
        f"{base}?t={token[:-1]}{('0' if token[-1] != '0' else '1')}&v=1",  # 篡改 mac
        f"{base}?t={token.upper()}&v=1",        # 大小写重编码即失配
        f"{base}?t={other_token}&v=1",          # 换产物重放（artifact_id 在签名消息内）
        f"{base}?t=not-a-token&v=1",            # 结构畸形
        f"{base}?t=1.2&v=1",                    # 两段
        f"{base}?t=x.y.z&v=1",                  # 前两段非整数
        f"/api/v1/share/dashboard/99999?t={token}&v=1",  # 产物不存在
    ]
    for url in cases:
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert resp.json()["detail"] == "not found", url


def test_open_expired_token_404(client, db_session, monkeypatch) -> None:
    """过期：TTL 负值生成即过期的令牌 → 打开 404（统一 detail）。"""
    artifact = _make_artifact(db_session, client)
    monkeypatch.setenv("FD_WIRE_SHARE_TTL_SECONDS", "-1")
    link = _share(client, artifact["id"])
    resp = client.get(link["url"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


# ---------------------------------------------------------------- 版本钉死


def test_open_pins_version_from_artifact_versions(client, db_session) -> None:
    """版本钉死：编辑出新版本后旧链接仍看旧版本（取 artifact_versions 历史行），
    且 v=2 配旧令牌 404；重新生成返回新版本。"""
    artifact = _make_artifact(db_session, client)
    aid = artifact["id"]
    link_v1 = _share(client, aid)

    # 编辑出新版本（published 仍可改 spec，版本递增到 2）
    put = client.put(f"/api/v1/artifacts/{aid}", json={"spec": SPEC_V2}, headers=CUST)
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2

    # 旧链接：仍返回 v1 快照（不看当前指针）
    opened = client.get(link_v1["url"])
    assert opened.status_code == 200
    body = opened.json()
    assert body["version"] == 1
    assert body["spec"] == SPEC_V1
    assert body["title"] == SPEC_V1["title"]

    # 旧令牌配 v=2：版本与令牌不一致 → 404
    stale = link_v1["url"].replace("&v=1", "&v=2")
    assert client.get(stale).status_code == 404

    # 重新生成：钉死新版本，打开即 v2 快照
    link_v2 = _share(client, aid)
    assert link_v2["version"] == 2
    opened_v2 = client.get(link_v2["url"])
    assert opened_v2.status_code == 200
    assert opened_v2.json()["version"] == 2
    assert opened_v2.json()["spec"] == SPEC_V2


def test_open_404_after_archive(client, db_session) -> None:
    """归档即失效：published 兜底——归档后旧链接 404（统一 detail）。"""
    artifact = _make_artifact(db_session, client)
    link = _share(client, artifact["id"])
    assert client.get(link["url"]).status_code == 200

    client.post(f"/api/v1/artifacts/{artifact['id']}/archive", headers=CUST)
    resp = client.get(link["url"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


# ------------------------------------------------------------ 未配置 secret


def test_secret_not_configured_503_both_endpoints(client, db_session, monkeypatch) -> None:
    """FD_WIRE_SHARE_SECRET 未配置：生成与打开两端点 503，detail 带原因
    （惰性 env——运行中撤掉配置即整体禁用）。"""
    artifact = _make_artifact(db_session, client)
    monkeypatch.delenv("FD_WIRE_SHARE_SECRET", raising=False)

    create = client.post(f"/api/v1/artifacts/{artifact['id']}/share", headers=CUST)
    assert create.status_code == 503
    assert "FD_WIRE_SHARE_SECRET" in create.json()["detail"]

    opened = client.get(f"/api/v1/share/dashboard/{artifact['id']}?t=1.2.abc&v=1")
    assert opened.status_code == 503
    assert "FD_WIRE_SHARE_SECRET" in opened.json()["detail"]

    # 配置回来后同进程即刻恢复（惰性读验证）
    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", SHARE_SECRET)
    assert _share(client, artifact["id"])["version"] == 1
