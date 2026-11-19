"""管线八分支（wire-transform-operators 任务组 2.1–2.3）。

新 op 与既有三原语在同一张线性链里按序执行，槽位语义与版本链不变：

- 四算子 + SQL 步骤创建 → 运行 → 同版本重跑**原地刷新**（不新建槽位）；
- 参数形状与既有端点一致（同一份 ``*In`` 模型，不发明第二套语义）；
- 旧 cell 引擎（工具不存在）→ 该步骤如实 failed、前序输出保留、剩余步骤
  不执行——失败隔离沿既有语义；
- 存量 derive/join/link 三步链行为逐字不变（零回归）。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from fd_wire_api.app import app as fastapi_app  # noqa: F401
from fd_wire_api.datasets.ingest import (
    TOOL_AGGREGATE_DERIVE,
    TOOL_DERIVE,
    TOOL_DERIVE_REFRESH,
    TOOL_FILTER,
    TOOL_LINK,
    TOOL_PROJECT,
    TOOL_SQL,
    TOOL_UNION,
    EngineCallError,
)
from test_datasets_common import (
    CUST_HEADERS,
    datasets_env,  # noqa: F401 - pytest fixture
    make_uploaded_dataset,
)

AUTH = CUST_HEADERS["cust-001"]


def _make_pipeline(env, steps, *, base=None, name="算子链") -> dict:
    if base is None:
        base = make_uploaded_dataset(env)
    resp = env.client.post(
        "/api/v1/pipelines",
        headers=AUTH,
        json={
            "name": name,
            "description": "算子链测试",
            "base_dataset_id": base["id"],
            "steps": steps,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run(env, pipeline_id: str):
    return env.client.post(
        f"/api/v1/pipelines/{pipeline_id}/run", headers=AUTH, json={}
    )


# --------------------------------------------------- 四算子 + SQL 一步链


def test_operator_chain_runs_in_order(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    right = make_uploaded_dataset(env, name="右表")
    pipeline = _make_pipeline(
        env,
        [
            {
                "name": "过滤",
                "op": "filter",
                "params": {
                    "filters": [{"column": "id", "op": "gte", "value": 2}],
                    "limit": 10,
                },
            },
            {
                "name": "投影",
                "op": "project",
                "params": {"columns": [{"source": "id", "as": "编号"}]},
            },
            {
                "name": "汇总",
                "op": "aggregate",
                "params": {
                    "group_by": ["编号"],
                    "measures": [{"func": "count"}],
                },
            },
            {
                "name": "合并",
                "op": "union",
                "params": {"right_dataset_id": right["id"]},
            },
            {
                "name": "SQL",
                "op": "sql",
                "params": {
                    "sql": "SELECT * FROM {t}",
                    "tables": [{"name": "t", "dataset_key": base["key"]}],
                },
            },
        ],
        base=base,
    )
    resp = _run(env, pipeline["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run"]["status"] == "success", body
    assert [s["status"] for s in body["steps"]] == ["success"] * 5
    assert [s["op"] for s in body["steps"]] == [
        "filter",
        "project",
        "aggregate",
        "union",
        "sql",
    ]
    # 线性：每步以前一步输出为输入
    filter_args = env.fake.calls_for(TOOL_FILTER)[0]
    project_args = env.fake.calls_for(TOOL_PROJECT)[0]
    aggregate_args = env.fake.calls_for(TOOL_AGGREGATE_DERIVE)[0]
    union_args = env.fake.calls_for(TOOL_UNION)[0]
    sql_args = env.fake.calls_for(TOOL_SQL)[0]
    assert project_args["parent_key"] == filter_args["child_key"]
    assert aggregate_args["parent_key"] == project_args["child_key"]
    assert union_args["left_key"] == aggregate_args["child_key"]
    assert sql_args["tables"][0]["dataset_key"] == base["key"]
    # 槽位五个、序号连续
    out_ids = [s["output_dataset_id"] for s in body["steps"]]
    assert len(set(out_ids)) == 5 and all(out_ids)


def test_operator_rerun_refreshes_in_place(datasets_env) -> None:
    env = datasets_env
    pipeline = _make_pipeline(
        env,
        [
            {
                "name": "过滤",
                "op": "filter",
                "params": {"filters": [{"column": "id", "op": "gte", "value": 1}]},
            },
            {
                "name": "投影",
                "op": "project",
                "params": {"columns": [{"source": "id"}]},
            },
        ],
    )
    first = _run(env, pipeline["id"]).json()
    out_ids = [s["output_dataset_id"] for s in first["steps"]]

    second = _run(env, pipeline["id"]).json()
    assert second["run"]["status"] == "success"
    assert [s["output_dataset_id"] for s in second["steps"]] == out_ids
    # 创建只发生一次；重跑走统一刷新路（derive_refresh × 2）
    assert len(env.fake.calls_for(TOOL_FILTER)) == 1
    assert len(env.fake.calls_for(TOOL_PROJECT)) == 1
    assert len(env.fake.calls_for(TOOL_DERIVE_REFRESH)) == 2


# --------------------------------------------------- 旧引擎降级与失败隔离


def test_old_engine_fails_that_step_only(datasets_env) -> None:
    """旧引擎缺 aggregate 工具 → 该步 failed，前序输出保留，第三步无 step 行。"""
    env = datasets_env
    pipeline = _make_pipeline(
        env,
        [
            {
                "name": "过滤",
                "op": "filter",
                "params": {"filters": [{"column": "id", "op": "gte", "value": 1}]},
            },
            {
                "name": "汇总",
                "op": "aggregate",
                "params": {
                    "group_by": ["id"],
                    "measures": [{"func": "count"}],
                },
            },
            {
                "name": "投影",
                "op": "project",
                "params": {"columns": [{"source": "id"}]},
            },
        ],
    )
    env.fake.fail_on[TOOL_AGGREGATE_DERIVE] = EngineCallError(
        "Unknown tool: customer_dataset_aggregate_derive"
    )
    resp = _run(env, pipeline["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run"]["status"] == "failed"
    assert [s["status"] for s in body["steps"]] == ["success", "failed"]
    # 前序输出保留
    assert body["steps"][0]["output_dataset_id"]
    # 第三步未执行（无 step 行）
    assert len(body["steps"]) == 2
    assert "引擎" in body["steps"][1]["error_text"]


def test_engine_rejection_marks_step_failed(datasets_env) -> None:
    """引擎域错误（如投影源列不存在）→ 该步 failed，错误文案如实带引擎原文。"""
    env = datasets_env
    pipeline = _make_pipeline(
        env,
        [
            {
                "name": "投影",
                "op": "project",
                "params": {"columns": [{"source": "nope"}]},
            }
        ],
    )
    env.fake.error_on[TOOL_PROJECT] = "投影源列不存在: 'nope'"
    body = _run(env, pipeline["id"]).json()
    assert body["run"]["status"] == "failed"
    assert "投影源列不存在" in body["steps"][0]["error_text"]


# --------------------------------------------------- 存量链零回归


def test_legacy_three_step_chain_unaffected(datasets_env) -> None:
    """derive→join→link 三步链行为不变（新 op 只是多出可选分支）。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    right = make_uploaded_dataset(env, name="客户表")
    pipeline = _make_pipeline(
        env,
        [
            {"name": "清洗", "op": "derive", "params": {"limit": 5}},
            {
                "name": "挂指标",
                "op": "join",
                "params": {
                    "indicators": [{"code": "cpi", "as": "cpi"}],
                    "date_column": "date",
                },
            },
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
        base=base,
    )
    body = _run(env, pipeline["id"]).json()
    assert body["run"]["status"] == "success", body
    assert [s["op"] for s in body["steps"]] == ["derive", "join", "link"]
    derive_args = env.fake.calls_for(TOOL_DERIVE)[0]
    assert derive_args["parent_key"] == base["key"]


def test_link_accepts_both_key_spellings(datasets_env) -> None:
    """link 连接键两套键名都收（控制台口径 left_keys / 实体口径 left_column）。

    此前只认 left_column：控制台编辑器发的 left_keys 步骤跑到执行时才会报
    「参数须含连接键列」——两处口径裂缝，此用例把它锁住。
    """
    env = datasets_env
    base = make_uploaded_dataset(env)
    right = make_uploaded_dataset(env, name="客户表")
    for keys in (
        {"left_keys": ["id"], "right_keys": ["id"]},
        {"left_column": "id", "right_column": "id"},
    ):
        pipeline = _make_pipeline(
            env,
            [
                {
                    "name": "关联",
                    "op": "link",
                    "params": {
                        "right_dataset_id": right["id"],
                        **keys,
                    },
                }
            ],
            base=base,
            name="关联链",
        )
        body = _run(env, pipeline["id"]).json()
        assert body["run"]["status"] == "success", body
        link_args = env.fake.calls_for(TOOL_LINK)[-1]
        assert link_args["left_column"] == ["id"]
        assert link_args["right_column"] == ["id"]
        assert link_args["right_key"] == right["key"]
