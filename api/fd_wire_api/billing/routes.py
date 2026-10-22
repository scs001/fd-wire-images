"""billing 域路由：公开档位端点 + 内部额度查询端点（wire-self-serve-launch
1.3 / 3.1）。

- ``GET /api/v1/plans``：公开只读（无鉴权），仅 ``is_public && is_active``，
  信封 ``{"plans": [...]}``，按创建序（seed 顺序）输出 ``sort`` 序号；旧三档
  （禁用）与自定义档（is_public=false）不出现。定价页/首页活取，i18n 面量退位。
- ``GET /internal/quota/{caller}``：当月行数门的数据源（registry 预检探针
  消费）——当月 metering_records 聚合 rows_returned vs 客户档位
  included_lines；与 admin 客户用量、控制台用量、结算聚合同源
  （credential_track='customer'、period 由 event_timestamp 派生）。
  ``enforce_balance = overage_unit_price_micro > 0``：无超额档（含免费档）
  不查 sub2api 余额，硬停由行数门承担（caller-preflight-map 增量）。
- ``monthly_used_lines``：当月行数聚合的唯一实现（三处消费同源）。

额度端点鉴权：``X-Internal-Token`` 与 env ``FD_WIRE_INTERNAL_TOKEN`` 常量
时间比对（demands/datasets 内部面同款）。会话：平台库（admin 域同源，
billing/metering/console 表同库）。
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..admin.db import get_db
from ..dbcompat import period_expression
from ..metering.models import MeteringRecord
from .models import Customer, CustomerStatus
from .plans import list_public_plans

router = APIRouter(tags=["billing"])


# ---------------------------------------------------------------------------
# 鉴权（内部面）
# ---------------------------------------------------------------------------

_DEFAULT_INTERNAL_TOKEN = "wire-internal-dev"


def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """内部调用鉴权：与 FD_WIRE_INTERNAL_TOKEN 常量时间比对（抄 demands）。"""
    expected = os.getenv("FD_WIRE_INTERNAL_TOKEN", _DEFAULT_INTERNAL_TOKEN)
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部端点需要有效的 X-Internal-Token",
        )


# ---------------------------------------------------------------------------
# 当月行数聚合（额度查询 / 控制台用量 / admin usage 同源口径）
# ---------------------------------------------------------------------------


def current_period(now: datetime | None = None) -> str:
    """当期 YYYY-MM（UTC；metering period 由 event_timestamp 派生同口径）。"""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def monthly_used_lines(db: Session, caller: str | None, period: str) -> int:
    """当月已用行数：customer 轨 × caller 身份 × period 的 rows_returned 合计。

    与 admin ``/internal/admin/customers/{id}/usage`` 的聚合完全同源
    （同一表、同一轨道过滤、同一双方言 period 派生——dbcompat）。
    """
    if not caller:
        return 0
    used = db.execute(
        select(func.sum(MeteringRecord.rows_returned)).where(
            MeteringRecord.credential_track == "customer",
            MeteringRecord.caller == caller,
            period_expression(db.get_bind(), MeteringRecord.event_timestamp) == period,
        )
    ).scalar_one()
    return int(used or 0)


# ---------------------------------------------------------------------------
# 公开档位端点（1.3）
# ---------------------------------------------------------------------------


class PublicPlanOut(BaseModel):
    """公开档位（对外契约逐字：code/monthly_price_yuan/included_lines/sort）。"""

    code: str
    monthly_price_yuan: int
    included_lines: int
    sort: int


class PublicPlansOut(BaseModel):
    """公开档位列表信封（对外契约逐字：``{"plans": [...]}``）。"""

    plans: list[PublicPlanOut]


@router.get("/api/v1/plans", response_model=PublicPlansOut)
def public_plans(db: Session = Depends(get_db)) -> PublicPlansOut:
    """公开档位列表（无鉴权；display name 不进 API，前端 i18n 按 code 映射）。

    ``sort``：公开档位在返回列表中的 1 起序号（seed 顺序=展示顺序；
    free/basic/standard 恒 1/2/3，后台改价不改变顺序）。
    """
    plans = list_public_plans(db)
    return PublicPlansOut(
        plans=[
            PublicPlanOut(
                code=plan.code,
                monthly_price_yuan=plan.monthly_price_micro // 1_000_000,
                included_lines=plan.included_lines,
                sort=ordinal,
            )
            for ordinal, plan in enumerate(plans, start=1)
        ]
    )


# ---------------------------------------------------------------------------
# 额度查询端点（3.1）
# ---------------------------------------------------------------------------


class QuotaOut(BaseModel):
    """额度查询（registry 预检行数门探针契约，字段名逐字）。"""

    caller: str
    plan_code: str
    included_lines: int
    used_lines: int
    remaining_lines: int
    period: str
    enforce_balance: bool


@router.get(
    "/internal/quota/{caller}",
    response_model=QuotaOut,
    dependencies=[Depends(require_internal_token)],
)
def caller_quota(caller: str, db: Session = Depends(get_db)) -> QuotaOut:
    """当月额度查询（caller = sub2api_user_id 承载的 wire 客户身份键）。

    未知 caller → 404（fail-closed：预检探针据此拒绝，不放行无主调用）。
    非 active 客户（suspended/closed）额度归零——行数门据此即断
    （self-serve-onboarding「停用即断」的额度侧实现）。
    """
    customer = db.execute(
        select(Customer).where(Customer.sub2api_user_id == caller)
    ).scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=404, detail=f"caller {caller} 未映射到客户")
    plan = customer.plan
    period = current_period()
    used = monthly_used_lines(db, caller, period)
    active = customer.status == CustomerStatus.ACTIVE.value
    included = plan.included_lines if (plan and active) else 0
    return QuotaOut(
        caller=caller,
        plan_code=plan.code if plan else "",
        included_lines=included,
        used_lines=used,
        remaining_lines=max(0, included - used),
        period=period,
        enforce_balance=bool(plan and plan.overage_unit_price_micro > 0),
    )
