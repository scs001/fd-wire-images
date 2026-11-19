"""两档预警：80%/95% 各一次、不重发、阈值边界整数判定。"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from fd_wire_api.billing import (
    Customer,
    NOTICE_CHANNELS,
    NoticeRecord,
    evaluate_usage_warnings,
    seed_plans,
)

from test_billing_common import add_customer, make_engine

PERIOD = "2026-09"
INCLUDED = 500_000  # starter


def notices_env():
    engine = make_engine()
    session = Session(engine)
    plans = seed_plans(session)
    customer = add_customer(session, name="壹座", plan_id=plans["starter"].id,
                            sub2api_user_id="u-1")
    session.commit()
    return session, customer


def rows_for(session: Session, customer: Customer) -> list[NoticeRecord]:
    return (
        session.query(NoticeRecord)
        .filter_by(customer_id=customer.id)
        .order_by(NoticeRecord.id)
        .all()
    )


def test_tier_80_boundary_exact() -> None:
    session, customer = notices_env()
    sent = evaluate_usage_warnings(session, customer=customer, period=PERIOD,
                                   used_lines=400_000, included_lines=INCLUDED)
    assert [n.threshold_pct for n in sent] == [80]  # 恰好 80% 触发
    assert len(rows_for(session, customer)) == 1


def test_below_80_no_notice() -> None:
    session, customer = notices_env()
    sent = evaluate_usage_warnings(session, customer=customer, period=PERIOD,
                                   used_lines=399_999, included_lines=INCLUDED)
    assert sent == []
    assert rows_for(session, customer) == []


def test_crossing_both_tiers_in_one_eval_sends_each_once() -> None:
    session, customer = notices_env()
    sent = evaluate_usage_warnings(session, customer=customer, period=PERIOD,
                                   used_lines=476_000, included_lines=INCLUDED)  # 95.2%
    assert [n.threshold_pct for n in sent] == [80, 95]

    # 重放/续涨：两档都已发过 → 不重发
    again = evaluate_usage_warnings(session, customer=customer, period=PERIOD,
                                    used_lines=490_000, included_lines=INCLUDED)
    assert again == []
    assert len(rows_for(session, customer)) == 2


def test_notice_fields_channels_and_payload() -> None:
    session, customer = notices_env()
    evaluate_usage_warnings(session, customer=customer, period=PERIOD,
                            used_lines=475_000, included_lines=INCLUDED)  # 恰 95%
    notice = rows_for(session, customer)[-1]
    assert notice.kind == "usage_warning" and notice.threshold_pct == 95
    assert notice.channels == "mail+inapp" == NOTICE_CHANNELS
    payload = json.loads(notice.payload_json)
    assert payload == {
        "used_lines": 475_000,
        "included_lines": INCLUDED,
        "ratio": "0.9500",
        "period": PERIOD,
    }


def test_zero_quota_guard_and_period_isolated() -> None:
    session, customer = notices_env()
    assert evaluate_usage_warnings(session, customer=customer, period=PERIOD,
                                   used_lines=1, included_lines=0) == []
    sent = evaluate_usage_warnings(session, customer=customer, period="2026-10",
                                   used_lines=410_000, included_lines=INCLUDED)
    assert [n.threshold_pct for n in sent] == [80]  # 新周期重新可发
    # 9 月（0 额度那次没发）与 10 月各一条
    assert [n.period for n in rows_for(session, customer)] == ["2026-10"]
