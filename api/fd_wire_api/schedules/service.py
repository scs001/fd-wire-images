"""调度表服务层：建删列暂停 / due 查询 / 校验（wire-refresh-scheduler 1.2）。

校验四闸（create_schedule 内按序）：

1. **类型**：``target_type`` ∈ {dataset, artifact}；
2. **属主 + 目标类型**：dataset 须本人 ``source_kind=derived`` 的数据集、
   artifact 须本人 ``kind=dashboard`` 的产物——不存在或跨客户一律
   :class:`ScheduleTargetNotFound`（404，不泄露他企存在性）；
3. **间隔地板**：dataset ≥60 分钟、artifact ≥360 分钟（env 可调——
   AI 刷新耗 token，地板封无人值守成本面）；
4. **客户配额**：每客户调度行数 ≤ ``FD_WIRE_SCHEDULE_MAX_PER_CUSTOMER``
   （默认 10）。

目标校验需跨库读（datasets 库 / ai 库），本层自行开会话短查即释放，
不与调用方共享事务。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import LAST_STATUSES, TARGET_TYPES, RefreshSchedule, utc_now_naive
from .settings import get_settings


class ScheduleTargetNotFound(LookupError):
    """目标不存在或跨客户（路由层 404——不泄露他企存在性）。"""


class InvalidScheduleTarget(ValueError):
    """目标类型不满足可调度条件（路由层 400 说明可调度目标类型）。"""


class IntervalFloorError(ValueError):
    """间隔低于类型地板（路由层 400 提示最小间隔）。"""


class ScheduleQuotaExceeded(Exception):
    """超出每客户调度配额（路由层 400 提示上限）。"""


def min_interval_minutes(target_type: str) -> int:
    """该目标类型的最小间隔（分钟，env 可调）。"""
    settings = get_settings()
    if target_type == "dataset":
        return settings.min_interval_dataset_minutes
    if target_type == "artifact":
        return settings.min_interval_artifact_minutes
    raise InvalidScheduleTarget(
        f"target_type 仅支持 {'/'.join(TARGET_TYPES)}——收到 {target_type!r}"
    )


def _validate_dataset_target(customer_id: str, target_id: str) -> None:
    """dataset 目标：须本人派生数据集（derived 来源——可重派生的唯一形态）。"""
    from ..datasets.db import get_session as get_datasets_session
    from ..datasets.models import Dataset

    with get_datasets_session() as db:
        dataset = db.get(Dataset, target_id)
        if dataset is None or dataset.customer_id != customer_id:
            raise ScheduleTargetNotFound(f"数据集 {target_id} 不存在")
        if dataset.source_kind != "derived":
            raise InvalidScheduleTarget(
                f"数据集 {target_id} 来源类型为 {dataset.source_kind}，"
                "仅派生（derived）数据集支持定时刷新"
            )


def _validate_artifact_target(customer_id: str, target_id: str) -> None:
    """artifact 目标：须本人 dashboard 产物（无头刷新轮的唯一支持形态）。"""
    from ..ai.db import get_session as get_ai_session
    from ..artifacts.models import Artifact

    try:
        artifact_id = int(target_id)
    except ValueError as exc:
        raise InvalidScheduleTarget(
            f"artifact 目标的 target_id 需为产物数字 id——收到 {target_id!r}"
        ) from exc
    with get_ai_session() as db:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None or artifact.customer_id != customer_id:
            raise ScheduleTargetNotFound(f"产物 {target_id} 不存在")
        if artifact.kind != "dashboard":
            raise InvalidScheduleTarget(
                f"产物 {artifact.id} 是 {artifact.kind}，仅仪表盘（dashboard）"
                "支持定时 AI 刷新"
            )


def _validate_target(customer_id: str, target_type: str, target_id: str) -> None:
    """目标校验统一入口：类型 → 属主（不存在/跨客户 404 语义）。"""
    if target_type == "dataset":
        _validate_dataset_target(customer_id, target_id)
    elif target_type == "artifact":
        _validate_artifact_target(customer_id, target_id)
    else:
        raise InvalidScheduleTarget(
            f"target_type 仅支持 {'/'.join(TARGET_TYPES)}——收到 {target_type!r}"
        )


def count_schedules(db: Session, customer_id: str) -> int:
    """该客户现有调度行数（配额口径：含 paused 行）。"""
    return int(
        db.scalar(
            select(func.count()).select_from(RefreshSchedule).where(
                RefreshSchedule.customer_id == customer_id
            )
        )
        or 0
    )


def create_schedule(
    db: Session,
    *,
    customer_id: str,
    target_type: str,
    target_id: str,
    interval_minutes: int,
    now: datetime | None = None,
) -> RefreshSchedule:
    """建调度（四闸校验全过后落行）；首次执行在 now+interval。"""
    _validate_target(customer_id, target_type, target_id)
    floor = min_interval_minutes(target_type)
    if interval_minutes < floor:
        raise IntervalFloorError(
            f"{target_type} 调度的最小间隔为 {floor} 分钟——收到 {interval_minutes} 分钟"
        )
    max_per_customer = get_settings().max_per_customer
    if count_schedules(db, customer_id) >= max_per_customer:
        raise ScheduleQuotaExceeded(
            f"客户 {customer_id} 的刷新调度已达上限（{max_per_customer} 条，"
            "FD_WIRE_SCHEDULE_MAX_PER_CUSTOMER 可调）——请先删除不需要的调度"
        )
    moment = now or utc_now_naive()
    row = RefreshSchedule(
        customer_id=customer_id,
        target_type=target_type,
        target_id=target_id,
        interval_minutes=interval_minutes,
        next_run_at=moment + timedelta(minutes=interval_minutes),
        paused=False,
        last_status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_schedules(db: Session, customer_id: str) -> list[RefreshSchedule]:
    """本人调度列表（创建时间正序——先建的先展示）。"""
    return list(
        db.execute(
            select(RefreshSchedule)
            .where(RefreshSchedule.customer_id == customer_id)
            .order_by(RefreshSchedule.created_at, RefreshSchedule.id)
        ).scalars()
    )


def get_owned_schedule(
    db: Session, schedule_id: int, customer_id: str
) -> RefreshSchedule:
    """取本人调度行；不存在或跨客户抛 ScheduleTargetNotFound（404 语义）。"""
    row = db.get(RefreshSchedule, schedule_id)
    if row is None or row.customer_id != customer_id:
        raise ScheduleTargetNotFound(f"调度 {schedule_id} 不存在")
    return row


def delete_schedule(db: Session, schedule_id: int, customer_id: str) -> RefreshSchedule:
    """删调度行（本人；幂等语义由 404 保证——不存在的行直接 404）。"""
    row = get_owned_schedule(db, schedule_id, customer_id)
    db.delete(row)
    db.commit()
    return row


def set_paused(
    db: Session, schedule_id: int, customer_id: str, *, paused: bool
) -> RefreshSchedule:
    """暂停/恢复（恢复后 next_run_at 不重置——到期即被下一 tick 拾起）。"""
    row = get_owned_schedule(db, schedule_id, customer_id)
    row.paused = paused
    db.commit()
    db.refresh(row)
    return row


def due_schedules(
    db: Session, now: datetime, *, limit: int = 50
) -> list[RefreshSchedule]:
    """到点且未暂停的调度（next_run_at 升序——最久欠账的先执行）。"""
    return list(
        db.execute(
            select(RefreshSchedule)
            .where(RefreshSchedule.paused.is_(False), RefreshSchedule.next_run_at <= now)
            .order_by(RefreshSchedule.next_run_at, RefreshSchedule.id)
            .limit(limit)
        ).scalars()
    )


def remove_schedule_row(db: Session, row: RefreshSchedule) -> None:
    """执行器清行入口：目标失存（删/归档）时移除调度（不再空转）。"""
    db.delete(row)
    db.commit()


def mark_running(db: Session, row: RefreshSchedule) -> None:
    """执行前置：last_status=running（调度器全局串行，无并发写竞争）。"""
    row.last_status = "running"
    db.commit()


def mark_finished(
    db: Session,
    row: RefreshSchedule,
    *,
    ok: bool,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """执行后置：ok/failed + last_run_at + last_error，next_run_at=now+interval。"""
    moment = now or utc_now_naive()
    row.last_status = "ok" if ok else "failed"
    row.last_run_at = moment
    row.last_error = error if not ok else None
    row.next_run_at = moment + timedelta(minutes=row.interval_minutes)
    db.commit()


__all__ = [
    "LAST_STATUSES",
    "TARGET_TYPES",
    "IntervalFloorError",
    "InvalidScheduleTarget",
    "RefreshSchedule",
    "ScheduleQuotaExceeded",
    "ScheduleTargetNotFound",
    "count_schedules",
    "create_schedule",
    "delete_schedule",
    "due_schedules",
    "get_owned_schedule",
    "list_schedules",
    "mark_finished",
    "mark_running",
    "min_interval_minutes",
    "remove_schedule_row",
    "set_paused",
]
