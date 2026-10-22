"""api 层本地修正层（wire-dataset-form-editing 任务 1.1–1.4 api 侧）。

覆盖（镜像引擎覆盖层语义）：
- 三操作端点走通：update 改值 / delete 删行 / insert 加行（合成行
  ``__row_id="c{correction_id}"``、值按 dtype 归一）、row_count 同步、
  基行永不改写（云端不破坏原始数据）；
- 回滚（仅置 reverted_at）+ 二次回滚 409 + 不存在 404；
- 悬空修正（锚行不在基表）允许落层并在清单如实标注 dangling；
- 列校验 400（update/insert 未知列、载荷缺件）且零落行；
- 行数闸：insert 使有效行数超免费档单集上限 → 429 quota（零部分执行）；
- 有效视图三口径一致：preview vs query vs aggregate（同一过滤条件同结果）；
- 他企隔离（修正只查本人 dataset_id）；
- 全链路零引擎调用（FakeEngine 断言）。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from sqlalchemy import select

from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.models import DatasetCorrection, DatasetRow
from test_datasets_common import (
    CUST_HEADERS,
    datasets_env,  # noqa: F401 - pytest fixture（free_env 的依赖链）
    patch_dataset_settings,
)
from test_datasets_free_tier import (
    CSV_SAMPLE,
    FREE,
    _aggregate,
    _query,
    free_env,
    make_free_dataset,
)

#: fixture 注册标记（pytest 按模块属性名收集；绑定引用使其成为已使用导入）
FREE_ENV_FIXTURE = free_env


def _post_correction(env, dataset_id: str, payload: dict, *, customer: str = FREE):
    return env.client.post(
        f"/api/v1/datasets/{dataset_id}/corrections",
        headers=CUST_HEADERS[customer],
        json=payload,
    )


def _list_corrections(env, dataset_id: str, *, customer: str = FREE, **params):
    return env.client.get(
        f"/api/v1/datasets/{dataset_id}/corrections",
        headers=CUST_HEADERS[customer],
        params=params,
    )


def _revert(env, dataset_id: str, correction_id: int, *, customer: str = FREE):
    return env.client.post(
        f"/api/v1/datasets/{dataset_id}/corrections/{correction_id}/revert",
        headers=CUST_HEADERS[customer],
    )


def _preview(env, dataset_id: str, *, page: int = 1, page_size: int = 200, customer: str = FREE):
    return env.client.get(
        f"/api/v1/datasets/{dataset_id}/rows",
        headers=CUST_HEADERS[customer],
        params={"page": page, "page_size": page_size},
    )


def _stored_corrections(dataset_id: str) -> list[DatasetCorrection]:
    session = get_session()
    try:
        return list(
            session.execute(
                select(DatasetCorrection)
                .where(DatasetCorrection.dataset_id == dataset_id)
                .order_by(DatasetCorrection.id)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


# ------------------------------------------------------------------ 三操作 + 回滚


def test_three_ops_revert_and_row_count_sync(free_env) -> None:
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]
    assert _preview(free_env, dataset_id).json()["total"] == 4

    update = _post_correction(
        free_env, dataset_id, {"op": "update", "row_id": "1", "column": "region", "value": "south"}
    )
    assert update.status_code == 201, update.text
    update_id = update.json()["correction_id"]
    delete = _post_correction(free_env, dataset_id, {"op": "delete", "row_id": "2"})
    assert delete.status_code == 201, delete.text
    delete_id = delete.json()["correction_id"]
    insert = _post_correction(
        free_env,
        dataset_id,
        {"op": "insert", "values": {"region": "new", "amount": 1.5, "qty": "9", "note": "n"}},
    )
    assert insert.status_code == 201, insert.text
    insert_id = insert.json()["correction_id"]

    # 有效视图：基行 - delete + insert，update 替换单元格（其余列不动）
    rows = _preview(free_env, dataset_id).json()
    assert rows["total"] == 4  # 4 基行 - 1 删除 + 1 插入
    by_id = {row["__row_id"]: row for row in rows["rows"]}
    assert by_id["0"]["region"] == "east"
    assert by_id["1"]["region"] == "south" and by_id["1"]["qty"] == 2  # 同行其余列保留
    assert by_id["3"]["region"] == "north"
    assert "2" not in by_id  # 删除行剔除
    inserted = by_id[f"c{insert_id}"]  # 插入行合成锚 c{id}
    assert inserted == {"region": "new", "amount": 1.5, "qty": 9, "note": "n", "__row_id": f"c{insert_id}"}
    assert inserted["qty"] == 9  # 字符串 "9" 已按 INTEGER 列归一

    # 基行永不改写：dataset_rows 原值原样
    session = get_session()
    try:
        base = session.execute(
            select(DatasetRow).where(DatasetRow.dataset_id == dataset_id, DatasetRow.row_index == 1)
        ).scalar_one()
        assert base.data["region"] == "west"
    finally:
        session.close()

    # row_count 同步 = 有效行数
    detail = free_env.client.get(f"/api/v1/datasets/{dataset_id}", headers=CUST_HEADERS[FREE]).json()
    assert detail["row_count"] == 4

    # 清单形态（镜像 web Correction 类型；insert row_id=null、update 带列与值、delete 只带锚）
    body = _list_corrections(free_env, dataset_id).json()
    items = {item["id"]: item for item in body["corrections"]}
    assert set(items) == {update_id, delete_id, insert_id}
    upd = items[update_id]
    assert upd["op"] == "update" and upd["row_id"] == "1" and upd["column"] == "region"
    assert upd["value"] == "south" and upd["values"] is None
    assert upd["actor"] == "customer:cust-001" and upd["channel"] == "console"
    assert upd["reverted_at"] is None and upd["dangling_at"] is None and upd["dangling_reason"] is None
    assert upd["created_at"] is not None
    dele = items[delete_id]
    assert dele["op"] == "delete" and dele["row_id"] == "2" and dele["column"] is None
    ins = items[insert_id]
    assert ins["op"] == "insert" and ins["row_id"] is None and ins["values"] == {
        "region": "new", "amount": 1.5, "qty": 9, "note": "n"
    }

    # 回滚 delete：仅置 reverted_at（留痕），有效视图恢复该行
    assert _revert(free_env, dataset_id, delete_id).json() == {"reverted": True}
    assert _preview(free_env, dataset_id).json()["total"] == 5
    detail = free_env.client.get(f"/api/v1/datasets/{dataset_id}", headers=CUST_HEADERS[FREE]).json()
    assert detail["row_count"] == 5
    body = _list_corrections(free_env, dataset_id).json()
    reverted = {item["id"]: item for item in body["corrections"]}[delete_id]
    assert reverted["reverted_at"] is not None
    active_only = _list_corrections(free_env, dataset_id, include_reverted="false").json()
    assert {item["id"] for item in active_only["corrections"]} == {update_id, insert_id}

    # 二次回滚拒绝（409）；不存在 404
    assert _revert(free_env, dataset_id, delete_id).status_code == 409
    assert _revert(free_env, dataset_id, 99999).status_code == 404

    # 回滚 insert：插入行从有效视图消失（基行不受影响）
    assert _revert(free_env, dataset_id, insert_id).status_code == 200
    assert _preview(free_env, dataset_id).json()["total"] == 4
    assert f"c{insert_id}" not in {
        row["__row_id"] for row in _preview(free_env, dataset_id).json()["rows"]
    }

    assert free_env.fake.calls == []  # 全链路零引擎调用


def test_update_null_value_and_update_on_inserted_row(free_env) -> None:
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]
    # 显式 null：单元格置空（与上传「空串→缺失值」口径一致）
    assert _post_correction(
        free_env, dataset_id, {"op": "update", "row_id": "0", "column": "note", "value": None}
    ).status_code == 201
    insert = _post_correction(
        free_env, dataset_id, {"op": "insert", "values": {"region": "east", "qty": 7}}
    ).json()
    anchor = f"c{insert['correction_id']}"
    # 插入行可再改值/再删除（锚 = 合成 __row_id）
    assert _post_correction(
        free_env, dataset_id, {"op": "update", "row_id": anchor, "column": "qty", "value": 8}
    ).status_code == 201
    rows = _preview(free_env, dataset_id).json()
    by_id = {row["__row_id"]: row for row in rows["rows"]}
    assert by_id[anchor]["qty"] == 8
    assert by_id["0"]["note"] is None
    assert _post_correction(free_env, dataset_id, {"op": "delete", "row_id": anchor}).status_code == 201
    rows = _preview(free_env, dataset_id).json()
    assert anchor not in {row["__row_id"] for row in rows["rows"]}
    assert rows["total"] == 4
    # 锚行可解析（插入行存活期间）→ 不标 dangling
    items = _list_corrections(free_env, dataset_id).json()["corrections"]
    assert all(item["dangling_at"] is None for item in items)
    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 悬空落层与标注


def test_dangling_corrections_land_and_flagged(free_env) -> None:
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]
    # 锚行不在基表：允许落层（与引擎悬空语义一致），清单如实标注
    assert _post_correction(
        free_env, dataset_id, {"op": "update", "row_id": "999", "column": "region", "value": "x"}
    ).status_code == 201
    assert _post_correction(free_env, dataset_id, {"op": "delete", "row_id": "888"}).status_code == 201
    assert _preview(free_env, dataset_id).json()["total"] == 4  # 有效视图不受悬空影响

    items = _list_corrections(free_env, dataset_id).json()["corrections"]
    assert len(items) == 2
    assert all(item["dangling_at"] is not None for item in items)
    assert all(item["dangling_reason"] == "锚行不在基表" for item in items)
    filtered = _list_corrections(free_env, dataset_id, include_dangling="false").json()
    assert filtered["corrections"] == []
    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 列校验 400


def test_unknown_column_and_bad_payloads_400(free_env) -> None:
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]
    cases = [
        ({"op": "update", "row_id": "0", "column": "ghost", "value": 1}, "列不存在: ghost"),
        ({"op": "insert", "values": {"region": "east", "ghost": 1}}, "列不存在: ghost"),
        ({"op": "insert"}, "insert 必须带 values"),
        ({"op": "insert", "values": {}}, "insert 必须带 values"),
        ({"op": "update", "row_id": "0", "column": "region"}, "update 必须带 value"),
        ({"op": "update", "column": "region", "value": 1}, "update 必须带 row_id 与 column"),
        ({"op": "delete"}, "delete 必须带 row_id"),
    ]
    for payload, message in cases:
        resp = _post_correction(free_env, dataset_id, payload)
        assert resp.status_code == 400, payload
        assert message in resp.json()["detail"], payload
    assert _stored_corrections(dataset_id) == []  # 校验失败零落行
    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 行数闸


def test_insert_row_gate_rejects_over_limit(free_env, monkeypatch) -> None:
    patch_dataset_settings(monkeypatch, free_tier_max_rows=4)  # 数据集已满 4 行
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]

    rejected = _post_correction(
        free_env, dataset_id, {"op": "insert", "values": {"region": "east", "qty": 9}}
    )
    assert rejected.status_code == 429
    detail = rejected.json()["detail"]
    assert detail.startswith("quota:") and "上限 4" in detail and "升级" in detail
    assert _stored_corrections(dataset_id) == []  # 零部分执行
    assert _preview(free_env, dataset_id).json()["total"] == 4

    # 删除一行腾出余量后 insert 可过（有效行数口径：4 - 1 + 1 = 4）
    assert _post_correction(free_env, dataset_id, {"op": "delete", "row_id": "0"}).status_code == 201
    ok = _post_correction(free_env, dataset_id, {"op": "insert", "values": {"region": "east", "qty": 9}})
    assert ok.status_code == 201, ok.text
    assert _preview(free_env, dataset_id).json()["total"] == 4
    detail = free_env.client.get(f"/api/v1/datasets/{dataset_id}", headers=CUST_HEADERS[FREE]).json()
    assert detail["row_count"] == 4
    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 有效视图三口径一致


def test_effective_view_consistent_preview_query_aggregate(free_env) -> None:
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]
    # 修正组合：改值 + 删行 + 加行（修正后 query/aggregate 切 Python 合并路径）
    assert _post_correction(
        free_env, dataset_id, {"op": "update", "row_id": "0", "column": "region", "value": "north"}
    ).status_code == 201
    assert _post_correction(free_env, dataset_id, {"op": "delete", "row_id": "3"}).status_code == 201
    assert _post_correction(
        free_env, dataset_id,
        {"op": "insert", "values": {"region": "east", "amount": 7.5, "qty": 9, "note": "x"}},
    ).status_code == 201

    preview_rows = _preview(free_env, dataset_id).json()["rows"]

    # 同一过滤条件：query 与 preview 推导同结果
    filtered = _query(free_env, dataset_id, {
        "filters": [{"column": "region", "op": "eq", "value": "north"}],
        "sort": [{"column": "qty", "dir": "desc"}],
    }).json()
    expected = sorted(
        (row for row in preview_rows if row["region"] == "north"),
        key=lambda row: row["qty"],
        reverse=True,
    )
    assert filtered["rows"] == expected
    assert filtered["total"] == len(expected)

    # count/distinct 同口径
    assert _query(free_env, dataset_id, {
        "count_only": True,
        "filters": [{"column": "region", "op": "eq", "value": "north"}],
    }).json()["total"] == len(expected)
    distinct = _query(free_env, dataset_id, {"distinct_column": "region"}).json()
    assert distinct["values"] == sorted({row["region"] for row in preview_rows})

    # 聚合同口径：组数与各组行数由 preview 推导
    aggregate = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [{"fn": "count"}, {"fn": "sum", "column": "qty"}],
    }).json()
    buckets: dict[str, list[dict]] = {}
    for row in preview_rows:
        buckets.setdefault(row["region"], []).append(row)
    assert aggregate["group_count"] == len(buckets)
    assert {g["region"]: g["count"] for g in aggregate["groups"]} == {
        region: len(rows) for region, rows in buckets.items()
    }
    assert {g["region"]: g["qty_sum"] for g in aggregate["groups"]} == {
        region: sum(row["qty"] for row in rows) for region, rows in buckets.items()
    }
    # 带过滤的聚合（过滤后单组）
    with_filter = _aggregate(free_env, dataset_id, {
        "group_by": ["region"],
        "measures": [{"fn": "count"}],
        "filters": [{"column": "region", "op": "eq", "value": "north"}],
    }).json()
    assert with_filter["groups"] == [{"region": "north", "count": len(expected)}]

    # wdk- 数据面 key 轨道同一本地合并路径（同一过滤条件同结果）
    key_resp = free_env.client.post(
        "/api/v1/data-plane-keys",
        headers=CUST_HEADERS[FREE],
        json={"label": "edit-app", "dataset_ids": []},
    )
    assert key_resp.status_code == 201, key_resp.text
    by_key = free_env.client.post(
        f"/api/v1/datasets/{dataset_id}/query",
        headers={"Authorization": f"Bearer {key_resp.json()['key']}"},
        json={
            "filters": [{"column": "region", "op": "eq", "value": "north"}],
            "sort": [{"column": "qty", "dir": "desc"}],
        },
    )
    assert by_key.status_code == 200, by_key.text
    assert by_key.json()["rows"] == expected
    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 数据集删除清理


def test_delete_dataset_purges_local_corrections(free_env) -> None:
    dataset = make_free_dataset(free_env)
    dataset_id = dataset["id"]
    assert _post_correction(
        free_env, dataset_id, {"op": "update", "row_id": "0", "column": "region", "value": "south"}
    ).status_code == 201
    assert len(_stored_corrections(dataset_id)) == 1
    deleted = free_env.client.delete(
        f"/api/v1/datasets/{dataset_id}", headers=CUST_HEADERS[FREE]
    )
    assert deleted.status_code == 200
    assert _stored_corrections(dataset_id) == []  # 覆盖层随数据集清理，不留孤儿行
    assert free_env.fake.calls == []


# ------------------------------------------------------------------ 他企隔离


def test_cross_customer_isolation(free_env) -> None:
    # cust-002 也切免费档：两个 api 层数据集各持各的修正
    from fd_wire_api.admin.db import get_session as platform_session
    from fd_wire_api.billing.models import Customer, Plan

    with platform_session() as sess:
        free = sess.execute(select(Plan).where(Plan.code == "free")).scalar_one()
        other = sess.execute(
            select(Customer).where(Customer.name == "cust-002")
        ).scalar_one()
        other.plan_id = free.id
        sess.commit()

    mine = make_free_dataset(free_env, name="Mine")
    mine_id = mine["id"]
    other_upload = free_env.client.post(
        "/api/v1/datasets/uploads",
        headers=CUST_HEADERS["cust-002"],
        files={"file": ("data.csv", CSV_SAMPLE.encode(), "text/csv")},
    )
    other_created = free_env.client.post(
        "/api/v1/datasets",
        headers=CUST_HEADERS["cust-002"],
        json={"name": "Theirs", "source": {"kind": "upload", "upload_id": other_upload.json()["upload_id"]}},
    )
    assert other_created.status_code == 201, other_created.text
    other_id = other_created.json()["id"]

    mine_update = _post_correction(
        free_env, mine_id, {"op": "update", "row_id": "0", "column": "region", "value": "south"}
    )
    assert mine_update.status_code == 201
    other_update = _post_correction(
        free_env, other_id, {"op": "update", "row_id": "0", "column": "region", "value": "north"},
        customer="cust-002",
    )
    assert other_update.status_code == 201

    # 跨企业访问一律 404（list/add/revert）
    assert _list_corrections(free_env, mine_id, customer="cust-002").status_code == 404
    assert _post_correction(
        free_env, mine_id, {"op": "delete", "row_id": "0"}, customer="cust-002"
    ).status_code == 404
    mine_correction = mine_update.json()["correction_id"]
    assert _revert(free_env, mine_id, mine_correction, customer="cust-002").status_code == 404

    # 各自清单只含本人修正
    mine_list = _list_corrections(free_env, mine_id).json()["corrections"]
    assert [item["id"] for item in mine_list] == [mine_correction]
    other_list = _list_corrections(free_env, other_id, customer="cust-002").json()["corrections"]
    assert [item["id"] for item in other_list] == [other_update.json()["correction_id"]]
    # 同库不同数据集的修正互不可见（dataset_id 归属过滤）
    assert _stored_corrections(mine_id)[0].dataset_id == mine_id
    assert free_env.fake.calls == []
