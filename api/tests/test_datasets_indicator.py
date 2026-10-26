"""指标目录建集（wire-indicator-to-dataset）。

拉数在 fetch_series 边界打桩（上游 business-mcp 有自己的套件）；本套件
只管 wire 侧行为：分派/双档物化/幂等/配额闸/负向面。免费档用例走
free_env（api 层本地物化），cell 档用例走 datasets_env（FakeEngine）。
"""
from __future__ import annotations

import pytest

from fd_wire_api.datasets import indicator_fetch
from test_datasets_common import (  # noqa: F401 - pytest fixtures
    CUST_HEADERS,
    datasets_env,
)
from test_datasets_free_tier import FREE, PAID, free_env  # noqa: F401

INDICATOR_ID = "yearbook_catalog:39709"


def _rows(n=3):
    return [
        {"indicator_id": INDICATOR_ID, "date": f"202{i}-12-31", "value": 100.0 + i,
         "unit": "元", "brand": "finddata"}
        for i in range(n)
    ]


@pytest.fixture()
def fake_fetch(monkeypatch):
    """fetch_series 桩：默认返回 3 行；用例可改写 rows/error/side_effect。"""
    state = {"rows": _rows(), "error": None, "side_effect": None, "calls": []}

    def _fake(indicator_id, entity_type, entity_id, start, end, **kwargs):
        state["calls"].append(
            {"indicator_id": indicator_id, "entity_type": entity_type,
             "entity_id": entity_id, "start": start, "end": end})
        if state["side_effect"] is not None:
            raise state["side_effect"]
        if state["error"] is not None:
            return indicator_fetch.IndicatorSeries(rows=[], error=state["error"])
        return indicator_fetch.IndicatorSeries(rows=list(state["rows"]), error=None)

    monkeypatch.setattr(indicator_fetch, "fetch_series", _fake)
    return state


def _create(client, headers_key, **overrides):
    payload = {
        "name": "年鉴-城镇人均可支配收入",
        "source": {
            "kind": "indicator",
            "indicator_id": INDICATOR_ID,
            "entity_type": "region",
            "entity_id": "北京市",
            "start": "2020",
            "end": "2024",
        },
    }
    payload["source"].update(overrides)
    return client.post(
        "/api/v1/datasets", json=payload, headers=CUST_HEADERS[headers_key]
    )


# ------------------------------------------------------------------ 免费档（api 层本地物化）

class TestFreeTier:
    def test_create_local_materialized(self, free_env, fake_fetch):
        resp = _create(free_env.client, FREE)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["source_kind"] == "indicator"
        assert body["storage_tier"] == "api"
        assert body["source_ref"] == INDICATOR_ID
        assert body["row_count"] == 3
        cols = [c["name"] for c in body["columns"]]
        assert cols == ["indicator_id", "date", "value", "unit"]
        # 行真落 api 层（零引擎调用）
        assert free_env.fake.calls == []
        client = free_env.client
        detail = client.get(f"/api/v1/datasets/{body['id']}/rows",
                            headers=CUST_HEADERS[FREE])
        assert detail.status_code == 200, detail.text
        rows = detail.json()["rows"]
        assert rows[0]["indicator_id"] == INDICATOR_ID
        assert rows[0]["date"] == "2020-12-31"
        assert rows[0]["value"] == 100.0

    def test_fetch_args_passthrough(self, free_env, fake_fetch):
        _create(free_env.client, FREE, start="2015", end="2024")
        call = fake_fetch["calls"][0]
        assert call["indicator_id"] == INDICATOR_ID
        assert call["entity_type"] == "region"
        assert call["entity_id"] == "北京市"
        assert call["start"] == "2015" and call["end"] == "2024"

    def test_row_quota_over_limit_rejects_whole_batch(self, free_env, fake_fetch,
                                                      monkeypatch):
        from fd_wire_api.datasets.service import QuotaExceeded
        from fd_wire_api.datasets import service as svc

        fake_fetch["rows"] = _rows(5)
        # ingest 途中兜底闸：直接在 api_store 抛（模拟行闸触发）
        from fd_wire_api.datasets import api_store
        real = api_store.ingest_indicator_rows

        def exploding(*a, **k):
            raise svc.QuotaExceeded("行数 10001 超过免费档单数据集上限 10000")

        monkeypatch.setattr(api_store, "ingest_indicator_rows", exploding)
        resp = _create(free_env.client, FREE)
        assert resp.status_code == 429, resp.text
        # 零半写：无数据集登记行残留
        listing = free_env.client.get("/api/v1/datasets", headers=CUST_HEADERS[FREE])
        assert listing.json() == []

    def test_idempotent_duplicate_409_with_existing_key(self, free_env, fake_fetch):
        first = _create(free_env.client, FREE)
        assert first.status_code == 201
        key = first.json()["key"]
        second = _create(free_env.client, FREE, name="第二集")
        assert second.status_code == 409
        assert key in second.json()["detail"]

    def test_no_data_422_truthful(self, free_env, fake_fetch):
        fake_fetch["rows"] = []
        resp = _create(free_env.client, FREE)
        assert resp.status_code == 422
        assert "无数据" in resp.json()["detail"]

    def test_upstream_unreachable_502(self, free_env, fake_fetch):
        from fd_wire_api.datasets.ingest import EngineUnavailable

        fake_fetch["side_effect"] = EngineUnavailable("指标拉数端点不可用")
        resp = _create(free_env.client, FREE)
        assert resp.status_code == 502
        assert resp.json()["detail"].startswith("engine_unavailable")

    def test_identity_rejection_422(self, free_env, fake_fetch):
        fake_fetch["error"] = "source_db wenshu_db 暂不支持序列拉取"
        resp = _create(free_env.client, FREE)
        assert resp.status_code == 422
        assert "暂不支持序列拉取" in resp.json()["detail"]

    def test_range_validation_before_fetch(self, free_env, fake_fetch):
        resp = _create(free_env.client, FREE, start="1900", end="2024")
        assert resp.status_code == 422
        assert fake_fetch["calls"] == []  # 超限绝不发起上游拉取

        resp2 = _create(free_env.client, FREE, start="2024", end="2020")
        assert resp2.status_code == 422
        assert fake_fetch["calls"] == []

    def test_bad_range_format_422(self, free_env, fake_fetch):
        resp = _create(free_env.client, FREE, start="not-a-date", end="2024")
        assert resp.status_code == 422

    def test_missing_indicator_id_400(self, free_env, fake_fetch):
        resp = _create(free_env.client, FREE, indicator_id=None)
        assert resp.status_code == 400

    def test_missing_entity_400(self, free_env, fake_fetch):
        resp = _create(free_env.client, FREE, entity_id=None)
        assert resp.status_code == 400

    def test_cross_customer_isolation(self, free_env, fake_fetch):
        first = _create(free_env.client, FREE)
        assert first.status_code == 201
        # 另一客户同指标建集不受幂等挡（幂等按 customer_id 隔离）
        resp = _create(free_env.client, PAID)
        assert resp.status_code == 201


# ------------------------------------------------------------------ cell 档（引擎 ingest 三段）

class TestCellTier:
    def test_create_via_engine_ingest(self, datasets_env, fake_fetch):
        resp = _create(datasets_env.client, PAID)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["source_kind"] == "indicator"
        assert body["storage_tier"] == "cell"
        assert body["row_count"] == 3
        begin = datasets_env.fake.calls_for("customer_dataset_ingest_begin")[0]
        assert begin["pk_columns"] == ["date"]
        assert begin["row_count"] == 3
        chunks = datasets_env.fake.calls_for("customer_dataset_ingest_chunk")
        assert sum(len(c["rows"]) for c in chunks) == 3
        commit = datasets_env.fake.calls_for("customer_dataset_ingest_commit")
        assert len(commit) == 1
        aborts = datasets_env.fake.calls_for("customer_dataset_ingest_abort")
        assert aborts == []

    def test_engine_failure_aborts_and_502(self, datasets_env, fake_fetch):
        from fd_wire_api.datasets.ingest import EngineCallError

        # commit 阶段失败：abort 必须已被调用，登记行零残留
        def boom(tool, arguments):
            datasets_env.fake.calls.append((tool, dict(arguments)))
            if tool == "customer_dataset_ingest_commit":
                raise EngineCallError("引擎提交失败")
            if tool == "customer_dataset_ingest_begin":
                return {"ingest_id": "ing-x"}
            if tool == "customer_dataset_ingest_chunk":
                return {"accepted": len(arguments["rows"])}
            if tool == "customer_dataset_ingest_abort":
                return {"aborted": True}
            return {}

        datasets_env.app.dependency_overrides[
            __import__("fd_wire_api.datasets.routes", fromlist=["get_engine_client_factory"])
            .get_engine_client_factory
        ] = lambda: lambda cid: type("F", (), {"call": staticmethod(boom)})()
        try:
            resp = _create(datasets_env.client, PAID)
            assert resp.status_code == 502
            tools = [name for name, _ in datasets_env.fake.calls]
            assert "customer_dataset_ingest_abort" in tools
            listing = datasets_env.client.get(
                "/api/v1/datasets", headers=CUST_HEADERS[PAID])
            assert listing.json() == []
        finally:
            datasets_env.app.dependency_overrides.clear()

    def test_ledger_event_recorded(self, datasets_env, fake_fetch):
        from fd_wire_api.datasets.db import get_session
        from fd_wire_api.provenance.models import LedgerEvent

        resp = _create(datasets_env.client, PAID)
        assert resp.status_code == 201
        dataset_key = resp.json()["key"]
        with get_session() as sess:
            ev = sess.execute(
                select_event(dataset_key)
            ).scalars().first()
        assert ev is not None
        assert ev.type == "dataset.indicator.registered"
        assert ev.payload["indicator_id"] == INDICATOR_ID
        # 时间线契约：dataset 事件 subject 带 key（routes 的 timeline 端点按
        # subject.key 过滤；带 id 不带 key 的事件在时间线上不可见）
        assert ev.subject["kind"] == "dataset"
        assert ev.subject["key"] == dataset_key


def select_event(dataset_key):
    from sqlalchemy import select

    from fd_wire_api.provenance.models import LedgerEvent

    return select(LedgerEvent).where(
        LedgerEvent.type == "dataset.indicator.registered",
        LedgerEvent.subject["key"].as_string() == dataset_key,
    )
