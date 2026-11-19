"""连接器推送三阶段、漂移判定与配额 fail-closed（任务 2.3 核心）。

覆盖：
- 三阶段对 fake 引擎的**调用序列**断言（工具全名 customer_dataset_*、
  chunk 的 start_index 累积）；
- 漂移判定在 api 侧（缺列/类型变 → 409 + paused_drift + 记 drift，且
  不再调引擎）；增列接受并追加；resume 人工确认后恢复；
- 漂移暂停中再同步 → 409；
- 行上限/存储上限 → 429 + paused_quota + 记 quota；
- wrong_index 防重放 409；abort 终态留痕（aborted 行保留不删）；
- 引擎不可达 → 502 fail-closed（绝不假成功）；未配 map/凭据 → 502。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from fd_wire_api.datasets import connector_routes
from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    EngineUnavailable,
    TOOL_INGEST_ABORT,
    TOOL_INGEST_BEGIN,
    TOOL_INGEST_CHUNK,
    TOOL_INGEST_COMMIT,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset, DatasetSync
from fd_wire_api.quality.service import TOOL_QUALITY
from test_datasets_common import (
    CUSTOMER_A,
    connector_headers,
    create_connector_dataset,
    datasets_env,  # noqa: F401 - pytest fixture
    pair_connector,
    patch_dataset_settings,
    push_sync,
)

COLUMNS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "amount", "type": "REAL"},
]
ROWS = [{"id": 1, "amount": 1.5}, {"id": 2, "amount": 2.5}]


def _setup_connector_dataset(env, *, name: str = "Sales Table"):
    paired = pair_connector(env)
    dataset = create_connector_dataset(
        env, connector_id=paired["connector_id"], name=name
    )
    return paired["connector_key"], dataset


def _dataset_row(dataset_id: str) -> Dataset:
    session = get_session()
    try:
        return session.get(Dataset, dataset_id)
    finally:
        session.close()


# ------------------------------------------------------------------ 三阶段


def test_push_three_phases_calls_engine_in_order(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
    )
    assert result["begin"].status_code == 201, result["begin"].text
    assert result["commit"].status_code == 200, result["commit"].text
    assert result["commit"].json() == {"status": "ok", "rows": 2}

    fake = datasets_env.fake
    # 末位 customer_dataset_quality：同步成功的质量快照自动钩子
    # （wire-asset-catalog 4.3——失败隔离，不影响同步结果）
    assert fake.tools == [
        TOOL_INGEST_BEGIN,
        TOOL_INGEST_CHUNK,
        TOOL_INGEST_COMMIT,
        TOOL_QUALITY,
    ]
    begin_args = fake.calls_for(TOOL_INGEST_BEGIN)[0]
    assert begin_args["dataset_key"] == dataset["key"]
    assert begin_args["columns"] == COLUMNS
    assert begin_args["pk_columns"] == ["id"]
    assert begin_args["actor"] == f"connector:{dataset['connector_id']}"
    chunk_args = fake.calls_for(TOOL_INGEST_CHUNK)[0]
    assert chunk_args["start_index"] == 0
    assert chunk_args["rows"] == ROWS
    assert chunk_args["ingest_id"] == f"ing-{dataset['key']}"

    detail = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}", headers=CUSTOMER_A
    ).json()
    assert detail["sync_status"] == "ok"
    assert detail["row_count"] == 2
    assert detail["columns"] == COLUMNS
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["status"] for s in syncs] == ["ok"]
    assert syncs[0]["rows"] == 2


def test_chunk_start_index_accumulates(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    rows = [{"id": i, "amount": float(i)} for i in range(1, 6)]
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=rows,
        chunk_size=2,
    )
    assert result["commit"].status_code == 200, result["commit"].text
    starts = [a["start_index"] for a in datasets_env.fake.calls_for(TOOL_INGEST_CHUNK)]
    assert starts == [0, 2, 4]
    assert datasets_env.fake.chunk_rows == 5


def test_chunk_wrong_index_409(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    headers = connector_headers(key)
    begun = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=headers,
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert begun.status_code == 201, begun.text
    sync_id = begun.json()["sync_id"]
    resp = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/chunks",
        headers=headers,
        json={"index": 3, "rows": ROWS},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("wrong_index:")


def test_commit_row_limit_error_maps_to_429_quota(datasets_env) -> None:
    """引擎侧 commit 报上限类错误 → 429 quota（fail-closed 归类）。"""
    key, dataset = _setup_connector_dataset(datasets_env)
    headers = connector_headers(key)
    begun = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=headers,
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert begun.status_code == 201
    sync_id = begun.json()["sync_id"]
    assert (
        datasets_env.client.post(
            f"/api/v1/connector/syncs/{sync_id}/chunks",
            headers=headers,
            json={"index": 0, "rows": ROWS},
        ).status_code
        == 200
    )
    datasets_env.fake.fail_on[TOOL_INGEST_COMMIT] = EngineCallError(
        "引擎工具报错: 行数超过上限 500000，拒绝提交"
    )
    resp = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/commit", headers=headers
    )
    assert resp.status_code == 429
    assert resp.json()["detail"].startswith("quota:")
    assert _dataset_row(dataset["id"]).sync_status == "paused_quota"
    assert TOOL_INGEST_ABORT in datasets_env.fake.tools


def test_abort_marks_inflight_aborted_and_keeps_history(datasets_env) -> None:
    """abort 留痕：行仍在、status=aborted、含原因；不删历史行
    （wire-provenance-ledger「不可变登记基线」）。"""
    key, dataset = _setup_connector_dataset(datasets_env)
    headers = connector_headers(key)
    begun = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=headers,
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    sync_id = begun.json()["sync_id"]
    assert (
        datasets_env.client.post(
            f"/api/v1/connector/syncs/{sync_id}/chunks",
            headers=headers,
            json={"index": 0, "rows": ROWS},
        ).status_code
        == 200
    )
    assert _dataset_row(dataset["id"]).sync_status == "syncing"
    aborted = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/abort", headers=headers
    )
    assert aborted.status_code == 200
    assert aborted.json() == {"aborted": True}
    assert TOOL_INGEST_ABORT in datasets_env.fake.tools
    assert _dataset_row(dataset["id"]).sync_status == "idle"
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["status"] for s in syncs] == ["aborted"]
    assert syncs[0]["id"] == sync_id
    assert syncs[0]["finished_at"] is not None
    assert "中止" in (syncs[0]["error"] or "")
    assert sync_id not in connector_routes._SYNC_SESSIONS
    # abort 后旧 sync 不能再推分块（终态行）
    assert (
        datasets_env.client.post(
            f"/api/v1/connector/syncs/{sync_id}/chunks",
            headers=headers,
            json={"index": 1, "rows": ROWS},
        ).status_code
        == 409
    )


def test_begin_supersedes_stale_inflight_sync(datasets_env) -> None:
    """未 abort 就重新 begin：旧在飞行终态化 aborted（含原因）、会话清理，
    旧 sid 推分块 → 409（终态，非 404——历史行保留）。"""
    key, dataset = _setup_connector_dataset(datasets_env)
    headers = connector_headers(key)
    payload = {
        "dataset_key": dataset["key"],
        "columns": COLUMNS,
        "pk_columns": [],
        "row_count": 2,
    }
    first = datasets_env.client.post("/api/v1/connector/syncs", headers=headers, json=payload)
    first_id = first.json()["sync_id"]
    second = datasets_env.client.post("/api/v1/connector/syncs", headers=headers, json=payload)
    assert second.status_code == 201
    assert first_id not in connector_routes._SYNC_SESSIONS
    stale_chunk = datasets_env.client.post(
        f"/api/v1/connector/syncs/{first_id}/chunks",
        headers=headers,
        json={"index": 0, "rows": ROWS},
    )
    # 旧行不删：sync_id 仍存在但已终态 → 409（原为 404——删行语义已废除）
    assert stale_chunk.status_code == 409
    assert "aborted" in stale_chunk.json()["detail"]
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    # 旧在飞行落 aborted 终态留痕（含原因）；新一次的在飞行在前
    assert [s["status"] for s in syncs] == ["syncing", "aborted"]
    assert syncs[0]["id"] != first_id
    assert syncs[1]["id"] == first_id
    assert "取代" in (syncs[1]["error"] or "")


def test_failed_sync_keeps_paused_reason_empty(datasets_env) -> None:
    """failed 不是暂停态：paused_reason 只服务 paused_drift/paused_quota。"""
    key, dataset = _setup_connector_dataset(datasets_env)
    datasets_env.fake.fail_on[TOOL_INGEST_BEGIN] = EngineUnavailable("cell 未开通")
    resp = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert resp.status_code == 502
    row = _dataset_row(dataset["id"])
    assert row.sync_status == "failed"
    assert row.paused_reason is None


def test_commit_engine_unreachable_502_fail_closed(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    headers = connector_headers(key)
    begun = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=headers,
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    sync_id = begun.json()["sync_id"]
    datasets_env.fake.fail_on[TOOL_INGEST_COMMIT] = EngineUnavailable(
        "customer cust-001 的 cell 未开通（AI_CELL_ENDPOINT_MAP 无映射）"
    )
    resp = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/commit", headers=headers
    )
    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("engine_unavailable:")
    row = _dataset_row(dataset["id"])
    assert row.sync_status == "failed"
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["status"] for s in syncs] == ["failed"]


def test_begin_without_engine_config_fail_closed_502(datasets_env, monkeypatch) -> None:
    """未配 AI_CELL_ENDPOINT_MAP / AI_PLATFORM_CREDENTIAL → 502，不假成功。"""
    key, dataset = _setup_connector_dataset(datasets_env)
    datasets_env.app.dependency_overrides.pop(get_engine_client_factory)
    monkeypatch.delenv("AI_CELL_ENDPOINT_MAP", raising=False)
    monkeypatch.delenv("AI_PLATFORM_CREDENTIAL", raising=False)
    from fd_wire_api.ai.settings import get_settings as ai_get_settings

    ai_get_settings.cache_clear()
    resp = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("engine_unavailable:")
    assert _dataset_row(dataset["id"]).sync_status == "failed"
    ai_get_settings.cache_clear()


# ------------------------------------------------------------------ 漂移


def test_missing_column_drift_409_pauses_without_engine_call(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    assert (
        push_sync(
            datasets_env, connector_key=key, dataset_key=dataset["key"],
            columns=COLUMNS, rows=ROWS,
        )["commit"].status_code
        == 200
    )
    calls_before = len(datasets_env.fake.calls)

    resp = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset["key"],
            "columns": [{"name": "amount", "type": "REAL"}],
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"].startswith("drift:")
    assert body["differences"] == [
        {"column": "id", "kind": "missing", "expected": "INTEGER", "actual": None}
    ]
    # 破坏性漂移不调引擎（判定在 api 侧）
    assert len(datasets_env.fake.calls) == calls_before
    row = _dataset_row(dataset["id"])
    assert row.sync_status == "paused_drift"
    assert "id" in (row.paused_reason or "")
    # 暂停中再同步 → 409（等待人工确认）
    again = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert again.status_code == 409
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["status"] for s in syncs] == ["drift", "ok"]


def test_type_change_drift_409(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    assert (
        push_sync(
            datasets_env, connector_key=key, dataset_key=dataset["key"],
            columns=COLUMNS, rows=ROWS,
        )["commit"].status_code
        == 200
    )
    resp = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset["key"],
            "columns": [
                {"name": "id", "type": "TEXT"},
                {"name": "amount", "type": "REAL"},
            ],
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["differences"] == [
        {
            "column": "id",
            "kind": "type_changed",
            "expected": "INTEGER",
            "actual": "TEXT",
        }
    ]


def test_additive_column_accepted_and_appended(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    assert (
        push_sync(
            datasets_env, connector_key=key, dataset_key=dataset["key"],
            columns=COLUMNS, rows=ROWS,
        )["commit"].status_code
        == 200
    )
    extended = COLUMNS + [{"name": "note", "type": "TEXT"}]
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=extended,
        rows=[{"id": 1, "amount": 1.5, "note": "x"}],
    )
    assert result["commit"].status_code == 200, result["commit"].text
    assert _dataset_row(dataset["id"]).columns_json == extended


def test_resume_after_drift_then_sync_succeeds(datasets_env) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    assert (
        push_sync(
            datasets_env, connector_key=key, dataset_key=dataset["key"],
            columns=COLUMNS, rows=ROWS,
        )["commit"].status_code
        == 200
    )
    dropped = [{"name": "amount", "type": "REAL"}]
    assert (
        datasets_env.client.post(
            "/api/v1/connector/syncs",
            headers=connector_headers(key),
            json={
                "dataset_key": dataset["key"],
                "columns": dropped,
                "pk_columns": [],
                "row_count": 1,
            },
        ).status_code
        == 409
    )
    resumed = datasets_env.client.post(
        f"/api/v1/datasets/{dataset['id']}/resume",
        headers=CUSTOMER_A,
        json={"accept_columns": dropped},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["sync_status"] == "idle"
    assert resumed.json()["columns"] == dropped
    # 仅 drift 暂停可恢复：非暂停态再 resume → 409
    assert (
        datasets_env.client.post(
            f"/api/v1/datasets/{dataset['id']}/resume",
            headers=CUSTOMER_A,
            json={"accept_columns": dropped},
        ).status_code
        == 409
    )
    result = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=dropped,
        rows=[{"amount": 9.5}],
    )
    assert result["commit"].status_code == 200


# ------------------------------------------------------------------ 配额


def test_row_quota_429_pauses_and_skips_engine(datasets_env, monkeypatch) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    patch_dataset_settings(monkeypatch, dataset_max_rows=2)
    calls_before = len(datasets_env.fake.calls)
    resp = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 3,
        },
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"].startswith("quota:")
    assert len(datasets_env.fake.calls) == calls_before
    row = _dataset_row(dataset["id"])
    assert row.sync_status == "paused_quota"
    assert "上限" in (row.paused_reason or "")
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["status"] for s in syncs] == ["quota"]


def test_storage_quota_429_on_next_sync(datasets_env, monkeypatch) -> None:
    key, dataset = _setup_connector_dataset(datasets_env)
    datasets_env.fake.commit_size_bytes = 4096
    patch_dataset_settings(monkeypatch, dataset_storage_limit_bytes=100)
    first = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
    )
    assert first["commit"].status_code == 200
    # 引擎回执精确尺寸越限：保留快照但暂停后续写入
    assert _dataset_row(dataset["id"]).sync_status == "paused_quota"
    resp = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(key),
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    assert resp.status_code == 429
    assert resp.json()["detail"].startswith("quota:")


def test_storage_quota_commit_precheck_aborts_staging(datasets_env, monkeypatch) -> None:
    """提交前的存储预检越限：abort 暂存、不 commit、记 quota。"""
    key, dataset = _setup_connector_dataset(datasets_env)
    headers = connector_headers(key)
    begun = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=headers,
        json={
            "dataset_key": dataset["key"],
            "columns": COLUMNS,
            "pk_columns": [],
            "row_count": 2,
        },
    )
    sync_id = begun.json()["sync_id"]
    datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/chunks",
        headers=headers,
        json={"index": 0, "rows": ROWS},
    )
    # 提交前把配额收紧到 10 字节：预检越限 → abort 暂存、绝不 commit
    patch_dataset_settings(monkeypatch, dataset_storage_limit_bytes=10)
    resp = datasets_env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/commit", headers=headers
    )
    assert resp.status_code == 429
    assert TOOL_INGEST_COMMIT not in datasets_env.fake.tools
    assert TOOL_INGEST_ABORT in datasets_env.fake.tools
    assert _dataset_row(dataset["id"]).sync_status == "paused_quota"
    syncs = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUSTOMER_A
    ).json()
    assert [s["status"] for s in syncs] == ["quota"]


def test_sync_history_row_model_terminal_statuses() -> None:
    """dataset_syncs 行结构按契约 §3.2（终态 ok/failed/drift/quota；在飞 syncing；
    mode 记本轮同步模式——wire-connector-incremental-sync 0025）。"""
    assert set(DatasetSync.__table__.columns.keys()) == {
        "id",
        "dataset_id",
        "started_at",
        "finished_at",
        "status",
        "rows",
        "error",
        "mode",
    }