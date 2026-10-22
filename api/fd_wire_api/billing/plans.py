"""套餐档位 seed（wire-self-serve-launch 1.2）：公开三档 + 旧三档禁用退场。

seed 策略=insert-if-missing：已存在的档位行不动（保留后台改价），缺则插入。
- 公开三档（free/basic/standard）：全部零超额单价——用完即停、次月恢复，
  硬停由当月行数门承担（见 settle/quota 口径）。
- 旧三档（starter/professional/enterprise）：置 ``is_active=False`` 禁用退场
  （行保留可审计，禁用后不可再指派；seed 每次运行都会把旧三档压回禁用，
  旧档退场是终态，不支持经 seed 复活）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Plan
from .money import yuan_to_micro


@dataclass(frozen=True)
class PlanSeed:
    code: str
    name: str
    monthly_price_yuan: str
    included_lines: int
    overage_unit_price_yuan: str
    is_public: bool = False
    is_active: bool = True
    #: 数据面分层限额（wire-customer-data-engine 6.4，Q10）：
    #: 缺省档 None → datasets 侧缺省（50 万行/数据集 + 全局存储设置）；
    #: standard 起 = 业务档（500 万行/数据集 + 5GB 存储）。
    limits: dict | None = None

    @property
    def monthly_price_micro(self) -> int:
        return yuan_to_micro(self.monthly_price_yuan)

    @property
    def overage_unit_price_micro(self) -> int:
        return yuan_to_micro(self.overage_unit_price_yuan)


#: 公开三档（portal-pricing 裁决，勿随手改价——改价走后台配置）：
#: free ¥0/1,000 行、basic ¥10/100,000 行、standard ¥50/1,000,000 行，
#: 超额单价一律 0（到量即停，无超额计费）。
PUBLIC_SEEDS: tuple[PlanSeed, ...] = (
    PlanSeed("free", "免费版", "0", 1_000, "0", is_public=True),
    PlanSeed("basic", "基础版", "10", 100_000, "0", is_public=True),
    PlanSeed(
        "standard", "标准版", "50", 1_000_000, "0", is_public=True,
        limits={"max_dataset_rows": 5_000_000, "storage_bytes": 5 * 1024**3},
    ),
)

#: 旧三档（高客单演示定价）：seed 禁用退场（is_active=False），行保留可审计。
LEGACY_SEEDS: tuple[PlanSeed, ...] = (
    PlanSeed(
        "starter", "起步版", "1999", 500_000, "0.004", is_public=True, is_active=False
    ),
    PlanSeed(
        "professional",
        "专业版",
        "6999",
        3_000_000,
        "0.003",
        is_public=True,
        is_active=False,
    ),
    PlanSeed(
        "enterprise",
        "旗舰版",
        "19999",
        12_000_000,
        "0.002",
        is_public=True,
        is_active=False,
    ),
)

#: 全部 seed（公开 + 旧档）；迁移 0014 的 _PLAN_SEEDS 与此处同源。
SEEDS: tuple[PlanSeed, ...] = PUBLIC_SEEDS + LEGACY_SEEDS

PLAN_CODES: tuple[str, ...] = tuple(s.code for s in SEEDS)

#: 禁用退场的旧档位码（指派校验与 seed 压制共用）
LEGACY_CODES: frozenset[str] = frozenset(s.code for s in LEGACY_SEEDS)

#: 席位缺省上限（wire-team-spaces-v2）：limits_json.seats 后台可配优先，
#: 此处只是缺省映射；免费档=1 即团队功能付费解锁。
SEAT_DEFAULTS: dict[str, int] = {"free": 1, "basic": 3, "standard": 10}
#: 未知/禁用旧档的缺省席位（保守=仅 owner）
SEAT_DEFAULT_UNKNOWN = 1


def seed_plans(session: Session) -> dict[str, Plan]:
    """幂等播种档位：缺则插入，存在则保留（后台可配优先）。

    唯一例外是旧三档的 ``is_active``：退场是终态，每次运行压回 False
    （防止历史行被误复活指派）。新建库同样得到六档（旧三档即插即禁用）。
    """
    plans: dict[str, Plan] = {
        p.code: p for p in session.execute(select(Plan)).scalars().all()
    }
    for seed in SEEDS:
        existing = plans.get(seed.code)
        if existing is None:
            existing = Plan(
                code=seed.code,
                name=seed.name,
                monthly_price_micro=seed.monthly_price_micro,
                included_lines=seed.included_lines,
                overage_unit_price_micro=seed.overage_unit_price_micro,
                is_active=seed.is_active,
                is_public=seed.is_public,
                limits_json=seed.limits,
            )
            session.add(existing)
            plans[seed.code] = existing
        elif seed.code in LEGACY_CODES and existing.is_active:
            existing.is_active = False
    session.flush()
    return plans


def get_plan(session: Session, code: str) -> Plan:
    plan = session.execute(select(Plan).where(Plan.code == code)).scalar_one_or_none()
    if plan is None:
        raise ValueError(f"未知套餐档位: {code!r}（可用: {list(PLAN_CODES)}）")
    return plan


def list_public_plans(session: Session) -> list[Plan]:
    """公开档位（is_public && is_active），按创建序（=seed 顺序=展示顺序）。"""
    return list(
        session.execute(
            select(Plan)
            .where(Plan.is_public.is_(True), Plan.is_active.is_(True))
            .order_by(Plan.id)
        )
        .scalars()
        .all()
    )


def seat_limit(session: Session, plan_code: str) -> int:
    """席位上限：plan.limits_json['seats'] 显式配置优先，否则 SEAT_DEFAULTS，
    未知档 SEAT_DEFAULT_UNKNOWN。

    ``limits_json`` 无 ``seats`` 键时才算缺省（后台改 limits_json 即时生效，
    不走 seed）；显式配 0 或负数不合法，按缺省处理。
    """
    plan = session.execute(
        select(Plan).where(Plan.code == plan_code)
    ).scalar_one_or_none()
    if plan is not None:
        limits = plan.limits_json if isinstance(plan.limits_json, dict) else None
        seats = limits.get("seats") if limits is not None else None
        if isinstance(seats, int) and seats > 0:
            return seats
    return SEAT_DEFAULTS.get(plan_code, SEAT_DEFAULT_UNKNOWN)
