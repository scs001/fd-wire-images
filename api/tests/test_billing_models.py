"""模型与档位种子：四档裁决值、旧三档禁用退场、幂等 seed、唯一约束、period 校验
（wire-self-serve-launch 1.2）。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fd_wire_api.billing import (
    Base,
    LedgerEntry,
    LEGACY_CODES,
    NoticeRecord,
    PLAN_CODES,
    Plan,
    PUBLIC_SEEDS,
    list_public_plans,
    micro_to_yuan,
    seed_plans,
    validate_period,
    yuan_to_micro,
)

from test_billing_common import make_engine


def test_plan_seeds_adjudicated_values() -> None:
    """公开三档裁决值（free ¥0/1,000、basic ¥10/100,000、standard ¥50/1,000,000，
    超额单价一律 0）+ 旧三档数值保留但禁用。"""
    engine = make_engine()
    with Session(engine) as session:
        plans = seed_plans(session)
        session.commit()
        free = plans["free"]
        assert (micro_to_yuan(free.monthly_price_micro), free.included_lines,
                free.overage_unit_price_micro) == (0.00, 1_000, 0.0)
        basic = plans["basic"]
        assert (micro_to_yuan(basic.monthly_price_micro), basic.included_lines,
                basic.overage_unit_price_micro) == (10.00, 100_000, 0.0)
        standard = plans["standard"]
        assert (micro_to_yuan(standard.monthly_price_micro), standard.included_lines,
                standard.overage_unit_price_micro) == (50.00, 1_000_000, 0.0)
        assert all(p.is_public and p.is_active for p in (free, basic, standard))
        # 旧三档：数值保留可审计，但 is_active=False 禁用退场
        starter = plans["starter"]
        assert (micro_to_yuan(starter.monthly_price_micro), starter.included_lines) == (1999.00, 500_000)
        assert yuan_to_micro("0.004") == starter.overage_unit_price_micro == 4_000
        prof = plans["professional"]
        assert (micro_to_yuan(prof.monthly_price_micro), prof.included_lines,
                prof.overage_unit_price_micro) == (6999.00, 3_000_000, 3_000)
        ent = plans["enterprise"]
        assert (micro_to_yuan(ent.monthly_price_micro), ent.included_lines,
                ent.overage_unit_price_micro) == (19999.00, 12_000_000, 2_000)
        assert not any(p.is_active for p in (starter, prof, ent))
        assert PLAN_CODES == ("free", "basic", "standard",
                              "starter", "professional", "enterprise")
        assert LEGACY_CODES == frozenset({"starter", "professional", "enterprise"})
        assert [s.code for s in PUBLIC_SEEDS] == ["free", "basic", "standard"]


def test_seed_plans_idempotent_and_preserves_admin_override() -> None:
    engine = make_engine()
    with Session(engine) as session:
        seed_plans(session)
        session.commit()
    # 后台改价（可配置不发版）
    with Session(engine) as session:
        basic = session.query(Plan).filter_by(code="basic").one()
        basic.monthly_price_micro = yuan_to_micro("12")
        session.commit()
    # 重播种：不覆盖后台改动，也不重复插入
    with Session(engine) as session:
        seed_plans(session)
        session.commit()
        assert session.query(Plan).count() == 6
        basic = session.query(Plan).filter_by(code="basic").one()
        assert basic.monthly_price_micro == yuan_to_micro("12")


def test_seed_idempotent_reapplication_is_noop() -> None:
    """seed 幂等：连跑两次，行数与状态不漂移（1.2 验证点）。"""
    engine = make_engine()
    with Session(engine) as session:
        first = seed_plans(session)
        session.commit()
        second = seed_plans(session)
        session.commit()
        assert set(first) == set(second)
        assert session.query(Plan).count() == 6
        rows = session.query(Plan).order_by(Plan.id).all()
        assert [(r.code, r.is_active, r.is_public) for r in rows] == [
            ("free", True, True),
            ("basic", True, True),
            ("standard", True, True),
            ("starter", False, True),
            ("professional", False, True),
            ("enterprise", False, True),
        ]


def test_seed_repushes_legacy_disabled_even_if_manually_reenabled() -> None:
    """旧档退场是终态：手工复活（is_active=True）会被下一次 seed 压回禁用。"""
    engine = make_engine()
    with Session(engine) as session:
        seed_plans(session)
        session.commit()
        starter = session.query(Plan).filter_by(code="starter").one()
        starter.is_active = True
        session.commit()
    with Session(engine) as session:
        seed_plans(session)
        session.commit()
        starter = session.query(Plan).filter_by(code="starter").one()
        assert starter.is_active is False


def test_public_plan_listing_excludes_disabled_and_private() -> None:
    """公开档位口径：仅 is_public && is_active；旧三档与自定义档不出现（1.3）。"""
    engine = make_engine()
    with Session(engine) as session:
        seed_plans(session)
        session.commit()
        session.add(Plan(code="vip-bespoke", name="定制档", monthly_price_micro=1,
                         included_lines=10, overage_unit_price_micro=0,
                         is_active=True, is_public=False))
        session.commit()
        codes = [p.code for p in list_public_plans(session)]
        assert codes == ["free", "basic", "standard"]
        assert "starter" not in codes and "vip-bespoke" not in codes


def test_disabled_plan_cannot_be_assigned() -> None:
    """禁用档不可指派：get_plan 仍可取到（可审计），但 is_active=False
    ——指派校验在 admin 路由层拒 409（API 面见 test_admin_api）。"""
    engine = make_engine()
    with Session(engine) as session:
        plans = seed_plans(session)
        session.commit()
        assert plans["starter"].is_active is False


def test_ledger_idempotency_key_unique_at_db_level() -> None:
    engine = make_engine()
    with Session(engine) as session:
        session.add(LedgerEntry(customer_id=1, kind="grant", period="2026-09",
                                idempotency_key="k1"))
        session.flush()
        session.add(LedgerEntry(customer_id=1, kind="grant", period="2026-09",
                                idempotency_key="k1"))
        with pytest.raises(IntegrityError):
            session.flush()


def test_notice_unique_once_per_tier() -> None:
    engine = make_engine()
    with Session(engine) as session:
        session.add(NoticeRecord(customer_id=1, period="2026-09",
                                 threshold_pct=80))
        session.flush()
        # 撞唯一约束：包 SAVEPOINT，外层事务继续
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(NoticeRecord(customer_id=1, period="2026-09",
                                         threshold_pct=80))
        # 同周期另一档位可再发
        session.add(NoticeRecord(customer_id=1, period="2026-09",
                                 threshold_pct=95))
        session.flush()


@pytest.mark.parametrize("bad", ["2026-1", "2026-13", "202609", "2026-00", "", None, 202609])
def test_validate_period_rejects(bad) -> None:
    with pytest.raises(ValueError):
        validate_period(bad)


def test_validate_period_accepts() -> None:
    assert validate_period("2026-09") == "2026-09"
    assert validate_period("2026-12") == "2026-12"


def test_billing_tables_created_and_metering_kept_separate() -> None:
    engine = make_engine()
    tables = set(Base.metadata.tables)
    assert {"billing_plans", "billing_customers", "billing_ledger_entries",
            "billing_notices"} <= tables
    assert "metering_records" not in tables  # 归 metering 模块，billing 不建
