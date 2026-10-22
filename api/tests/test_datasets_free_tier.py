"""免费档 api 层数据集（wire-free-tier-data-gate 任务组 1–3、5.1/5.2/5.3 api 侧）。

覆盖：
- tier 分流：免费档上传建档落 ``storage_tier=api``，全链路**零引擎调用**
  （FakeEngine 断言）；付费档（basic）回归 cell 层零变化；
- 两闸：建集闸（默认 3 集超限 429 含余量；删除后名额恢复）、写入闸
  （行数超限 429 含上限与实际行数；上传件不消费、无半写状态）；
- 配额端点（§7 预约定）：免费档真实数字 / 付费档 max=null；
- 数据面本地执行：query（eq/in/gt/gte/lt/lte/contains/startswith、排序、
  分页、count、distinct）与 aggregate（group by ≤3 × count/sum/avg/min/max）；
  免费窄面不支持的算子/非数值聚合明确 400；
- 档位闸：连接器/声明式建集、derive/join/batchWrite/加列 → 403
  ``PLAN_TIER_REQUIRED``；lineage/修正清单最小空呈现（修正三件套对 api
  层已放行本地实现——wire-dataset-form-editing，见 test_local_corrections）；
- 计量口径（D5/D7）：查询/聚合不产生 metering_records；
- 双方言冒烟（5.3）：PG 方言编译断言（``->>`` 取值、数值 CAST、JSONB DDL）；
  SQLite 真跑即既有用例本体。

免费客户构造：datasets_env 种子为 basic（cell 链路回归基线），本文件在
用例内把 cust-001 切到 free 档（tier 按建集时刻 plan 判定，design D1）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.schema import CreateTable

from fd_wire_api.datasets import api_store, service
from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.models import DatasetColumn, DatasetRow
from fd_wire_api.datasets.settings import DatasetsSettings
from test_datasets_common import (
    CUST_HEADERS,
    create_upload_dataset,
    datasets_env,  # noqa: F401 - pytest fixture
    patch_dataset_settings,
    upload_csv,
)

CSV_SAMPLE = (
    "region,amount,qty,note\n"
    "east,10.5,1,hello\n"
    "west,20.75,2,\n"
    "east,30,3,world\n"
    "north,5.25,4,hi\n"
)

FREE = "cust-001"  # datasets_env 种子 basic；free_env 内切免费档
PAID = "cust-002"  # basic（cell 链路回归对照）


# ------------------------------------------------------------------ 环境


@pytest.fixture()
def free_env(datasets_env):  # noqa: F811 - 组合共享 fixture
    """cust-001 → 免费档（建集 tier 判定的输入；datasets_env 本底 basic）。"""
    from fd_wire_api.admin.db import get_session as platform_session
    from fd_wire_api.billing.models import Customer, Plan

    with platform_session() as sess:
        free = sess.execute(select(Plan).where(Plan.code == "free")).scalar_one()
        cust = sess.execute(
            select(Customer).where(Customer.name == FREE)
        ).scalar_one()
        cust.plan_id = free.id
        sess.commit()
    datasets_env.fake.calls.clear()
    return datasets_env


def make_free_dataset(env, *, content: str = CSV_SAMPLE, name: str = "Free Data") -> dict:
    """经上传通道为免费客户建一个 api 层数据集（返回建档响应体）。"""
    resp = upload_csv(env, content, customer=FREE)
    assert resp.status_code == 201, resp.text
    created = create_upload_dataset(env, resp.json()["upload_id"], name=name, customer=FREE)
    assert created.status_code == 201, created.text
    return created.json()


def _query(env, dataset_id: str, body: dict, *, customer: str = FREE):
    return env.client.post(
        f"/api/v1/datasets/{dataset_id}/query",
        headers=CUST_HEADERS[customer],
        json=body,
    )


def _aggregate(env, dataset_id: str, body: dict, *, customer: str = FREE):
    return env.client.post(
        f"/api/v1/datasets/{dataset_id}/aggregate",
        headers=CUST_HEADERS[customer],
        json=body,
    )


# ------------------------------------------------------------------ 1.2 配置


def test_free_settings_defaults_and_env_names(monkeypatch) -> None:
    """默认 3 集 × 10000 行；FD_WIRE_FREE_* env 可覆盖（任务 1.2）。"""
    monkeypatch.delenv("FD_WIRE_FREE_MAX_DATASETS", raising=False)
    monkeypatch.delenv("FD_WIRE_FREE_MAX_ROWS", raising=False)
    defaults = DatasetsSettings()
    assert defaults.free_tier_max_datasets == 3
    assert defaults.free_tier_max_rows == 10000
    monkeypatch.setenv("FD_WIRE_FREE_MAX_DATASETS", "5")
    monkeypatch.setenv("FD_WIRE_FREE_MAX_ROWS", "77")
    overridden = DatasetsSettings()
    assert overridden.free_tier_max_datasets == 5
    assert overridden.free_tier_max_rows == 77


# ------------------------------------------------------------------ 3.1 tier 分流


def test_free_upload_creates_api_tier_dataset_zero_engine_calls(free_env) -> None:
    """免费档：上传→建档→列表/详情/行预览全链路零引擎调用（5.2 断言）。"""
    dataset = make_free_dataset(free_env)
    assert dataset["storage_tier"] == "api"
    assert dataset["row_count"] == 4
    assert dataset["columns"] == [
        {"name": "region", "type": "TEXT"},
        {"name": "amount", "type": "REAL"},
        {"name": "qty", "type": "INTEGER"},
        {"name": "note", "type": "TEXT"},
    ]
    listed = free_env.client.get("/api/v1/datasets", headers=CUST_HEADERS[FREE]).json()
    assert [d["storage_tier"] for d in listed] == ["api"]  # 与 cell 层同列呈现
    detail = free_env.client.get(
        f"/api/v1/datasets/{dataset['id']}", headers=CUST_HEADERS[FREE]
    )
    assert detail.status_code == 200
    assert detail.json()["storage_tier"] == "api"
    # schema 预览（任务 2.4）：详情端点复用 dataset_columns 聚合
    assert detail.json()["row_count"] == 4
    assert detail.json()["columns"] == [
        {"name": "region", "type": "TEXT"},
        {"name": "amount", "type": "REAL"},
        {"name": "qty", "type": "INTEGER"},
        {"name": "note", "type": "TEXT"},
    ]
    rows = free_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/rows",
        headers=CUST_HEADERS[FREE],
        params={"page": 1, "page_size": 2},
    )
    assert rows.status_code == 200, rows.text
    assert rows.json()["total"] == 4
    assert [r["region"] for r in rows.json()["rows"]] == ["east", "west"]
    assert rows.json()["rows"][0]["__row_id"] == "0"
    # 全链路零引擎调用（上传解析/建档/读取都不过 EngineToolClient）
    assert free_env.fake.calls == []


def test_free_rows_written_with_dtype_normalization(free_env) -> None:
    """写入归一（任务 2.1）：INTEGER→int、REAL→float、TEXT→str、空串→None。"""
    dataset = make_free_dataset(free_env)
    session = get_session()
    try:
        columns = session.execute(
            select(DatasetColumn)
            .where(DatasetColumn.dataset_id == dataset["id"])
            .order_by(DatasetColumn.ordinal)
        ).scalars().all()
        assert [(c.name, c.dtype) for c in columns] == [
            ("region", "TEXT"),
            ("amount", "REAL"),
            ("qty", "INTEGER"),
            ("note", "TEXT"),
        ]
        rows = session.execute(
            select(DatasetRow)
            .where(DatasetRow.dataset_id == dataset["id"])
            .order_by(DatasetRow.row_index)
        ).scalars().all()
        assert len(rows) == 4
        assert rows[0].data == {
            "region": "east", "amount": 10.5, "qty": 1, "note": "hello"
        }
        assert rows[0].row_index == 0
        assert rows[1].data["note"] is None  # 空串 → 缺失值
        assert isinstance(rows[0].data["qty"], int)
        assert isinstance(rows[0].data["amount"], float)
    finally:
        session.close()
    assert free_env.fake.calls == []


def test_paid_tier_regression_cell_zero_change(free_env) -> None:
    """付费档（basic）回归（5.2）：tier=cell、引擎三段式调用、行为零变化。"""
    upload = upload_csv(free_env, "id,amount\n1,10.5\n2,20\n", customer=PAID)
    assert upload.status_code == 201
    created = create_upload_dataset(
        free_env, upload.json()["upload_id"], name="Paid Data", customer=PAID
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["storage_tier"] == "cell"
    from fd_wire_api.datasets.ingest import (
        TOOL_INGEST_BEGIN,
        TOOL_INGEST_CHUNK,
        TOOL_INGEST_COMMIT,
    )
    from fd_wire_api.quality.service import TOOL_QUALITY

    # 末位 customer_dataset_quality：cell 档建档成功的质量快照自动钩子（4.3）
    assert free_env.fake.tools == [
        TOOL_INGEST_BEGIN,
        TOOL_INGEST_CHUNK,
        TOOL_INGEST_COMMIT,
        TOOL_QUALITY,
    ]
    begin = free_env.fake.calls_for(TOOL_INGEST_BEGIN)[0]
    assert begin["dataset_key"] == "paid_data"
    # 同列表共存、tier 徽标区分（免费集与付费集）
    listed = free_env.client.get("/api/v1/datasets", headers=CUST_HEADERS[PAID]).json()
    assert [d["storage_tier"] for d in listed] == ["cell"]


# ------------------------------------------------------------------ 3.2 两闸


def test_free_dataset_count_gate_default_three(free_env) -> None:
    """建集闸（任务 3.2）：默认 3 集，第 4 个 429 含余量；删除恢复名额。"""
    ids = []
    for index in range(3):
        dataset = make_free_dataset(free_env, name=f"Free {index + 1}")
        ids.append(dataset["id"])
    quota = free_env.client.get("/api/v1/datasets/quota", headers=CUST_HEADERS[FREE]).json()
    assert quota == {
        "plan_code": "free",
        "used_datasets": 3,
        "max_datasets": 3,
        "max_rows_per_dataset": 10000,
    }
    upload = upload_csv(free_env, "id\n1\n")
    fourth = create_upload_dataset(free_env, upload.json()["upload_id"], name="Fourth")
    assert fourth.status_code == 429
    assert "已用 3/3" in fourth.json()["detail"]
    assert "升级" in fourth.json()["detail"]
    # 上传件未消费（名额释放后可再建）
    session = get_session()
    try:
        from fd_wire_api.datasets.models import Upload

        record = session.get(Upload, upload.json()["upload_id"])
        assert record.consumed_at is None
    finally:
        session.close()
    # 删除（本地、零引擎调用）后名额恢复
    deleted = free_env.client.delete(
        f"/api/v1/datasets/{ids[2]}", headers=CUST_HEADERS[FREE]
    )
    assert deleted.status_code == 200
    retry = create_upload_dataset(free_env, upload.json()["upload_id"], name="Fourth")
    assert retry.status_code == 201, retry.text
    assert free_env.fake.calls == []


def test_free_row_gate_rejects_without_partial_write(free_env, monkeypatch) -> None:
    """写入闸（任务 2.1/3.2）：行数超限 429 含上限与实际行数；无半写状态。"""
    patch_dataset_settings(monkeypatch, free_tier_max_rows=3)
    upload = upload_csv(free_env, CSV_SAMPLE)  # 4 行：上传预览不拒（预览≠写入）
    assert upload.status_code == 201
    rejected = create_upload_dataset(free_env, upload.json()["upload_id"], name="Too Big")
    assert rejected.status_code == 429
    detail = rejected.json()["detail"]
    assert "上限 3" in detail and "4" in detail
    session = get_session()
    try:
        from fd_wire_api.datasets.models import Dataset, Upload

        record = session.get(Upload, upload.json()["upload_id"])
        assert record.consumed_at is None  # 上传件不消费
        assert session.query(DatasetRow).count() == 0  # 无半写行
        assert (
            session.execute(
                select(Dataset).where(Dataset.storage_tier == service.TIER_API)
            ).scalar_one_or_none()
            is None
        )  # 无半建数据集
    finally:
        session.close()
    assert free_env.fake.calls == []


def test_ingest_local_in_flight_row_gate(free_env, monkeypatch, tmp_path) -> None:
    """行闸途中兜底（任务 2.1）：迭代中发现超限同样 fail-closed。"""
    from fd_wire_api.datasets.models import Dataset

    csv_path = tmp_path / "rows.csv"
    csv_path.write_text("id\n1\n2\n3\n", encoding="utf-8")
    patch_dataset_settings(monkeypatch, free_tier_max_rows=2)
    session = get_session()
    try:
        dataset = Dataset(
            customer_id=FREE,
            key="gate_probe",
            name="gate probe",
            source_kind="upload",
            storage_tier=service.TIER_API,
        )
        session.add(dataset)
        session.flush()
        with pytest.raises(service.QuotaExceeded, match="上限 2"):
            api_store.ingest_local(
                session,
                dataset=dataset,
                path=csv_path,
                columns=[{"name": "id", "type": "INTEGER"}],
            )
        session.rollback()
    finally:
        session.close()


# ------------------------------------------------------------------ §7 配额端点


def test_quota_endpoint_free_and_paid(free_env) -> None:
    """配额端点：免费真实数字、付费 max=null；无凭据 401。"""
    anonymous = free_env.client.get("/api/v1/datasets/quota")
    assert anonymous.status_code == 401
    empty = free_env.client.get("/api/v1/datasets/quota", headers=CUST_HEADERS[FREE]).json()
    assert empty == {
        "plan_code": "free",
        "used_datasets": 0,
        "max_datasets": 3,
        "max_rows_per_dataset": 10000,
    }
    make_free_dataset(free_env)
    after = free_env.client.get("/api/v1/datasets/quota", headers=CUST_HEADERS[FREE]).json()
    assert after["used_datasets"] == 1
    paid = free_env.client.get("/api/v1/datasets/quota", headers=CUST_HEADERS[PAID]).json()
    assert paid == {
        "plan_code": "basic",
        "used_datasets": 0,
        "max_datasets": None,
        "max_rows_per_dataset": None,
    }


# ------------------------------------------------------------------ 2.2 查询


def test_free_query_filters_sort_paging_count_distinct(free_env) -> None:
    """窄查询面（任务 2.2）：八算子/排序/分页/count/distinct + 明确 400。"""
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]

    plain = _query(free_env, dataset_id, {"limit": 10}).json()
    assert plain["total"] == 4
    assert [r["qty"] for r in plain["rows"]] == [1, 2, 3, 4]
    assert all("__row_id" in r for r in plain["rows"])
    assert plain["columns"][0] == {"name": "region", "type": "TEXT"}

    eq = _query(free_env, dataset_id, {
        "filters": [{"column": "region", "op": "eq", "value": "east"}]
    }).json()
    assert eq["total"] == 2

    in_op = _query(free_env, dataset_id, {
        "filters": [{"column": "region", "op": "in", "values": ["east", "west"]}]
    }).json()
    assert in_op["total"] == 3

    gt = _query(free_env, dataset_id, {
        "filters": [{"column": "qty", "op": "gt", "value": 2}]
    }).json()
    assert [r["qty"] for r in gt["rows"]] == [3, 4]
    gte = _query(free_env, dataset_id, {
        "filters": [{"column": "qty", "op": "gte", "value": 3}]
    }).json()
    assert gte["total"] == 2
    lt = _query(free_env, dataset_id, {
        "filters": [{"column": "amount", "op": "lt", "value": 11}]
    }).json()
    assert sorted(r["amount"] for r in lt["rows"]) == [5.25, 10.5]
    lte = _query(free_env, dataset_id, {
        "filters": [{"column": "amount", "op": "lte", "value": 10.5}]
    }).json()
    assert lte["total"] == 2

    contains = _query(free_env, dataset_id, {
        "filters": [{"column": "note", "op": "contains", "value": "l"}]
    }).json()
    assert contains["total"] == 2  # hello / world（hi 与缺失不中）
    startswith = _query(free_env, dataset_id, {
        "filters": [{"column": "note", "op": "startswith", "value": "he"}]
    }).json()
    assert startswith["total"] == 1

    null_eq = _query(free_env, dataset_id, {
        "filters": [{"column": "note", "op": "eq", "value": None}]
    }).json()
    assert null_eq["total"] == 1  # 空串写入为缺失值

    desc = _query(free_env, dataset_id, {
        "sort": [{"column": "qty", "dir": "desc"}]
    }).json()
    assert [r["qty"] for r in desc["rows"]] == [4, 3, 2, 1]

    page = _query(free_env, dataset_id, {"limit": 2, "offset": 1}).json()
    assert page["total"] == 4 and len(page["rows"]) == 2 and page["truncated"] is True

    count_only = _query(free_env, dataset_id, {"count_only": True}).json()
    assert count_only["rows"] == [] and count_only["total"] == 4

    distinct = _query(free_env, dataset_id, {"distinct_column": "region"}).json()
    assert distinct["values"] == ["east", "north", "west"] and distinct["count"] == 3

    # 免费窄面之外的算子/坏参数：明确 400（如实标注差异，不静默降级）
    unsupported = _query(free_env, dataset_id, {
        "filters": [{"column": "region", "op": "ne", "value": "east"}]
    })
    assert unsupported.status_code == 400
    assert "不支持过滤算子" in unsupported.json()["detail"]
    unknown_column = _query(free_env, dataset_id, {
        "filters": [{"column": "ghost", "op": "eq", "value": 1}]
    })
    assert unknown_column.status_code == 400
    assert "不存在的列" in unknown_column.json()["detail"]
    numeric_contains = _query(free_env, dataset_id, {
        "filters": [{"column": "qty", "op": "contains", "value": "1"}]
    })
    assert numeric_contains.status_code == 400

    # console 会话轨道之外：wdk- key 轨道同一本地分支（限流沿用既有 rpm）
    key_resp = free_env.client.post(
        "/api/v1/data-plane-keys",
        headers=CUST_HEADERS[FREE],
        json={"label": "free-app", "dataset_ids": []},
    )
    assert key_resp.status_code == 201, key_resp.text
    by_key = free_env.client.post(
        f"/api/v1/datasets/{dataset_id}/query",
        headers={"Authorization": f"Bearer {key_resp.json()['key']}"},
        json={"filters": [{"column": "region", "op": "eq", "value": "east"}]},
    )
    assert by_key.status_code == 200, by_key.text
    assert by_key.json()["total"] == 2

    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 2.3 聚合


def test_free_aggregate_group_by_and_measures(free_env) -> None:
    """聚合面（任务 2.3）：≤3 列分组 × 五度量；非数值聚合/超宽分组明确 400。"""
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]

    by_region = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [
            {"fn": "count"},
            {"fn": "sum", "column": "amount"},
            {"fn": "avg", "column": "amount"},
            {"fn": "min", "column": "qty"},
            {"fn": "max", "column": "qty"},
        ],
    }).json()
    assert by_region["group_by"] == ["region"]
    groups = {g["region"]: g for g in by_region["groups"]}
    assert groups["east"] == {
        "region": "east", "count": 2, "amount_sum": 40.5,
        "amount_avg": 20.25, "qty_min": 1, "qty_max": 3,
    }
    assert groups["west"]["amount_avg"] == 20.75
    assert by_region["group_count"] == 3 and by_region["truncated"] is False

    two_cols = _aggregate(free_env, dataset_id, {
        "group_by": ["region", "note"],
        "measures": [{"fn": "count"}],
    }).json()
    assert two_cols["group_count"] == 4
    three_cols = _aggregate(free_env, dataset_id, {
        "group_by": ["region", "note", "qty"],
        "measures": [{"fn": "count"}],
    }).json()
    assert three_cols["group_count"] == 4

    with_filter = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [{"fn": "count"}, {"fn": "sum", "column": "qty"}],
        "filters": [{"column": "region", "op": "eq", "value": "east"}],
    }).json()
    assert with_filter["groups"] == [{"region": "east", "count": 2, "qty_sum": 4}]

    empty = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [{"fn": "count"}],
        "filters": [{"column": "region", "op": "eq", "value": "nowhere"}],
    }).json()
    assert empty["groups"] == [] and empty["group_count"] == 0  # 空组如实

    text_min = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [{"fn": "min", "column": "note"}],
    }).json()
    assert {g["region"]: g["note_min"] for g in text_min["groups"]}["west"] is None

    non_numeric = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [{"fn": "sum", "column": "note"}],
    })
    assert non_numeric.status_code == 400
    assert "仅支持数值列" in non_numeric.json()["detail"]

    too_wide = _aggregate(free_env, dataset_id, {
        "group_by": ["region", "note", "qty", "amount"],
        "measures": [{"fn": "count"}],
    })
    assert too_wide.status_code == 400
    assert "上限为 3" in too_wide.json()["detail"]

    bad_fn = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [{"fn": "median", "column": "amount"}],
    })
    assert bad_fn.status_code == 400

    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 3.2/3.3 档位闸与计量


def test_free_tier_gates_return_plan_tier_required(free_env) -> None:
    """付费面档位闸（任务 3.2）：403 + PLAN_TIER_REQUIRED + 升档说明；
    不产生任何引擎调用（不部分执行）。"""
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]
    headers = CUST_HEADERS[FREE]

    def assert_gate(resp):
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "PLAN_TIER_REQUIRED"
        assert "升级" in detail["upgrade"]

    assert_gate(free_env.client.post(
        f"/api/v1/datasets/{dataset_id}/derive", headers=headers,
        json={"name": "Child", "filters": [], "sort": []},
    ))
    assert_gate(free_env.client.post(
        f"/api/v1/datasets/{dataset_id}/join", headers=headers,
        json={
            "name": "Joined",
            "indicators": [{"code": "gdp", "as": "gdp"}],
            "date_column": "note",
            "filters": [],
        },
    ))
    assert_gate(free_env.client.post(
        f"/api/v1/datasets/{dataset_id}/rows:batchWrite", headers=headers,
        json={"ops": [{"op": "insert", "values": {"qty": 9}}]},
    ))
    assert_gate(free_env.client.post(
        f"/api/v1/datasets/{dataset_id}/columns", headers=headers,
        json={"column": {"name": "extra", "type": "TEXT"}},
    ))
    # 修正三件套不再 403：api 层已放行本地修正层
    # （wire-dataset-form-editing，正路径见 test_local_corrections.py）
    assert_gate(free_env.client.post(
        "/api/v1/datasets", headers=headers,
        json={
            "name": "conn",
            "source": {"kind": "connector", "connector_id": "c", "source_name": "s", "source_ref": "r"},
        },
    ))
    assert_gate(free_env.client.post(
        "/api/v1/datasets", headers=headers,
        json={
            "name": "decl",
            "source": {"kind": "declared", "columns": [{"name": "id", "type": "INTEGER"}]},
        },
    ))

    # 读面最小呈现：修正清单（本地修正层，空历史）与 lineage 本地空视图
    corrections = free_env.client.get(
        f"/api/v1/datasets/{dataset_id}/corrections", headers=headers
    )
    assert corrections.status_code == 200
    assert corrections.json() == {"corrections": []}
    lineage = free_env.client.get(
        f"/api/v1/datasets/{dataset_id}/lineage", headers=headers
    )
    assert lineage.status_code == 200
    assert lineage.json() == {"derivation": [], "children": []}

    assert free_env.fake.calls == []


def test_free_reads_do_not_meter(free_env, tmp_path, monkeypatch) -> None:
    """计量口径（任务 3.3 / D5/D7）：查询/聚合不产生 metering_records。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/metering.db")
    from fd_wire_api.metering.db import (
        ensure_schema,
        get_engine as metering_engine,
        reset_metering_runtime,
    )

    reset_metering_runtime()
    ensure_schema()
    try:
        dataset = make_free_dataset(free_env)
        for _ in range(3):
            assert _query(free_env, dataset["id"], {"limit": 2}).status_code == 200
            assert _aggregate(free_env, dataset["id"], {
                "group_by": ["region"], "measures": [{"fn": "count"}]
            }).status_code == 200
        with metering_engine().connect() as conn:
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM metering_records"
            ).scalar()
        assert count == 0
        assert free_env.fake.calls == []  # 引擎零调用 = 计量轨道零入账的前提
    finally:
        reset_metering_runtime()


# ------------------------------------------------------------------ 5.1 E2E


def test_free_path_e2e(free_env, monkeypatch) -> None:
    """免费路径 E2E（任务 5.1）：上传 → 建集 → 预览 → 查询/聚合 → 两闸。"""
    # schema 预览（上传响应即列/类型/样例）
    preview = upload_csv(free_env, CSV_SAMPLE)
    assert preview.status_code == 201, preview.text
    body = preview.json()
    assert body["row_count"] == 4
    assert [c["name"] for c in body["columns"]] == ["region", "amount", "qty", "note"]
    assert body["sample_rows"][0]["qty"] == 1

    created = create_upload_dataset(free_env, body["upload_id"], name="E2E")
    assert created.status_code == 201, created.text
    dataset_id = created.json()["id"]

    rows = free_env.client.get(
        f"/api/v1/datasets/{dataset_id}/rows", headers=CUST_HEADERS[FREE]
    ).json()
    assert rows["total"] == 4

    query = _query(free_env, dataset_id, {
        "filters": [{"column": "region", "op": "eq", "value": "east"}],
        "sort": [{"column": "qty", "dir": "desc"}],
    }).json()
    assert [r["qty"] for r in query["rows"]] == [3, 1]
    aggregate = _aggregate(free_env, dataset_id, {
        "group_by": ["region"], "measures": [{"fn": "count"}, {"fn": "sum", "column": "qty"}]
    }).json()
    assert {g["region"]: g["count"] for g in aggregate["groups"]} == {
        "east": 2, "west": 1, "north": 1,
    }

    # 第 4 个数据集被拒（默认 3 集）
    for index in range(2):
        assert make_free_dataset(free_env, name=f"E2E {index + 2}")["id"]
    fourth_upload = upload_csv(free_env, "id\n1\n")
    fourth = create_upload_dataset(free_env, fourth_upload.json()["upload_id"], name="E2E 4")
    assert fourth.status_code == 429
    assert "已用 3/3" in fourth.json()["detail"]

    # 行数超限被拒（写入闸；先删一集腾出建集名额，使两闸分别可观测）
    freed = free_env.client.delete(
        f"/api/v1/datasets/{dataset_id}", headers=CUST_HEADERS[FREE]
    )
    assert freed.status_code == 200
    patch_dataset_settings(monkeypatch, free_tier_max_rows=1)
    small = upload_csv(free_env, "id\n1\n2\n")
    over = create_upload_dataset(free_env, small.json()["upload_id"], name="E2E rows")
    assert over.status_code == 429
    assert "上限 1" in over.json()["detail"]

    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 5.3 双方言冒烟


def test_pg_dialect_compile_smoke() -> None:
    """双方言冒烟（任务 5.3）：无本地 PG 实例 → SQLAlchemy 方言编译断言。"""
    from sqlalchemy.dialects import postgresql, sqlite

    text_expr = api_store._text_expr("region")
    pg_sql = str(text_expr.compile(dialect=postgresql.dialect()))
    sqlite_sql = str(text_expr.compile(dialect=sqlite.dialect()))
    assert "->>" in pg_sql  # PG：JSONB 文本取值
    assert "JSON_EXTRACT" in sqlite_sql  # SQLite：JSON 取值

    numeric = api_store._numeric_expr("amount") > 1.5
    assert "CAST(" in str(numeric.compile(dialect=postgresql.dialect()))
    assert "CAST(" in str(numeric.compile(dialect=sqlite.dialect()))

    rows_table = DatasetRow.__table__
    pg_ddl = str(CreateTable(rows_table).compile(dialect=postgresql.dialect()))
    sqlite_ddl = str(CreateTable(rows_table).compile(dialect=sqlite.dialect()))
    assert "JSONB" in pg_ddl and "JSON" in sqlite_ddl
    assert "uq_dataset_rows_dataset_index" in pg_ddl
