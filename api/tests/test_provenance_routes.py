"""provenance 端点测试（wire-provenance-ledger 任务 2.x）。

覆盖：ingest 令牌校验（无/错令牌 401、对令牌 200）、ingest 入账与
event_id 幂等、时间线（倒序/before_seq/主体过滤/鉴权 401/跨企业 404）、
status（档位/完整性/无事件如实标注）、tier 调档（含非法档 422）、
overview 概览。

鉴权走 datasets 路由同款身份（Bearer ID token → logto_sub → Customer，
wire-self-serve-launch 4.5）；数据集行经同库会话直插（本组端点不经引擎
通道）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.provenance import reset_provenance_runtime
from fd_wire_api.provenance.settings import get_settings

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_A = "sub-cust-001"
SUB_B = "sub-cust-002"
CUSTOMER_A = bearer(mint_id_token(SUB_A))
CUSTOMER_B = bearer(mint_id_token(SUB_B))


@pytest.fixture()
def provenance_env(tmp_path, monkeypatch):
    """隔离运行时（内存库）+ 全量 app TestClient（与 datasets 测试同款）。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("FD_WIRE_PROVENANCE_INGEST_TOKEN", raising=False)
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path, monkeypatch,
        customers=[(SUB_A, "cust-001"), (SUB_B, "cust-002")],
    )
    reset_datasets_runtime()
    reset_provenance_runtime()
    with TestClient(fastapi_app) as client:
        yield SimpleNamespace(client=client, tmp_path=tmp_path, monkeypatch=monkeypatch)
    reset_provenance_runtime()
    reset_datasets_runtime()


def make_dataset(customer: str = "cust-001", key: str = "sales_2026") -> None:
    """直插数据集登记行（本组端点只读 datasets 元数据，不经引擎通道）。"""
    db = get_session()
    db.add(Dataset(customer_id=customer, key=key, name=key, source_kind="upload"))
    db.commit()
    db.close()


def ingest(
    client: TestClient,
    *,
    ledger: str = "cust-001",
    type: str = "dataset.correction.added",
    key: str = "sales_2026",
    event_id: str | None = None,
    headers: dict | None = None,
):
    body = {
        "ledger": ledger,
        "type": type,
        "actor": {"kind": "customer", "id": ledger, "sig": None},
        "subject": {"kind": "dataset", "key": key},
        "payload": {"op": "update", "anchor": "pk:1", "correction_id": 7},
    }
    if event_id:
        body["event_id"] = event_id
    return client.post(
        "/api/v1/provenance/ingest", json=body, headers=headers or {}
    )


# ------------------------------------------------------------- ingest


def test_ingest_appends_and_returns_envelope(provenance_env) -> None:
    client = provenance_env.client
    first = ingest(client)
    assert first.status_code == 200, first.text
    env1 = first.json()
    assert env1["v"] == 1
    assert env1["seq"] == 1
    assert env1["algo"] == "sha256"
    assert env1["prev_hash"] == "sha256:" + "0" * 64
    assert env1["payload_hash"].startswith("sha256:")
    assert env1["event_hash"].startswith("sha256:")

    second = ingest(client)
    env2 = second.json()
    assert env2["seq"] == 2
    assert env2["prev_hash"] == env1["event_hash"]

    overview = client.get("/api/v1/provenance/overview", headers=CUSTOMER_A)
    assert overview.json()["last_seq"] == 2


def test_ingest_idempotent_same_event_id(provenance_env) -> None:
    client = provenance_env.client
    first = ingest(client, event_id="evt_http_dup")
    assert first.status_code == 200
    again = ingest(client, event_id="evt_http_dup")
    assert again.status_code == 200
    assert again.json()["seq"] == first.json()["seq"] == 1
    assert again.json()["id"] == first.json()["id"] == "evt_http_dup"

    following = ingest(client)
    assert following.json()["seq"] == 2  # 幂等重放未占号
    overview = client.get("/api/v1/provenance/overview", headers=CUSTOMER_A)
    assert overview.json()["last_seq"] == 2


def test_ingest_token_enforced(provenance_env, monkeypatch) -> None:
    """ingest_token 非空：缺头/错头 401，对头 200（常量时间比对路径）。"""
    monkeypatch.setattr(get_settings(), "ingest_token", "secret-token")
    client = provenance_env.client

    missing = ingest(client)
    assert missing.status_code == 401, missing.text

    wrong = ingest(client, headers={"X-Provenance-Token": "wrong"})
    assert wrong.status_code == 401, wrong.text

    ok = ingest(client, headers={"X-Provenance-Token": "secret-token"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["seq"] == 1


def test_ingest_rejects_bad_body(provenance_env) -> None:
    resp = provenance_env.client.post(
        "/api/v1/provenance/ingest",
        json={"ledger": "", "type": "x", "actor": {}, "subject": {}, "payload": {}},
    )
    assert resp.status_code == 422


# ------------------------------------------------------------- 时间线 / 状态


def test_timeline_desc_filter_and_paging(provenance_env) -> None:
    client = provenance_env.client
    make_dataset(key="sales_2026")
    make_dataset(key="other_table")
    for _ in range(3):
        ingest(client, key="sales_2026")
    ingest(client, key="other_table")  # 他人不进本数据集时间线
    ingest(client, ledger="cust-002", key="sales_2026")  # 他人账本

    resp = client.get(
        "/api/v1/provenance/datasets/sales_2026/events", headers=CUSTOMER_A
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert [e["seq"] for e in events] == [3, 2, 1]  # 倒序 + 主体过滤
    first = events[0]
    assert first["type"] == "dataset.correction.added"
    assert first["ts_platform"].endswith("Z")
    assert first["actor"] == {"kind": "customer", "id": "cust-001", "sig": None}
    assert first["subject"] == {"kind": "dataset", "key": "sales_2026"}
    assert first["payload"]["op"] == "update"
    assert first["event_id"].startswith("evt_")

    # before_seq 向上翻页
    older = client.get(
        "/api/v1/provenance/datasets/sales_2026/events",
        params={"before_seq": 3},
        headers=CUSTOMER_A,
    )
    assert [e["seq"] for e in older.json()] == [2, 1]
    limited = client.get(
        "/api/v1/provenance/datasets/sales_2026/events",
        params={"limit": 2},
        headers=CUSTOMER_A,
    )
    assert [e["seq"] for e in limited.json()] == [3, 2]


def test_timeline_auth_and_cross_customer_404(provenance_env) -> None:
    client = provenance_env.client
    make_dataset(key="sales_2026")

    no_header = client.get("/api/v1/provenance/datasets/sales_2026/events")
    assert no_header.status_code == 401

    stranger = client.get(
        "/api/v1/provenance/datasets/sales_2026/events", headers=CUSTOMER_B
    )
    assert stranger.status_code == 404

    missing = client.get(
        "/api/v1/provenance/datasets/no_such/events", headers=CUSTOMER_A
    )
    assert missing.status_code == 404


def test_status_without_events_notes_origin(provenance_env) -> None:
    """无事件数据集：如实标注启用前无历史、完整性 checked=0、不伪造起点。"""
    make_dataset(key="sales_2026")
    resp = provenance_env.client.get(
        "/api/v1/provenance/datasets/sales_2026/status", headers=CUSTOMER_A
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "chain_only"  # 新数据集默认档（D6）
    assert body["integrity"] == {"ok": True, "checked": 0}
    assert body["anchored_through"] is None
    assert body["first_event_ts"] is None
    assert body["note"] == "启用前无历史事件（溯源起点=启用时点）"


def test_status_with_events(provenance_env) -> None:
    client = provenance_env.client
    make_dataset(key="sales_2026")
    first = ingest(client).json()

    resp = client.get(
        "/api/v1/provenance/datasets/sales_2026/status", headers=CUSTOMER_A
    )
    body = resp.json()
    assert body["tier"] == "chain_only"
    assert body["integrity"] == {"ok": True, "checked": 1}
    assert body["first_event_ts"] == first["ts_platform"]
    assert body["note"] is None


# ------------------------------------------------------------- 调档


def test_tier_switch_persists(provenance_env) -> None:
    client = provenance_env.client
    make_dataset(key="sales_2026")

    resp = client.post(
        "/api/v1/provenance/datasets/sales_2026/tier",
        json={"tier": "anchored_batch"},
        headers=CUSTOMER_A,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "sales_2026", "tier": "anchored_batch"}

    status = client.get(
        "/api/v1/provenance/datasets/sales_2026/status", headers=CUSTOMER_A
    )
    assert status.json()["tier"] == "anchored_batch"

    # 全档位可调 + 非法档 422 + 跨企业 404 + 缺头 401
    for tier in ("off", "chain_only", "anchored_realtime"):
        switched = client.post(
            "/api/v1/provenance/datasets/sales_2026/tier",
            json={"tier": tier},
            headers=CUSTOMER_A,
        )
        assert switched.status_code == 200
        assert switched.json()["tier"] == tier
    bad = client.post(
        "/api/v1/provenance/datasets/sales_2026/tier",
        json={"tier": "turbo"},
        headers=CUSTOMER_A,
    )
    assert bad.status_code == 422
    stranger = client.post(
        "/api/v1/provenance/datasets/sales_2026/tier",
        json={"tier": "off"},
        headers=CUSTOMER_B,
    )
    assert stranger.status_code == 404
    no_header = client.post(
        "/api/v1/provenance/datasets/sales_2026/tier", json={"tier": "off"}
    )
    assert no_header.status_code == 401


# ------------------------------------------------------------- 概览


def test_overview_reflects_ledger(provenance_env) -> None:
    client = provenance_env.client
    make_dataset(key="sales_2026")

    empty = client.get("/api/v1/provenance/overview", headers=CUSTOMER_A)
    assert empty.status_code == 200
    assert empty.json() == {
        "ledger": "cust-001",
        "last_seq": 0,
        "integrity": {"ok": True, "checked": 0},
        "anchoring": {"backend": "none", "last_sealed_epoch": None, "lag_events": 0},
    }

    ingest(client)
    ingest(client)
    ingest(client, ledger="cust-002")  # 他账本不计入

    resp = client.get("/api/v1/provenance/overview", headers=CUSTOMER_A)
    body = resp.json()
    assert body["last_seq"] == 2
    assert body["integrity"] == {"ok": True, "checked": 2}
    assert body["anchoring"]["backend"] == "none"
    assert body["anchoring"]["last_sealed_epoch"] is None
    assert body["anchoring"]["lag_events"] == 2  # 未封存：全量滞后如实计数

    no_header = client.get("/api/v1/provenance/overview")
    assert no_header.status_code == 401
