"""wire 权威账本：同 idempotency_key 重放只记一条。

写入用 SAVEPOINT（begin_nested）包住 flush：并发/重放撞唯一约束时
只回滚该条插入，不殃及同事务内其它账目。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import LedgerEntry, LedgerKind, validate_period


@dataclass(frozen=True)
class RecordResult:
    entry: LedgerEntry
    created: bool  # False = 同键重放命中既有条目


def grant_key(customer_id: int, period: str) -> str:
    return f"grant:{customer_id}:{period}"


def settle_key(period: str, batch_ref: str | None, customer_id: int) -> str:
    scope = batch_ref if batch_ref else "period"
    return f"settle:{period}:{scope}:{customer_id}"


def demand_key(demand_ref: str) -> str:
    return f"demand:{demand_ref}"


def adjustment_key(source_ref: str, customer_id: int, period: str) -> str:
    return f"adjust:{customer_id}:{period}:{source_ref}"


def hardstop_key(customer_id: int, period: str, ref: str) -> str:
    return f"hardstop:{customer_id}:{period}:{ref}"


class Ledger:
    """账本服务：所有记账必经此口（幂等 + 可追溯 source_ref）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- 查询 -------------------------------------------------------------

    def find(self, idempotency_key: str) -> LedgerEntry | None:
        return self._session.execute(
            select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    def entries(self, customer_id: int, period: str | None = None) -> list[LedgerEntry]:
        stmt = select(LedgerEntry).where(LedgerEntry.customer_id == customer_id)
        if period is not None:
            stmt = stmt.where(LedgerEntry.period == validate_period(period))
        return list(self._session.execute(stmt.order_by(LedgerEntry.id)).scalars().all())

    def applied_total(self, customer_id: int, period: str | None = None) -> int:
        """已实际经 sub2api 执行的扣/调合计（对账的账本侧口径）。"""
        return sum(
            e.amount_micro for e in self.entries(customer_id, period) if e.sub2api_applied
        )

    # -- 幂等记账 ----------------------------------------------------------

    def record(
        self,
        *,
        customer_id: int,
        kind: LedgerKind,
        period: str,
        idempotency_key: str,
        quantity: int = 0,
        amount_micro: int = 0,
        source_ref: str | None = None,
        sub2api_applied: bool = False,
    ) -> RecordResult:
        """记一条；同 idempotency_key 已存在则原样返回（created=False）。"""
        validate_period(period)
        existing = self.find(idempotency_key)
        if existing is not None:
            return RecordResult(existing, created=False)
        entry = LedgerEntry(
            customer_id=customer_id,
            kind=kind.value,
            quantity=quantity,
            amount_micro=amount_micro,
            period=period,
            idempotency_key=idempotency_key,
            source_ref=source_ref,
            sub2api_applied=sub2api_applied,
        )
        try:
            with self._session.begin_nested():
                self._session.add(entry)
                self._session.flush()
        except IntegrityError:
            existing = self.find(idempotency_key)
            if existing is None:  # pragma: no cover - 非幂等键的完整性问题
                raise
            return RecordResult(existing, created=False)
        return RecordResult(entry, created=True)

    # -- 语义化快捷方式（都是 record 的薄封装，键规范在此收口） ----------------

    def ensure_grant(
        self,
        *,
        customer_id: int,
        period: str,
        monthly_price_micro: int,
        included_lines: int,
        source_ref: str | None = None,
    ) -> RecordResult:
        """月度套餐额度（月费 + 含额行数）——grant:{customer}:{period} 幂等。"""
        return self.record(
            customer_id=customer_id,
            kind=LedgerKind.GRANT,
            period=period,
            idempotency_key=grant_key(customer_id, period),
            quantity=included_lines,
            amount_micro=monthly_price_micro,
            source_ref=source_ref,
        )

    def record_demand(
        self, *, customer_id: int, period: str, amount_micro: int, demand_ref: str
    ) -> RecordResult:
        """需求单独立记账（不与套餐行数混算，不自动动 sub2api 余额）。"""
        if amount_micro < 0:
            raise ValueError("需求单记账金额必须为正（报价金额）")
        return self.record(
            customer_id=customer_id,
            kind=LedgerKind.DEMAND,
            period=period,
            idempotency_key=demand_key(demand_ref),
            amount_micro=amount_micro,
            source_ref=demand_ref,
        )

    def record_adjustment(
        self,
        *,
        customer_id: int,
        period: str,
        amount_micro: int,
        source_ref: str,
        sub2api_applied: bool = False,
    ) -> RecordResult:
        """人工调整（带符号）；只有已手工在 sub2api 执行过才置 sub2api_applied。"""
        if amount_micro == 0:
            raise ValueError("调整金额不能为 0")
        return self.record(
            customer_id=customer_id,
            kind=LedgerKind.ADJUSTMENT,
            period=period,
            idempotency_key=adjustment_key(source_ref, customer_id, period),
            amount_micro=amount_micro,
            source_ref=source_ref,
            sub2api_applied=sub2api_applied,
        )

    def record_hardstop(
        self, *, customer_id: int, period: str, ref: str
    ) -> RecordResult:
        """额度耗尽硬停事件留档（amount=0，仅事件轨迹）。"""
        return self.record(
            customer_id=customer_id,
            kind=LedgerKind.HARDSTOP,
            period=period,
            idempotency_key=hardstop_key(customer_id, period, ref),
            source_ref=ref,
        )
