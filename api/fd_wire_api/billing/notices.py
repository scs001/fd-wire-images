"""用量两档预警（80% / 95%）：同客户同周期同档位只发一次。

阈值比较全整数运算（used*100 >= pct*included），不吃浮点亏；
「只发一次」由 billing_notices 的唯一约束兜底（SAVEPOINT 吞重复）。
渠道固定 mail+inapp（spec 裁决）；实际投递由通知通道消费
NoticeRecord 行，本模块只负责判定与留档。
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import NOTICE_CHANNELS, Customer, NoticeKind, NoticeRecord, validate_period

USAGE_WARNING_TIERS: tuple[int, ...] = (80, 95)


def _payload(used_lines: int, included_lines: int, period: str) -> str:
    ratio = (Decimal(used_lines) / Decimal(included_lines)).quantize(Decimal("0.0001"))
    return json.dumps(
        {
            "used_lines": used_lines,
            "included_lines": included_lines,
            "ratio": str(ratio),
            "period": period,
        },
        ensure_ascii=False,
    )


def already_sent(session: Session, customer_id: int, period: str, threshold_pct: int) -> bool:
    return (
        session.execute(
            select(NoticeRecord).where(
                NoticeRecord.customer_id == customer_id,
                NoticeRecord.period == period,
                NoticeRecord.kind == NoticeKind.USAGE_WARNING.value,
                NoticeRecord.threshold_pct == threshold_pct,
            )
        ).scalar_one_or_none()
        is not None
    )


def evaluate_usage_warnings(
    session: Session,
    *,
    customer: Customer,
    period: str,
    used_lines: int,
    included_lines: int,
) -> list[NoticeRecord]:
    """按已用/含额判定两档预警，新建未发过的档位（幂等），返回新建列表。"""
    validate_period(period)
    if included_lines <= 0:
        return []
    created: list[NoticeRecord] = []
    for pct in USAGE_WARNING_TIERS:
        if used_lines * 100 < pct * included_lines:
            continue
        if already_sent(session, customer.id, period, pct):
            continue
        notice = NoticeRecord(
            customer_id=customer.id,
            kind=NoticeKind.USAGE_WARNING.value,
            period=period,
            threshold_pct=pct,
            channels=NOTICE_CHANNELS,
            payload_json=_payload(used_lines, included_lines, period),
        )
        try:
            with session.begin_nested():
                session.add(notice)
                session.flush()
        except IntegrityError:
            continue  # 并发对手已发——只发一次
        created.append(notice)
    return created
