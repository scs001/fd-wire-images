"""变换管线 API 测试（wire-transform-pipelines 任务组 2/3/4 + 6.2/6.3 面）。

覆盖（spec 场景对照）：
- 版本语义：改步骤出新版本（append-only，旧版本原样可查）；只改元信息
  不动版本（Requirement 1）；
- 三步链 derive→join→link：按序执行、参数形状与既有端点一致、输出命名
  可追溯（Requirement 2/3）；
- 输出槽位：同版本重跑同 id 原地刷新（refresh 不 create）、新版本新槽位、
  旧版本输出存续（Requirement 3）；
- 失败隔离：第二步失败 → run=failed、step 定位、第一步输出保留、第三步
  无 step 行（Requirement 4）；运行历史倒序可展开；保留期裁剪；
- 调度：materialized_artifacts 挂载、到点自动运行 trigger=schedule、暂停
  后不触发（Requirement 5）；
- 统一路：步骤刷新继承质量快照（Requirement 6——钩子经本 change 收拢到
  data_routes.refresh_derived_dataset，见该函数 docstring）；
- 配额硬停：step=quota_exhausted、run=failed、无绕过路径（Requirement 6）；
- 隔离与角色：跨客户 404、viewer 只读 403 写、editor 可写、未鉴权 401
  （Requirement 7）；
- link 诚实降级：旧引擎无 link 工具 → 该步骤如实失败、前序输出保留
  （Requirement 8）。

环境：datasets 内存库（管线五表挂 DatasetsBase）+ materialization 临时库
+ FakeEngine（test_datasets_common——本批扩展 TOOL_LINK 应答）。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.billing.models import Customer
from fd_wire_api.console.models import CustomerMember
from fd_wire_api.datasets import connector_routes
from fd_wire_api.datasets.db import (
    get_session as get_datasets_session,
    reset_datasets_runtime,
)
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    TOOL_DERIVE,
    TOOL_DERIVE_REFRESH,
    TOOL_JOIN_INDICATORS,
    TOOL_LINK,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.materialization.db import (
    get_session as get_mat_session,
    reset_materialization_runtime,
)
from fd_wire_api.materialization.models import MaterializedArtifact
from fd_wire_api.pipelines.models import (
    PipelineRun,
    PipelineRunStep,
    TransformPipelineSlot,
)
from fd_wire_api.quality.models import DatasetQualitySnapshot

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token
from test_datasets_common import (
    CUST_HEADERS,
    INTERNAL_TOKEN,
    FakeEngine,
    make_uploaded_dataset,
)

SUB_EDITOR = "sub-pipe-editor"
SUB_VIEWER = "sub-pipe-viewer"
EDITOR = bearer(mint_id_token(SUB_EDITOR))
VIEWER = bearer(mint_id_token(SUB_VIEWER))

THREE_STEPS = [
    {"name": "清洗", "op": "derive", "params": {"filters": [], "sort": [], "limit": 100}},
    {
        "name": "挂指标",
        "op": "join",
        "params": {
            "indicators": [{"code": "cpi_china", "as": "cpi"}],
            "date_column": "date",
        },
    },
    {"name": "关联客户表", "op": "link", "params": {}},  # right_dataset_id 建链时补
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """datasets 内存库 + materialization 临时库 + 三角色种子 + FakeEngine。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
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
    # 团队成员三角角色：editor/viewer 挂 cust-001（owner=sub-cust-001 本人）
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

    fake = FakeEngine()
    fastapi_app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: fake
    )
    with TestClient(fastapi_app) as client:
        yield SimpleNamespace(client=client, fake=fake, app=fastapi_app)
    fastapi_app.dependency_overrides.clear()
    connector_routes._SYNC_SESSIONS.clear()
    reset_datasets_runtime()
    reset_materialization_runtime()


def make_pipeline(
    env, *, steps=None, base=None, name="销售加工链", customer="cust-001"
) -> dict:
    """建一条管线（默认三步 derive→join→link；link 右集自动补）。"""
    if base is None:
        base = make_uploaded_dataset(env, customer=customer)
    if steps is None:
        right = make_uploaded_dataset(env, customer=customer, name="客户表")
        steps = [dict(s) for s in THREE_STEPS]
        steps[2]["params"] = {
            "right_dataset_id": right["id"],
            "left_column": "id",
            "right_column": "id",
        }
    resp = env.client.post(
        "/api/v1/pipelines",
        headers=CUST_HEADERS[customer],
        json={
            "name": name,
            "description": "测试管线",
            "base_dataset_id": base["id"],
            "steps": steps,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def run(env, pipeline_id: str, *, version: int | None = None, customer="cust-001"):
    body = {} if version is None else {"version": version}
    return env.client.post(
        f"/api/v1/pipelines/{pipeline_id}/run",
        headers=CUST_HEADERS[customer],
        json=body,
    )


def _slots(pipeline_id: str) -> list[TransformPipelineSlot]:
    with get_datasets_session() as db:
        return list(
            db.execute(
                select(TransformPipelineSlot).where(
                    TransformPipelineSlot.pipeline_id == pipeline_id
                )
            ).scalars()
        )


# ── 2.2 版本语义 ─────────────────────────────────────────────────────


class TestVersioning:
    def test_create_v1_list_detail(self, env):
        pipeline = make_pipeline(env)
        assert pipeline["current_version"] == 1
        assert len(pipeline["steps"]) == 3

        listed = env.client.get("/api/v1/pipelines", headers=CUST_HEADERS["cust-001"])
        assert listed.status_code == 200
        item = next(i for i in listed.json()["items"] if i["id"] == pipeline["id"])
        assert item["steps_count"] == 3
        assert item["current_version"] == 1
        assert item["schedule"] is None and item["last_run"] is None
        assert item["base_dataset_name"]  # 名称解析自 datasets 表

        detail = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}", headers=CUST_HEADERS["cust-001"]
        )
        assert detail.status_code == 200
        versions = detail.json()["versions"]
        assert [v["version"] for v in versions] == [1]
        assert [s["op"] for s in versions[0]["steps"]] == ["derive", "join", "link"]

    def test_steps_change_appends_version_v1_intact(self, env):
        pipeline = make_pipeline(env)
        new_steps = [
            {"name": "只清洗", "op": "derive", "params": {"filters": [], "limit": 5}}
        ]
        put = env.client.put(
            f"/api/v1/pipelines/{pipeline['id']}",
            headers=CUST_HEADERS["cust-001"],
            json={"steps": new_steps},
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["version_created"] == 2 and body["current_version"] == 2

        detail = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}", headers=CUST_HEADERS["cust-001"]
        ).json()
        assert [v["version"] for v in detail["versions"]] == [1, 2]
        assert len(detail["versions"][0]["steps"]) == 3  # 旧版本原样可查
        assert detail["versions"][1]["steps"][0]["name"] == "只清洗"

    def test_meta_only_change_keeps_version(self, env):
        pipeline = make_pipeline(env)
        put = env.client.put(
            f"/api/v1/pipelines/{pipeline['id']}",
            headers=CUST_HEADERS["cust-001"],
            json={"description": "仅改描述"},
        )
        assert put.status_code == 200
        body = put.json()
        assert "version_created" not in body and body["current_version"] == 1

    def test_base_dataset_change_appends_version(self, env):
        pipeline = make_pipeline(env)
        other_base = make_uploaded_dataset(env, name="新基表")
        put = env.client.put(
            f"/api/v1/pipelines/{pipeline['id']}",
            headers=CUST_HEADERS["cust-001"],
            json={"base_dataset_id": other_base["id"]},
        )
        assert put.status_code == 200, put.text
        assert put.json()["version_created"] == 2
        detail = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}", headers=CUST_HEADERS["cust-001"]
        ).json()
        assert detail["versions"][1]["base_dataset_id"] == other_base["id"]

    def test_invalid_steps_rejected(self, env):
        base = make_uploaded_dataset(env)
        for steps in (
            [],
            # 「窗口算子」是 v1 明确挂账不做的 op（路线图如实记录）
            [{"name": "x", "op": "window", "params": {}}],
            [{"name": "x", "op": "derive", "params": "不是对象"}],
            [{"op": "derive", "params": {}}],
        ):
            resp = env.client.post(
                "/api/v1/pipelines",
                headers=CUST_HEADERS["cust-001"],
                json={"name": "p", "base_dataset_id": base["id"], "steps": steps},
            )
            assert resp.status_code == 422, (steps, resp.text)

    def test_create_with_foreign_base_dataset_rejected(self, env):
        foreign = make_uploaded_dataset(env, customer="cust-002", name="他企表")
        resp = env.client.post(
            "/api/v1/pipelines",
            headers=CUST_HEADERS["cust-001"],
            json={
                "name": "p",
                "base_dataset_id": foreign["id"],
                "steps": [{"name": "s", "op": "derive", "params": {}}],
            },
        )
        assert resp.status_code == 422


# ── 2.3 隔离与角色 ───────────────────────────────────────────────────


class TestIsolationAndRoles:
    def test_cross_customer_404_everywhere(self, env):
        pipeline = make_pipeline(env)  # cust-001 的管线
        pid = pipeline["id"]
        attempts = [
            ("get", f"/api/v1/pipelines/{pid}", {}),
            ("put", f"/api/v1/pipelines/{pid}", {"json": {"description": "x"}}),
            ("post", f"/api/v1/pipelines/{pid}/run", {"json": {}}),
            ("get", f"/api/v1/pipelines/{pid}/runs", {}),
            ("put", f"/api/v1/pipelines/{pid}/schedule", {"json": {"cron": "* * * * *"}}),
            ("delete", f"/api/v1/pipelines/{pid}/schedule", {}),
            ("post", f"/api/v1/pipelines/{pid}/schedule/pause", {}),
        ]
        for method, path, kwargs in attempts:
            resp = getattr(env.client, method)(
                path, headers=CUST_HEADERS["cust-002"], **kwargs
            )
            assert resp.status_code == 404, (method, path, resp.text)
        listed = env.client.get("/api/v1/pipelines", headers=CUST_HEADERS["cust-002"])
        assert all(i["id"] != pid for i in listed.json()["items"])  # 列表不含他企

    def test_viewer_read_ok_write_403(self, env):
        pipeline = make_pipeline(env)
        pid = pipeline["id"]
        assert env.client.get("/api/v1/pipelines", headers=VIEWER).status_code == 200
        assert (
            env.client.get(f"/api/v1/pipelines/{pid}", headers=VIEWER).status_code == 200
        )
        assert (
            env.client.get(f"/api/v1/pipelines/{pid}/runs", headers=VIEWER).status_code
            == 200
        )
        # 写面全 403（viewer 只读；body 形态合法，证明拦在角色门而非校验）
        assert (
            env.client.post(
                "/api/v1/pipelines",
                headers=VIEWER,
                json={
                    "name": "v",
                    "base_dataset_id": pipeline["base_dataset_id"],
                    "steps": [{"name": "s", "op": "derive", "params": {}}],
                },
            ).status_code
            == 403
        )
        assert (
            env.client.put(
                f"/api/v1/pipelines/{pid}", headers=VIEWER, json={"description": "x"}
            ).status_code
            == 403
        )
        assert (
            env.client.put(
                f"/api/v1/pipelines/{pid}/schedule",
                headers=VIEWER,
                json={"cron": "* * * * *"},
            ).status_code
            == 403
        )
        assert run(env, pid).status_code == 200  # owner 可运行
        env.fake.calls.clear()
        assert (
            env.client.post(f"/api/v1/pipelines/{pid}/run", headers=VIEWER, json={}).status_code
            == 403
        )
        assert env.fake.calls == []  # 拒在门禁层，未触引擎

    def test_editor_can_write(self, env):
        base = make_uploaded_dataset(env)
        created = env.client.post(
            "/api/v1/pipelines",
            headers=EDITOR,
            json={
                "name": "editor 的管线",
                "base_dataset_id": base["id"],
                "steps": [{"name": "s", "op": "derive", "params": {}}],
            },
        )
        assert created.status_code == 201, created.text
        assert run(env, created.json()["id"]).status_code == 200

    def test_unauthenticated_401(self, env):
        pipeline = make_pipeline(env)
        pid = pipeline["id"]
        attempts = [
            ("get", "/api/v1/pipelines", {}),
            ("get", f"/api/v1/pipelines/{pid}", {}),
            ("post", f"/api/v1/pipelines/{pid}/run", {"json": {}}),
            ("get", f"/api/v1/pipelines/{pid}/runs", {}),
            ("put", f"/api/v1/pipelines/{pid}/schedule", {"json": {"cron": "* * * * *"}}),
        ]
        for method, path, kwargs in attempts:
            resp = getattr(env.client, method)(path, **kwargs)
            assert resp.status_code == 401, (method, path, resp.text)


# ── 3.1/3.2 三步链与输出槽位 ─────────────────────────────────────────


class TestExecution:
    def test_three_step_happy_path(self, env):
        base = make_uploaded_dataset(env, name="销售流水")
        right = make_uploaded_dataset(env, name="客户表")
        steps = [dict(s) for s in THREE_STEPS]
        steps[2]["params"] = {
            "right_dataset_id": right["id"],
            "left_column": "id",
            "right_column": "id",
        }
        pipeline = make_pipeline(env, base=base, steps=steps)
        resp = run(env, pipeline["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["run"]["status"] == "success"
        assert body["run"]["finished_at"] is not None
        assert [s["status"] for s in body["steps"]] == ["success"] * 3
        assert [s["op"] for s in body["steps"]] == ["derive", "join", "link"]
        out_ids = [s["output_dataset_id"] for s in body["steps"]]
        assert all(out_ids)

        # 引擎调用链（线性：每步以前一步输出为输入）
        derive_args = env.fake.calls_for(TOOL_DERIVE)[0]
        join_args = env.fake.calls_for(TOOL_JOIN_INDICATORS)[0]
        link_args = env.fake.calls_for(TOOL_LINK)[0]
        assert derive_args["parent_key"] == base["key"]
        assert join_args["parent_key"] == derive_args["child_key"]
        assert link_args["left_key"] == join_args["child_key"]
        assert link_args["right_key"] == right["key"]
        # join 参数形状与既有端点一致（指标别名 as 同形透传）
        assert join_args["indicators"][0]["as"] == "cpi"
        assert join_args["date_column"] == "date"

        # 输出命名可追溯：<pipeline>.v<version>.<seq>.<step-name>
        with get_datasets_session() as db:
            names = [db.get(Dataset, ds_id).name for ds_id in out_ids]
        assert names == [
            "销售加工链.v1.1.清洗",
            "销售加工链.v1.2.挂指标",
            "销售加工链.v1.3.关联客户表",
        ]

        # 槽位绑定（管线, 版本, 序号 → 输出）
        slots = sorted(_slots(pipeline["id"]), key=lambda s: s.seq)
        assert [s.dataset_id for s in slots] == out_ids
        assert [s.version for s in slots] == [1, 1, 1]

    def test_same_version_rerun_refreshes_in_place(self, env):
        pipeline = make_pipeline(env)
        first = run(env, pipeline["id"]).json()
        out_ids = [s["output_dataset_id"] for s in first["steps"]]

        # 行数以引擎为准刷新：overview 行数上调后重跑应体现在 step 行
        env.fake.overview_result["rows"] = 7
        second = run(env, pipeline["id"]).json()
        assert second["run"]["status"] == "success"
        assert [s["output_dataset_id"] for s in second["steps"]] == out_ids
        assert all(s["row_count"] == 7 for s in second["steps"])

        # 创建只发生一次；重跑走统一刷新路（derive_refresh × 3 步）
        assert len(env.fake.calls_for(TOOL_DERIVE)) == 1
        assert len(env.fake.calls_for(TOOL_JOIN_INDICATORS)) == 1
        assert len(env.fake.calls_for(TOOL_LINK)) == 1
        assert len(env.fake.calls_for(TOOL_DERIVE_REFRESH)) == 3
        assert len(_slots(pipeline["id"])) == 3  # 槽位不增

    def test_new_version_new_slots_old_outputs_intact(self, env):
        pipeline = make_pipeline(env)
        first = run(env, pipeline["id"]).json()
        v1_out = first["steps"][0]["output_dataset_id"]

        put = env.client.put(
            f"/api/v1/pipelines/{pipeline['id']}",
            headers=CUST_HEADERS["cust-001"],
            json={"steps": [{"name": "清洗2", "op": "derive", "params": {"limit": 9}}]},
        )
        assert put.status_code == 200 and put.json()["version_created"] == 2
        second = run(env, pipeline["id"]).json()
        assert second["run"]["version"] == 2
        v2_out = second["steps"][0]["output_dataset_id"]
        assert v2_out != v1_out  # 新版本新槽位

        slots = _slots(pipeline["id"])
        assert {(s.version, s.seq) for s in slots} == {(1, 1), (1, 2), (1, 3), (2, 1)}
        with get_datasets_session() as db:
            assert db.get(Dataset, v1_out) is not None  # 旧版本输出存续

    def test_run_specific_old_version(self, env):
        pipeline = make_pipeline(env)
        run(env, pipeline["id"])  # v1 建槽
        env.client.put(
            f"/api/v1/pipelines/{pipeline['id']}",
            headers=CUST_HEADERS["cust-001"],
            json={"steps": [{"name": "清洗2", "op": "derive", "params": {}}]},
        )
        run(env, pipeline["id"])  # v2 建槽
        resp = run(env, pipeline["id"], version=1)
        assert resp.status_code == 200
        assert resp.json()["run"]["version"] == 1
        assert len(env.fake.calls_for(TOOL_DERIVE)) == 2  # v1/v2 首跑各一次创建
        assert len([s for s in _slots(pipeline["id"]) if s.version == 1]) == 3

    def test_run_unknown_version_404(self, env):
        pipeline = make_pipeline(env)
        assert run(env, pipeline["id"], version=9).status_code == 404

    def test_quality_snapshot_inherited_on_step_refresh(self, env):
        """统一刷新路的质量快照（Requirement 6 场景）：重跑后步骤输出获得
        triggered_by=materialize 快照——与 runner 派生重派生同源钩子。"""
        pipeline = make_pipeline(env)
        first = run(env, pipeline["id"]).json()
        out_ids = [s["output_dataset_id"] for s in first["steps"]]
        second = run(env, pipeline["id"]).json()
        assert second["run"]["status"] == "success"
        with get_datasets_session() as db:
            snaps = list(
                db.execute(
                    select(DatasetQualitySnapshot).where(
                        DatasetQualitySnapshot.dataset_id.in_(out_ids),
                        DatasetQualitySnapshot.triggered_by == "materialize",
                    )
                ).scalars()
            )
        assert snaps, "重跑经统一刷新路应触发质量快照"
        assert {s.dataset_id for s in snaps} == set(out_ids)


# ── 3.3/3.4 失败隔离、留痕、配额与保留 ───────────────────────────────


def _two_step_pipeline(env) -> dict:
    base = make_uploaded_dataset(env)
    right = make_uploaded_dataset(env, name="客户表")
    steps = [
        {"name": "清洗", "op": "derive", "params": {"filters": []}},
        {
            "name": "关联",
            "op": "link",
            "params": {
                "right_dataset_id": right["id"],
                "left_column": "id",
                "right_column": "id",
            },
        },
    ]
    return make_pipeline(env, base=base, steps=steps, name="两步链")


class TestFailuresAndHistory:
    def test_step2_fails_isolation(self, env):
        pipeline = _two_step_pipeline(env)
        env.fake.error_on[TOOL_LINK] = "右键列不存在: id"
        resp = run(env, pipeline["id"])
        assert resp.status_code == 200  # 运行完成（状态如实 failed）
        body = resp.json()
        assert body["run"]["status"] == "failed"
        assert "步骤 2" in body["run"]["error_text"]
        steps = body["steps"]
        assert steps[0]["status"] == "success"
        assert steps[1]["status"] == "failed" and steps[1]["error_text"]
        assert steps[1]["output_dataset_id"] is None
        # 第一步输出保留（数据集在库、槽位绑定）
        with get_datasets_session() as db:
            assert db.get(Dataset, steps[0]["output_dataset_id"]) is not None
        assert [s.seq for s in _slots(pipeline["id"])] == [1]

    def test_three_step_second_fails_no_step3_row(self, env):
        pipeline = make_pipeline(env, name="三步失败链")
        env.fake.fail_on[TOOL_JOIN_INDICATORS] = EngineCallError("join 引擎爆炸")
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "failed"
        assert [s["seq"] for s in body["steps"]] == [1, 2]  # 第三步未执行无记录
        assert body["steps"][0]["status"] == "success"  # 前序输出保留
        assert body["steps"][1]["status"] == "failed"

    def test_runs_history_desc_and_expandable(self, env):
        pipeline = _two_step_pipeline(env)
        first = run(env, pipeline["id"]).json()
        second = run(env, pipeline["id"]).json()
        listed = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}/runs",
            headers=CUST_HEADERS["cust-001"],
        ).json()
        assert listed["total"] == 2
        assert [r["id"] for r in listed["items"]] == [
            second["run"]["id"],
            first["run"]["id"],
        ]  # 时间倒序
        detail = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}/runs/{first['run']['id']}",
            headers=CUST_HEADERS["cust-001"],
        )
        assert detail.status_code == 200
        assert [s["seq"] for s in detail.json()["steps"]] == [1, 2]

    def test_run_detail_cross_customer_404(self, env):
        pipeline = _two_step_pipeline(env)
        run_id = run(env, pipeline["id"]).json()["run"]["id"]
        resp = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}/runs/{run_id}",
            headers=CUST_HEADERS["cust-002"],
        )
        assert resp.status_code == 404

    def test_quota_hardstop_records_quota_exhausted(self, env, monkeypatch):
        """配额硬停中途：step=quota_exhausted、run=failed、无绕过路径。"""
        from fd_wire_api.datasets import service as ds_service

        pipeline = _two_step_pipeline(env)
        original = ds_service.ensure_new_dataset_room
        calls = {"n": 0}

        def quota_after_first(db, *, customer_id):
            calls["n"] += 1
            if calls["n"] > 1:
                raise ds_service.QuotaExceeded("存储用量已达配额（测试注入）")
            return original(db, customer_id=customer_id)

        # data_routes 与本切片都经模块属性引用——打模块属性即拦两条创建路
        monkeypatch.setattr(ds_service, "ensure_new_dataset_room", quota_after_first)
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "failed"
        assert body["steps"][0]["status"] == "success"
        assert body["steps"][1]["status"] == "quota_exhausted"
        assert "配额" in body["steps"][1]["error_text"]
        # 无绕过：第二步在引擎调用前被拒（不产生绕过配额的替代路径）
        assert len(env.fake.calls_for(TOOL_LINK)) == 0

    def test_retention_keeps_latest_50_runs(self, env):
        base = make_uploaded_dataset(env)
        pipeline = make_pipeline(
            env,
            base=base,
            steps=[{"name": "清洗", "op": "derive", "params": {}}],
        )
        for _ in range(52):
            assert run(env, pipeline["id"]).status_code == 200
        listed = env.client.get(
            f"/api/v1/pipelines/{pipeline['id']}/runs?page_size=100",
            headers=CUST_HEADERS["cust-001"],
        ).json()
        assert listed["total"] == 50
        with get_datasets_session() as db:
            steps = list(
                db.execute(
                    select(PipelineRunStep).where(
                        PipelineRunStep.run_id.in_([r["id"] for r in listed["items"]])
                    )
                ).scalars()
            )
        assert len(steps) == 50  # step 随 run 同生共死（每 run 1 step × 50）


# ── 3.6 link 诚实降级 ────────────────────────────────────────────────


class TestLinkHonestFailure:
    def test_engine_without_link_tool_fails_honestly(self, env):
        base = make_uploaded_dataset(env)
        right = make_uploaded_dataset(env, name="客户表")
        pipeline = make_pipeline(
            env,
            base=base,
            steps=[
                {"name": "清洗", "op": "derive", "params": {}},
                {
                    "name": "关联",
                    "op": "link",
                    "params": {
                        "right_dataset_id": right["id"],
                        "left_column": "id",
                        "right_column": "id",
                    },
                },
            ],
        )
        env.fake.fail_on[TOOL_LINK] = EngineCallError(
            "unknown tool: customer_dataset_link"
        )
        body = run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "failed"
        failed = body["steps"][1]
        assert failed["status"] == "failed"
        # 错误明示引擎不支持（不静默跳过、不伪造输出）
        assert "customer_dataset_link" in failed["error_text"]
        assert "引擎" in failed["error_text"]
        assert failed["output_dataset_id"] is None
        # 前序输出保留
        with get_datasets_session() as db:
            assert db.get(Dataset, body["steps"][0]["output_dataset_id"]) is not None


# ── 4.1/4.2 管线级调度 ───────────────────────────────────────────────


class TestScheduling:
    def test_put_get_delete_schedule(self, env):
        pipeline = make_pipeline(env)
        pid = pipeline["id"]
        put = env.client.put(
            f"/api/v1/pipelines/{pid}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "0 9 * * *"},
        )
        assert put.status_code == 200, put.text
        assert put.json() == {
            "pipeline_id": pid,
            "cron": "0 9 * * *",
            "enabled": True,
            "last_run_at": None,
            "last_status": None,
        }
        # materialized_artifacts 挂载形态（D3：kind/ref_key 关联，artifact_id 置空）
        mat = get_mat_session()
        try:
            row = mat.execute(
                select(MaterializedArtifact).where(
                    MaterializedArtifact.kind == "transform_pipeline",
                    MaterializedArtifact.ref_key == pid,
                )
            ).scalar_one()
            assert row.artifact_id is None
            assert row.spec["pipeline_id"] == pid
            assert row.customer_id == "cust-001"
        finally:
            mat.close()

        got = env.client.get(
            f"/api/v1/pipelines/{pid}/schedule", headers=CUST_HEADERS["cust-001"]
        )
        assert got.json()["cron"] == "0 9 * * *" and got.json()["enabled"] is True

        bad = env.client.put(  # 非法 cron 400（复用既有 5 字段解析）
            f"/api/v1/pipelines/{pid}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "JAN * *"},
        )
        assert bad.status_code == 400

        deleted = env.client.delete(
            f"/api/v1/pipelines/{pid}/schedule", headers=CUST_HEADERS["cust-001"]
        )
        assert deleted.status_code == 200
        after = env.client.get(
            f"/api/v1/pipelines/{pid}/schedule", headers=CUST_HEADERS["cust-001"]
        )
        assert after.json()["cron"] is None and after.json()["enabled"] is False

    def test_scheduler_dispatch_runs_pipeline_trigger_schedule(self, env):
        from fd_wire_api.materialization import runner as runner_mod
        from fd_wire_api.materialization.scheduler import default_runner_factory, should_run

        pipeline = make_pipeline(env)
        pid = pipeline["id"]
        assert env.client.put(
            f"/api/v1/pipelines/{pid}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "0 9 * * *"},
        ).status_code == 200

        mat = get_mat_session()
        try:
            row = mat.execute(
                select(MaterializedArtifact).where(
                    MaterializedArtifact.kind == "transform_pipeline",
                    MaterializedArtifact.ref_key == pid,
                )
            ).scalar_one()
            row.schedule = "* * * * *"  # 每分钟命中（测试直改调度）
            mat.commit()
            mat.refresh(row)
            moment = datetime.now(timezone.utc).replace(
                tzinfo=None, second=0, microsecond=0
            )
            assert should_run(row, moment) is True

            runner_mod._engine_factory_override = lambda cid: env.fake
            try:
                runner = default_runner_factory()
                try:
                    result = runner.run_artifact_row(row, now=moment)
                finally:
                    runner.close()
                    runner.release_db()
            finally:
                runner_mod._engine_factory_override = None
            assert result.status == "success", result.error
            mat.refresh(row)
            assert row.last_status == "success"
        finally:
            mat.close()

        with get_datasets_session() as db:
            runs = list(
                db.execute(
                    select(PipelineRun).where(PipelineRun.pipeline_id == pid)
                ).scalars()
            )
        assert len(runs) == 1
        assert runs[0].trigger == "schedule"  # 触发方式如实标注
        assert runs[0].status == "success"

    def test_paused_schedule_not_triggered(self, env):
        from fd_wire_api.materialization.scheduler import should_run, tick_once

        pipeline = make_pipeline(env)
        pid = pipeline["id"]
        assert env.client.put(
            f"/api/v1/pipelines/{pid}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "* * * * *"},
        ).status_code == 200
        paused = env.client.post(
            f"/api/v1/pipelines/{pid}/schedule/pause", headers=CUST_HEADERS["cust-001"]
        )
        assert paused.status_code == 200 and paused.json()["enabled"] is False

        mat = get_mat_session()
        try:
            row = mat.execute(
                select(MaterializedArtifact).where(
                    MaterializedArtifact.kind == "transform_pipeline",
                    MaterializedArtifact.ref_key == pid,
                )
            ).scalar_one()
            moment = datetime.now(timezone.utc).replace(
                tzinfo=None, second=0, microsecond=0
            )
            assert should_run(row, moment) is False  # 暂停后到点不触发
            tick_once(now=moment)
        finally:
            mat.close()
        with get_datasets_session() as db:
            runs = list(
                db.execute(
                    select(PipelineRun).where(PipelineRun.pipeline_id == pid)
                ).scalars()
            )
        assert runs == []  # 未产生运行
        assert run(env, pid).status_code == 200  # 手动运行不受影响

    def test_pause_without_schedule_404_then_resume(self, env):
        pipeline = make_pipeline(env)
        pid = pipeline["id"]
        resp = env.client.post(
            f"/api/v1/pipelines/{pid}/schedule/pause", headers=CUST_HEADERS["cust-001"]
        )
        assert resp.status_code == 404
        assert env.client.put(
            f"/api/v1/pipelines/{pid}/schedule",
            headers=CUST_HEADERS["cust-001"],
            json={"cron": "0 9 * * *"},
        ).status_code == 200
        assert (
            env.client.post(
                f"/api/v1/pipelines/{pid}/schedule/pause",
                headers=CUST_HEADERS["cust-001"],
            ).json()["enabled"]
            is False
        )
        assert (
            env.client.post(
                f"/api/v1/pipelines/{pid}/schedule/resume",
                headers=CUST_HEADERS["cust-001"],
            ).json()["enabled"]
            is True
        )
