"""WAL 转发器测试（wire-provenance-ledger 任务 3.3）。

不打真引擎：``FakeWalEngine`` 按 ack-游标语义吐页（已吐且已 ack 的不再
吐，吐干后回空页 last_seq 回落 after_seq），经 monkeypatch 注入
``forwarder.get_engine_client_factory``。覆盖：

- 两页 WAL 全部入账、ack 收到正确 upto、账本 event_id 与 WAL 一致；
- off 档：事件不入账但 ack 照发（不留积压）；
- 崩溃语义：中途 append 异常 → ack 只到最后成功条；重跑后失败条补入
  且账本按 event_id 无重复；
- 旧引擎（wal_pending 抛 McpTransportError/工具不存在）→ errors 有记录、
  不殃及同轮其它客户、绝不抛出；
- 数据集行缺失按默认档 chain_only 入账（不静默丢事件）；
- 后台循环 start/stop：起一次立刻停，task 不泄漏。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fd_mcp_client import McpTransportError
from sqlalchemy import select

from fd_wire_api.ai.settings import get_settings as get_ai_settings
from fd_wire_api.datasets.db import get_session, init_db, reset_datasets_runtime
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.provenance import envelope, forwarder, reset_provenance_runtime
from fd_wire_api.provenance.forwarder import (
    TOOL_WAL_ACK,
    TOOL_WAL_PENDING,
    forward_once,
    start_forwarder_loop,
    stop_forwarder_loop,
)
from fd_wire_api.provenance.ledger import verify_chain
from fd_wire_api.provenance.models import LedgerEvent


@pytest.fixture()
def wal_env(tmp_path, monkeypatch):
    """隔离运行时（内存库 + 账本表/触发器就绪）；转发器直调不经 HTTP。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("FD_WIRE_PROVENANCE_FORWARD_INTERVAL_SECONDS", raising=False)
    reset_datasets_runtime()
    reset_provenance_runtime()
    init_db()
    from fd_wire_api.provenance import init_provenance_db

    init_provenance_db()
    yield SimpleNamespace(monkeypatch=monkeypatch)
    reset_provenance_runtime()
    reset_datasets_runtime()


def make_dataset(
    customer: str = "cust-001", key: str = "sales_2026", tier: str = "chain_only"
) -> None:
    db = get_session()
    try:
        db.add(
            Dataset(
                customer_id=customer,
                key=key,
                name=key,
                source_kind="upload",
                provenance_tier=tier,
            )
        )
        db.commit()
    finally:
        db.close()


def wal_event(seq: int, dataset_key: str = "sales_2026", **extra) -> dict:
    """一条引擎 WAL 事件（契约形态：seq/event_id/type/payload/created_at）。"""
    payload = {
        "dataset_key": dataset_key,
        "op": "update",
        "anchor": f"pk:{seq}",
        "correction_id": seq,
        "actor": "customer:cust-001",
    }
    payload.update(extra)
    return {
        "seq": seq,
        "event_id": f"evt_wal_{seq}",
        "type": "dataset.correction.added",
        "payload": payload,
        "created_at": f"2026-10-07T00:00:{seq:02d}Z",
    }


class FakeWalEngine:
    """引擎 WAL 工具假实现：按序吐页，吐干回空页（last_seq 回落 after_seq）。

    ack-游标语义由用例编排体现：已成功 ack 的页下一轮不再编排即等效于
    引擎侧只返回未 ack。
    """

    def __init__(self, *pages: dict) -> None:
        self.pages = [dict(page) for page in pages]
        self.pending_calls: list[dict] = []
        self.ack_calls: list[int] = []
        self.pending_error: Exception | None = None

    def call(self, tool: str, arguments: dict) -> dict:
        if tool == TOOL_WAL_PENDING:
            self.pending_calls.append(dict(arguments))
            if self.pending_error is not None:
                raise self.pending_error
            if self.pages:
                page = self.pages.pop(0)
                return {"events": page["events"], "last_seq": page["last_seq"]}
            return {"events": [], "last_seq": arguments.get("after_seq", 0)}
        if tool == TOOL_WAL_ACK:
            self.ack_calls.append(arguments["upto_seq"])
            return {"acked": 1}
        raise AssertionError(f"意外的引擎工具调用: {tool}")


def patch_engines(monkeypatch, engine_map: dict[str, FakeWalEngine]) -> None:
    """注入假引擎工厂 + endpoint map（与 ingest 通道同源的枚举口径）。"""
    monkeypatch.setattr(
        get_ai_settings(),
        "ai_cell_endpoint_map",
        ",".join(
            f"{cid}=https://mcp.test/fd-wire-cell-{cid}/mcp" for cid in engine_map
        ),
    )

    def fake_factory(customer_id: str) -> FakeWalEngine:
        return engine_map[customer_id]

    monkeypatch.setattr(forwarder, "get_engine_client_factory", lambda: fake_factory)


def ledger_rows(ledger: str) -> list[LedgerEvent]:
    db = get_session()
    try:
        return list(
            db.execute(
                select(LedgerEvent)
                .where(LedgerEvent.ledger == ledger)
                .order_by(LedgerEvent.seq)
            )
            .scalars()
        )
    finally:
        db.close()


# ------------------------------------------------------------- 两页拉干 + ack


def test_two_pages_all_appended_and_acked(wal_env) -> None:
    make_dataset()
    engine = FakeWalEngine(
        {"events": [wal_event(1), wal_event(2)], "last_seq": 2},
        {"events": [wal_event(3), wal_event(4)], "last_seq": 4},
    )
    patch_engines(wal_env.monkeypatch, {"cust-001": engine})

    stats = forward_once(get_session())

    assert stats.customers == 1
    assert stats.appended == 4
    assert stats.skipped_off == 0
    assert stats.errors == []
    assert stats.acked_upto == {"cust-001": 4}
    # 游标在引擎侧：恒 after_seq=0 拉取，ack 推进；两页后拉干（共 3 次 pending）
    assert engine.pending_calls == [
        {"after_seq": 0, "limit": 200},
    ] * 3
    assert engine.ack_calls == [2, 4]

    rows = ledger_rows("cust-001")
    assert [r.id for r in rows] == [f"evt_wal_{i}" for i in range(1, 5)]  # WAL 同源 id
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    first = rows[0]
    assert first.type == "dataset.correction.added"  # 事件 type 原样透传
    assert first.actor == {"kind": "customer", "id": "customer:cust-001", "sig": None}
    assert first.subject == {"kind": "dataset", "key": "sales_2026"}
    assert first.payload == wal_event(1)["payload"]  # payload 原样入账
    assert all(
        envelope.payload_hash(r.payload) == r.payload_hash for r in rows
    )  # 负载哈希可复算
    chain = verify_chain(get_session(), "cust-001")  # 链完整
    assert (chain.ok, chain.checked) == (True, 4)


def test_dataset_row_missing_appends_with_default_tier(wal_env) -> None:
    """数据集行缺失（已删除等）：按默认档 chain_only 入账，不静默丢事件。"""
    engine = FakeWalEngine({"events": [wal_event(1, dataset_key="ghost")], "last_seq": 1})
    patch_engines(wal_env.monkeypatch, {"cust-001": engine})

    stats = forward_once(get_session())

    assert stats.appended == 1
    assert stats.acked_upto == {"cust-001": 1}
    rows = ledger_rows("cust-001")
    assert [r.id for r in rows] == ["evt_wal_1"]
    assert rows[0].subject == {"kind": "dataset", "key": "ghost"}


# ------------------------------------------------------------- off 档


def test_off_tier_skips_append_but_acks(wal_env) -> None:
    make_dataset(tier="off")
    engine = FakeWalEngine({"events": [wal_event(1), wal_event(2)], "last_seq": 2})
    patch_engines(wal_env.monkeypatch, {"cust-001": engine})

    stats = forward_once(get_session())

    assert stats.appended == 0
    assert stats.skipped_off == 2
    assert stats.errors == []
    assert ledger_rows("cust-001") == []  # 不入账
    assert engine.ack_calls == [2]  # ack 照发（引擎侧不留积压）
    assert stats.acked_upto == {"cust-001": 2}


# ------------------------------------------------------------- 崩溃语义


def test_partial_failure_acks_last_success_then_retry_dedups(wal_env) -> None:
    make_dataset()
    engine = FakeWalEngine({"events": [wal_event(1), wal_event(2)], "last_seq": 2})
    patch_engines(wal_env.monkeypatch, {"cust-001": engine})

    original_append = forwarder.append_event

    def flaky_append(db, **kwargs):
        if kwargs.get("event_id") == "evt_wal_2":
            raise RuntimeError("账本瞬时故障")
        return original_append(db, **kwargs)

    wal_env.monkeypatch.setattr(forwarder, "append_event", flaky_append)
    first = forward_once(get_session())

    assert first.appended == 1  # 第一条已落账
    assert first.acked_upto == {"cust-001": 1}
    assert len(first.errors) == 1 and "evt_wal_2" in first.errors[0]
    assert engine.ack_calls == [1]  # ack 只到第一条
    assert [r.id for r in ledger_rows("cust-001")] == ["evt_wal_1"]

    # 下一轮重拉（引擎只回未 ack）：evt_wal_1 幂等重放、evt_wal_2 补入
    wal_env.monkeypatch.setattr(forwarder, "append_event", original_append)
    engine.pages = [{"events": [wal_event(1), wal_event(2)], "last_seq": 2}]
    second = forward_once(get_session())

    assert second.appended == 1  # 重放不计新增
    assert second.errors == []
    assert second.acked_upto == {"cust-001": 2}
    assert engine.ack_calls == [1, 2]
    rows = ledger_rows("cust-001")
    assert [r.id for r in rows] == ["evt_wal_1", "evt_wal_2"]  # 无重复
    chain = verify_chain(get_session(), "cust-001")
    assert (chain.ok, chain.checked) == (True, 2)


# ------------------------------------------------------------- 旧引擎 / 不可达


def test_old_engine_errors_recorded_and_never_raises(wal_env) -> None:
    make_dataset(customer="cust-001")
    make_dataset(customer="cust-002", key="sales_2026")
    healthy = FakeWalEngine({"events": [wal_event(1)], "last_seq": 1})
    old = FakeWalEngine()
    old.pending_error = McpTransportError("tools/call: 引擎端点返回 404（无该工具）")
    patch_engines(wal_env.monkeypatch, {"cust-001": healthy, "cust-002": old})

    stats = forward_once(get_session())  # 绝不抛出

    assert stats.customers == 2
    assert len(stats.errors) == 1
    assert "cust-002" in stats.errors[0] and "wal_pending" in stats.errors[0]
    assert stats.acked_upto == {"cust-001": 1}  # 同轮其它客户不受牵连
    assert [r.id for r in ledger_rows("cust-001")] == ["evt_wal_1"]
    assert ledger_rows("cust-002") == []


# ------------------------------------------------------------- 后台循环


def test_interval_seconds_env_overridable(wal_env) -> None:
    assert forwarder._interval_seconds() == 30  # 缺省
    wal_env.monkeypatch.setenv("FD_WIRE_PROVENANCE_FORWARD_INTERVAL_SECONDS", "5")
    assert forwarder._interval_seconds() == 5
    wal_env.monkeypatch.setenv("FD_WIRE_PROVENANCE_FORWARD_INTERVAL_SECONDS", "junk")
    assert forwarder._interval_seconds() == 30  # 非法值回落
    wal_env.monkeypatch.setenv("FD_WIRE_PROVENANCE_FORWARD_INTERVAL_SECONDS", "0")
    assert forwarder._interval_seconds() == 30  # 非正值回落


def test_start_stop_loop_no_leaked_task(wal_env) -> None:
    """起一次立刻停：task 收尾完成、模块引用清空、无残留任务。"""
    wal_env.monkeypatch.setenv("FD_WIRE_PROVENANCE_FORWARD_INTERVAL_SECONDS", "30")

    async def scenario() -> None:
        task = start_forwarder_loop()
        assert task is not None and not task.done()
        assert start_forwarder_loop() is task  # 幂等启动
        await asyncio.sleep(0)  # 让循环进入首个 sleep（先睡后跑，无转发发生）
        await stop_forwarder_loop()
        assert task.done()  # 已收尾，不泄漏
        assert forwarder._forwarder_task is None
        await stop_forwarder_loop()  # 幂等停止
        assert asyncio.all_tasks() == {asyncio.current_task()}  # 无残留任务

    asyncio.run(scenario())

    # 事件循环外启动：记日志返回 None，不抛出
    assert start_forwarder_loop() is None
    assert forwarder._forwarder_task is None
