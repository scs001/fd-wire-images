"""数据集质量快照通道测试（wire-asset-catalog 任务组 4，任务 4.1–4.6）。

覆盖：
- 快照内容：好 payload → ok 快照行；**payload 无任何数据值**（honeypot
  越权字段被白名单净化剥掉）；未声明键列 → pk_declared=False 且比例恒
  None（不给伪造数字）；
- 引擎版本滚动窗口（design D6）：EngineUnavailable / 工具缺失类报错 →
  ``unsupported``；自动触发 24h 退避（窗口内第二次自动触发静默跳过、
  引擎零调用），手动触发不受限；
- 失败隔离（spec「快照失败不伤主流程」）：质量工具崩 → ``failed`` +
  error_text，同步主流程照常 200/ok；三时机自动快照（create=上传建档、
  sync=连接器 commit、materialize=派生重算）；
- 历史与保留期：连续快照序列有序，每数据集裁剪保留 30 条；
- 新鲜度（design D4，读取时计算）：声明日更 + 最新 ok 快照 4 天前 →
  「过期」；无节奏 → 「未知」（绝不「过期」）；日更 + 刚刷新 → 「正常」；
  cadence 种进姊妹切片的 dataset_asset_meta（优先 ORM，模块缺席回落
  最小表原生 SQL——本测试只写测试数据，业务面永不写该表）；
- 鉴权：三端点未鉴权 401；手动刷新 viewer 403（统一角色门禁）、可读
  端点 viewer 200；他企数据集 404；api 层数据集手动刷新 403 档位闸。

环境：datasets 内存库 + 平台临时库（basic 档种子）+ 假引擎（质量工具
可编程），照 test_datasets_common 的 datasets_env 纪律。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.datasets import connector_routes
from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    EngineUnavailable,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.quality.models import DatasetQualitySnapshot
from fd_wire_api.quality.service import TOOL_QUALITY

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token
from test_datasets_common import (
    CUSTOMER_B,
    CUST_HEADERS,
    INTERNAL_TOKEN,
    SUB_A,
    SUB_B,
    FakeEngine,
    create_connector_dataset,
    make_uploaded_dataset,
    pair_connector,
    push_sync,
)

SUB_VIEWER = "sub-viewer-001"
VIEWER = bearer(mint_id_token(SUB_VIEWER))

#: 快照行里的 honeypot 数据值——白名单净化后绝不允许出现在任何存储字段
_HONEYPOT = "SECRET-DATA-VALUE"


class QualityFakeEngine(FakeEngine):
    """引擎假件扩展：支持质量工具（payload 可编程；含 honeypot 越权字段）。"""

    def __init__(self) -> None:
        super().__init__()
        #: 好样本 payload（computed_at="NOW" → 调用时取当前 UTC 时间，测试
        #: 不随日历漂移）；sample_rows 是引擎**多给**的字段（含数据值），
        #: 净化器必须剥掉——快照 SHALL NOT 包含任何数据值。
        self.quality_payload: dict = {
            "row_count": 120,
            "computed_at": "NOW",
            "pk_uniqueness": {"columns": ["id"], "unique_ratio": 0.9833},
            "pk_declared": True,
            "null_rates": [
                {"column": "id", "null_rate": 0.0, "sampled_rows": 10000},
                {"column": "amount", "null_rate": 0.05, "sampled_rows": 10000},
            ],
            "sample_limit_hit": False,
            "sample_rows": [{"id": 7, "amount": _HONEYPOT}],
        }

    def _result(self, tool: str, arguments: dict) -> dict:
        if tool == TOOL_QUALITY:
            payload = dict(self.quality_payload)
            if payload.get("computed_at") == "NOW":
                payload["computed_at"] = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
            return {"success": True, **payload}
        return super()._result(tool, arguments)


@pytest.fixture()
def quality_env(tmp_path, monkeypatch):
    """隔离运行时（datasets 内存库 + 平台临时库 basic 档）+ 质量假引擎。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    # materialization 侧（派生重算钩子用例）：独立引擎钉进临时库
    monkeypatch.setenv("MATERIALIZATION_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db")
    monkeypatch.setenv("MATERIALIZATION_AUTO_CREATE_TABLES", "1")
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_A, "cust-001"), (SUB_B, "cust-002")],
        internal_token=INTERNAL_TOKEN,
        plan_code="basic",
    )
    # viewer 成员（wire-team-spaces 三角角色：手动刷新 owner/editor、viewer 403）
    from fd_wire_api.console.models import CustomerMember

    with console_env.session() as sess:
        sess.add(
            CustomerMember(
                customer_id=console_env.subs[SUB_A],
                logto_sub=SUB_VIEWER,
                role="viewer",
            )
        )
        sess.commit()

    reset_datasets_runtime()
    connector_routes._SYNC_SESSIONS.clear()
    from fd_wire_api.materialization.db import reset_materialization_runtime

    reset_materialization_runtime()

    fake = QualityFakeEngine()
    fastapi_app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: fake
    )
    with TestClient(fastapi_app) as client:
        yield SimpleNamespace(client=client, fake=fake, app=fastapi_app)
    fastapi_app.dependency_overrides.clear()
    connector_routes._SYNC_SESSIONS.clear()
    reset_datasets_runtime()
    reset_materialization_runtime()


# ------------------------------------------------------------------ 直读助手


def _snapshots(dataset_id: str) -> list[DatasetQualitySnapshot]:
    """快照行直读（插入序；测试只读断言用）。"""
    session = get_session()
    try:
        return list(
            session.execute(
                select(DatasetQualitySnapshot)
                .where(DatasetQualitySnapshot.dataset_id == dataset_id)
                .order_by(DatasetQualitySnapshot.id)
            )
            .scalars()
        )
    finally:
        session.close()


def _seed_cadence(dataset_id: str, cadence: str | None) -> None:
    """种 dataset_asset_meta.cadence_hint（姊妹切片的表，业务面只读）。

    优先走姊妹模块 ORM（缺省值齐）；模块缺席时回落最小表原生 SQL
    （sqlite 专用，仅测试）。None = 清除声明。
    """
    try:
        from fd_wire_api.assets.models import AssetMeta
    except ImportError:
        session = get_session()
        try:
            session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS dataset_asset_meta ("
                    "dataset_id VARCHAR(36) PRIMARY KEY, cadence_hint VARCHAR(16))"
                )
            )
            session.execute(
                text(
                    "DELETE FROM dataset_asset_meta WHERE dataset_id = :d"
                ),
                {"d": dataset_id},
            )
            if cadence is not None:
                session.execute(
                    text(
                        "INSERT INTO dataset_asset_meta (dataset_id, cadence_hint)"
                        " VALUES (:d, :c)"
                    ),
                    {"d": dataset_id, "c": cadence},
                )
            session.commit()
        finally:
            session.close()
        return
    session = get_session()
    try:
        session.merge(AssetMeta(dataset_id=dataset_id, cadence_hint=cadence))
        session.commit()
    finally:
        session.close()


def _insert_snapshot(
    dataset_id: str,
    *,
    status: str = "ok",
    computed_at: datetime | None = None,
    triggered_by: str = "sync",
    row_count: int | None = 999,
) -> int:
    """直插一条快照（新鲜度边界用例构造历史时间戳；绕过引擎）。"""
    session = get_session()
    try:
        row = DatasetQualitySnapshot(
            dataset_id=dataset_id,
            computed_at=computed_at
            or datetime.now(timezone.utc).replace(tzinfo=None),
            triggered_by=triggered_by,
            status=status,
            row_count=row_count,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


# ------------------------------------------------------------------ 快照内容


def test_upload_create_writes_ok_snapshot_without_data_values(quality_env) -> None:
    """上传建档（数据刚落引擎）→ create 触发 ok 快照；净化后无任何数据值。"""
    dataset = make_uploaded_dataset(quality_env)

    calls = quality_env.fake.calls_for(TOOL_QUALITY)
    assert calls and calls[0]["dataset_key"] == dataset["key"]

    rows = _snapshots(dataset["id"])
    assert len(rows) == 1
    snap = rows[0]
    assert snap.status == "ok"
    assert snap.triggered_by == "create"
    assert snap.row_count == 120
    assert snap.pk_declared is True
    assert snap.pk_unique_ratio == pytest.approx(0.9833)
    assert snap.sample_limit_hit is False
    assert snap.error_text is None
    assert snap.computed_at is not None

    # 空值率条目只承载聚合三键（column/null_rate/sampled_rows）
    for item in snap.null_rates_json or []:
        assert set(item) == {"column", "null_rate", "sampled_rows"}

    # honeypot 数据值（引擎多给的 sample_rows 字段）绝不出现在任何存储字段
    dump = str(
        [snap.null_rates_json, snap.error_text, snap.row_count, snap.pk_unique_ratio]
    )
    assert _HONEYPOT not in dump
    assert "sample_rows" not in dump


def test_pk_not_declared_gives_null_ratio(quality_env) -> None:
    """未声明键列 → pk_declared=False、比例恒 None（不给伪造数字）。"""
    dataset = make_uploaded_dataset(quality_env)
    quality_env.fake.quality_payload = {
        "row_count": 10,
        "computed_at": "NOW",
        "pk_uniqueness": None,
        "pk_declared": False,
        "null_rates": [{"column": "id", "null_rate": 0.0, "sampled_rows": 10}],
        "sample_limit_hit": False,
    }
    resp = quality_env.client.post(
        f"/api/v1/datasets/{dataset['id']}/quality/refresh", headers=CUST_HEADERS["cust-001"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["triggered_by"] == "manual"
    assert body["pk_declared"] is False
    assert body["pk_unique_ratio"] is None  # 未声明键列：不给比例值


# ------------------------------------------------- 引擎不支持降级与退避（D6）


def test_engine_unsupported_then_automatic_backoff_manual_always_tries(
    quality_env,
) -> None:
    """EngineUnavailable → unsupported；24h 内自动触发退避（引擎零调用）、
    手动触发不受限。"""
    paired = pair_connector(quality_env)
    dataset = create_connector_dataset(
        quality_env, connector_id=paired["connector_id"]
    )
    quality_env.fake.fail_on[TOOL_QUALITY] = EngineUnavailable(
        "customer cust-001 的 cell 未开通（握手期端点拒绝）"
    )

    first = push_sync(
        quality_env,
        connector_key=paired["connector_key"],
        dataset_key=dataset["key"],
        columns=[{"name": "id", "type": "INTEGER"}],
        rows=[{"id": 1}],
    )
    assert first["commit"].status_code == 200, first["commit"].text
    rows = _snapshots(dataset["id"])
    assert [r.status for r in rows] == ["unsupported"]
    assert rows[0].triggered_by == "sync"
    assert rows[0].error_text is not None
    assert rows[0].row_count is None  # 不支持态不给任何指标值
    assert len(quality_env.fake.calls_for(TOOL_QUALITY)) == 1

    # 退避窗口内的第二次自动触发：静默跳过——引擎不再被叫、快照不新增
    second = push_sync(
        quality_env,
        connector_key=paired["connector_key"],
        dataset_key=dataset["key"],
        columns=[{"name": "id", "type": "INTEGER"}],
        rows=[{"id": 2}],
    )
    assert second["commit"].status_code == 200, second["commit"].text
    assert len(quality_env.fake.calls_for(TOOL_QUALITY)) == 1
    assert len(_snapshots(dataset["id"])) == 1

    # 手动触发永远尝试（不受退避限制）
    manual = quality_env.client.post(
        f"/api/v1/datasets/{dataset['id']}/quality/refresh",
        headers=CUST_HEADERS["cust-001"],
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["status"] == "unsupported"
    assert len(quality_env.fake.calls_for(TOOL_QUALITY)) == 2
    assert len(_snapshots(dataset["id"])) == 2


def test_tool_not_found_error_maps_to_unsupported(quality_env) -> None:
    """旧 cell 镜像无质量工具（工具缺失类报错）→ unsupported 降级态。"""
    paired = pair_connector(quality_env)
    dataset = create_connector_dataset(
        quality_env, connector_id=paired["connector_id"]
    )
    quality_env.fake.fail_on[TOOL_QUALITY] = EngineCallError(
        "引擎MCP error: Unknown tool: customer_dataset_quality"
    )
    result = push_sync(
        quality_env,
        connector_key=paired["connector_key"],
        dataset_key=dataset["key"],
        columns=[{"name": "id", "type": "INTEGER"}],
        rows=[{"id": 1}],
    )
    assert result["commit"].status_code == 200
    rows = _snapshots(dataset["id"])
    assert [r.status for r in rows] == ["unsupported"]


# ------------------------------------------------- 失败隔离与三时机钩子（4.3）


def test_generic_failure_records_failed_and_main_flow_unaffected(quality_env) -> None:
    """质量工具崩 → failed 快照行 + error_text；同步主流程照常成功。"""
    paired = pair_connector(quality_env)
    dataset = create_connector_dataset(
        quality_env, connector_id=paired["connector_id"]
    )
    quality_env.fake.fail_on[TOOL_QUALITY] = EngineCallError(
        "引擎工具报错: 内部协程崩溃"
    )
    result = push_sync(
        quality_env,
        connector_key=paired["connector_key"],
        dataset_key=dataset["key"],
        columns=[{"name": "id", "type": "INTEGER"}, {"name": "v", "type": "REAL"}],
        rows=[{"id": 1, "v": 1.5}, {"id": 2, "v": 2.5}],
    )
    # 主流程不受影响：commit 成功、登记行 ok、同步历史 ok
    assert result["commit"].status_code == 200, result["commit"].text
    detail = quality_env.client.get(
        f"/api/v1/datasets/{dataset['id']}", headers=CUST_HEADERS["cust-001"]
    ).json()
    assert detail["sync_status"] == "ok"
    assert detail["row_count"] == 2
    syncs = quality_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/syncs", headers=CUST_HEADERS["cust-001"]
    ).json()
    assert [s["status"] for s in syncs] == ["ok"]

    # 失败如实留档：failed + error_text，不用旧值冒充
    rows = _snapshots(dataset["id"])
    assert [r.status for r in rows] == ["failed"]
    assert "内部协程崩溃" in (rows[0].error_text or "")
    assert rows[0].row_count is None


def test_derived_materialization_triggers_snapshot(quality_env) -> None:
    """派生重算成功（runner._run_derived_refresh）→ materialize 触发快照。"""
    from fd_wire_api.admin.db import get_session as platform_session
    from fd_wire_api.billing.models import Customer as BillingCustomer
    from fd_wire_api.materialization import runner as runner_mod
    from fd_wire_api.materialization.db import get_session as get_mat_session
    from fd_wire_api.materialization.models import MaterializedArtifact
    from fd_wire_api.materialization.scheduler import default_runner_factory

    dataset = make_uploaded_dataset(quality_env)
    # 配额守卫按 sub2api_user_id 解析（生产口径=客户名；测试种子对齐）
    with platform_session() as sess:
        bcust = sess.execute(
            select(BillingCustomer).where(BillingCustomer.name == "cust-001")
        ).scalar_one()
        bcust.sub2api_user_id = "cust-001"
        sess.commit()

    mat = get_mat_session()
    try:
        row = MaterializedArtifact(
            artifact_id=None,
            customer_id="cust-001",
            kind="derived_dataset",
            ref_key=dataset["id"],
            spec={},
            enabled=True,
        )
        mat.add(row)
        mat.commit()
        mat.refresh(row)

        runner_mod._engine_factory_override = lambda cid: quality_env.fake
        runner = default_runner_factory()
        try:
            result = runner.run_artifact_row(row, now=datetime.now(timezone.utc))
        finally:
            runner.close()
            runner.release_db()
            runner_mod._engine_factory_override = None
        assert result.status == "success", result.error
    finally:
        mat.close()

    rows = _snapshots(dataset["id"])
    assert [r.triggered_by for r in rows] == ["create", "materialize"]
    assert rows[-1].status == "ok"


def test_free_tier_api_dataset_hooks_skipped(quality_env) -> None:
    """api 层数据集（免费档轻量行存储）不落伪造快照（引擎侧无其数据表）。"""
    session = get_session()
    try:
        dataset = Dataset(
            customer_id="cust-001",
            key="api_tier_set",
            name="Api Tier",
            source_kind="upload",
            storage_tier="api",
            columns_json=[],
            row_count=1,
            size_bytes=10,
            sync_status="ok",
        )
        session.add(dataset)
        session.commit()
        dataset_id = dataset.id
    finally:
        session.close()

    from fd_wire_api.quality import service as quality_service

    quality_service.notify_dataset_changed(
        _detached_dataset(dataset_id), "sync", client=quality_env.fake
    )
    assert _snapshots(dataset_id) == []
    # 手动刷新走档位闸（引擎侧无数据，平台直读被红线禁止——如实 403）
    resp = quality_env.client.post(
        f"/api/v1/datasets/{dataset_id}/quality/refresh",
        headers=CUST_HEADERS["cust-001"],
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLAN_TIER_REQUIRED"


def _detached_dataset(dataset_id: str) -> Dataset:
    session = get_session()
    try:
        dataset = session.get(Dataset, dataset_id)
        session.expunge(dataset)
        return dataset
    finally:
        session.close()


# ------------------------------------------------- 历史趋势与保留期（4.5）


def test_history_ordered_and_pruned_to_30(quality_env) -> None:
    """连续多次快照 → 序列有序；保留期裁剪每数据集留 30 条。"""
    from fd_wire_api.quality.settings import get_settings as get_quality_settings

    assert get_quality_settings().quality_history_retention == 30

    dataset = make_uploaded_dataset(quality_env)
    total = 32
    for _ in range(total - 1):  # create 钩子已落 1 条
        resp = quality_env.client.post(
            f"/api/v1/datasets/{dataset['id']}/quality/refresh",
            headers=CUST_HEADERS["cust-001"],
        )
        assert resp.status_code == 200, resp.text

    rows = _snapshots(dataset["id"])
    assert len(rows) == 30  # 裁剪掉最旧 2 条（含 create 那条）
    ids = [r.id for r in rows]
    assert ids == sorted(ids)
    assert all(r.triggered_by == "manual" for r in rows)  # 最旧的 create 行已被裁剪

    detail = quality_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/quality", headers=CUST_HEADERS["cust-001"]
    ).json()
    history = detail["history"]
    assert len(history) == 30
    assert [h["id"] for h in history] == sorted(ids, reverse=True)  # 新→旧
    assert detail["latest"]["id"] == ids[-1]
    # 趋势面字段在场（行数与空值率）
    assert all("row_count" in h and "null_rates" in h for h in history)


# ------------------------------------------------- 新鲜度判定（4.6，D4）


def test_freshness_stale_for_daily_cadence_old_snapshot(quality_env) -> None:
    """声明日更 + 最新 ok 快照 4 天前 → 「过期」并给最近刷新时间。"""
    dataset = make_uploaded_dataset(quality_env)
    _seed_cadence(dataset["id"], "daily")
    _insert_snapshot(  # 更高 id 的旧时间戳 ok 快照 = 最近一次成功刷新
        dataset["id"], computed_at=datetime.now(timezone.utc) - timedelta(days=4)
    )

    summary = quality_env.client.get(
        f"/api/v1/quality/summary?dataset_ids={dataset['id']}",
        headers=CUST_HEADERS["cust-001"],
    )
    assert summary.status_code == 200, summary.text
    entry = summary.json()["statuses"][dataset["id"]]
    assert entry["freshness"] == "stale"
    assert entry["last_refresh_at"] is not None
    assert entry["status"] == "ok"
    assert "指示性检查" in summary.json()["note"]

    detail = quality_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/quality", headers=CUST_HEADERS["cust-001"]
    ).json()
    assert detail["freshness"] == "stale"
    assert detail["cadence_hint"] == "daily"


def test_freshness_unknown_without_cadence(quality_env) -> None:
    """未声明节奏（无 asset 行）→ 「未知」，绝不判「过期」。"""
    dataset = make_uploaded_dataset(quality_env)
    _insert_snapshot(
        dataset["id"], computed_at=datetime.now(timezone.utc) - timedelta(days=30)
    )
    entry = quality_env.client.get(
        f"/api/v1/quality/summary?dataset_ids={dataset['id']}",
        headers=CUST_HEADERS["cust-001"],
    ).json()["statuses"][dataset["id"]]
    assert entry["freshness"] == "unknown"


def test_freshness_fresh_within_cadence(quality_env) -> None:
    """声明日更 + 刚刷新 → 「正常」。"""
    dataset = make_uploaded_dataset(quality_env)  # create 钩子刚落 ok 快照
    _seed_cadence(dataset["id"], "daily")
    entry = quality_env.client.get(
        f"/api/v1/quality/summary?dataset_ids={dataset['id']}",
        headers=CUST_HEADERS["cust-001"],
    ).json()["statuses"][dataset["id"]]
    assert entry["freshness"] == "fresh"
    assert entry["row_count"] == 120


def test_freshness_statuses_contract_importable(quality_env) -> None:
    """跨切片契约：freshness_statuses(customer_id, dataset_ids) 独立可用。"""
    from fd_wire_api.quality import service as quality_service

    dataset = make_uploaded_dataset(quality_env)
    _seed_cadence(dataset["id"], "weekly")
    statuses = quality_service.freshness_statuses(
        "cust-001", [dataset["id"], "not-a-dataset", "00000000-0000-0000-0000-000000000000"]
    )
    assert statuses[dataset["id"]] == "fresh"
    assert statuses["not-a-dataset"] == "unknown"
    assert set(statuses.values()) <= {"fresh", "stale", "unknown"}


# ------------------------------------------------- 鉴权与边界


def test_unauthenticated_401_on_all_three_endpoints(quality_env) -> None:
    """未鉴权：summary / 详情 / 手动刷新一律 401。"""
    dataset = make_uploaded_dataset(quality_env)
    assert (
        quality_env.client.get(
            f"/api/v1/quality/summary?dataset_ids={dataset['id']}"
        ).status_code
        == 401
    )
    assert (
        quality_env.client.get(f"/api/v1/datasets/{dataset['id']}/quality").status_code
        == 401
    )
    assert (
        quality_env.client.post(
            f"/api/v1/datasets/{dataset['id']}/quality/refresh"
        ).status_code
        == 401
    )


def test_viewer_cannot_manual_refresh_but_can_read(quality_env) -> None:
    """viewer 只读：手动刷新 403（统一角色门禁），读端点 200。"""
    dataset = make_uploaded_dataset(quality_env)
    blocked = quality_env.client.post(
        f"/api/v1/datasets/{dataset['id']}/quality/refresh", headers=VIEWER
    )
    assert blocked.status_code == 403
    assert "viewer" in blocked.json()["detail"]
    assert (
        quality_env.client.get(
            f"/api/v1/datasets/{dataset['id']}/quality", headers=VIEWER
        ).status_code
        == 200
    )
    assert (
        quality_env.client.get(
            f"/api/v1/quality/summary?dataset_ids={dataset['id']}", headers=VIEWER
        ).status_code
        == 200
    )
    # viewer 被拒后引擎未被调用、无快照写入
    assert quality_env.fake.calls_for(TOOL_QUALITY)[-1]  # 既有 create 触发仍在
    assert [r.triggered_by for r in _snapshots(dataset["id"])] == ["create"]


def test_cross_customer_404_and_summary_unknown(quality_env) -> None:
    """他企数据集：详情/手动刷新 404（不泄露存在性）；summary 对未知 id 给 unknown。"""
    dataset = make_uploaded_dataset(quality_env)
    assert (
        quality_env.client.get(
            f"/api/v1/datasets/{dataset['id']}/quality", headers=CUSTOMER_B
        ).status_code
        == 404
    )
    assert (
        quality_env.client.post(
            f"/api/v1/datasets/{dataset['id']}/quality/refresh", headers=CUSTOMER_B
        ).status_code
        == 404
    )
    body = quality_env.client.get(
        f"/api/v1/quality/summary?dataset_ids={dataset['id']}",
        headers=CUST_HEADERS["cust-002"],
    ).json()
    assert body["statuses"][dataset["id"]] == {
        "freshness": "unknown",
        "last_refresh_at": None,
        "row_count": None,
        "status": None,
    }


def test_summary_requires_dataset_ids(quality_env) -> None:
    """缺 dataset_ids 参数 → 422（缺参如实拒）。"""
    assert (
        quality_env.client.get(
            "/api/v1/quality/summary", headers=CUST_HEADERS["cust-001"]
        ).status_code
        == 422
    )
