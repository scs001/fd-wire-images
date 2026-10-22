"""计费域 SQLAlchemy 模型：套餐 / 客户 / 账本 / 预警通知。

金额一律用「微元」（1 元 = 1,000,000 微元）的整数存储与运算，
避免浮点误差（超额单价 ¥0.004/行 = 4,000 微元/行）。

注意：
- `metering_records` 表归 metering 模块所有，刻意不在本 Base 的
  metadata 里（见 billing/metering_source.py 的共享读取契约）；
  billing 的 create_all 只建 billing_* 表。
- alembic 链尚未建立：先 `Base.metadata.create_all(engine)` + `seed_plans()`，
  迁移版本由主代理收敛（见任务 4.2 约定）。
"""

from __future__ import annotations

import enum
import re
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_period(period: str) -> str:
    """period 必须形如 YYYY-MM，否则抛 ValueError。"""
    if not isinstance(period, str) or not PERIOD_RE.match(period):
        raise ValueError(f"period 需为 YYYY-MM: {period!r}")
    return period


class Base(DeclarativeBase):
    """计费域 ORM 基类（billing_* 表）。"""


class LedgerKind(str, enum.Enum):
    """账本条目类型。"""

    GRANT = "grant"          # 月度套餐额度（月费 + 含额行数）
    OVERAGE = "overage"      # 超额按行计费
    HARDSTOP = "hardstop"    # 额度耗尽硬停事件
    DEMAND = "demand"        # 需求单独立报价记账（不进套餐计量）
    ADJUSTMENT = "adjustment"  # 人工调整（带符号）


#: 其金额变动会经由 sub2api adjustBalance 执行的账目类型。
#: grant=套餐额度分配（月费走账单，不扣 sub2api 余额）；
#: demand=独立单据（预付/到付按单据约定，v1 不自动扣）。
SUB2API_APPLIED_KINDS = frozenset({LedgerKind.OVERAGE.value, LedgerKind.ADJUSTMENT.value})


class CustomerStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class Plan(Base):
    """套餐档位（已裁决默认值见 plans.SEEDS；后台可改不发版）。

    ``is_public``：公开档位标记（wire-self-serve-launch 1.1）——公开档位
    端点只返回 ``is_public && is_active``；自定义档（admin 建档）恒 false。
    """

    __tablename__ = "billing_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    monthly_price_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    included_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overage_unit_price_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    #: 数据面分层限额（wire-customer-data-engine 6.4）：None=缺省档；
    #: {"storage_bytes": int, "max_dataset_rows": int}，后台可改价同渠道可改
    limits_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    customers: Mapped[list["Customer"]] = relationship(back_populates="plan")

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<Plan {self.code} ¥{self.monthly_price_micro / 1_000_000}/mo>"


class Customer(Base):
    """客户与支付侧（sub2api）账户映射。

    ``logto_sub``（wire-self-serve-launch 1.1）：控制台登录身份 → Customer
    的唯一映射键（可空——人工开户的历史行允许暂无登录身份；首登自动
    开户/绑定时回写）。唯一 sub 至多映射一个客户。
    ``name``（wire-console-resilience）：客户命名空间键，全表唯一——
    档位解析（plan_code_for/storage_limit_for）按 name 收敛，重名会令
    解析不确定（生产 2026-10-15 孤儿行事故）。迁移 0037 自愈改名后建
    唯一索引（迁移是权威——create_all 仅新库一致）。
    """

    __tablename__ = "billing_customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    sub2api_user_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    logto_sub: Mapped[str | None] = mapped_column(String(64), unique=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("billing_plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CustomerStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    plan: Mapped[Plan] = relationship(back_populates="customers")

    @property
    def status_enum(self) -> CustomerStatus:
        return CustomerStatus(self.status)


class LedgerEntry(Base):
    """wire 权威账本条目。

    - quantity：行数（grant=含额行数、overage=超额行数，其余可为 0）
    - amount_micro：微元金额，平台应收视角带符号（adjustment 可为负）
    - idempotency_key：全表唯一——同键重放只记一条
    - sub2api_applied：该笔是否已（或应已）通过 sub2api adjustBalance 执行，
      对账口径以此为准（spec 未列此列，属工程补充，见汇报）
    """

    __tablename__ = "billing_ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("billing_customers.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(191))
    sub2api_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    customer: Mapped[Customer] = relationship()


class NoticeKind(str, enum.Enum):
    USAGE_WARNING = "usage_warning"


#: 预警渠道（spec：mail+inapp）
NOTICE_CHANNELS = "mail+inapp"


class NoticeRecord(Base):
    """预警/通知留档：同客户同周期同档位只发一次（数据库唯一约束兜底）。"""

    __tablename__ = "billing_notices"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "period", "kind", "threshold_pct", name="uq_notice_once_per_tier"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("billing_customers.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=NoticeKind.USAGE_WARNING.value)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False)  # 80 / 95
    channels: Mapped[str] = mapped_column(String(32), nullable=False, default=NOTICE_CHANNELS)
    payload_json: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
