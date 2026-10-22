"""registry Mongo 直读轮询测试：假 collection 注入，不打真库。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from fd_wire_api.metering import registry_poller
from fd_wire_api.metering.db import get_session
from fd_wire_api.metering.registry_poller import poll_registry_once

from test_metering_support import auth_headers, db_row_count, make_event, metering_client, metering_env


class FakeCollection:
    """pymongo collection 的最小假实现：find(filter, sort=, limit=)。"""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def find(self, filter: dict[str, Any], sort: list[tuple[str, int]], limit: int) -> list[dict]:
        matched = [d for d in self.docs if self._matches(d, filter)]
        key, direction = sort[0]
        matched.sort(key=lambda d: d[key], reverse=direction < 0)
        return matched[:limit]

    @staticmethod
    def _matches(doc: dict[str, Any], filter: dict[str, Any]) -> bool:
        from datetime import datetime

        for field, condition in filter.items():
            value = doc.get(field)
            if isinstance(condition, dict) and "$gt" in condition:
                if isinstance(value, str):  # ISO 串 → datetime 才能与边界比较
                    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if value is None or value <= condition["$gt"]:
                    return False
            elif value != condition:
                return False
        return True


def _run_poll(client_docs: list[dict[str, Any]]) -> dict[str, Any]:
    with get_session() as session:
        result = poll_registry_once(session, collection=FakeCollection(client_docs))
        session.commit()
    return result.as_dict()


def test_poll_ingests_and_is_idempotent(metering_client: TestClient) -> None:
    docs = [
        make_event(request_id="m-1", username="alice", rows=100, ts="2026-10-01T10:00:00+00:00"),
        make_event(request_id="m-2", username="svc-vaas", rows=40, ts="2026-10-01T11:00:00+00:00"),
        # 非 MCP 审计流：跳过
        {**make_event(request_id="m-3"), "log_type": "registry_api_access"},
        # 缺戳：异常留痕
        make_event(request_id="m-4", rows=None, rows_location="missing",
                   ts="2026-10-01T12:00:00+00:00"),
    ]
    first = _run_poll(docs)
    assert first["fetched"] == 3  # registry_api_access 在 find 阶段被过滤
    assert first["accepted"] == 2
    assert first["anomalies"] == 1
    assert first["skipped"] == 0
    assert db_row_count("metering_records") == 2

    # 第二轮：水位=12:00（计量记录与异常留痕取大者），5min 重叠窗内
    # 只有 12:00 的 m-4 会被重拉 → 幂等去重，不双记
    second = _run_poll(docs)
    assert second["fetched"] == 1
    assert second["accepted"] == 0
    assert second["anomalies"] == 0
    assert second["anomaly_duplicates"] == 1
    assert db_row_count("metering_records") == 2  # 行数不变
    assert db_row_count("metering_anomalies") == 1


def test_poll_picks_up_new_events_after_watermark(metering_client: TestClient) -> None:
    early = make_event(request_id="m-1", rows=10, ts="2026-10-01T10:00:00+00:00")
    _run_poll([early])
    late = [
        early,
        make_event(request_id="m-2", rows=20, ts="2026-10-01T10:00:30+00:00"),
        make_event(request_id="m-3", rows=30, ts="2026-10-01T10:00:20+00:00"),
    ]
    result = _run_poll(late)
    # 重叠窗内全部重拉（水位 10:00:00 - 5min），新增 2 条入库，旧 1 条去重
    assert result["accepted"] == 2
    assert result["duplicates"] == 1
    assert db_row_count("metering_records") == 3


def test_poll_respects_batch_limit(metering_client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("METERING_POLL_LIMIT", "2")
    from fd_wire_api.metering.db import reset_metering_runtime

    reset_metering_runtime()  # 使 poll limit 生效（其余 env 不变）
    docs = [
        make_event(request_id=f"m-{i}", rows=i, ts=f"2026-10-01T10:00:0{i if i < 10 else 9}+00:00")
        for i in range(1, 5)
    ]
    result = _run_poll(docs)
    assert result["fetched"] == 2
    assert result["accepted"] == 2


def test_poll_endpoint_conflict_when_mongo_unconfigured(metering_client: TestClient) -> None:
    """REGISTRY_MONGO_URI 未配置：轮询端点 409，明示配置缺失。"""
    resp = metering_client.post("/internal/metering/ingest/poll", headers=auth_headers())
    assert resp.status_code == 409
    assert "REGISTRY_MONGO_URI" in resp.json()["detail"]


def test_poll_endpoint_requires_token(metering_client: TestClient) -> None:
    resp = metering_client.post(
        "/internal/metering/ingest/poll", headers={"Authorization": ""}
    )
    assert resp.status_code == 401


def test_poll_endpoint_with_injected_collection(tmp_path, monkeypatch) -> None:
    """端到端：POST /ingest/poll → 直读 → 规范化 → 落库（假 collection）。"""
    with metering_env(tmp_path, monkeypatch) as client:
        docs = [make_event(request_id="e-1", username="alice", rows=60)]
        monkeypatch.setattr(
            registry_poller, "connect_collection", lambda: FakeCollection(docs)
        )
        resp = client.post("/internal/metering/ingest/poll", headers=auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["fetched"] == 1
        assert body["accepted"] == 1
        usage = client.get("/internal/metering/usage", headers=auth_headers()).json()
        assert usage["totals"] == {"calls": 1, "rows": 60}
