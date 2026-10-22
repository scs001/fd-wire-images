"""入账驱动：poll→settle 一轮全链、未配置跳过、降级重放不双扣、段间隔离、开关。

真实表 + 假 Mongo collection + FakeSub2Api（含幂等缓存与故障注入）——
不打真库不打真计费面；事件形状对齐 make_event。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import select

from fd_wire_api.billing import Customer, LedgerEntry, LedgerKind, init_billing, seed_plans
from fd_wire_api.metering import driver as driver_mod
from fd_wire_api.metering.driver import _closed_period, start_metering_driver, tick_once
from fd_wire_api.metering.db import get_session

from test_billing_common import FakeSub2Api, add_customer
from test_metering_poller import FakeCollection
from test_metering_support import make_event, metering_env

USER = "cust-1@customer.com"
OPENING = 10_000_000  # 预存 ¥10
OCT = datetime(2026, 10, 2, tzinfo=timezone.utc)  # 闭月聚合域 = 2026-09


@contextmanager
def driver_env(tmp_path: Any, monkeypatch, **overrides: str | None):
    """metering_env + billing 表/套餐 + 一个 starter 客户（=事件 username）。"""
    env = {"SUB2API_ADMIN_KEY": None, **overrides}
    with metering_env(tmp_path, monkeypatch, **env) as client:
        init_billing()  # billing_* 落同一 DATABASE_URL
        fake = FakeSub2Api()
        session = get_session()
        plans = seed_plans(session)
        session.commit()
        add_customer(session, name="客户A", plan_id=plans["starter"].id, sub2api_user_id=USER)
        fake.add_user(USER, balance_micro=OPENING)
        session.commit()
        session.close()
        yield fake, client


def _events(n: int = 1, rows: int = 600_000, month_ts: str = "2026-09") -> list[dict[str, Any]]:
    """n 条客户轨道事件（默认落闭月 2026-09 聚合域），rows 合计必超 starter 含额。"""
    return [
        make_event(request_id=f"r-{i}", username=USER, rows=rows,
                   ts=f"{month_ts}-01T10:0{i}:00+00:00")
        for i in range(n)
    ]


def ledger_entries(kind: str) -> list[LedgerEntry]:
    with get_session() as session:
        return list(
            session.execute(select(LedgerEntry).where(LedgerEntry.kind == kind)).scalars()
        )


def test_tick_full_pipe_polls_ingests_and_settles(tmp_path, monkeypatch) -> None:
    """一轮：事件经假 Mongo 摄取落表 → 当月聚合 → 超额入账 sub2api 恰一次。"""
    with driver_env(tmp_path, monkeypatch) as (fake, _client):
        outcome = tick_once(
            collection=FakeCollection(_events(rows=600_000)),
            sub2api=fake.client(),
            now=OCT,
        )
        assert outcome["polled"]["accepted"] == 1
        assert outcome["settled"]["ok"] is True
        assert outcome["settled"]["failures"] == 0
        grants = ledger_entries(LedgerKind.GRANT)
        overages = ledger_entries(LedgerKind.OVERAGE)
        assert len(grants) == 1  # 月度套餐额度条目
        assert len(overages) == 1 and overages[0].sub2api_applied is True
        assert len(fake.adjust_calls) == 1
        assert fake.balance(USER) < OPENING  # 超额已扣


def test_tick_replay_same_domain_does_not_double_charge(tmp_path, monkeypatch) -> None:
    """重跑同聚合域：账本 settle 键短路，sub2api 零新调用、余额不再动。"""
    with driver_env(tmp_path, monkeypatch) as (fake, _client):
        events = _events(rows=600_000)
        first = tick_once(collection=FakeCollection(events), sub2api=fake.client(), now=OCT)
        assert first["settled"]["ok"] is True
        balance_after_first = fake.balance(USER)
        second = tick_once(collection=FakeCollection(events), sub2api=fake.client(), now=OCT)
        assert second["polled"]["duplicates"] == 1  # 幂等去重（重叠窗重拉）
        assert second["settled"]["ok"] is True
        assert fake.balance(USER) == balance_after_first
        assert len(ledger_entries(LedgerKind.OVERAGE)) == 1


def test_tick_degraded_sub2api_then_replay_same_key(tmp_path, monkeypatch) -> None:
    """降级自愈：adjust 503 → 不写超额账；恢复后同幂等键重放，净一次扣减。"""
    with driver_env(tmp_path, monkeypatch) as (fake, _client):
        fake.fail_adjust_left = 1
        degraded = tick_once(
            collection=FakeCollection(_events(rows=600_000)), sub2api=fake.client(), now=OCT
        )
        assert degraded["settled"]["ok"] is False
        assert degraded["settled"]["failures"] == 1
        assert ledger_entries(LedgerKind.OVERAGE) == []  # 失败不落账
        assert fake.balance(USER) == OPENING
        recovered = tick_once(
            collection=FakeCollection(_events(rows=600_000)), sub2api=fake.client(), now=OCT
        )
        assert recovered["settled"]["ok"] is True
        overages = ledger_entries(LedgerKind.OVERAGE)
        assert len(overages) == 1 and overages[0].sub2api_applied is True
        # 两次 POST 打到计费面（一次 503 + 一次成功），幂等缓存保证只扣一次
        assert len(fake.adjust_calls) == 2
        assert fake.balance(USER) < OPENING


def test_tick_poll_crash_does_not_block_settle(tmp_path, monkeypatch) -> None:
    """段间隔离：poll 段炸（Mongo 故障）只记日志，settle 照常入账。"""
    class BadCollection:
        def find(self, *a, **k):
            raise RuntimeError("mongo down")

    with driver_env(tmp_path, monkeypatch) as (fake, _client):
        # 先经好 collection 摄取，再用坏 collection 触发 poll 段异常
        tick_once(collection=FakeCollection(_events(rows=600_000)), sub2api=fake.client(), now=OCT)
        outcome = tick_once(collection=BadCollection(), sub2api=fake.client(), now=OCT)
        assert outcome["polled"] is None  # 异常被段内吞掉，不外抛
        assert outcome["settled"]["ok"] is True


def test_tick_skips_when_unconfigured(tmp_path, monkeypatch) -> None:
    """未配置即静默跳过：无 Mongo 配置、无 SUB2API_ADMIN_KEY、无注入。"""
    with driver_env(tmp_path, monkeypatch) as (_fake, _client):
        outcome = tick_once(collection=None, sub2api=None, now=OCT)
        assert outcome["polled"] is None
        assert outcome["settled"] is None


def test_closed_period_previous_month() -> None:
    assert _closed_period(datetime(2026, 10, 2, tzinfo=timezone.utc)) == "2026-09"
    assert _closed_period(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)) == "2025-12"


def test_current_month_deferred_metered_not_settled(tmp_path, monkeypatch) -> None:
    """当月事件只计量不结算：闭月语义——月内消费靠预检/预警兜底，rollover 结清。"""
    with driver_env(tmp_path, monkeypatch) as (fake, _client):
        outcome = tick_once(
            collection=FakeCollection(_events(rows=600_000, month_ts="2026-10")),
            sub2api=fake.client(),
            now=OCT,
        )
        assert outcome["polled"]["accepted"] == 1  # 计量已落库
        assert outcome["settled"]["customers"] == 0  # 闭月（2026-09）无用量
        assert ledger_entries(LedgerKind.GRANT) == []
        assert fake.adjust_calls == []
        assert fake.balance(USER) == OPENING


def test_start_disabled_is_noop(tmp_path, monkeypatch) -> None:
    import asyncio

    async def _call() -> None:
        start_metering_driver()  # 需在事件循环内（与 app lifespan 同约束）

    with driver_env(tmp_path, monkeypatch, METERING_DRIVER_ENABLED="false") as (_fake, _c):
        asyncio.run(_call())
        assert driver_mod._task is None
