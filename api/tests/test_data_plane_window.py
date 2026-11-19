"""窗口算子 api 腿（wire-window-operator 任务 2.1–2.4）。

api 侧契约（design D5、spec「确定性算子工具契约」）：

- 数据面端点 ``POST /api/v1/datasets/{id}/window``：参数形状即引擎工具
  入参（``partition_by`` / ``order_by`` / ``window``），形状与四算子端点
  同族；design D5 写的 ``/api/v1/data/datasets/{id}/window`` 是同一函数的
  路径别名。
- 统一代码路（配额闸 → allocate_key → 引擎工具 → 派生登记 → 列体量回填）
  一步不绕：窗口产出 ``source_kind=derived``、父边 origin=``window``、
  列与行数以引擎 overview 为准。
- 引擎校验矩阵（排序类缺 order_by / ``as`` 撞源列 / offset 越界 / 函数闭集）
  → **422 + 引擎原文**（design D1 与 spec 场景逐条写明 422）；旧镜像缺工具
  → 502「引擎暂不支持」如实失败（零伪造）。
- 值正确性（分区/排序/tie-break 的手算对账）在引擎套件
  （DAAS tests/test_customer_data_window.py）——api 腿锁的是**参数保真**
  （引擎收到的参数逐字段等于手写期望）、登记形状与降级语义，不在此重算
  窗口数学（不造第二套实现）。
- 管线 ``window`` 步骤：首跑创建、同版本重跑原地刷新（槽位不变）、失败
  隔离（旧引擎只挂该步）。
- 重派生：api 只发 ``{dataset_key}``，引擎按血缘 spec（kind=window 存
  partition_by/order_by/window）重放；旧算子（filter）刷新入参逐字相同
  ——旧 spec 无 window 键的重放路径不受本算子影响。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest

# 顺序要紧：先装 app（各域路由装齐），再取 datasets.ingest 常量——反过来会
# 撞 ingest ↔ data_plane 循环导入（既有测试同款约定）。
from fd_wire_api.app import app as fastapi_app  # noqa: F401
from fd_wire_api.datasets.ingest import (
    TOOL_DERIVE_REFRESH,
    TOOL_FILTER,
    TOOL_OVERVIEW,
    TOOL_PROJECT,
    TOOL_WINDOW,
    EngineCallError,
)
from test_datasets_common import (
    CUST_HEADERS,
    datasets_env,  # noqa: F401 - pytest fixture
    make_uploaded_dataset,
)

AUTH = CUST_HEADERS["cust-001"]

#: 手写的窗口声明：分区=region、按 amount 降序、row_number + running sum。
#: 引擎收到这三个块必须逐字段等于本常量（参数保真；值由引擎侧手算对账）。
WINDOW_BODY = {
    "name": "分区排名",
    "partition_by": ["region"],
    "order_by": [{"column": "amount", "dir": "desc"}],
    "window": [
        {"func": "row_number", "as": "rn"},
        {
            "func": "sum",
            "column": "amount",
            "as": "running_amount",
            "frame": "running",
        },
    ],
}

#: 引擎 overview 回填后的期望列（源列原样 + 窗口列按 as 追加）。
WINDOW_COLUMNS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "amount", "type": "REAL"},
    {"name": "rn", "type": "INTEGER"},
    {"name": "running_amount", "type": "REAL"},
]


def _post_window(env, dataset_id: str, payload: dict | None = None, *, path: str | None = None):
    return env.client.post(
        path or f"/api/v1/datasets/{dataset_id}/window",
        headers=AUTH,
        json=dict(WINDOW_BODY if payload is None else payload),
    )


def _datasets(env) -> list[dict]:
    return env.client.get("/api/v1/datasets", headers=AUTH).json()


def _parent_refs(env, dataset_id: str) -> list[tuple[str, str]]:
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


def _seed_api_tier_dataset() -> str:
    """免费档（api 层）数据集：无引擎侧表 → 窗口是 cell 耦合付费面。"""
    from fd_wire_api.datasets.db import get_session
    from fd_wire_api.datasets.models import Dataset

    session = get_session()
    try:
        dataset = Dataset(
            customer_id="cust-001",
            key="free_window_src",
            name="免费档源表",
            source_kind="upload",
            storage_tier="api",
            columns_json=[
                {"name": "id", "type": "INTEGER"},
                {"name": "amount", "type": "REAL"},
            ],
            row_count=2,
        )
        session.add(dataset)
        session.commit()
        session.refresh(dataset)
        return dataset.id
    finally:
        session.close()


# ---------------------------------------------------------------- 端点正路径


def test_window_creates_derived_child_with_hand_checked_args(datasets_env) -> None:
    """端点 happy path：参数逐字段保真 + 统一代码路登记 + 列体量回填。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.overview_result = {
        "key": "any",
        "columns": list(WINDOW_COLUMNS),
        "rows": 3,
        "size_bytes": 4096,
    }
    resp = _post_window(env, base["id"])
    assert resp.status_code == 201, resp.text
    child = resp.json()

    # 派生登记：source_kind=derived、父集指向源、名称来自请求
    assert child["source_kind"] == "derived"
    assert child["name"] == "分区排名"
    assert child["source_ref"] == base["key"]
    assert child["storage_tier"] == "cell"
    # 列/行数/体量以引擎 overview 为准（不是本地估算）
    assert [c["name"] for c in child["columns"]] == [
        "id",
        "amount",
        "rn",
        "running_amount",
    ]
    assert child["row_count"] == 3
    assert child["size_bytes"] == 4096
    # 父边登记：唯一父=源，origin=window（敏感度闭包与「随父变化」不漏）
    assert _parent_refs(env, child["id"]) == [(base["id"], "window")]

    # 引擎入参 = 手写期望（parent_key/三块参数 + 统一代码路补齐的字段）
    args = env.fake.calls_for(TOOL_WINDOW)[0]
    assert args["parent_key"] == base["key"]
    assert args["partition_by"] == ["region"]
    assert args["order_by"] == [{"column": "amount", "dir": "desc"}]
    assert args["window"] == WINDOW_BODY["window"]
    assert args["child_key"] and args["child_key"] != base["key"]
    assert args["actor"] == "customer:cust-001"
    assert args["channel"] == "console"
    # 体量回填调用在窗口调用之后
    assert [tool for tool, _ in env.fake.calls][-1] == TOOL_OVERVIEW


def test_window_alias_path_matches_design_contract(datasets_env) -> None:
    """design D5 路径别名与既有数据面路径同函数同语义。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = _post_window(
        env, base["id"], path=f"/api/v1/data/datasets/{base['id']}/window"
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["source_kind"] == "derived"
    assert env.fake.calls_for(TOOL_WINDOW)[0]["parent_key"] == base["key"]


def test_window_counts_and_empty_partition_pass_through(datasets_env) -> None:
    """count（无 column）与无分区（整表一个分区）都是合法声明，原样透传。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    resp = _post_window(
        env,
        base["id"],
        {
            "name": "全表累计",
            "partition_by": [],
            "order_by": [{"column": "id", "dir": "asc"}],
            "window": [
                {"func": "count", "as": "n"},
                {"func": "lag", "column": "amount", "offset": 2, "as": "prev2"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    args = env.fake.calls_for(TOOL_WINDOW)[0]
    assert args["partition_by"] == []
    assert args["order_by"] == [{"column": "id", "dir": "asc"}]
    assert args["window"] == [
        {"func": "count", "as": "n"},
        {"func": "lag", "column": "amount", "offset": 2, "as": "prev2"},
    ]


# ---------------------------------------------------------------- 校验矩阵


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"name": "缺 window"}, id="missing-window"),
        pytest.param(
            {"name": "空 window", "window": []}, id="empty-window"
        ),
        pytest.param(
            {
                "name": "窗口行超限",
                "window": [{"func": "row_number", "as": "rn"}] * 21,
            },
            id="window-rows-over-20",
        ),
        pytest.param(
            {
                "name": "分区列超限",
                "partition_by": [f"c{i}" for i in range(11)],
                "window": [{"func": "count", "as": "n"}],
            },
            id="partition-by-over-10",
        ),
        pytest.param(
            {
                "name": "排序列超限",
                "order_by": [{"column": f"c{i}", "dir": "asc"} for i in range(11)],
                "window": [{"func": "row_number", "as": "rn"}],
            },
            id="order-by-over-10",
        ),
    ],
)
def test_window_structural_limits_422(datasets_env, payload) -> None:
    """结构性上限（与工具入参同形）由请求模型 422，零引擎调用、零登记。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.calls.clear()  # 建档本身有引擎调用；只看窗口请求这一段
    before = len(_datasets(env))
    resp = _post_window(env, base["id"], payload)
    assert resp.status_code == 422, resp.text
    assert env.fake.calls == []  # 请求模型拒收即零引擎
    assert len(_datasets(env)) == before


@pytest.mark.parametrize(
    "engine_error",
    [
        "row_number 需要 order_by（排序类窗口函数）",
        "window 输出列名与源列名冲突: 'amount'（请换一个 as，不静默改名）",
        "lag 的 offset 必须在 1..10000 之间（当前 0）",
        "非法窗口函数: 'foo'（允许 ['avg', 'count', 'dense_rank', 'lag', 'lead', 'max', 'min', 'rank', 'row_number', 'sum']）",
        "frame=running 需要 order_by（累积帧必须有确定行序）",
    ],
)
def test_window_engine_validation_maps_to_422(datasets_env, engine_error) -> None:
    """引擎校验错误 → 422 + 引擎原文（spec 场景「拒绝」口径），零登记。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.error_on[TOOL_WINDOW] = engine_error
    before = len(_datasets(env))
    resp = _post_window(env, base["id"])
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert engine_error in detail
    # 引擎拒绝在子集登记之前：数据集表零新增、父边零新增
    assert len(_datasets(env)) == before


def test_window_engine_call_error_maps_to_422(datasets_env) -> None:
    """引擎抛域错误（非工具缺失）同样 422 如实（不是 502）。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.fail_on[TOOL_WINDOW] = EngineCallError(
        "排序列不存在: 'amount'"
    )
    resp = _post_window(env, base["id"])
    assert resp.status_code == 422
    assert "排序列不存在" in resp.json()["detail"]


def test_existing_operators_keep_400_rejection_mapping(datasets_env) -> None:
    """四算子既有 400 映射零漂移（rejection_status 缺省值不改旧行为）。"""
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


# ---------------------------------------------------------------- 旧引擎降级


def test_old_engine_without_window_tool_is_honest_502(datasets_env) -> None:
    """旧 cell 镜像无 window 工具 → 502 且明示「引擎暂不支持」，零伪造。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.fail_on[TOOL_WINDOW] = EngineCallError(
        "Unknown tool: customer_dataset_window"
    )
    before = len(_datasets(env))
    resp = _post_window(env, base["id"])
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "不支持" in detail and "窗口计算（window）" in detail
    assert "customer_dataset_window" in detail
    assert len(_datasets(env)) == before


# ---------------------------------------------------------------- 闸门


def test_window_tier_gate_api_tier_403(datasets_env) -> None:
    """api 层（免费档）数据集无引擎侧表 → 403 档位闸，零引擎调用。"""
    env = datasets_env
    dataset_id = _seed_api_tier_dataset()
    resp = _post_window(env, dataset_id)
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "PLAN_TIER_REQUIRED"
    assert "升级" in detail["upgrade"]
    assert env.fake.calls == []


def test_window_tenant_isolation_404(datasets_env) -> None:
    """他企数据集一律 404（不泄露存在性），零引擎调用。"""
    env = datasets_env
    foreign = make_uploaded_dataset(env, customer="cust-002", name="他企表")
    env.fake.calls.clear()
    resp = _post_window(env, foreign["id"])
    assert resp.status_code == 404
    assert env.fake.calls == []


def test_window_allowlist_gate_data_key_403(datasets_env) -> None:
    """wdk- allowlist 越界 → 403（引擎零触达）；放行数据集同轨道可写。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    other = make_uploaded_dataset(env, name="放行表")
    minted = env.client.post(
        "/api/v1/data-plane-keys",
        headers=AUTH,
        json={"label": "scoped", "dataset_ids": [other["id"]]},
    )
    assert minted.status_code == 201, minted.text
    key_headers = {"Authorization": f"Bearer {minted.json()['key']}"}
    env.fake.calls.clear()
    resp = env.client.post(
        f"/api/v1/datasets/{base['id']}/window",
        headers=key_headers,
        json=dict(WINDOW_BODY),
    )
    assert resp.status_code == 403, resp.text
    assert "允许清单" in resp.json()["detail"]
    assert env.fake.calls == []
    # 放行数据集走同一端点成功（data-key 轨道可写面）
    ok = env.client.post(
        f"/api/v1/datasets/{other['id']}/window",
        headers=key_headers,
        json=dict(WINDOW_BODY),
    )
    assert ok.status_code == 201, ok.text
    assert env.fake.calls_for(TOOL_WINDOW)[0]["channel"] == "data-key"


# ---------------------------------------------------------------- 管线步骤


def _make_pipeline(env, steps, *, base=None, name="窗口链") -> dict:
    if base is None:
        base = make_uploaded_dataset(env)
    resp = env.client.post(
        "/api/v1/pipelines",
        headers=AUTH,
        json={
            "name": name,
            "description": "window 步骤测试",
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


def test_pipeline_window_step_creates_and_refreshes_in_place(datasets_env) -> None:
    """project → window 两步链：首跑创建、重跑原地刷新（槽位不变）。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    env.fake.overview_result = {
        "key": "any",
        "columns": list(WINDOW_COLUMNS),
        "rows": 3,
        "size_bytes": 4096,
    }
    pipeline = _make_pipeline(
        env,
        [
            {
                "name": "投影",
                "op": "project",
                "params": {"columns": [{"source": "id"}, {"source": "amount"}]},
            },
            {
                "name": "排名",
                "op": "window",
                "params": {
                    "partition_by": WINDOW_BODY["partition_by"],
                    "order_by": WINDOW_BODY["order_by"],
                    "window": WINDOW_BODY["window"],
                },
            },
        ],
        base=base,
    )
    first = _run(env, pipeline["id"]).json()
    assert first["run"]["status"] == "success", first
    assert [s["op"] for s in first["steps"]] == ["project", "window"]
    out_ids = [s["output_dataset_id"] for s in first["steps"]]
    assert len(set(out_ids)) == 2 and all(out_ids)

    # 线性：window 的 parent = project 的 child
    project_args = env.fake.calls_for(TOOL_PROJECT)[0]
    window_args = env.fake.calls_for(TOOL_WINDOW)[0]
    assert window_args["parent_key"] == project_args["child_key"]
    # 槽位命名带版本/序号（可追溯）
    window_out = env.client.get(
        f"/api/v1/datasets/{out_ids[1]}", headers=AUTH
    ).json()
    assert window_out["name"].startswith(pipeline["name"] + ".v1.2.")
    assert window_out["row_count"] == 3
    assert [c["name"] for c in window_out["columns"]][-2:] == [
        "rn",
        "running_amount",
    ]

    # 同版本重跑：槽位原地刷新，不新建输出、不重复调创建工具
    second = _run(env, pipeline["id"]).json()
    assert second["run"]["status"] == "success"
    assert [s["output_dataset_id"] for s in second["steps"]] == out_ids
    assert len(env.fake.calls_for(TOOL_WINDOW)) == 1
    assert len(env.fake.calls_for(TOOL_PROJECT)) == 1
    assert len(env.fake.calls_for(TOOL_DERIVE_REFRESH)) == 2


def test_pipeline_old_engine_fails_window_step_only(datasets_env) -> None:
    """旧引擎缺 window 工具：该步 failed 明示不支持，前序保留、后续无 step 行。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    pipeline = _make_pipeline(
        env,
        [
            {
                "name": "过滤",
                "op": "filter",
                "params": {"filters": [{"column": "id", "op": "gte", "value": 1}]},
            },
            {
                "name": "排名",
                "op": "window",
                "params": {
                    "partition_by": [],
                    "order_by": [{"column": "id", "dir": "asc"}],
                    "window": [{"func": "row_number", "as": "rn"}],
                },
            },
            {
                "name": "投影",
                "op": "project",
                "params": {"columns": [{"source": "id"}]},
            },
        ],
        base=base,
    )
    env.fake.fail_on[TOOL_WINDOW] = EngineCallError(
        "Unknown tool: customer_dataset_window"
    )
    body = _run(env, pipeline["id"]).json()
    assert body["run"]["status"] == "failed"
    assert [s["status"] for s in body["steps"]] == ["success", "failed"]
    assert body["steps"][0]["output_dataset_id"]  # 前序输出保留
    assert len(body["steps"]) == 2  # 第三步未执行、无 step 行
    assert "不支持" in body["steps"][1]["error_text"]
    # 后续步骤的引擎工具未被调用（失败隔离）
    assert not env.fake.calls_for(TOOL_PROJECT)


def test_pipeline_window_engine_validation_marks_step_failed(datasets_env) -> None:
    """window 步骤的引擎校验错误（422）如实落 step failed 并带引擎原文。"""
    env = datasets_env
    pipeline = _make_pipeline(
        env,
        [
            {
                "name": "排名",
                "op": "window",
                "params": {
                    "partition_by": ["region"],
                    "order_by": [],
                    "window": [{"func": "row_number", "as": "rn"}],
                },
            }
        ],
    )
    env.fake.error_on[TOOL_WINDOW] = "row_number 需要 order_by（排序类窗口函数）"
    body = _run(env, pipeline["id"]).json()
    assert body["run"]["status"] == "failed"
    assert "row_number 需要 order_by" in body["steps"][0]["error_text"]


# ---------------------------------------------------------------- 重派生重放


def test_window_child_refresh_replays_via_engine_spec(datasets_env) -> None:
    """重派生：api 只发 dataset_key，引擎按 kind=window 存档 spec 重放。

    旧算子（filter）刷新入参逐字相同——api 侧不存在「带 window 键才重放」
    的第二条路径，旧 spec 的重放字节等价。
    """
    env = datasets_env
    base = make_uploaded_dataset(env)
    window_child = _post_window(env, base["id"]).json()
    filter_child = env.client.post(
        f"/api/v1/datasets/{base['id']}/filter",
        headers=AUTH,
        json={"name": "过滤子集", "filters": [{"column": "id", "op": "gte", "value": 1}]},
    ).json()

    # 引擎重放后的体量以 overview 为准回填（derive_refresh 的 rows 不直接落库）
    env.fake.overview_result = {**env.fake.overview_result, "rows": 4}
    for child in (window_child, filter_child):
        resp = env.client.post(
            f"/api/v1/datasets/{child['id']}/refresh", headers=AUTH
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["source_kind"] == "derived"

    refresh_args = env.fake.calls_for(TOOL_DERIVE_REFRESH)
    assert refresh_args == [
        {"dataset_key": window_child["key"]},
        {"dataset_key": filter_child["key"]},
    ]
    # FakeEngine 的 derive_refresh 返回 rows=4 → 本地行数如实回填
    refreshed = env.client.get(
        f"/api/v1/datasets/{window_child['id']}", headers=AUTH
    ).json()
    assert refreshed["row_count"] == 4


def test_window_child_cascade_refresh(datasets_env) -> None:
    """级联刷新把 window 子集当普通派生子刷新（统一路，零特判）。"""
    env = datasets_env
    base = make_uploaded_dataset(env)
    window_child = _post_window(env, base["id"]).json()
    grandchild = env.client.post(
        f"/api/v1/datasets/{window_child['id']}/project",
        headers=AUTH,
        json={"name": "下游投影", "columns": [{"source": "id"}, {"source": "rn"}]},
    ).json()
    resp = env.client.post(
        f"/api/v1/datasets/{window_child['id']}/refresh?cascade=true", headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    cascade = resp.json()["cascade"]
    ids = {entry["dataset_id"] for entry in cascade}
    assert grandchild["id"] in ids
    assert all(entry["status"] == "ok" for entry in cascade)
    # 本体 + 级联子各重放一次（统一路，窗口子集零特判）
    refreshed = [args["dataset_key"] for args in env.fake.calls_for(TOOL_DERIVE_REFRESH)]
    assert refreshed == [window_child["key"], grandchild["key"]]
