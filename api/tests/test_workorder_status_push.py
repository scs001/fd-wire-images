"""推送式状态回流（tracked / status 端点）——fd-prod 海外出站挡死 raw
通道后的架构校正（2026-10-11 生产实测）：拉取器读本地工单文件回推，
wire 零出站。覆盖：tracked 清单口径、状态回推幂等、错误路径、
slug unverified 放行、客户面安全子集。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app
from fd_wire_api.demands import get_db
from fd_wire_api.demands.db import Base

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

from test_demands_workorder import (
    CUST,
    INTERNAL,
    at_in_progress,
    workorder,
)

def _ticket_path() -> str:
    """出单路径内嵌出单日 UTC 日期（DE-YYYYMMDD-01）——运行时取当日，免日期翻转 flake。"""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"reports/health-tickets/{today}-wire-DE-{today}-01.yaml"


def _lifecycle_yaml() -> str:
    return f"""ticket_id: {_ticket_path().rsplit("/", 1)[-1][: -len(".yaml")]}
kind: generate
unit: spiders/stats-gov-cn/
terminal: null
lifecycle:
- at: '2026-10-11T08:00:00+00:00'
  event: created
- at: '2026-10-11T09:00:00+00:00'
  event: submitted
  http_status: 200
  ok: true
- at: '2026-10-11T09:30:00+00:00'
  event: status
  state: done
  pr_url: 'https://github.com/FindDataTechnology/fd-industry-data/pull/999'
"""


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
    make_console_env(tmp_path, monkeypatch, customers=[("sub-cust-001", "cust-001")])
    with TestClient(app) as test_client:
        yield SimpleNamespace(client=test_client, Session=TestingSession)
    app.dependency_overrides.clear()


@pytest.fixture()
def slug_unverified(monkeypatch):
    """slug 探测不可达：unverified 如实放行（新三态契约）。"""
    from fd_wire_api.demands import orchestration

    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug, **kw: "unverified")


def _exported(harness) -> tuple[int, str]:
    did, _ = at_in_progress(harness.client)
    resp = workorder(harness.client, did)
    assert resp.status_code == 200, resp.text
    return did, resp.json()["ticket_path"]


def test_slug_unverified_passes_honestly(harness, slug_unverified):
    """出单时探测不可达 → 放行且 slug_check=unverified（不假装已验证）。"""
    did, path = _exported(harness)
    assert path == _ticket_path()
    # 重新出单再看响应字段
    resp = workorder(harness.client, did)
    assert resp.status_code == 200
    assert resp.json()["slug_check"] == "unverified"


def test_tracked_lists_latest_export(harness, slug_unverified):
    """tracked=最新导出路径（含已 ack）；未出单单不出现。"""
    did, path = _exported(harness)
    resp = harness.client.get(
        "/internal/demands/workorders/tracked", headers=INTERNAL
    )
    assert resp.status_code == 200
    assert resp.json()["tracked"] == [path]
    # ack 之后仍在 tracked（状态演化发生在投递之后）
    ack = harness.client.post(
        "/internal/demands/workorders/ack",
        json={"ticket_path": path},
        headers=INTERNAL,
    )
    assert ack.status_code == 200
    resp = harness.client.get(
        "/internal/demands/workorders/tracked", headers=INTERNAL
    )
    assert resp.json()["tracked"] == [path]


def test_status_push_idempotent_and_customer_safe(harness, slug_unverified):
    """回推：事件落库幂等；客户面只见安全子集。"""
    did, path = _exported(harness)
    push = lambda: harness.client.post(
        "/internal/demands/workorders/status",
        json={"ticket_path": path, "content": _lifecycle_yaml()},
        headers=INTERNAL,
    )
    first = push()
    assert first.status_code == 200, first.text
    assert first.json()["events_appended"] == 2  # submitted + pr_open
    second = push()
    assert second.status_code == 200
    assert second.json()["events_appended"] == 0  # 幂等：重复推送零追加

    detail = harness.client.get(f"/api/v1/demands/{did}", headers=CUST).json()
    stages = [e["stage"] for e in detail.get("orchestration") or []]
    assert stages == ["workorder_submitted", "pr_open"]
    pr_urls = [e.get("pr_url") for e in detail["orchestration"]]
    assert pr_urls[1] == "https://github.com/FindDataTechnology/fd-industry-data/pull/999"
    body_text = str(detail)
    assert "ticket_yaml" not in body_text and "http_status" not in body_text


def test_status_push_error_paths(harness, slug_unverified):
    """未知路径 404；YAML 坏 400 如实回绝；无令牌 401。"""
    _exported(harness)
    resp = harness.client.post(
        "/internal/demands/workorders/status",
        json={"ticket_path": "reports/health-tickets/20990101-wire-DE-20990101-99.yaml",
              "content": "kind: generate"},
        headers=INTERNAL,
    )
    assert resp.status_code == 404
    did, path = _exported(harness)
    bad = harness.client.post(
        "/internal/demands/workorders/status",
        json={"ticket_path": path, "content": "!!!not yaml [[["},
        headers=INTERNAL,
    )
    assert bad.status_code == 400
    noauth = harness.client.get("/internal/demands/workorders/tracked")
    assert noauth.status_code == 401
