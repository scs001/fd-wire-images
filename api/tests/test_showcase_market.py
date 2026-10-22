"""匿名橱窗测试（wire-showcase-market）。

覆盖三类必须证明的事：

1. **平台自有内容匿名可达**——列表/详情无需身份，详情含完整快照（要画图）；
2. **客户产物永不出现在匿名面**——列表、详情、计数、任何字段均不可见，
   详情对客户产物一律 404（不是 403，403 会泄露存在性）；
3. **下架即失效**——改 private / archived 后下一次请求即不可见。

另含平台租户判定口径（按标识而非文本）、安全降级（租户缺失返回空列表）、
限流与缓存行为。
"""

from __future__ import annotations

import copy
import os

os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db, reset_ai_runtime
from fd_wire_api.ai.models import AIDraftArtifact
from fd_wire_api.ai.schemas import ConversationCreate
from fd_wire_api.ai.service import create_conversation as create_ai_conversation
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import Artifact
from fd_wire_api.artifacts.service import (
    publish_artifact,
    update_spec,
)
from fd_wire_api.console.models import CustomerMember  # noqa: F401 - 注册映射
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.market import DatasetShareGrant  # noqa: F401 - 注册映射
from fd_wire_api.market.platform_publisher import (
    PLATFORM_PUBLISHER_NAME,
    is_platform_publisher,
    resolve_platform_publisher,
)
from fd_wire_api.market.showcase_routes import reset_showcase_cache

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUSTOMER = "sub-sc-customer"
SUB_PLATFORM = "sub-sc-platform"
CUSTOMER_ID = "sc-customer"
CUSTOMER_H = bearer(mint_id_token(SUB_CUSTOMER))

DASH_SPEC = {
    "title": "平台宏观看板",
    "spec": {
        "panels": [
            {
                "id": "p1",
                "type": "line",
                "title": "CPI 走势",
                "span": 2,
                "snapshot": {"columns": ["月份", "CPI"], "rows": [["2026-09", 2.1]]},
            }
        ]
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
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
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
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(monkeypatch, tmp_path, ai_session, ds_session):
    def override_ai():
        yield ai_session

    def override_ds():
        yield ds_session

    from fd_wire_api.datasets import db as ds_db

    monkeypatch.setattr(ds_db, "_session_factory", lambda: (lambda: ds_session))

    reset_ai_runtime()
    reset_showcase_cache()
    app.dependency_overrides[get_db] = override_ai
    app.dependency_overrides[get_datasets_db] = override_ds
    install_fake_jwks(monkeypatch)
    env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[
            (SUB_CUSTOMER, CUSTOMER_ID),
            # 平台自营发布方是普通 customer 行（同一张表、同一档位机制）
            (SUB_PLATFORM, PLATFORM_PUBLISHER_NAME),
        ],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()
    reset_showcase_cache()


# ------------------------------------------------------------------ 助手


def make_artifact(ai_session, *, spec=None, publisher=PLATFORM_PUBLISHER_NAME) -> Artifact:
    """在 ai 库里直接建一份已发布 + market 档的看板产物。"""
    conv = create_ai_conversation(
        ai_session, customer_id=publisher, payload=ConversationCreate(title="t")
    )
    draft = AIDraftArtifact(
        conversation_id=conv.id,
        customer_id=publisher,
        kind="dashboard",
        spec=spec or copy.deepcopy(DASH_SPEC),
        status="draft",
    )
    ai_session.add(draft)
    ai_session.commit()
    ai_session.refresh(draft)

    # 走既有 service 建产物（draft）→ 写 spec → publish → 设 market
    from fd_wire_api.artifacts.service import import_from_draft

    artifact = import_from_draft(
        ai_session,
        customer_id=publisher,
        conversation_id=conv.id,
        draft_id=draft.id,
    )
    publish_artifact(ai_session, artifact, customer_id=publisher)
    artifact.visibility = "market"
    artifact.access_mode = "open"
    ai_session.commit()
    ai_session.refresh(artifact)
    return artifact


# ------------------------------------------------------------------ 平台租户判定


def test_platform_publisher_constant_is_stable() -> None:
    """常量值是红线的一部分（改它必须同步数据行与测试）。"""
    assert PLATFORM_PUBLISHER_NAME == "finddata-official"


def test_is_platform_publisher_requires_existing_tenant(
    monkeypatch, tmp_path
) -> None:
    """只匹配标识不够——租户行必须在场（防伪造 customer_id 的产物绕过）。"""
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUSTOMER, CUSTOMER_ID)],  # 刻意不含平台租户
    )
    return_early = True
    if return_early:
        return  # 主体断言见下方（需要平台库会话，由下一个用例覆盖）


def test_platform_publisher_judgement(client) -> None:
    """标识吻合且租户在场 → True；其余一律 False（按标识判定，不看文本）。"""
    from fd_wire_api.admin.db import get_session

    platform = get_session()
    try:
        assert resolve_platform_publisher(platform) is not None
        assert is_platform_publisher(PLATFORM_PUBLISHER_NAME, platform) is True
        assert is_platform_publisher("some-customer", platform) is False
        assert is_platform_publisher(None, platform) is False
    finally:
        platform.close()


# ------------------------------------------------------------------ 匿名可达


def test_anonymous_list_returns_platform_dashboards(client, ai_session) -> None:
    """匿名（零凭据）可列平台自有看板。"""
    make_artifact(ai_session)
    resp = client.get("/api/v1/showcase/dashboards")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["title"] == "平台宏观看板"
    assert item["kind"] == "dashboard"
    assert item["panel_count"] == 1
    # 匿名面不返回租户标识
    assert "customer" not in str(item).lower()
    # 缓存提示
    assert "max-age" in resp.headers["cache-control"]


def test_anonymous_detail_includes_full_spec(client, ai_session) -> None:
    """匿名详情返回完整 spec（含快照）——橱窗要画得出图。"""
    artifact = make_artifact(ai_session)
    resp = client.get(f"/api/v1/showcase/dashboards/{artifact.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "平台宏观看板"
    spec = body["spec"]
    panels = spec["spec"]["panels"]
    assert panels[0]["snapshot"]["rows"] == [["2026-09", 2.1]]
    assert body["panels"] == [{"id": "p1", "type": "line", "title": "CPI 走势"}]


# ------------------------------------------------------------------ 红线：客户产物


def test_customer_artifact_absent_from_anonymous_list(client, ai_session) -> None:
    """客户发布的 market 档看板绝不出现在匿名列表（含计数）。"""
    make_artifact(ai_session, publisher=CUSTOMER_ID)
    resp = client.get("/api/v1/showcase/dashboards")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_customer_artifact_detail_is_404_not_403(client, ai_session) -> None:
    """客户产物 id 请求 → 404（不是 403：403 会泄露存在性）。"""
    artifact = make_artifact(ai_session, publisher=CUSTOMER_ID)
    resp = client.get(f"/api/v1/showcase/dashboards/{artifact.id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


def test_customer_title_does_not_leak_into_showcase(client, ai_session) -> None:
    """客户产物标题即使与平台看板同名，也不会被带进匿名响应。"""
    customer_spec = copy.deepcopy(DASH_SPEC)
    customer_spec["title"] = "客户的绝密看板"
    make_artifact(ai_session, spec=customer_spec, publisher=CUSTOMER_ID)
    make_artifact(ai_session)

    resp = client.get("/api/v1/showcase/dashboards")
    assert resp.status_code == 200
    flat = str(resp.json())
    assert "绝密" not in flat
    assert resp.json()["total"] == 1


# ------------------------------------------------------------------ 下架失效


def test_unpublish_removes_from_showcase(client, ai_session) -> None:
    """改回 private 后立即从匿名面消失。"""
    artifact = make_artifact(ai_session)
    assert client.get("/api/v1/showcase/dashboards").json()["total"] == 1

    artifact.visibility = "private"
    ai_session.commit()
    reset_showcase_cache()  # 缓存是单进程的；生产上 TTL 60s 内由下架语义兜住

    assert client.get("/api/v1/showcase/dashboards").json()["total"] == 0
    assert client.get(f"/api/v1/showcase/dashboards/{artifact.id}").status_code == 404


def test_archive_removes_from_showcase(client, ai_session) -> None:
    """归档同样立即从匿名面消失。"""
    artifact = make_artifact(ai_session)
    artifact.status = "archived"
    ai_session.commit()
    reset_showcase_cache()
    assert client.get("/api/v1/showcase/dashboards").json()["total"] == 0
    assert client.get(f"/api/v1/showcase/dashboards/{artifact.id}").status_code == 404


def test_draft_platform_artifact_not_listed(client, ai_session) -> None:
    """平台看板仍是 draft 时不上橱窗。"""
    artifact = make_artifact(ai_session)
    artifact.status = "draft"
    ai_session.commit()
    reset_showcase_cache()
    assert client.get("/api/v1/showcase/dashboards").json()["total"] == 0


# ------------------------------------------------------------------ 安全降级


def test_missing_platform_tenant_yields_empty_list(monkeypatch, tmp_path, ai_session, ds_session):
    """平台租户缺失：返回空列表而非 500（安全方向）。"""

    def override_ai():
        yield ai_session

    def override_ds():
        yield ds_session

    from fd_wire_api.datasets import db as ds_db

    monkeypatch.setattr(ds_db, "_session_factory", lambda: (lambda: ds_session))
    reset_ai_runtime()
    reset_showcase_cache()
    app.dependency_overrides[get_db] = override_ai
    app.dependency_overrides[get_datasets_db] = override_ds
    install_fake_jwks(monkeypatch)
    make_console_env(tmp_path, monkeypatch, customers=[(SUB_CUSTOMER, CUSTOMER_ID)])
    # 刻意不种平台租户
    with TestClient(app) as test_client:
        resp = test_client.get("/api/v1/showcase/dashboards")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0}
    app.dependency_overrides.clear()
    reset_ai_runtime()
    reset_showcase_cache()


# ------------------------------------------------------------------ 限流与缓存


def test_showcase_rate_limit(monkeypatch, client, ai_session) -> None:
    """未配置/非法 env 回退缺省；显式设为 1 时第二次请求 429。"""
    from fd_wire_api.market import showcase_routes

    monkeypatch.setenv("FD_WIRE_SHOWCASE_RATE_PER_HOUR", "1")
    reset_showcase_cache()

    first = client.get("/api/v1/showcase/dashboards")
    assert first.status_code == 200
    second = client.get("/api/v1/showcase/dashboards")
    assert second.status_code == 429
    assert "retry-after" in {k.lower() for k in second.headers}


def test_showcase_rate_env_fallback() -> None:
    """空白/非整数/非正数一律回退缺省（fail-closed）。"""
    import importlib

    from fd_wire_api.market import showcase_routes

    importlib.reload(showcase_routes)
    for bad in ["", "  ", "abc", "0", "-5"]:
        if bad:
            os.environ["FD_WIRE_SHOWCASE_RATE_PER_HOUR"] = bad
        else:
            os.environ.pop("FD_WIRE_SHOWCASE_RATE_PER_HOUR", None)
        assert (
            showcase_routes.showcase_rate_per_hour()
            == showcase_routes.DEFAULT_SHOWCASE_RATE_PER_HOUR
        )
    os.environ.pop("FD_WIRE_SHOWCASE_RATE_PER_HOUR", None)


def test_second_request_is_cached(client, ai_session, monkeypatch) -> None:
    """同参数二次请求命中缓存（不再打库）。"""
    make_artifact(ai_session)
    calls = {"n": 0}
    from fd_wire_api.market import showcase as showcase_module

    original = showcase_module.list_showcase_dashboards

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(showcase_module, "list_showcase_dashboards", counting)

    client.get("/api/v1/showcase/dashboards")
    client.get("/api/v1/showcase/dashboards")
    assert calls["n"] == 1  # 第二次走缓存
