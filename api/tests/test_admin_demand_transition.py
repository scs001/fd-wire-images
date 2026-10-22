"""admin 派发台交付迁移的自动私有 scope 钩子（wire-demand-visibility 任务 1.1+1.2）。

``POST /internal/admin/demands/{id}/transition`` 迁入 ``delivered`` 时与内部
端点 ``POST /internal/demands/{id}/transition`` 逐字同语义：迁移成功后补调
``delivery_service.maybe_auto_deliver`` 自动发起客户私有 scope 交付——成功
``scope_status=ok``，scope 面失败只落 ``pending`` 留档、绝不打断迁移响应
（spec 场景「管理台交付自动开 scope」/「scope 失败不打断」）。

本文件只测 admin 面的接线与各态（成功/失败/未配置/意外异常）+ 与内部端点
的响应形状一致性；scope 客户端本体调用形状与交付 service 语义见
test_demands_pipeline_delivery.py，派发台其余口径见 test_admin_api.py。
"""

from __future__ import annotations

import pytest

from fd_wire_api.app import app
from fd_wire_api.delivery.scope_client import ScopeClientError, scope_client_dep

from console_fixture import bearer, mint_id_token
from test_admin_api import AUTH, admin_env, seed_console_customers

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
#: 目录锚点（交付时逐指标 grant 进 scope allow-list）
INDICATOR = "IND-AUTO-014"


# ---------------------------------------------------------------------------
# 假件与夹具
# ---------------------------------------------------------------------------


class MockScopeClient:
    """满足 ScopeClient 协议的内存假件：记录调用形状，可注入建 scope 失败。

    与 test_demands_pipeline_delivery.MockScopeClient 同款；本文件不复用其
    导入（该模块 import 期会改写 FD_WIRE_DATABASE_URL，跨模块导入会波及
    默认引擎的构建时点）。
    """

    def __init__(
        self, *, fail_create: bool = False, error: Exception | None = None
    ) -> None:
        self.calls: list[tuple] = []
        self.fail_create = fail_create
        self.error = error or ScopeClientError(
            "scope API 500：内部错误", status_code=500
        )
        self.scopes: dict[str, dict] = {}

    def create_scope(self, name, summary, source_dbs=(), *, semantic_codes=()):
        self.calls.append(
            ("create_scope", name, summary, tuple(source_dbs), tuple(semantic_codes))
        )
        if self.fail_create:
            raise self.error
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
        codes = sorted(set(scope["rules"]["semantic_codes"]) | {indicator_code})
        scope["rules"]["semantic_codes"] = codes
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
def scope_client_override():
    """依赖覆盖安装器：按需注入 mock scope 客户端，用例退出自动摘除覆盖。"""

    def install(mock):
        app.dependency_overrides[scope_client_dep] = lambda: mock
        return mock

    yield install
    app.dependency_overrides.pop(scope_client_dep, None)


@pytest.fixture()
def scope_mock(scope_client_override):
    """默认成功的 mock scope 客户端（断言调用形状用）。"""
    return scope_client_override(MockScopeClient())


@pytest.fixture()
def internal_token(monkeypatch):
    """内部端点 token 钉定（parity 用例直调内部端点用）。"""
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


def _submit(client, customer_id: str = "cust-001") -> dict:
    """客户面造数：带目录锚点的意向单（交付时逐指标 grant 进 scope）。"""
    resp = client.post(
        "/api/v1/demands",
        json={
            "description": "需要上海市 2024 年以来乘用车月度销量与价格带分布，用于市场分析",
            "contact_email": f"contact-{customer_id}@example.com",
            "company": "上海某某咨询",
            "indicator_code": INDICATOR,
            "source_urls": ["https://www.stats.gov.cn/sj/zxfb/"],
            "public_data_asserted": True,
        },
        headers=bearer(mint_id_token(f"sub-{customer_id}")),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _admin_transition(client, demand_id: int, target: str, **extra):
    return client.post(
        f"/internal/admin/demands/{demand_id}/transition",
        json={"target_status": target, **extra},
        headers=AUTH,
    )


def _internal_transition(client, demand_id: int, target: str, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/transition",
        json={"target_status": target, **extra},
        headers=INTERNAL,
    )


def _at_in_progress(
    client, customer_id: str = "cust-001", *, via: str = "admin"
) -> dict:
    """造数到 in_progress：客户面提交 → 派发台（缺省）或内部端点接单。"""
    seed_console_customers(customer_id)
    demand = _submit(client, customer_id)
    resp = (
        _admin_transition(client, demand["id"], "in_progress", by="admin-console")
        if via == "admin"
        else _internal_transition(client, demand["id"], "in_progress", by="ops-001")
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 管理台交付自动开 scope（成功态）
# ---------------------------------------------------------------------------


def test_admin_delivered_auto_creates_private_scope(admin_env, scope_mock):
    """派发台迁 delivered → 自动建客户私有 scope，响应 scope_status=ok。"""
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _admin_transition(client, demand["id"], "delivered", note="处理完成")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "ok"  # 自动私有 scope 交付（同内部端点）
    assert body["scope_name"] == f"demand-{demand['code']}"

    # 调用形状：scope 命名 demand-<code>、summary 含需求摘要、逐指标 grant
    creates = scope_mock.calls_of("create_scope")
    assert len(creates) == 1
    _, name, summary, source_dbs, codes = creates[0]
    assert name == f"demand-{demand['code']}"
    assert demand["title"] in summary
    assert source_dbs == ()
    assert codes == (INDICATOR,)
    assert [c[2] for c in scope_mock.calls_of("grant_indicator")] == [INDICATOR]

    # 交付留档（append-only）
    event = body["delivery_events"][-1]
    assert event["type"] == "scope_created"
    assert event["scope_name"] == name
    assert event["granted"] == [INDICATOR]


# ---------------------------------------------------------------------------
# scope 面失败：迁移照常、只落 pending 留档
# ---------------------------------------------------------------------------


def test_admin_delivered_scope_failure_marks_pending_then_retry(
    admin_env, scope_client_override, internal_token
):
    """scope API 5xx：迁移仍成功（delivered），scope_status=pending，重试补课。"""
    mock = scope_client_override(MockScopeClient(fail_create=True))
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _admin_transition(client, demand["id"], "delivered", by="admin-console")
    assert resp.status_code == 200, resp.text  # 迁移照常完成，异常不外溢
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "pending"  # 交付挂起，如实可见
    assert body["scope_name"] is None
    failed = body["delivery_events"][-1]
    assert failed["type"] == "scope_create_failed"
    assert "500" in failed["error"]

    # 重试端点补课：失败留档不改写（append-only）
    mock.fail_create = False
    retry = client.post(
        f"/internal/demands/{demand['id']}/deliver", json={}, headers=INTERNAL
    )
    assert retry.status_code == 200, retry.text
    retry_body = retry.json()
    assert retry_body["status"] == "delivered"
    assert retry_body["scope_status"] == "ok"
    assert [e["type"] for e in retry_body["delivery_events"]] == [
        "scope_create_failed",
        "scope_created",
    ]


def test_admin_delivered_unconfigured_scope_client_marks_pending(
    admin_env, scope_client_override
):
    """scope API 未配置（客户端为 None）：迁移仍成功，pending 留档。"""
    scope_client_override(None)
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _admin_transition(client, demand["id"], "delivered")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "pending"
    assert body["scope_name"] is None
    assert "未配置" in body["delivery_events"][-1]["error"]


def test_admin_delivered_scope_hook_never_breaks_transition(
    admin_env, scope_client_override
):
    """scope 客户端抛非 ScopeClientError 的意外异常：钩子兜住，迁移不打断。"""
    mock = scope_client_override(
        MockScopeClient(fail_create=True, error=RuntimeError("boom"))
    )
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _admin_transition(client, demand["id"], "delivered")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "pending"
    assert body["delivery_events"][-1]["type"] == "scope_create_failed"
    assert "意外失败" in body["delivery_events"][-1]["error"]
    assert len(mock.calls_of("create_scope")) == 1


# ---------------------------------------------------------------------------
# 非 delivered 迁移：绝不触发交付
# ---------------------------------------------------------------------------


def test_admin_transition_non_delivered_skips_scope_delivery(admin_env, scope_mock):
    """in_progress / rejected 迁移不触发 scope 交付（scope_status 保持 None）。"""
    client = admin_env.client
    seed_console_customers("cust-001")

    accepted = _submit(client)
    resp = _admin_transition(client, accepted["id"], "in_progress", by="admin-console")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["scope_status"] is None
    assert body["scope_name"] is None
    assert body["delivery_events"] == []

    rejected = _submit(client)
    resp = _admin_transition(
        client, rejected["id"], "rejected", reason="非公开数据，线下沟通"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["scope_status"] is None
    assert body["scope_name"] is None
    assert body["delivery_events"] == []

    assert scope_mock.calls == []  # 非 delivered 迁移绝不触发交付


# ---------------------------------------------------------------------------
# 与内部端点行为一致性（同语义 parity）
# ---------------------------------------------------------------------------


def test_admin_delivered_parity_with_internal_endpoint(
    admin_env, scope_mock, internal_token
):
    """同一 mock 下，派发台与内部端点的 delivered 响应形状逐字段同形。"""
    client = admin_env.client
    admin_demand = _at_in_progress(client, "cust-001", via="admin")
    internal_demand = _at_in_progress(client, "cust-002", via="internal")

    admin_resp = _admin_transition(
        client, admin_demand["id"], "delivered", by="admin-console"
    )
    internal_resp = _internal_transition(
        client, internal_demand["id"], "delivered", by="ops-001"
    )
    assert admin_resp.status_code == internal_resp.status_code == 200
    admin_body = admin_resp.json()
    internal_body = internal_resp.json()

    # 响应键集一致（同一 response_model）
    assert set(admin_body) == set(internal_body)
    # 交付语义逐字段一致；scope_name 各自绑定本单 code
    assert admin_body["status"] == internal_body["status"] == "delivered"
    assert admin_body["scope_status"] == internal_body["scope_status"] == "ok"
    assert admin_body["scope_name"] == f"demand-{admin_demand['code']}"
    assert internal_body["scope_name"] == f"demand-{internal_demand['code']}"
    assert admin_body["linked_indicators"] == internal_body["linked_indicators"]
    assert admin_body["gate"] == internal_body["gate"]
    assert admin_body["pipeline_events"] == internal_body["pipeline_events"]

    # 交付留档：事件类型/授权指标序列一致
    admin_events = admin_body["delivery_events"]
    internal_events = internal_body["delivery_events"]
    assert [e["type"] for e in admin_events] == ["scope_created"]
    assert [e["type"] for e in internal_events] == ["scope_created"]
    assert [e["granted"] for e in admin_events] == [e["granted"] for e in internal_events]

    # 两次交付各建一个 scope（互不串单）
    names = sorted(c[1] for c in scope_mock.calls_of("create_scope"))
    assert names == sorted(
        [f"demand-{admin_demand['code']}", f"demand-{internal_demand['code']}"]
    )
