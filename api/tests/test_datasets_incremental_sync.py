"""增量水位同步 api 侧（wire-connector-incremental-sync 3.2/3.3/3.5）。

覆盖：
- config 下发 ``sync_mode``/``watermark_column``/``watermark_value`` 三键
  （水位值字符串原样——整型精度端到端；snapshot 数据集缺省形态）；
- begin 模式分派：增量+水位 → 引擎 ingest_begin 参数含 ``mode=merge``；
  增量无水位（首同步）→ 不含 mode（=replace 建基线）；快照 → 不含 mode；
  dataset_syncs 历史行记 mode；
- commit 水位：增量数据集水位落库（Trim 后存储）；空串不动；非增量数据集
  带水位 422（fail-closed 防误配）；超长 422 且零副作用（引擎未 commit）；
- 控制台 sync-settings：非连接器 422、列外列名 422、incremental 缺水位列
  422、列基线为空 422、非法模式 422、快照态给水位列 422、切回 snapshot
  清两字段、换水位列清水位值、reset_watermark 只清水位值、跨客户 404、
  详情/历史响应带新字段；
- 既有 failed 语义不受影响（增量数据集引擎报错 → 502 + failed 留痕）。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    TOOL_INGEST_BEGIN,
    TOOL_INGEST_COMMIT,
)
from fd_wire_api.datasets.models import Dataset
from test_datasets_common import (
    CUSTOMER_A,
    CUSTOMER_B,
    connector_headers,
    create_connector_dataset,
    datasets_env,  # noqa: F401 - pytest fixture
    make_uploaded_dataset,
    pair_connector,
    push_sync,
)

COLUMNS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "amount", "type": "REAL"},
]
ROWS = [{"id": 1, "amount": 1.5}, {"id": 2, "amount": 2.5}]
#: 大整型水位（int64 上界）：字符串原样落库/下发，精度零损失
BIG_INT_WATERMARK = "9223372036854775807"


def _synced_connector_dataset(env):
    """配对 + 建连接器数据集 + 一次成功快照同步（columns_json 有基线）。"""
    paired = pair_connector(env)
    key = paired["connector_key"]
    dataset = create_connector_dataset(env, connector_id=paired["connector_id"])
    result = push_sync(
        env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
    )
    assert result["commit"].status_code == 200, result["commit"].text
    return key, dataset


def _patch_settings(env, dataset_id: str, body: dict, headers=CUSTOMER_A):
    return env.client.patch(
        f"/api/v1/datasets/{dataset_id}/sync-settings", headers=headers, json=body
    )


def _enable_incremental(env, dataset_id: str, column: str = "id") -> dict:
    resp = _patch_settings(env, dataset_id, {"sync_mode": "incremental", "watermark_column": column})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _dataset_row(dataset_id: str) -> Dataset:
    session = get_session()
    try:
        return session.get(Dataset, dataset_id)
    finally:
        session.close()


def _begin(env, key: str, dataset_key: str) -> object:
    return env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset_key,
            "columns": COLUMNS,
            "pk_columns": ["id"],
            "row_count": len(ROWS),
        },
    )


# ------------------------------------------------------------------ config 下发


def test_config_downloads_sync_mode_and_watermark_fields(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    # 增量首同步（无水位）+ commit 捕获初始水位：大整型字符串原样
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value=BIG_INT_WATERMARK,
    )
    assert result["commit"].status_code == 200, result["commit"].text
    config = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(key)
    ).json()
    entry = config["datasets"][0]
    assert entry["sync_mode"] == "incremental"
    assert entry["watermark_column"] == "id"
    assert entry["watermark_value"] == BIG_INT_WATERMARK


def test_config_defaults_snapshot_watermark_none(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    config = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(key)
    ).json()
    entry = config["datasets"][0]
    assert entry["sync_mode"] == "snapshot"
    assert entry["watermark_column"] is None
    assert entry["watermark_value"] is None


# ------------------------------------------------------------------ begin 模式分派


def test_begin_incremental_with_watermark_sends_merge(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    first = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="100",
    )
    assert first["commit"].status_code == 200
    begun = _begin(datasets_env, key, dataset["key"])
    assert begun.status_code == 201, begun.text
    begin_args = datasets_env.fake.calls_for(TOOL_INGEST_BEGIN)[-1]
    assert begin_args["mode"] == "merge"


def test_begin_incremental_without_watermark_omits_mode(datasets_env) -> None:
    """增量首同步（无水位值）：引擎参数不含 mode（=replace 建基线）。"""
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    begun = _begin(datasets_env, key, dataset["key"])
    assert begun.status_code == 201, begun.text
    begin_args = datasets_env.fake.calls_for(TOOL_INGEST_BEGIN)[-1]
    assert "mode" not in begin_args


def test_begin_snapshot_omits_mode(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    begun = _begin(datasets_env, key, dataset["key"])
    assert begun.status_code == 201, begun.text
    begin_args = datasets_env.fake.calls_for(TOOL_INGEST_BEGIN)[-1]
    assert "mode" not in begin_args


def test_sync_history_rows_record_mode(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)  # 首行 snapshot
    _enable_incremental(datasets_env, dataset["id"], column="id")
    second = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="100",
    )
    assert second["commit"].status_code == 200
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["mode"] for s in syncs] == ["incremental", "snapshot"]


# ------------------------------------------------------------------ commit 水位


def test_commit_watermark_persisted_trimmed(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="  2026-10-11T08:00:00Z  ",
    )
    assert result["commit"].status_code == 200, result["commit"].text
    row = _dataset_row(dataset["id"])
    assert row.watermark_value == "2026-10-11T08:00:00Z"  # Trim 后存储


def test_commit_empty_string_watermark_is_noop(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    first = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="100",
    )
    assert first["commit"].status_code == 200
    # 第二轮零行：连接器上报空串=未上报，水位不动
    second = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="",
    )
    assert second["commit"].status_code == 200
    assert _dataset_row(dataset["id"]).watermark_value == "100"


def _open_sync_session(env, key: str, dataset_key: str) -> str:
    begun = _begin(env, key, dataset_key)
    assert begun.status_code == 201, begun.text
    return begun.json()["sync_id"]


def test_commit_watermark_on_snapshot_dataset_422(datasets_env) -> None:
    """非增量数据集带水位 → 422 拒绝（fail-closed，防误配）。"""
    key, dataset = _synced_connector_dataset(datasets_env)
    sync_id = _open_sync_session(datasets_env, key, dataset["key"])
    commits_before = len(datasets_env.fake.calls_for(TOOL_INGEST_COMMIT))
    resp = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/commit",
        headers=connector_headers(key),
        json={"watermark_value": "100"},
    )
    assert resp.status_code == 422, resp.text
    assert "非增量" in resp.json()["detail"]
    # 零副作用：引擎未被 commit，会话仍在（可无水位重提）
    assert len(datasets_env.fake.calls_for(TOOL_INGEST_COMMIT)) == commits_before
    retry = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/commit", headers=connector_headers(key)
    )
    assert retry.status_code == 200


def test_commit_watermark_overlong_422(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    sync_id = _open_sync_session(datasets_env, key, dataset["key"])
    commits_before = len(datasets_env.fake.calls_for(TOOL_INGEST_COMMIT))
    resp = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/commit",
        headers=connector_headers(key),
        json={"watermark_value": "  " + "x" * 300 + "  "},
    )
    assert resp.status_code == 422, resp.text
    assert "超长" in resp.json()["detail"]
    assert len(datasets_env.fake.calls_for(TOOL_INGEST_COMMIT)) == commits_before


def test_commit_watermark_boundary_256_accepted(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    value = "9" * 256
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value=value,
    )
    assert result["commit"].status_code == 200
    assert _dataset_row(dataset["id"]).watermark_value == value


# ------------------------------------------------------------------ sync-settings


def test_sync_settings_rejects_non_connector_dataset(datasets_env) -> None:
    uploaded = make_uploaded_dataset(datasets_env)  # source_kind=upload
    resp = _patch_settings(
        datasets_env, uploaded["id"], {"sync_mode": "incremental", "watermark_column": "id"}
    )
    assert resp.status_code == 422
    assert "连接器" in resp.json()["detail"]


def test_sync_settings_rejects_unknown_watermark_column(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    resp = _patch_settings(
        datasets_env, dataset["id"], {"sync_mode": "incremental", "watermark_column": "nope"}
    )
    assert resp.status_code == 422
    assert "nope" in resp.json()["detail"]
    assert "既有列" in resp.json()["detail"]


def test_sync_settings_incremental_requires_watermark_column(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    resp = _patch_settings(datasets_env, dataset["id"], {"sync_mode": "incremental"})
    assert resp.status_code == 422
    assert "水位列" in resp.json()["detail"]


def test_sync_settings_rejects_empty_column_baseline(datasets_env) -> None:
    """列基线为空（连接器数据集尚未首同步）：任何列名都不合格。"""
    paired = pair_connector(datasets_env)
    dataset = create_connector_dataset(
        datasets_env, connector_id=paired["connector_id"]
    )
    resp = _patch_settings(
        datasets_env,
        dataset["id"],
        {"sync_mode": "incremental", "watermark_column": "id"},
    )
    assert resp.status_code == 422
    assert "既有列" in resp.json()["detail"]


def test_sync_settings_rejects_invalid_mode_and_snapshot_column(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    bad_mode = _patch_settings(
        datasets_env, dataset["id"], {"sync_mode": "fast"}
    )
    assert bad_mode.status_code == 422  # pydantic Literal 拦截
    snapshot_column = _patch_settings(
        datasets_env, dataset["id"], {"watermark_column": "id"}
    )
    assert snapshot_column.status_code == 422
    assert "快照" in snapshot_column.json()["detail"]


def test_sync_settings_switch_back_to_snapshot_clears_fields(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="100",
    )
    assert result["commit"].status_code == 200
    resp = _patch_settings(datasets_env, dataset["id"], {"sync_mode": "snapshot"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sync_mode"] == "snapshot"
    assert body["watermark_column"] is None
    assert body["watermark_value"] is None
    # 切回快照后 begin 不再带 mode
    begun = _begin(datasets_env, key, dataset["key"])
    assert begun.status_code == 201
    assert "mode" not in datasets_env.fake.calls_for(TOOL_INGEST_BEGIN)[-1]


def test_sync_settings_reset_watermark_only_clears_value(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="100",
    )
    assert result["commit"].status_code == 200
    resp = _patch_settings(datasets_env, dataset["id"], {"reset_watermark": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sync_mode"] == "incremental"  # 模式保留
    assert body["watermark_column"] == "id"  # 列保留
    assert body["watermark_value"] is None  # 只清水位值
    # 重置后下一轮 begin 回全量替换（不含 mode），commit 重新捕获
    rebuilt = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="200",
    )
    assert rebuilt["commit"].status_code == 200
    assert "mode" not in datasets_env.fake.calls_for(TOOL_INGEST_BEGIN)[-1]
    assert _dataset_row(dataset["id"]).watermark_value == "200"


def test_sync_settings_changing_column_clears_value(datasets_env) -> None:
    """换水位列：旧水位值属于旧列（比较语义失效），一并清空重建基线。"""
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="100",
    )
    assert result["commit"].status_code == 200
    resp = _patch_settings(datasets_env, dataset["id"], {"watermark_column": "amount"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sync_mode"] == "incremental"
    assert body["watermark_column"] == "amount"
    assert body["watermark_value"] is None


def test_sync_settings_cross_customer_404(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    resp = _patch_settings(
        datasets_env,
        dataset["id"],
        {"sync_mode": "incremental", "watermark_column": "id"},
        headers=CUSTOMER_B,
    )
    assert resp.status_code == 404


def test_dataset_detail_and_list_expose_incremental_fields(datasets_env) -> None:
    key, dataset = _synced_connector_dataset(datasets_env)
    detail = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}", headers=CUSTOMER_A
    ).json()
    assert detail["sync_mode"] == "snapshot"
    assert detail["watermark_column"] is None
    assert detail["watermark_value"] is None
    listed = datasets_env.client.get("/api/v1/datasets", headers=CUSTOMER_A).json()
    assert listed[0]["sync_mode"] == "snapshot"
    _enable_incremental(datasets_env, dataset["id"], column="amount")
    detail = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}", headers=CUSTOMER_A
    ).json()
    assert detail["sync_mode"] == "incremental"
    assert detail["watermark_column"] == "amount"


# ------------------------------------------------------------------ 既有 failed 语义


def test_incremental_engine_error_still_failed_not_paused(datasets_env) -> None:
    """增量数据集引擎报错（如 merge 超限在引擎侧整批回滚）→ 既有 failed
    口径：502 fail-closed、sync_status=failed、历史行留痕（含 mode）。"""
    key, dataset = _synced_connector_dataset(datasets_env)
    _enable_incremental(datasets_env, dataset["id"], column="id")
    first = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
        watermark_value="100",
    )
    assert first["commit"].status_code == 200
    datasets_env.fake.fail_on[TOOL_INGEST_BEGIN] = EngineCallError(
        "引擎工具报错: merge 合并后行数超过上限 500000，已回滚"
    )
    resp = _begin(datasets_env, key, dataset["key"])
    assert resp.status_code == 502
    row = _dataset_row(dataset["id"])
    assert row.sync_status == "failed"
    assert row.paused_reason is None
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["status"] for s in syncs] == ["failed", "ok", "ok"]
    assert syncs[0]["mode"] == "incremental"
