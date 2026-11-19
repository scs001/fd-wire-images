"""需求单内部视图端点（原 wire-platform-v1 任务 6.4 确认台，三态化收窄）。

- ``POST /internal/demands/{id}/evaluate``  旧八态评估流遗留端点：
  wire-ux-consolidation 3.1 起 evaluating 态废除，对现行状态一律 409
  （评估全文字段随旧单保留可查）。
- ``GET  /internal/demands/{id}``           内部完整视图：
  评估全文（verdict/confidence/reasons/hints/compliance_flags）+
  报价留档（operator/微元金额/历史）——这些字段绝不出现在客户侧响应。

鉴权与既有内部端点一致（``X-Internal-Token``）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import evaluation
from .db import get_db
from .routes import _demand_or_404, _require_internal
from .schemas import DemandInternalOut
from .statemachine import TransitionError

router = APIRouter(tags=["demands-internal"])


@router.get(
    "/internal/demands/{demand_id}",
    response_model=DemandInternalOut,
    dependencies=[Depends(_require_internal)],
)
def get_internal_demand(
    demand_id: int,
    db: Session = Depends(get_db),
) -> DemandInternalOut:
    """内部完整视图：评估全文 + 报价留档（operator 侧确认台上下文）。"""
    demand = _demand_or_404(db, demand_id)
    return DemandInternalOut.model_validate(demand)


@router.post(
    "/internal/demands/{demand_id}/evaluate",
    response_model=DemandInternalOut,
    dependencies=[Depends(_require_internal)],
)
def run_evaluation(
    demand_id: int,
    db: Session = Depends(get_db),
) -> DemandInternalOut:
    """旧八态评估流遗留端点（wire-ux-consolidation 3.1）：一律 409。

    原语义（submitted 自动接单 / evaluating 重跑）随 evaluating 态废除；
    诚实纪律不变：LLM 不可用或输出无法解析时如实落 doubtful——绝不编造结论。
    """
    demand = _demand_or_404(db, demand_id)
    try:
        demand = evaluation.evaluate_demand(db, demand, by="ai")
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DemandInternalOut.model_validate(demand)
