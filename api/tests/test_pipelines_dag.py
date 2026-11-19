"""DAG 执行器测试（wire-dag-executor 批 2：D1–D6）。

覆盖（spec 场景对照）：

- D1 定义校验：有环 422 指名环边、引用不存在 422 指名、深度/节点数上限；
- D2 拓扑执行：fan-out/fan-in 并行时间线重叠可观测、节点行状态、fail-fast
  skipped、并发上限生效、线性 v1 逐字节等价；
- D3 租约队列：超窗返回 queued 并可轮询收敛、断点恢复不重算已成功节点、
  死租约重领取；
- D4 重试（仅可重试错误，退避可注入）、配额硬停不重试、节点超时、run 超预算；
- D5 取消：队列态就地收敛、在跑调度器下一周期收敛（未启动 skipped、在飞
  节点跑完）；
- D6 血缘多父：fan-in 节点父集 [A, B] 全登记。

环境：datasets 内存库 + materialization 临时库 + FakeEngine 并发观测子类。
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.assets.models import DatasetParentRef
from fd_wire_api.billing.models import Customer
from fd_wire_api.console.models import CustomerMember
from fd_wire_api.datasets import connector_routes
from fd_wire_api.datasets.db import get_session as get_datasets_session, reset_datasets_runtime
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    TOOL_DERIVE,
    TOOL_DERIVE_REFRESH,
    TOOL_UNION,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.materialization.db import reset_materialization_runtime
from fd_wire_api.pipelines import queue as pipeline_queue
from fd_wire_api.pipelines import service
from fd_wire_api.pipelines.executor import INTERRUPTED_REASON
from fd_wire_api.pipelines.models import (
    PipelineRun,
    PipelineRunNode,
    PipelineRunStep,
    TransformPipeline,
    TransformPipelineSlot,
)
from fd_wire_api.pipelines.settings import get_settings, reset_pipeline_settings

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token
from test_datasets_common import (
    CUST_HEADERS,
    INTERNAL_TOKEN,
    FakeEngine,
    make_uploaded_dataset,
)

SUB_EDITOR = "sub-dag-editor"
SUB_VIEWER = "sub-dag-viewer"
EDITOR = bearer(mint_id_token(SUB_EDITOR))
VIEWER = bearer(mint_id_token(SUB_VIEWER))


class DagFakeEngine(FakeEngine):
    """可观测并发 + 可注入延时/失败次数/调用的引擎假件。"""

    def __init__(self) -> None:
        super().__init__()
        self.delay_by_tool: dict[str, float] = {}
        self.default_delay = 0.0
        self.fail_counts: dict[str, int] = {}
        self.hooks: dict[str, object] = {}
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def call(self, tool: str, arguments: dict) -> dict:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            hook = self.hooks.get(tool)
            if hook is not None:
                hook(tool, arguments)
            delay = self.delay_by_tool.get(tool, self.default_delay)
            if delay:
                time.sleep(delay)
            remaining = self.fail_counts.get(tool, 0)
            if remaining > 0:
                self.fail_counts[tool] = remaining - 1
                self.calls.append((tool, dict(arguments)))
                raise EngineCallError(f"引擎超时（测试注入 {tool}）")
            return super().call(tool, arguments)
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture(autouse=True)
def _reset_pipeline_settings():
    reset_pipeline_settings()
    yield
    reset_pipeline_settings()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """datasets 文件库 + materialization 临时库 + 三角色种子 + DagFakeEngine。

    datasets 用**文件库**（非 ``sqlite://`` 内存库）：DAG 节点在独立线程各开
    Session，内存库的 StaticPool 单连接不支持并发 flush（执行器会在单连接
    池下如实降级为串行，那样就观测不到并行重叠）。
    """
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", f"sqlite:///{tmp_path}/datasets.db")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MATERIALIZATION_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db")
    monkeypatch.setenv("MATERIALIZATION_AUTO_CREATE_TABLES", "1")
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[("sub-cust-001", "cust-001"), ("sub-cust-002", "cust-002")],
        internal_token=INTERNAL_TOKEN,
        plan_code="basic",
    )
    from fd_wire_api.admin.db import get_session as platform_session

    with platform_session() as sess:
        cust = sess.execute(
            select(Customer).where(Customer.name == "cust-001")
        ).scalar_one()
        sess.add(CustomerMember(customer_id=cust.id, logto_sub=SUB_EDITOR, role="editor"))
        sess.add(CustomerMember(customer_id=cust.id, logto_sub=SUB_VIEWER, role="viewer"))
        sess.commit()

    reset_datasets_runtime()
    connector_routes._SYNC_SESSIONS.clear()
    reset_materialization_runtime()

    fake = DagFakeEngine()
    fastapi_app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: fake
    )
    with TestClient(fastapi_app) as client:
        yield SimpleNamespace(client=client, fake=fake, app=fastapi_app)
    fastapi_app.dependency_overrides.clear()
    connector_routes._SYNC_SESSIONS.clear()
    reset_datasets_runtime()
    reset_materialization_runtime()


# ------------------------------------------------------------------ 助手


def set_pipeline_env(monkeypatch, **values) -> None:
    """设置执行器 env 并清配置缓存（执行时惰性读取，测试可逐用例切换）。"""
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    reset_pipeline_settings()


def make_pipeline(env, *, steps, base=None, name="DAG 管线") -> dict:
    if base is None:
        base = make_uploaded_dataset(env)
    resp = env.client.post(
        "/api/v1/pipelines",
        headers=CUST_HEADERS["cust-001"],
        json={
            "name": name,
            "description": "DAG 测试",
            "base_dataset_id": base["id"],
            "steps": steps,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def run(env, pipeline_id: str, *, version: int | None = None):
    body = {} if version is None else {"version": version}
    return env.client.post(
        f"/api/v1/pipelines/{pipeline_id}/run",
        headers=CUST_HEADERS["cust-001"],
        json=body,
    )


def run_detail(env, pipeline_id: str, run_id: int) -> dict:
    return env.client.get(
        f"/api/v1/pipelines/{pipeline_id}/runs/{run_id}",
        headers=CUST_HEADERS["cust-001"],
    ).json()


def nodes_of(run_id: int) -> dict[str, PipelineRunNode]:
    with get_datasets_session() as db:
        rows = db.execute(
            select(PipelineRunNode).where(PipelineRunNode.run_id == run_id)
        ).scalars()
        return {row.node_key: row for row in rows}


def fake_factory(env):
    """引擎工厂形状：按 customer_id 返回假件（worker 路径直传工厂）。"""
    return lambda customer_id: env.fake


def set_cancel(run_id: int) -> None:
    """模拟另一请求的取消（独立会话，避免与执行线程共享状态）。"""
    with get_datasets_session() as db:
        row = db.get(PipelineRun, run_id)
        row.cancel_requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()


FAN_OUT_STEPS = [
    {"name": "A", "op": "derive", "inputs": ["base"], "params": {"filters": []}},
    {"name": "B", "op": "derive", "inputs": ["base"], "params": {"sort": []}},
    {"name": "C", "op": "union", "inputs": ["A", "B"], "params": {"on": "by_name"}},
]


# ------------------------------------------------------------------ D1 定义校验


class TestDefinitionValidation:
    def test_cycle_rejected_422_names_edge(self, env):
        base = make_uploaded_dataset(env)
        steps = [
            {"name": "step-2", "op": "derive", "inputs": ["step-3"], "params": {}},
            {"name": "step-3", "op": "derive", "inputs": ["step-2"], "params": {}},
        ]
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={"name": "环", "base_dataset_id": base["id"], "steps": steps},
        )
        assert resp.status_code == 422, resp.text
        detail = str(resp.json()["detail"])
        assert "环" in detail and "step-2" in detail and "step-3" in detail
        with get_datasets_session() as db:
            assert db.execute(select(TransformPipeline)).scalars().all() == []

    def test_unknown_reference_422_names_it(self, env):
        base = make_uploaded_dataset(env)
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={
                "name": "未知引用",
                "base_dataset_id": base["id"],
                "steps": [
                    {"name": "A", "op": "derive", "inputs": ["不存在的步骤"], "params": {}}
                ],
            },
        )
        assert resp.status_code == 422, resp.text
        assert "不存在的步骤" in str(resp.json()["detail"])

    def test_depth_limit(self, env):
        base = make_uploaded_dataset(env)
        steps = []
        for index in range(1, 12):  # 11 层链 > 10
            steps.append(
                {
                    "name": f"n{index}",
                    "op": "derive",
                    "inputs": ["base"] if index == 1 else [f"n{index - 1}"],
                    "params": {},
                }
            )
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={"name": "深", "base_dataset_id": base["id"], "steps": steps},
        )
        assert resp.status_code == 422, resp.text
        assert "深度" in str(resp.json()["detail"])

    def test_node_count_limit(self, env):
        base = make_uploaded_dataset(env)
        steps = [
            {"name": f"n{index}", "op": "derive", "inputs": ["base"], "params": {}}
            for index in range(1, 52)
        ]
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={"name": "宽", "base_dataset_id": base["id"], "steps": steps},
        )
        assert resp.status_code == 422, resp.text  # 51 > 50（DAG 节点护栏）

        ok = steps[:50]
        resp_ok = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={"name": "宽50", "base_dataset_id": base["id"], "steps": ok},
        )
        assert resp_ok.status_code == 201, resp_ok.text

    def test_linear_v1_keeps_ten_step_guard(self, env):
        """v1 线性链仍是 10 步护栏（DAG 才放宽到 50——v1 语义零漂移）。"""
        base = make_uploaded_dataset(env)
        steps = [{"name": f"s{index}", "op": "derive", "params": {}} for index in range(11)]
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={"name": "v1 超长", "base_dataset_id": base["id"], "steps": steps},
        )
        assert resp.status_code == 422
        assert "上限 10" in str(resp.json()["detail"])

    def test_reserved_base_name_rejected(self, env):
        base = make_uploaded_dataset(env)
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={
                "name": "保留名",
                "base_dataset_id": base["id"],
                "steps": [
                    {"name": "base", "op": "derive", "inputs": ["base"], "params": {}}
                ],
            },
        )
        assert resp.status_code == 422
        assert "保留名" in str(resp.json()["detail"])

    def test_duplicate_names_rejected_in_dag(self, env):
        base = make_uploaded_dataset(env)
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={
                "name": "重名",
                "base_dataset_id": base["id"],
                "steps": [
                    {"name": "X", "op": "derive", "inputs": ["base"], "params": {}},
                    {"name": "X", "op": "derive", "inputs": ["X"], "params": {}},
                ],
            },
        )
        assert resp.status_code == 422
        assert "重复" in str(resp.json()["detail"])


# ------------------------------------------------------------------ D2 拓扑执行


class TestTopologicalExecution:
    def test_fan_out_fan_in_parallel_and_lineage(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=2)
        env.fake.delay_by_tool[TOOL_DERIVE] = 0.3
        pipeline = make_pipeline(env, steps=FAN_OUT_STEPS)
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "success"
        nodes = {n["node_key"]: n for n in body["nodes"]}
        assert set(nodes) == {"A", "B", "C"}
        assert all(n["status"] == "succeeded" for n in nodes.values())
        assert nodes["C"]["op"] == "union"

        # 并行可观测：A/B 的引擎调用时间窗重叠
        assert env.fake.max_active >= 2, env.fake.max_active

        # C 在 A/B 之后启动（C 的 started_at 不早于 A/B 的 finished_at）
        a_fin = nodes["A"]["finished_at"]
        b_fin = nodes["B"]["finished_at"]
        c_start = nodes["C"]["started_at"]
        assert a_fin and b_fin and c_start
        assert c_start >= min(a_fin, b_fin)

        # union 右集 = B 输出（inputs 声明即契约）
        union_args = env.fake.calls_for(TOOL_UNION)[0]
        b_out = nodes["B"]["output_dataset_id"]
        with get_datasets_session() as db:
            assert union_args["right_key"] == db.get(Dataset, b_out).key
            assert union_args["left_key"] == db.get(
                Dataset, nodes["A"]["output_dataset_id"]
            ).key

        # 血缘多父：C 的父集 = [A 输出, B 输出]
        with get_datasets_session() as db:
            parents = {
                row[0]
                for row in db.execute(
                    select(DatasetParentRef.parent_dataset_id).where(
                        DatasetParentRef.child_dataset_id
                        == nodes["C"]["output_dataset_id"]
                    )
                ).all()
            }
        assert parents == {nodes["A"]["output_dataset_id"], nodes["B"]["output_dataset_id"]}

        # 槽位按 node_key 写入
        with get_datasets_session() as db:
            slots = db.execute(
                select(TransformPipelineSlot).where(
                    TransformPipelineSlot.pipeline_id == pipeline["id"]
                )
            ).scalars()
            assert {s.node_key for s in slots} == {"A", "B", "C"}

    def test_concurrency_limit_respected(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=1)
        env.fake.delay_by_tool[TOOL_DERIVE] = 0.2
        steps = [
            {"name": f"n{index}", "op": "derive", "inputs": ["base"], "params": {}}
            for index in range(1, 5)
        ]
        pipeline = make_pipeline(env, steps=steps, name="四分支")
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "success"
        assert env.fake.max_active == 1  # 并发上限生效

    def test_concurrency_config_clamped(self, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=0)
        assert get_settings().effective_concurrency == 1
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=99)
        assert get_settings().effective_concurrency == 8

    def test_fail_fast_marks_unstarted_skipped(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=1)
        steps = [
            {"name": "A", "op": "derive", "inputs": ["base"], "params": {}},
            {"name": "B", "op": "derive", "inputs": ["A"], "params": {}},
            {"name": "C", "op": "derive", "inputs": ["B"], "params": {}},
        ]
        pipeline = make_pipeline(env, steps=steps, name="失败链")
        # 第二次 derive 调用失败（A=第 1 次，B=第 2 次）
        original = env.fake.call
        counter = {"n": 0}

        def call_with_failure(tool, arguments):
            if tool == TOOL_DERIVE:
                counter["n"] += 1
                if counter["n"] >= 2:
                    env.fake.calls.append((tool, dict(arguments)))
                    raise EngineCallError("B 步骤引擎爆炸")
            return original(tool, arguments)

        monkeypatch.setattr(env.fake, "call", call_with_failure)
        body = run(env, pipeline["id"]).json()
        nodes = {n["node_key"]: n for n in body["nodes"]}
        assert body["run"]["status"] == "failed"
        assert nodes["A"]["status"] == "succeeded"
        assert nodes["B"]["status"] == "failed"
        assert nodes["C"]["status"] == "skipped"
        assert "B" in body["run"]["error_text"]
        # step 行保持失败隔离：未执行节点无 step 行
        assert [s["seq"] for s in body["steps"]] == [1, 2]

    def test_linear_v1_byte_equivalence(self, env):
        """无 inputs 的版本：run/step/槽位形状与变更前逐字节一致。"""
        base = make_uploaded_dataset(env)
        steps = [
            {"name": "清洗", "op": "derive", "params": {"filters": [], "limit": 5}},
            {"name": "过滤", "op": "filter", "params": {"filters": []}},
        ]
        pipeline = make_pipeline(env, base=base, steps=steps, name="线性链")
        resp = run(env, pipeline["id"])
        body = resp.json()
        assert set(body["run"]) == {
            "id",
            "pipeline_id",
            "version",
            "trigger",
            "status",
            "started_at",
            "finished_at",
            "error_text",
        }
        assert body["run"]["status"] == "success"
        assert [s["seq"] for s in body["steps"]] == [1, 2]
        assert [s["name"] for s in body["steps"]] == ["清洗", "过滤"]
        assert [s["op"] for s in body["steps"]] == ["derive", "filter"]
        assert [s["status"] for s in body["steps"]] == ["success", "success"]
        assert all(s["output_dataset_id"] for s in body["steps"])

        detail = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}", headers=CUST_HEADERS["cust-001"]
        ).json()
        assert "inputs" not in detail["versions"][0]["steps"][0]  # 无 inputs 键

        with get_datasets_session() as db:
            slots = list(
                db.execute(
                    select(TransformPipelineSlot)
                    .where(TransformPipelineSlot.pipeline_id == pipeline["id"])
                    .order_by(TransformPipelineSlot.seq)
                ).scalars()
            )
            assert [s.seq for s in slots] == [1, 2]
            assert [s.node_key for s in slots] == ["清洗", "过滤"]

        # 节点行（新增簿记）与 step 行状态一致
        nodes = nodes_of(body["run"]["id"])
        assert {k: v.status for k, v in nodes.items()} == {
            "清洗": "succeeded",
            "过滤": "succeeded",
        }

    def test_rerun_uses_refresh_and_keeps_slots(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=2)
        pipeline = make_pipeline(env, steps=FAN_OUT_STEPS)
        first = run(env, pipeline["id"]).json()
        out_ids = {n["node_key"]: n["output_dataset_id"] for n in first["nodes"]}
        second = run(env, pipeline["id"]).json()
        assert second["run"]["status"] == "success"
        assert {
            n["node_key"]: n["output_dataset_id"] for n in second["nodes"]
        } == out_ids
        assert len(env.fake.calls_for(TOOL_UNION)) == 1  # 重跑走刷新不新建
        assert len(env.fake.calls_for(TOOL_DERIVE_REFRESH)) == 3
        with get_datasets_session() as db:
            slots = db.execute(
                select(TransformPipelineSlot).where(
                    TransformPipelineSlot.pipeline_id == pipeline["id"]
                )
            ).scalars()
            assert len(list(slots)) == 3


# ------------------------------------------------------------------ D3 租约队列/恢复


class TestLeaseQueueAndResume:
    def test_queued_beyond_window_then_poll_converges(self, env, monkeypatch):
        """超窗返回 queued run + 运行 id；随后 worker 领取并收敛为 success。"""
        set_pipeline_env(monkeypatch, PIPELINE_RUN_WAIT_SECONDS=0)
        pipeline = make_pipeline(
            env,
            steps=[{"name": "A", "op": "derive", "inputs": ["base"], "params": {}}],
            name="短窗",
        )
        with monkeypatch.context() as m:
            m.setattr(pipeline_queue, "worker_active", lambda: True)
            resp = run(env, pipeline["id"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["run"]["status"] == "queued"
        run_id = body["run"]["id"]
        assert nodes_of(run_id)["A"].status == "queued"

        # worker 领取执行（同一执行核心）
        result = pipeline_queue.worker_tick("worker-test", engine_factory=lambda cid: env.fake)
        assert result["claimed"] == 1
        detail = run_detail(env, pipeline["id"], run_id)
        assert detail["run"]["status"] == "success"
        assert detail["nodes"][0]["status"] == "succeeded"

    def test_resume_after_interruption_skips_succeeded(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=1)
        steps = [
            {"name": "A", "op": "derive", "inputs": ["base"], "params": {}},
            {"name": "B", "op": "derive", "inputs": ["A"], "params": {}},
            {"name": "C", "op": "derive", "inputs": ["B"], "params": {}},
        ]
        pipeline = make_pipeline(env, steps=steps, name="恢复链")
        first = run(env, pipeline["id"]).json()
        assert first["run"]["status"] == "success"
        a_out = next(n for n in first["nodes"] if n["node_key"] == "A")["output_dataset_id"]
        with get_datasets_session() as db:
            a_rows = db.get(Dataset, a_out).row_count
        refresh_before = len(env.fake.calls_for(TOOL_DERIVE_REFRESH))

        # 建第二条 run 并模拟 worker 中断：A 已成功、B 执行中、C 未启动
        with get_datasets_session() as db:
            pipe = db.get(TransformPipeline, pipeline["id"])
            run_row = service.enqueue_run(db, pipe)
            run_id = run_row.id
        with get_datasets_session() as db:
            nodes = {
                row.node_key: row
                for row in db.execute(
                    select(PipelineRunNode).where(PipelineRunNode.run_id == run_id)
                ).scalars()
            }
            nodes["A"].status = "succeeded"
            nodes["A"].attempts = 1
            nodes["A"].output_dataset_id = a_out
            nodes["A"].row_count = a_rows
            nodes["B"].status = "running"
            nodes["B"].attempts = 1
            nodes["B"].started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            run_row = db.get(PipelineRun, run_id)
            run_row.status = "running"
            run_row.claimed_by = "worker-dead"
            run_row.heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=120
            )
            db.commit()

        result = pipeline_queue.worker_tick("worker-live", engine_factory=lambda cid: env.fake)
        assert result["claimed"] == 1 and result["run_id"] == run_id
        detail = run_detail(env, pipeline["id"], run_id)
        assert detail["run"]["status"] == "success"
        nodes = {n["node_key"]: n for n in detail["nodes"]}
        assert nodes["A"]["status"] == "succeeded"
        assert nodes["A"]["attempts"] == 1  # 不重算已成功节点
        assert nodes["B"]["status"] == "succeeded"
        assert nodes["B"]["attempts"] == 2  # 中断消耗一次 + 重跑一次
        assert INTERRUPTED_REASON in nodes["B"]["error_text"]  # 中断事实保留
        assert nodes["C"]["status"] == "succeeded"

        # 只有 B/C 重放（A 不重算）：刷新调用恰 2 次
        assert len(env.fake.calls_for(TOOL_DERIVE_REFRESH)) - refresh_before == 2
        with get_datasets_session() as db:
            assert db.get(Dataset, a_out).row_count == a_rows

    def test_claim_skips_fresh_lease(self, env):
        """心跳新鲜的在飞 run 不被其他 worker 抢（租约语义）。"""
        pipeline = make_pipeline(
            env,
            steps=[{"name": "A", "op": "derive", "inputs": ["base"], "params": {}}],
            name="租约",
        )
        with get_datasets_session() as db:
            pipe = db.get(TransformPipeline, pipeline["id"])
            run_row = service.enqueue_run(db, pipe)
            run_id = run_row.id
        with get_datasets_session() as db:
            row = db.get(PipelineRun, run_id)
            row.claimed_by = "worker-a"
            row.status = "running"
            row.heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
        with get_datasets_session() as db:
            assert pipeline_queue.claim_next_run(db, "worker-b") is None

    def test_viewer_cannot_run_or_cancel(self, env):
        pipeline = make_pipeline(
            env,
            steps=[{"name": "A", "op": "derive", "inputs": ["base"], "params": {}}],
            name="角色",
        )
        assert run(env, pipeline["id"]).status_code == 200
        run_id = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}/runs",
            headers=CUST_HEADERS["cust-001"],
        ).json()["items"][0]["id"]
        resp = env.client.post(
            f"/api/v1/pipelines/{pipeline['id']}/runs/{run_id}/cancel", headers=VIEWER
        )
        assert resp.status_code == 403


# ------------------------------------------------------------------ D4 重试/超时/预算


class TestRetryTimeoutBudget:
    def test_retry_retryable_until_success(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RETRY_BACKOFF_SECONDS="0,0")
        env.fake.fail_counts[TOOL_DERIVE] = 2  # 前两次超时，第三次成功
        pipeline = make_pipeline(
            env,
            steps=[
                {
                    "name": "A",
                    "op": "derive",
                    "inputs": ["base"],
                    "retries": 2,
                    "params": {},
                }
            ],
            name="重试",
        )
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "success"
        assert body["nodes"][0]["status"] == "succeeded"
        assert body["nodes"][0]["attempts"] == 3

    def test_quota_hardstop_never_retried(self, env, monkeypatch):
        from fd_wire_api.datasets import service as ds_service

        pipeline = make_pipeline(
            env,
            steps=[
                {
                    "name": "A",
                    "op": "derive",
                    "inputs": ["base"],
                    "retries": 2,
                    "params": {},
                }
            ],
            name="配额",
        )

        def quota(db, *, customer_id):
            raise ds_service.QuotaExceeded("存储用量已达配额（测试注入）")

        monkeypatch.setattr(ds_service, "ensure_new_dataset_room", quota)
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "failed"
        assert body["nodes"][0]["status"] == "failed"
        assert body["nodes"][0]["attempts"] == 1  # 配额硬停不重试
        assert body["steps"][0]["status"] == "quota_exhausted"
        assert "配额" in body["nodes"][0]["error_text"]

    def test_node_timeout_fails(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_NODE_TIMEOUT_SECONDS=0.4)
        env.fake.delay_by_tool[TOOL_DERIVE] = 2.0
        pipeline = make_pipeline(
            env,
            steps=[{"name": "A", "op": "derive", "inputs": ["base"], "params": {}}],
            name="超时",
        )
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "failed"
        assert body["nodes"][0]["status"] == "failed"
        assert "超时" in body["nodes"][0]["error_text"]

    def test_run_budget_skips_unstarted(self, env, monkeypatch):
        set_pipeline_env(
            monkeypatch, PIPELINE_RUN_BUDGET_SECONDS=0.5, PIPELINE_RUN_CONCURRENCY=1
        )
        env.fake.delay_by_tool[TOOL_DERIVE] = 1.2
        pipeline = make_pipeline(
            env,
            steps=[
                {"name": "A", "op": "derive", "inputs": ["base"], "params": {}},
                {"name": "B", "op": "derive", "inputs": ["A"], "params": {}},
            ],
            name="预算",
        )
        body = run(env, pipeline["id"]).json()
        nodes = {n["node_key"]: n for n in body["nodes"]}
        assert nodes["A"]["status"] == "succeeded"  # 在飞节点跑完
        assert nodes["B"]["status"] == "skipped"
        assert body["run"]["status"] == "failed"
        assert "运行超预算" in body["run"]["error_text"]


# ------------------------------------------------------------------ D5 取消


class TestCancel:
    def test_cancel_queued_run_converges(self, env):
        pipeline = make_pipeline(
            env,
            steps=[
                {"name": "A", "op": "derive", "inputs": ["base"], "params": {}},
                {"name": "B", "op": "derive", "inputs": ["A"], "params": {}},
            ],
            name="队列取消",
        )
        with get_datasets_session() as db:
            pipe = db.get(TransformPipeline, pipeline["id"])
            run_row = service.enqueue_run(db, pipe)
            run_id = run_row.id
        resp = env.client.post(
            f"/api/v1/pipelines/{pipeline['id']}/runs/{run_id}/cancel",
            headers=CUST_HEADERS["cust-001"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["run"]["status"] == "cancelled"
        assert {n["status"] for n in body["nodes"]} == {"skipped"}
        assert env.fake.calls_for(TOOL_DERIVE) == []  # 未启动节点不触引擎

    def test_cancel_during_run_skips_rest(self, env, monkeypatch):
        set_pipeline_env(monkeypatch, PIPELINE_RUN_CONCURRENCY=1)
        steps = [
            {"name": "A", "op": "derive", "inputs": ["base"], "params": {}},
            {"name": "B", "op": "derive", "inputs": ["A"], "params": {}},
        ]
        pipeline = make_pipeline(env, steps=steps, name="运行中取消")
        with get_datasets_session() as db:
            pipe = db.get(TransformPipeline, pipeline["id"])
            run_row = service.enqueue_run(db, pipe)
            run_id = run_row.id
        # A 执行时（引擎调用中）另一请求取消：A 跑完、B 不启动
        env.fake.hooks[TOOL_DERIVE] = lambda tool, args: set_cancel(run_id)
        result = pipeline_queue.worker_tick("worker-cancel", engine_factory=lambda cid: env.fake)
        assert result["claimed"] == 1
        detail = run_detail(env, pipeline["id"], run_id)
        assert detail["run"]["status"] == "cancelled"
        nodes = {n["node_key"]: n for n in detail["nodes"]}
        assert nodes["A"]["status"] == "succeeded"  # 在飞节点跑完，输出保留
        assert nodes["B"]["status"] == "skipped"
        assert nodes["A"]["output_dataset_id"]
