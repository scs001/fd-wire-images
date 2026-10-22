"""产物物化计量测试（wire-platform-v1 任务 5.6，全 mock 不打真网）。

覆盖：settings/凭据解析（fail-closed）、cron 子集解析与边界、三类 spec
读取清单提取（宽容解析）、promote 内部端点、运行器成功/部分失败/全败/
缺凭据拒绝、quota_exhausted 硬停 skip、调度命中与同分钟去重、
fake 时钟下两产物各跑两次的演示、alembic 0009 迁移。

外部 HTTP（wire 入口）经注入的 httpx.MockTransport mock。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app
from fd_wire_api.billing.models import Base as BillingBase
from fd_wire_api.billing.models import Customer, LedgerEntry, Plan
from fd_wire_api.materialization import (
    STATUS_QUOTA_EXHAUSTED,
    STATUS_QUOTA_UNVERIFIABLE,
    AllowAllQuotaGuard,
    ArtifactNotFoundError,
    MaterializationRunner,
    reset_materialization_runtime,
    resolve_customer_credential,
)
from fd_wire_api.materialization.credentials import CustomerCredentialMissingError
from fd_wire_api.materialization.cron import parse_cron
from fd_wire_api.materialization.db import MatBase, utc_now_naive
from fd_wire_api.materialization.models import MaterializedArtifact
from fd_wire_api.materialization.runner import (
    STATUS_FAILED,
    STATUS_FAILED_NO_CREDENTIAL,
    STATUS_FAILED_NO_WIRE_ENTRY,
    STATUS_FAILED_SPEC,
    STATUS_PARTIAL_FAILURE,
    STATUS_SUCCESS,
)
from fd_wire_api.materialization.routes import get_db as get_mat_db
from fd_wire_api.materialization.routes import get_draft_loader
from fd_wire_api.materialization.scheduler import tick_once
from fd_wire_api.materialization.settings import get_settings, parse_credential_map
from fd_wire_api.materialization.specs import (
    MATERIALIZABLE_KINDS,
    SpecReadListError,
    extract_read_list,
)
from fd_wire_api.metering.db import MeteringBase
from fd_wire_api.metering.models import MeteringRecord

API_DIR = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 基建


def make_engine() -> "sqlalchemy.engine.Engine":
    """共享内存库：物化 + billing + metering 三域表（硬停联查需要）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    MatBase.metadata.create_all(bind=engine)
    BillingBase.metadata.create_all(bind=engine)
    MeteringBase.metadata.create_all(bind=engine)
    return engine


class WireEntryMock:
    """wire 入口 mock：按指标 code 定制响应，记录 Authorization 与请求体。"""

    def __init__(self, behaviors: dict | None = None) -> None:
        self.behaviors = behaviors or {}
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        code = (body.get("arguments") or {}).get("code")
        self.calls.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("Authorization"),
                "tool": body.get("tool"),
                "code": code,
            }
        )
        action = self.behaviors.get(code, {"rows_returned": 5})
        if isinstance(action, Exception):
            raise action
        if isinstance(action, int):
            return httpx.Response(200, json={"rows_returned": action})
        if isinstance(action, httpx.Response):
            return action
        return httpx.Response(200, json=action)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def seed_customer(session: Session, *, included_lines: int = 1_000_000) -> Customer:
    plan = Plan(
        code=f"plan-{included_lines}",
        name="测试档",
        monthly_price_micro=0,
        included_lines=included_lines,
        overage_unit_price_micro=4_000,
        is_active=True,
    )
    session.add(plan)
    session.flush()
    customer = Customer(
        name="壹座测试客户",
        sub2api_user_id="cust-001",
        plan_id=plan.id,
        status="active",
    )
    session.add(customer)
    session.commit()
    return customer


def add_usage(session: Session, rows: int, *, when: datetime) -> None:
    session.add(
        MeteringRecord(
            event_timestamp=when,
            request_id=f"req-{rows}-{when.isoformat()}",
            caller="cust-001",
            credential_track="customer",
            server_name="wire-entry",
            tool_name="read",
            rows_returned=rows,
            source="customer",
        )
    )
    session.commit()


def register_artifact(
    session: Session,
    *,
    artifact_id: int,
    customer_id: str = "cust-001",
    kind: str = "dashboard",
    spec: dict | None = None,
    schedule: str | None = None,
) -> MaterializedArtifact:
    artifact = MaterializedArtifact(
        artifact_id=artifact_id,
        customer_id=customer_id,
        kind=kind,
        spec=spec
        or {
            "title": "GDP 仪表盘",
            "spec": {"indicators": ["IND-GDP-US", "IND-GDP-CN"]},
        },
        schedule=schedule,
        enabled=True,
    )
    session.add(artifact)
    session.commit()
    return artifact


# ------------------------------------------------------------------ settings/凭据


def test_settings_defaults_and_credential_map(monkeypatch) -> None:
    for name in (
        "MATERIALIZATION_DATABASE_URL",
        "WIRE_ENTRY_URL",
        "CUSTOMER_CREDENTIAL_MAP",
        "MATERIALIZATION_INTERVAL",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_materialization_runtime()
    s = get_settings()
    assert s.wire_entry_url == ""
    assert s.credential_map() == {}
    assert s.interval_seconds == 60.0
    reset_materialization_runtime()


def test_parse_credential_map_forms() -> None:
    assert parse_credential_map(None) == {}
    assert parse_credential_map({"a": "k1"}) == {"a": "k1"}
    assert parse_credential_map('{"a": "k1"}') == {"a": "k1"}
    assert parse_credential_map("cust-001=wsk-1,cust-002=wsk-2") == {
        "cust-001": "wsk-1",
        "cust-002": "wsk-2",
    }
    with pytest.raises(ValueError):
        parse_credential_map("no-equals")
    with pytest.raises(ValueError):
        parse_credential_map("cust-001= ")  # 凭据为空即坏映射，拒绝


def test_interval_clamped_to_minimum(monkeypatch) -> None:
    monkeypatch.setenv("MATERIALIZATION_INTERVAL", "0")
    reset_materialization_runtime()
    assert get_settings().interval_seconds == 1.0
    reset_materialization_runtime()


def test_resolve_customer_credential_fail_closed(monkeypatch) -> None:
    """缺客户凭据 → 拒绝执行；绝不静默回落平台服务凭据轨道。"""
    monkeypatch.setenv("CUSTOMER_CREDENTIAL_MAP", "cust-001=wsk-1")
    monkeypatch.setenv("AI_PLATFORM_CREDENTIAL", "plat-cred")
    reset_materialization_runtime()
    assert resolve_customer_credential("cust-001") == "wsk-1"
    with pytest.raises(CustomerCredentialMissingError, match="fail-closed"):
        resolve_customer_credential("cust-999")
    reset_materialization_runtime()


# ------------------------------------------------------------------ cron 子集


def test_cron_basic_subset_matching() -> None:
    every_15 = parse_cron("*/15 * * * *")
    assert every_15.matches(datetime(2026, 10, 5, 10, 0))
    assert every_15.matches(datetime(2026, 10, 5, 10, 45))
    assert not every_15.matches(datetime(2026, 10, 5, 10, 20))
    hourly_9 = parse_cron("0 9 * * *")
    assert hourly_9.matches(datetime(2026, 10, 5, 9, 0))
    assert not hourly_9.matches(datetime(2026, 10, 5, 9, 1))
    listed = parse_cron("15,45 * * * *")
    assert listed.matches(datetime(2026, 10, 5, 10, 15))
    assert not listed.matches(datetime(2026, 10, 5, 10, 30))
    month_day = parse_cron("0 0 1 1 *")
    assert month_day.matches(datetime(2027, 1, 1, 0, 0))
    assert not month_day.matches(datetime(2026, 10, 1, 0, 0))


def test_cron_day_of_week_and_or_semantics() -> None:
    # 2026-10-04 是周日：cron 0 与 7 均应命中
    assert parse_cron("0 0 * * 0").matches(datetime(2026, 10, 4, 0, 0))
    assert parse_cron("0 0 * * 7").matches(datetime(2026, 10, 4, 0, 0))
    workday = parse_cron("0 9 * * 1-5")
    assert workday.matches(datetime(2026, 10, 5, 9, 0))  # 周一
    assert not workday.matches(datetime(2026, 10, 4, 9, 0))  # 周日
    # 日/周同受限取 OR（标准 cron 语义）：10-01 是周四，日=1 命中即命中
    both = parse_cron("0 0 1 * 1")
    assert both.matches(datetime(2026, 10, 1, 0, 0))
    assert not both.matches(datetime(2026, 10, 2, 0, 0))  # 日/周均不命中
    # 仅周受限（日为 *）时：10-01 周四不命中周一
    only_dow = parse_cron("0 0 * * 1")
    assert not only_dow.matches(datetime(2026, 10, 1, 0, 0))
    assert only_dow.matches(datetime(2026, 10, 5, 0, 0))


def test_cron_range_step_and_parse_errors() -> None:
    stepped = parse_cron("10-30/10 * * * *")  # 区间 [10,30] 步进 10 → 10/20/30
    assert stepped.matches(datetime(2026, 10, 5, 10, 10))
    assert stepped.matches(datetime(2026, 10, 5, 10, 20))
    assert stepped.matches(datetime(2026, 10, 5, 10, 30))
    assert not stepped.matches(datetime(2026, 10, 5, 10, 40))
    assert not stepped.matches(datetime(2026, 10, 5, 10, 5))
    for bad in (
        "",            # 空
        "* * * *",     # 四字段
        "* * * * * *",  # 六字段
        "60 * * * *",  # 分越界
        "* 24 * * *",  # 时越界
        "0 9 * * mon",  # 名称写法不支持
        "*/0 * * * *",  # 步进 0
        "5-1 * * * *",  # 区间终点小于起点
        "-5 * * * *",   # 负数
    ):
        with pytest.raises(ValueError):
            parse_cron(bad)


# ------------------------------------------------------------------ spec 清单提取


def test_extract_dashboard_read_list() -> None:
    spec = {
        "title": "中美日人均 GDP 对比",
        "spec": {
            "indicators": ["IND-GDP-US", "IND-GDP-CN"],
            "panels": [
                {"metric": "IND-GDP-JP", "chart": "line"},
                {"metrics": ["IND-CPI-US"], "params": {"years": 10}},
                {"title": "无指标的布局占位"},
            ],
        },
    }
    items = extract_read_list("dashboard", spec)
    assert [i.code for i in items] == ["IND-GDP-US", "IND-GDP-CN", "IND-GDP-JP", "IND-CPI-US"]
    assert items[3].params == {"years": 10}
    # 布局重复引用不重复读取
    again = extract_read_list(
        "dashboard", {"spec": {"metrics": ["A"], "charts": [{"metric": "A"}]}}
    )
    assert [i.code for i in again] == ["A"]


def test_extract_composite_read_list() -> None:
    spec = {
        "spec": {
            "formula": "a + b",
            "sources": [
                {"code": "IND-EMP-URBAN", "params": {"unit": "万人"}},
                "IND-EMP-RURAL",
                {"tool": "read_range", "metric": "IND-EMP-TOTAL", "arguments": {"limit": 120}},
            ],
        }
    }
    items = extract_read_list("composite_indicator", spec)
    assert [i.code for i in items] == [
        "IND-EMP-URBAN",
        "IND-EMP-RURAL",
        "IND-EMP-TOTAL",
    ]
    assert items[0].params == {"unit": "万人"}
    assert items[2].tool == "read_range"
    assert items[2].params == {"limit": 120}


def test_extract_report_read_list() -> None:
    spec = {
        "spec": {
            "data_scope": {"metrics": ["IND-POP"]},
            "sections": [
                {"title": "增长", "indicators": ["IND-GDP-CN"]},
                {"title": "无数据章节"},
            ],
        }
    }
    items = extract_read_list("research_report", spec)
    assert [i.code for i in items] == ["IND-POP", "IND-GDP-CN"]


def test_extract_read_list_fail_paths() -> None:
    # 类型不支持：alert/refresh 无读取清单提取器
    with pytest.raises(SpecReadListError, match="无读取清单提取器"):
        extract_read_list("alert_rule", {"spec": {"threshold": 1}})
    # 清单为空：如实报错不硬跑
    with pytest.raises(SpecReadListError, match="提取不出非空读取清单"):
        extract_read_list("dashboard", {"spec": {"layout": "grid"}})
    # spec 非对象
    with pytest.raises(SpecReadListError):
        extract_read_list("dashboard", ["not", "a", "dict"])
    assert MATERIALIZABLE_KINDS == {"dashboard", "composite_indicator", "research_report"}


# ------------------------------------------------------------------ promote API


class _Draft:
    """内存草稿（绕开 ai 切片引擎，专注 promote 语义）。"""

    def __init__(self, artifact_id: int, kind: str, spec: dict, customer_id="cust-001"):
        self.id = artifact_id
        self.kind = kind
        self.spec = spec
        self.customer_id = customer_id
        self.status = "draft"


DASHBOARD_DRAFT = _Draft(
    101,
    "dashboard",
    {"title": "GDP", "spec": {"indicators": ["IND-GDP-US", "IND-GDP-CN"]}},
)


@pytest.fixture()
def promote_client(monkeypatch):
    monkeypatch.setenv("MATERIALIZATION_INTERNAL_TOKEN", "internal-tok")
    engine = make_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    drafts: dict[int, _Draft] = {DASHBOARD_DRAFT.id: DASHBOARD_DRAFT}

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_mat_db] = override_get_db
    app.dependency_overrides[get_draft_loader] = lambda: drafts.get
    reset_materialization_runtime()
    client = TestClient(app)  # 不进 lifespan：避免默认库建表
    yield client, TestSession, drafts
    app.dependency_overrides.clear()
    reset_materialization_runtime()


def test_promote_copies_draft_and_registers_schedule(promote_client) -> None:
    client, _, _ = promote_client
    resp = client.post(
        "/internal/materialization/101/promote",
        json={"schedule": "*/30 * * * *"},
        headers={"X-Internal-Token": "internal-tok"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["artifact_id"] == 101
    assert body["customer_id"] == "cust-001"
    assert body["kind"] == "dashboard"
    assert body["title"] == "GDP"
    assert body["schedule"] == "*/30 * * * *"
    assert body["last_status"] is None  # 尚未运行
    # spec 快照原样拷贝
    summary = client.get(
        "/internal/materialization/101", headers={"X-Internal-Token": "internal-tok"}
    )
    assert summary.status_code == 200
    assert summary.json()["artifact_id"] == 101


def test_promote_without_schedule_defaults_none(promote_client) -> None:
    client, _, _ = promote_client
    resp = client.post(
        "/internal/materialization/101/promote",
        json={},
        headers={"X-Internal-Token": "internal-tok"},
    )
    assert resp.status_code == 201
    assert resp.json()["schedule"] is None  # 不调度（仅手动触发）


def test_promote_validations(promote_client) -> None:
    client, _, drafts = promote_client
    ok = {"X-Internal-Token": "internal-tok"}
    # 草稿不存在
    assert client.post("/internal/materialization/999/promote", json={}, headers=ok).status_code == 404
    # cron 不合支持范围
    bad = client.post(
        "/internal/materialization/101/promote", json={"schedule": "bad cron"}, headers=ok
    )
    assert bad.status_code == 400
    assert "cron" in bad.json()["detail"]
    # 不支持的产物类型
    drafts[102] = _Draft(102, "alert_rule", {"spec": {"threshold": 3}})
    kind_resp = client.post("/internal/materialization/102/promote", json={}, headers=ok)
    assert kind_resp.status_code == 400
    assert "alert_rule" in kind_resp.json()["detail"]
    # 同草稿重复转正
    assert client.post("/internal/materialization/101/promote", json={}, headers=ok).status_code == 201
    dup = client.post("/internal/materialization/101/promote", json={}, headers=ok)
    assert dup.status_code == 409
    # 未登记的查询
    assert client.get("/internal/materialization/999", headers=ok).status_code == 404


def test_promote_token_fail_closed(promote_client) -> None:
    client, _, _ = promote_client
    # 错 token → 403
    wrong = client.post(
        "/internal/materialization/101/promote", json={}, headers={"X-Internal-Token": "nope"}
    )
    assert wrong.status_code == 403


def test_promote_unconfigured_token_503(monkeypatch) -> None:
    """token 未配置 fail-closed（503），与 catalog 切片同款。"""
    monkeypatch.delenv("MATERIALIZATION_INTERNAL_TOKEN", raising=False)
    reset_materialization_runtime()
    engine = make_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_mat_db] = override_get_db
    app.dependency_overrides[get_draft_loader] = lambda: (lambda i: None)
    try:
        client = TestClient(app)
        resp = client.post("/internal/materialization/101/promote", json={})
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.clear()
        reset_materialization_runtime()


# ------------------------------------------------------------------ 运行器


@pytest.fixture()
def runner_env():
    engine = make_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestSession()
    wire = WireEntryMock()
    runner = MaterializationRunner(
        session,
        http_client=wire.client(),
        credential_map={"cust-001": "wsk-cust-001"},
        wire_entry_url="http://wire-entry.example/mcp",
        quota_guard=AllowAllQuotaGuard(),
    )
    yield session, runner, wire
    runner.close()
    session.close()


def test_run_success_rows_billed_to_customer_track(runner_env) -> None:
    session, runner, wire = runner_env
    artifact = register_artifact(session, artifact_id=201)
    result = runner.run("cust-001", 201, now=datetime(2026, 10, 5, 10, 0))
    assert result.status == STATUS_SUCCESS
    assert result.rows == 10  # 2 项 × 默认 5 行
    session.refresh(artifact)
    assert artifact.last_status == STATUS_SUCCESS
    assert artifact.last_rows == 10
    assert artifact.last_run_at == datetime(2026, 10, 5, 10, 0)
    # 计量语义：每个请求都带客户凭据（Bearer wsk-cust-001）→ registry audit
    # 侧据此把行数记到客户账单；绝无平台凭据混入
    assert len(wire.calls) == 2
    assert all(c["auth"] == "Bearer wsk-cust-001" for c in wire.calls)
    assert [c["code"] for c in wire.calls] == ["IND-GDP-US", "IND-GDP-CN"]
    assert all(c["tool"] == "read" for c in wire.calls)


def test_run_partial_failure_records_per_item(runner_env) -> None:
    session, runner, wire = runner_env
    wire.behaviors = {
        "IND-GDP-US": {"rows_returned": 120},  # 顶层形态
        "IND-GDP-CN": httpx.Response(500, text="upstream boom"),
    }
    artifact = register_artifact(session, artifact_id=202)
    result = runner.run("cust-001", 202, now=datetime(2026, 10, 5, 10, 0))
    assert result.status == STATUS_PARTIAL_FAILURE
    assert result.rows == 120
    assert len(result.items) == 2
    failed = [i for i in result.items if not i.ok]
    assert failed[0].code == "IND-GDP-CN"
    assert "500" in (failed[0].error or "")
    session.refresh(artifact)
    assert artifact.last_run_detail["items"][0] == {
        "code": "IND-GDP-US", "ok": True, "rows": 120, "error": None,
    }


def test_run_all_failed_and_missing_rows_returned(runner_env) -> None:
    session, runner, wire = runner_env
    wire.behaviors = {
        "IND-GDP-US": {"data": []},   # 缺 rows_returned → 不猜 0，按失败记录
        "IND-GDP-CN": {"payload": {}},  # 同样缺
    }
    register_artifact(session, artifact_id=203)
    result = runner.run("cust-001", 203)
    assert result.status == STATUS_FAILED
    assert result.rows == 0
    assert all("rows_returned" in (i.error or "") for i in result.items)


def test_run_rows_returned_nested_forms(runner_env) -> None:
    """rows_returned 宽容提取：顶层 / result 内 / meta 内三种形态。"""
    session, runner, wire = runner_env
    wire.behaviors = {
        "IND-GDP-US": {"result": {"rows_returned": 7}},
        "IND-GDP-CN": {"meta": {"rows_returned": 3}},
    }
    register_artifact(session, artifact_id=210)
    result = runner.run("cust-001", 210)
    assert result.status == STATUS_SUCCESS
    assert result.rows == 10


def test_run_network_error_counts_as_item_failure(runner_env) -> None:
    session, runner, wire = runner_env
    wire.behaviors = {
        "IND-GDP-US": {"rows_returned": 3},
        "IND-GDP-CN": httpx.ConnectError("dns boom"),
    }
    register_artifact(session, artifact_id=204)
    result = runner.run("cust-001", 204)
    assert result.status == STATUS_PARTIAL_FAILURE
    assert result.rows == 3


def test_run_missing_credential_fail_closed_no_http(runner_env) -> None:
    """缺客户凭据：一条 HTTP 都不发（fail-closed），绝不换平台凭据跑。"""
    session, _, wire = runner_env
    runner = MaterializationRunner(
        session,
        http_client=wire.client(),
        credential_map={"cust-002": "wsk-2"},  # cust-001 无凭据
        wire_entry_url="http://wire-entry.example/mcp",
        quota_guard=AllowAllQuotaGuard(),
    )
    try:
        artifact = register_artifact(session, artifact_id=205)
        result = runner.run("cust-001", 205)
        assert result.status == STATUS_FAILED_NO_CREDENTIAL
        assert "CUSTOMER_CREDENTIAL_MAP" in (result.error or "")
        assert wire.calls == []  # 一条都没发出去
        session.refresh(artifact)
        assert artifact.last_status == STATUS_FAILED_NO_CREDENTIAL
    finally:
        runner.close()


def test_run_wire_entry_unconfigured_fail_closed(runner_env) -> None:
    session, _, wire = runner_env
    runner = MaterializationRunner(
        session,
        http_client=wire.client(),
        credential_map={"cust-001": "wsk-1"},
        wire_entry_url="",  # 未配置
        quota_guard=AllowAllQuotaGuard(),
    )
    try:
        register_artifact(session, artifact_id=206)
        result = runner.run("cust-001", 206)
        assert result.status == STATUS_FAILED_NO_WIRE_ENTRY
        assert wire.calls == []
    finally:
        runner.close()


def test_run_spec_extraction_failure_no_http(runner_env) -> None:
    session, runner, wire = runner_env
    register_artifact(
        session, artifact_id=207, spec={"title": "空", "spec": {"layout": "grid"}}
    )
    result = runner.run("cust-001", 207)
    assert result.status == STATUS_FAILED_SPEC
    assert wire.calls == []
    assert "提取不出非空读取清单" in (result.error or "")


def test_run_disabled_artifact_skipped(runner_env) -> None:
    session, runner, wire = runner_env
    artifact = register_artifact(session, artifact_id=208)
    artifact.enabled = False
    session.commit()
    result = runner.run("cust-001", 208)
    assert result.status == "disabled"
    assert wire.calls == []


def test_run_customer_isolation(runner_env) -> None:
    session, runner, _ = runner_env
    register_artifact(session, artifact_id=209, customer_id="cust-001")
    with pytest.raises(ArtifactNotFoundError):
        runner.run("cust-002", 209)  # 他企访问按不存在处理


# ------------------------------------------------------------------ 硬停（quota）


@pytest.fixture()
def quota_env():
    engine = make_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestSession()
    wire = WireEntryMock()
    runner = MaterializationRunner(
        session,
        http_client=wire.client(),
        credential_map={"cust-001": "wsk-cust-001"},
        wire_entry_url="http://wire-entry.example/mcp",
    )  # 默认 SqlQuotaGuard（billing+metering 只读联查）
    yield session, runner, wire
    runner.close()
    session.close()


def test_run_quota_exhausted_hard_stop(quota_env) -> None:
    """当期已用 ≥ 套餐额度 → skip + last_status=quota_exhausted，零读取。"""
    session, runner, wire = quota_env
    seed_customer(session, included_lines=1_000)
    add_usage(session, 1_000, when=datetime(2026, 10, 5, 8, 0, 0))
    artifact = register_artifact(session, artifact_id=301)
    result = runner.run("cust-001", 301, now=datetime(2026, 10, 5, 10, 0))
    assert result.status == STATUS_QUOTA_EXHAUSTED
    assert result.rows == 0
    assert wire.calls == []
    session.refresh(artifact)
    assert artifact.last_status == STATUS_QUOTA_EXHAUSTED
    assert artifact.last_rows == 0


def test_run_quota_exhausted_records_ledger_hardstop(quota_env) -> None:
    """额度耗尽硬停同时落账本 hardstop 事件（每客户每期幂等一条）——

    wire-platform-v1 4.4 的「账本硬停半链」：物化侧的拒绝不再只是 skip 记录，
    账本获得同一口径的事件轨迹（预检 402 与账本硬停一致、控制台可如实展示）。
    """
    session, runner, wire = quota_env
    customer = seed_customer(session, included_lines=1_000)
    add_usage(session, 1_000, when=datetime(2026, 10, 5, 8, 0, 0))
    register_artifact(session, artifact_id=302)

    result = runner.run("cust-001", 302, now=datetime(2026, 10, 5, 10, 0))
    assert result.status == STATUS_QUOTA_EXHAUSTED
    rows = (
        session.execute(select(LedgerEntry).where(LedgerEntry.kind == "hardstop"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].customer_id == customer.id
    assert rows[0].period == "2026-10"

    # 幂等：再次运行（含次日同月）不新增
    runner.run("cust-001", 302, now=datetime(2026, 10, 6, 10, 0))
    again = (
        session.execute(select(LedgerEntry).where(LedgerEntry.kind == "hardstop"))
        .scalars()
        .all()
    )
    assert len(again) == 1
    assert wire.calls == []


def test_run_quota_unverifiable_does_not_record_hardstop(quota_env) -> None:
    """「无法核实额度」是 fail-closed 拒绝但不是额度耗尽——不落硬停事件。"""
    session, runner, wire = quota_env
    register_artifact(session, artifact_id=303)  # 未 seed 客户 → 映射不到
    result = runner.run("cust-001", 303, now=datetime(2026, 10, 5, 10, 0))
    assert result.status == STATUS_QUOTA_UNVERIFIABLE
    rows = (
        session.execute(select(LedgerEntry).where(LedgerEntry.kind == "hardstop"))
        .scalars()
        .all()
    )
    assert rows == []
    assert wire.calls == []


def test_run_quota_allows_below_limit(quota_env) -> None:
    session, runner, wire = quota_env
    seed_customer(session, included_lines=1_000)
    add_usage(session, 999, when=datetime(2026, 10, 5, 8, 0, 0))
    register_artifact(session, artifact_id=302)
    result = runner.run("cust-001", 302, now=datetime(2026, 10, 5, 10, 0))
    assert result.status == STATUS_SUCCESS
    assert len(wire.calls) == 2


def test_run_quota_unmapped_customer_fail_closed(quota_env) -> None:
    """客户未映射 billing（无法核实额度）→ fail-closed 跳过，不硬跑。"""
    session, runner, wire = quota_env
    register_artifact(session, artifact_id=303)
    result = runner.run("cust-001", 303, now=datetime(2026, 10, 5, 10, 0))
    assert result.status == STATUS_QUOTA_UNVERIFIABLE
    assert "未映射" in (result.error or "")
    assert wire.calls == []


# ------------------------------------------------------------------ 调度器 + fake 时钟演示


@pytest.fixture()
def scheduler_env():
    """调度环境：共享内存库 + 宽额度 billing 客户 + wire 入口 mock。

    tick_once 每轮自带会话纪律（扫描会话与 runner 会话各自新建、各自
    关闭），测试侧只提供绑定同一引擎的会话工厂。
    """
    engine = make_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    seed = TestSession()
    try:
        seed_customer(seed, included_lines=1_000_000)  # 宽额度（演示计量不是硬停）
    finally:
        seed.close()
    wire = WireEntryMock()

    def runner_factory():
        return MaterializationRunner(
            TestSession(),
            http_client=wire.client(),
            credential_map={"cust-001": "wsk-cust-001"},
            wire_entry_url="http://wire-entry.example/mcp",
        )

    yield TestSession, runner_factory, wire


def _tick(test_session_cls, runner_factory, moment) -> list:
    return tick_once(
        now=moment,
        runner_factory=runner_factory,
        session_factory=lambda: test_session_cls(),
    )


def _by_artifact_id(test_session_cls, artifact_id: int) -> MaterializedArtifact:
    from sqlalchemy import select

    session = test_session_cls()
    try:
        return session.execute(
            select(MaterializedArtifact).where(
                MaterializedArtifact.artifact_id == artifact_id
            )
        ).scalar_one()
    finally:
        session.close()


def test_scheduler_hits_only_matching_minutes(scheduler_env) -> None:
    test_session_cls, runner_factory, _ = scheduler_env
    seed = test_session_cls()
    try:
        register_artifact(seed, artifact_id=401, schedule="*/30 * * * *")
    finally:
        seed.close()
    # 不命中的分钟：无事发生
    assert _tick(test_session_cls, runner_factory, datetime(2026, 10, 5, 10, 7)) == []
    assert _tick(test_session_cls, runner_factory, datetime(2026, 10, 5, 10, 29)) == []
    # 命中 10:00
    outcomes = _tick(test_session_cls, runner_factory, datetime(2026, 10, 5, 10, 0))
    assert [(o[0], o[1]) for o in outcomes] == [(401, STATUS_SUCCESS)]


def test_scheduler_same_minute_dedupe_and_next_hit(scheduler_env) -> None:
    test_session_cls, runner_factory, wire = scheduler_env
    seed = test_session_cls()
    try:
        register_artifact(seed, artifact_id=402, schedule="*/30 * * * *")
    finally:
        seed.close()
    first = _tick(test_session_cls, runner_factory, datetime(2026, 10, 5, 10, 0))
    assert len(first) == 1
    # 同一命中分钟内重复扫描（间隔 60s < cron 粒度）：不重复触发
    assert _tick(test_session_cls, runner_factory, datetime(2026, 10, 5, 10, 0, 30)) == []
    assert len(wire.calls) == 2  # 仍是一次运行的两个读数项
    # 下一个命中分钟（10:30）再次触发
    second = _tick(test_session_cls, runner_factory, datetime(2026, 10, 5, 10, 30))
    assert len(second) == 1
    assert len(wire.calls) == 4


def test_scheduler_invalid_schedule_skipped_not_crashed(scheduler_env) -> None:
    test_session_cls, runner_factory, _ = scheduler_env
    seed = test_session_cls()
    try:
        register_artifact(seed, artifact_id=403, schedule="not a cron")
    finally:
        seed.close()
    assert _tick(test_session_cls, runner_factory, datetime(2026, 10, 5, 10, 0)) == []


def test_fake_clock_demo_two_artifacts_two_runs_each(scheduler_env) -> None:
    """手动演示（fake 时钟）：两产物按各自 cron 各跑两次，行数进 last_rows。

    - 产物 A：*/30（每逢 :00/:30 触发）
    - 产物 B：15,45（每逢 :15/:45 触发）
    时钟序列 10:00、10:00（重复扫描）、10:15、10:30、10:45 →
    A 跑 2 次（10:00/10:30）、B 跑 2 次（10:15/10:45）。
    """
    test_session_cls, runner_factory, wire = scheduler_env
    seed = test_session_cls()
    try:
        register_artifact(
            seed,
            artifact_id=501,
            spec={"title": "A", "spec": {"indicators": ["IND-A1", "IND-A2"]}},
            schedule="*/30 * * * *",
        )
        register_artifact(
            seed,
            artifact_id=502,
            spec={"title": "B", "spec": {"indicators": ["IND-B1"]}},
            schedule="15,45 * * * *",
        )
    finally:
        seed.close()
    moments = [
        datetime(2026, 10, 5, 10, 0),
        datetime(2026, 10, 5, 10, 0, 30),  # 同分钟重复扫描 → 去重
        datetime(2026, 10, 5, 10, 15),
        datetime(2026, 10, 5, 10, 30),
        datetime(2026, 10, 5, 10, 45),
    ]
    for moment in moments:
        _tick(test_session_cls, runner_factory, moment)
    a = _by_artifact_id(test_session_cls, 501)
    b = _by_artifact_id(test_session_cls, 502)
    # 各跑两次：A 4 项读数（2 次 × 2 指标）、B 2 项读数
    assert a.last_rows == 10  # 最近一次（10:30）：2 指标 × 5 行
    assert a.last_run_at == datetime(2026, 10, 5, 10, 30)
    assert a.last_status == STATUS_SUCCESS
    assert b.last_rows == 5  # 最近一次（10:45）：1 指标 × 5 行
    assert b.last_run_at == datetime(2026, 10, 5, 10, 45)
    assert b.last_status == STATUS_SUCCESS
    # 总读数项 = 2×2 + 2×1 = 6，全部带客户凭据（计量进客户账单）
    assert len(wire.calls) == 6
    assert all(c["auth"] == "Bearer wsk-cust-001" for c in wire.calls)


# ------------------------------------------------------------------ alembic 0009


@pytest.fixture()
def migrated_sqlite(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "head")
    return db_url, cfg


def test_migration_0009_creates_materialized_artifacts(migrated_sqlite) -> None:
    db_url, _ = migrated_sqlite
    engine = create_engine(db_url)
    inspector = inspect(engine)
    assert "materialized_artifacts" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("materialized_artifacts")}
    # ref_key：wire-customer-data-engine 6.1 派生调度行的关联键
    assert columns == {
        "id", "artifact_id", "customer_id", "kind", "ref_key", "spec", "schedule",
        "last_run_at", "last_status", "last_rows", "last_run_detail",
        "last_error", "enabled", "created_at",
    }
    with engine.connect() as conn:
        indexes = conn.exec_driver_sql(
            "PRAGMA index_list(materialized_artifacts)"
        ).fetchall()
        uniques = set()
        for _, name, is_unique, *_ in indexes:
            if is_unique:
                cols = {
                    row[2]
                    for row in conn.exec_driver_sql(f"PRAGMA index_info({name})").fetchall()
                }
                uniques.add(frozenset(cols))
        assert frozenset({"artifact_id"}) in uniques  # 同草稿只转正一次
    engine.dispose()


def test_migration_0009_downgrade_drops_table(migrated_sqlite, monkeypatch) -> None:
    db_url, cfg = migrated_sqlite
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.downgrade(cfg, "0008_demand_pipeline")
    engine = create_engine(db_url)
    assert "materialized_artifacts" not in inspect(engine).get_table_names()
    engine.dispose()
