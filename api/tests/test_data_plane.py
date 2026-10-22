"""REST 数据面（wire-customer-data-engine §5：key 生命周期、双凭据端点、
限流/额度、CORS 关闭与平台数据边界）。

引擎经 FakeEngine（test_datasets_common）闭环；console 身份经 console_fixture
的假 JWKS。数据面 key 鉴权路径不依赖 Logto（wdk- 直验），同一族端点两种
凭据轨道都要走到。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

# 先经 app 入口导入（照 test_assets 先例）：直接首导入 data_plane.service 会
# 触发 datasets.ingest → ai → entities → data_plane.data_routes 的环，从 app
# 侧进入则各模块已完整初始化（单文件运行同样成立）
from fd_wire_api.app import app  # noqa: F401

from fd_wire_api.data_plane import service as key_service
from fd_wire_api.datasets.ingest import (
    TOOL_ADD_COLUMN,
    TOOL_CREATE_DECLARED,
    TOOL_DERIVE,
    TOOL_DERIVE_REFRESH,
    TOOL_JOIN_INDICATORS,
    TOOL_LINEAGE,
    TOOL_OVERVIEW,
    TOOL_QUERY,
    TOOL_WRITE_BATCH,
)
from test_datasets_common import CUST_HEADERS, datasets_env  # noqa: F401


@pytest.fixture()
def env(datasets_env):  # noqa: F811 - 组合共享 fixture
    key_service.reset_runtime()
    yield datasets_env
    key_service.reset_runtime()


def _mint_key(env, *, label="app-1", dataset_ids=None) -> tuple[str, str]:
    resp = env.client.post(
        "/api/v1/data-plane-keys",
        headers=CUST_HEADERS["cust-001"],
        json={"label": label, "dataset_ids": dataset_ids or []},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["key"]


def _key_headers(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _make_dataset(env, *, name="Sales Table") -> str:
    paired = env.client.post(
        "/api/v1/connectors/pairing-tokens", headers=CUST_HEADERS["cust-001"]
    )
    resp = env.client.post(
        "/api/v1/datasets",
        headers=CUST_HEADERS["cust-001"],
        json={
            "name": name,
            "source": {"kind": "connector", "connector_id": None, "source_name": None, "source_ref": None}
            | {
                "connector_id": _pair(env),
                "source_name": "erp",
                "source_ref": "erp_db",
            },
        },
    )
    # connector 建档是惰性的（无 engine 调用）；返回 dataset id
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _pair(env) -> str:
    token = env.client.post(
        "/api/v1/connectors/pairing-tokens", headers=CUST_HEADERS["cust-001"]
    ).json()["token"]
    resp = env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": "conn-1", "version": "1.0.0"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["connector_id"]


# ── 5.2 key 生命周期 ────────────────────────────────────────────────


class TestKeyLifecycle:
    def test_mint_returns_plaintext_once(self, env):
        key_id, plaintext = _mint_key(env, label="订单系统")
        assert plaintext.startswith("wdk-") and len(plaintext) > 40
        listed = env.client.get(
            "/api/v1/data-plane-keys", headers=CUST_HEADERS["cust-001"]
        ).json()
        assert len(listed) == 1
        assert listed[0]["id"] == key_id
        assert listed[0]["fingerprint"] and "key" not in listed[0]
        assert listed[0]["dataset_ids"] == []
        assert listed[0]["status"] == "active"

    def test_mint_allowlist_validates_dataset_ids(self, env):
        resp = env.client.post(
            "/api/v1/data-plane-keys",
            headers=CUST_HEADERS["cust-001"],
            json={"label": "scoped", "dataset_ids": ["not-a-dataset"]},
        )
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_revocation_immediate(self, env):
        dataset_id = _make_dataset(env)
        key_id, plaintext = _mint_key(env, dataset_ids=[dataset_id])
        resp = env.client.post(
            f"/api/v1/datasets/{dataset_id}/query",
            headers=_key_headers(plaintext),
            json={},
        )
        assert resp.status_code == 200, resp.text
        revoked = env.client.delete(
            f"/api/v1/data-plane-keys/{key_id}", headers=CUST_HEADERS["cust-001"]
        )
        assert revoked.status_code == 200
        after = env.client.post(
            f"/api/v1/datasets/{dataset_id}/query",
            headers=_key_headers(plaintext),
            json={},
        )
        assert after.status_code == 401
        assert "已吊销" in after.json()["detail"]

    def test_console_and_cross_customer_isolation(self, env):
        resp = env.client.get("/api/v1/data-plane-keys")  # 无凭据
        assert resp.status_code == 401
        other = env.client.get(
            "/api/v1/data-plane-keys", headers=CUST_HEADERS["cust-002"]
        )
        assert other.status_code == 200 and other.json() == []


# ── 5.3 数据端点（双凭据 + 薄壳转发）────────────────────────────────


class TestDataEndpoints:
    def test_query_via_key_and_console(self, env):
        dataset_id = _make_dataset(env)
        _, plaintext = _mint_key(env)
        by_key = env.client.post(
            f"/api/v1/datasets/{dataset_id}/query",
            headers=_key_headers(plaintext),
            json={"filters": [{"column": "id", "op": "eq", "value": 1}], "limit": 10},
        )
        assert by_key.status_code == 200, by_key.text
        assert by_key.json()["total"] == 1
        assert env.fake.calls_for(TOOL_QUERY)[0]["dataset_key"]  # 引擎 key 映射正确

        by_console = env.client.post(
            f"/api/v1/datasets/{dataset_id}/query",
            headers=CUST_HEADERS["cust-001"],
            json={"count_only": True},
        )
        assert by_console.status_code == 200

    def test_batch_write_carries_channel_attribution(self, env):
        dataset_id = _make_dataset(env)
        _, plaintext = _mint_key(env)
        resp = env.client.post(
            f"/api/v1/datasets/{dataset_id}/rows:batchWrite",
            headers=_key_headers(plaintext),
            json={"ops": [{"op": "insert", "values": {"id": 9}}]},
        )
        assert resp.status_code == 200, resp.text
        args = env.fake.calls_for(TOOL_WRITE_BATCH)[0]
        assert args["channel"] == "data-key" and args["actor_id"]
        assert env.fake.calls_for(TOOL_OVERVIEW)  # 行数回填以引擎为准

    def test_declared_creation(self, env):
        resp = env.client.post(
            "/api/v1/datasets",
            headers=CUST_HEADERS["cust-001"],
            json={
                "name": "Order Table",
                "source": {
                    "kind": "declared",
                    "columns": [
                        {"name": "order_no", "type": "TEXT"},
                        {"name": "qty", "type": "INTEGER"},
                    ],
                    "pk_columns": ["order_no"],
                },
            },
        )
        assert resp.status_code in (200, 201), resp.text
        body = resp.json()
        assert body["source_kind"] == "declared"
        assert body["columns"][0]["name"] == "order_no"
        args = env.fake.calls_for(TOOL_CREATE_DECLARED)[0]
        assert args["pk_columns"] == ["order_no"]
        assert args["columns"][1] == {"name": "qty", "type": "INTEGER"}

    def test_add_column_syncs_local_metadata(self, env):
        dataset_id = _make_dataset(env)
        resp = env.client.post(
            f"/api/v1/datasets/{dataset_id}/columns",
            headers=CUST_HEADERS["cust-001"],
            json={"column": {"name": "status", "type": "TEXT"}},
        )
        assert resp.status_code == 200, resp.text
        assert {"name": "status", "type": "TEXT"} in resp.json()["columns"]

    def test_derive_registers_child_and_lineage_ids(self, env):
        source_id = _make_dataset(env)
        resp = env.client.post(
            f"/api/v1/datasets/{source_id}/derive",
            headers=CUST_HEADERS["cust-001"],
            json={"name": "Derived View", "filters": [], "sort": []},
        )
        assert resp.status_code == 201, resp.text
        child = resp.json()
        assert child["source_kind"] == "derived"
        # 行数以引擎 overview 回填为准（FakeEngine 默认 2），非派生返回值
        assert child["row_count"] == 2
        args = env.fake.calls_for(TOOL_DERIVE)[0]
        assert args["parent_key"] and args["child_key"] == child["key"]

        # 刷新走 derive_refresh；非派生集拒绝
        ok = env.client.post(
            f"/api/v1/datasets/{child['id']}/refresh", headers=CUST_HEADERS["cust-001"]
        )
        assert ok.status_code == 200
        assert env.fake.calls_for(TOOL_DERIVE_REFRESH)
        refused = env.client.post(
            f"/api/v1/datasets/{source_id}/refresh", headers=CUST_HEADERS["cust-001"]
        )
        assert refused.status_code == 400

    def test_join_passes_indicator_contract(self, env):
        dataset_id = _make_dataset(env)
        resp = env.client.post(
            f"/api/v1/datasets/{dataset_id}/join",
            headers=CUST_HEADERS["cust-001"],
            json={
                "name": "With GDP",
                "indicators": [{"code": "GDP_MO", "as": "gdp"}],
                "date_column": "month",
            },
        )
        assert resp.status_code == 201, resp.text
        args = env.fake.calls_for(TOOL_JOIN_INDICATORS)[0]
        assert args["indicators"] == [
            {"code": "GDP_MO", "concept_id": None, "as": "gdp"}
        ]
        assert args["date_column"] == "month"

    def test_engine_rejection_maps_to_400(self, env):
        dataset_id = _make_dataset(env)
        env.fake.error_on[TOOL_WRITE_BATCH] = "第 2 条失败: 行锚未命中任何行: pk:[999]"
        resp = env.client.post(
            f"/api/v1/datasets/{dataset_id}/rows:batchWrite",
            headers=CUST_HEADERS["cust-001"],
            json={"ops": [{"op": "delete", "anchor": "pk:[999]"}]},
        )
        assert resp.status_code == 400
        assert "行锚未命中" in resp.json()["detail"]

    def test_lineage_maps_keys_to_dataset_ids(self, env):
        dataset_id = _make_dataset(env)
        from fd_wire_api.datasets.db import get_session
        from fd_wire_api.datasets.models import Dataset as DatasetRow

        with get_session() as sess:
            row = sess.query(DatasetRow).filter(DatasetRow.id == dataset_id).one()
            key = row.key
        env.fake.lineage_result = {
            "derivation": [{"parent_key": key, "kind": "query", "spec": {}}],
            "children": [],
        }
        resp = env.client.get(
            f"/api/v1/datasets/{dataset_id}/lineage", headers=CUST_HEADERS["cust-001"]
        )
        assert resp.status_code == 200
        assert resp.json()["derivation"][0]["parent_dataset_id"] == dataset_id
        assert env.fake.calls_for(TOOL_LINEAGE)


# ── 5.4 allowlist / 限流 / 额度 ─────────────────────────────────────


class TestGuards:
    def test_allowlist_enforcement_403_no_engine_call(self, env):
        ds_a = _make_dataset(env, name="A Table")
        ds_b = _make_dataset(env, name="B Table")
        _, plaintext = _mint_key(env, dataset_ids=[ds_a])
        ok = env.client.post(
            f"/api/v1/datasets/{ds_a}/query", headers=_key_headers(plaintext), json={}
        )
        assert ok.status_code == 200
        before = len(env.fake.calls)
        denied = env.client.post(
            f"/api/v1/datasets/{ds_b}/query", headers=_key_headers(plaintext), json={}
        )
        assert denied.status_code == 403
        assert "允许清单" in denied.json()["detail"]
        assert len(env.fake.calls) == before  # 引擎零触达

    def test_rate_limit_429_per_key(self, env, monkeypatch):
        monkeypatch.setenv("FD_WIRE_DATA_PLANE_RPM", "2")
        key_service.reset_runtime()
        dataset_id = _make_dataset(env)
        _, plaintext = _mint_key(env)
        for _ in range(2):
            assert (
                env.client.post(
                    f"/api/v1/datasets/{dataset_id}/query",
                    headers=_key_headers(plaintext),
                    json={},
                ).status_code
                == 200
            )
        limited = env.client.post(
            f"/api/v1/datasets/{dataset_id}/query",
            headers=_key_headers(plaintext),
            json={},
        )
        assert limited.status_code == 429
        assert "限流" in limited.json()["detail"]
        # 其他 key 不受影响
        _, other = _mint_key(env, label="app-2")
        assert (
            env.client.post(
                f"/api/v1/datasets/{dataset_id}/query",
                headers=_key_headers(other),
                json={},
            ).status_code
            == 200
        )

    def test_storage_quota_fail_closed_on_write(self, env, monkeypatch):
        from test_datasets_common import patch_dataset_settings

        dataset_id = _make_dataset(env)
        _, plaintext = _mint_key(env)
        patch_dataset_settings(monkeypatch, dataset_storage_limit_bytes=1)
        resp = env.client.post(
            f"/api/v1/datasets/{dataset_id}/rows:batchWrite",
            headers=_key_headers(plaintext),
            json={"ops": [{"op": "insert", "values": {"id": 5, "amount": 9.9}}]},
        )
        assert resp.status_code == 429
        assert "配额" in resp.json()["detail"]
        assert not env.fake.calls_for(TOOL_WRITE_BATCH)  # 引擎零触达


# ── 5.5 CORS 关闭 + 平台数据边界 ────────────────────────────────────


class TestBoundaries:
    def test_cors_preflight_rejected(self, env):
        resp = env.client.options(
            "/api/v1/datasets/some-id/query",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert resp.status_code in (404, 405)
        assert "access-control-allow-origin" not in resp.headers

    def test_platform_indicators_boundary_points_to_mcp(self, env):
        resp = env.client.get("/api/v1/platform-indicators/gdp/latest")
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert "MCP" in detail and "join" in detail


# ── 6.1 定时重派生 ──────────────────────────────────────────────────


class TestDerivedSchedule:
    def _derived_dataset(self, env) -> dict:
        source_id = _make_dataset(env)
        resp = env.client.post(
            f"/api/v1/datasets/{source_id}/derive",
            headers=CUST_HEADERS["cust-001"],
            json={"name": "Sched Child", "filters": []},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_schedule_lifecycle_and_validation(self, env):
        child = self._derived_dataset(env)
        bad = env.client.put(
            f"/api/v1/datasets/{child['id']}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "not-a-cron"},
        )
        assert bad.status_code == 400

        put = env.client.put(
            f"/api/v1/datasets/{child['id']}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "0 9 * * *"},
        )
        assert put.status_code == 200 and put.json()["cron"] == "0 9 * * *"

        got = env.client.get(
            f"/api/v1/datasets/{child['id']}/schedule", headers=CUST_HEADERS["cust-001"]
        )
        assert got.status_code == 200 and got.json()["cron"] == "0 9 * * *"

        removed = env.client.delete(
            f"/api/v1/datasets/{child['id']}/schedule", headers=CUST_HEADERS["cust-001"]
        )
        assert removed.status_code == 200
        assert env.client.get(
            f"/api/v1/datasets/{child['id']}/schedule", headers=CUST_HEADERS["cust-001"]
        ).json()["cron"] is None

    def test_non_derived_dataset_rejected(self, env):
        dataset_id = _make_dataset(env)
        resp = env.client.put(
            f"/api/v1/datasets/{dataset_id}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "0 9 * * *"},
        )
        assert resp.status_code == 400 and "派生" in resp.json()["detail"]

    def test_runner_dispatches_derived_refresh(self, env):
        from datetime import datetime, timezone

        from fd_wire_api.materialization.models import MaterializedArtifact
        from fd_wire_api.materialization.scheduler import default_runner_factory, should_run

        child = self._derived_dataset(env)
        put = env.client.put(
            f"/api/v1/datasets/{child['id']}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "0 9 * * *"},
        )
        assert put.status_code == 200

        from fd_wire_api.materialization.db import get_session as get_mat_session

        # 配额守卫按 sub2api_user_id 解析（生产上开户 ceremony 与客户名一致；
        # 测试种子是 caller-N，对齐到生产口径）
        from fd_wire_api.admin.db import get_session as platform_session
        from fd_wire_api.billing.models import Customer as BillingCustomer

        with platform_session() as sess:
            bcust = sess.execute(
                select(BillingCustomer).where(BillingCustomer.name == "cust-001")
            ).scalar_one()
            bcust.sub2api_user_id = "cust-001"
            sess.commit()

        mat = get_mat_session()
        try:
            row = mat.execute(
                select(MaterializedArtifact).where(
                    MaterializedArtifact.ref_key == child["id"]
                )
            ).scalar_one()
            row.schedule = "* * * * *"  # 每分钟命中（测试直改调度）
            mat.commit()
            moment = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
            assert should_run(row, moment) is True

            from fd_wire_api.materialization import runner as runner_mod

            runner_mod._engine_factory_override = lambda cid: env.fake
            runner = default_runner_factory()
            try:
                result = runner.run_artifact_row(row, now=moment)
            finally:
                runner.close()
                runner.release_db()
                runner_mod._engine_factory_override = None
            assert result.status == "success", result.error
        finally:
            mat.close()
        assert env.fake.calls_for(TOOL_DERIVE_REFRESH)
        assert env.customers  # 引擎按客户命名空间键调用


# ── 6.4 套餐分层配额 ────────────────────────────────────────────────


class TestPlanTieredQuota:
    def test_storage_limit_resolves_from_plan(self, env, monkeypatch):
        from test_datasets_common import patch_dataset_settings

        from fd_wire_api.admin.db import get_session as platform_session
        from fd_wire_api.billing.models import Customer, Plan
        from fd_wire_api.datasets import service

        patch_dataset_settings(monkeypatch, dataset_storage_limit_bytes=1)
        assert service.storage_limit_for(customer_id="cust-001") == 1

        with platform_session() as sess:
            plan = sess.execute(select(Plan).where(Plan.code == "standard")).scalar_one()
            assert plan.limits_json["storage_bytes"] == 5 * 1024**3
            customer = sess.execute(
                select(Customer).where(Customer.name == "cust-002")
            ).scalar_one()
            customer.plan_id = plan.id
            sess.commit()
        assert service.storage_limit_for(customer_id="cust-002") == 5 * 1024**3

    def test_plan_seed_carries_business_tier_limits(self, env):
        from fd_wire_api.admin.db import get_session as platform_session
        from fd_wire_api.billing.models import Plan

        with platform_session() as sess:
            standard = sess.execute(
                select(Plan).where(Plan.code == "standard")
            ).scalar_one()
            free = sess.execute(select(Plan).where(Plan.code == "free")).scalar_one()
        assert standard.limits_json == {
            "max_dataset_rows": 5_000_000,
            "storage_bytes": 5 * 1024**3,
        }
        assert free.limits_json is None
