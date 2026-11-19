"""wire-rich-text-docs 后端分享与展示面测试（任务 5.1 / 1.4）。

覆盖：
- 生成面：published doc → 链接 ``/api/v1/share/doc/{id}?t=…&v=…``（钉死
  生成时刻版本）；draft/archived 403 提示先发布；kind 门文案点名文档（doc）
  （白名单外 kind 403——文案随 kind 集合扩充，任务 5.1）；
- 公开面：无鉴权打开 200 返回钉死版本 spec 信封原样（正文源文本，渲染在
  web 侧）+ title 回退；统一 404 矩阵（缺 t/篡改/v 不一致/畸形/产物不存在/
  非 published 产物/跨 kind 令牌/归档断链——detail 恒 "not found"，跨例
  逐字相等不泄露失败原因差异）；
- 钉死版本：分享后再保存新版本，旧链接仍呈现旧版本正文（改版不串版，
  tasks 5.3 场景）；重新生成钉死新版本；
- TTL 0 档长效：expires_at=null、令牌中段 "0"；时间冻结到五十年后仍验签
  通过（exp=0 哨兵跳过过期校验——wire-record-view D4 语义在 doc 同享）；
- secret 未配置：生成与公开两端点 503 detail 带原因（惰性 env 即刻恢复）；
- 展示面：SHARE_KIND_PATHS 收录 doc、WORKBENCH_KINDS 收录 doc，
  ``artifacts_section`` 计数含 doc 且 active_total 对账（archived 不计）。

环境：照 test_record_view 先例（ai 与 datasets 同引擎合库、分享 secret 已
配置、JWKS 假件 + 平台库客户种子）。doc 产物**直插 DB 建档**——doc kind 的
service 层出生/保存路径由并行任务实现，本测试只锚定分享面与展示面接线，
不依赖空白创建面。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from types import SimpleNamespace

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import Artifact, ArtifactVersion
from fd_wire_api.share import routes as share_routes
from fd_wire_api.share import service as share_service
from fd_wire_api.workbench import WORKBENCH_KINDS, artifacts_section

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-doc-share-001"
SUB_OTHER = "sub-doc-share-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

SHARE_SECRET = "test-doc-share-secret-32bytes"

TITLE = "年度质量报告"
TEXT_V1 = "# 质量报告\n\n第一段正文，**加粗**，含列表：\n\n- 指标一\n- 指标二\n"


@pytest.fixture()
def db_session():
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
    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", SHARE_SECRET)
    monkeypatch.delenv("FD_WIRE_SHARE_TTL_SECONDS", raising=False)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_ai_db] = override_get_db
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


# ------------------------------------------------------------------ 种子助手


def doc_envelope(title: str, text: str) -> dict:
    """doc spec 信封（契约：``{title, spec:{format:"markdown", text}}``）。"""
    return {"title": title, "spec": {"format": "markdown", "text": text}}


def seed_doc(
    db_session,
    *,
    customer_id: str = "cust-001",
    title: str = TITLE,
    status: str = "published",
    text: str = TEXT_V1,
    spec: dict | None = None,
) -> int:
    """直插 doc 产物 + v1 版本行（不经 kind 创建面——与其他 agent 的工作解耦）。"""
    artifact = Artifact(
        customer_id=customer_id,
        kind="doc",
        title=title,
        status=status,
        version=1,
        status_history=[],
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            version=1,
            spec=spec if spec is not None else doc_envelope(title, text),
            operator="test",
        )
    )
    db_session.commit()
    return artifact.id


def append_version(db_session, artifact_id: int, *, version: int, title: str, text: str) -> None:
    """追加新版本行并推进产物版本指针（模拟「保存版本」后的版本链状态）。"""
    artifact = db_session.get(Artifact, artifact_id)
    assert artifact is not None
    artifact.version = version
    artifact.title = title
    db_session.add(
        ArtifactVersion(
            artifact_id=artifact_id,
            version=version,
            spec=doc_envelope(title, text),
            operator="test",
        )
    )
    db_session.commit()


def seed_other_artifact(db_session, *, kind: str, title: str) -> int:
    """直插非 doc 产物（跨 kind 令牌用例的对照件，v1 最小 spec 即可）。"""
    artifact = Artifact(
        customer_id="cust-001",
        kind=kind,
        title=title,
        status="published",
        version=1,
        status_history=[],
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            version=1,
            spec={"title": title, "spec": {"title": title, "panels": []}},
            operator="test",
        )
    )
    db_session.commit()
    return artifact.id


def _share(client, artifact_id: int, *, ttl_days: int | None = None) -> dict:
    json = None if ttl_days is None else {"ttl_days": ttl_days}
    resp = client.post(
        f"/api/v1/artifacts/{artifact_id}/share", headers=CUST, json=json
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _parse_link(url: str) -> tuple[int, int, str]:
    """拆 url 的 (version, exp, mac) 三段令牌（url 的 v 与令牌内版本一致）。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    assert set(query) == {"t", "v"}
    version_tok, exp, mac = query["t"][0].split(".")
    assert int(query["v"][0]) == int(version_tok)
    return int(version_tok), int(exp), mac


# ------------------------------------------------------------ 生成面（任务 5.1）


def test_doc_share_link_generated(client, db_session) -> None:
    """published doc → POST share 200：url 走 doc 路径，带 t=…&v=…，缺省 30
    天；令牌三段格式与 mac 白盒钉死（钉死生成时刻版本）。"""
    aid = seed_doc(db_session)
    link = _share(client, aid)

    assert link["version"] == 1
    assert link["url"].startswith(f"/api/v1/share/doc/{aid}?t=")
    assert f"&v={link['version']}" in link["url"]
    version_tok, exp, mac = _parse_link(link["url"])
    assert version_tok == 1
    expected = hmac.new(
        SHARE_SECRET.encode("utf-8"),
        f"{aid}|{version_tok}|{exp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(expected, mac)
    assert exp > 0 and link["expires_at"] and link["expires_at"].startswith("20")


def test_doc_share_requires_published(client, db_session) -> None:
    """draft 403 提示先发布；published 后归档再生成同样 403（既有状态语义）。"""
    draft_id = seed_doc(db_session, status="draft", title="草稿文档")
    rejected = client.post(f"/api/v1/artifacts/{draft_id}/share", headers=CUST)
    assert rejected.status_code == 403
    assert "published" in rejected.json()["detail"]

    published_id = seed_doc(db_session, title="已发布文档")
    archived = client.post(f"/api/v1/artifacts/{published_id}/archive", headers=CUST)
    assert archived.status_code == 200, archived.text
    rejected = client.post(f"/api/v1/artifacts/{published_id}/share", headers=CUST)
    assert rejected.status_code == 403
    assert "published" in rejected.json()["detail"]


def test_doc_in_share_kind_paths_and_gate_message(client, db_session) -> None:
    """接线零漂移：SHARE_KIND_PATHS 收录 doc；白名单外 kind 的 403 文案点名
    文档（doc）——kind 门文案随 kind 集合扩充（任务 5.1）。"""
    assert share_routes.SHARE_KIND_PATHS.get("doc") == "doc"

    alert_id = seed_other_artifact(db_session, kind="alert_rule", title="告警")
    rejected = client.post(f"/api/v1/artifacts/{alert_id}/share", headers=CUST)
    assert rejected.status_code == 403
    detail = rejected.json()["detail"]
    assert "文档" in detail and "doc" in detail and "dashboard" in detail


# ------------------------------------------------------------ 公开面（任务 5.1）


def test_doc_public_read_happy_path(client, db_session) -> None:
    """无鉴权打开：200 返回钉死版本 spec 信封原样 + title + expires_at；
    信封 title 空白时回退产物当前 title。"""
    aid = seed_doc(db_session)
    link = _share(client, aid)

    opened = client.get(link["url"])  # 无任何鉴权头
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert set(body) == {"title", "spec", "version", "expires_at"}
    assert body["title"] == TITLE
    assert body["spec"] == doc_envelope(TITLE, TEXT_V1)
    assert body["version"] == 1
    assert body["expires_at"] == link["expires_at"]

    # title 回退：该版本信封 title 空白 → 取产物当前 title（快照自洽兜底）
    fallback_id = seed_doc(
        db_session,
        title="兜底标题",
        spec={"title": "  ", "spec": {"format": "markdown", "text": "正文"}},
    )
    fallback_link = _share(client, fallback_id)
    assert client.get(fallback_link["url"]).json()["title"] == "兜底标题"


def test_doc_public_pinned_version(client, db_session) -> None:
    """钉死版本（tasks 5.3 改版不串版）：分享后再存新版，旧链接仍呈现 v1
    正文；重新生成钉死 v2。"""
    aid = seed_doc(db_session)
    old_link = _share(client, aid)

    append_version(
        db_session, aid, version=2, title=f"{TITLE}（修订）", text="# 修订版\n第二版正文"
    )

    old = client.get(old_link["url"])
    assert old.status_code == 200, old.text
    assert old.json()["version"] == 1
    assert old.json()["spec"] == doc_envelope(TITLE, TEXT_V1)
    assert old.json()["title"] == TITLE

    new_link = _share(client, aid)
    assert new_link["version"] == 2
    new = client.get(new_link["url"]).json()
    assert new["version"] == 2
    assert new["spec"] == doc_envelope(f"{TITLE}（修订）", "# 修订版\n第二版正文")


def test_doc_public_404_matrix(client, db_session) -> None:
    """统一 404 矩阵：缺 t/篡改/v 不一致/畸形/产物不存在/非 published（draft
    令牌）/跨 kind 令牌/归档断链——一律 404 且 detail 跨例逐字相等（不向
    未认证方泄露失败原因差异）。"""
    aid = seed_doc(db_session)
    link = _share(client, aid)
    base, _, query = link["url"].partition("?")
    token = query.split("t=")[1].split("&")[0]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

    # draft doc：生成面拒发链接，手工签令牌模拟「产物转为非 published 后
    # 旧令牌仍在手」的公开面兜底
    draft_id = seed_doc(db_session, status="draft", title="草稿")
    draft_token, _ = share_service.build_token(
        SHARE_SECRET, artifact_id=draft_id, version=1, ttl=3600
    )
    # 跨 kind 两态：dashboard 的合法令牌（签名对 dashboard 的 artifact_id 成立）
    # 打 doc 路径 → kind 复核 404；换产物重放（doc 令牌不等于 board id）→
    # 签名失配 404。两条路径同一 detail。
    board_id = seed_other_artifact(db_session, kind="dashboard", title="看板")
    board_token, _ = share_service.build_token(
        SHARE_SECRET, artifact_id=board_id, version=1, ttl=3600
    )

    cases = [
        base,  # 缺 t 缺 v
        f"{base}?t={tampered}&v=1",  # 篡改 mac
        f"{base}?t={token}&v=99",  # v 与令牌内版本不一致
        f"{base}?t=not-a-token&v=1",  # 结构畸形
        f"/api/v1/share/doc/99999?t={token}&v=1",  # 产物不存在
        f"{base}?t={draft_token}&v=1",  # 非 published（draft）
        f"/api/v1/share/doc/{board_id}?t={board_token}&v=1",  # kind 复核（dashboard）
        f"{base}?t={board_token}&v=1",  # 换产物重放（签名失配）
    ]
    details: list[str] = []
    for url in cases:
        resp = client.get(url)
        assert resp.status_code == 404, url
        details.append(resp.json()["detail"])

    # 归档即失效（tasks 5.3 归档撤销场景）
    assert client.get(link["url"]).status_code == 200
    client.post(f"/api/v1/artifacts/{aid}/archive", headers=CUST)
    archived = client.get(link["url"])
    assert archived.status_code == 404
    details.append(archived.json()["detail"])

    # 全部失败 detail 逐字相等（恒 "not found"）
    assert set(details) == {"not found"}


def test_doc_share_disabled_without_secret(client, db_session, monkeypatch) -> None:
    """FD_WIRE_SHARE_SECRET 未配置：生成与公开两端点 503 detail 带原因；
    配置回来同进程即刻恢复（惰性 env）。"""
    aid = seed_doc(db_session)
    monkeypatch.delenv("FD_WIRE_SHARE_SECRET", raising=False)

    create = client.post(f"/api/v1/artifacts/{aid}/share", headers=CUST)
    assert create.status_code == 503
    assert "FD_WIRE_SHARE_SECRET" in create.json()["detail"]

    opened = client.get(f"/api/v1/share/doc/{aid}?t=1.2.abc&v=1")
    assert opened.status_code == 503
    assert "FD_WIRE_SHARE_SECRET" in opened.json()["detail"]

    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", SHARE_SECRET)
    assert client.post(f"/api/v1/artifacts/{aid}/share", headers=CUST).status_code == 200


def test_doc_ttl_zero_long_lived(client, db_session, monkeypatch) -> None:
    """TTL 0 档长效：生成 expires_at=null、令牌中段 "0"；时间冻结到五十年后
    仍验签通过且公开打开 200（exp=0 是「无到期」哨兵，跳过过期校验）。"""
    aid = seed_doc(db_session)
    link = _share(client, aid, ttl_days=0)
    assert link["expires_at"] is None
    version_tok, exp, _mac = _parse_link(link["url"])
    assert (version_tok, exp) == (1, 0)

    # 只替换 share.service 模块内的 time 名（不碰全局 time 模块——避免
    # 影响同进程其他计时器）；verify_token 的过期判定是唯一被冻结的墙钟读
    far_future = int(time.time()) + 50 * 365 * 24 * 3600
    monkeypatch.setattr(
        share_service, "time", SimpleNamespace(time=lambda: far_future)
    )
    token = link["url"].split("t=")[1].split("&")[0]
    assert share_service.verify_token(
        SHARE_SECRET, artifact_id=aid, token=token, v_param=1
    ) == (1, 0)
    still_open = client.get(link["url"])
    assert still_open.status_code == 200, still_open.text
    assert still_open.json()["expires_at"] is None
    assert still_open.json()["spec"] == doc_envelope(TITLE, TEXT_V1)


# ------------------------------------------------------------ 展示面（任务 1.4）


def test_workbench_includes_doc(db_session) -> None:
    """artifacts_section 计数含 doc：by_kind 八键全给、doc 计入 active_total、
    archived 不计、最近列表含 doc 行。"""
    published_id = seed_doc(db_session)
    seed_doc(db_session, status="archived", title="归档文档")

    section = artifacts_section(db_session, customer_id="cust-001")
    assert "doc" in WORKBENCH_KINDS
    assert set(section.by_kind) == set(WORKBENCH_KINDS)
    assert section.by_kind["doc"] == 1  # archived 不计（活跃口径）
    assert section.active_total == sum(section.by_kind.values()) == 1
    assert [item.kind for item in section.recent] == ["doc"]
    assert [item.id for item in section.recent] == [published_id]

    # 第二份 published doc：计数与活跃总数同步 +1
    seed_doc(db_session, title="第二份文档")
    section2 = artifacts_section(db_session, customer_id="cust-001")
    assert section2.by_kind["doc"] == 2
    assert section2.active_total == 2
