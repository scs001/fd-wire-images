"""工单状态回流与客户可见性测试（wire-demand-generation-orchestration 任务 3）。

覆盖（tasks 3.1–3.3）：
- ``map_ticket_to_events`` 各态映射（纯函数，契约照真实工单 YAML 样本造假 dict）；
- ``sync_demand_workorders``：候选筛选（已投递=export 后有 pipeline_run）、
  事件追加（ticket_path/at）、幂等（二次同步零新增）、404→workorder_lost
  一次且该单停扫、网络/5xx 拉取失败零写入、YAML 解析失败零写入；
- 客户面安全子集：``orchestration_customer_view`` 与 ``DemandOut`` 组装
  只出 {stage, at, pr_url}，workorder_exported / pipeline_run 原文与
  agent 回执文本绝不进客户响应；
- 端到端：同步落事件 → ``GET /api/v1/demands/{id}`` 客户详情呈现编排
  时间线（DemandOut._derive_orchestration 响应组装点）。

lifespan 循环不直测（tick 逻辑=注入 session 单测 sync_demand_workorders）。
真实工单样本见 fd-industry-data/reports/health-tickets/*.yaml——本文件只
消费契约形态，绝不访问网络。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（test_demands_pipeline_delivery 同款
# 纪律：只在导入期间生效，导入完即还原，避免泄漏进同会话其余模块）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import yaml
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]

from fd_wire_api.demands import Demand, DemandStatus, get_db
from fd_wire_api.demands.db import Base
from fd_wire_api.demands.models import Demand as DemandModel
from fd_wire_api.demands.orchestration_sync import (
    map_ticket_to_events,
    orchestration_customer_view,
    sync_demand_workorders,
)
from fd_wire_api.demands.schemas import DemandOut

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
SUB_CUST = "sub-cust-sync-001"
CUST = bearer(mint_id_token(SUB_CUST))

TICKET_PATH = "reports/health-tickets/20261011-wire-DE-20261011-01.yaml"
RAW_URL = (
    "https://raw.githubusercontent.com/FindDataTechnology/"
    "fd-industry-data/main/" + TICKET_PATH
)


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def session():
    """独立内存库会话（sync 集成测试用）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = maker()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """内存库 + 覆盖 get_db 的 TestClient（返回 (client, TestingSession)）。

    暴露会话工厂便于在测试内直接注入 pipeline_events（同引擎即同库）。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )

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
        yield test_client, TestingSession
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ helpers


def make_demand(
    db,
    *,
    code: str = "DE-20261011-01",
    status: str = DemandStatus.in_progress.value,
    events: list | None = None,
) -> DemandModel:
    demand = DemandModel(
        code=code,
        customer_id="cust-001",
        title="标题",
        intent="意图",
        status=status,
        status_history=[],
    )
    db.add(demand)
    db.commit()
    for event in events or []:
        demand.append_pipeline_event(event)
    db.commit()
    return demand


def exported_event(ticket_path: str = TICKET_PATH) -> dict:
    """workorder_exported 留档（照任务 2 事实形态：全文 + 哈希 + ticket_path）。"""
    return {
        "type": "workorder_exported",
        "ticket_path": ticket_path,
        "yaml_text": "kind: generate\nunit: spiders/example-com/\n...",
        "sha": "deadbeef",
        "by": "operator:ops-001",
    }


def ack_run_event() -> dict:
    """舰队拉取器 ack 的 pipeline_run 留档（含 agent 回执文本——客户面绝不可见）。"""
    return {
        "type": "pipeline_run",
        "pipeline_run_id": "run-ack-001",
        "repo": "fd-industry-data",
        "branch": "main",
        "note": "拉取器回执：ticket written, self-pushed to main（agent response body）",
        "by": "fleet-puller",
    }


def ticket_text(lifecycle: list, terminal=None) -> str:
    return yaml.safe_dump(
        {"lifecycle": lifecycle, "terminal": terminal}, sort_keys=False
    )


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def submitted(ok: bool, http_status: int = 200) -> dict:
    entry = {"at": "2026-10-11T08:00:00+00:00", "event": "submitted", "ok": ok}
    if ok:
        entry["http_status"] = http_status
    return entry


# ------------------------------------------- 3.1 map_ticket_to_events（纯函数）


def test_map_ticket_only_created_yields_nothing():
    """仅 created：无任何事件（绝不编造进度）。"""
    ticket = {
        "lifecycle": [{"at": "2026-10-11T07:00:00+00:00", "event": "created"}],
        "terminal": None,
    }
    assert map_ticket_to_events(ticket) == []


def test_map_ticket_submitted_ok_once():
    """首次 submitted ok:true → 恰一条 workorder_submitted。"""
    ticket = {
        "lifecycle": [
            {"at": "2026-10-11T07:00:00+00:00", "event": "created"},
            submitted(True),
        ],
        "terminal": None,
    }
    assert map_ticket_to_events(ticket) == [{"type": "workorder_submitted"}]


def test_map_ticket_pr_url_opens_pr():
    """出现非空 pr_url → pr_open（附链接）。"""
    ticket = {
        "lifecycle": [
            {"at": "2026-10-11T07:00:00+00:00", "event": "created"},
            submitted(True),
            {
                "at": "2026-10-11T09:00:00+00:00",
                "event": "status",
                "state": None,
                "pr_url": "https://github.com/FindDataTechnology/fd-industry-data/pull/42",
            },
        ],
        "terminal": None,
    }
    events = map_ticket_to_events(ticket)
    assert events == [
        {"type": "workorder_submitted"},
        {
            "type": "pr_open",
            "pr_url": "https://github.com/FindDataTechnology/fd-industry-data/pull/42",
        },
    ]


def test_map_ticket_state_done_opens_pr_without_url():
    """state=done（暂无 pr_url）→ 也算 pr_open，链接缺省不编造。"""
    ticket = {
        "lifecycle": [
            submitted(True),
            {"at": "2026-10-11T09:30:00+00:00", "event": "status", "state": "done", "pr_url": ""},
        ],
        "terminal": None,
    }
    events = map_ticket_to_events(ticket)
    assert events == [{"type": "workorder_submitted"}, {"type": "pr_open"}]


def test_map_ticket_terminal_closed_merges():
    """terminal=closed → merged（PR 链接一并留档）。"""
    pr = "https://github.com/FindDataTechnology/fd-industry-data/pull/42"
    ticket = {
        "lifecycle": [
            submitted(True),
            {"at": "2026-10-11T10:00:00+00:00", "event": "status", "state": "done", "pr_url": pr},
        ],
        "terminal": "closed",
    }
    events = map_ticket_to_events(ticket)
    assert [e["type"] for e in events] == ["workorder_submitted", "pr_open", "merged"]
    assert events[-1] == {"type": "merged", "pr_url": pr}


def test_map_ticket_three_failures_block():
    """submitted ≥3 次全 ok:false 且无成功 → blocked（恰一条，无 submitted 事件）。"""
    ticket = {
        "lifecycle": [
            {"at": "2026-10-11T07:00:00+00:00", "event": "created"},
            submitted(False, 409),
            submitted(False, 500),
            submitted(False, 500),
        ],
        "terminal": None,
    }
    events = map_ticket_to_events(ticket)
    assert len(events) == 1
    assert events[0]["type"] == "blocked"
    assert "3 次未成功" in events[0]["note"]


def test_map_ticket_two_failures_or_late_success_do_not_block():
    """2 次失败、或失败后成功——都不 blocked（有成功即已进流水线）。"""
    two_failures = {
        "lifecycle": [submitted(False), submitted(False)],
        "terminal": None,
    }
    assert map_ticket_to_events(two_failures) == []

    late_success = {
        "lifecycle": [
            submitted(False),
            submitted(False),
            submitted(False),
            submitted(True),  # 第 4 次成功——不是 blocked
        ],
        "terminal": None,
    }
    events = map_ticket_to_events(late_success)
    assert [e["type"] for e in events] == ["workorder_submitted"]


# ------------------------------------------- 3.1 sync（候选 / 追加 / 幂等）


def test_sync_appends_events_for_delivered_workorder(session):
    demand = make_demand(session, events=[exported_event(), ack_run_event()])
    body = ticket_text(
        [
            {"at": "2026-10-11T07:00:00+00:00", "event": "created"},
            submitted(True),
            {
                "at": "2026-10-11T09:00:00+00:00",
                "event": "status",
                "state": "done",
                "pr_url": "https://github.com/FindDataTechnology/fd-industry-data/pull/42",
            },
        ],
        terminal="closed",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == RAW_URL
        return httpx.Response(200, text=body)

    summary = sync_demand_workorders(session, http_client=mock_client(handler))

    assert summary == {"checked": 1, "updated": 1, "skipped": 0}
    events = demand.pipeline_events
    assert [e["type"] for e in events] == [
        "workorder_exported", "pipeline_run",
        "workorder_submitted", "pr_open", "merged",
    ]
    for event in events[2:]:
        assert event["ticket_path"] == TICKET_PATH
        assert event["at"]  # append_pipeline_event 盖章
    assert events[3]["pr_url"].endswith("/pull/42")


def test_sync_is_idempotent_second_round_zero_new(session):
    demand = make_demand(session, events=[exported_event(), ack_run_event()])
    body = ticket_text(
        [submitted(True),
         {"at": "t", "event": "status", "state": "done", "pr_url": "https://github.com/x/y/pull/1"}],
        terminal="closed",
    )
    http = mock_client(lambda request: httpx.Response(200, text=body))

    first = sync_demand_workorders(session, http_client=http)
    assert first["updated"] == 1
    snapshot = list(demand.pipeline_events)

    second = sync_demand_workorders(session, http_client=http)
    assert second == {"checked": 1, "updated": 0, "skipped": 1}
    assert demand.pipeline_events == snapshot  # 零新增零改写


def test_sync_candidate_requires_pipeline_run_after_export(session):
    """未 ack（export 后无 pipeline_run）或 run 在 export 前——都不扫。"""
    make_demand(session, code="DE-20261011-10", events=[exported_event()])  # 未 ack
    make_demand(
        session,
        code="DE-20261011-11",
        events=[ack_run_event(), exported_event()],  # run 在 export 前
    )
    make_demand(
        session,
        code="DE-20261011-12",
        status=DemandStatus.submitted.value,  # 非处理期
        events=[exported_event(), ack_run_event()],
    )
    http = mock_client(
        lambda request: pytest.fail("无候选单时绝不应发起拉取")
    )
    assert sync_demand_workorders(session, http_client=http) == {
        "checked": 0, "updated": 0, "skipped": 0,
    }


def test_sync_uses_latest_exported_ticket_path(session):
    """同单重出新工单：取最新 workorder_exported 的 ticket_path（旧单不再扫）。"""
    old_path = "reports/health-tickets/20261011-wire-DE-20261011-01.yaml"
    new_path = "reports/health-tickets/20261012-wire-DE-20261011-01.yaml"
    demand = make_demand(
        session,
        events=[
            exported_event(old_path),
            ack_run_event(),
            exported_event(new_path),
            ack_run_event(),  # 新工单也已 ack（否则最新单未投递、不扫）
        ],
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=ticket_text([submitted(True)], terminal=None))

    sync_demand_workorders(session, http_client=mock_client(handler))
    # 只拉最新路径；事件 ticket_path 与之对齐
    new_raw = (
        "https://raw.githubusercontent.com/FindDataTechnology/"
        "fd-industry-data/main/" + new_path
    )
    assert seen == [new_raw]
    assert demand.pipeline_events[-1]["ticket_path"] == new_path


# ------------------------------------------- 3.1 失败语义（404 / 网络 / 解析）


def test_sync_404_records_lost_once_and_stops_scanning(session):
    demand = make_demand(session, events=[exported_event(), ack_run_event()])
    state = {"status": 404}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(state["status"], text="404: Not Found")

    http = mock_client(handler)
    summary = sync_demand_workorders(session, http_client=http)
    assert summary == {"checked": 1, "updated": 1, "skipped": 0}
    lost = [e for e in demand.pipeline_events if e["type"] == "workorder_lost"]
    assert len(lost) == 1
    assert lost[0]["ticket_path"] == TICKET_PATH
    assert "404" in lost[0]["note"]
    snapshot = list(demand.pipeline_events)

    # 工单文件「回来了」（200 + 可映射内容）——该单已留痕停扫，零新增
    state["status"] = 200
    body = ticket_text(
        [submitted(True),
         {"at": "t", "event": "status", "state": "done", "pr_url": "https://github.com/x/y/pull/9"}],
        terminal="closed",
    )
    http2 = mock_client(lambda request: httpx.Response(200, text=body))
    summary2 = sync_demand_workorders(session, http_client=http2)
    assert summary2 == {"checked": 0, "updated": 0, "skipped": 0}
    assert demand.pipeline_events == snapshot


def test_sync_network_error_writes_nothing(session):
    demand = make_demand(session, events=[exported_event(), ack_run_event()])

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    summary = sync_demand_workorders(session, http_client=mock_client(handler))
    assert summary == {"checked": 1, "updated": 0, "skipped": 1}
    assert [e["type"] for e in demand.pipeline_events] == [
        "workorder_exported", "pipeline_run",
    ]


def test_sync_5xx_retries_then_skips_without_writes(session):
    demand = make_demand(session, events=[exported_event(), ack_run_event()])
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="upstream down")

    summary = sync_demand_workorders(session, http_client=mock_client(handler))
    assert attempts["n"] == 3  # 首次 + 2 次重试
    assert summary == {"checked": 1, "updated": 0, "skipped": 1}
    assert [e["type"] for e in demand.pipeline_events] == [
        "workorder_exported", "pipeline_run",
    ]


def test_sync_unparseable_yaml_writes_nothing(session):
    """解析失败=本轮跳过不写（不猜状态、不误标 lost）。"""
    demand = make_demand(session, events=[exported_event(), ack_run_event()])
    http = mock_client(lambda request: httpx.Response(200, text="{ not: valid: yaml :"))
    summary = sync_demand_workorders(session, http_client=http)
    assert summary == {"checked": 1, "updated": 0, "skipped": 1}
    assert [e["type"] for e in demand.pipeline_events] == [
        "workorder_exported", "pipeline_run",
    ]


# ------------------------------------------- 3.2 客户面安全子集


def test_customer_view_safe_subset_only():
    """组装函数输出：只含 {stage, at, pr_url}，四态之外的类型绝不外露。"""
    events = [
        exported_event(),
        ack_run_event(),
        {"type": "gate_result", "passed": True, "note": "内部观测", "at": "t0"},
        {"type": "workorder_submitted", "ticket_path": TICKET_PATH, "at": "t1"},
        {
            "type": "pr_open",
            "ticket_path": TICKET_PATH,
            "at": "t2",
            "pr_url": "https://github.com/x/y/pull/42",
            "note": "agent 回执全文……绝不外露",
        },
        {"type": "merged", "ticket_path": TICKET_PATH, "at": "t3"},
        {"type": "blocked", "ticket_path": TICKET_PATH, "at": "t4", "note": "内部细节"},
        {"type": "workorder_lost", "ticket_path": TICKET_PATH, "at": "t5", "note": "404"},
    ]
    view = orchestration_customer_view(events)
    assert view == [
        {"stage": "workorder_submitted", "at": "t1"},
        {"stage": "pr_open", "at": "t2", "pr_url": "https://github.com/x/y/pull/42"},
        {"stage": "merged", "at": "t3"},
        {"stage": "blocked", "at": "t4"},
    ]
    dumped = repr(view)
    for forbidden in (
        "ticket_path", "note", "agent 回执", "yaml_text", "sha",
        "workorder_exported", "pipeline_run", "gate_result", "workorder_lost",
    ):
        assert forbidden not in dumped


def test_demand_out_assembles_orchestration_from_internal_events(session):
    """DemandOut 响应组装：orchestration 进客户面，内部原文绝不出现。"""
    demand = make_demand(
        session,
        events=[
            exported_event(),
            ack_run_event(),
            {
                "type": "workorder_submitted",
                "ticket_path": TICKET_PATH,
                "at": "2026-10-11T08:00:00+00:00",
            },
            {
                "type": "pr_open",
                "ticket_path": TICKET_PATH,
                "at": "2026-10-11T09:00:00+00:00",
                "pr_url": "https://github.com/FindDataTechnology/fd-industry-data/pull/42",
            },
        ],
    )
    out = DemandOut.model_validate(demand)
    assert [e.stage for e in out.orchestration] == ["workorder_submitted", "pr_open"]
    assert out.orchestration[1].pr_url.endswith("/pull/42")

    dumped = out.model_dump()
    assert "pipeline_events" not in dumped  # 内部留档全文不进客户视图
    body = repr(dumped)
    for forbidden in (
        "ticket_path", "agent 回执", "拉取器回执", "yaml_text", "deadbeef",
        "workorder_exported", "pipeline_run",
    ):
        assert forbidden not in body


def test_customer_detail_api_shows_orchestration_after_sync(client):
    """端到端：sync 落事件 → 客户详情时间线呈现安全子集。"""
    test_client, TestingSession = client
    payload = {
        "description": "需要某公开源的日度行情数据，用于投研",
        "contact_email": "buyer@example.com",
        "source_urls": ["https://example.com/data"],
        "public_data_asserted": True,
    }
    resp = test_client.post("/api/v1/demands", json=payload, headers=CUST)
    assert resp.status_code == 201, resp.text
    demand_id = resp.json()["id"]

    transition = test_client.post(
        f"/internal/demands/{demand_id}/transition",
        json={"target_status": "in_progress", "by": "ops-001"},
        headers=INTERNAL,
    )
    assert transition.status_code == 200, transition.text

    # 同引擎注入出单+ack 事实，随后跑一轮真实 sync（mock raw 通道）
    db = TestingSession()
    try:
        demand = db.get(Demand, demand_id)
        demand.append_pipeline_event(exported_event())
        demand.append_pipeline_event(ack_run_event())
        db.commit()
        body = ticket_text(
            [
                {"at": "2026-10-11T07:00:00+00:00", "event": "created"},
                submitted(True),
                {
                    "at": "2026-10-11T09:00:00+00:00",
                    "event": "status",
                    "state": "done",
                    "pr_url": "https://github.com/FindDataTechnology/fd-industry-data/pull/42",
                },
            ],
            terminal="closed",
        )
        http = mock_client(lambda request: httpx.Response(200, text=body))
        summary = sync_demand_workorders(db, http_client=http)
        assert summary == {"checked": 1, "updated": 1, "skipped": 0}
    finally:
        db.close()

    detail = test_client.get(f"/api/v1/demands/{demand_id}", headers=CUST)
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["status"] == "in_progress"  # 编排绝不自动交付
    assert [
        (e["stage"], e.get("pr_url")) for e in data["orchestration"]
    ] == [
        ("workorder_submitted", None),
        ("pr_open", "https://github.com/FindDataTechnology/fd-industry-data/pull/42"),
        ("merged", "https://github.com/FindDataTechnology/fd-industry-data/pull/42"),
    ]
    # 客户面纪律：内部留档与回执文本绝不出现
    for forbidden in (
        "pipeline_events", "ticket_path", "agent 回执", "拉取器回执",
        "yaml_text", "deadbeef",
    ):
        assert forbidden not in detail.text
