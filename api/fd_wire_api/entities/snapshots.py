"""本体版本快照服务与日循环（wire-ontology-versioning 任务组 3）。

- **创建**（3.1）：版本号 = 客户内 max+1；manifest=当前态规范形态；digest 为
  规范 JSON 的 sha256。同 digest 不重复落快照（手动版响应如实说「与 vN
  内容一致」，日任务静默跳过）。无本体的客户不产生快照（spec「无本体不出
  快照」）。**快照不改变当前态**——纯读后落档（3.5）。
- **日循环**（3.2）：lifespan 启动的 asyncio 循环，沿 materialization
  scheduler 模式（``asyncio.to_thread`` 下放阻塞 IO）；每客户每天至多一份
  日版（当日已有日版即跳过）；最新快照之后无事件则跳过（避免一串相同
  digest 的噪声，design D5）。首个快照仍是存量本体的基线 v1（design D6：
  存量客户零事件也照出基线）。
- **保留清理**（3.4）：手动版永久；日版只留最近 ``ONTOLOGY_SNAPSHOT_DAILY_KEEP``
  份（默认 30）。
- **手动打发布版**（3.3）走同一 ``create_snapshot``（trigger=manual + label）。

env：``ONTOLOGY_SNAPSHOT_ENABLED``（默认开）/ ``ONTOLOGY_SNAPSHOT_HOUR_UTC``
（默认 ``03:30``，与既有 pg_dump 同刻）/ ``ONTOLOGY_SNAPSHOT_DAILY_KEEP``。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import versioning
from .models import Entity, EntityRelationship, OntologySnapshot
from .models import utc_now_naive

logger = logging.getLogger("fd_wire_api.entities.snapshots")

#: 手动发布版 label 上限（spec：≤80 字）
MAX_LABEL_LENGTH = 80

#: 日循环的默认触发时刻（UTC；与 pg_dump 同刻）
DEFAULT_SNAPSHOT_HOUR_UTC = "03:30"

#: 日版保留份数缺省（手动版永久）
DEFAULT_DAILY_KEEP = 30

#: 日循环单轮睡眠上限（秒）：即使配置时刻很远也让停止/计划变更及时生效
_MAX_SLEEP_SECONDS = 300

_task: asyncio.Task | None = None


# ------------------------------------------------------------------ env


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def snapshot_enabled() -> bool:
    """日快照循环开关（``ONTOLOGY_SNAPSHOT_ENABLED``，默认开）。"""
    return _flag("ONTOLOGY_SNAPSHOT_ENABLED")


def snapshot_hour_utc() -> tuple[int, int]:
    """配置的日快照时刻（UTC 时/分；非法值退回 03:30 并记警告）。"""
    raw = os.getenv("ONTOLOGY_SNAPSHOT_HOUR_UTC", DEFAULT_SNAPSHOT_HOUR_UTC)
    try:
        hour_text, _, minute_text = raw.strip().partition(":")
        hour, minute = int(hour_text), int(minute_text or "0")
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except ValueError:
        pass
    logger.warning("ONTOLOGY_SNAPSHOT_HOUR_UTC 不可解析: %r——退回 03:30", raw)
    return 3, 30


def daily_keep() -> int:
    """日版保留份数（``ONTOLOGY_SNAPSHOT_DAILY_KEEP``，默认 30，最少 1）。"""
    raw = os.getenv("ONTOLOGY_SNAPSHOT_DAILY_KEEP", str(DEFAULT_DAILY_KEEP))
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ONTOLOGY_SNAPSHOT_DAILY_KEEP 不可解析: %r——退回 30", raw)
        return DEFAULT_DAILY_KEEP
    return max(1, value)


# ------------------------------------------------------------------ 创建


def snapshot_customers(db: Session) -> list[str]:
    """有本体（实体或关系）的客户（无本体的客户不出空快照）。"""
    entity_customers = set(
        db.scalars(select(Entity.customer_id).distinct()).all()
    )
    relationship_customers = set(
        db.scalars(select(EntityRelationship.customer_id).distinct()).all()
    )
    return sorted(entity_customers | relationship_customers)


def create_snapshot(
    db: Session,
    *,
    customer_id: str,
    trigger: str,
    label: str | None = None,
    created_by: str | None = None,
    now: datetime | None = None,
) -> tuple[OntologySnapshot | None, str]:
    """给客户打一份快照（幂等按 digest）。

    返回 ``(snapshot, status)``，status ∈:

    - ``created``：新快照已落（调用方负责 commit）
    - ``unchanged``：当前态与最新快照 digest 相同——不新增版本
      （此时 snapshot 返回**已有**的那份，供响应指认「与 vN 内容一致」）
    - ``empty``：客户没有任何实体/关系——不落快照（design D6）
    """
    if trigger not in versioning.TRIGGERS:  # pragma: no cover - 内部常量
        raise ValueError(f"未知快照 trigger: {trigger!r}")
    manifest = versioning.build_manifest(db, customer_id=customer_id)
    if versioning.manifest_is_empty(manifest):
        return None, "empty"
    digest = versioning.manifest_digest(manifest)
    latest = versioning.latest_snapshot(db, customer_id=customer_id)
    if latest is not None and latest.digest == digest:
        return latest, "unchanged"
    version = (latest.version if latest is not None else 0) + 1
    snapshot = OntologySnapshot(
        customer_id=customer_id,
        version=version,
        digest=digest,
        manifest_json=manifest,
        trigger=trigger,
        label=label,
        created_by=created_by,
        # 保留微秒：「最近快照之后的事件」按 created_at 严格比较，同秒内的
        # 事件不该被误判为未发布（截断会把它算进来）
        created_at=(now or utc_now_naive()),
    )
    db.add(snapshot)
    db.flush()
    return snapshot, "created"


def prune_daily_snapshots(
    db: Session, *, customer_id: str, keep: int | None = None
) -> list[int]:
    """日版保留清理：只留最近 ``keep`` 份日版；手动版永久。

    返回被删版本号（升序）。不动当前态与事件流。
    """
    limit = daily_keep() if keep is None else max(1, int(keep))
    daily = [
        snapshot
        for snapshot in versioning.list_snapshots(db, customer_id=customer_id, order="asc")
        if snapshot.trigger == versioning.TRIGGER_DAILY
    ]
    excess = daily[:-limit] if len(daily) > limit else []
    removed: list[int] = []
    for snapshot in excess:
        removed.append(snapshot.version)
        db.delete(snapshot)
    if removed:
        db.flush()
    return removed


# ------------------------------------------------------------------ 日循环


def _already_daily_today(
    snapshots: list[OntologySnapshot], moment: datetime
) -> bool:
    """当日（UTC 日期）是否已有日版（每客户每天至多一份日版）。"""
    for snapshot in snapshots:
        if snapshot.trigger != versioning.TRIGGER_DAILY:
            continue
        created = snapshot.created_at
        if (created.year, created.month, created.day) == (
            moment.year,
            moment.month,
            moment.day,
        ):
            return True
    return False


def tick_once(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> list[dict[str, Any]]:
    """跑一轮日快照（测试与演示共用入口；返回逐客户结果）。

    逐客户判定：无本体→skip(empty)；当日已有日版→skip(already)；
    最新快照后无事件→skip(no_changes)；否则``trigger=daily``落一份。
    最后清理日版保留量。
    """
    moment = (now or utc_now_naive()).replace(microsecond=0)
    if session_factory is None:
        from .db import get_session  # noqa: PLC0415 —— 环规避

        make_session: Callable[[], Session] = get_session
    else:
        make_session = session_factory
    session = make_session()
    outcomes: list[dict[str, Any]] = []
    try:
        for customer_id in snapshot_customers(session):
            snapshots = versioning.list_snapshots(
                session, customer_id=customer_id, order="desc"
            )
            latest = snapshots[0] if snapshots else None
            if _already_daily_today(snapshots, moment):
                outcomes.append(
                    {
                        "customer_id": customer_id,
                        "status": "skipped",
                        "reason": "already_daily_today",
                    }
                )
                continue
            if latest is not None:
                changes = versioning.count_events_since(
                    session,
                    customer_id=customer_id,
                    after_created_at=latest.created_at,
                )
                if changes == 0:
                    logger.info(
                        "ontology snapshot skipped (no changes) customer=%s latest=v%s",
                        customer_id,
                        latest.version,
                    )
                    outcomes.append(
                        {
                            "customer_id": customer_id,
                            "status": "skipped",
                            "reason": "no_changes",
                        }
                    )
                    continue
            snapshot, status = create_snapshot(
                session,
                customer_id=customer_id,
                trigger=versioning.TRIGGER_DAILY,
                now=moment,
            )
            if snapshot is not None:
                prune_daily_snapshots(session, customer_id=customer_id)
            outcomes.append(
                {
                    "customer_id": customer_id,
                    "status": status,
                    "version": snapshot.version if snapshot is not None else None,
                }
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return outcomes


def next_run_after(moment: datetime, hour: int, minute: int) -> datetime:
    """下一次触发时刻（严格晚于 moment；naive UTC）。"""
    candidate = moment.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate <= moment:
        candidate = candidate + timedelta(days=1)
    return candidate


async def _loop() -> None:
    while True:
        now = utc_now_naive()
        hour, minute = snapshot_hour_utc()
        target = next_run_after(now, hour, minute)
        wait_seconds = min(
            max((target - now).total_seconds(), 0.0) + 1.0, _MAX_SLEEP_SECONDS
        )
        await asyncio.sleep(wait_seconds)
        if utc_now_naive() < target:
            continue  # 分段睡眠未到点（配置可能已变，重新计算）
        try:
            await asyncio.to_thread(tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 失败只记日志，等下一轮
            logger.exception("ontology snapshot tick failed; will retry next cycle")


def start_snapshot_scheduler() -> None:
    """启动日快照循环（幂等；需在事件循环内；ONTOLOGY_SNAPSHOT_ENABLED=false 关）。"""
    global _task
    if not snapshot_enabled():
        logger.info("ontology snapshot scheduler disabled by settings")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; ontology snapshot scheduler not started")
        return
    _task = loop.create_task(_loop())
    hour, minute = snapshot_hour_utc()
    logger.info(
        "ontology snapshot scheduler started (daily at %02d:%02d UTC)", hour, minute
    )


def stop_snapshot_scheduler() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None


__all__ = [
    "DEFAULT_DAILY_KEEP",
    "DEFAULT_SNAPSHOT_HOUR_UTC",
    "MAX_LABEL_LENGTH",
    "create_snapshot",
    "daily_keep",
    "next_run_after",
    "prune_daily_snapshots",
    "snapshot_customers",
    "snapshot_enabled",
    "snapshot_hour_utc",
    "start_snapshot_scheduler",
    "stop_snapshot_scheduler",
    "tick_once",
]
