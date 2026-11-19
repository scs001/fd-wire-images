"""registry Mongo 直读轮询测试：假 collection 注入，不打真库。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from fd_wire_api.metering import registry_poller
from fd_wire_api.metering.db import get_session
from fd_wire_api.metering.registry_poller import poll_registry_once

from test_metering_support import auth_headers, db_row_count, make_event, metering_client, metering_env


class FakeCollection:
    """pymongo collection 的最小假实现：find(filter, sort=, limit=)。

    支持 ``{"$gt": ...}``、``{"$or": [...]}`` 与多键排序——复合游标
    （``timestamp`` + ``_id``）靠它们才测得出来。
    """

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def find(self, filter: dict[str, Any], sort: list[tuple[str, int]], limit: int) -> list[dict]:
        matched = [d for d in self.docs if self._matches(d, filter)]
        for key, direction in reversed(sort):  # 稳定排序：从末键往前排
            # 缺键（老夹具没有 `_id`）排到末尾，不参与类型比较
            matched.sort(
                key=lambda d: (1,) if d.get(key) is None else (0, d.get(key)),
                reverse=direction < 0,
            )
        return matched[:limit]

    @classmethod
    def _matches(cls, doc: dict[str, Any], filter: dict[str, Any]) -> bool:
        from datetime import datetime

        def _norm(value: Any) -> Any:
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return value
            return value

        for field, condition in filter.items():
            if field == "$or":
                if not any(cls._matches(doc, sub) for sub in condition):
                    return False
                continue
            value = _norm(doc.get(field))
            if isinstance(condition, dict) and "$gt" in condition:
                bound = _norm(condition["$gt"])
                if value is None or value <= bound:
                    return False
            elif value != _norm(condition):
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


def test_poll_pages_until_caught_up(metering_client: TestClient, monkeypatch) -> None:
    """批上限只限**页大小**，不再限一轮总量：4 条 / 每页 2 条 ⇒ 一轮取完。"""
    monkeypatch.setenv("METERING_POLL_LIMIT", "2")
    from fd_wire_api.metering.db import reset_metering_runtime

    reset_metering_runtime()  # 使 poll limit 生效（其余 env 不变）
    docs = [
        make_event(request_id=f"m-{i}", rows=i, ts=f"2026-10-01T10:00:0{i if i < 10 else 9}+00:00")
        for i in range(1, 5)
    ]
    result = _run_poll(docs)
    assert result["fetched"] == 4  # 两页
    assert result["accepted"] == 4


def test_poll_max_pages_bounds_single_tick(metering_client: TestClient, monkeypatch) -> None:
    """页数上限能圈住单轮工作量（防一次 tick 无限拉）。"""
    monkeypatch.setenv("METERING_POLL_LIMIT", "2")
    from fd_wire_api.metering.db import get_session, reset_metering_runtime
    from fd_wire_api.metering.registry_poller import poll_registry_once

    reset_metering_runtime()
    docs = [
        make_event(request_id=f"m-{i}", rows=i, ts=f"2026-10-01T10:00:0{i}+00:00")
        for i in range(1, 7)
    ]
    with get_session() as session:
        result = poll_registry_once(
            session, collection=FakeCollection(docs), max_pages=2
        )
        session.commit()
    assert result.fetched == 4  # 2 页 × 2 条
    assert result.ingest.accepted == 4


def _bulk(prefix: str, count: int, ts: str, start_id: int = 1) -> list[dict[str, Any]]:
    return [
        {**make_event(request_id=f"{prefix}-{i}", rows=1, ts=ts), "_id": start_id + i}
        for i in range(count)
    ]


def test_poll_advances_when_overlap_window_is_full(metering_client: TestClient, monkeypatch) -> None:
    """回归：重叠窗被「已入库的旧事件」填满时，一轮必须翻页前进。

    2026-11-15 生产实历：窗口内恰好 500 条、批上限也是 500，旧实现每轮都
    重读同一页（`fetched=500 / accepted=0 / duplicates=500`），水位永不前进，
    此后 7 天 64,532 条事件全部进不来，而驱动一直在跑。
    """
    monkeypatch.setenv("METERING_POLL_LIMIT", "3")
    from fd_wire_api.metering.db import reset_metering_runtime

    reset_metering_runtime()

    # 先灌水：3 条同一时刻的旧事件成为水位（10:00:00）
    old = _bulk("old", 3, "2026-10-01T10:00:00+00:00")
    first = _run_poll(old)
    assert first["accepted"] == 3

    # 满窗：再塞 3 条落在水位前 5 分钟窗内（模拟「窗口里全是已入库的」）
    window_fillers = _bulk("fill", 3, "2026-10-01T09:59:00+00:00")
    docs_after_fill = old + window_fillers
    _run_poll(docs_after_fill)
    # 这一轮会把 3 条 filler 也吃进来（它们是新事件）
    assert db_row_count("metering_records") == 6

    # 现在窗口(09:59:00..10:00:00] 内共 6 条、全部已入库，页大小 3
    # 若只取一页就会永远停在「3 条重复」。带新事件再来一轮：
    fresh = _bulk("new", 3, "2026-10-01T10:05:00+00:00", start_id=100)
    result = _run_poll(docs_after_fill + fresh)
    assert result["accepted"] == 3, result
    assert result["duplicates"] >= 3  # 重叠窗确实被重读了
    assert db_row_count("metering_records") == 9


def test_poll_composite_cursor_splits_same_timestamp(metering_client: TestClient, monkeypatch) -> None:
    """同一时间戳的事件超过一页时，靠 `_id` 游标也要能翻过去（只按 ts 会死循环）。"""
    monkeypatch.setenv("METERING_POLL_LIMIT", "2")
    from fd_wire_api.metering.db import reset_metering_runtime

    reset_metering_runtime()
    same_ts = _bulk("same", 5, "2026-10-01T10:00:00+00:00")
    result = _run_poll(same_ts)
    assert result["fetched"] == 5
    assert result["accepted"] == 5
    assert db_row_count("metering_records") == 5


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
