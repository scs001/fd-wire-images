"""结算执行器 settle()：计量聚合 → 扣套餐额度 → 超额入账 → sub2api 幂等扣减。

时序（每客户）：
1. 聚合客户凭据轨道的 rows_returned（MeteringSource，只读 metering 表；
   真实表 adapter=整月聚合域，batch_ref 供水位线实现扩展）
2. ensure_grant：月度套餐额度条目（幂等键 grant:{customer}:{period}）
3. overage_lines = max(0, 用量 - 含额)；单价×行数 = 超额金额
4. 超额>0 且账本无 settle 键 → sub2api.adjustBalance(幂等键=settle 键)
   → 成功才写 overage 条目（sub2api_applied=True）
5. 失败（sub2api 不可达/4xx）→ 不写账，报告标 failed_sub2api，
   下轮重放同键：sub2api 服务端幂等去重、账本补记——净效果一次扣减

重放保证：
- settle 键已存在于账本 → 直接 replayed，sub2api 零调用、账本零新增
  （验收：同批次重放不双扣不双记）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ledger import Ledger, RecordResult, settle_key
from .models import Customer, CustomerStatus, LedgerKind, validate_period
from .metering_source import MeteringSource
from .notices import evaluate_usage_warnings
from .sub2api import Sub2ApiClient, Sub2ApiError

CustomerResolver = Callable[[str], Customer | None]


def default_resolver(session: Session) -> CustomerResolver:
    """默认身份映射（耦合点）：metering 的 caller → Customer.sub2api_user_id。

    v1 以 sub2api_user_id 承载 wire 侧客户身份键；正式的
    Logto username ↔ Customer 映射落地后注入自定义 resolver 即可，
    结算流程不用改。
    """

    def resolve(ref: str) -> Customer | None:
        return session.execute(
            select(Customer).where(Customer.sub2api_user_id == ref)
        ).scalar_one_or_none()

    return resolve


@dataclass
class CustomerSettlement:
    """单客户结算结果。status ∈ settled / replayed / skipped_* / failed_*。"""

    status: str
    customer_ref: str
    rows: int
    customer_id: int | None = None
    grant_created: bool = False
    included_lines: int = 0
    overage_lines: int = 0
    overage_amount_micro: int = 0
    sub2api_called: bool = False
    notice_tiers_sent: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("settled", "replayed")


@dataclass
class SettlementReport:
    period: str
    batch_ref: str | None
    results: list[CustomerSettlement] = field(default_factory=list)

    @property
    def sub2api_call_count(self) -> int:
        return sum(1 for r in self.results if r.sub2api_called)

    @property
    def failures(self) -> list[CustomerSettlement]:
        return [r for r in self.results if not r.ok]


class SettlementService:
    """结算执行器（一个实例一个 session，非线程共享）。"""

    def __init__(
        self,
        session: Session,
        sub2api: Sub2ApiClient,
        metering: MeteringSource,
        *,
        resolver: CustomerResolver | None = None,
    ) -> None:
        self._session = session
        self._sub2api = sub2api
        self._metering = metering
        self._ledger = Ledger(session)
        self._resolver = resolver or default_resolver(session)

    def settle(self, period: str, batch_ref: str | None = None) -> SettlementReport:
        """结算一个计量批次（batch_ref=None 表示整月聚合域）。"""
        validate_period(period)
        report = SettlementReport(period=period, batch_ref=batch_ref)
        try:
            usages = sorted(
                self._metering.aggregates(period, batch_ref), key=lambda u: u.customer_ref
            )
        except Exception as exc:  # 计量源不可读：整批失败，不动任何账
            report.results.append(
                CustomerSettlement(
                    status="failed_metering_source", customer_ref="", rows=0, error=str(exc)
                )
            )
            return report

        for usage in usages:
            report.results.append(self._settle_customer(period, batch_ref, usage))
        self._session.commit()  # 逐客户失败已入报告；成功部分照常落账
        return report

    # -- 单客户 -------------------------------------------------------------

    def _settle_customer(
        self, period: str, batch_ref: str | None, usage
    ) -> CustomerSettlement:
        result = CustomerSettlement(
            status="settled", customer_ref=usage.customer_ref, rows=usage.rows
        )
        customer = self._resolver(usage.customer_ref)
        if customer is None:
            result.status = "skipped_no_customer"
            result.error = f"metering customer_ref 无法映射客户: {usage.customer_ref}"
            return result
        result.customer_id = customer.id
        if customer.status != CustomerStatus.ACTIVE.value:
            result.status = "skipped_inactive"
            result.error = f"客户状态 {customer.status}，不结算"
            return result

        plan = customer.plan
        result.included_lines = plan.included_lines

        # 1) 月度套餐额度（幂等：grant:{customer}:{period}）
        source_ref = batch_ref if batch_ref is not None else f"period:{period}"
        grant: RecordResult = self._ledger.ensure_grant(
            customer_id=customer.id,
            period=period,
            monthly_price_micro=plan.monthly_price_micro,
            included_lines=plan.included_lines,
            source_ref=source_ref,
        )
        result.grant_created = grant.created
        result.status = "settled" if grant.created else "replayed"

        # 2) 超额判定与幂等扣减
        # 无超额档（超额单价 0，含四档新定价全部公开档）：超出部分不产生任何
        # 超额账目、不动 sub2api——硬停由当月行数门承担（portal-pricing
        # 「计费模型无超额」；闭月结算金额=月费 grant 条目）。
        result.overage_lines = max(0, usage.rows - plan.included_lines)
        if plan.overage_unit_price_micro <= 0:
            result.overage_amount_micro = 0
            result.status = "settled" if grant.created else "replayed"
            self._notify(period, customer, usage.rows, plan.included_lines, result)
            return result
        if result.overage_lines <= 0:
            result.status = "settled" if grant.created else "replayed"
            self._notify(period, customer, usage.rows, plan.included_lines, result)
            return result

        result.overage_amount_micro = result.overage_lines * plan.overage_unit_price_micro
        key = settle_key(period, batch_ref, customer.id)
        if self._ledger.find(key) is not None:
            result.status = "replayed"  # 账本已有 → 不再碰 sub2api、不再记账
            self._notify(period, customer, usage.rows, plan.included_lines, result)
            return result

        if not customer.sub2api_user_id:
            result.status = "failed_no_sub2api_user"
            result.error = "客户未绑定 sub2api 账户"
            return result
        try:
            self._sub2api.adjust_balance(
                user_id=customer.sub2api_user_id,
                amount_micro=-result.overage_amount_micro,  # 扣减为负
                idempotency_key=key,
            )
        except Sub2ApiError as exc:
            result.status = "failed_sub2api"
            result.error = str(exc)
            return result  # 不写账——下轮同键重放自愈
        result.sub2api_called = True

        entry = self._ledger.record(
            customer_id=customer.id,
            kind=LedgerKind.OVERAGE,
            period=period,
            idempotency_key=key,
            quantity=result.overage_lines,
            amount_micro=result.overage_amount_micro,
            source_ref=source_ref,
            sub2api_applied=True,
        )
        if not entry.created:  # 并发对手已记（sub2api 同键幂等，净一次扣减）
            result.sub2api_called = False
            result.status = "replayed"
        else:
            # grant 可能是重放（早前批次已建），但本批次超额是新建的
            result.status = "settled"

        # 3) 两档预警（不中断结算）
        self._notify(period, customer, usage.rows, plan.included_lines, result)
        return result

    def _notify(self, period: str, customer: Customer, used: int, included: int,
                result: CustomerSettlement) -> None:
        try:
            notices = evaluate_usage_warnings(
                self._session,
                customer=customer,
                period=period,
                used_lines=used,
                included_lines=included,
            )
            result.notice_tiers_sent = [n.threshold_pct for n in notices]
        except Exception as exc:  # 预警失败不阻塞结算，留报告线索
            result.error = result.error or f"预警评估失败: {exc}"
