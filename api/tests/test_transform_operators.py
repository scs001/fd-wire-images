"""确定性算子 + SQL 步骤（wire-transform-operators 任务组 2）。

api 侧契约（四算子 + SQL 物化 + SQL 试运行）：

- 端点形状与引擎工具入参同形（spec「参数形状一致」）；
- 统一代码路：配额闸 → allocate_key → 引擎工具 → 派生登记（父边）→
  列体量回填——一步都不许绕；
- 旧 cell 引擎（工具不存在）→ 502 且文案明示「引擎暂不支持」，不伪造；
- SQL 试运行**零副作用**（不登记数据集、不落任何 dataset 行）；
- union / sql 的多父集都登记父边（敏感度闭包与派生关系不漏侧）；
- 管线八分支：新 op 创建 → 运行 → 槽位原地刷新（不新建）→ 血缘可重放。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest

# 顺序要紧：先装 app（它把各域路由装齐），再取 datasets.ingest 的常量——
# 反过来会撞上 ingest ↔ data_plane 的循环导入（既有测试同款约定）。
from fd_wire_api.app import app as fastapi_app  # noqa: F401
from fd_wire_api.datasets.ingest import (
    TOOL_AGGREGATE_DERIVE,
    TOOL_FILTER,
    TOOL_PROJECT,
    TOOL_SQL,
    TOOL_SQL_PREVIEW,
    TOOL_UNION,
    EngineCallError,
)
from test_datasets_common import (
    CUST_HEADERS,
    datasets_env,  # noqa: F401 - pytest fixture
    make_uploaded_dataset,
)

AUTH = CUST_HEADERS["cust-001"]


def _cols(env, dataset_id: str) -> list[dict]:
    return env.client.get(f"/api/v1/datasets/{dataset_id}", headers=AUTH).json()[
        "columns"
    ]


def _parent_refs(env, dataset_id: str) -> list[tuple[str, str]]:
    """子集登记的父边（父数据集 id + 来源）——直接读登记表断言。"""
    from fd_wire_api.assets.models import DatasetParentRef
    from fd_wire_api.datasets.db import get_session

    session = get_session()
    try:
        rows = (
            session.query(DatasetParentRef)
            .filter(DatasetParentRef.child_dataset_id == dataset_id)
            .all()
        )
        return sorted((r.parent_dataset_id, r.origin) for r in rows)
    finally:
        session.close()


# ---------------------------------------------------------------- filter


def test_filter_creates_derived_child(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/filter",
        headers=AUTH,
        json={
            "name": "已完成订单",
            "filters": [{"column": "status", "op": "eq", "value": "done"}],
            "sort": [{"column": "id", "dir": "desc"}],
            "limit": 10,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_kind"] == "derived"
    assert body["name"] == "已完成订单"
    # 引擎入参形状 = 工具入参（父 key + 同一套 filters/sort/limit）
    args = env.fake.calls_for(TOOL_FILTER)[0]
    assert args["parent_key"] == base["key"]
    assert args["filters"] == [{"column": "status", "op": "eq", "value": "done"}]
    assert args["limit"] == 10
    assert args["child_key"] and args["actor"] and args["channel"]


def test_filter_bad_operator_is_engine_rejection(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.error_on[TOOL_FILTER] = "非法过滤操作符: 'regex'"
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/filter",
        headers=AUTH,
        json={"name": "x", "filters": [{"column": "a", "op": "regex", "value": "1"}]},
    )
    assert resp.status_code == 400
    assert "引擎拒绝" in resp.text


# ---------------------------------------------------------------- project


def test_project_passes_columns_with_alias(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/project",
        headers=AUTH,
        json={
            "name": "投影",
            "columns": [{"source": "region", "as": "区域"}, {"source": "amount"}],
        },
    )
    assert resp.status_code == 201, resp.text
    args = env.fake.calls_for(TOOL_PROJECT)[0]
    assert args["columns"] == [{"source": "region", "as": "区域"}, {"source": "amount"}]
    assert args["parent_key"] == base["key"]


def test_project_requires_columns(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/project",
        headers=AUTH,
        json={"name": "x", "columns": []},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- aggregate


def test_aggregate_derive_uses_materializing_tool(datasets_env) -> None:
    """物化聚合走 aggregate_derive，**不**是只读的 customer_dataset_aggregate。"""
    from fd_wire_api.datasets.ingest import TOOL_AGGREGATE

    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/aggregate/derive",
        headers=AUTH,
        json={
            "name": "按地区汇总",
            "group_by": ["region"],
            "measures": [{"func": "count"}, {"func": "sum", "column": "amount"}],
        },
    )
    assert resp.status_code == 201, resp.text
    assert env.fake.calls_for(TOOL_AGGREGATE_DERIVE), "应调用物化聚合工具"
    assert not env.fake.calls_for(TOOL_AGGREGATE), "不得误用只读聚合工具"
    args = env.fake.calls_for(TOOL_AGGREGATE_DERIVE)[0]
    assert args["group_by"] == ["region"]
    assert args["measures"][0] == {"func": "count"}


def test_aggregate_measure_cap(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/aggregate/derive",
        headers=AUTH,
        json={
            "name": "x",
            "group_by": ["region"],
            "measures": [{"func": "count"}] * 21,
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- union


def test_union_registers_both_parents(datasets_env) -> None:
    env = datasets_env
    left = make_uploaded_dataset(env, name="左表")
    right = make_uploaded_dataset(env, name="右表")
    resp = env.client.post(
        f"/api/v1/datasets/{left['id']}/union",
        headers=AUTH,
        json={"name": "合并", "right_dataset_id": right["id"]},
    )
    assert resp.status_code == 201, resp.text
    child = resp.json()
    args = env.fake.calls_for(TOOL_UNION)[0]
    assert args["left_key"] == left["key"] and args["right_key"] == right["key"]
    assert args["on"] == "by_name"
    # 两个父集都进血缘（否则右集的敏感档会在合并结果上消失）
    refs = _parent_refs(env, child["id"])
    assert (left["id"], "union") in refs
    assert (right["id"], "union") in refs


def test_union_foreign_right_dataset_404(datasets_env) -> None:
    env = datasets_env
    left = make_uploaded_dataset(env)
    foreign = make_uploaded_dataset(env, customer="cust-002", name="他企表")
    resp = env.client.post(
        f"/api/v1/datasets/{left['id']}/union",
        headers=AUTH,
        json={"name": "x", "right_dataset_id": foreign["id"]},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------- sql


def test_sql_step_materializes_with_placeholders(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/sql",
        headers=AUTH,
        json={
            "name": "SQL 汇总",
            "sql": "SELECT region, count(*) AS n FROM {orders} GROUP BY region",
            "tables": [{"name": "orders", "dataset_key": base["key"]}],
            "params": {},
        },
    )
    assert resp.status_code == 201, resp.text
    args = env.fake.calls_for(TOOL_SQL)[0]
    assert "{orders}" in args["sql"]
    assert args["tables"] == [{"name": "orders", "dataset_key": base["key"]}]


def test_sql_preview_has_no_side_effect(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    before = env.client.get("/api/v1/datasets", headers=AUTH).json()
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/sql/preview",
        headers=AUTH,
        json={
            "sql": "SELECT * FROM {orders}",
            "tables": [{"name": "orders", "dataset_key": base["key"]}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["preview"] is True
    assert body["limit"] == 100
    assert body["columns"] == [
        {"name": "region", "type": "TEXT"},
        {"name": "n", "type": "INTEGER"},
    ]
    # 零副作用：数据集总数不变
    after = env.client.get("/api/v1/datasets", headers=AUTH).json()
    assert len(after) == len(before)


def test_sql_preview_limit_ceiling(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/sql/preview",
        headers=AUTH,
        json={
            "sql": "SELECT * FROM {orders}",
            "tables": [{"name": "orders", "dataset_key": base["key"]}],
            "limit": 101,
        },
    )
    assert resp.status_code == 422


def test_sql_undeclared_dataset_key_404(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/sql",
        headers=AUTH,
        json={
            "name": "x",
            "sql": "SELECT * FROM {a} JOIN {b} ON 1=1",
            "tables": [
                {"name": "a", "dataset_key": base["key"]},
                {"name": "b", "dataset_key": "not_mine"},
            ],
        },
    )
    assert resp.status_code == 404


# ------------------------------------------------------- 旧引擎如实降级


@pytest.mark.parametrize(
    "op,path,payload,tool",
    [
        ("filter", "filter", {"name": "x"}, TOOL_FILTER),
        (
            "project",
            "project",
            {"name": "x", "columns": [{"source": "id"}]},
            TOOL_PROJECT,
        ),
        (
            "aggregate",
            "aggregate/derive",
            {"name": "x", "group_by": ["region"], "measures": [{"func": "count"}]},
            TOOL_AGGREGATE_DERIVE,
        ),
    ],
)
def test_old_engine_reports_unsupported(datasets_env, op, path, payload, tool) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.fail_on[tool] = EngineCallError("Unknown tool: customer_dataset_x")
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/{path}", headers=AUTH, json=payload
    )
    assert resp.status_code == 502, resp.text
    assert "引擎" in resp.text and "不支持" in resp.text


def test_old_engine_sql_preview_unsupported(datasets_env) -> None:
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.fail_on[TOOL_SQL_PREVIEW] = EngineCallError("tool not found")
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/sql/preview",
        headers=AUTH,
        json={
            "sql": "SELECT * FROM {o}",
            "tables": [{"name": "o", "dataset_key": base["key"]}],
        },
    )
    assert resp.status_code == 502
    assert "不支持" in resp.text


# ------------------------------------------------------- 配额闸（统一代码路）


def test_operator_respects_dataset_room(datasets_env, monkeypatch) -> None:
    """新算子必须走配额闸（不得旁路——「无第二条物化路径」）。"""
    env = datasets_env
    base = make_uploaded_dataset(env)

    from fd_wire_api.datasets import service as ds_service

    def _boom(*args, **kwargs):
        raise ds_service.QuotaExceeded("数据集个数已达上限")

    monkeypatch.setattr(ds_service, "ensure_new_dataset_room", _boom)
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/filter",
        headers=AUTH,
        json={"name": "x", "filters": []},
    )
    assert resp.status_code == 429, resp.text
    assert not env.fake.calls_for(TOOL_FILTER), "配额硬停时不得调引擎"
