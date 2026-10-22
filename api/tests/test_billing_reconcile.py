"""对账：账本（权威）vs sub2api 快照 → 差异报告；只读不改账。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fd_wire_api.billing import (
    Ledger,
    Plan,
    SettlementService,
    SqlMeteringSource,
    reconcile,
    seed_plans,
    yuan_to_micro,
)

from test_billing_common import (
    FakeSub2Api,
    add_customer,
    insert_metering,
    make_engine,
)

PERIOD = "2026-09"
TS = "2026-09-15 10:00:00+00:00"
OPENING = 10_000_000  # 预存 ¥10


def settled_env(overage_yuan: str = "400", *, usage: int = 600_000):
    """结算一单超额（starter：¥400），返回 (session, fake, customer)。"""
    engine = make_engine()
    fake = FakeSub2Api()
    session = Session(engine)
    plans = seed_plans(session)
    customer = add_customer(session, name="壹座", plan_id=plans["starter"].id,
                            sub2api_user_id="u-1")
    fake.add_user("u-1", balance_micro=OPENING)
    session.commit()
    insert_metering(engine, customer_ref="u-1", ts=TS, rows=usage)
    service = SettlementService(session, fake.client(), SqlMeteringSource(engine))
    report = service.settle(PERIOD)
    assert report.results[0].ok
    return session, fake, customer


def test_reconcile_matched_no_diff() -> None:
    session, fake, customer = settled_env()
    report = reconcile(session, fake.client(),
                       opening_balances={customer.id: OPENING})
    assert report.checked == 1 and report.matched == 1
    assert report.mismatches == [] and report.errors == []
    row = report.rows[0]
    assert row.ledger_applied_micro == yuan_to_micro("400")
    assert row.expected_micro == OPENING - yuan_to_micro("400")
    assert row.difference_micro == 0


def test_reconcile_detects_difference_and_does_not_mutate_ledger() -> None:
    session, fake, customer = settled_env()
    # 支付侧被人为动过：比账本口径多扣 ¥10
    fake.users["u-1"]["balance_micro"] = OPENING - yuan_to_micro("410")
    entries_before = [
        (e.id, e.kind, e.amount_micro) for e in Ledger(session).entries(customer.id)
    ]

    report = reconcile(session, fake.client(),
                       opening_balances={customer.id: OPENING})

    assert report.mismatches and report.matched == 0
    diff = report.mismatches[0]
    assert diff.customer_id == customer.id
    assert diff.snapshot_micro == OPENING - yuan_to_micro("410")
    assert diff.difference_micro == -yuan_to_micro("10")  # 快照比账本少 ¥10
    # 只读：对账后账本原封不动
    entries_after = [
        (e.id, e.kind, e.amount_micro) for e in Ledger(session).entries(customer.id)
    ]
    assert entries_after == entries_before
    assert Ledger(session).applied_total(customer.id) == yuan_to_micro("400")


def test_reconcile_unreachable_billing_plane_is_error_row() -> None:
    session, fake, customer = settled_env()
    fake.fail_reads_left = 9  # 快照读失败（fail-closed：不猜）
    report = reconcile(session, fake.client(),
                       opening_balances={customer.id: OPENING})
    assert report.checked == 1 and report.matched == 0
    row = report.rows[0]
    assert not row.reachable and row.snapshot_micro is None and row.difference_micro is None
    assert row.error and row.is_mismatch is False  # 差异≠错误，分开呈报


def test_reconcile_demand_not_counted_but_applied_adjustment_is() -> None:
    session, fake, customer = settled_env()
    ledger = Ledger(session)
    # 需求单独立记账（不进 sub2api 口径）
    ledger.record_demand(customer_id=customer.id, period=PERIOD,
                         amount_micro=yuan_to_micro("12000"), demand_ref="DM-1")
    session.commit()
    report = reconcile(session, fake.client(),
                       opening_balances={customer.id: OPENING})
    assert report.matched == 1  # 需求单不影响期望余额

    # 人工调整 + 手工执行过 sub2api（−¥100）
    ledger.record_adjustment(customer_id=customer.id, period=PERIOD,
                             amount_micro=yuan_to_micro("100"),
                             source_ref="OPS-77", sub2api_applied=True)
    session.commit()
    fake.users["u-1"]["balance_micro"] -= yuan_to_micro("100")
    report = reconcile(session, fake.client(),
                       opening_balances={customer.id: OPENING})
    assert report.matched == 1
    assert report.rows[0].ledger_applied_micro == yuan_to_micro("500")


def test_reconcile_period_scope_and_unbound_customers_ignored() -> None:
    session, fake, customer = settled_env()
    # 未绑定 sub2api 的客户不参与对账
    plans = {p.code: p for p in session.query(Plan).all()}
    add_customer(session, name="未绑定", plan_id=plans["starter"].id,
                 sub2api_user_id=None)
    session.commit()

    report = reconcile(session, fake.client(),
                       opening_balances={customer.id: OPENING})
    assert report.checked == 1  # 只有绑定支付账户的客户

    scoped = reconcile(session, fake.client(), period=PERIOD,
                       opening_balances={customer.id: OPENING})
    assert scoped.period == PERIOD and scoped.matched == 1
    other = reconcile(session, fake.client(), period="2026-08",
                      opening_balances={customer.id: OPENING})
    assert other.rows[0].ledger_applied_micro == 0  # 8 月无账 → 期望=期初
    # 快照是全时点余额（含 9 月扣减），按 8 月口径对账自然呈差
    assert other.rows[0].difference_micro == -yuan_to_micro("400")
