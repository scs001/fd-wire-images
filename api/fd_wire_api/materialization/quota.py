"""物化前置的额度硬停检查（复用 billing 聚合口径，只读）。

对齐 wire-billing「硬停范围一致」：客户额度耗尽时**交互查询与产物定时
刷新一律拒绝**——物化运行器在发出任何读取前先问本模块。

只读复用口径（与 admin 切片的 customer_usage / billing 结算同源）：
- 身份映射：v1 以 ``billing_customers.sub2api_user_id`` 承载 wire 侧客户
  身份键（物化的字符串 customer_id 按该列映射到 billing Customer）；
- 已用行数 = metering_records 中 ``credential_track='customer'`` 且
  ``caller=<身份键>``、period（YYYY-MM，由 event_timestamp 派生）为当期
  的 ``rows_returned`` 合计——物化读取本身也经客户凭据走 wire 入口，
  其行数会进同一张表，下一轮检查自然把它们计入（闭环）；
- 额度 = ``customer.plan.included_lines``；``used >= included`` 即耗尽。

fail-closed 边界：客户映射不到 / 非活跃 → ``allowed=False`` 且
``reason`` 如实说明（无法核实额度就不放行，与缺凭据拒绝同一纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..billing.models import Customer, CustomerStatus
from ..dbcompat import period_expression
from ..metering.models import MeteringRecord
from .db import utc_now_naive

#: last_status 取值：额度耗尽跳过 / 额度无法核实跳过
STATUS_QUOTA_EXHAUSTED = "quota_exhausted"
STATUS_QUOTA_UNVERIFIABLE = "quota_unverifiable"


@dataclass(frozen=True)
class QuotaVerdict:
    """一次额度检查的结论。``allowed=False`` 时 reason 说明原因。"""

    allowed: bool
    used_lines: int = 0
    included_lines: int = 0
    status: str | None = None  # 跳过时对应的 last_status
    reason: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.status == STATUS_QUOTA_EXHAUSTED


class QuotaGuard(Protocol):
    """额度检查接口（测试与生产实现共守）。"""

    def check(self, customer_id: str, *, now: datetime | None = None) -> QuotaVerdict: ...


class AllowAllQuotaGuard:
    """测试/演练用：不设额度的直通守卫（生产绝不使用）。"""

    def check(self, customer_id: str, *, now: datetime | None = None) -> QuotaVerdict:
        return QuotaVerdict(allowed=True)


class SqlQuotaGuard:
    """生产守卫：billing + metering 只读联查（同库会话）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def check(self, customer_id: str, *, now: datetime | None = None) -> QuotaVerdict:
        moment = now or utc_now_naive()
        period = moment.strftime("%Y-%m")
        customer = self._session.execute(
            select(Customer).where(Customer.sub2api_user_id == customer_id)
        ).scalar_one_or_none()
        if customer is None:
            return QuotaVerdict(
                allowed=False,
                status=STATUS_QUOTA_UNVERIFIABLE,
                reason=(
                    f"customer {customer_id} 未映射到 billing 客户"
                    "（sub2api_user_id 未覆盖）——无法核实额度，fail-closed 跳过"
                ),
            )
        if customer.status != CustomerStatus.ACTIVE.value:
            return QuotaVerdict(
                allowed=False,
                status=STATUS_QUOTA_UNVERIFIABLE,
                reason=f"客户状态 {customer.status}，物化跳过",
            )
        included = customer.plan.included_lines if customer.plan else 0
        used = int(
            self._session.execute(
                select(func.sum(MeteringRecord.rows_returned)).where(
                    MeteringRecord.credential_track == "customer",
                    MeteringRecord.caller == customer_id,
                    period_expression(
                        self._session.get_bind(), MeteringRecord.event_timestamp
                    )
                    == period,
                )
            ).scalar_one()
            or 0
        )
        if used >= included:
            return QuotaVerdict(
                allowed=False,
                used_lines=used,
                included_lines=included,
                status=STATUS_QUOTA_EXHAUSTED,
                reason=(
                    f"当期（{period}）已用 {used} 行 ≥ 套餐额度 {included} 行，"
                    "硬停：产物物化跳过"
                ),
            )
        return QuotaVerdict(
            allowed=True, used_lines=used, included_lines=included
        )
