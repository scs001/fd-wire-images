"""admin API 的请求/响应模型（pydantic v2）。

复用约定：需求单的出参/入参直接复用 ``demands.schemas``（DemandInternalOut
/ TransitionIn）——派发台只是内部 transition 端点语义的转发面，不另立契约。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ..billing.models import CustomerStatus
from ..billing.money import micro_to_yuan, micro_to_yuan_str


class PlanOut(BaseModel):
    """套餐档位（billing_plans 只读视图；后台下拉数据源，含禁用/公开标记）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    monthly_price_micro: int
    monthly_price_yuan: str
    included_lines: int
    overage_unit_price_micro: int
    is_active: bool
    is_public: bool = False

    @classmethod
    def from_plan(cls, plan) -> "PlanOut":
        return cls(
            id=plan.id,
            code=plan.code,
            name=plan.name,
            monthly_price_micro=plan.monthly_price_micro,
            monthly_price_yuan=micro_to_yuan_str(plan.monthly_price_micro),
            included_lines=plan.included_lines,
            overage_unit_price_micro=plan.overage_unit_price_micro,
            is_active=plan.is_active,
            is_public=bool(getattr(plan, "is_public", False)),
        )


class PlanCreateIn(BaseModel):
    """自定义档建档（wire-self-serve-launch 2.2）：询价定案的月费/含额。

    ``is_public`` 恒为 false（自定义档不进公开档位列表，公开端点不可见）。
    金额一律微元（1 元 = 1,000,000 微元）；超额单价缺省 0（用完即停口径）。
    """

    code: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    name: str = Field(min_length=1, max_length=64)
    monthly_price_micro: int = Field(ge=0)
    included_lines: int = Field(ge=0)
    overage_unit_price_micro: int = Field(default=0, ge=0)


class CustomerOut(BaseModel):
    """客户（billing_customers 视图；带套餐档位联查字段）。"""

    id: int
    name: str
    # v1 兼作计量身份映射键（metering_records.caller）；sub2api 代建账户
    # 落地后语义回归「支付侧用户 id」
    sub2api_user_id: str | None = None
    # 控制台登录身份映射（wire-self-serve-launch 1.1；首登自动开户时回写）
    logto_sub: str | None = None
    plan_id: int
    plan_code: str
    plan_name: str
    status: str
    created_at: datetime


class CustomerCreateIn(BaseModel):
    """开户：名称 + 套餐档位 code（sub2api 代建留给后续，先本地档位关联）。"""

    name: str = Field(min_length=1, max_length=128)
    plan_code: str = Field(min_length=1, max_length=32)
    #: 计量身份（caller 名）——v1 由 billing_customers.sub2api_user_id 承载，
    #: 开户时录入即建立「客户 ↔ 调用身份」映射（用量联查依赖此列）
    sub2api_user_id: str | None = Field(default=None, max_length=64)


class CustomerPatchIn(BaseModel):
    """改套餐档位 / 客户状态（两者均可选，至少给一个）。"""

    plan_code: str | None = Field(default=None, min_length=1, max_length=32)
    status: CustomerStatus | None = None


class UsagePeriodRow(BaseModel):
    period: str
    calls: int
    rows: int


class CustomerUsageOut(BaseModel):
    """单客户用量（metering_records 只读联查：customer 轨 × caller 身份）。"""

    customer_id: int
    customer_name: str
    # 计量身份映射键；为空 = 尚未建立映射，无法归属用量（如实返回空聚合）
    caller: str | None = None
    credential_track: str = "customer"
    totals: UsagePeriodRow  # period 恒为 "all"
    periods: list[UsagePeriodRow]


class LedgerEntryOut(BaseModel):
    """账本条目（billing_ledger_entries 只读；金额同时给微元与元展示）。"""

    id: int
    customer_id: int
    kind: str
    quantity: int
    amount_micro: int
    amount_yuan: Decimal
    amount_yuan_str: str
    period: str
    idempotency_key: str
    source_ref: str | None = None
    sub2api_applied: bool
    created_at: datetime

    @classmethod
    def from_entry(cls, entry) -> "LedgerEntryOut":
        return cls(
            id=entry.id,
            customer_id=entry.customer_id,
            kind=entry.kind,
            quantity=entry.quantity,
            amount_micro=entry.amount_micro,
            amount_yuan=micro_to_yuan(entry.amount_micro),
            amount_yuan_str=micro_to_yuan_str(entry.amount_micro),
            period=entry.period,
            idempotency_key=entry.idempotency_key,
            source_ref=entry.source_ref,
            sub2api_applied=entry.sub2api_applied,
            created_at=entry.created_at,
        )


class ReviewNoteIn(BaseModel):
    """目录审核意见录入（append-only，只记 admin 自己的表）。"""

    note: str = Field(min_length=1, max_length=4000)
    author: str = Field(default="admin-console", max_length=64)


class ReviewNoteOut(BaseModel):
    """已留档的审核意见。"""

    id: int
    indicator_id: str
    note: str
    author: str
    created_at: str


class IndicatorPendingItem(BaseModel):
    """目录治理视图的镜像行（只读；字段集对齐 catalog 镜像展示字段）。"""

    indicator_id: str
    code: str
    name_zh: str | None = None
    name_en: str | None = None
    domain: str | None = None
    frequency: str | None = None
    status: str
    synced_at: datetime


class CatalogPendingOut(BaseModel):
    status: str
    items: list[IndicatorPendingItem]
    total: int


class TrackRow(BaseModel):
    """分轨聚合行（credential_track 或 source 维度）。"""

    key: str
    calls: int
    rows: int


class UsageOverviewOut(BaseModel):
    """用量分轨概览：客户轨/平台轨数字卡 + 来源分布。"""

    credential_track: str | None = None
    start: str | None = None
    end: str | None = None
    tracks: list[TrackRow]
    by_source: list[TrackRow]
    totals: TrackRow
