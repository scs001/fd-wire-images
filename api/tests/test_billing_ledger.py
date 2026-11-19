"""账本幂等记账与语义化条目（grant/demand/adjustment/hardstop）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fd_wire_api.billing import (
    Customer,
    Ledger,
    LedgerKind,
    micro_to_yuan,
    seed_plans,
    yuan_to_micro,
)

from test_billing_common import add_customer, make_engine


def ledger_env():
    engine = make_engine()
    session = Session(engine)
    plans = seed_plans(session)
    session.commit()
    customer = add_customer(session, name="壹座", plan_id=plans["starter"].id,
                            sub2api_user_id="u-1")
    session.commit()
    return session, customer


def test_record_replay_same_key_returns_existing_entry() -> None:
    session, customer = ledger_env()
    ledger = Ledger(session)
    r1 = ledger.record(customer_id=customer.id, kind=LedgerKind.OVERAGE,
                       period="2026-09", idempotency_key="settle:2026-09:b1:1",
                       quantity=100_000, amount_micro=yuan_to_micro("400"),
                       source_ref="b1", sub2api_applied=True)
    r2 = ledger.record(customer_id=customer.id, kind=LedgerKind.OVERAGE,
                       period="2026-09", idempotency_key="settle:2026-09:b1:1",
                       quantity=100_000, amount_micro=yuan_to_micro("400"),
                       source_ref="b1", sub2api_applied=True)
    assert r1.created and not r2.created
    assert r1.entry.id == r2.entry.id
    assert len(ledger.entries(customer.id)) == 1  # 重放只记一条


def test_record_race_hits_unique_and_returns_existing() -> None:
    session, customer = ledger_env()
    ledger = Ledger(session)
    r1 = ledger.record(customer_id=customer.id, kind=LedgerKind.GRANT,
                       period="2026-09", idempotency_key="race-key")
    assert r1.created
    session.commit()

    # 模拟竞态：预检 find 未命中（对手已提交同键），插入撞唯一约束 →
    # SAVEPOINT 吞掉 IntegrityError，返回既有条目
    real_find = ledger.find
    seen = {"n": 0}

    def racing_find(key: str):
        seen["n"] += 1
        if seen["n"] == 1:
            return None
        return real_find(key)

    ledger.find = racing_find  # type: ignore[method-assign]
    r2 = ledger.record(customer_id=customer.id, kind=LedgerKind.GRANT,
                       period="2026-09", idempotency_key="race-key")
    assert not r2.created and r2.entry.id == r1.entry.id
    assert len(ledger.entries(customer.id)) == 1


def test_ensure_grant_records_plan_fee_and_quota() -> None:
    session, customer = ledger_env()
    ledger = Ledger(session)
    r = ledger.ensure_grant(customer_id=customer.id, period="2026-09",
                            monthly_price_micro=yuan_to_micro("1999"),
                            included_lines=500_000, source_ref="2026-09")
    assert r.created
    assert r.entry.kind == "grant"
    assert micro_to_yuan(r.entry.amount_micro) == 1999.00
    assert r.entry.quantity == 500_000
    again = ledger.ensure_grant(customer_id=customer.id, period="2026-09",
                                monthly_price_micro=yuan_to_micro("1999"),
                                included_lines=500_000)
    assert not again.created and again.entry.id == r.entry.id


def test_demand_and_adjustment_and_hardstop_entries() -> None:
    session, customer = ledger_env()
    ledger = Ledger(session)
    demand = ledger.record_demand(customer_id=customer.id, period="2026-09",
                                  amount_micro=yuan_to_micro("12000"),
                                  demand_ref="DM-2026-001")
    assert demand.entry.kind == "demand" and demand.entry.source_ref == "DM-2026-001"
    replay = ledger.record_demand(customer_id=customer.id, period="2026-09",
                                  amount_micro=yuan_to_micro("12000"),
                                  demand_ref="DM-2026-001")
    assert not replay.created

    credit = ledger.record_adjustment(customer_id=customer.id, period="2026-09",
                                      amount_micro=-yuan_to_micro("100"),
                                      source_ref="OPS-77", sub2api_applied=True)
    assert credit.entry.amount_micro == -yuan_to_micro("100") and credit.entry.sub2api_applied

    stop = ledger.record_hardstop(customer_id=customer.id, period="2026-09",
                                  ref="hardstop-2026-09-17")
    assert stop.entry.kind == "hardstop" and stop.entry.amount_micro == 0

    with_session_total = ledger.applied_total(customer.id)  # 只含 sub2api_applied
    assert with_session_total == -yuan_to_micro("100")  # demand 不进对账口径

    kinds = [e.kind for e in ledger.entries(customer.id)]
    assert set(kinds) == {"demand", "adjustment", "hardstop"}


def test_record_rejects_bad_period() -> None:
    session, customer = ledger_env()
    ledger = Ledger(session)
    try:
        ledger.record(customer_id=customer.id, kind=LedgerKind.GRANT,
                      period="2026-9", idempotency_key="x")
    except ValueError as exc:
        assert "YYYY-MM" in str(exc)
    else:
        raise AssertionError("period 非法应被拒绝")
