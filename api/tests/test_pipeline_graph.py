"""管线图端点测试（wire-transform-pipeline-view 任务 1.2）。

覆盖（spec transform-pipeline 四场景 + 契约细节）：
- 派生链成图（A 上传 → B 派生 → C 派生自 B：深度 0/1/2、两边、op=derived）；
- 无派生空图（nodes 含上传、edges 空）；
- 企业隔离（两客户数据集互不可见——读取面隔离）；
- 环防护（B←A 且 A←B 人造环：涉环边跳过 anomalies≥1，端点不 500）；
- 孤儿边（source_ref 指向不存在 key：anomalies+1、节点 depth=0 照常出现）；
- schedule_cron 命中（经 data_plane PUT schedule 真路径登记）/未登记 null；
- truncated（505 行轻量插入：按 updated_at 倒序截 500、最老行丢弃）；
- 401 未登录。

环境复用 test_datasets_common.datasets_env（内存 datasets 库 + 临时
materialization 库 + 假 JWKS 双客户）；图是零引擎端点，FakeEngine 全程
零调用（断言无 customer_dataset_* 工具触达）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient  # noqa: F401 - 类型标注用

from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.models import Dataset as DatasetRow

from test_datasets_common import CUST_HEADERS, datasets_env  # noqa: F401

GRAPH_URL = "/api/v1/pipeline/graph"


def insert_dataset(
    *,
    key: str,
    customer: str = "cust-001",
    name: str | None = None,
    source_kind: str = "upload",
    source_ref: str | None = None,
    updated_at: datetime | None = None,
    row_count: int = 0,
) -> str:
    """直插一行数据集登记（图端点是零引擎纯读，无需走引擎链路建档）。"""
    with get_session() as sess:
        row = DatasetRow(
            customer_id=customer,
            key=key,
            name=name or key,
            source_kind=source_kind,
            source_ref=source_ref,
            storage_tier="cell",
            row_count=row_count,
            **({"updated_at": updated_at} if updated_at else {}),
        )
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id


def fetch_graph(client, customer: str = "cust-001") -> dict:
    resp = client.get(GRAPH_URL, headers=CUST_HEADERS[customer])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 冻结契约形状：顶层四键、节点字段集固定
    assert set(body) == {"nodes", "edges", "anomalies", "truncated"}
    for node in body["nodes"]:
        assert set(node) == {
            "dataset_id",
            "key",
            "name",
            "source_kind",
            "storage_tier",
            "row_count",
            "updated_at",
            "depth",
            "schedule_cron",
        }
    for edge in body["edges"]:
        assert set(edge) == {"from_dataset_id", "to_dataset_id", "op"}
    return body


def nodes_by_key(body: dict) -> dict[str, dict]:
    return {node["key"]: node for node in body["nodes"]}


# ── 派生链成图 ────────────────────────────────────────────────────────


def test_derived_chain_forms_graph(datasets_env) -> None:
    """A 上传 → B 派生(A) → C 派生(B)：深度 0/1/2、两边、op 统一 derived。"""
    a = insert_dataset(key="chain_a", name="源表", row_count=10)
    b = insert_dataset(key="chain_b", name="B", source_kind="derived", source_ref="chain_a", row_count=6)
    c = insert_dataset(key="chain_c", name="C", source_kind="derived", source_ref="chain_b", row_count=3)

    body = fetch_graph(datasets_env.client)
    assert len(body["nodes"]) == 3
    indexed = nodes_by_key(body)
    assert indexed["chain_a"]["depth"] == 0
    assert indexed["chain_b"]["depth"] == 1
    assert indexed["chain_c"]["depth"] == 2
    assert indexed["chain_a"]["dataset_id"] == a
    assert indexed["chain_a"]["row_count"] == 10
    # updated_at 是 ISO 字符串（naive UTC，与 data_plane schedule 响应同口径）
    datetime.fromisoformat(indexed["chain_a"]["updated_at"])

    assert body["anomalies"] == 0
    assert body["truncated"] is False
    assert len(body["edges"]) == 2
    assert {"from_dataset_id": a, "to_dataset_id": b, "op": "derived"} in body["edges"]
    assert {"from_dataset_id": b, "to_dataset_id": c, "op": "derived"} in body["edges"]
    # 零引擎调用：图端点是纯本地元数据组装
    assert datasets_env.fake.calls == []


# ── 无派生空图 ────────────────────────────────────────────────────────


def test_empty_graph_with_uploads_only(datasets_env) -> None:
    """只有上传数据集：nodes 含上传、edges 空、anomalies=0。"""
    insert_dataset(key="only_upload", name="上传表", row_count=7)

    body = fetch_graph(datasets_env.client)
    assert [node["key"] for node in body["nodes"]] == ["only_upload"]
    assert body["nodes"][0]["source_kind"] == "upload"
    assert body["nodes"][0]["depth"] == 0
    assert body["edges"] == []
    assert body["anomalies"] == 0
    assert body["truncated"] is False


# ── 企业隔离 ──────────────────────────────────────────────────────────


def test_enterprise_isolation(datasets_env) -> None:
    """两客户各自数据集互不可见（他企节点与边都不出现）。"""
    insert_dataset(key="mine_a", customer="cust-001")
    insert_dataset(
        key="mine_b", customer="cust-001", source_kind="derived", source_ref="mine_a"
    )
    insert_dataset(key="theirs_x", customer="cust-002")
    insert_dataset(
        key="theirs_y", customer="cust-002", source_kind="derived", source_ref="theirs_x"
    )

    mine = fetch_graph(datasets_env.client, "cust-001")
    mine_keys = set(nodes_by_key(mine))
    assert mine_keys == {"mine_a", "mine_b"}
    assert mine["edges"] == [
        {
            "from_dataset_id": nodes_by_key(mine)["mine_a"]["dataset_id"],
            "to_dataset_id": nodes_by_key(mine)["mine_b"]["dataset_id"],
            "op": "derived",
        }
    ]

    theirs = fetch_graph(datasets_env.client, "cust-002")
    assert set(nodes_by_key(theirs)) == {"theirs_x", "theirs_y"}
    # 同名 key 也不串库：cust-002 图里没有 cust-001 的 key
    assert not (mine_keys & set(nodes_by_key(theirs)))


# ── 环防护 ────────────────────────────────────────────────────────────


def test_cycle_guard_no_500(datasets_env) -> None:
    """人造环（B.source_ref=A.key 且 A.source_ref=B.key）：涉环边跳过、
    anomalies 如实计数、端点不报错，两节点照常出现（深度按源层 0 兜底）。"""
    insert_dataset(key="cyc_a", source_kind="derived", source_ref="cyc_b")
    insert_dataset(key="cyc_b", source_kind="derived", source_ref="cyc_a")

    body = fetch_graph(datasets_env.client)
    assert len(body["nodes"]) == 2
    assert all(node["depth"] == 0 for node in body["nodes"])
    assert body["edges"] == []  # 两条涉环边全部跳过
    assert body["anomalies"] >= 1
    assert body["anomalies"] == 2  # 两条边各计一次（环成员两条入边都在环上）


# ── 孤儿边 ────────────────────────────────────────────────────────────


def test_orphan_source_ref_counts_anomaly(datasets_env) -> None:
    """source_ref 指向不存在的 key：孤儿派生行按源处理 depth=0 照常出现，
    anomalies+1、无边。"""
    insert_dataset(key="root_ok", name="正常源")
    orphan_id = insert_dataset(
        key="orphan_child", source_kind="derived", source_ref="no_such_parent"
    )

    body = fetch_graph(datasets_env.client)
    indexed = nodes_by_key(body)
    assert indexed["orphan_child"]["dataset_id"] == orphan_id
    assert indexed["orphan_child"]["depth"] == 0
    assert indexed["orphan_child"]["source_kind"] == "derived"
    assert body["edges"] == []  # 父 key 无对应行 → 孤儿边跳过
    assert body["anomalies"] == 1


# ── schedule_cron 徽标 ────────────────────────────────────────────────


def test_schedule_cron_hit_and_null(datasets_env) -> None:
    """经 data_plane PUT schedule 真路径登记定时重派生：命中节点带 cron、
    未登记节点 null（含非派生源行）。"""
    insert_dataset(key="sched_src")
    child_id = insert_dataset(
        key="sched_child", source_kind="derived", source_ref="sched_src"
    )
    other_id = insert_dataset(
        key="sched_other", source_kind="derived", source_ref="sched_src"
    )
    resp = datasets_env.client.put(
        f"/api/v1/datasets/{child_id}/schedule",
        headers=CUST_HEADERS["cust-001"],
        json={"cron": "0 * * * *"},
    )
    assert resp.status_code == 200, resp.text

    body = fetch_graph(datasets_env.client)
    indexed = nodes_by_key(body)
    assert indexed["sched_child"]["dataset_id"] == child_id
    assert indexed["sched_child"]["schedule_cron"] == "0 * * * *"
    assert indexed["sched_other"]["dataset_id"] == other_id
    assert indexed["sched_other"]["schedule_cron"] is None
    assert indexed["sched_src"]["schedule_cron"] is None


# ── 节点上限截断 ──────────────────────────────────────────────────────


def test_truncation_over_500_nodes(datasets_env) -> None:
    """505 行轻量数据集：按 updated_at 倒序保留 500、truncated=true、
    最老 5 行不出图。"""
    base = datetime(2026, 1, 1, 0, 0, 0)
    with get_session() as sess:
        sess.add_all(
            DatasetRow(
                customer_id="cust-001",
                key=f"bulk_{i:04d}",
                name=f"bulk-{i}",
                source_kind="upload",
                storage_tier="cell",
                row_count=0,
                updated_at=base + timedelta(minutes=i),
            )
            for i in range(505)
        )
        sess.commit()

    body = fetch_graph(datasets_env.client)
    assert body["truncated"] is True
    assert len(body["nodes"]) == 500
    keys = set(nodes_by_key(body))
    # 最老 5 行（updated_at 最小）被截断
    assert {"bulk_0000", "bulk_0001", "bulk_0002", "bulk_0003", "bulk_0004"} <= (
        set(f"bulk_{i:04d}" for i in range(505)) - keys
    )
    assert "bulk_0504" in keys
    assert body["anomalies"] == 0


# ── 鉴权 ──────────────────────────────────────────────────────────────


def test_unauthenticated_401(datasets_env) -> None:
    """无 console 身份：401（与 datasets 客户面同语义）。"""
    resp = datasets_env.client.get(GRAPH_URL)
    assert resp.status_code == 401
