"""结算执行器：阶梯计价 / 额度内不超额 / 幂等重放不双扣不双记 / 失败自愈。

真实 metering_records 表（caller/event_timestamp/credential_track）按整月
聚合域结算（batch_ref=None）；批次切分语义在协议层用 Static 验证。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from fd_wire_api.billing import (
    Customer,
    Ledger,
    LedgerEntry,
    LedgerKind,
    MeteringUsage,
    NoticeRecord,
    SettlementService,
    SqlMeteringSource,
    StaticMeteringSource,
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
TS = "2026-09-15 10:00:00+00:00"  # 落在 2026-09 聚合域
OPENING = 10_000_000  # 预存 ¥10
SOURCE_REF = f"period:{PERIOD}"


def build_env(rows_by_ref: dict[str, int], *, plan_by_ref: dict[str, str] | None = None,
              fake: FakeSub2Api | None = None, ts: str = TS):
    """rows_by_ref: caller（=sub2api_user_id）→ 客户凭据轨道行数。"""
    engine = make_engine()
    fake = fake or FakeSub2Api()
    session = Session(engine)
    plans = seed_plans(session)
    session.commit()
    plan_lookup = plan_by_ref or {}
    for ref in rows_by_ref:
        plan_code = plan_lookup.get(ref, "starter")
        add_customer(session, name=f"客户-{ref}", plan_id=plans[plan_code].id,
                     sub2api_user_id=ref)
        fake.add_user(ref, name=ref, balance_micro=OPENING)
    session.commit()
    for ref, rows in rows_by_ref.items():
        insert_metering(engine, customer_ref=ref, ts=ts, rows=rows)
    service = SettlementService(session, fake.client(), SqlMeteringSource(engine))
    return engine, session, fake, service


def customer_by_ref(session: Session, ref: str) -> Customer:
    return session.execute(
        select(Customer).where(Customer.sub2api_user_id == ref)
    ).scalar_one()


def ledger_count(session: Session, model) -> int:
    return len(list(session.execute(select(model)).scalars()))


def test_overage_ladder_pricing_three_plans() -> None:
    engine, session, fake, service = build_env(
        {"u-starter": 600_000, "u-prof": 3_100_000, "u-ent": 12_500_000},
        plan_by_ref={"u-starter": "starter", "u-prof": "professional",
                     "u-ent": "enterprise"},
    )
    report = service.settle(PERIOD)
    assert all(r.ok for r in report.results), [r.error for r in report.results]
    by_ref = {r.customer_ref: r for r in report.results}

    # 阶梯：starter ¥0.004/行×100k=¥400；prof ¥0.003×100k=¥300；ent ¥0.002×500k=¥1000
    assert by_ref["u-starter"].overage_lines == 100_000
    assert by_ref["u-starter"].overage_amount_micro == yuan_to_micro("400")
    assert by_ref["u-prof"].overage_lines == 100_000
    assert by_ref["u-prof"].overage_amount_micro == yuan_to_micro("300")
    assert by_ref["u-ent"].overage_lines == 500_000
    assert by_ref["u-ent"].overage_amount_micro == yuan_to_micro("1000")

    # sub2api 余额按各自超额扣减（预存 ¥10）
    assert fake.balance("u-starter") == OPENING - yuan_to_micro("400")
    assert fake.balance("u-prof") == OPENING - yuan_to_micro("300")
    assert fake.balance("u-ent") == OPENING - yuan_to_micro("1000")
    assert report.sub2api_call_count == 3

    # 账本：每客户 grant + overage 各一条，金额可追溯到聚合域
    for ref, expected in (("u-starter", "400"), ("u-prof", "300"), ("u-ent", "1000")):
        entries = Ledger(session).entries(customer_by_ref(session, ref).id, PERIOD)
        assert {en.kind for en in entries} == {"grant", "overage"}
        overage = next(en for en in entries if en.kind == "overage")
        assert overage.amount_micro == yuan_to_micro(expected)
        assert overage.sub2api_applied and overage.source_ref == SOURCE_REF


def test_within_quota_no_overage_no_sub2api_call() -> None:
    engine, session, fake, service = build_env({"u-starter": 500_000})
    report = service.settle(PERIOD)
    result = report.results[0]
    assert result.ok and result.overage_lines == 0 and result.overage_amount_micro == 0
    assert report.sub2api_call_count == 0 and len(fake.adjust_calls) == 0
    assert fake.balance("u-starter") == OPENING  # 分文未扣
    assert sorted(result.notice_tiers_sent) == [80, 95]  # 100% 用量两档预警
    entries = Ledger(session).entries(customer_by_ref(session, "u-starter").id, PERIOD)
    assert [en.kind for en in entries] == ["grant"]  # 账单仅含月费


def test_replay_same_period_no_double_charge_no_double_entry() -> None:
    engine, session, fake, service = build_env({"u-prof": 3_400_000},
                                               plan_by_ref={"u-prof": "professional"})
    first = service.settle(PERIOD)
    assert first.results[0].status == "settled"
    entries_before = ledger_count(session, LedgerEntry)
    notices_before = ledger_count(session, NoticeRecord)
    balance_after_first = fake.balance("u-prof")

    second = service.settle(PERIOD)  # 同聚合域重放（重试/重复提交）
    assert second.results[0].status == "replayed"
    assert ledger_count(session, LedgerEntry) == entries_before  # 账本只记一次
    assert fake.balance("u-prof") == balance_after_first  # 不双扣
    assert len(fake.adjust_calls) == 1  # sub2api 只被调一次
    assert second.sub2api_call_count == 0
    assert ledger_count(session, NoticeRecord) == notices_before  # 预警不重发


def test_sub2api_failure_then_retry_self_heals() -> None:
    fake = FakeSub2Api()
    engine, session, _, service = build_env({"u-starter": 600_000}, fake=fake)
    fake.fail_adjust_left = 1  # 第一次 adjust 503 → 计费面不可用语义

    failed = service.settle(PERIOD)
    result = failed.results[0]
    assert result.status == "failed_sub2api" and result.error
    entries = Ledger(session).entries(customer_by_ref(session, "u-starter").id, PERIOD)
    assert [en.kind for en in entries] == ["grant"]  # 失败不写 overage 账
    assert fake.balance("u-starter") == OPENING  # 未扣成

    retried = service.settle(PERIOD)  # 同聚合域重试（同幂等键）
    assert retried.results[0].status == "settled"
    assert fake.balance("u-starter") == OPENING - yuan_to_micro("400")  # 净扣一次
    entries = Ledger(session).entries(customer_by_ref(session, "u-starter").id, PERIOD)
    assert sorted(en.kind for en in entries) == ["grant", "overage"]


def test_crash_between_adjust_and_ledger_write_nets_single_charge() -> None:
    """sub2api 扣成但账本未写（进程崩溃）：重放走同键 → 服务端去重、账本补记。"""
    engine, session, fake, service = build_env({"u-starter": 600_000})
    service.settle(PERIOD)
    customer = customer_by_ref(session, "u-starter")
    overage_id = (
        session.execute(
            select(LedgerEntry).where(
                LedgerEntry.customer_id == customer.id,
                LedgerEntry.kind == LedgerKind.OVERAGE.value,
            )
        ).scalar_one()
        .id
    )
    session.query(LedgerEntry).filter_by(id=overage_id).delete()
    session.commit()  # 模拟：扣减已执行、账本条目丢失
    balance_before = fake.balance("u-starter")
    adjust_calls_before = len(fake.adjust_calls)

    report = service.settle(PERIOD)
    assert report.results[0].status == "settled"  # 补记账
    assert fake.balance("u-starter") == balance_before  # 同键去重——净扣不变
    assert len(fake.adjust_calls) == adjust_calls_before + 1  # 请求重发（可接受）
    overage_entries = [
        en
        for en in Ledger(session).entries(customer.id, PERIOD)
        if en.kind == LedgerKind.OVERAGE.value
    ]
    assert len(overage_entries) == 1


def test_unresolvable_customer_ref_skipped_with_reason() -> None:
    engine = make_engine()
    fake = FakeSub2Api()
    session = Session(engine)
    plans = seed_plans(session)
    add_customer(session, name="已映射", plan_id=plans["starter"].id,
                 sub2api_user_id="u-known")
    fake.add_user("u-known", balance_micro=0)
    session.commit()
    insert_metering(engine, customer_ref="u-known", ts=TS, rows=1_000)
    insert_metering(engine, customer_ref="ghost", ts=TS, rows=9_999)
    service = SettlementService(session, fake.client(), SqlMeteringSource(engine))
    report = service.settle(PERIOD)
    by_ref = {r.customer_ref: r for r in report.results}
    assert by_ref["u-known"].ok
    ghost = by_ref["ghost"]
    assert ghost.status == "skipped_no_customer" and "ghost" in ghost.error
    assert ghost.customer_id is None and len(fake.adjust_calls) == 0


def test_platform_credential_track_not_billed() -> None:
    engine, session, fake, service = build_env({"u-starter": 100_000})
    # 平台服务凭据轨道（AI 探索归集）5 百万行——不得进客户账单
    insert_metering(engine, customer_ref="u-starter", ts=TS, rows=5_000_000,
                    track="platform")
    report = service.settle(PERIOD)
    result = report.results[0]
    assert result.ok and result.overage_lines == 0
    assert len(fake.adjust_calls) == 0  # 5.1M 行里只有 100k 计费——额度内


def test_period_boundary_scoped_by_event_timestamp() -> None:
    """聚合域按 event_timestamp 派生 period：10 月的行不进 9 月账单。"""
    engine, session, fake, service = build_env({"u-starter": 100_000})
    insert_metering(engine, customer_ref="u-starter", ts="2026-10-02 09:00:00+00:00",
                    rows=600_000)
    september = service.settle(PERIOD)
    assert september.results[0].overage_lines == 0  # 9 月只有 100k
    assert len(fake.adjust_calls) == 0
    october = service.settle("2026-10")
    assert october.results[0].overage_lines == 100_000  # 超额记在 10 月
    assert fake.balance("u-starter") == OPENING - yuan_to_micro("400")


def test_inactive_customer_skipped() -> None:
    engine = make_engine()
    fake = FakeSub2Api()
    session = Session(engine)
    plans = seed_plans(session)
    suspended = add_customer(session, name="停用客户", plan_id=plans["starter"].id,
                             sub2api_user_id="u-susp", status="suspended")
    session.commit()
    fake.add_user("u-susp")
    insert_metering(engine, customer_ref="u-susp", ts=TS, rows=600_000)
    service = SettlementService(session, fake.client(), SqlMeteringSource(engine))
    report = service.settle(PERIOD)
    assert report.results[0].status == "skipped_inactive"
    assert len(fake.adjust_calls) == 0
    assert Ledger(session).entries(suspended.id) == []


def test_real_adapter_refuses_batch_scoping() -> None:
    engine = make_engine()
    source = SqlMeteringSource(engine)
    with pytest.raises(ValueError, match="batch_ref"):
        source.aggregates(PERIOD, "b1")


def test_batch_scoping_via_protocol_source() -> None:
    """协议层 batch_ref 语义：不同批次独立幂等键，互不吞并、互不重扣。"""
    engine = make_engine()
    fake = FakeSub2Api()
    session = Session(engine)
    plans = seed_plans(session)
    add_customer(session, name="增量客户", plan_id=plans["starter"].id,
                 sub2api_user_id="u-inc")
    fake.add_user("u-inc", balance_micro=OPENING)
    session.commit()

    b1 = SettlementService(session, fake.client(),
                           StaticMeteringSource([MeteringUsage("u-inc", 300_000)]))
    b2 = SettlementService(session, fake.client(),
                           StaticMeteringSource([MeteringUsage("u-inc", 300_000)]))
    r1 = b1.settle(PERIOD, "b1")
    r1_replay = b1.settle(PERIOD, "b1")  # 同批次重放：不双扣
    r2 = b2.settle(PERIOD, "b2")  # 异批次：各自结算
    assert r1.results[0].overage_lines == 0
    assert r1_replay.results[0].status == "replayed" and len(fake.adjust_calls) == 0
    assert r2.results[0].overage_lines == 0  # 单批 300k 均在额度内
    assert fake.balance("u-inc") == OPENING


@pytest.mark.parametrize("bad", ["2026-9", "2026-13"])
def test_settle_rejects_bad_period(bad: str) -> None:
    engine, session, fake, service = build_env({"u-starter": 1})
    with pytest.raises(ValueError):
        service.settle(bad)


# ---------------------------------------------------------------------------
# 无超额档口径（wire-self-serve-launch 2.3）：公开三档超额单价一律 0——
# 超出含额不产生任何超额账目、不动 sub2api；闭月结算金额=月费（grant）。
# ---------------------------------------------------------------------------


def test_zero_overage_plan_no_overage_entries_no_sub2api_calls() -> None:
    """免费档客户用量超出含额（1,000 行）：无 overage 条目、sub2api 零调用。"""
    engine, session, fake, service = build_env({"u-free": 1_234},
                                               plan_by_ref={"u-free": "free"})
    fake.add_user("u-free", balance_micro=0)  # 免费档 sub2api 余额恒零
    report = service.settle(PERIOD)
    result = report.results[0]
    assert result.ok
    assert result.overage_lines == 234
    assert result.overage_amount_micro == 0
    assert report.sub2api_call_count == 0
    assert len(fake.adjust_calls) == 0
    entries = Ledger(session).entries(customer_by_ref(session, "u-free").id, PERIOD)
    assert [en.kind for en in entries] == ["grant"]  # 无超额账目
    # 闭月结算金额=月费（免费档 ¥0）
    assert entries[0].amount_micro == 0


def test_zero_overage_basic_plan_settlement_amounts_monthly_fee() -> None:
    """基础档：闭月结算账目=grant 一条、金额=月费 ¥10（行数门之外无账目）。"""
    engine, session, fake, service = build_env(
        {"u-basic": 150_000}, plan_by_ref={"u-basic": "basic"}  # 超含额 50k 行
    )
    report = service.settle(PERIOD)
    result = report.results[0]
    assert result.ok and result.overage_lines == 50_000
    assert result.overage_amount_micro == 0
    assert len(fake.adjust_calls) == 0
    entries = Ledger(session).entries(customer_by_ref(session, "u-basic").id, PERIOD)
    assert [en.kind for en in entries] == ["grant"]
    assert entries[0].amount_micro == yuan_to_micro("10")
    assert entries[0].quantity == 100_000
    assert fake.balance("u-basic") == OPENING  # 余额分文未动


def test_zero_overage_settlement_idempotent_replay() -> None:
    """无超额档重放：同月再结算不重复落 grant、不产生超额账目。"""
    engine, session, fake, service = build_env(
        {"u-free": 2_000}, plan_by_ref={"u-free": "free"}
    )
    r1 = service.settle(PERIOD)
    r2 = service.settle(PERIOD)
    assert r1.results[0].ok and r2.results[0].status == "replayed"
    entries = Ledger(session).entries(customer_by_ref(session, "u-free").id, PERIOD)
    assert [en.kind for en in entries] == ["grant"]
