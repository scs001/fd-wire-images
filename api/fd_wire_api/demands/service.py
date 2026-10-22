"""需求单业务逻辑：建档、编码、状态迁移。

所有状态变更都收敛到 :func:`apply_transition`——先经状态机白名单校验
（非法即 ``TransitionError`` → 路由层 409），再 append-only 追加时间线。
wire-ux-consolidation 任务 3.1 起为三态机（submitted → in_progress →
delivered，另 rejected 终态）；评估/报价写入（write_evaluation/
submit_quote）挂接的旧态已废除，对现行状态一律 409。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Demand
from .schemas import DemandCreate
from ..billing.money import micro_to_yuan, yuan_to_micro
from .statemachine import (
    DemandStatus,
    TransitionError,
    ensure_transition,
    utc_now_iso,
)


class DemandNotFound(LookupError):
    """需求单不存在（或不属于该客户）——路由层转 404。"""


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _next_code(db: Session) -> str:
    """生成业务编码 ``DE-YYYYMMDD-XX``：按当日已有编码取最大序号 +1。"""
    prefix = f"DE-{datetime.now(timezone.utc):%Y%m%d}-"
    rows = db.execute(
        select(Demand.code).where(Demand.code.like(prefix + "%"))
    ).scalars().all()
    max_seq = 0
    for code in rows:
        try:
            max_seq = max(max_seq, int(code.rsplit("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return f"{prefix}{max_seq + 1:02d}"


def _summary_title(description: str) -> str:
    """由需求描述生成列表摘要（title 列）：截 200 字符，多行压成一行。"""

    one_line = " ".join(description.split())
    return one_line[:200]


def create_demand(db: Session, *, customer_id: str, payload: DemandCreate) -> Demand:
    """客户提交意向单：初始状态 submitted，时间线首条即提交事件。

    四入参 + 合规门两入参落库：description → intent 全文（title 存截断
    摘要）、contact_email / company 直落同名列、indicator_code 原样进
    linked_indicators（目录锚点，预填语义）；source_urls（已过服务端
    硬校验的公开源地址）与 public_data_asserted（公开数据声明，恒为
    True 才能到这）原样落库。
    """
    demand = Demand(
        code=_next_code(db),
        customer_id=customer_id,
        title=_summary_title(payload.description),
        intent=payload.description,
        contact_email=payload.contact_email,
        company=payload.company,
        linked_indicators=[payload.indicator_code]
        if payload.indicator_code
        else [],
        source_urls=list(payload.source_urls),
        public_data_asserted=payload.public_data_asserted,
        status=DemandStatus.submitted.value,
        status_history=[],
    )
    demand.append_history(
        status=DemandStatus.submitted,
        by=f"customer:{customer_id}",
        note="需求单提交" + (
            f"（目录锚点：{payload.indicator_code}）"
            if payload.indicator_code
            else ""
        ),
    )
    db.add(demand)
    try:
        db.commit()
    except IntegrityError:
        # 极小概率的编码并发冲突：换一个序号重试一次。
        db.rollback()
        demand.code = _next_code(db)
        db.add(demand)
        db.commit()
    db.refresh(demand)
    return demand


def list_demands(
    db: Session, *, customer_id: str, status: DemandStatus | None = None
) -> list[Demand]:
    """本企业的需求单列表（按创建时间倒序）。"""
    stmt = select(Demand).where(Demand.customer_id == customer_id)
    if status is not None:
        stmt = stmt.where(Demand.status == status.value)
    stmt = stmt.order_by(Demand.id.desc())
    return list(db.scalars(stmt).all())


def get_demand(db: Session, demand_id: int) -> Demand:
    demand = db.get(Demand, demand_id)
    if demand is None:
        raise DemandNotFound(f"demand {demand_id} 不存在")
    return demand


def get_customer_demand(db: Session, *, demand_id: int, customer_id: str) -> Demand:
    """客户侧详情：不存在或非本企业一律 404（不泄露他企需求单存在性）。"""
    demand = get_demand(db, demand_id)
    if demand.customer_id != customer_id:
        raise DemandNotFound(f"demand {demand_id} 不存在")
    return demand


def apply_transition(
    db: Session,
    demand: Demand,
    *,
    target: DemandStatus,
    by: str,
    note: str | None = None,
    reason: str | None = None,
) -> Demand:
    """执行一次合法迁移并追加时间线（历史只增不改）。

    三态机（wire-ux-consolidation 3.1）：submitted → in_progress →
    delivered，另 rejected 终态（必带 reason）。废除态迁入与废除态迁出
    一律 409（存量归并迁移 alembic 0019 前的读窗口行同样如实回绝）。
    """
    current = DemandStatus(demand.status)
    ensure_transition(current, target, reason=reason)
    entry_note = note
    if target is DemandStatus.rejected:
        entry_note = note or f"拒绝理由：{reason}"
    demand.append_history(status=target, by=by, note=entry_note)
    if target is DemandStatus.rejected:
        demand.rejection_reason = reason
    demand.status = target.value
    demand.updated_at = _utcnow_naive()
    db.commit()
    db.refresh(demand)
    return demand


def write_evaluation(
    db: Session,
    demand: Demand,
    *,
    summary: str,
    by: str = "ai",
    detail: dict | None = None,
) -> Demand:
    """写 AI 预评估结论：summary 客户可见，detail 为内部全文（客户不可见）。

    wire-ux-consolidation 3.1 起：submitted→evaluating 自动接单迁移已被
    三态白名单回绝，本函数对现行状态一律 ``TransitionError``（→409）；
    既有 evaluation 字段随旧单保留可查。detail 为 None 时保留既有内部全文。
    """
    current = DemandStatus(demand.status)
    if current is DemandStatus.submitted:
        apply_transition(
            db, demand, target=DemandStatus.evaluating, by=by, note="后台/AI 接单，开始预评估"
        )
    elif current is not DemandStatus.evaluating:
        raise TransitionError(
            current,
            DemandStatus.evaluating,
            f"当前状态 {current.value} 不允许写入评估结论（仅 submitted/evaluating）",
        )
    demand.evaluation_summary = summary
    if detail is not None:
        demand.evaluation_detail = detail
    demand.updated_at = _utcnow_naive()
    db.commit()
    db.refresh(demand)
    return demand


def submit_quote(
    db: Session,
    demand: Demand,
    *,
    amount: Decimal | None = None,
    amount_micro: int | None = None,
    note: str | None = None,
    valid_until=None,
    operator: str | None = None,
    confirmed_by_operator: bool,
    by: str | None = None,
) -> Demand:
    """人工确认报价（旧八态工作流遗留）：写 quote 字段并迁移 evaluating→quoted。

    wire-ux-consolidation 3.1 起报价不进三态状态机（走定价页自助或线下
    沟通）：仅 evaluating 可报价的前置在现行单上永不满足，一律 409；
    既有 quote 字段随旧单保留可查（设计 D4：不做字段级映射）。

    硬前置（缺一 409，绝不进入 quoted、绝不向客户发出报价）：
    - ``confirmed_by_operator=True``（后台人工确认标志）；
    - ``operator`` 操作人标识（报价必留档操作人）。
    金额以微元整数为准（``amount_micro``）；兼容旧参 ``amount``（元）。
    落库同时 append-only 追加 ``quote_history`` 留档。仅 evaluating 可报价。
    """
    current = DemandStatus(demand.status)
    if current is not DemandStatus.evaluating:
        raise TransitionError(
            current,
            DemandStatus.quoted,
            f"当前状态 {current.value} 不允许报价（须先处于 evaluating）",
        )
    if not confirmed_by_operator:
        raise TransitionError(
            current,
            DemandStatus.quoted,
            "没有后台人工确认（confirmed_by_operator=false），绝不进入 quoted、不发出报价",
        )
    if not (operator or "").strip():
        raise TransitionError(
            current,
            DemandStatus.quoted,
            "缺少 operator 操作人标识：报价必须留档操作人，"
            "confirmed_by_operator 与 operator 缺一不可",
        )
    if amount_micro is None:
        if amount is None:
            raise TransitionError(
                current,
                DemandStatus.quoted,
                "报价必须携带金额（amount_micro；兼容旧参 amount）",
            )
        amount_micro = yuan_to_micro(amount)
    amount_micro = int(amount_micro)
    if amount_micro < 0:
        raise TransitionError(current, DemandStatus.quoted, "报价金额不能为负")

    operator_id = operator.strip()
    demand.quote_amount_micro = amount_micro
    demand.quote_amount = micro_to_yuan(amount_micro)
    demand.quote_note = note
    demand.quote_valid_until = valid_until
    demand.quote_operator = operator_id
    demand.append_quote(
        operator=operator_id,
        amount_micro=amount_micro,
        note=note,
        valid_until=valid_until,
    )
    return apply_transition(
        db,
        demand,
        target=DemandStatus.quoted,
        by=by or f"operator:{operator_id}",
        note=note or "后台人工确认报价",
        confirmed_by_operator=True,
    )
