"""对账 reconcile()：wire 账本（权威）vs sub2api 余额快照 → 差异报告。

只读不动账（差异供后台人工处置，spec wire-billing「对账差异以账本为准」）。

口径：
    expected = opening − Σ(账本 sub2api_applied 条目金额)
    opening = 期初余额（客户预存等，由调用方给；缺省 0）
    difference = snapshot − expected  （≠0 即差异条目）
账本侧只计 sub2api_applied=True 的条目（settle 写 overage 时置位；
grant 是套餐额度分配、demand 是独立单据，都不动 sub2api 余额）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ledger import Ledger
from .models import Customer, validate_period
from .sub2api import Sub2ApiClient, Sub2ApiError


@dataclass(frozen=True)
class ReconcileRow:
    """单客户对账行：error 行 reachable=False（不猜快照）。"""

    customer_id: int
    customer_name: str
    sub2api_user_id: str | None
    ledger_applied_micro: int
    opening_micro: int
    expected_micro: int
    snapshot_micro: int | None = None
    difference_micro: int | None = None  # None = 快照不可得
    reachable: bool = True
    error: str | None = None

    @property
    def is_mismatch(self) -> bool:
        return self.difference_micro not in (None, 0)


@dataclass
class ReconcileReport:
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    period: str | None = None
    rows: list[ReconcileRow] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.rows)

    @property
    def mismatches(self) -> list[ReconcileRow]:
        return [r for r in self.rows if r.is_mismatch]

    @property
    def errors(self) -> list[ReconcileRow]:
        return [r for r in self.rows if r.error is not None]

    @property
    def matched(self) -> int:
        return sum(1 for r in self.rows if r.difference_micro == 0)


def reconcile(
    session: Session,
    sub2api: Sub2ApiClient,
    *,
    period: str | None = None,
    opening_balances: Mapping[int, int] | None = None,
    customer_ids: Iterable[int] | None = None,
) -> ReconcileReport:
    """生成差异报告；不写任何表。customer_ids 缺省=全部绑定 sub2api 的客户。"""
    if period is not None:
        validate_period(period)
    opening = opening_balances or {}
    stmt = select(Customer).where(Customer.sub2api_user_id.is_not(None))
    if customer_ids is not None:
        stmt = stmt.where(Customer.id.in_(list(customer_ids)))
    customers = list(session.execute(stmt.order_by(Customer.id)).scalars().all())

    ledger = Ledger(session)
    report = ReconcileReport(period=period)
    for customer in customers:
        applied = ledger.applied_total(customer.id, period=period)
        opening_micro = opening.get(customer.id, 0)
        expected = opening_micro - applied
        row = ReconcileRow(
            customer_id=customer.id,
            customer_name=customer.name,
            sub2api_user_id=customer.sub2api_user_id,
            ledger_applied_micro=applied,
            opening_micro=opening_micro,
            expected_micro=expected,
        )
        try:
            snapshot = sub2api.read_user(customer.sub2api_user_id)
        except Sub2ApiError as exc:
            report.rows.append(
                ReconcileRow(
                    customer_id=row.customer_id,
                    customer_name=row.customer_name,
                    sub2api_user_id=row.sub2api_user_id,
                    ledger_applied_micro=applied,
                    opening_micro=opening_micro,
                    expected_micro=expected,
                    snapshot_micro=None,
                    difference_micro=None,
                    reachable=False,
                    error=f"sub2api 读取失败: {exc}",
                )
            )
            continue
        report.rows.append(
            ReconcileRow(
                customer_id=row.customer_id,
                customer_name=row.customer_name,
                sub2api_user_id=row.sub2api_user_id,
                ledger_applied_micro=applied,
                opening_micro=opening_micro,
                expected_micro=expected,
                snapshot_micro=snapshot.balance_micro,
                difference_micro=snapshot.balance_micro - expected,
                reachable=True,
            )
        )
    return report
