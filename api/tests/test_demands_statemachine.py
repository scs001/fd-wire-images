"""需求单三态状态机测试（wire-ux-consolidation 任务 3.1，自八态压缩）。

覆盖：
- 合法迁移全集：submitted→in_progress、in_progress→delivered、
  submitted/in_progress→rejected（必带 reason）
- 迁移表穷举：全部枚举成员 × 全部目标（含五个废除态作当前态的读窗口行），
  白名单外一律 409
- 废除态迁入 409（如实回显）；rejected 缺 reason 409
- 轻量表单（3.2）：必填逐字段 422、邮箱形态校验、目录锚点原样存储、
  摘要自动生成
- 历史不可改：只追加，前缀逐字节稳定，409 尝试不落历史
- 客户侧企业隔离 + 内部 token 鉴权 + 客户响应无废除态名
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

# 在导入 app 前把默认引擎指向内存库：TestClient 启动钩子里的
# init_demands_db() 不至于在测试机上落下 fd_wire_demands.db 文件。
# wire-dedicated-postgres 起 FD_WIRE_DATABASE_URL 升为元数据库收敛开关
# （全平台模块最高优先级）：只在 app 导入期间生效，导入完即还原，避免
# 「内存库」泄漏进同会话其余模块的 URL 解析。
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
from fd_wire_api.demands import DemandStatus, get_db
from fd_wire_api.demands.db import Base
from fd_wire_api.demands.models import Demand
from fd_wire_api.demands.statemachine import ALLOWED_TRANSITIONS, LEGACY_STATUSES

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
SUB_CUST = "sub-cust-001"
SUB_OTHER = "sub-cust-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

ALL_STATUSES = list(DemandStatus)
CODE_RE = re.compile(r"^DE-\d{8}-\d{2,}$")


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """内存库 + 覆盖 get_db 的 TestClient + 直写会话（种子废除态行用）。"""
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
    make_console_env(
        tmp_path, monkeypatch,
        customers=[(SUB_CUST, "cust-001"), (SUB_OTHER, "cust-002")],
    )
    with TestClient(app) as test_client:
        yield SimpleNamespace(client=test_client, Session=TestingSession)
    app.dependency_overrides.clear()


@pytest.fixture()
def client(harness):
    return harness.client


# ------------------------------------------------------------------ helpers


def submit(client, customer="cust-001", **overrides) -> dict:
    payload = {
        "description": "需要上海市 2024 年以来乘用车月度销量与价格带分布，用于市场分析",
        "contact_email": "buyer@example.com",
        "company": "上海某某咨询",
        "indicator_code": "IND-AUTO-014",
        # 合规门两入参（wire-demand-generation-orchestration 1.1 起必填）
        "source_urls": ["https://www.stats.gov.cn/sj/zxfb/"],
        "public_data_asserted": True,
    }
    payload.update(overrides)
    resp = client.post(
        "/api/v1/demands", json=payload,
        headers=CUST if customer == "cust-001" else OTHER,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def transition(client, demand_id: int, target: str, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/transition",
        json={"target_status": target, **extra},
        headers=INTERNAL,
    )


def demand_at(harness, target: DemandStatus) -> int:
    """走合法路径把一条需求单推进到指定状态（废除态直写种子），返回 id。"""
    if target in LEGACY_STATUSES:
        seq = sorted(s.value for s in LEGACY_STATUSES).index(target.value)
        with harness.Session() as db:
            demand = Demand(
                code=f"DE-19990101-{seq:02d}",
                customer_id="cust-001",
                title="存量废除态种子行",
                intent="旧八态工作流遗留的单子（读窗口模拟）",
                status=target.value,
                status_history=[],
            )
            db.add(demand)
            db.commit()
            db.refresh(demand)
            return demand.id
    d = submit(harness.client)
    did = d["id"]
    if target is DemandStatus.submitted:
        return did
    if target is DemandStatus.in_progress:
        assert transition(harness.client, did, "in_progress", by="ops-001").status_code == 200
        return did
    if target is DemandStatus.delivered:
        assert transition(harness.client, did, "in_progress", by="ops-001").status_code == 200
        assert transition(harness.client, did, "delivered", by="ops-001").status_code == 200
        return did
    assert target is DemandStatus.rejected
    assert transition(
        harness.client, did, "rejected", by="ops-001", reason="合规风险：涉个人敏感信息"
    ).status_code == 200
    return did


def get_detail(client, demand_id: int, customer="cust-001") -> dict:
    resp = client.get(
        f"/api/v1/demands/{demand_id}", headers=CUST if customer == "cust-001" else OTHER
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------- 合法迁移全集走通


def test_happy_path_three_state_lifecycle(client):
    d = submit(client)  # 四入参：描述/邮箱/公司/锚点
    assert CODE_RE.match(d["code"])
    assert d["status"] == "submitted"
    assert d["contact_email"] == "buyer@example.com"
    assert d["company"] == "上海某某咨询"
    assert d["linked_indicators"] == ["IND-AUTO-014"]  # 锚点原样存储
    assert [e["status"] for e in d["status_history"]] == ["submitted"]

    did = d["id"]

    # 派发接单：submitted → in_progress
    d = transition(client, did, "in_progress", by="ops-001", note="派发台接单").json()
    assert d["status"] == "in_progress"

    # 处理完成：in_progress → delivered
    d = transition(client, did, "delivered", by="ops-001", note="数据已交付").json()
    assert d["status"] == "delivered"

    # 客户视角：时间线与状态序列
    detail = get_detail(client, did)
    timeline = [e["status"] for e in detail["status_history"]]
    assert timeline == ["submitted", "in_progress", "delivered"]
    ats = [e["at"] for e in detail["status_history"]]
    assert ats == sorted(ats)


def test_reject_from_submitted_and_in_progress(client):
    """×→rejected（带理由）两例：拒绝理由留档客户可见，终态不可再迁移。"""
    # submitted → rejected
    d = submit(client)
    did = d["id"]
    d = transition(
        client, did, "rejected", by="ops-001", reason="数据源不存在且无可替代源"
    ).json()
    assert d["status"] == "rejected"
    detail = get_detail(client, did)
    assert detail["rejection_reason"] == "数据源不存在且无可替代源"
    assert detail["status_history"][-1]["status"] == "rejected"
    assert "数据源不存在" in detail["status_history"][-1]["note"]

    # in_progress → rejected
    d2 = submit(client)
    did2 = d2["id"]
    assert transition(client, did2, "in_progress", by="ops-001").status_code == 200
    d2 = transition(client, did2, "rejected", by="ops-002", reason="处理中发现合规风险").json()
    assert d2["status"] == "rejected"
    assert get_detail(client, did2)["rejection_reason"] == "处理中发现合规风险"

    # 终态不可再迁移（合法目标也不行）
    for target in ("in_progress", "delivered", "rejected"):
        assert transition(client, did, target).status_code == 409


# ------------------------------------------------------- 迁移表穷举（409）


@pytest.mark.parametrize("current", ALL_STATUSES)
@pytest.mark.parametrize("target", ALL_STATUSES)
def test_transition_table_exhaustive(harness, current, target):
    """白名单内 → 200；白名单外（含废除态作目标/当前态）→ 409。"""
    did = demand_at(harness, current)
    resp = transition(
        harness.client,
        did,
        target.value,
        by="probe",
        note="探测",
        reason="探测用拒绝理由",  # 前置给足，409 只可能来自白名单
    )
    allowed = target in ALLOWED_TRANSITIONS.get(current, frozenset())
    if allowed:
        assert resp.status_code == 200, resp.text
        assert get_detail(harness.client, did)["status"] == target.value
    else:
        assert resp.status_code == 409, resp.text


def test_legacy_targets_rejected_with_honest_detail(client):
    """五个废除态迁入一律 409，且错误信息如实回显（服务端为唯一裁决面）。"""
    did = submit(client)["id"]
    for legacy in sorted(s.value for s in LEGACY_STATUSES):
        resp = transition(client, did, legacy, by="probe")
        assert resp.status_code == 409, f"迁入 {legacy} 不应被接受"
        assert legacy in resp.json()["detail"]  # 如实回显目标态
        assert "废除" in resp.json()["detail"]
    assert get_detail(client, did)["status"] == "submitted"  # 状态不动

    # in_progress 起点同样全部 409
    assert transition(client, did, "in_progress", by="ops-001").status_code == 200
    for legacy in sorted(s.value for s in LEGACY_STATUSES):
        assert transition(client, did, legacy, by="probe").status_code == 409
    assert get_detail(client, did)["status"] == "in_progress"


def test_legacy_current_state_transitions_409(harness):
    """读窗口（存量未归并的废除态行）：任何迁移 409 而非 500。"""
    for legacy in LEGACY_STATUSES:
        did = demand_at(harness, legacy)
        for target in ("in_progress", "delivered", "rejected"):
            resp = transition(
                harness.client, did, target, by="probe", reason="读窗口探测"
            )
            assert resp.status_code == 409, resp.text
            assert "废除态" in resp.json()["detail"]


# ------------------------------------------------------- rejected 必带理由


def test_rejected_requires_reason(client):
    did = submit(client)["id"]
    for bad in (None, "", "   "):
        resp = transition(client, did, "rejected", reason=bad)
        assert resp.status_code == 409, f"reason={bad!r} 不应被接受"
    assert get_detail(client, did)["status"] == "submitted"

    d = transition(client, did, "rejected", reason="合规风险：涉个人敏感信息").json()
    assert d["rejection_reason"] == "合规风险：涉个人敏感信息"


# ------------------------------------------------------- 轻量表单校验（3.2）


def test_form_required_fields_per_field_422(client):
    """必填缺失逐字段报 422；不产生需求单。"""
    # 缺需求描述
    resp = client.post(
        "/api/v1/demands",
        json={"contact_email": "buyer@example.com"},
        headers=CUST,
    )
    assert resp.status_code == 422
    assert any(tuple(e["loc"]) == ("body", "description") for e in resp.json()["detail"])

    # 缺联系邮箱
    resp = client.post(
        "/api/v1/demands",
        json={"description": "需要航运运价日度数据"},
        headers=CUST,
    )
    assert resp.status_code == 422
    assert any(tuple(e["loc"]) == ("body", "contact_email") for e in resp.json()["detail"])

    # 两者都缺：逐字段都在案
    resp = client.post("/api/v1/demands", json={}, headers=CUST)
    assert resp.status_code == 422
    locs = {tuple(e["loc"]) for e in resp.json()["detail"]}
    assert ("body", "description") in locs
    assert ("body", "contact_email") in locs

    # 空白描述 / 邮箱形态不合法：同样逐字段 422
    resp = client.post(
        "/api/v1/demands",
        json={"description": "   ", "contact_email": "buyer@example.com"},
        headers=CUST,
    )
    assert resp.status_code == 422
    assert any(tuple(e["loc"]) == ("body", "description") for e in resp.json()["detail"])

    resp = client.post(
        "/api/v1/demands",
        json={"description": "需要煤炭价格数据", "contact_email": "not-an-email"},
        headers=CUST,
    )
    assert resp.status_code == 422
    assert any(tuple(e["loc"]) == ("body", "contact_email") for e in resp.json()["detail"])

    # 列表为空：没落任何单
    assert client.get("/api/v1/demands", headers=CUST).json() == []


def test_form_optional_fields_and_summary_title(client):
    """公司名/锚点选填：最小提交可行；描述生成单行摘要。"""
    d = submit(client, company=None, indicator_code=None)
    assert d["company"] is None
    assert d["linked_indicators"] == []

    long_description = "需要全国光伏装机\n月度数据，" + "口径明细。" * 60
    d = submit(client, description=long_description)
    assert d["intent"] == long_description            # 全文入 intent
    assert "\n" not in d["title"]                     # 摘要压成单行
    assert len(d["title"]) <= 200                     # 截断 200
    assert d["title"].startswith("需要全国光伏装机 月度数据")

    # 旧表单字段（title/entities_hint/frequency_expect）不再收：缺新必填报 422
    resp = client.post(
        "/api/v1/demands",
        json={"title": "旧调用", "intent": "旧字段", "entities_hint": {}, "frequency_expect": "daily"},
        headers=CUST,
    )
    assert resp.status_code == 422
    locs = {tuple(e["loc"]) for e in resp.json()["detail"]}
    assert ("body", "description") in locs and ("body", "contact_email") in locs


# ------------------------------------------------------- 历史不可改


def test_history_is_append_only(client):
    did = submit(client)["id"]
    assert transition(client, did, "in_progress", by="ops-001").status_code == 200
    snapshot = get_detail(client, did)["status_history"]

    # 后续合法迁移：只追加，前缀逐字节不变
    assert transition(
        client, did, "rejected", by="ops-002", reason="处理中撤单"
    ).status_code == 200
    after = get_detail(client, did)["status_history"]
    assert after[: len(snapshot)] == snapshot
    assert len(after) == len(snapshot) + 1

    # 非法迁移（409）：历史完全不动
    again = get_detail(client, did)["status_history"]
    assert transition(client, did, "in_progress").status_code == 409
    assert get_detail(client, did)["status_history"] == again


# ------------------------------------------------------- 鉴权与企业隔离


def test_customer_scoping_and_auth(client):
    d_a = submit(client, customer="cust-001")
    submit(client, customer="cust-002")

    # 各自列表隔离，且带当前状态与时间线
    resp = client.get("/api/v1/demands", headers=CUST)
    assert resp.status_code == 200
    items = resp.json()
    assert [i["id"] for i in items] == [d_a["id"]]
    assert items[0]["status"] == "submitted"
    assert items[0]["status_history"][0]["status"] == "submitted"

    # 他企详情一律 404（不泄露存在性）
    resp = client.get(f"/api/v1/demands/{d_a['id']}", headers=OTHER)
    assert resp.status_code == 404

    # 缺 Bearer → 401
    assert client.get("/api/v1/demands").status_code == 401
    assert client.get(f"/api/v1/demands/{d_a['id']}").status_code == 401

    # 内部端点：缺/错 token → 401
    for headers in ({}, {"X-Internal-Token": "wrong"}):
        resp = transition_with_headers(client, d_a["id"], "in_progress", headers)
        assert resp.status_code == 401

    # 状态筛选（?status=，三态值生效；废除态值只回空集，不出现旧态名）
    resp = client.get("/api/v1/demands", params={"status": "submitted"}, headers=CUST)
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()] == [d_a["id"]]
    resp = client.get("/api/v1/demands", params={"status": "in_progress"}, headers=CUST)
    assert resp.status_code == 200
    assert resp.json() == []


def transition_with_headers(client, demand_id: int, target: str, headers: dict):
    return client.post(
        f"/internal/demands/{demand_id}/transition",
        json={"target_status": target, "reason": "r"},
        headers=headers,
    )


# ------------------------------------------------------- 客户响应无旧态名


def test_customer_responses_contain_no_legacy_status_names(client):
    """客户面列表/详情的 JSON 原文不含五个废除态名（3.2）。"""
    d1 = submit(client)
    did = d1["id"]
    assert transition(client, did, "in_progress", by="ops-001").status_code == 200
    assert transition(client, did, "delivered", by="ops-001").status_code == 200
    submit(client)

    for resp in (
        client.get("/api/v1/demands", headers=CUST),
        client.get(f"/api/v1/demands/{did}", headers=CUST),
        client.get(f"/api/v1/demands/{d1['id']}", headers=CUST),
    ):
        assert resp.status_code == 200
        for legacy in (s.value for s in LEGACY_STATUSES):
            assert legacy not in json.dumps(resp.json(), ensure_ascii=False)


# ------------------------------------------------------- 编码序列


def test_daily_code_sequence(client):
    d1 = submit(client)
    d2 = submit(client)
    assert d1["code"] != d2["code"]
    day_seq = [int(c.rsplit("-", 1)[1]) for c in (d1["code"], d2["code"])]
    assert day_seq[1] == day_seq[0] + 1
