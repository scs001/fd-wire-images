"""交付 scope 重试端点（wire-scope-delivery-live 任务 1.1–1.3）。

``POST /internal/demands/{id}/scope-retry``（内网令牌）与管理台同语义端点
``POST /internal/admin/demands/{id}/scope-retry``：对 status=delivered 且
scope_status=pending 的单受理——前置闸 409 如实回显当前状态/scope 态
（零副作用），通过后复用 ``deliver_demand`` 的重试分支只重试建客户私有
scope（不迁移状态），成功 ok+scope_name 落档，失败照旧留档 pending
（spec 场景「pending 补课成功」/「非法状态拒」/「重试再失败如实」）。

本文件照 test_admin_demand_transition 的 harness（admin_env 单库联查 +
MockScopeClient 依赖覆盖）；交付 service 本体语义见
test_demands_pipeline_delivery.py。
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
INDICATOR = "IND-RETRY-014"


# ---------------------------------------------------------------------------
# 假件与夹具
# ---------------------------------------------------------------------------


class MockScopeClient:
    """满足 ScopeClient 协议的内存假件：记录调用形状，可注入建 scope 失败。

    与 test_admin_demand_transition.MockScopeClient 同款；不复用其导入
    （import 期改写 FD_WIRE_DATABASE_URL 的模块间波同因）。
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


def _at_in_progress(client, customer_id: str = "cust-001") -> dict:
    """造数到 in_progress：客户面提交 → 派发台接单。"""
    seed_console_customers(customer_id)
    demand = _submit(client, customer_id)
    resp = _admin_transition(client, demand["id"], "in_progress", by="admin-console")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _at_delivered(
    client, mock=None, customer_id: str = "cust-001", *, fail_create: bool = True
) -> dict:
    """造数到 delivered（mock 可注入建 scope 失败 → scope_status=pending；
    mock=None 配合覆盖为 None 的客户端即「未配置」路径）。"""
    if mock is not None:
        mock.fail_create = fail_create
    demand = _at_in_progress(client, customer_id)
    resp = _admin_transition(client, demand["id"], "delivered", by="admin-console")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _scope_retry(client, demand_id: int, *, via: str = "internal", **payload):
    if via == "internal":
        return client.post(
            f"/internal/demands/{demand_id}/scope-retry",
            json=payload or None,
            headers=INTERNAL,
        )
    return client.post(
        f"/internal/admin/demands/{demand_id}/scope-retry",
        json=payload or None,
        headers=AUTH,
    )


# ---------------------------------------------------------------------------
# pending → ok：补课成功（spec 场景「pending 补课成功」）
# ---------------------------------------------------------------------------


def test_scope_retry_pending_to_ok(admin_env, scope_client_override):
    """delivered+pending → 重试建 scope 成功：ok + scope_name + 留档追加。"""
    mock = scope_client_override(MockScopeClient())
    client = admin_env.client
    demand = _at_delivered(client, mock)  # 交付时失败 → pending
    assert demand["scope_status"] == "pending"

    mock.fail_create = False
    resp = _scope_retry(client, demand["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"  # 重试不迁移状态
    assert body["scope_status"] == "ok"
    assert body["scope_name"] == f"demand-{demand['code']}"

    # 调用形状：scope 命名/summary 照旧，指标缺省用 linked_indicators
    creates = mock.calls_of("create_scope")
    assert len(creates) == 2  # 首次失败一次 + 重试成功一次
    _, name, _summary, source_dbs, codes = creates[-1]
    assert name == f"demand-{demand['code']}"
    assert source_dbs == ()
    assert codes == (INDICATOR,)
    assert [c[2] for c in mock.calls_of("grant_indicator")] == [INDICATOR]

    # 留档 append-only：失败事件不改写，新成功事件追加
    assert [e["type"] for e in body["delivery_events"]] == [
        "scope_create_failed",
        "scope_created",
    ]
    assert body["delivery_events"][-1]["scope_name"] == name
    assert body["delivery_events"][-1]["granted"] == [INDICATOR]


def test_scope_retry_indicator_override(admin_env, scope_client_override):
    """body 带 indicators/source_dbs 覆盖：mock 收到覆盖值而非 linked_indicators。"""
    mock = scope_client_override(MockScopeClient())
    client = admin_env.client
    demand = _at_delivered(client, mock)
    mock.fail_create = False

    resp = _scope_retry(
        client, demand["id"], indicators=["x.y"], source_dbs=["cell_alpha"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_status"] == "ok"

    _, _name, _summary, source_dbs, codes = mock.calls_of("create_scope")[-1]
    assert codes == ("x.y",)  # 覆盖生效：不是 linked 的 INDICATOR
    assert source_dbs == ("cell_alpha",)
    assert body["delivery_events"][-1]["indicators"] == ["x.y"]
    assert [c[2] for c in mock.calls_of("grant_indicator")] == ["x.y"]


# ---------------------------------------------------------------------------
# pending → 再失败：如实留档、保持 pending（spec 场景「重试再失败如实」）
# ---------------------------------------------------------------------------


def test_scope_retry_fails_again_stays_pending(admin_env, scope_client_override):
    """上游仍不可达：200 响应单态如实——pending 保持，scope_create_failed 追加。"""
    mock = scope_client_override(
        MockScopeClient(
            fail_create=True,
            error=ScopeClientError("scope API 不可达（连接超时）", status_code=503),
        )
    )
    client = admin_env.client
    demand = _at_delivered(client, mock)

    resp = _scope_retry(client, demand["id"])
    assert resp.status_code == 200, resp.text  # 失败被消化留档，不外溢异常
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "pending"
    assert body["scope_name"] is None
    events = [e["type"] for e in body["delivery_events"]]
    assert events == ["scope_create_failed", "scope_create_failed"]
    assert "不可达" in body["delivery_events"][-1]["error"]
    assert len(mock.calls_of("create_scope")) == 2  # 重试确实发起了


def test_scope_retry_unconfigured_client_honest_pending(
    admin_env, scope_client_override
):
    """scope client 未配置（None）：200 + pending，「scope API 未配置」留档。"""
    scope_client_override(None)
    client = admin_env.client
    # 交付时 client 即未配置 → 诚实降级 pending；重试同样如实留档
    demand = _at_delivered(client)

    resp = _scope_retry(client, demand["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_status"] == "pending"
    assert body["scope_name"] is None
    assert "scope API 未配置" in body["delivery_events"][-1]["error"]


# ---------------------------------------------------------------------------
# 前置闸：非法状态 409 如实回显、零副作用（spec 场景「非法状态拒」）
# ---------------------------------------------------------------------------


def test_scope_retry_rejects_in_progress(admin_env, scope_client_override):
    """in_progress 单 → 409 文案含当前状态；零副作用（无事件、零调用）。"""
    mock = scope_client_override(MockScopeClient())
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _scope_retry(client, demand["id"])
    assert resp.status_code == 409, resp.text
    assert "仅已交付单可重试开 scope" in resp.json()["detail"]
    assert "in_progress" in resp.json()["detail"]
    # 零副作用：未发起任何 scope 调用、无交付留档
    assert mock.calls == []

    probe = client.get(f"/internal/demands/{demand['id']}", headers=INTERNAL)
    assert probe.status_code == 200
    assert probe.json()["scope_status"] is None
    assert probe.json()["delivery_events"] == []


def test_scope_retry_rejects_scope_ok(admin_env, scope_client_override):
    """delivered+ok 单 → 409 文案含 scope 状态；留档不追加。"""
    mock = scope_client_override(MockScopeClient())
    client = admin_env.client
    demand = _at_delivered(client, mock, fail_create=False)  # 交付即成功
    assert demand["scope_status"] == "ok"

    resp = _scope_retry(client, demand["id"])
    assert resp.status_code == 409, resp.text
    assert "scope 已交付或无 pending 留档" in resp.json()["detail"]
    assert "ok" in resp.json()["detail"]
    # 零副作用：仍只有首次成功那一笔留档，无新调用
    assert len(mock.calls_of("create_scope")) == 1
    probe = client.get(f"/internal/demands/{demand['id']}", headers=INTERNAL)
    assert probe.json()["scope_status"] == "ok"
    assert [e["type"] for e in probe.json()["delivery_events"]] == ["scope_created"]


# ---------------------------------------------------------------------------
# 管理台同语义端点（admin 双闸下的同一行为）
# ---------------------------------------------------------------------------


def test_admin_scope_retry_same_semantics_success(admin_env, scope_client_override):
    """管理台端点补课成功：与内部端点逐字同语义（ok + scope_name + 留档）。"""
    mock = scope_client_override(MockScopeClient())
    client = admin_env.client
    demand = _at_delivered(client, mock)
    mock.fail_create = False

    resp = _scope_retry(client, demand["id"], via="admin")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["scope_status"] == "ok"
    assert body["scope_name"] == f"demand-{demand['code']}"
    assert [e["type"] for e in body["delivery_events"]] == [
        "scope_create_failed",
        "scope_created",
    ]


def test_admin_scope_retry_same_semantics_gates(admin_env, scope_client_override):
    """管理台前置闸同文案：in_progress → 409 含状态；delivered+ok → 409 含 scope 态。"""
    mock = scope_client_override(MockScopeClient())
    client = admin_env.client
    in_progress = _at_in_progress(client)
    resp = _scope_retry(client, in_progress["id"], via="admin")
    assert resp.status_code == 409, resp.text
    assert "仅已交付单可重试开 scope" in resp.json()["detail"]
    assert "in_progress" in resp.json()["detail"]

    delivered_ok = _at_delivered(client, mock, "cust-002", fail_create=False)
    resp = _scope_retry(client, delivered_ok["id"], via="admin")
    assert resp.status_code == 409, resp.text
    assert "scope 已交付或无 pending 留档" in resp.json()["detail"]
    assert "ok" in resp.json()["detail"]
    assert mock.calls_of("create_scope")[-1][1] == f"demand-{delivered_ok['code']}"
