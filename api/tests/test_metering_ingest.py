"""摄取端点与规范化层测试：幂等、轨道解析、来源映射、缺戳异常。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_wire_api.metering.db import get_session
from fd_wire_api.metering.models import MeteringAnomaly, MeteringRecord

from test_metering_support import (
    TOKEN,
    auth_headers,
    db_row_count,
    make_event,
    metering_client,
    metering_env,
)


def push(client: TestClient, events: list[dict[str, Any]], token: str | None = TOKEN):
    headers = auth_headers(token) if token else {}
    return client.post("/internal/metering/events", json=events, headers=headers)


def all_records() -> list[MeteringRecord]:
    with get_session() as session:
        return list(session.execute(select(MeteringRecord)).scalars().all())


def all_anomalies() -> list[MeteringAnomaly]:
    with get_session() as session:
        return list(session.execute(select(MeteringAnomaly)).scalars().all())


# ---------------------------------------------------------------------------
# 鉴权（fail-closed）
# ---------------------------------------------------------------------------


def test_push_requires_bearer_token(metering_client: TestClient) -> None:
    metering_client.headers.pop("Authorization", None)
    resp = push(metering_client, [make_event()], token=None)
    assert resp.status_code == 401
    resp = push(metering_client, [make_event()], token="wrong-token")
    assert resp.status_code == 401


def test_push_fails_closed_when_token_unconfigured(tmp_path, monkeypatch) -> None:
    """METERING_INGEST_TOKEN 未配置：拒绝而非无鉴权放行。"""
    with metering_env(tmp_path, monkeypatch, METERING_INGEST_TOKEN=None) as client:
        resp = client.post(
            "/internal/metering/events",
            json=[make_event()],
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 403
        assert "not configured" in resp.json()["detail"]


def test_push_rejects_non_array_body(metering_client: TestClient) -> None:
    resp = metering_client.post(
        "/internal/metering/events", json={"unexpected": True}, headers=auth_headers()
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 批量摄取与字段映射
# ---------------------------------------------------------------------------


def test_batch_ingest_creates_normalized_records(metering_client: TestClient) -> None:
    events = [
        make_event(request_id="r-1", username="alice@customer.com", rows=120),
        make_event(request_id="r-2", username="bob@customer.com", server="fininfo", rows=30),
    ]
    resp = push(metering_client, events)
    assert resp.status_code == 200
    assert resp.json() == {
        "received": 2,
        "accepted": 2,
        "duplicates": 0,
        "anomalies": 0,
        "anomaly_duplicates": 0,
        "skipped": 0,
    }
    records = {r.request_id: r for r in all_records()}
    assert set(records) == {"r-1", "r-2"}
    first = records["r-1"]
    assert first.caller == "alice@customer.com"
    assert first.caller_principal == "alice@customer.com"
    assert first.credential_track == "customer"
    assert first.server_name == "business-mcp"
    assert first.server_path == "/servers/business-mcp"
    assert first.tool_name == "read_range"
    assert first.rows_returned == 120
    assert first.source == "yizuo"  # business-mcp 命中来源映射
    assert first.event_timestamp.isoformat().startswith("2026-10-01T10:00:00")
    assert first.created_at is not None


def test_push_accepts_envelope_body(metering_client: TestClient) -> None:
    """兼容 {"events": [...]} 包装形态。"""
    resp = metering_client.post(
        "/internal/metering/events",
        json={"events": [make_event(request_id="env-1", rows=5)]},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


# ---------------------------------------------------------------------------
# 幂等：request_id + server 唯一
# ---------------------------------------------------------------------------


def test_replayed_batch_does_not_double_count(metering_client: TestClient) -> None:
    events = [
        make_event(request_id="r-1", rows=100),
        make_event(request_id="r-2", rows=200),
    ]
    first = push(metering_client, events)
    assert first.json()["accepted"] == 2
    second = push(metering_client, events)
    assert second.json()["accepted"] == 0
    assert second.json()["duplicates"] == 2
    # 验收口径：重复摄取同一批事件，metering_records 行数不变
    assert db_row_count("metering_records") == 2


def test_duplicate_within_single_batch(metering_client: TestClient) -> None:
    events = [make_event(request_id="r-1", rows=10), make_event(request_id="r-1", rows=10)]
    resp = push(metering_client, events)
    assert resp.json()["accepted"] == 1
    assert resp.json()["duplicates"] == 1
    assert db_row_count("metering_records") == 1


def test_same_request_id_different_server_both_kept(metering_client: TestClient) -> None:
    """幂等键是 request_id+server：同请求打两个 server 是两条计量。"""
    events = [
        make_event(request_id="r-1", server="business-mcp", rows=10),
        make_event(request_id="r-1", server="fininfo", rows=20),
    ]
    resp = push(metering_client, events)
    assert resp.json()["accepted"] == 2
    assert db_row_count("metering_records") == 2


# ---------------------------------------------------------------------------
# 凭据轨道与来源标记解析
# ---------------------------------------------------------------------------


def test_credential_track_resolution(metering_client: TestClient) -> None:
    events = [
        make_event(request_id="p-1", username="svc-analysis", rows=1),
        make_event(request_id="p-2", username="SVC-VAAS", rows=2),  # 名单大小写不敏感
        make_event(request_id="c-1", username="alice@customer.com", rows=3),
    ]
    push(metering_client, events)
    records = {r.request_id: r for r in all_records()}
    assert records["p-1"].credential_track == "platform"
    assert records["p-2"].credential_track == "platform"
    assert records["c-1"].credential_track == "customer"


def test_source_resolution(metering_client: TestClient) -> None:
    events = [
        # platform 轨道未配置身份映射时恒 platform-internal，即使 server 命中映射
        make_event(request_id="s-1", username="svc-analysis", server="business-mcp", rows=1),
        # 精确映射
        make_event(request_id="s-2", server="business-mcp", rows=1),
        # 前缀通配映射
        make_event(request_id="s-3", server="wanxing-cell-a", rows=1),
        # 未映射 → customer
        make_event(request_id="s-4", server="fininfo", rows=1),
    ]
    push(metering_client, events)
    records = {r.request_id: r for r in all_records()}
    assert records["s-1"].source == "platform-internal"
    assert records["s-2"].source == "yizuo"
    assert records["s-3"].source == "wanxing"
    assert records["s-4"].source == "customer"


def test_platform_track_source_by_identity_map(tmp_path, monkeypatch) -> None:
    """G1：platform 轨按「身份 → 来源」映射区分壹座 / 萬星 / 平台内部。

    - 前缀通配命中：``yizuo-*`` → yizuo
    - 精确条目命中：``wanxing-bot-9`` → wanxing
    - platform 轨身份未命中映射 → 回落 platform-internal
    """
    with metering_env(
        tmp_path,
        monkeypatch,
        PLATFORM_IDENTITIES="yizuo-agent-01,wanxing-bot-9,svc-wire",
        PLATFORM_IDENTITY_SOURCE_MAP="yizuo-*=yizuo,wanxing-bot-9=wanxing",
    ) as client:
        events = [
            make_event(request_id="i-1", username="yizuo-agent-01", rows=1),
            # 身份名单大小写不敏感：大写形态仍在 platform 轨，精确命中映射
            make_event(request_id="i-2", username="WANXING-BOT-9", rows=2),
            make_event(request_id="i-3", username="svc-wire", rows=3),
        ]
        push(client, events)
    records = {r.request_id: r for r in all_records()}
    assert all(r.credential_track == "platform" for r in records.values())
    assert records["i-1"].source == "yizuo"
    assert records["i-2"].source == "wanxing"
    assert records["i-3"].source == "platform-internal"


def test_platform_identity_source_map_case_insensitive(tmp_path, monkeypatch) -> None:
    """G1：身份映射匹配两侧大小写不敏感（映射键大写、身份原样/大写）。"""
    with metering_env(
        tmp_path,
        monkeypatch,
        PLATFORM_IDENTITIES="yizuo-agent-01,wanxing-bot-9",
        PLATFORM_IDENTITY_SOURCE_MAP="YIZUO-*=yizuo,WANXING-BOT-9=wanxing",
    ) as client:
        events = [
            # 映射键大写 + 前缀通配，身份小写 → 命中
            make_event(request_id="c-1", username="yizuo-agent-01", rows=1),
            # 映射键大写 + 精确条目，身份大写 → 命中
            make_event(request_id="c-2", username="WANXING-BOT-9", rows=2),
        ]
        push(client, events)
    records = {r.request_id: r for r in all_records()}
    assert records["c-1"].source == "yizuo"
    assert records["c-2"].source == "wanxing"


def test_identity_map_does_not_affect_customer_track(tmp_path, monkeypatch) -> None:
    """G1：身份映射只作用于 platform 轨；customer 轨仍按 server 名解析。"""
    with metering_env(
        tmp_path,
        monkeypatch,
        PLATFORM_IDENTITY_SOURCE_MAP="alice*=wanxing",  # 故意与 server 映射结论不同
    ) as client:
        events = [
            make_event(request_id="k-1", username="alice@customer.com", server="business-mcp", rows=1),
            make_event(request_id="k-2", username="alice@customer.com", server="unmapped-srv", rows=1),
        ]
        push(client, events)
    records = {r.request_id: r for r in all_records()}
    assert all(r.credential_track == "customer" for r in records.values())
    assert records["k-1"].source == "yizuo"  # server 精确映射生效，未被身份映射劫持
    assert records["k-2"].source == "customer"


# ---------------------------------------------------------------------------
# rows_returned 提取位置与缺戳异常
# ---------------------------------------------------------------------------


def test_rows_returned_extraction_locations(metering_client: TestClient) -> None:
    events = [
        make_event(request_id="x-1", rows=7, rows_location="response"),
        make_event(request_id="x-2", rows=8, rows_location="top"),
        make_event(request_id="x-3", rows=9, rows_location="meta"),
        make_event(request_id="x-4", rows=11, rows_location="result"),
    ]
    resp = push(metering_client, events)
    assert resp.json()["accepted"] == 4
    assert {r.rows_returned for r in all_records()} == {7, 8, 9, 11}


def test_missing_rows_returned_writes_anomaly(metering_client: TestClient) -> None:
    """缺戳事件不进计量、写 metering_anomalies，可经计数端点查得。"""
    events = [
        make_event(request_id="ok-1", rows=50),
        make_event(request_id="bad-1", rows=None, rows_location="missing"),
    ]
    resp = push(metering_client, events)
    body = resp.json()
    assert body["accepted"] == 1
    assert body["anomalies"] == 1

    assert db_row_count("metering_records") == 1
    anomalies = all_anomalies()
    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.reason == "missing_rows_returned"
    assert anomaly.request_id == "bad-1"
    assert anomaly.server_name == "business-mcp"
    assert anomaly.tool_name == "read_range"
    assert anomaly.caller == "alice@customer.com"
    assert anomaly.credential_track == "customer"

    count = metering_client.get("/internal/metering/anomalies/count").json()
    assert count["total"] == 1
    assert count["by_reason"] == {"missing_rows_returned": 1}
    listing = metering_client.get("/internal/metering/anomalies").json()
    assert listing["count"] == 1
    assert listing["anomalies"][0]["request_id"] == "bad-1"


def test_anomaly_replay_does_not_duplicate(metering_client: TestClient) -> None:
    event = make_event(request_id="bad-1", rows=None, rows_location="missing")
    push(metering_client, [event])
    second = push(metering_client, [event])
    assert second.json()["anomalies"] == 0
    assert second.json()["anomaly_duplicates"] == 1
    assert db_row_count("metering_anomalies") == 1


def test_unparseable_event_writes_anomaly(metering_client: TestClient) -> None:
    """缺 request_id/server/timestamp 的事件：异常留痕而非静默丢弃。"""
    broken = make_event(request_id="", rows=10)
    resp = push(metering_client, [broken])
    assert resp.json()["anomalies"] == 1
    anomaly = all_anomalies()[0]
    assert anomaly.reason == "unparseable_event"
    assert anomaly.request_id is None


def test_non_mcp_audit_streams_skipped(metering_client: TestClient) -> None:
    """registry_api_access / token_mint 流与逐调用计量无关：跳过不落表。"""
    events = [
        make_event(request_id="m-1", rows=5),
        {**make_event(request_id="a-1"), "log_type": "registry_api_access"},
        {**make_event(request_id="t-1"), "log_type": "token_mint"},
    ]
    resp = push(metering_client, events)
    body = resp.json()
    assert body["accepted"] == 1
    assert body["skipped"] == 2
    assert body["anomalies"] == 0
    assert db_row_count("metering_records") == 1
    assert db_row_count("metering_anomalies") == 0


# ---------------------------------------------------------------------------
# 竞态吸收：并发同键由唯一约束兜底（on_conflict_do_nothing，不炸批）
# ---------------------------------------------------------------------------


def _record_values(request_id: str) -> dict:
    from datetime import datetime, timezone

    from fd_wire_api.metering.db import utc_now

    ts = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    return {
        "event_timestamp": ts,
        "request_id": request_id,
        "caller": "alice@customer.com",
        "caller_principal": "alice@customer.com",
        "credential_track": "customer",
        "server_name": "business-mcp",
        "server_path": "/servers/business-mcp",
        "tool_name": "read_range",
        "rows_returned": 10,
        "source": "yizuo",
        "created_at": utc_now(),
    }


def test_concurrent_same_key_record_absorbed(metering_client: TestClient) -> None:
    """竞态形状：预检不见、INSERT 撞并发同键 → 约束吸收计重复，不抛异常。

    同事务内连续两次执行同一落库语句等价于「预检窗口内并发事务已提交」
    的数据库侧结局：第二次 INSERT 命中唯一约束，被 ON CONFLICT DO
    NOTHING 原子吸收（rowcount=0），事务不回滚、行数不增。
    """
    from fd_wire_api.metering.ingest import _insert_on_conflict_do_nothing

    values = _record_values("race-1")
    with get_session() as session:
        first = _insert_on_conflict_do_nothing(
            session, MeteringRecord, values, index_elements=["request_id", "server_name"]
        )
        second = _insert_on_conflict_do_nothing(
            session, MeteringRecord, values, index_elements=["request_id", "server_name"]
        )
        session.commit()
    assert first == 1
    assert second == 0
    assert db_row_count("metering_records") == 1


def test_concurrent_same_key_anomaly_absorbed(metering_client: TestClient) -> None:
    """异常留痕同语义：uq_anomaly_request_reason 三键竞态吸收。"""
    import json as json_mod
    from datetime import datetime, timezone

    from fd_wire_api.metering.db import utc_now
    from fd_wire_api.metering.ingest import _insert_on_conflict_do_nothing

    values = {
        "reason": "missing_rows_returned",
        "request_id": "race-bad",
        "server_name": "business-mcp",
        "tool_name": "read_range",
        "caller": "alice@customer.com",
        "credential_track": "customer",
        "detail": json_mod.dumps({"where": "response"}, ensure_ascii=False),
        "event_timestamp": datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
        "created_at": utc_now(),
    }
    with get_session() as session:
        first = _insert_on_conflict_do_nothing(
            session,
            MeteringAnomaly,
            values,
            index_elements=["request_id", "server_name", "reason"],
        )
        second = _insert_on_conflict_do_nothing(
            session,
            MeteringAnomaly,
            values,
            index_elements=["request_id", "server_name", "reason"],
        )
        session.commit()
    assert first == 1
    assert second == 0
    assert db_row_count("metering_anomalies") == 1


def test_on_conflict_clause_compiles_for_both_dialects() -> None:
    """生产 PG 与测试 SQLite 双方言都编译出 ON CONFLICT DO NOTHING。"""
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    pg_sql = str(
        pg_insert(MeteringRecord)
        .values(request_id="r", server_name="s")
        .on_conflict_do_nothing(index_elements=["request_id", "server_name"])
        .compile(dialect=postgresql.dialect())
    )
    assert "ON CONFLICT (request_id, server_name) DO NOTHING" in pg_sql

    lite_sql = str(
        sqlite_insert(MeteringAnomaly)
        .values(reason="r", request_id="q", server_name="s")
        .on_conflict_do_nothing(index_elements=["request_id", "server_name", "reason"])
        .compile(dialect=sqlite.dialect())
    )
    assert "ON CONFLICT" in lite_sql
    assert "DO NOTHING" in lite_sql
