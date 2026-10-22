"""计量内部端点（/internal/metering）。

- ``POST /internal/metering/events``：HTTP 推送摄取（数组批量），
  ``Authorization: Bearer <METERING_INGEST_TOKEN>`` 校验；令牌未配置时
  fail-closed（403），绝不无令牌放行。
- ``POST /internal/metering/ingest/poll``：触发一轮 registry Mongo 直读
  轮询（配置了 REGISTRY_MONGO_URI/DB 时可用），同样受 ingest 令牌保护。
- ``GET /internal/metering/usage``：按（身份×工具）与合计聚合调用数/行数，
  可按凭据轨道与时间窗过滤。
- ``GET /internal/metering/anomalies`` / ``/anomalies/count``：计费异常
  留痕查询与计数（缺 rows_returned 不静默）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db, to_utc
from .ingest import ingest_events
from .models import MeteringAnomaly, MeteringRecord
from .registry_poller import PollResult, poll_registry_once
from .settings import ALLOWED_TRACKS, get_settings

router = APIRouter(prefix="/internal/metering", tags=["metering"])


# ---------------------------------------------------------------------------
# 鉴权：bearer 令牌（METERING_INGEST_TOKEN）
# ---------------------------------------------------------------------------


async def require_ingest_token(request: Request) -> None:
    """写入面鉴权：Bearer 令牌与 env 严格相等；未配置令牌即 fail-closed。"""
    expected = get_settings().metering_ingest_token
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="missing bearer token")
    if expected is None or not expected:
        # 未配置令牌：拒绝写入而不是无鉴权放行（fail-closed）
        raise HTTPException(status_code=403, detail="ingest token not configured")
    if token.strip() != expected:
        raise HTTPException(status_code=401, detail="invalid ingest token")


# ---------------------------------------------------------------------------
# 摄取（写入面）
# ---------------------------------------------------------------------------


class EventsEnvelope(BaseModel):
    """推送体：对象数组，或 ``{"events": [...]}`` 包装。"""

    events: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/events", dependencies=[Depends(require_ingest_token)])
async def ingest_push(request: Request, db: Session = Depends(get_db)) -> dict[str, int]:
    """批量摄取 registry 审计事件（MCPServerAccessRecord 数组）。

    请求体不锁 Pydantic 模型（audit schema 允许演进，未知字段放行），
    直接取原始 JSON 数组交给规范化层。
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="body must be JSON") from exc
    events = _coerce_events(payload)
    result = ingest_events(db, events)
    return result.as_dict()


@router.post("/ingest/poll", dependencies=[Depends(require_ingest_token)])
def ingest_poll(db: Session = Depends(get_db)) -> dict[str, Any]:
    """触发一轮 registry Mongo 直读轮询（可选路径的手动/定时触发口）。"""
    try:
        poll: PollResult = poll_registry_once(db)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return poll.as_dict()


def _coerce_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and "events" in payload:
        envelope = EventsEnvelope.model_validate(payload)
        return envelope.events
    raise HTTPException(status_code=422, detail="body must be a JSON array of audit events")


# ---------------------------------------------------------------------------
# 聚合查询（读取面）
# ---------------------------------------------------------------------------


class UsageRow(BaseModel):
    caller: str
    tool_name: str | None
    calls: int
    rows: int


class UsageTotals(BaseModel):
    calls: int
    rows: int


class UsageResponse(BaseModel):
    credential_track: str | None
    start: str | None
    end: str | None
    breakdown: list[UsageRow]
    totals: UsageTotals


@router.get("/usage", response_model=UsageResponse, dependencies=[Depends(require_ingest_token)])
def usage(
    credential_track: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> UsageResponse:
    """按（调用者×工具）聚合调用数与行数，附合计；可按轨道与时间窗过滤。"""
    if credential_track is not None and credential_track not in ALLOWED_TRACKS:
        raise HTTPException(
            status_code=422,
            detail=f"credential_track must be one of {list(ALLOWED_TRACKS)}",
        )
    start_dt = _parse_window("start", start)
    end_dt = _parse_window("end", end)

    conditions = []
    if credential_track:
        conditions.append(MeteringRecord.credential_track == credential_track)
    if start_dt:
        conditions.append(MeteringRecord.event_timestamp >= start_dt)
    if end_dt:
        conditions.append(MeteringRecord.event_timestamp <= end_dt)

    breakdown_q = select(
        MeteringRecord.caller,
        MeteringRecord.tool_name,
        func.count(MeteringRecord.id).label("calls"),
        func.sum(MeteringRecord.rows_returned).label("rows"),
    ).group_by(MeteringRecord.caller, MeteringRecord.tool_name)
    for condition in conditions:
        breakdown_q = breakdown_q.where(condition)
    breakdown_q = breakdown_q.order_by(MeteringRecord.caller, MeteringRecord.tool_name)
    rows = db.execute(breakdown_q).all()

    totals_q = select(
        func.count(MeteringRecord.id),
        func.coalesce(func.sum(MeteringRecord.rows_returned), 0),
    )
    for condition in conditions:
        totals_q = totals_q.where(condition)
    total_calls, total_rows = db.execute(totals_q).one()

    return UsageResponse(
        credential_track=credential_track,
        start=start,
        end=end,
        breakdown=[
            UsageRow(caller=caller, tool_name=tool_name, calls=int(calls), rows=int(rows or 0))
            for caller, tool_name, calls, rows in rows
        ],
        totals=UsageTotals(calls=int(total_calls), rows=int(total_rows)),
    )


def _parse_window(name: str, value: str | None) -> Any:
    if value is None:
        return None
    from datetime import datetime

    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be ISO-8601 datetime") from exc


# ---------------------------------------------------------------------------
# 计费异常（读取面）
# ---------------------------------------------------------------------------


class AnomalyCountResponse(BaseModel):
    total: int
    by_reason: dict[str, int]


@router.get("/anomalies/count", response_model=AnomalyCountResponse, dependencies=[Depends(require_ingest_token)])
def anomaly_count(
    reason: str | None = Query(default=None), db: Session = Depends(get_db)
) -> AnomalyCountResponse:
    """计费异常计数（可按 reason 过滤）——缺戳告警的消费口。"""
    total_q = select(func.count(MeteringAnomaly.id))
    group_q = select(MeteringAnomaly.reason, func.count(MeteringAnomaly.id)).group_by(
        MeteringAnomaly.reason
    )
    if reason:
        total_q = total_q.where(MeteringAnomaly.reason == reason)
        group_q = group_q.where(MeteringAnomaly.reason == reason)
    total = db.execute(total_q).scalar_one()
    by_reason = {r: int(c) for r, c in db.execute(group_q).all()}
    return AnomalyCountResponse(total=int(total), by_reason=by_reason)


@router.get("/anomalies", dependencies=[Depends(require_ingest_token)])
def list_anomalies(
    reason: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """最近的计费异常留痕（新→旧），detail 为 JSON 文本。"""
    q = select(MeteringAnomaly).order_by(MeteringAnomaly.id.desc()).limit(limit)
    if reason:
        q = q.where(MeteringAnomaly.reason == reason)
    items = [row.as_dict() for row in db.execute(q).scalars().all()]
    return {"anomalies": items, "count": len(items)}
