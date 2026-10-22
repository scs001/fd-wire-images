"""工单生成与投递测试（wire-demand-generation-orchestration 任务 2.4，
投递通道=舰队拉取器：wire 零外部 git 凭据）。

覆盖（对齐 spec demand-orchestration 前两条 Requirement）：
- 映射确定性：同输入两次出单哈希一致 + YAML 字段序稳定（kind→unit→
  category→brief→golden→verify；brief 内 source_urls→expectations→
  cadence→notes）；cadence 映射 daily/hourly/weekly，非法值并入 notes。
- slug 派生与冲突：hostname 净化规则、override 覆写、40 字符截断；
  冲突探测 raw 通道各状态（404=空闲、200/5xx/网络异常=占用、注入拒）；
  端点冲突 409 转人工且零事件写入。
- 脱敏红线：工单全文不含 contact_email；公司名不入单（notes 只留
  customer_id 前 8 位代号）。
- 诚实降级（拉取器未部署/无 ack）：出单响应恒带工单全文、delivered=
  false、queued_for_pull=true，无任何自动化投递成功假象，且全程零
  git/subprocess 调用（design D6：wire 不持有外部仓 git 凭据）。
- 舰队拉取通道：pending 仅列 in_progress 单、每单至多一条最新未 ack
  导出（重出新单取代旧单）；ack 记 ``pipeline_run`` 事件（含可追溯
  路径）；ack 幂等（重复 ack 不重复记事件）；同日重出新单在旧 ack 之后
  重新入队；未知工单 404；两端点缺内部令牌 401。
- 状态门：非 in_progress（submitted/delivered）出单 409；无 source_urls
  的存量单 422 如实提示；出单不迁移状态、时间线不动。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

# 在导入 app 前把默认引擎指向内存库（与 test_demands_statemachine 同款纪律；
# 只在导入期间生效，导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import httpx
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
from fd_wire_api.demands.orchestration import (
    build_workorder,
    check_slug_free,
    derive_slug,
)

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
SUB_CUST = "sub-cust-001"
CUST = bearer(mint_id_token(SUB_CUST))

VALID_SOURCES = [
    "https://www.stats.gov.cn/sj/zxfb/",
    "http://www.caam.org.cn/chn/4/cate_30/list_1.html",
]
STATS_SLUG = "stats-gov-cn"  # www.stats.gov.cn 净化后的期望 slug


# ------------------------------------------------------------------ fixtures


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
def slug_free(monkeypatch):
    """端点用 slug 探测默认放行（真探测走 raw 网络通道，单测另行覆盖）。"""
    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug, **kw: "free")


@pytest.fixture()
def no_git(monkeypatch):
    """红线：投递通道=舰队拉取器——wire 侧绝不调用 git/subprocess。"""
    def boom(*args, **kwargs):
        raise AssertionError("wire 侧绝不调用 git/subprocess（舰队拉取通道）")

    monkeypatch.setattr(subprocess, "run", boom)


# ------------------------------------------------------------------ helpers


def submit(client, **overrides) -> dict:
    payload = {
        "description": "需要上海市 2024 年以来乘用车月度销量与价格带分布，用于市场分析",
        "contact_email": "buyer@example.com",
        "company": "上海某某咨询",
        "source_urls": list(VALID_SOURCES),
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
    """走合法路径到 in_progress（出单唯一的合法窗口），返回 (id, 提交响应)。"""
    d = submit(client)
    did = d["id"]
    resp = transition(client, did, "in_progress", by="ops-001", note="派发台接单")
    assert resp.status_code == 200, resp.text
    return did, d


def workorder(client, demand_id: int, **body) -> dict:
    return client.post(
        f"/api/v1/demands/{demand_id}/workorder",
        json={**body} if body else {},
        headers=INTERNAL,
    )


def pending(client) -> list[dict]:
    resp = client.get("/internal/demands/workorders/pending", headers=INTERNAL)
    assert resp.status_code == 200, resp.text
    return resp.json()["pending"]


def ack(client, ticket_path: str) -> dict:
    return client.post(
        "/internal/demands/workorders/ack",
        json={"ticket_path": ticket_path},
        headers=INTERNAL,
    )


def internal_detail(client, demand_id: int) -> dict:
    resp = client.get(f"/internal/demands/{demand_id}", headers=INTERNAL)
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_demand(**overrides) -> Demand:
    """直构需求单（纯函数入参，不落库）：映射测试的固定输入。"""
    fields = {
        "code": "DE-20261011-07",
        "customer_id": "customer-abcdef-123",
        "title": "上海乘用车销量需求",
        "intent": "需要上海市 2024 年以来乘用车月度销量与价格带分布，用于市场分析",
        "contact_email": "buyer@example.com",
        "company": "上海某某咨询",
        "entities_hint": {"行业": "汽车", "地域": "上海"},
        "frequency_expect": "monthly",
        "source_urls": [VALID_SOURCES[0]],
    }
    fields.update(overrides)
    return Demand(**fields)


def top_level_keys(yaml_text: str) -> list[str]:
    """YAML 文本的一级字段序（无缩进 key 行，按出现顺序）。"""
    return [m.group(1) for m in re.finditer(r"^([a-z_]+):", yaml_text, re.M)]


# ------------------------------------------------- 映射确定性与字段序（纯函数）


def test_build_workorder_deterministic_with_stable_field_order():
    demand = make_demand()
    yaml1, path1 = build_workorder(demand)
    yaml2, path2 = build_workorder(demand)
    # 出单时刻如实入单：两次调用仅 created_at 漂移，其余字节级一致
    d1, d2 = yaml.safe_load(yaml1), yaml.safe_load(yaml2)
    d1.pop("created_at"), d2.pop("created_at")
    assert d1 == d2
    assert d1["ticket_id"] == d2["ticket_id"]  # 同日稳定
    assert path1 == path2

    # 字段序稳定（舰队 new_ticket canonical + health-loop 契约形态）
    assert top_level_keys(yaml1) == [
        "ticket_id", "source", "unit", "kind", "created_at", "category",
        "brief", "golden", "verify", "evidence", "lifecycle",
    ]
    data = yaml.safe_load(yaml1)
    assert list(data) == [
        "ticket_id", "source", "unit", "kind", "created_at", "category",
        "brief", "golden", "verify", "evidence", "lifecycle",
    ]
    assert list(data["brief"]) == ["source_urls", "expectations", "cadence", "notes"]

    # 舰队 validate_ticket 必填七字段契约（wire-workorder-ticket-conformance）
    for field in ("ticket_id", "source", "unit", "created_at", "evidence", "verify", "lifecycle"):
        assert field in data and data[field] is not None and data[field] != ""
    assert data["ticket_id"] == path1.split("/")[-1][: -len(".yaml")]
    assert data["source"] == "wire"
    assert data["lifecycle"] == []
    assert isinstance(data["evidence"], dict) and isinstance(data["verify"]["commands"], list)

    # 映射断言（design D3 表）
    assert data["kind"] == "generate"
    assert data["category"] is None
    assert data["unit"] == f"spiders/{STATS_SLUG}/"
    assert data["brief"]["source_urls"] == [VALID_SOURCES[0]]  # 原样
    assert demand.intent in data["brief"]["expectations"]
    assert "实体提示" in data["brief"]["expectations"]
    assert data["brief"]["cadence"] == "daily"  # monthly 未映射 → 缺省
    assert "monthly" in data["brief"]["notes"]  # 原文并入 notes
    assert f"wire 需求单 {demand.code}" in data["brief"]["notes"]
    assert "客户代号 customer" in data["brief"]["notes"]  # customer_id 前 8 位
    assert data["golden"] == [f"spiders/{STATS_SLUG}/golden/"]
    assert data["verify"]["commands"] == [
        f"python3 scripts/health_verify.py --ticket {path1}"
    ]
    assert path1 == (
        f"reports/health-tickets/{datetime.now(timezone.utc):%Y%m%d}"
        "-wire-DE-20261011-07.yaml"
    )


def test_build_workorder_evidence_redaction():
    """evidence 脱敏：只留需求单编码+客户代号+源地址+声明，绝无邮箱/公司名。"""
    demand = make_demand()
    yaml_text, _ = build_workorder(demand)
    evidence = yaml.safe_load(yaml_text)["evidence"]
    assert evidence["demand_code"] == demand.code
    assert evidence["customer_code"] == demand.customer_id[:8]
    assert evidence["source_urls"] == [VALID_SOURCES[0]]
    assert evidence["public_data_asserted"] is bool(demand.public_data_asserted)
    raw = yaml.safe_dump(evidence, allow_unicode=True)
    assert "buyer@example.com" not in raw
    assert "上海某某咨询" not in raw


@pytest.mark.parametrize(
    ("frequency", "cadence", "in_notes"),
    [
        ("daily", "daily", False),
        ("hourly", "hourly", False),
        ("weekly", "weekly", False),
        (None, "daily", False),   # 旧单无频率 → 缺省日更
        ("每季度", "daily", True),  # 非法值 → 缺省 + 原文并入 notes
    ],
)
def test_cadence_mapping(frequency, cadence, in_notes):
    demand = make_demand(frequency_expect=frequency)
    yaml_text, _ = build_workorder(demand)
    data = yaml.safe_load(yaml_text)
    assert data["brief"]["cadence"] == cadence
    if frequency:
        # 未映射频率原文并入 notes；已映射值不重复入 notes
        assert ("每季度" in data["brief"]["notes"]) is in_notes


# ------------------------------------------------- slug 派生与冲突探测（纯函数）


def test_derive_slug_sanitization_rules():
    assert derive_slug("https://www.stats.gov.cn/sj/zxfb/") == "stats-gov-cn"
    assert derive_slug("http://www.caam.org.cn/chn/4/cate_30/list_1.html") == "caam-org-cn"
    assert derive_slug("https://Data.Example.COM/x") == "data-example-com"
    assert derive_slug("https://www.a--b.cn/") == "a-b-cn"  # 折叠连续 -
    assert derive_slug("https://x.cn/", override="My_Custom.Unit") == "my-custom-unit"
    long_host = "a" * 60 + ".cn"
    assert derive_slug(f"https://{long_host}/") == "a" * 40  # 截 40 字符
    with pytest.raises(ValueError, match="主机名"):
        derive_slug("not a url at all")
    with pytest.raises(ValueError, match="主机名"):
        derive_slug("")


def test_check_slug_free_raw_probe_outcomes():
    def probe(handler) -> bool:
        client_ = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            return check_slug_free(STATS_SLUG, http_client=client_)
        finally:
            client_.close()

    # 三态契约：404=free / 200 等 2xx=occupied / 5xx 与网络错=unverified
    # （fd-prod 海外出站挡死 raw 通道——fail-closed 会误伤全部出单，
    # 占用复检在舰队拉取器本地 checkout 权威执行）
    assert probe(lambda req: httpx.Response(404, text="missing")) == "free"
    assert probe(lambda req: httpx.Response(200, text="manifest")) == "occupied"
    assert probe(lambda req: httpx.Response(503, text="down")) == "unverified"

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert probe(unreachable) == "unverified"
    # 非法 slug（路径注入形态）本地即拒，不进 URL
    assert check_slug_free("foo/bar", http_client=httpx.Client()) == "occupied"


# ------------------------------------------------- 端点：状态门与身份门


def test_workorder_endpoints_require_internal_token(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    for headers in ({}, {"X-Internal-Token": "wrong"}):
        resp = harness.client.post(
            f"/api/v1/demands/{did}/workorder", json={}, headers=headers
        )
        assert resp.status_code == 401, resp.text
        assert harness.client.get(
            "/internal/demands/workorders/pending", headers=headers
        ).status_code == 401
        assert harness.client.post(
            "/internal/demands/workorders/ack",
            json={"ticket_path": "reports/health-tickets/x.yaml"},
            headers=headers,
        ).status_code == 401


def test_workorder_rejects_non_in_progress(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    assert transition(harness.client, did, "delivered", by="ops-001").status_code == 200
    resp = workorder(harness.client, did)
    assert resp.status_code == 409
    assert "in_progress" in resp.json()["detail"]
    # 终态零事件写入（出单未发生）
    assert internal_detail(harness.client, did)["pipeline_events"] == []

    # submitted 窗口同样拒（出单属于处理期）
    fresh = submit(harness.client)
    resp = workorder(harness.client, fresh["id"])
    assert resp.status_code == 409
    assert "in_progress" in resp.json()["detail"]
    assert internal_detail(harness.client, fresh["id"])["pipeline_events"] == []


def test_workorder_legacy_demand_without_source_urls_422(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    with harness.Session() as s:  # 存量旧单形态：source_urls=None
        row = s.get(Demand, did)
        row.source_urls = None
        s.commit()
    resp = workorder(harness.client, did)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "source_urls" in detail and "存量" in detail
    assert internal_detail(harness.client, did)["pipeline_events"] == []


def test_workorder_slug_conflict_rejected(harness, monkeypatch):
    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug, **kw: "occupied")
    did, _ = at_in_progress(harness.client)
    resp = workorder(harness.client, did)
    assert resp.status_code == 409, resp.text
    assert "转人工" in resp.json()["detail"]
    assert STATS_SLUG in resp.json()["detail"]
    assert internal_detail(harness.client, did)["pipeline_events"] == []


def test_workorder_slug_override(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    resp = workorder(harness.client, did, slug="My_Custom.Unit")
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(resp.json()["ticket_yaml"])
    assert data["unit"] == "spiders/my-custom-unit/"
    assert data["golden"] == ["spiders/my-custom-unit/golden/"]


# ------------------------------------------------- 端点：脱敏红线 + 留档


def test_workorder_redaction_no_email_no_company(harness, slug_free):
    did, submitted = at_in_progress(harness.client)
    resp = workorder(harness.client, did)
    assert resp.status_code == 200, resp.text
    ticket_yaml = resp.json()["ticket_yaml"]
    assert "buyer@example.com" not in ticket_yaml       # contact_email 绝不入单
    assert "@" not in ticket_yaml
    assert "上海某某咨询" not in ticket_yaml            # 公司名不入单
    assert "客户代号 cust-001" in ticket_yaml           # 只留 customer_id 前 8 位
    assert submitted["intent"] in ticket_yaml           # 意图原文保留

    # 留档全文与哈希（append-only；出单不迁移状态、时间线不动）
    body = internal_detail(harness.client, did)
    event = body["pipeline_events"][-1]
    assert event["type"] == "workorder_exported"
    assert event["ticket_yaml"] == ticket_yaml
    assert event["ticket_path"] == resp.json()["ticket_path"]
    assert event["sha256"] == hashlib.sha256(ticket_yaml.encode("utf-8")).hexdigest()
    assert body["status"] == "in_progress"
    assert [h["status"] for h in body["status_history"]] == ["submitted", "in_progress"]


# ------------------------------------------------- 诚实降级（拉取器未部署/无 ack）


def test_export_honest_degradation_and_zero_git(harness, slug_free, no_git):
    """拉取器未部署（无 ack）：出单照常、全文随响应、无投递成功假象、
    零 git/subprocess 调用（design D6：wire 零外部 git 凭据）。"""
    did, _ = at_in_progress(harness.client)
    resp = workorder(harness.client, did)
    assert resp.status_code == 200, resp.text  # no_git 假件未被触发
    body = resp.json()
    assert body["ticket_yaml"]                    # 全文恒在（人工推送降级可用）
    assert body["ticket_path"].startswith("reports/health-tickets/")
    assert body["delivered"] is False             # 未投递——绝不假装自动成功
    assert body["queued_for_pull"] is True        # 已入队等待舰队拉取器
    events = internal_detail(harness.client, did)["pipeline_events"]
    assert [e["type"] for e in events] == ["workorder_exported"]  # 无 pipeline_run 假象


# ------------------------------------------------- 舰队拉取通道（pending / ack）


def test_pending_lists_latest_unacked_in_progress_only(harness, slug_free):
    exported, exported_body = at_in_progress(harness.client)  # A：已出单待拉取
    untouched, _ = at_in_progress(harness.client)             # B：未出单
    acked, _ = at_in_progress(harness.client)                 # C：已出单且已 ack
    delivered, _ = at_in_progress(harness.client)             # D：出单后已交付

    workorder(harness.client, exported)
    acked_path = workorder(harness.client, acked).json()["ticket_path"]
    assert ack(harness.client, acked_path).status_code == 200
    workorder(harness.client, delivered)
    assert transition(harness.client, delivered, "delivered", by="ops-001").status_code == 200

    # 重出新单取代旧单：每单至多一条最新导出
    a2 = workorder(harness.client, exported).json()

    entries = pending(harness.client)
    # B 无导出、C 已 ack、D 已迁出 in_progress——只有 A 在队
    assert [e["demand_id"] for e in entries] == [exported]
    entry = entries[0]
    assert entry["code"] == exported_body["code"]           # 路径可追溯到需求单
    assert entry["ticket_path"] == a2["ticket_path"]        # 最新一条（同日重出=同路径）
    assert entry["ticket_yaml"] == a2["ticket_yaml"]
    assert entry["sha256"] == hashlib.sha256(a2["ticket_yaml"].encode()).hexdigest()
    assert entry["exported_at"]

    # 未 ack 前 pending 稳定可见（拉取器按此清单落仓自推）
    assert [e["demand_id"] for e in pending(harness.client)] == [exported]


def test_ack_records_pipeline_run_and_empties_pending(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    exported = workorder(harness.client, did).json()
    ticket_path = exported["ticket_path"]
    assert [e["demand_id"] for e in pending(harness.client)] == [did]

    resp = ack(harness.client, ticket_path)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    filename = ticket_path.rsplit("/", 1)[-1]
    assert body == {
        "demand_id": did,
        "ticket_path": ticket_path,
        "pipeline_run_id": filename,
        "acked": True,
        "already_acked": False,
    }

    # 事件留档：workorder_exported → pipeline_run（含可追溯路径）
    events = internal_detail(harness.client, did)["pipeline_events"]
    assert [e["type"] for e in events] == ["workorder_exported", "pipeline_run"]
    run = events[-1]
    assert run["pipeline_run_id"] == filename
    assert run["repo"] == "fd-industry-data"
    assert run["branch"] == "main"
    assert run["note"] == "workorder delivered"
    assert run["pr_url"] is None
    # 投递事实不迁移状态（编排绝不自动置 delivered）
    assert internal_detail(harness.client, did)["status"] == "in_progress"
    assert pending(harness.client) == []  # 已 ack 出队


def test_ack_idempotent(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    ticket_path = workorder(harness.client, did).json()["ticket_path"]

    first = ack(harness.client, ticket_path)
    assert first.status_code == 200 and first.json()["already_acked"] is False
    snapshot = internal_detail(harness.client, did)["pipeline_events"]

    second = ack(harness.client, ticket_path)  # 重复 ack：幂等
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["acked"] is True
    assert body["already_acked"] is True
    assert body["pipeline_run_id"] == first.json()["pipeline_run_id"]
    # 事件零追加（同工单重复 ack 不重复记事件）
    assert internal_detail(harness.client, did)["pipeline_events"] == snapshot
    assert pending(harness.client) == []


def test_ack_after_same_day_reexport_requeues(harness, slug_free):
    """同日重出新单（同路径新导出）不吃旧 ack：重新入队、再 ack 如实记
    第二次投递事实。"""
    did, _ = at_in_progress(harness.client)
    path1 = workorder(harness.client, did).json()["ticket_path"]
    assert ack(harness.client, path1).status_code == 200
    assert pending(harness.client) == []

    path2 = workorder(harness.client, did).json()["ticket_path"]
    assert path2 == path1  # 同日重出=同路径（确定性映射）
    assert [e["demand_id"] for e in pending(harness.client)] == [did]  # 重新入队

    again = ack(harness.client, path2)
    assert again.status_code == 200
    assert again.json()["already_acked"] is False  # 新导出的首次 ack
    events = internal_detail(harness.client, did)["pipeline_events"]
    assert [e["type"] for e in events] == [
        "workorder_exported", "pipeline_run", "workorder_exported", "pipeline_run",
    ]
    assert pending(harness.client) == []


def test_ack_unknown_ticket_404(harness, slug_free):
    did, _ = at_in_progress(harness.client)
    assert workorder(harness.client, did).status_code == 200
    for unknown in (
        "reports/health-tickets/20990101-wire-DE-20990101-99.yaml",  # 形态合法但无留档
        "reports/health-tickets/not-a-ticket.md",                    # 形态不合契约
        "spiders/foo/manifest.yaml",                                 # tickets 目录外
    ):
        resp = ack(harness.client, unknown)
        assert resp.status_code == 404, unknown
    # 原有留档不受影响
    assert len(pending(harness.client)) == 1
