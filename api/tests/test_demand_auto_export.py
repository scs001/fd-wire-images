"""受理后自动出单补扫测试（wire-demand-auto-export）。

覆盖 spec「受理后 SHALL 自动出单（后台补扫）」全场景：
- 受理后补扫出单（trigger=auto 标注 + pending 端点可见）；
- 无源地址/slug 占用被拒 → auto_export_failed 留痕、状态零变化；
- 补扫幂等（已出单/已有失败留痕的单零新事件）；
- 非 in_progress（submitted/delivered）不被扫；
- 意外异常落失败事件不抛出、不中断本轮其余单；
- 关闭开关零动作；人工出单事件不带 trigger（零漂移）。

slug 探测一律 monkeypatch 成 free（零网络）；被拒用例用真实拒绝路径
（无源地址 422 / slug occupied 409）。
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
from fd_wire_api.demands import orchestration
from fd_wire_api.demands.auto_export import (
    AUTO_TRIGGER,
    EVENT_AUTO_EXPORT_FAILED,
    auto_export_demand,
    sweep_missing_exports,
)
from fd_wire_api.demands.db import Base
from fd_wire_api.demands.models import Demand
from fd_wire_api.demands.orchestration import export_workorder_service

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-cust-001"
CUST = bearer(mint_id_token(SUB_CUST))
INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}

VALID_SOURCE = "https://www.stats.gov.cn/sj/zxfb/"


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
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    # 零网络：slug 探测一律 free
    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug: "free")
    with TestClient(app) as test_client:
        yield SimpleNamespace(client=test_client, Session=TestingSession)
    app.dependency_overrides.clear()


def _seed(
    Session,
    *,
    code: str = "DE-20261012-01",
    status: str = "in_progress",
    source_urls: list[str] | None = None,
    events: list[dict] | None = None,
) -> int:
    """直写入库一张需求单（可指定状态/源地址/既有事件）。"""
    db = Session()
    try:
        demand = Demand(
            code=code,
            customer_id="cust-001",
            title="自动出单测试单",
            intent="测试",
            contact_email="a@b.com",
            status=status,
            source_urls=source_urls,
            public_data_asserted=True,
        )
        if events is not None:
            demand.pipeline_events = events
        db.add(demand)
        db.commit()
        db.refresh(demand)
        return demand.id
    finally:
        db.close()


def _events(Session, demand_id: int) -> list[dict]:
    db = Session()
    try:
        return list(db.get(Demand, demand_id).pipeline_events or [])
    finally:
        db.close()


# ---------------------------------------------------------------- 受理后自动出单


def test_sweep_exports_missing_in_progress_demand(harness) -> None:
    """in_progress 且零留档 → 补扫自动出单，事件带 trigger=auto。"""
    demand_id = _seed(harness.Session, source_urls=[VALID_SOURCE])
    db = harness.Session()
    try:
        summary = sweep_missing_exports(db)
    finally:
        db.close()
    assert summary["checked"] == 1
    assert summary["exported"] == 1

    events = _events(harness.Session, demand_id)
    exported = [e for e in events if e["type"] == "workorder_exported"]
    assert len(exported) == 1
    assert exported[0]["trigger"] == AUTO_TRIGGER
    assert exported[0]["ticket_path"].startswith("reports/health-tickets/")
    assert exported[0]["sha256"]


def test_auto_exported_enters_pull_queue(harness) -> None:
    """自动出单即入舰队拉取队列（pending 端点可见）。"""
    _seed(harness.Session, source_urls=[VALID_SOURCE])
    db = harness.Session()
    try:
        sweep_missing_exports(db)
    finally:
        db.close()

    resp = harness.client.get(
        "/internal/demands/workorders/pending", headers=INTERNAL
    )
    assert resp.status_code == 200, resp.text
    pending = resp.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["code"] == "DE-20261012-01"
    assert pending[0]["ticket_yaml"]


# ---------------------------------------------------------------- 被拒留痕


def test_missing_source_urls_records_failure(harness) -> None:
    """无源地址存量单：落 auto_export_failed（422），状态与事件外零变化。"""
    demand_id = _seed(harness.Session, source_urls=None)
    db = harness.Session()
    try:
        summary = sweep_missing_exports(db)
    finally:
        db.close()
    assert summary["failed"] == 1

    events = _events(harness.Session, demand_id)
    assert len(events) == 1
    failed = events[0]
    assert failed["type"] == EVENT_AUTO_EXPORT_FAILED
    assert failed["status_code"] == 422
    assert "source_urls" in failed["reason"]
    assert failed["trigger"] == AUTO_TRIGGER

    db = harness.Session()
    try:
        demand = db.get(Demand, demand_id)
        assert demand.status == "in_progress"  # 状态零变化
    finally:
        db.close()


def test_slug_occupied_records_failure(harness, monkeypatch) -> None:
    """slug 占用：落 auto_export_failed（409），零工单产出。"""
    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug: "occupied")
    demand_id = _seed(harness.Session, source_urls=[VALID_SOURCE])
    db = harness.Session()
    try:
        sweep_missing_exports(db)
    finally:
        db.close()

    events = _events(harness.Session, demand_id)
    assert [e["type"] for e in events] == [EVENT_AUTO_EXPORT_FAILED]
    assert events[0]["status_code"] == 409
    assert "已占用" in events[0]["reason"]


def test_blocked_source_records_failure(harness) -> None:
    """禁爬源存量单（绕过提交口）：出单闸复检拒绝 → 失败留痕零工单。"""
    demand_id = _seed(
        harness.Session, source_urls=["https://www.pinduoduo.com/"]
    )
    db = harness.Session()
    try:
        sweep_missing_exports(db)
    finally:
        db.close()

    events = _events(harness.Session, demand_id)
    assert [e["type"] for e in events] == [EVENT_AUTO_EXPORT_FAILED]
    assert events[0]["status_code"] == 409
    assert "pinduoduo.com" in events[0]["reason"]


# ---------------------------------------------------------------- 幂等与跳过


def test_sweep_idempotent_for_exported(harness) -> None:
    """已出单的单经历多轮补扫零新事件（逐字节不变）。"""
    demand_id = _seed(harness.Session, source_urls=[VALID_SOURCE])
    db = harness.Session()
    try:
        sweep_missing_exports(db)
        before = _events(harness.Session, demand_id)
        for _ in range(3):
            summary = sweep_missing_exports(db)
        assert summary["skipped"] == 1
    finally:
        db.close()
    assert _events(harness.Session, demand_id) == before


def test_sweep_skips_failed_demand_no_retry(harness) -> None:
    """已有 auto_export_failed 的单不重试（防刷屏），其余漏网单照常补出。"""
    failed_id = _seed(
        harness.Session,
        code="DE-20261012-01",
        source_urls=None,
        events=[
            {
                "type": EVENT_AUTO_EXPORT_FAILED,
                "at": "2026-10-12T00:00:00+00:00",
                "trigger": AUTO_TRIGGER,
                "reason": "存量单无源地址",
                "status_code": 422,
            }
        ],
    )
    healthy_id = _seed(
        harness.Session, code="DE-20261012-02", source_urls=[VALID_SOURCE]
    )
    db = harness.Session()
    try:
        summary = sweep_missing_exports(db)
    finally:
        db.close()

    assert summary["checked"] == 2
    assert summary["exported"] == 1
    assert summary["skipped"] == 1
    # 失败单零新事件
    assert len(_events(harness.Session, failed_id)) == 1
    # 健康单照常补出
    types = [e["type"] for e in _events(harness.Session, healthy_id)]
    assert types == ["workorder_exported"]


@pytest.mark.parametrize("status", ["submitted", "delivered", "rejected"])
def test_sweep_skips_non_in_progress(harness, status: str) -> None:
    """非 in_progress 单不被扫（补扫只动作于处理期）。"""
    demand_id = _seed(
        harness.Session, status=status, source_urls=[VALID_SOURCE]
    )
    db = harness.Session()
    try:
        summary = sweep_missing_exports(db)
    finally:
        db.close()
    assert summary["checked"] == 0
    assert _events(harness.Session, demand_id) == []


# ---------------------------------------------------------------- 异常兜底


def test_unexpected_exception_records_failure_not_raises(harness, monkeypatch) -> None:
    """出单 service 抛意外异常：落失败事件不抛出，本轮其余单不受影响。"""
    demand_id = _seed(harness.Session, source_urls=[VALID_SOURCE])
    healthy_id = _seed(
        harness.Session, code="DE-20261012-02", source_urls=[VALID_SOURCE]
    )

    real = orchestration.export_workorder_service
    calls = {"n": 0}

    def flaky(db, demand, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(db, demand, **kwargs)

    monkeypatch.setattr(
        "fd_wire_api.demands.auto_export.export_workorder_service", flaky
    )
    db = harness.Session()
    try:
        summary = sweep_missing_exports(db)
    finally:
        db.close()

    assert summary["checked"] == 2
    assert summary["failed"] == 1
    assert summary["exported"] == 1
    crashed = _events(harness.Session, demand_id)
    assert [e["type"] for e in crashed] == [EVENT_AUTO_EXPORT_FAILED]
    assert "异常" in crashed[0]["reason"]
    assert "status_code" not in crashed[0]  # 非服务端拒绝，无状态码
    # 后续单照常出单
    assert [e["type"] for e in _events(harness.Session, healthy_id)] == [
        "workorder_exported"
    ]


# ---------------------------------------------------------------- 开关与零漂移


def test_sweeper_disabled_by_env(monkeypatch) -> None:
    """DEMAND_AUTO_EXPORT_ENABLED=false：循环不启动（start 幂等空转）。"""
    from fd_wire_api.demands import auto_export

    monkeypatch.setenv("DEMAND_AUTO_EXPORT_ENABLED", "false")
    auto_export._task = None
    auto_export.start_auto_export_sweeper()
    assert auto_export._task is None


def test_manual_export_event_has_no_trigger(harness) -> None:
    """人工出单（不经补扫）事件不带 trigger 键——人工路径零漂移。"""
    demand_id = _seed(harness.Session, source_urls=[VALID_SOURCE])
    db = harness.Session()
    try:
        export_workorder_service(db, db.get(Demand, demand_id))
    finally:
        db.close()
    events = _events(harness.Session, demand_id)
    assert len(events) == 1
    assert "trigger" not in events[0]


def test_auto_export_demand_returns_skipped_for_non_in_progress(harness) -> None:
    """直接调钩子：非 in_progress 单返回 skipped 且零事件。"""
    demand_id = _seed(harness.Session, status="submitted", source_urls=[VALID_SOURCE])
    db = harness.Session()
    try:
        outcome = auto_export_demand(db, db.get(Demand, demand_id))
    finally:
        db.close()
    assert outcome == {"action": "skipped", "reason": "not_in_progress"}
    assert _events(harness.Session, demand_id) == []


# ---------------------------------------------------------------- 客户面红线


def test_auto_export_failed_never_leaks_to_customer_view(harness) -> None:
    """auto_export_failed（含 reason 全文）绝不进客户面编排时间线（安全子集）。"""
    demand_id = _seed(harness.Session, source_urls=None)
    db = harness.Session()
    try:
        sweep_missing_exports(db)
    finally:
        db.close()
    # 内部留档确已落（前置成立）
    assert [e["type"] for e in _events(harness.Session, demand_id)] == [
        EVENT_AUTO_EXPORT_FAILED
    ]
    # 客户面详情：orchestration 安全子集为空、且响应文本零内部留档细节
    # （source_urls 是客户自己提交的输入，属可见字段；红线是内部事件与文案）
    resp = harness.client.get(f"/api/v1/demands/{demand_id}", headers=CUST)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["orchestration"] == []
    assert "auto_export_failed" not in resp.text
    assert "存量单无源地址" not in resp.text  # 内部拒绝文案不外露
