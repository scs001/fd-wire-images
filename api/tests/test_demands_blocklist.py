"""禁爬源清单测试（wire-crawl-source-blocklist）。

覆盖 spec「禁爬源 SHALL 在提交口硬拒」与「出单闸 SHALL 复检禁爬源」：
- 匹配矩阵：淘宝/拼多多系（含子域/大小写/端口）命中；伪装域
  （taobao.com.evil.example）不命中；正常公开源不命中；
- 提交口：含禁爬源的意向单 422，错误 loc 带索引；
- 出单闸：存量单（直写入库绕过提交口）带禁爬源 → 409 且零留档。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]
from fd_wire_api.demands import get_db
from fd_wire_api.demands.db import Base
from fd_wire_api.demands.models import Demand
from fd_wire_api.demands.orchestration import (
    WorkorderExportError,
    export_workorder_service,
)
from fd_wire_api.demands.source_policy import (
    BLOCKED_SOURCE_DOMAINS,
    blocked_source_domain,
)

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-cust-001"
CUST = bearer(mint_id_token(SUB_CUST))


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(tmp_path, monkeypatch, customers=[(SUB_CUST, "cust-001")])
    with TestClient(app) as test_client:
        yield SimpleNamespace(client=test_client, Session=TestingSession)
    app.dependency_overrides.clear()


@pytest.fixture()
def client(harness):
    return harness.client


# ---------------------------------------------------------------- 匹配矩阵


@pytest.mark.parametrize(
    "url,expected",
    [
        # 淘宝系
        ("https://item.taobao.com/item.htm?id=1", "taobao.com"),
        ("https://www.tmall.com/", "tmall.com"),
        ("https://detail.1688.com/offer/1.html", "1688.com"),
        ("https://www.alibaba.com/product-detail/x", "alibaba.com"),
        ("https://render.alipay.com/p/yuyan/1", "alipay.com"),
        # 拼多多系
        ("https://mobile.yangkeduo.com/goods.html?gid=1", "yangkeduo.com"),
        ("https://www.pinduoduo.com/", "pinduoduo.com"),
        ("https://img.pddpic.com/a.jpg", "pddpic.com"),
        ("https://www.duoduoyoucai.com/", "duoduoyoucai.com"),
        # 大小写 / 端口
        ("https://ITEM.TAOBAO.COM/x", "taobao.com"),
        ("https://item.taobao.com:443/x", "taobao.com"),
        # 伪装域：前缀形不是子域，不命中
        ("https://taobao.com.evil.example/x", None),
        ("https://nottaobao.com/x", None),
        # 正常公开源
        ("https://www.stats.gov.cn/sj/zxfb/", None),
        ("https://data.stats.gov.cn/easyquery.htm", None),
    ],
)
def test_blocked_source_domain_matrix(url: str, expected: str | None) -> None:
    assert blocked_source_domain(url) == expected


def test_blocklist_covers_both_platforms() -> None:
    """首版清单含淘宝系五域+拼多多系四域（防误删/误改回归）。"""
    assert set(BLOCKED_SOURCE_DOMAINS) == {
        "taobao.com",
        "tmall.com",
        "alibaba.com",
        "1688.com",
        "alipay.com",
        "pinduoduo.com",
        "yangkeduo.com",
        "pddpic.com",
        "duoduoyoucai.com",
    }


# ---------------------------------------------------------------- 提交口


def test_submit_taobao_source_422(client) -> None:
    resp = client.post(
        "/api/v1/demands",
        json={
            "description": "抓淘宝商品价格",
            "contact_email": "a@b.com",
            "source_urls": ["https://item.taobao.com/item.htm?id=1"],
            "public_data_asserted": True,
        },
        headers=CUST,
    )
    assert resp.status_code == 422
    detail = resp.text
    assert "taobao.com" in detail
    assert "不在代爬服务范围" in detail


def test_submit_pinduoduo_source_422(client) -> None:
    resp = client.post(
        "/api/v1/demands",
        json={
            "description": "抓拼多多商品",
            "contact_email": "a@b.com",
            "source_urls": ["https://mobile.yangkeduo.com/goods.html?gid=1"],
            "public_data_asserted": True,
        },
        headers=CUST,
    )
    assert resp.status_code == 422
    assert "yangkeduo.com" in resp.text


def test_submit_mixed_sources_second_blocked(client) -> None:
    """混单：第一条合法、第二条禁爬 → 整单 422 且错误定位到索引 1。"""
    resp = client.post(
        "/api/v1/demands",
        json={
            "description": "混单",
            "contact_email": "a@b.com",
            "source_urls": [
                "https://www.stats.gov.cn/sj/zxfb/",
                "https://www.pinduoduo.com/",
            ],
            "public_data_asserted": True,
        },
        headers=CUST,
    )
    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any(
        err.get("loc", [])[-1] == 1 for err in errors if isinstance(err, dict)
    ), errors


def test_submit_normal_sources_pass(client) -> None:
    resp = client.post(
        "/api/v1/demands",
        json={
            "description": "正常公开源",
            "contact_email": "a@b.com",
            "source_urls": ["https://www.stats.gov.cn/sj/zxfb/"],
            "public_data_asserted": True,
        },
        headers=CUST,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------- 出单闸


def _seed_in_progress(Session, source_urls: list[str]) -> int:
    """直写入库一张 in_progress 单（绕过提交口，模拟存量/直接入库）。"""
    db = Session()
    try:
        demand = Demand(
            code="DE-20261012-99",
            customer_id="cust-001",
            title="存量单",
            intent="测试",
            contact_email="a@b.com",
            status="in_progress",
            source_urls=source_urls,
            public_data_asserted=True,
        )
        db.add(demand)
        db.commit()
        db.refresh(demand)
        return demand.id
    finally:
        db.close()


def test_export_blocked_source_409_zero_side_effect(harness) -> None:
    demand_id = _seed_in_progress(harness.Session, ["https://www.pinduoduo.com/"])
    db = harness.Session()
    try:
        demand = db.get(Demand, demand_id)
        with pytest.raises(WorkorderExportError) as exc_info:
            export_workorder_service(db, demand)
        assert exc_info.value.status_code == 409
        assert "pinduoduo.com" in str(exc_info.value)
        db.refresh(demand)
        assert (demand.pipeline_events or []) == []  # 零留档
    finally:
        db.close()


def test_export_normal_source_unaffected(harness, monkeypatch) -> None:
    """合规单出单路径零回归（slug 探测 stub 成 free，避免真实网络）。"""
    from fd_wire_api.demands import orchestration

    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug: "free")
    demand_id = _seed_in_progress(
        harness.Session, ["https://www.stats.gov.cn/sj/zxfb/"]
    )
    db = harness.Session()
    try:
        demand = db.get(Demand, demand_id)
        result = export_workorder_service(db, demand)
        assert result["queued_for_pull"] is True
        db.refresh(demand)
        types = [e.get("type") for e in (demand.pipeline_events or [])]
        assert "workorder_exported" in types
    finally:
        db.close()
