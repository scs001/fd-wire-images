"""刷新调度 CRUD API 路由（wire-refresh-scheduler 3.1）。

登录鉴权照 artifacts 切片（Bearer ID token → ``require_console_customer_id``）：

- ``GET    /api/v1/refresh-schedules``            本人调度列表
- ``POST   /api/v1/refresh-schedules``            建（类型/属主/地板/配额四闸）
- ``DELETE /api/v1/refresh-schedules/{id}``       删（幂等 404 语义）
- ``POST   /api/v1/refresh-schedules/{id}/pause`` 暂停
- ``POST   /api/v1/refresh-schedules/{id}/resume`` 恢复

跨客户一律 404（不泄露他企存在性）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..console.auth import require_console_customer_id
from . import service
from .db import get_db
from .models import RefreshSchedule

router = APIRouter(prefix="/api/v1/refresh-schedules", tags=["refresh-schedules"])


class ScheduleCreateIn(BaseModel):
    """建调度请求体：目标（类型+id）与间隔（分钟）。"""

    target_type: str = Field(min_length=1, max_length=16)
    target_id: str = Field(min_length=1, max_length=64)
    interval_minutes: int = Field(ge=1, le=525_600)


class ScheduleOut(BaseModel):
    """调度卡片：下次运行 / 上次结果 / 上次错误（web 管理小节消费）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    target_type: str
    target_id: str
    interval_minutes: int
    next_run_at: datetime
    paused: bool
    last_status: str
    last_run_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=list[ScheduleOut])
def list_my_schedules(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[ScheduleOut]:
    """本人调度列表（仅本企业；创建时间正序）。"""
    return [
        ScheduleOut.model_validate(row)
        for row in service.list_schedules(db, customer_id)
    ]


@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
def create_schedule(
    payload: ScheduleCreateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ScheduleOut:
    """建调度：类型（dataset 须 derived / artifact 须 dashboard）+ 属主 +
    间隔地板（dataset≥60 / artifact≥360 分钟，env 可调）+ 每客户配额。"""
    try:
        row = service.create_schedule(
            db,
            customer_id=customer_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            interval_minutes=payload.interval_minutes,
        )
    except service.ScheduleTargetNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        service.InvalidScheduleTarget,
        service.IntervalFloorError,
        service.ScheduleQuotaExceeded,
    ) as exc:
        raise _bad_request(exc) from exc
    return ScheduleOut.model_validate(row)


def _owned_or_404(db: Session, schedule_id: int, customer_id: str) -> RefreshSchedule:
    try:
        return service.get_owned_schedule(db, schedule_id, customer_id)
    except service.ScheduleTargetNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/{schedule_id}")
def delete_my_schedule(
    schedule_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """删调度（本人；跨客户 404）。"""
    _owned_or_404(db, schedule_id, customer_id)
    service.delete_schedule(db, schedule_id, customer_id)
    return {"id": schedule_id, "removed": True}


def _toggle(schedule_id: int, *, paused: bool, customer_id: str, db: Session) -> ScheduleOut:
    """暂停/恢复共用骨架：本人取行（404 语义）→ 翻转 paused。"""
    _owned_or_404(db, schedule_id, customer_id)
    row = service.set_paused(db, schedule_id, customer_id, paused=paused)
    return ScheduleOut.model_validate(row)


@router.post("/{schedule_id}/pause", response_model=ScheduleOut)
def pause_schedule(
    schedule_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ScheduleOut:
    """暂停（到期不再执行；恢复后按原 next_run_at 继续）。"""
    return _toggle(schedule_id, paused=True, customer_id=customer_id, db=db)


@router.post("/{schedule_id}/resume", response_model=ScheduleOut)
def resume_schedule(
    schedule_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ScheduleOut:
    """恢复（next_run_at 不重置——已过期即被下一 tick 拾起）。"""
    return _toggle(schedule_id, paused=False, customer_id=customer_id, db=db)
