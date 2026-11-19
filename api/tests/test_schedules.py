"""统一刷新调度器 API 测试（wire-refresh-scheduler 任务 1.x/2.x/3.x）。

覆盖：
- CRUD 四闸全路径：类型校验（dataset 须 derived / artifact 须 dashboard）、
  属主校验（跨客户 404）、间隔地板（dataset 60 / artifact 360，env 可调）、
  每客户配额（env 可调）；
- 列表/删除/暂停/恢复 + 跨客户隔离 + 未登录 401；
- 调度循环（注入时钟 + 假执行器）：due 执行流（未到点不执行 / 到点执行 /
  next_run_at 前移 / 顺序）、失败记 last_error 不断环、paused 跳过、
  TargetGone 自动清行；
- 执行器真身集成：dataset 路径（假引擎经 override 注入，级联传导）、
  artifact 路径（假 LLM/工具执行器，新版本落盘）、目标失存清理；
- 级联刷新（手动端点）：三层链传导、cascade=false 关级联、环防护、
  单环失败隔离（其余环继续）；
- 迁移链 0023：建表/索引、幂等重跑、与 ORM 漂移检查、降级。
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

# 在导入 app 相关模块前钉住各库：测试进程内任何 lifespan 的建表都落在
# 内存库，不在测试机上落下残留文件（照 test_packs / test_share 先例）。
os.environ.setdefault("FD_WIRE_SCHEDULES_DATABASE_URL", "sqlite://")
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from fd_wire_api.ai.db import init_db as init_ai_db
from fd_wire_api.ai.db import reset_ai_runtime
from fd_wire_api.ai.db import get_session as get_ai_session
from fd_wire_api.ai.models import AIDraftArtifact
from fd_wire_api.ai.schemas import ConversationCreate
from fd_wire_api.ai.service import create_conversation as create_ai_conversation
from fd_wire_api.datasets.db import get_session as get_datasets_session
from fd_wire_api.datasets.ingest import (
    EngineUnavailable,
    TOOL_DERIVE_REFRESH,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset as DatasetRow
from fd_wire_api.schedules import reset_schedules_runtime
from fd_wire_api.schedules import executor as executor_mod
from fd_wire_api.schedules import runner as runner_mod
from fd_wire_api.schedules import service as schedules_service
from fd_wire_api.schedules.db import get_session as get_schedules_session
from fd_wire_api.schedules.db import init_db as init_schedules_db
from fd_wire_api.schedules.models import RefreshSchedule, utc_now_naive

from test_artifacts_refresh import FakeExecutor, FakeLlm, text_reply
from test_datasets_common import CUST_HEADERS, FakeEngine, datasets_env  # noqa: F401

API_DIR = Path(__file__).resolve().parent.parent

#: 可过 artifacts 最小校验的仪表盘 spec（from-draft / 刷新轮共用）
DASH_SPEC = {
    "title": "宏观概览",
    "spec": {
        "title": "宏观概览",
        "panels": [
            {
                "id": "p1",
                "type": "kpi",
                "title": "中国 GDP",
                "span": 1,
                "source": "IND-GDP-CN",
                "snapshot": {"columns": ["指标", "值"], "rows": [["GDP", "129.4"]]},
                "config": {"unit": "万亿元"},
            }
        ],
    },
}


class CascadeFakeEngine(FakeEngine):
    """FakeEngine 扩展：可按 dataset_key 注入 derive_refresh 失败（隔离用例）。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_refresh_keys: set[str] = set()

    def call(self, tool: str, arguments: dict) -> dict:
        if (
            tool == TOOL_DERIVE_REFRESH
            and arguments.get("dataset_key") in self.fail_refresh_keys
        ):
            # 先记录再抛（失败环的「尝试过」可从调用序列观测）
            self.calls.append((tool, dict(arguments)))
            raise EngineUnavailable(f"引擎不可达（注入失败）：{arguments.get('dataset_key')}")
        return super().call(tool, arguments)


@pytest.fixture()
def env(datasets_env, tmp_path, monkeypatch):  # noqa: F811 - 组合共享 fixture
    """datasets_env 之上：schedules/ai 库指向临时文件 + 可注入失败的假引擎。"""
    monkeypatch.setenv(
        "FD_WIRE_SCHEDULES_DATABASE_URL", f"sqlite:///{tmp_path}/schedules.db"
    )
    monkeypatch.setenv("AI_DATABASE_URL", f"sqlite:///{tmp_path}/ai.db")
    # artifact 刷新轮的 cell 开通护栏（executor 路径直查 env map）
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
    reset_schedules_runtime()
    reset_ai_runtime()
    init_schedules_db()
    init_ai_db()

    fake = CascadeFakeEngine()

    def fake_factory(customer_id: str) -> CascadeFakeEngine:
        return fake

    datasets_env.app.dependency_overrides[get_engine_client_factory] = lambda: fake_factory
    executor_mod._engine_factory_override = fake_factory
    try:
        yield SimpleNamespace(
            client=datasets_env.client, fake=fake, tmp_path=tmp_path
        )
    finally:
        executor_mod.reset_overrides()
        reset_schedules_runtime()
        reset_ai_runtime()


# ------------------------------------------------------------ 建档捷径


def _pair(env, headers) -> str:
    token = env.client.post(
        "/api/v1/connectors/pairing-tokens", headers=headers
    ).json()["token"]
    resp = env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": "conn-1", "version": "1.0.0"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["connector_id"]


def _make_dataset(env, headers, *, name="Source Table") -> dict:
    """connector 来源数据集（非派生——类型校验负路径 / 派生链根均用它）。"""
    resp = env.client.post(
        "/api/v1/datasets",
        headers=headers,
        json={
            "name": name,
            "source": {
                "kind": "connector",
                "connector_id": _pair(env, headers),
                "source_name": "erp",
                "source_ref": "erp_db",
            },
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _derive(env, headers, parent_id: str, *, name="Derived View") -> dict:
    resp = env.client.post(
        f"/api/v1/datasets/{parent_id}/derive",
        headers=headers,
        json={"name": name, "filters": [], "sort": []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_artifact(
    env, headers, *, customer_id="cust-001", kind="dashboard", spec=DASH_SPEC, publish=True
) -> dict:
    """直插会话草稿 → from-draft 导入 →（可选）发布，返回产物详情。"""
    with get_ai_session() as db:
        conversation = create_ai_conversation(
            db, customer_id=customer_id, payload=ConversationCreate(title="GDP 分析")
        )
        draft = AIDraftArtifact(
            conversation_id=conversation.id,
            customer_id=customer_id,
            kind=kind,
            spec=spec,
            status="draft",
        )
        db.add(draft)
        db.commit()
        db.refresh(draft)
        pair = {"conversation_id": conversation.id, "draft_id": draft.id}
    resp = env.client.post("/api/v1/artifacts/from-draft", headers=headers, json=pair)
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    if publish:
        published = env.client.post(
            f"/api/v1/artifacts/{artifact['id']}/publish", headers=headers
        )
        assert published.status_code == 200, published.text
        artifact = published.json()
    return artifact


def _create_schedule(env, headers, body: dict):
    return env.client.post("/api/v1/refresh-schedules", headers=headers, json=body)


def _derived_dataset(env, headers=None, *, name="Derived View") -> dict:
    headers = headers or CUST_HEADERS["cust-001"]
    source = _make_dataset(env, headers, name=f"{name} 源")
    return _derive(env, headers, source["id"], name=name)


# ------------------------------------------------------------ CRUD 四闸


class TestCrudValidation:
    def test_create_dataset_schedule_happy_path(self, env):
        child = _derived_dataset(env)
        resp = _create_schedule(
            env,
            CUST_HEADERS["cust-001"],
            {
                "target_type": "dataset",
                "target_id": child["id"],
                "interval_minutes": 60,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["target_type"] == "dataset"
        assert body["target_id"] == child["id"]
        assert body["interval_minutes"] == 60
        assert body["paused"] is False
        assert body["last_status"] == "pending"
        assert body["last_run_at"] is None and body["last_error"] is None
        # 首次执行在 now+interval（容差 ±2 分钟）
        from datetime import datetime

        next_run = datetime.fromisoformat(body["next_run_at"])
        delta = (next_run - utc_now_naive()).total_seconds() / 60
        assert 58 <= delta <= 62

    def test_interval_floor_dataset(self, env):
        child = _derived_dataset(env)
        refused = _create_schedule(
            env,
            CUST_HEADERS["cust-001"],
            {
                "target_type": "dataset",
                "target_id": child["id"],
                "interval_minutes": 59,
            },
        )
        assert refused.status_code == 400
        assert "60" in refused.json()["detail"] and "分钟" in refused.json()["detail"]

    def test_interval_floor_artifact_and_env_override(self, env, monkeypatch):
        dashboard = _make_artifact(env, CUST_HEADERS["cust-001"])
        refused = _create_schedule(
            env,
            CUST_HEADERS["cust-001"],
            {
                "target_type": "artifact",
                "target_id": str(dashboard["id"]),
                "interval_minutes": 359,
            },
        )
        assert refused.status_code == 400
        assert "360" in refused.json()["detail"]

        # env 调低地板后同值放行（设置单例需重置才生效）
        monkeypatch.setenv("FD_WIRE_SCHEDULE_MIN_INTERVAL_ARTIFACT", "60")
        reset_schedules_runtime()
        init_schedules_db()
        ok = _create_schedule(
            env,
            CUST_HEADERS["cust-001"],
            {
                "target_type": "artifact",
                "target_id": str(dashboard["id"]),
                "interval_minutes": 60,
            },
        )
        assert ok.status_code == 201, ok.text

    def test_type_validation_rejects_non_derived_dataset(self, env):
        source = _make_dataset(env, CUST_HEADERS["cust-001"])
        resp = _create_schedule(
            env,
            CUST_HEADERS["cust-001"],
            {
                "target_type": "dataset",
                "target_id": source["id"],
                "interval_minutes": 120,
            },
        )
        assert resp.status_code == 400
        assert "派生" in resp.json()["detail"]

    def test_type_validation_rejects_non_dashboard_artifact(self, env):
        alert = _make_artifact(
            env,
            CUST_HEADERS["cust-001"],
            kind="alert_rule",
            spec={"title": "告警", "spec": {"indicator": "X", "threshold": 1}},
        )
        resp = _create_schedule(
            env,
            CUST_HEADERS["cust-001"],
            {
                "target_type": "artifact",
                "target_id": str(alert["id"]),
                "interval_minutes": 360,
            },
        )
        assert resp.status_code == 400
        assert "dashboard" in resp.json()["detail"]

    def test_unknown_target_type_rejected(self, env):
        resp = _create_schedule(
            env,
            CUST_HEADERS["cust-001"],
            {"target_type": "report", "target_id": "x", "interval_minutes": 60},
        )
        assert resp.status_code == 400

    def test_owner_validation_cross_customer_404(self, env):
        other_child = _derived_dataset(
            env, headers=CUST_HEADERS["cust-002"], name="他企派生集"
        )
        assert (
            _create_schedule(
                env,
                CUST_HEADERS["cust-001"],
                {
                    "target_type": "dataset",
                    "target_id": other_child["id"],
                    "interval_minutes": 60,
                },
            ).status_code
            == 404
        )
        # 不存在的目标同样 404（不泄露存在性）；artifact 轨道同口径
        assert (
            _create_schedule(
                env,
                CUST_HEADERS["cust-001"],
                {"target_type": "dataset", "target_id": "no-such-id", "interval_minutes": 60},
            ).status_code
            == 404
        )
        dashboard = _make_artifact(env, CUST_HEADERS["cust-001"])
        assert (
            _create_schedule(
                env,
                CUST_HEADERS["cust-002"],
                {
                    "target_type": "artifact",
                    "target_id": str(dashboard["id"]),
                    "interval_minutes": 360,
                },
            ).status_code
            == 404
        )

    def test_quota_per_customer(self, env, monkeypatch):
        monkeypatch.setenv("FD_WIRE_SCHEDULE_MAX_PER_CUSTOMER", "2")
        reset_schedules_runtime()
        init_schedules_db()
        headers = CUST_HEADERS["cust-001"]
        child = _derived_dataset(env, headers)
        for _ in range(2):
            assert (
                _create_schedule(
                    env,
                    headers,
                    {"target_type": "dataset", "target_id": child["id"], "interval_minutes": 60},
                ).status_code
                == 201
            )
        third = _create_schedule(
            env,
            headers,
            {"target_type": "dataset", "target_id": child["id"], "interval_minutes": 90},
        )
        assert third.status_code == 400
        assert "上限" in third.json()["detail"]
        # 配额是每客户口径：他企不受挤占
        other = _derived_dataset(env, headers=CUST_HEADERS["cust-002"], name="配额他企")
        assert (
            _create_schedule(
                env,
                CUST_HEADERS["cust-002"],
                {"target_type": "dataset", "target_id": other["id"], "interval_minutes": 60},
            ).status_code
            == 201
        )


class TestCrudLifecycle:
    def test_list_pause_resume_delete_and_isolation(self, env):
        headers = CUST_HEADERS["cust-001"]
        child_a = _derived_dataset(env, name="链 A")
        child_b = _derived_dataset(env, name="链 B")
        other = _derived_dataset(env, headers=CUST_HEADERS["cust-002"], name="他企")
        first = _create_schedule(
            env, headers, {"target_type": "dataset", "target_id": child_a["id"], "interval_minutes": 60}
        ).json()
        second = _create_schedule(
            env, headers, {"target_type": "dataset", "target_id": child_b["id"], "interval_minutes": 120}
        ).json()
        _create_schedule(
            env,
            CUST_HEADERS["cust-002"],
            {"target_type": "dataset", "target_id": other["id"], "interval_minutes": 60},
        )

        # 列表隔离：只看到本人两行
        listed = env.client.get("/api/v1/refresh-schedules", headers=headers).json()
        assert {row["id"] for row in listed} == {first["id"], second["id"]}

        # 暂停/恢复
        paused = env.client.post(
            f"/api/v1/refresh-schedules/{first['id']}/pause", headers=headers
        )
        assert paused.status_code == 200 and paused.json()["paused"] is True
        resumed = env.client.post(
            f"/api/v1/refresh-schedules/{first['id']}/resume", headers=headers
        )
        assert resumed.status_code == 200 and resumed.json()["paused"] is False

        # 跨客户 404（不泄露存在性）
        assert (
            env.client.post(
                f"/api/v1/refresh-schedules/{first['id']}/pause",
                headers=CUST_HEADERS["cust-002"],
            ).status_code
            == 404
        )
        assert (
            env.client.delete(
                f"/api/v1/refresh-schedules/{first['id']}",
                headers=CUST_HEADERS["cust-002"],
            ).status_code
            == 404
        )

        # 删除 + 列表收缩；再删 404（幂等 404 语义）
        removed = env.client.delete(
            f"/api/v1/refresh-schedules/{first['id']}", headers=headers
        )
        assert removed.status_code == 200 and removed.json()["removed"] is True
        after = env.client.get("/api/v1/refresh-schedules", headers=headers).json()
        assert {row["id"] for row in after} == {second["id"]}
        assert (
            env.client.delete(
                f"/api/v1/refresh-schedules/{first['id']}", headers=headers
            ).status_code
            == 404
        )

    def test_unauthenticated_401(self, env):
        assert env.client.get("/api/v1/refresh-schedules").status_code == 401
        assert env.client.post("/api/v1/refresh-schedules", json={}).status_code == 401


# ------------------------------------------------- 调度循环（注入时钟+假执行器）


class TestRunnerTick:
    def _make_row(self, env, *, interval=60, now=None):
        """直接经服务建行（绕过 HTTP），返回 (row_id, next_run_at)。"""
        child = _derived_dataset(env)
        with get_schedules_session() as db:
            row = schedules_service.create_schedule(
                db,
                customer_id="cust-001",
                target_type="dataset",
                target_id=child["id"],
                interval_minutes=interval,
                now=now,
            )
            return row.id, row.next_run_at

    def _get_row(self, row_id) -> RefreshSchedule:
        with get_schedules_session() as db:
            return db.get(RefreshSchedule, row_id)

    def test_not_due_is_skipped_then_due_executes(self, env):
        t0 = utc_now_naive()
        row_id, next_run = self._make_row(env, interval=60, now=t0)
        assert next_run == t0 + timedelta(minutes=60)

        calls: list[int] = []
        early = runner_mod.tick_once(now=t0 + timedelta(minutes=30), execute=lambda r: calls.append(r.id))
        assert early == {"due": 0, "ok": 0, "failed": 0, "removed": 0}
        assert calls == []

        t1 = t0 + timedelta(minutes=61)
        hit = runner_mod.tick_once(now=t1, execute=lambda r: calls.append(r.id))
        assert hit == {"due": 1, "ok": 1, "failed": 0, "removed": 0}
        assert calls == [row_id]
        row = self._get_row(row_id)
        assert row.last_status == "ok"
        assert row.last_run_at == t1
        assert row.next_run_at == t1 + timedelta(minutes=60)  # 前移一个间隔

        # 执行后立即再 tick（未到新 next_run_at）→ 空转
        again = runner_mod.tick_once(now=t1 + timedelta(minutes=1), execute=lambda r: calls.append(r.id))
        assert again["due"] == 0 and calls == [row_id]

    def test_failure_records_error_and_keeps_looping(self, env):
        t0 = utc_now_naive()
        row_id, _ = self._make_row(env, interval=60, now=t0)

        def boom(row):
            raise RuntimeError("引擎拒绝: boom")

        t1 = t0 + timedelta(minutes=61)
        stats = runner_mod.tick_once(now=t1, execute=boom)
        assert stats == {"due": 1, "ok": 0, "failed": 1, "removed": 0}
        row = self._get_row(row_id)
        assert row.last_status == "failed"
        assert "boom" in row.last_error
        assert row.next_run_at == t1 + timedelta(minutes=60)  # 失败也前移（下轮再试）

    def test_paused_schedule_not_due(self, env):
        t0 = utc_now_naive()
        row_id, _ = self._make_row(env, interval=60, now=t0)
        with get_schedules_session() as db:
            schedules_service.set_paused(db, row_id, "cust-001", paused=True)
        calls: list[int] = []
        stats = runner_mod.tick_once(
            now=t0 + timedelta(minutes=61), execute=lambda r: calls.append(r.id)
        )
        assert stats["due"] == 0 and calls == []

    def test_target_gone_removes_row(self, env):
        t0 = utc_now_naive()
        row_id, _ = self._make_row(env, interval=60, now=t0)

        def gone(row):
            raise executor_mod.TargetGone("数据集已删除")

        stats = runner_mod.tick_once(now=t0 + timedelta(minutes=61), execute=gone)
        assert stats == {"due": 1, "ok": 0, "failed": 0, "removed": 1}
        assert self._get_row(row_id) is None  # 行已自动清理

    def test_multiple_due_ordered_by_next_run_at(self, env):
        t0 = utc_now_naive()
        early_id, _ = self._make_row(env, interval=60, now=t0 - timedelta(minutes=30))
        late_id, _ = self._make_row(env, interval=120, now=t0)
        calls: list[int] = []
        stats = runner_mod.tick_once(now=t0 + timedelta(minutes=125), execute=lambda r: calls.append(r.id))
        assert stats["due"] == 2
        assert calls == [early_id, late_id]  # 最久欠账的先执行

    def test_start_disabled_is_noop(self, env, monkeypatch):
        import asyncio

        monkeypatch.setenv("FD_WIRE_REFRESH_SCHEDULER_ENABLED", "false")
        reset_schedules_runtime()
        runner_mod.stop_refresh_scheduler()  # 清掉 app lifespan 可能已起的环

        async def _call() -> None:
            runner_mod.start_refresh_scheduler()  # 需在事件循环内（同 metering）

        asyncio.run(_call())
        assert runner_mod._task is None  # disabled → 不起环


# ------------------------------------------------- 执行器真身（假引擎/假 LLM）


class TestExecutorIntegration:
    def test_dataset_path_refreshes_with_cascade(self, env):
        child = _derived_dataset(env)
        # 孙链：child → grandchild（级联应传导到孙）
        grandchild = _derive(env, CUST_HEADERS["cust-001"], child["id"], name="孙集")
        t0 = utc_now_naive()
        with get_schedules_session() as db:
            row = schedules_service.create_schedule(
                db,
                customer_id="cust-001",
                target_type="dataset",
                target_id=child["id"],
                interval_minutes=60,
                now=t0,
            )
            row_id = row.id
        stats = runner_mod.tick_once(now=t0 + timedelta(minutes=61))
        assert stats == {"due": 1, "ok": 1, "failed": 0, "removed": 0}
        refreshed_keys = [args["dataset_key"] for args in env.fake.calls_for(TOOL_DERIVE_REFRESH)]
        assert refreshed_keys == [child["key"], grandchild["key"]]  # 本体+级联
        with get_schedules_session() as db:
            assert db.get(RefreshSchedule, row_id).last_status == "ok"

    def test_dataset_gone_cleans_row(self, env):
        child = _derived_dataset(env)
        t0 = utc_now_naive()
        with get_schedules_session() as db:
            row = schedules_service.create_schedule(
                db,
                customer_id="cust-001",
                target_type="dataset",
                target_id=child["id"],
                interval_minutes=60,
                now=t0,
            )
            row_id = row.id
        # 数据集删除（硬删本地登记行）→ 调度行自动清理
        with get_datasets_session() as db:
            db.delete(db.get(DatasetRow, child["id"]))
            db.commit()
        stats = runner_mod.tick_once(now=t0 + timedelta(minutes=61))
        assert stats["removed"] == 1
        with get_schedules_session() as db:
            assert db.get(RefreshSchedule, row_id) is None

    def test_artifact_path_refreshes_and_versions(self, env):
        dashboard = _make_artifact(env, CUST_HEADERS["cust-001"])
        assert dashboard["version"] == 1
        executor_mod._llm_factory_override = lambda: FakeLlm(
            [text_reply(json.dumps(DASH_SPEC))]
        )
        executor_mod._tool_executor_factory_override = lambda cid: FakeExecutor()
        t0 = utc_now_naive()
        with get_schedules_session() as db:
            row = schedules_service.create_schedule(
                db,
                customer_id="cust-001",
                target_type="artifact",
                target_id=str(dashboard["id"]),
                interval_minutes=360,
                now=t0,
            )
            row_id = row.id
        stats = runner_mod.tick_once(now=t0 + timedelta(minutes=361))
        assert stats == {"due": 1, "ok": 1, "failed": 0, "removed": 0}
        detail = env.client.get(
            f"/api/v1/artifacts/{dashboard['id']}", headers=CUST_HEADERS["cust-001"]
        ).json()
        assert detail["version"] == 2  # 无头轮成功落了新版本
        with get_schedules_session() as db:
            saved = db.get(RefreshSchedule, row_id)
            assert saved.last_status == "ok" and saved.last_error is None

    def test_artifact_archived_cleans_row(self, env):
        dashboard = _make_artifact(env, CUST_HEADERS["cust-001"])
        t0 = utc_now_naive()
        with get_schedules_session() as db:
            row = schedules_service.create_schedule(
                db,
                customer_id="cust-001",
                target_type="artifact",
                target_id=str(dashboard["id"]),
                interval_minutes=360,
                now=t0,
            )
            row_id = row.id
        archived = env.client.post(
            f"/api/v1/artifacts/{dashboard['id']}/archive", headers=CUST_HEADERS["cust-001"]
        )
        assert archived.status_code == 200
        stats = runner_mod.tick_once(now=t0 + timedelta(minutes=361))
        assert stats["removed"] == 1  # 归档即失存 → 清行
        with get_schedules_session() as db:
            assert db.get(RefreshSchedule, row_id) is None

    def test_artifact_refresh_failure_marks_failed(self, env):
        dashboard = _make_artifact(env, CUST_HEADERS["cust-001"])
        # 终答垃圾 → RefreshFailed → 调度记 failed+last_error（零半写）
        executor_mod._llm_factory_override = lambda: FakeLlm([text_reply("不是 JSON")])
        executor_mod._tool_executor_factory_override = lambda cid: FakeExecutor()
        t0 = utc_now_naive()
        with get_schedules_session() as db:
            row = schedules_service.create_schedule(
                db,
                customer_id="cust-001",
                target_type="artifact",
                target_id=str(dashboard["id"]),
                interval_minutes=360,
                now=t0,
            )
            row_id = row.id
        stats = runner_mod.tick_once(now=t0 + timedelta(minutes=361))
        assert stats["failed"] == 1
        detail = env.client.get(
            f"/api/v1/artifacts/{dashboard['id']}", headers=CUST_HEADERS["cust-001"]
        ).json()
        assert detail["version"] == 1  # 失败零半写
        with get_schedules_session() as db:
            saved = db.get(RefreshSchedule, row_id)
            assert saved.last_status == "failed" and "解析" in (saved.last_error or "")


# ------------------------------------------------- 级联刷新（手动端点）


class TestCascadeRefresh:
    def _chain(self, env, headers=None):
        """三层链：source（根）→ mid → leaf（refresh mid 时级联到 leaf）。"""
        headers = headers or CUST_HEADERS["cust-001"]
        source = _make_dataset(env, headers, name="链根")
        mid = _derive(env, headers, source["id"], name="中层")
        leaf = _derive(env, headers, mid["id"], name="叶层")
        return source, mid, leaf

    def test_three_level_chain_propagates(self, env):
        _, mid, leaf = self._chain(env)
        resp = env.client.post(
            f"/api/v1/datasets/{mid['id']}/refresh", headers=CUST_HEADERS["cust-001"]
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 顶层字段保持数据集形态（既有契约不动），额外附 cascade 摘要
        assert body["id"] == mid["id"] and body["source_kind"] == "derived"
        assert [c["dataset_id"] for c in body["cascade"]] == [leaf["id"]]
        assert all(c["status"] == "ok" for c in body["cascade"])
        assert body["cascade"][0]["depth"] == 1
        # 引擎调用序：本体 → 级联子（transitively）
        refreshed = [a["dataset_key"] for a in env.fake.calls_for(TOOL_DERIVE_REFRESH)]
        assert refreshed == [mid["key"], leaf["key"]]

    def test_cascade_false_disables_propagation(self, env):
        _, mid, leaf = self._chain(env)
        resp = env.client.post(
            f"/api/v1/datasets/{mid['id']}/refresh?cascade=false",
            headers=CUST_HEADERS["cust-001"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["cascade"] == []
        refreshed = [a["dataset_key"] for a in env.fake.calls_for(TOOL_DERIVE_REFRESH)]
        assert refreshed == [mid["key"]]  # 只刷本体

    def test_cycle_guard_terminates(self, env):
        _, mid, leaf = self._chain(env)
        # 异常数据态：把 mid 的来源指到 leaf，mid↔leaf 成环
        with get_datasets_session() as db:
            row = db.get(DatasetRow, mid["id"])
            row.source_ref = leaf["key"]
            db.commit()
        resp = env.client.post(
            f"/api/v1/datasets/{mid['id']}/refresh", headers=CUST_HEADERS["cust-001"]
        )
        assert resp.status_code == 200, resp.text  # visited 阻断，不无限递归
        refreshed = [a["dataset_key"] for a in env.fake.calls_for(TOOL_DERIVE_REFRESH)]
        assert sorted(refreshed) == sorted([mid["key"], leaf["key"]])  # 各恰好一次

    def test_failure_isolated_rest_continue(self, env):
        _, mid, leaf = self._chain(env)
        # 子环失败：本体成功，级联子失败被记录（摘要透出），端点不 5xx——
        # 其余环继续（这里链只有一环；深链版本见 depth 用例）
        env.fake.fail_refresh_keys = {leaf["key"]}
        resp = env.client.post(
            f"/api/v1/datasets/{mid['id']}/refresh", headers=CUST_HEADERS["cust-001"]
        )
        assert resp.status_code == 200, resp.text
        cascade = resp.json()["cascade"]
        assert cascade[0]["dataset_id"] == leaf["id"]
        assert cascade[0]["status"] == "failed"
        assert "注入失败" in cascade[0]["error"]
        # 失败环的调用也确实发生（尝试过、被隔离记录）
        refreshed = [a["dataset_key"] for a in env.fake.calls_for(TOOL_DERIVE_REFRESH)]
        assert refreshed == [mid["key"], leaf["key"]]

        # 对照：本体失败仍如实 502（隔离只作用于级联环，不吞主路径错误）
        env.fake.fail_refresh_keys = {mid["key"]}
        env.fake.calls.clear()
        root_failed = env.client.post(
            f"/api/v1/datasets/{mid['id']}/refresh", headers=CUST_HEADERS["cust-001"]
        )
        assert root_failed.status_code == 502

    def test_deep_chain_respects_depth_cap(self, env):
        # 12 层链：从 c1 刷新向下级联——本体 + 深度 1..10 共 11 次后截断
        headers = CUST_HEADERS["cust-001"]
        current = _make_dataset(env, headers, name="深链根")
        nodes = []
        for i in range(12):
            child = _derive(env, headers, current["id"], name=f"深链 {i}")
            nodes.append(child)
            current = child
        resp = env.client.post(
            f"/api/v1/datasets/{nodes[0]['id']}/refresh", headers=headers
        )
        assert resp.status_code == 200, resp.text
        refreshed = [a["dataset_key"] for a in env.fake.calls_for(TOOL_DERIVE_REFRESH)]
        assert len(refreshed) == 11  # 本体 + 10 层级联即到深度上限
        assert refreshed[0] == nodes[0]["key"]
        assert nodes[11]["key"] not in refreshed  # 第 11 层之外不再刷新
        assert len(resp.json()["cascade"]) == 10


# ------------------------------------------------- 迁移链 0023（1.1）


@pytest.fixture()
def migrated_0023(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade 至 0023（链首 0001 起，全链真实迁移）。"""
    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    db_url = f"sqlite:///{tmp_path}/schedules_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    command.upgrade(cfg, "0023_refresh_schedules")
    return cfg, db_url


def test_migration_creates_refresh_schedules(migrated_0023) -> None:
    """0023 建表 + customer_id/next_run_at 索引；幂等重跑无恙。"""
    cfg, db_url = migrated_0023
    command.upgrade(cfg, "0023_refresh_schedules")  # 幂等重跑

    engine = create_engine(db_url)
    inspector = inspect(engine)
    assert "refresh_schedules" in inspector.get_table_names()
    assert {c["name"] for c in inspector.get_columns("refresh_schedules")} == {
        "id", "customer_id", "target_type", "target_id", "interval_minutes",
        "next_run_at", "paused", "last_status", "last_run_at", "last_error",
        "created_at",
    }
    assert {ix["name"] for ix in inspector.get_indexes("refresh_schedules")} >= {
        "ix_refresh_schedules_customer_id",
        "ix_refresh_schedules_next_run_at",
    }
    engine.dispose()


def test_migration_schema_matches_orm(migrated_0023) -> None:
    """drift check：迁移产出列集与 ORM 声明（SchedulesBase.metadata）一致。"""
    from fd_wire_api.schedules.db import SchedulesBase

    _, db_url = migrated_0023
    engine = create_engine(db_url)
    inspector = inspect(engine)
    migrated = {c["name"] for c in inspector.get_columns("refresh_schedules")}
    declared = set(SchedulesBase.metadata.tables["refresh_schedules"].columns.keys())
    assert migrated == declared, f"refresh_schedules 迁移与 ORM 列漂移: {migrated ^ declared}"
    engine.dispose()


def test_migration_downgrade_drops_table(migrated_0023) -> None:
    cfg, db_url = migrated_0023
    command.downgrade(cfg, "0022_pack_subscriptions")
    engine = create_engine(db_url)
    assert "refresh_schedules" not in inspect(engine).get_table_names()
    engine.dispose()
