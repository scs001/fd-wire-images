"""萬星直连调度测试（wire-wanxing-direct-dispatch 任务 2）。

覆盖 spec demand-orchestration 三条 Requirement：
- 出单闸复用：非 in_progress 409 / 无 source_urls 422 / 禁爬源 409 /
  slug occupied 409——与「转生成工单」同一拒绝口径，零事件留档；
- 未配置如实拒：工厂出口 None（键缺席）→ 503、零留档；
- 成功路径：响应形状逐字段（含 a2a 四键、delivered=true、
  queued_for_pull=false）、``wanxing_dispatched`` 事件留档、**不含**
  ``workorder_exported``（拉取队列零收录）、状态不迁移；
- 幂等键稳定：同一单同一工单内容连发两次 → 两键相同且
  = ``wire-{code}-{sha256[:16]}``（created_at 冻结证内容同源）；
- 失败降级：502、detail(dict) 含工单全文与 error 定位、事件追加
  ``wanxing_dispatch_failed`` 且无 dispatched；
- 双闸：内部端点缺 X-Internal-Token 401；admin 端点 happy path 200。

客户端一律 ``set_wanxing_client_factory`` 注入假件（零网络零凭据），
slug 探测 monkeypatch 成 free（零网络）。
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace

# 在导入 app 前把默认引擎指向内存库（与 test_demands_workorder 同款纪律；
# 只在导入期间生效，导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]
from fd_wire_api.demands import get_db
from fd_wire_api.demands import orchestration
from fd_wire_api.demands.db import Base
from fd_wire_api.demands.models import Demand
from fd_wire_api.demands.wanxing import (
    WANXING_CALLER_KEY_ENV,
    WanxingInvokeError,
    reset_wanxing_client_factory,
    set_wanxing_client_factory,
)

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token
from test_admin_api import AUTH, admin_env, seed_console_customers, submit_demand

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
SUB_CUST = "sub-cust-001"
CUST = bearer(mint_id_token(SUB_CUST))

VALID_SOURCE = "https://www.stats.gov.cn/sj/zxfb/"


# ------------------------------------------------------------------ 假客户端


class FakeWanxingClient:
    """记录每次 send_message 入参；按预设回执成功或抛 WanxingInvokeError。"""

    def __init__(self, *, receipt: dict | None = None, error: Exception | None = None):
        self.sent: list[dict] = []
        self._receipt = receipt or {
            "http_status": 200,
            "message_id": "m-1",
            "reply_text": "收到",
        }
        self._error = error

    def send_message(self, text, *, idempotency_key, rpc_id=None) -> dict:
        self.sent.append(
            {"text": text, "idempotency_key": idempotency_key, "rpc_id": rpc_id}
        )
        if self._error is not None:
            raise self._error
        return dict(self._receipt)


@pytest.fixture(autouse=True)
def _clean_wanxing_factory(monkeypatch):
    """每用例从干净出口出发：工厂清零、调用键 env 清空（未配置口径）。"""
    reset_wanxing_client_factory()
    monkeypatch.delenv(WANXING_CALLER_KEY_ENV, raising=False)
    yield
    reset_wanxing_client_factory()


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """内存库 + 覆盖 get_db 的 TestClient + 直写会话（存量单种子用）。"""
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
def slug_free(monkeypatch):
    """slug 探测钉 free：网络面隔离（三态探测语义另有专测）。"""
    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug, **kw: "free")


@pytest.fixture()
def fake_client():
    """注入成功假客户端；用例内随时可改其回执/错误。"""
    client = FakeWanxingClient()
    set_wanxing_client_factory(lambda: client)
    return client


# ------------------------------------------------------------------ helpers


def submit(client, **overrides) -> dict:
    payload = {
        "description": "需要全国汽车销量月度数据，用于市场分析",
        "contact_email": "buyer@example.com",
        "company": "上海某某咨询",
        "source_urls": [VALID_SOURCE],
        "public_data_asserted": True,
    }
    payload.update(overrides)
    resp = client.post("/api/v1/demands", json=payload, headers=CUST)
    assert resp.status_code == 201, resp.text
    return resp.json()


def transition(client, demand_id: int, target: str, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/transition",
        json={"target_status": target, **extra},
        headers=INTERNAL,
    )


def at_in_progress(client) -> tuple[int, dict]:
    d = submit(client)
    did = d["id"]
    resp = transition(client, did, "in_progress", by="ops-001", note="派发台接单")
    assert resp.status_code == 200, resp.text
    return did, d


def dispatch(client, demand_id: int, **body):
    return client.post(
        f"/api/v1/demands/{demand_id}/wanxing-dispatch",
        json={**body} if body else {},
        headers=INTERNAL,
    )


def internal_detail(client, demand_id: int) -> dict:
    resp = client.get(f"/internal/demands/{demand_id}", headers=INTERNAL)
    assert resp.status_code == 200, resp.text
    return resp.json()


def pending(client) -> list[dict]:
    resp = client.get("/internal/demands/workorders/pending", headers=INTERNAL)
    assert resp.status_code == 200, resp.text
    return resp.json()["pending"]


def seed_in_progress(Session, *, source_urls, code="DE-20261012-77") -> int:
    """直写一张 in_progress 单（绕过提交口，模拟存量/直接入库）。"""
    db = Session()
    try:
        demand = Demand(
            code=code,
            customer_id="cust-001",
            title="存量单",
            intent="测试",
            contact_email="a@b.com",
            status="in_progress",
            source_urls=list(source_urls),
            public_data_asserted=True,
        )
        db.add(demand)
        db.commit()
        db.refresh(demand)
        return demand.id
    finally:
        db.close()


# ------------------------------------------------------------------ 出单闸复用


def test_dispatch_rejects_non_in_progress_409_zero_events(harness, fake_client, slug_free):
    client = harness.client
    did, _ = at_in_progress(client)
    assert transition(client, did, "delivered", by="ops-001").status_code == 200

    resp = dispatch(client, did)
    assert resp.status_code == 409, resp.text
    assert "in_progress" in resp.json()["detail"]
    assert internal_detail(client, did)["pipeline_events"] == []  # 零事件
    assert fake_client.sent == []  # 闸未过——绝不触达萬星


def test_dispatch_no_source_urls_422(harness, fake_client, slug_free):
    client = harness.client
    did, _ = at_in_progress(client)
    with harness.Session() as s:  # 存量旧单形态：source_urls=None
        row = s.get(Demand, did)
        row.source_urls = None
        s.commit()
    resp = dispatch(client, did)
    assert resp.status_code == 422, resp.text
    assert "source_urls" in resp.json()["detail"]
    assert internal_detail(client, did)["pipeline_events"] == []
    assert fake_client.sent == []


def test_dispatch_blocked_source_409(harness, fake_client, slug_free):
    """命中禁爬源（taobao 域，直写入库绕过提交口）→ 出单闸最后一道拒。"""
    did = seed_in_progress(
        harness.Session, source_urls=["https://item.taobao.com/item.htm?id=1"]
    )
    resp = dispatch(harness.client, did)
    assert resp.status_code == 409, resp.text
    assert "taobao.com" in resp.json()["detail"]
    assert internal_detail(harness.client, did)["pipeline_events"] == []
    assert fake_client.sent == []


def test_dispatch_slug_occupied_409(harness, fake_client, monkeypatch):
    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug, **kw: "occupied")
    did, _ = at_in_progress(harness.client)
    resp = dispatch(harness.client, did)
    assert resp.status_code == 409, resp.text
    assert "转人工" in resp.json()["detail"]
    assert internal_detail(harness.client, did)["pipeline_events"] == []
    assert fake_client.sent == []


# ------------------------------------------------------------------ 未配置如实拒


def test_dispatch_unconfigured_503_zero_events(harness, slug_free):
    """工厂清零 + 键缺席（autouse fixture 已保证）→ 503、零留档。"""
    did, _ = at_in_progress(harness.client)
    resp = dispatch(harness.client, did)
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "未配置" in detail and "WANXING_CALLER_KEY" in detail
    assert internal_detail(harness.client, did)["pipeline_events"] == []


# ------------------------------------------------------------------ 成功路径


def test_dispatch_success_shape_events_and_queue_exclusion(harness, fake_client, slug_free):
    client = harness.client
    did, submitted = at_in_progress(client)

    resp = dispatch(client, did)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 响应形状逐字段（design D2 冻结）
    assert set(body) == {
        "ticket_path", "ticket_yaml", "slug", "sha256",
        "delivered", "queued_for_pull", "a2a",
    }
    assert body["delivered"] is True
    assert body["queued_for_pull"] is False  # 直连单不进拉取队列
    assert set(body["a2a"]) == {
        "http_status", "message_id", "idempotency_key", "reply_excerpt"
    }
    assert body["a2a"]["http_status"] == 200
    assert body["a2a"]["message_id"] == "m-1"
    assert body["a2a"]["reply_excerpt"] == "收到"
    assert body["a2a"]["idempotency_key"].startswith(f"wire-{submitted['code']}-")
    assert body["ticket_path"].startswith("reports/health-tickets/")
    assert body["sha256"] == hashlib.sha256(
        body["ticket_yaml"].encode("utf-8")
    ).hexdigest()
    assert yaml.safe_load(body["ticket_yaml"])["unit"] == "spiders/stats-gov-cn/"
    # 脱敏红线随同映射继承：全文无联系邮箱
    assert "buyer@example.com" not in body["ticket_yaml"]

    # 假客户端收到的是工单 YAML 全文（同一映射保证红线）
    assert fake_client.sent[0]["text"] == body["ticket_yaml"]
    assert fake_client.sent[0]["rpc_id"] == (
        f"wire-{submitted['code']}-" + body["sha256"][:8]
    )

    # 事件留档：wanxing_dispatched 逐键；绝无 workorder_exported
    detail = internal_detail(client, did)
    events = detail["pipeline_events"]
    assert [e["type"] for e in events] == ["wanxing_dispatched"]
    event = events[0]
    assert event["ticket_path"] == body["ticket_path"]
    assert event["ticket_yaml"] == body["ticket_yaml"]
    assert event["sha256"] == body["sha256"]
    assert event["slug"] == body["slug"]
    assert event["idempotency_key"] == body["a2a"]["idempotency_key"]
    assert event["message_id"] == "m-1"
    assert event["http_status"] == 200
    assert event["reply_excerpt"] == "收到"

    # 拉取队列零收录；状态不迁移
    assert [e["demand_id"] for e in pending(client)] == []
    assert detail["status"] == "in_progress"


def test_dispatch_idempotency_key_stable_for_same_ticket(
    harness, fake_client, slug_free, monkeypatch
):
    """同一单同一工单内容连发两次 → 两键相同且 = wire-{code}-{sha[:16]}。

    冻结 created_at（build_workorder 的漂移字段）证「同内容」——幂等键
    派生自内容哈希，内容同则键同（wanxing 侧去重口径）。
    """
    frozen = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(orchestration, "datetime", FrozenDatetime)

    client = harness.client
    did, submitted = at_in_progress(client)
    first = dispatch(client, did)
    second = dispatch(client, did)
    assert first.status_code == second.status_code == 200

    sha = hashlib.sha256(first.json()["ticket_yaml"].encode("utf-8")).hexdigest()
    assert first.json()["ticket_yaml"] == second.json()["ticket_yaml"]  # 内容同源
    expected = f"wire-{submitted['code']}-{sha[:16]}"
    assert fake_client.sent[0]["idempotency_key"] == expected
    assert fake_client.sent[1]["idempotency_key"] == expected  # 两键相同


# ------------------------------------------------------------------ 失败降级


def test_dispatch_failure_502_with_full_ticket_and_event(harness, slug_free):
    failing = FakeWanxingClient(
        error=WanxingInvokeError(
            "萬星 A2A 鉴权失败（HTTP 403，error=CALLER_NOT_AUTHORIZED）——"
            "调用者不在 agent 允许清单",
            status_code=403,
            reason="forbidden",
        )
    )
    set_wanxing_client_factory(lambda: failing)

    client = harness.client
    did, _ = at_in_progress(client)
    resp = dispatch(client, did)
    assert resp.status_code == 502, resp.text

    # 502 detail 是 dict：message（错误定位）+ 工单全文等 payload
    detail = resp.json()["detail"]
    assert set(detail) == {"message", "ticket_yaml", "ticket_path", "sha256", "slug"}
    assert "forbidden" in detail["message"]
    assert "403" in detail["message"]
    assert detail["ticket_path"].startswith("reports/health-tickets/")
    assert detail["sha256"] == hashlib.sha256(
        detail["ticket_yaml"].encode("utf-8")
    ).hexdigest()
    assert "buyer@example.com" not in detail["ticket_yaml"]  # 降级面仍守脱敏红线

    # 失败留档：wanxing_dispatch_failed（error/reason/全文），无 dispatched
    view = internal_detail(client, did)
    events = view["pipeline_events"]
    assert [e["type"] for e in events] == ["wanxing_dispatch_failed"]
    event = events[0]
    assert event["reason"] == "forbidden"
    assert "forbidden" in event["error"] or "403" in event["error"]
    assert event["ticket_yaml"] == detail["ticket_yaml"]
    assert event["ticket_path"] == detail["ticket_path"]
    assert event["sha256"] == detail["sha256"]
    assert event["slug"] == detail["slug"]
    assert view["status"] == "in_progress"  # 失败同样不迁移状态
    assert pending(client) == []


# ------------------------------------------------------------------ 内部鉴权


def test_dispatch_endpoint_requires_internal_token(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    for headers in ({}, {"X-Internal-Token": "wrong"}):
        resp = harness.client.post(
            f"/api/v1/demands/{did}/wanxing-dispatch", json={}, headers=headers
        )
        assert resp.status_code == 401, resp.text
    assert internal_detail(harness.client, did)["pipeline_events"] == []


# ------------------------------------------------------------------ admin 双闸


def test_admin_dispatch_happy_path_200(admin_env, slug_free):
    """admin 控制台动作：与内部端点共用 service——形状与留档逐字段一致。"""
    fake = FakeWanxingClient()
    set_wanxing_client_factory(lambda: fake)
    client = admin_env.client
    seed_console_customers("cust-001")
    demand = submit_demand(client, "cust-001", "光伏装机")
    resp = client.post(
        f"/internal/admin/demands/{demand['id']}/transition",
        json={"target_status": "in_progress", "by": "admin-console"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(
        f"/internal/admin/demands/{demand['id']}/wanxing-dispatch",
        json={},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "ticket_path", "ticket_yaml", "slug", "sha256",
        "delivered", "queued_for_pull", "a2a",
    }
    assert body["delivered"] is True
    assert body["queued_for_pull"] is False
    assert body["a2a"]["message_id"] == "m-1"

    # 同一 service 的留档事实（admin 闸与内部闸零分叉）
    view = next(
        d
        for d in client.get("/internal/admin/demands", headers=AUTH).json()
        if d["id"] == demand["id"]
    )
    assert [e["type"] for e in view["pipeline_events"]] == ["wanxing_dispatched"]
    assert view["status"] == "in_progress"
