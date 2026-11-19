"""admin API 的请求/响应模型（pydantic v2）。

复用约定：需求单的出参/入参直接复用 ``demands.schemas``（DemandInternalOut
/ TransitionIn）——派发台只是内部 transition 端点语义的转发面，不另立契约。
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

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
    # 开户 cell 映射提醒（close-wire-implementation-gaps 2.1）：仅 create_customer
    # 在显式部署口径（AI_CELL_ENDPOINT_MAP 已配）且缺该客户键时携带文案；
    # 列表/改档等其余端点恒 None（map 未配=约定派生口径，不提醒）
    cell_mapping_warning: str | None = None


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


class ContactMessageOut(BaseModel):
    """商留提交行（contact_messages 运营视图）。

    红线（spec contact-intake）：``ip_hash`` 不进响应模型——来源只以哈希
    留档，任何 API 面都不回显。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    company: str | None = None
    message: str
    status: str
    created_at: datetime


class ContactMessagesOut(BaseModel):
    """商留列表页：本页条目 + 匹配总数 + 全表未读数（分区入口徽标数据源）。"""

    items: list[ContactMessageOut]
    total: int
    unread: int


class ContactMessagePatchIn(BaseModel):
    """标记已读（status→read 是唯一合法目标；其余值 422 如实拒）。"""

    status: Literal["read"]


class CellProvisionRequestOut(BaseModel):
    """cell 配给请求行（add-self-serve-cell-provision；admin 状态页只读）。

    ``steps`` 落库为 JSON 文本——响应前解析成数组（失败时承载 step 痕，
    运营排查入口）；首期只读——无任何操作按钮/写面。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: str
    state: str
    steps: list[dict] = []
    error: str | None = None
    created_at: datetime

    @field_validator("steps", mode="before")
    @classmethod
    def _parse_steps_json(cls, value: object) -> object:
        """库内 JSON 文本 → 数组（坏帧如实降级为空数组，不让列表页 500）。"""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return []
        return value


class CellProvisionRequestsOut(BaseModel):
    """cell 配给列表页：本页条目 + 匹配总数（分页语义与商留列表一致）。"""

    items: list[CellProvisionRequestOut]
    total: int
