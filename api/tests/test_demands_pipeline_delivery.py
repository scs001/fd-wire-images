"""需求单流水线衔接与私有 scope 交付测试（wire-platform-v1 任务 6.5+6.6，
wire-ux-consolidation 3.1 三态化适配）。

覆盖：
- 流水线事实 append-only、仅 in_progress 可记录（三态化后的观测挂点）、
  内部视图透出 events 与 gate 摘要
- gate 结果：append-only 观测留档（passed/fail 都只记录，不再门禁状态迁移）
- scope 客户端：HttpScopeClient 全操作形状（httpx.MockTransport，含鉴权头、
  rules 载荷、读改合并、publish/unpublish、错误路径）与空规则拒绝
- 交付动作：deliver 自 in_progress 迁 delivered 并自动建客户私有 scope
  （命名 demand-<code>、summary 含摘要）；失败→scope_pending+重试成功；
  transition 到 delivered 自动交付；未配置客户端如实挂起
- 授权转公开：granted=true 调 publish、撤回调 unpublish，append-only 留档，
  客户视图可见授权状态且不含内部细节
- 完整闭环：提交→in_progress（pipeline event）→gate fail→重跑 pass→
  deliver（scope 建）→delivered→授权转公开→撤回
- alembic 0008/0019：demands 表流水线/交付列与联系方式列，链头唯一

真实 gate 命令（python3 fd-industry-data/scripts/conformance_gate.py）在外部
仓——本文件绝不代跑，只消费结果载荷。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（与 test_demands_statemachine 同款纪律；
# wire-dedicated-postgres 起该 env 升为元数据库收敛开关——只在导入期间生效，
# 导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]
from fd_wire_api.demands import DemandStatus, get_db
from fd_wire_api.demands.db import Base
from fd_wire_api.delivery.scope_client import (
    HttpScopeClient,
    ScopeClientError,
    build_rules,
    scope_client_dep,
)

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-cust-001"
SUB_OTHER = "sub-cust-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

API_DIR = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立的内存库 + 覆盖 get_db 的 TestClient（外加平台库身份种子）。"""
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
        yield test_client
    app.dependency_overrides.clear()


class MockScopeClient:
    """满足 ScopeClient 协议的内存 mock：记录调用形状，可注入建 scope 失败。"""

    def __init__(self, *, fail_create: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail_create = fail_create
        self.scopes: dict[str, dict] = {}

    def create_scope(self, name, summary, source_dbs=(), *, semantic_codes=()):
        self.calls.append(
            ("create_scope", name, summary, tuple(source_dbs), tuple(semantic_codes))
        )
        if self.fail_create:
            raise ScopeClientError("scope API 500：内部错误", status_code=500)
        scope = {
            "name": name,
            "description": summary,
            "rules": {
                "source_dbs": sorted(set(source_dbs)),
                "domains": [],
                "semantic_codes": sorted(set(semantic_codes)),
                "native_codes": [],
            },
            "matched": len(set(semantic_codes)),
        }
        self.scopes[name] = scope
        return dict(scope)

    def grant_indicator(self, *, scope_name, indicator_code):
        self.calls.append(("grant_indicator", scope_name, indicator_code))
        scope = self.scopes[scope_name]
        codes = list(scope["rules"]["semantic_codes"])
        if indicator_code not in codes:
            codes.append(indicator_code)
            scope["rules"]["semantic_codes"] = sorted(codes)
        return dict(scope)

    def list_scopes(self):
        self.calls.append(("list_scopes",))
        return [dict(s) for s in self.scopes.values()]

    def publish_indicators(self, indicator_codes):
        self.calls.append(("publish", tuple(indicator_codes)))
        return {"published": list(indicator_codes)}

    def unpublish_indicators(self, indicator_codes):
        self.calls.append(("unpublish", tuple(indicator_codes)))
        return {"unpublished": list(indicator_codes)}

    def calls_of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture()
def scope_mock():
    """注入 mock scope 客户端（依赖覆盖），并返回 mock 供断言调用形状。"""
    mock = MockScopeClient()
    app.dependency_overrides[scope_client_dep] = lambda: mock
    return mock


@pytest.fixture()
def migrated_demands_sqlite(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade head（DATABASE_URL 指路），返回连接串。"""
    db_url = f"sqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "head")
    return db_url


# ------------------------------------------------------------------ helpers


def submit(client, customer="cust-001") -> dict:
    payload = {
        "description": "需要上海市 2024 年以来乘用车月度销量与价格带分布，用于市场分析",
        "contact_email": "buyer@example.com",
        "company": "上海某某咨询",
        "indicator_code": "IND-AUTO-014",
        # 合规门两入参（wire-demand-generation-orchestration 1.1 起必填）
        "source_urls": ["https://www.stats.gov.cn/sj/zxfb/"],
        "public_data_asserted": True,
    }
    resp = client.post(
        "/api/v1/demands", json=payload, headers=CUST if customer == "cust-001" else OTHER
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def transition(client, demand_id: int, target: str, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/transition",
        json={"target_status": target, **extra},
        headers=INTERNAL,
    )


def gate_result(client, demand_id: int, passed: bool, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/gate-result",
        json={"passed": passed, **extra},
        headers=INTERNAL,
    )


def pipeline_run(client, demand_id: int, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/pipeline",
        json={"pipeline_run_id": "run-001", **extra},
        headers=INTERNAL,
    )


def deliver(client, demand_id: int, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/deliver",
        json={"by": "customer:cust-001", **extra},
        headers=INTERNAL,
    )


def publish_authorization(client, demand_id: int, granted: bool, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/publish-authorization",
        json={"granted": granted, **extra},
        headers=INTERNAL,
    )


def at_in_progress(client) -> tuple[int, dict]:
    """走合法路径到 in_progress（三态机的处理期），返回 (id, 提交响应)。"""
    d = submit(client)
    did = d["id"]
    resp = transition(client, did, "in_progress", by="ops-001", note="派发台接单")
    assert resp.status_code == 200, resp.text
    return did, d


def internal_detail(client, demand_id: int) -> dict:
    resp = client.get(f"/internal/demands/{demand_id}", headers=INTERNAL)
    assert resp.status_code == 200, resp.text
    return resp.json()


def customer_detail(client, demand_id: int) -> dict:
    resp = client.get(f"/api/v1/demands/{demand_id}", headers=CUST)
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_http_client(handler) -> HttpScopeClient:
    """基于 MockTransport 的 HttpScopeClient（鉴权头/token 固定便于断言）。"""
    return HttpScopeClient(
        base_url="http://scope-api.test",
        token="tok-123",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# ------------------------------------------------------- 6.5 流水线事实（in_progress 期）


def test_pipeline_run_recorded_append_only(client):
    did, _ = at_in_progress(client)

    resp = pipeline_run(
        client, did,
        pipeline_run_id="run-20261004-01",
        repo="fd-industry-data",
        branch="feature/de-20261004-01",
        note="scraw-builder 批量生成 + 测试舰队",
        by="ci",
    )
    assert resp.status_code == 200, resp.text
    event = resp.json()["pipeline_events"][-1]
    assert event["type"] == "pipeline_run"
    assert event["pipeline_run_id"] == "run-20261004-01"
    assert event["repo"] == "fd-industry-data"
    assert event["branch"] == "feature/de-20261004-01"
    assert event["by"] == "ci"
    assert "at" in event

    # append-only：后续事件只追加，前缀逐字节稳定；409 尝试不动事件
    snapshot = internal_detail(client, did)["pipeline_events"]
    resp = pipeline_run(client, did, pipeline_run_id="run-20261004-02")
    assert resp.status_code == 200
    after = internal_detail(client, did)["pipeline_events"]
    assert after[: len(snapshot)] == snapshot
    assert len(after) == len(snapshot) + 1

    bad = transition(client, did, "building", by="probe")  # 废除态迁入 → 409
    assert bad.status_code == 409
    assert internal_detail(client, did)["pipeline_events"] == after

    # 仅 in_progress 可记录流水线事实（submitted/delivered 均 409）
    did2 = submit(client)["id"]
    assert pipeline_run(client, did2).status_code == 409
    did3 = submit(client)["id"]
    assert transition(client, did3, "in_progress", by="ops-001").status_code == 200
    assert transition(client, did3, "delivered", by="ops-001").status_code == 200
    assert pipeline_run(client, did3).status_code == 409


def test_internal_view_exposes_pipeline_and_gate(client):
    did, _ = at_in_progress(client)
    pipeline_run(client, did, pipeline_run_id="run-1")
    gate_result(client, did, False, note="首跑未过")
    gate_result(client, did, True, note="重跑全绿")

    body = internal_detail(client, did)
    assert body["gate"] == {
        "passed": True,
        "attempts": 2,
        "pipeline_runs": 1,
        "latest": {
            "type": "gate_result",
            "passed": True,
            "report_path": None,
            "note": "重跑全绿",
            "by": "pipeline",
            "at": body["gate"]["latest"]["at"],
        },
    }
    assert len(body["pipeline_events"]) == 3  # 1 run + 2 gate


def test_pipeline_endpoints_require_internal_token(client):
    did, _ = at_in_progress(client)
    for path, payload in (
        (f"/internal/demands/{did}/pipeline", {"pipeline_run_id": "run-1"}),
        (f"/internal/demands/{did}/gate-result", {"passed": True}),
        (f"/internal/demands/{did}/deliver", {}),
        (f"/internal/demands/{did}/publish-authorization", {"granted": True}),
    ):
        for headers in ({}, {"X-Internal-Token": "wrong"}):
            resp = client.post(path, json=payload, headers=headers)
            assert resp.status_code == 401, f"{path} {headers}"
    # 缺 run id → 422（schema 校验）
    assert pipeline_run(client, did, pipeline_run_id="").status_code == 422


# ------------------------------------------------------- gate 观测留档（不再门禁）


def test_gate_results_recorded_without_gating_transitions(client):
    """三态化后 gate 结果仅观测留档：passed 与否都不影响状态迁移裁决。"""
    did, _ = at_in_progress(client)

    # gate fail：如实入档（note/report_path），状态保持 in_progress
    resp = gate_result(
        client, did, False,
        note="conformance gate 未过：2 处违规（字段命名/幂等键）",
        report_path="fd-industry-data/reports/gate-0001.txt",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["gate"]["passed"] is False
    assert body["gate"]["attempts"] == 1
    event = body["pipeline_events"][-1]
    assert event["type"] == "gate_result"
    assert event["passed"] is False
    assert event["report_path"].endswith("gate-0001.txt")
    assert event["note"].startswith("conformance gate 未过")

    # gate 尚未通过也能交付（不再门禁）：重跑通过 → 交付放行前也放行
    resp = transition(client, did, "delivered", by="ops-001", note="无 gate 记录交付")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "delivered"

    # 已交付后不可再记 gate（gate 属于处理期观测）
    resp = gate_result(client, did, True)
    assert resp.status_code == 409


# ------------------------------------------------------- 6.6 scope 客户端


def test_http_scope_client_create_scope_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"name": "demand-DE-1", "matched": 2})

    client_ = make_http_client(handler)
    out = client_.create_scope(
        "demand-DE-1", "需求单《标题》私有交付", ["src-b", "src-a", "src-a"]
    )
    assert out == {"name": "demand-DE-1", "matched": 2}
    assert seen["method"] == "POST"
    assert seen["url"] == "http://scope-api.test/scopes"
    assert seen["auth"] == "Bearer tok-123"
    assert seen["body"] == {
        "name": "demand-DE-1",
        "description": "需求单《标题》私有交付",
        "rules": {
            "source_dbs": ["src-a", "src-b"],  # 去重排序
            "domains": [],
            "semantic_codes": [],
            "native_codes": [],
        },
    }


def test_http_scope_client_grant_indicator_merges_rules():
    seen: list[dict] = []
    state = {"scopes": [{"name": "demand-DE-1", "rules": {
        "source_dbs": [], "domains": [], "semantic_codes": ["IND-A"], "native_codes": [],
    }}]}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append({
            "method": request.method,
            "url": str(request.url),
            "body": json.loads(request.content) if request.content else None,
        })
        if request.method == "GET":
            return httpx.Response(200, json=state["scopes"])
        state["scopes"][0]["rules"] = seen[-1]["body"]["rules"]
        return httpx.Response(200, json=state["scopes"][0])

    client_ = make_http_client(handler)
    client_.grant_indicator(scope_name="demand-DE-1", indicator_code="IND-B")
    patches = [s for s in seen if s["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["url"] == "http://scope-api.test/scopes/demand-DE-1"
    assert patches[0]["body"]["rules"]["semantic_codes"] == ["IND-A", "IND-B"]

    # 幂等：已在 allow-list 的指标不重复 PATCH
    client_.grant_indicator(scope_name="demand-DE-1", indicator_code="IND-A")
    assert len([s for s in seen if s["method"] == "PATCH"]) == 1

    # 未知 scope → 显式错误
    with pytest.raises(ScopeClientError, match="不存在"):
        client_.grant_indicator(scope_name="ghost", indicator_code="IND-X")


def test_http_scope_client_list_publish_and_unpublish():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append({
            "method": request.method,
            "url": str(request.url),
            "body": json.loads(request.content) if request.content else None,
        })
        if request.url.path == "/scopes":
            return httpx.Response(200, json=[{"name": "demand-DE-1", "rules": {}}])
        return httpx.Response(200, json={"ok": True})

    client_ = make_http_client(handler)
    assert client_.list_scopes() == [{"name": "demand-DE-1", "rules": {}}]
    client_.publish_indicators(["IND-A", "IND-B"])
    client_.unpublish_indicators(["IND-A"])
    posts = [(s["url"], s["body"]) for s in seen if s["method"] == "POST"]
    assert posts == [
        ("http://scope-api.test/indicators/publish", {"indicator_codes": ["IND-A", "IND-B"]}),
        ("http://scope-api.test/indicators/unpublish", {"indicator_codes": ["IND-A"]}),
    ]


def test_http_scope_client_error_paths():
    # 非 2xx → 带 status_code 的显式错误
    client_ = make_http_client(lambda req: httpx.Response(503, text="down"))
    with pytest.raises(ScopeClientError) as excinfo:
        client_.list_scopes()
    assert excinfo.value.status_code == 503

    # 2xx 但载荷是显式错误对象（_scope_error 形状）→ 同样上抛
    client_ = make_http_client(
        lambda req: httpx.Response(200, json={"error": "invalid_scope", "detail": "空 scope"})
    )
    with pytest.raises(ScopeClientError, match="invalid_scope"):
        client_.create_scope("s", "d", ["src"])

    # 网络不可达 → ScopeClientError（fail 显式，不静默）
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client_ = make_http_client(boom)
    with pytest.raises(ScopeClientError, match="不可达"):
        client_.list_scopes()

    # 空规则：本地即拒（真实 API 会拒空 scope），不发请求
    with pytest.raises(ScopeClientError, match="allow-list"):
        build_rules([], [])

    # base 未配置 → 显式错误
    bare = HttpScopeClient(base_url="", http_client=httpx.Client())
    with pytest.raises(ScopeClientError, match="未配置"):
        bare.list_scopes()


# ------------------------------------------------------- 6.6 交付动作


def test_deliver_creates_private_scope(client, scope_mock):
    did, submitted = at_in_progress(client)

    resp = deliver(client, did, note="完成交付")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"

    # scope 命名 demand-<code>，summary 含需求摘要，指标缺省用 linked_indicators
    creates = scope_mock.calls_of("create_scope")
    assert len(creates) == 1
    _, name, summary, source_dbs, codes = creates[0]
    assert name == f"demand-{submitted['code']}"
    assert submitted["title"] in summary
    assert source_dbs == ()
    assert codes == ("IND-AUTO-014",)
    assert [c[2] for c in scope_mock.calls_of("grant_indicator")] == ["IND-AUTO-014"]

    # 交付状态与留档
    assert body["scope_status"] == "ok"
    assert body["scope_name"] == name
    event = body["delivery_events"][-1]
    assert event["type"] == "scope_created"
    assert event["scope_name"] == name
    assert event["granted"] == ["IND-AUTO-014"]


def test_deliver_failure_marks_pending_then_retry_succeeds(client):
    mock = MockScopeClient(fail_create=True)
    app.dependency_overrides[scope_client_dep] = lambda: mock
    did, _ = at_in_progress(client)

    resp = deliver(client, did)
    assert resp.status_code == 200, resp.text  # 迁移照常完成
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "pending"  # 交付挂起，如实可见
    assert body["scope_name"] is None
    failed = body["delivery_events"][-1]
    assert failed["type"] == "scope_create_failed"
    assert "500" in failed["error"]

    # 重试：scope 面恢复后同一端点补课，失败留档不改写（append-only）
    mock.fail_create = False
    resp = deliver(client, did)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "ok"
    events = internal_detail(client, did)["delivery_events"]
    assert [e["type"] for e in events] == ["scope_create_failed", "scope_created"]


def test_deliver_unconfigured_client_marks_pending(client, monkeypatch):
    monkeypatch.delenv("SCOPE_API_BASE", raising=False)
    did, _ = at_in_progress(client)

    resp = deliver(client, did)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "pending"
    assert "未配置" in body["delivery_events"][-1]["error"]


def test_transition_to_delivered_auto_creates_scope(client, scope_mock):
    did, _ = at_in_progress(client)

    resp = transition(client, did, "delivered", by="ops-001", note="处理完成")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "ok"  # 自动私有 scope 交付
    code = customer_detail(client, did)["code"]
    assert scope_mock.calls_of("create_scope")[0][1] == f"demand-{code}"


def test_deliver_state_rules(client, scope_mock):
    did = submit(client)["id"]
    resp = deliver(client, did)  # submitted 直接交付 → 409
    assert resp.status_code == 409
    assert "in_progress" in resp.json()["detail"]

    did2, _ = at_in_progress(client)
    assert deliver(client, did2).status_code == 200
    resp = deliver(client, did2)  # 已交付且 scope 就绪 → 不可重复
    assert resp.status_code == 409
    assert scope_mock.calls_of("create_scope").__len__() == 1


# ------------------------------------------------------- 6.6 授权转公开


def test_publish_authorization_grant_and_revoke(client, scope_mock):
    did, _ = at_in_progress(client)
    assert deliver(client, did).status_code == 200

    # 授权转公开：客户显式动作留档 + publish 调用形状
    resp = publish_authorization(
        client, did, True, conditions="脱敏口径经客户确认", indicators=["IND-AUTO-014"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    event = body["delivery_events"][-1]
    assert event["type"] == "publish_authorization"
    assert event["granted"] is True
    assert event["conditions"] == "脱敏口径经客户确认"
    assert event["by"] == "customer:cust-001"  # 缺省归属客户
    assert event["indicators"] == ["IND-AUTO-014"]
    assert event["publish_outcome"]["ok"] is True
    assert scope_mock.calls_of("publish") == [("publish", ("IND-AUTO-014",))]

    # 客户视图：授权状态可见
    assert customer_detail(client, did)["delivery"]["publish_authorized"] is True

    # 撤回：unpublish 调用 + 再留档（append-only）
    resp = publish_authorization(client, did, False, by="customer:cust-001")
    assert resp.status_code == 200, resp.text
    assert scope_mock.calls_of("unpublish") == [
        ("unpublish", ("IND-AUTO-014",))  # 缺省回到 linked_indicators
    ]
    events = internal_detail(client, did)["delivery_events"]
    auth_events = [e for e in events if e["type"] == "publish_authorization"]
    assert [e["granted"] for e in auth_events] == [True, False]
    assert customer_detail(client, did)["delivery"]["publish_authorized"] is False


def test_publish_authorization_requires_delivered(client):
    did, _ = at_in_progress(client)
    resp = publish_authorization(client, did, True)
    assert resp.status_code == 409
    assert "delivered" in resp.json()["detail"]


def test_publish_authorization_unconfigured_records_failure(client, monkeypatch):
    monkeypatch.delenv("SCOPE_API_BASE", raising=False)
    did, _ = at_in_progress(client)
    assert deliver(client, did).status_code == 200

    resp = publish_authorization(client, did, True)
    assert resp.status_code == 200, resp.text
    event = resp.json()["delivery_events"][-1]
    assert event["granted"] is True  # 授权是客户事实，照常留档
    assert event["publish_outcome"]["ok"] is False
    assert "未配置" in event["publish_outcome"]["error"]
    # 客户视图仍如实反映授权动作
    assert customer_detail(client, did)["delivery"]["publish_authorized"] is True


# ------------------------------------------------------- 客户视图分工


def test_customer_view_delivery_fields_without_internals(client, scope_mock):
    did, submitted = at_in_progress(client)
    deliver(client, did)
    publish_authorization(client, did, True)

    detail = customer_detail(client, did)
    # 交付状态与授权状态客户可见……
    assert detail["delivery"] == {
        "scope_name": f"demand-{submitted['code']}",
        "scope_status": "ok",
        "publish_authorized": True,
    }
    # ……内部细节绝不出现
    for internal_field in (
        "pipeline_events", "delivery_events", "gate",
        "evaluation_detail", "quote_operator", "quote_history",
    ):
        assert internal_field not in detail

    # 内部视图齐全（确认台上下文）
    internal = internal_detail(client, did)
    assert internal["gate"]["passed"] is False  # 未记 gate，摘要如实
    assert len(internal["pipeline_events"]) >= 0
    assert [e["type"] for e in internal["delivery_events"]] == [
        "scope_created", "publish_authorization",
    ]


# ------------------------------------------------------- 完整闭环（验收）


def test_full_closed_loop(client, scope_mock):
    """提交→in_progress（pipeline event）→gate fail→重跑 pass→deliver
    （scope 建）→delivered→授权转公开→撤回。"""
    d = submit(client)
    did = d["id"]

    # 派发接单 → in_progress，随后流水线事实入档
    assert transition(client, did, "in_progress", by="ops-001").status_code == 200
    assert pipeline_run(
        client, did, pipeline_run_id="run-closed-loop", repo="fd-industry-data", branch="main"
    ).status_code == 200

    # gate fail → 如实留档（仅观测，不门禁）；重跑 pass
    assert gate_result(client, did, False, note="首跑 2 处违规").status_code == 200
    assert customer_detail(client, did)["status"] == "in_progress"
    assert gate_result(client, did, True, note="重跑全绿").status_code == 200

    # deliver：delivered + 客户私有 scope 建立
    resp = deliver(client, did, note="完成交付")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "delivered"
    assert resp.json()["scope_status"] == "ok"

    # 授权转公开 → 撤回（都留档，客户视图可见）
    assert publish_authorization(client, did, True, conditions="年度汇总口径").status_code == 200
    assert customer_detail(client, did)["delivery"]["publish_authorized"] is True
    assert publish_authorization(client, did, False).status_code == 200
    assert customer_detail(client, did)["delivery"]["publish_authorized"] is False

    # 全程留档核对（三态时间线）
    internal = internal_detail(client, did)
    assert [e["status"] for e in internal["status_history"]] == [
        "submitted", "in_progress", "delivered",
    ]
    assert [e["type"] for e in internal["pipeline_events"]] == [
        "pipeline_run", "gate_result", "gate_result",
    ]
    assert [e["type"] for e in internal["delivery_events"]] == [
        "scope_created", "publish_authorization", "publish_authorization",
    ]
    # scope 客户端调用形状全程可追溯
    assert [c[0] for c in scope_mock.calls] == [
        "create_scope", "grant_indicator", "publish", "unpublish",
    ]


# ------------------------------------------------------- 迁移链


def test_migration_head_adds_demand_columns(migrated_demands_sqlite):
    engine = create_engine(migrated_demands_sqlite)
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("demands")}
    assert {
        "pipeline_events", "delivery_events", "scope_status", "scope_name",
        # wire-ux-consolidation 3.2：意向单联系方式列
        "contact_email", "company",
        # wire-demand-generation-orchestration 1.1：合规门两列
        "source_urls", "public_data_asserted",
    } <= columns
    with engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    # 链头唯一且随链推进（wire-rich-text-docs 起链头=0038）
    assert version == "0047_dataset_acl"
