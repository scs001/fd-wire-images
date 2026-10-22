"""意向单合规门测试（wire-demand-generation-orchestration 任务 1.2+1.3）。

覆盖 spec「意向单表单 SHALL 轻量收集」的三条硬拒场景 + 合法路径 + 存量
兼容——
- 源地址形态硬校验：非 http(s) / 内嵌账密（user:pass@host）逐条 422，
  错误 loc 带索引定位（errors[].loc 含 (body, source_urls, i)）；
- 公开数据声明门：False / 缺失即 422（文案提示非公开数据请线下沟通）；
- 合法提交：两字段落库可读回（客户视图 + 内部视图）；
- 存量兼容：旧单（source_urls=None / public_data_asserted=None）读路径
  零回归（None 语义=旧单）。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

# 在导入 app 前把默认引擎指向内存库（与 test_demands_statemachine 同款纪律；
# 只在导入期间生效，导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
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

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
SUB_CUST = "sub-cust-001"
CUST = bearer(mint_id_token(SUB_CUST))

VALID_SOURCES = [
    "https://www.stats.gov.cn/sj/zxfb/",
    "http://www.caam.org.cn/chn/4/cate_30/list_1.html",
]


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """内存库 + 覆盖 get_db 的 TestClient + 直写会话（存量旧单种子用）。"""
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


# ------------------------------------------------------------------ helpers


def payload(**overrides) -> dict:
    base = {
        "description": "需要上海市 2024 年以来乘用车月度销量与价格带分布，用于市场分析",
        "contact_email": "buyer@example.com",
        "source_urls": list(VALID_SOURCES),
        "public_data_asserted": True,
    }
    base.update(overrides)
    return base


def submit(client, **overrides):
    return client.post("/api/v1/demands", json=payload(**overrides), headers=CUST)


def source_url_error_indices(body: dict) -> set[int]:
    """422 响应里定位到 source_urls 元素级的错误索引集合。"""
    return {
        err["loc"][-1]
        for err in body.get("detail", [])
        if "source_urls" in err["loc"]
    }


def demand_count(client) -> int:
    resp = client.get("/api/v1/demands", headers=CUST)
    assert resp.status_code == 200, resp.text
    return len(resp.json())


# ------------------------------------------------- 源地址形态硬校验（422）


def test_rejects_non_http_source_urls_with_index(client):
    """非 http(s) 地址逐条 422：错误 loc 带 (body, source_urls, i) 索引定位，
    合法条目不误伤，且不产生需求单。"""
    resp = submit(
        client,
        source_urls=[
            "ftp://data.example.com/sales.csv",   # 非 http(s) 协议
            VALID_SOURCES[0],                      # 合法条目夹在中间
            "example.com/data",                    # 裸域名无协议
        ],
    )
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    indices = source_url_error_indices(resp.json())
    assert indices == {0, 2}  # 逐条索引定位；合法的第 1 条不出现
    for err in errors:
        if "source_urls" in err["loc"]:
            assert err["loc"][:2] == ["body", "source_urls"]  # (body, source_urls, i)
            assert "http" in err["msg"]
    assert demand_count(client) == 0  # 不产生需求单


def test_rejects_embedded_credentials_in_source_urls(client):
    """内嵌账密（user:pass@host）即拒——工单面只承接公开数据。"""
    resp = submit(
        client,
        source_urls=[
            VALID_SOURCES[0],
            "https://buyer:secret@api.example.com/v1/sales",
        ],
    )
    assert resp.status_code == 422, resp.text
    assert source_url_error_indices(resp.json()) == {1}
    msg = [
        err["msg"] for err in resp.json()["detail"] if "source_urls" in err["loc"]
    ][0]
    assert "账号密码" in msg
    assert demand_count(client) == 0


def test_rejects_empty_and_blank_source_urls(client):
    """空列表（min_length=1）与全空白条目都过不了合规门。"""
    resp = submit(client, source_urls=[])
    assert resp.status_code == 422, resp.text
    assert any("source_urls" in err["loc"] for err in resp.json()["detail"])

    resp = submit(client, source_urls=["   "])
    assert resp.status_code == 422, resp.text
    assert source_url_error_indices(resp.json()) == {0}


def test_scheme_check_is_case_insensitive_but_strict_on_prefix(client):
    """HTTP:// 大写协议放行（大小写不敏感）；js: 等伪协议照样拒。"""
    resp = submit(client, source_urls=["HTTP://stats.example.com/pub"])
    assert resp.status_code == 201, resp.text

    resp = submit(client, source_urls=["javascript://example.com/x"])
    assert resp.status_code == 422, resp.text


# ------------------------------------------------- 公开数据声明门（422）


def test_rejects_false_public_data_assertion(client):
    """声明未勾选（false）即拒：文案提示非公开数据请线下沟通。"""
    resp = submit(client, public_data_asserted=False)
    assert resp.status_code == 422, resp.text
    errors = [
        err for err in resp.json()["detail"] if "public_data_asserted" in err["loc"]
    ]
    assert errors, resp.text
    declaration_error = errors[0]
    assert declaration_error["loc"] == ["body", "public_data_asserted"]
    assert "线下沟通" in declaration_error["msg"]
    assert demand_count(client) == 0  # 不产生需求单


def test_rejects_missing_public_data_assertion(client):
    """声明缺失（字段不传）即拒：必填字段 422。"""
    body = payload()
    del body["public_data_asserted"]
    resp = client.post("/api/v1/demands", json=body, headers=CUST)
    assert resp.status_code == 422, resp.text
    assert any(
        err["loc"] == ["body", "public_data_asserted"]
        for err in resp.json()["detail"]
    )
    assert demand_count(client) == 0


# ------------------------------------------------- 合法提交：落库可读回


def test_valid_submission_persists_and_reads_back(client):
    """合法提交：201 + 两字段落库；客户详情/列表/内部视图读回一致。"""
    resp = submit(client)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    did = created["id"]
    assert created["source_urls"] == VALID_SOURCES   # 过门后原样存储
    assert created["public_data_asserted"] is True

    detail = client.get(f"/api/v1/demands/{did}", headers=CUST).json()
    assert detail["source_urls"] == VALID_SOURCES
    assert detail["public_data_asserted"] is True

    listed = client.get("/api/v1/demands", headers=CUST).json()
    assert len(listed) == 1
    assert listed[0]["source_urls"] == VALID_SOURCES

    internal = client.get(f"/internal/demands/{did}", headers=INTERNAL).json()
    assert internal["source_urls"] == VALID_SOURCES
    assert internal["public_data_asserted"] is True


# ------------------------------------------------- 存量兼容：旧单读路径


def test_legacy_demand_without_compliance_fields_reads_cleanly(harness):
    """存量单（两字段 None）一切读路径零回归：详情/列表 200，None 语义保留。"""
    with harness.Session() as db:
        legacy = Demand(
            code="DE-19990101-01",
            customer_id="cust-001",
            title="存量旧单（合规门前）",
            intent="意向单旧表单提交的单子",
            status="in_progress",
            status_history=[],
        )
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        legacy_id = legacy.id

    client = harness.client
    detail = client.get(f"/api/v1/demands/{legacy_id}", headers=CUST)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["source_urls"] is None
    assert body["public_data_asserted"] is None

    listed = client.get("/api/v1/demands", headers=CUST).json()
    assert len(listed) == 1
    assert listed[0]["source_urls"] is None

    internal = client.get(
        f"/internal/demands/{legacy_id}", headers=INTERNAL
    )
    assert internal.status_code == 200, internal.text
    assert internal.json()["public_data_asserted"] is None
