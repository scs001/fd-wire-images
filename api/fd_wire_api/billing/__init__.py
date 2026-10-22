"""计费域（wire-billing）：账本权威 + sub2api 幂等结算 + 对账 + 两档预警。

入口一览：
- 建表/播种：`init_billing(engine)`（create_all + seed_plans；alembic 链
  由主代理收敛，落地前以此起步）
- 开户：`ensure_customer(session, sub2api, name=..., plan_code=...)`
- 结算：`SettlementService(session, sub2api, metering).settle(period, batch_ref)`
- 对账：`reconcile(session, sub2api, opening_balances=...)`
- 预警：`evaluate_usage_warnings(session, customer=..., ...)`（settle 内已自动调）
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .customers import ensure_customer
from .ledger import Ledger, RecordResult
from .metering_source import (
    METERING_RECORDS_DDL,
    MeteringSource,
    MeteringUsage,
    SqlMeteringSource,
    StaticMeteringSource,
    ensure_metering_records_table,
)
from .models import (
    NOTICE_CHANNELS,
    Base,
    Customer,
    CustomerStatus,
    LedgerEntry,
    LedgerKind,
    NoticeKind,
    NoticeRecord,
    Plan,
    SUB2API_APPLIED_KINDS,
    validate_period,
)
from .money import MICRO_PER_YUAN, micro_to_yuan, micro_to_yuan_str, yuan_to_micro
from .notices import USAGE_WARNING_TIERS, evaluate_usage_warnings
from .plans import (
    LEGACY_CODES,
    PLAN_CODES,
    PUBLIC_SEEDS,
    SEEDS,
    PlanSeed,
    get_plan,
    list_public_plans,
    seed_plans,
)
from .reconcile import ReconcileReport, ReconcileRow, reconcile
from .settle import SettlementReport, SettlementService, default_resolver
from .sub2api import (
    ProbeResult,
    Sub2ApiClient,
    Sub2ApiError,
    Sub2ApiUnavailable,
    Sub2User,
)

__all__ = [
    "METERING_RECORDS_DDL",
    "MICRO_PER_YUAN",
    "NOTICE_CHANNELS",
    "LEGACY_CODES",
    "PLAN_CODES",
    "PUBLIC_SEEDS",
    "SEEDS",
    "SUB2API_APPLIED_KINDS",
    "USAGE_WARNING_TIERS",
    "Base",
    "Customer",
    "CustomerStatus",
    "Ledger",
    "LedgerEntry",
    "LedgerKind",
    "MeteringSource",
    "MeteringUsage",
    "NoticeKind",
    "NoticeRecord",
    "Plan",
    "PlanSeed",
    "ProbeResult",
    "RecordResult",
    "ReconcileReport",
    "ReconcileRow",
    "SettlementReport",
    "SettlementService",
    "SqlMeteringSource",
    "StaticMeteringSource",
    "Sub2ApiClient",
    "Sub2ApiError",
    "Sub2ApiUnavailable",
    "Sub2User",
    "default_resolver",
    "ensure_customer",
    "ensure_metering_records_table",
    "evaluate_usage_warnings",
    "get_plan",
    "init_billing",
    "list_public_plans",
    "micro_to_yuan",
    "micro_to_yuan_str",
    "reconcile",
    "seed_plans",
    "validate_period",
    "yuan_to_micro",
]


def init_billing(engine: Engine | None = None, *, session: Session | None = None) -> Engine:
    """建 billing_* 表并播种三档套餐（幂等）。返回所用 engine。

    注意：不建 `metering_records`（归 metering 模块）；迁移链就位前
    用 create_all 起步，alembic 版本由主代理统一收敛。
    """
    if engine is None:
        import os

        from ..config import canonical_metadata_database_url, normalize_database_url

        url = (
            canonical_metadata_database_url()
            or os.getenv("BILLING_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or "sqlite:///./billing.db"
        )
        engine = create_engine(normalize_database_url(url))
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        seed_plans(sess)
        sess.commit()
    return engine
