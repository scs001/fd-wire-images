"""聚合查询测试：按（身份×工具）与合计的调用数/行数，轨道与时间窗过滤。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from test_metering_support import auth_headers, make_event, metering_client


def seed(client: TestClient) -> None:
    events = [
        # alice：read_range 120 行 + read 30 行（同工具两次调用）
        make_event(request_id="a-1", username="alice", tool="read_range", rows=120,
                   ts="2026-10-01T10:00:00+00:00"),
        make_event(request_id="a-2", username="alice", tool="read_range", rows=80,
                   ts="2026-10-02T10:00:00+00:00"),
        make_event(request_id="a-3", username="alice", tool="read", rows=30,
                   ts="2026-10-02T11:00:00+00:00"),
        # bob（平台身份）：read_range 50 行
        make_event(request_id="b-1", username="svc-analysis", tool="read_range", rows=50,
                   ts="2026-10-03T09:00:00+00:00"),
        # alice 在窗外（9 月）
        make_event(request_id="a-0", username="alice", tool="read_range", rows=999,
                   ts="2026-09-15T10:00:00+00:00"),
    ]
    resp = client.post("/internal/metering/events", json=events, headers=auth_headers())
    assert resp.json()["accepted"] == 5


def test_usage_aggregates_by_caller_and_tool(metering_client: TestClient) -> None:
    seed(metering_client)
    resp = metering_client.get("/internal/metering/usage")
    assert resp.status_code == 200
    body = resp.json()
    breakdown = {(row["caller"], row["tool_name"]): row for row in body["breakdown"]}
    assert set(breakdown) == {("alice", "read"), ("alice", "read_range"), ("svc-analysis", "read_range")}
    assert breakdown[("alice", "read_range")]["calls"] == 3  # 含 9 月窗外的 a-0
    assert breakdown[("alice", "read_range")]["rows"] == 120 + 80 + 999
    assert breakdown[("alice", "read")]["calls"] == 1
    assert breakdown[("alice", "read")]["rows"] == 30
    assert breakdown[("svc-analysis", "read_range")]["calls"] == 1
    assert breakdown[("svc-analysis", "read_range")]["rows"] == 50
    # 合计
    assert body["totals"] == {"calls": 5, "rows": 120 + 80 + 30 + 50 + 999}


def test_usage_filters_by_credential_track(metering_client: TestClient) -> None:
    seed(metering_client)
    body = metering_client.get(
        "/internal/metering/usage", params={"credential_track": "platform"}
    ).json()
    assert body["totals"] == {"calls": 1, "rows": 50}
    assert [row["caller"] for row in body["breakdown"]] == ["svc-analysis"]

    body = metering_client.get(
        "/internal/metering/usage", params={"credential_track": "customer"}
    ).json()
    assert body["totals"]["calls"] == 4
    assert body["totals"]["rows"] == 120 + 80 + 30 + 999


def test_usage_filters_by_time_window(metering_client: TestClient) -> None:
    seed(metering_client)
    body = metering_client.get(
        "/internal/metering/usage",
        params={"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-02T23:59:59+00:00"},
    ).json()
    # 窗内：a-1/a-2/a-3（b-1 在 10-03，a-0 在 9 月）
    assert body["totals"] == {"calls": 3, "rows": 120 + 80 + 30}
    body = metering_client.get(
        "/internal/metering/usage", params={"start": "2026-10-03T00:00:00Z"}
    ).json()
    assert body["totals"] == {"calls": 1, "rows": 50}


def test_usage_rejects_bad_params(metering_client: TestClient) -> None:
    assert (
        metering_client.get("/internal/metering/usage", params={"credential_track": "nope"})
        .status_code
        == 422
    )
    assert (
        metering_client.get("/internal/metering/usage", params={"start": "not-a-date"}).status_code
        == 422
    )


def test_usage_empty_db(metering_client: TestClient) -> None:
    body = metering_client.get("/internal/metering/usage").json()
    assert body["breakdown"] == []
    assert body["totals"] == {"calls": 0, "rows": 0}
