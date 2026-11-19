"""告警管理面 API 路由（wire-alert-evaluation 任务 3）。

登录鉴权照 artifacts 切片（Bearer ID token → ``require_console_customer_id``）：

- ``GET  /api/v1/alerts/states``                    本企业全部 alert_rule 的
  评估状态（从未评估的规则不出键；last_error_at=最近一条 error 事件时刻）
- ``GET  /api/v1/alerts/events?artifact_id=&limit=`` 事件时间线（id 倒序；
  limit 夹取 1..200 缺省 50；跨客户过滤为空不泄露）
- ``POST /api/v1/artifacts/{id}/alert/test``         试运行：即时评估返回
  {value, would_fire}，**零副作用**（不写 states/events、不通知）——人机
  调试面；查询/归约异常 400 如实递回。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.db import get_db
from ..artifacts import service as artifacts_service
from ..artifacts.models import Artifact
from ..console.auth import require_console_customer_id
from ..datasets.db import get_db as get_datasets_db
from ..datasets.ingest import EngineClientFactory, get_engine_client_factory
from . import evaluator
from .models import AlertEvent, AlertState
from .spec_validate import validate_alert_spec

router = APIRouter(tags=["alerts"])

#: 事件时间线 limit 夹取区间
_EVENTS_LIMIT_FLOOR, _EVENTS_LIMIT_CEIL = 1, 200


def _alert_artifact_or_404(db: Session, artifact_id: int, customer_id: str) -> Artifact:
    """客户侧 alert_rule 产物：不存在/他企/非 alert_rule 一律 404。"""
    try:
        artifact = artifacts_service.get_customer_artifact(
            db, artifact_id=artifact_id, customer_id=customer_id
        )
    except artifacts_service.ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if artifact.kind != "alert_rule":
        raise HTTPException(
            status_code=404,
            detail=f"产物 {artifact_id} 是 {artifact.kind}，仅告警规则（alert_rule）支持试运行。",
        )
    return artifact


@router.get("/api/v1/alerts/states")
def list_alert_states(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """本企业全部 alert_rule 的评估状态（从未评估的规则不出键）。"""
    artifact_ids = list(
        db.scalars(
            select(Artifact.id).where(
                Artifact.customer_id == customer_id, Artifact.kind == "alert_rule"
            )
        )
    )
    if not artifact_ids:
        return {"states": {}}
    states = list(
        db.scalars(
            select(AlertState).where(AlertState.artifact_id.in_(artifact_ids))
        )
    )
    if not states:
        return {"states": {}}
    # 最近一条 error 事件的 created_at（last_error_at；无 error 事件为 None）。
    # GROUP BY artifact_id 必须显式——sqlite 对裸列+聚合宽容、PG 严格拒绝
    # （GroupingError，2026-10-12 生产实弹抓出）。
    last_error_ids = dict(
        db.execute(
            select(AlertEvent.artifact_id, func.max(AlertEvent.id))
            .where(
                AlertEvent.artifact_id.in_(artifact_ids),
                AlertEvent.kind == "error",
            )
            .group_by(AlertEvent.artifact_id)
        ).all()
    )
    error_times: dict[int, object] = {}
    if last_error_ids:
        for event in db.scalars(
            select(AlertEvent).where(AlertEvent.id.in_(list(last_error_ids.values())))
        ):
            error_times[event.artifact_id] = event.created_at
    return {
        "states": {
            str(state.artifact_id): {
                "state": state.state,
                "last_value": state.last_value,
                "last_evaluated_at": state.last_evaluated_at,
                "last_error_at": error_times.get(state.artifact_id),
            }
            for state in states
        }
    }


@router.get("/api/v1/alerts/events")
def list_alert_events(
    artifact_id: int | None = Query(default=None),
    limit: int = Query(default=50),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """本企业告警事件时间线（id 倒序；artifact_id 可选过滤——他企 id 结果
    为空不泄露；limit 夹取 1..200，缺省 50）。"""
    limit = max(_EVENTS_LIMIT_FLOOR, min(_EVENTS_LIMIT_CEIL, limit))
    stmt = select(AlertEvent).where(AlertEvent.customer_id == customer_id)
    if artifact_id is not None:
        stmt = stmt.where(AlertEvent.artifact_id == artifact_id)
    stmt = stmt.order_by(AlertEvent.id.desc()).limit(limit)
    return {
        "events": [
            {
                "id": event.id,
                "artifact_id": event.artifact_id,
                "kind": event.kind,
                "value": event.value,
                "threshold": event.threshold,
                "detail": event.detail,
                "created_at": event.created_at,
            }
            for event in db.scalars(stmt)
        ]
    }


@router.post("/api/v1/artifacts/{artifact_id}/alert/test")
def test_alert_rule(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """试运行（人机调试面）：即时评估当前 spec，返回 {value, would_fire}。

    语义边界：产物不存在/他企/非 alert_rule → 404；spec 校验不过 → 422
    detail=校验文案；查询/归约失败 → 400 detail=原因（如实递回）；
    **零副作用**——不写 states/events、不发通知。
    """
    artifact = _alert_artifact_or_404(db, artifact_id, customer_id)
    spec = artifacts_service.current_spec(db, artifact)
    inner = spec.get("spec") if isinstance(spec, dict) else None
    error = validate_alert_spec(inner)
    if error is not None:
        raise HTTPException(status_code=422, detail=error) from None
    value, would_fire, eval_error = evaluator.evaluate_once(
        datasets_db,
        artifact=artifact,
        spec=spec,
        engine_factory=engine_factory,
    )
    if eval_error is not None:
        raise HTTPException(status_code=400, detail=eval_error) from None
    return {"value": float(value), "would_fire": bool(would_fire)}
