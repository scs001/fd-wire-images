"""需求单流水线衔接与私有 scope 交付端点（wire-platform-v1 任务 6.5+6.6）。

内部侧（``X-Internal-Token``，与既有内部端点一致）：
- ``POST /internal/demands/{id}/pipeline``             记录流水线事实
  {pipeline_run_id, repo?, branch?, note}（append-only，仅 in_progress）
- ``POST /internal/demands/{id}/gate-result``          记录 conformance gate
  结果 {passed, report_path?, note}（append-only 观测留档；wire-ux-
  consolidation 3.1 起不再门禁状态迁移）
- ``POST /internal/demands/{id}/deliver``              交付动作：
  in_progress→delivered + 自动建客户私有 scope（失败如实留档、
  scope_status=pending；delivered+pending 时重试建 scope）
- ``POST /internal/demands/{id}/publish-authorization`` 客户授权转公开/撤回
  {granted, conditions?, indicators?}——append-only 留档

流水线执行本身（scraw-builder / conformance gate / 测试舰队）在外部仓，
真实 gate 命令 ``python3 fd-industry-data/scripts/conformance_gate.py``
（finddata 根、只读）——本仓只消费其结果，绝不代跑。
``GET /internal/demands/{id}``（admin_routes）透出 pipeline_events 与
gate 摘要。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..delivery.scope_client import ScopeClient, scope_client_dep
from . import delivery as delivery_service
from . import pipeline as pipeline_service
from .db import get_db
from .routes import _demand_or_404, _require_internal
from .schemas import (
    DemandInternalOut,
    DeliverIn,
    GateResultIn,
    PipelineRunIn,
    PublishAuthorizationIn,
)
from .statemachine import TransitionError

router = APIRouter(tags=["demands-pipeline"])


@router.post(
    "/internal/demands/{demand_id}/pipeline",
    response_model=DemandInternalOut,
    dependencies=[Depends(_require_internal)],
)
def record_pipeline_run(
    demand_id: int,
    payload: PipelineRunIn,
    db: Session = Depends(get_db),
) -> DemandInternalOut:
    """记录流水线事实（append-only）：run id / 仓 / 分支 / 备注。"""
    demand = _demand_or_404(db, demand_id)
    try:
        demand = pipeline_service.record_pipeline_run(
            db,
            demand,
            pipeline_run_id=payload.pipeline_run_id,
            repo=payload.repo,
            branch=payload.branch,
            note=payload.note,
            by=payload.by,
        )
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DemandInternalOut.model_validate(demand)


@router.post(
    "/internal/demands/{demand_id}/gate-result",
    response_model=DemandInternalOut,
    dependencies=[Depends(_require_internal)],
)
def record_gate_result(
    demand_id: int,
    payload: GateResultIn,
    db: Session = Depends(get_db),
) -> DemandInternalOut:
    """记录 conformance gate 结果：append-only 观测留档（不再门禁迁移）。"""
    demand = _demand_or_404(db, demand_id)
    try:
        demand = pipeline_service.record_gate_result(
            db,
            demand,
            passed=payload.passed,
            report_path=payload.report_path,
            note=payload.note,
            by=payload.by,
        )
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DemandInternalOut.model_validate(demand)


@router.post(
    "/internal/demands/{demand_id}/deliver",
    response_model=DemandInternalOut,
    dependencies=[Depends(_require_internal)],
)
def deliver_demand(
    demand_id: int,
    payload: DeliverIn,
    db: Session = Depends(get_db),
    scope_client: ScopeClient | None = Depends(scope_client_dep),
) -> DemandInternalOut:
    """交付：迁移 delivered + 自动建客户私有 scope（失败→scope_pending，
    同一端点重试）。"""
    demand = _demand_or_404(db, demand_id)
    try:
        demand = delivery_service.deliver_demand(
            db,
            demand,
            scope_client=scope_client,
            by=payload.by,
            note=payload.note,
            indicators=payload.indicators,
            source_dbs=payload.source_dbs,
        )
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DemandInternalOut.model_validate(demand)


@router.post(
    "/internal/demands/{demand_id}/publish-authorization",
    response_model=DemandInternalOut,
    dependencies=[Depends(_require_internal)],
)
def publish_authorization(
    demand_id: int,
    payload: PublishAuthorizationIn,
    db: Session = Depends(get_db),
    scope_client: ScopeClient | None = Depends(scope_client_dep),
) -> DemandInternalOut:
    """客户授权转公开（granted=true 调 publish）/ 撤回（false 调 unpublish），
    授权与撤回都 append-only 留档。"""
    demand = _demand_or_404(db, demand_id)
    try:
        demand = delivery_service.authorize_publish(
            db,
            demand,
            granted=payload.granted,
            conditions=payload.conditions,
            by=payload.by,
            indicators=payload.indicators,
            scope_client=scope_client,
        )
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DemandInternalOut.model_validate(demand)
