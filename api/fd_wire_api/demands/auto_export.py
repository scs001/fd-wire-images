"""受理后自动出单补扫（wire-demand-auto-export）。

ROADMAP 方向 C 的最后一个手工环节是「触发出单」——操作员受理
（submitted→in_progress）后需手动点「转生成工单」。本模块把它自动化：
lifespan 挂 tick 循环，定期补扫「in_progress 且无任何出单尝试留档」的
需求单，逐单调用既有出单 service（``export_workorder_service``——语义/
留档/拒绝口径逐字复用），送入舰队拉取队列。

设计决策：不做同步迁移钩子——出单含 slug 网络探测（fd-prod 海外出站
挡死 raw.githubusercontent.com，每次探测 ~10s 超时），挂在受理请求上会
拖慢操作员响应；补扫天然覆盖两个受理面（内部端点/管理台）与受理时点
前后的进程重启/瞬时异常，单机制更简、响应零延迟。

留痕纪律（与人工出单的差异只在标注，不在语义）：

- 成功：``workorder_exported`` 事件由出单 service 落档，带
  ``trigger="auto"`` 标注（人工出单不带此键——历史零漂移）；
- 被拒（无源地址 422 / slug 占用 409 / 禁爬复检 409）：
  ``auto_export_failed`` 事件（reason + status_code 如实留档），状态零
  变化、零半写——拒绝原因均为需人工处置类，操作员在进度面可见并可用
  人工入口重试；
- 意外异常：同样落 ``auto_export_failed`` 不抛出，循环绝不被单点炸停。

幂等：已有任一尝试留档（``workorder_exported`` 或
``auto_export_failed``）的单一律跳过——失败单不重试防事件刷屏。

lifespan 驱动（orchestration_sync.py 同构）：
``start_auto_export_sweeper()`` / ``stop_auto_export_sweeper()``，tick
缺省 60s（``DEMAND_AUTO_EXPORT_INTERVAL_SECONDS`` 覆盖）、
``DEMAND_AUTO_EXPORT_ENABLED=false`` 关闭。
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, init_db
from .models import Demand
from .orchestration import (
    EVENT_WORKORDER_EXPORTED,
    WorkorderExportError,
    export_workorder_service,
)
from .statemachine import DemandStatus

logger = logging.getLogger(__name__)

#: 自动出单被拒的留痕事件（append-only；reason/status_code 如实落档）。
EVENT_AUTO_EXPORT_FAILED = "auto_export_failed"

#: 自动出单的 trigger 标注（进 workorder_exported 事件；人工出单无此键）。
AUTO_TRIGGER = "auto"

#: 补扫缺省间隔（秒）。60s 是「受理后约一分钟内自动出单」的节奏；
#: slug 探测在 fd-prod 单次可达 10s 超时，间隔下限由循环串行天然限制。
DEFAULT_INTERVAL_SECONDS = 60.0


def _has_export_attempt(events: list[dict]) -> bool:
    """事件流里是否已有出单尝试留档（成功导出或被拒留痕，任一即算）。"""
    return any(
        event.get("type") in (EVENT_WORKORDER_EXPORTED, EVENT_AUTO_EXPORT_FAILED)
        for event in events
    )


def auto_export_demand(db: Session, demand: Demand) -> dict:
    """对单张需求单执行自动出单（幂等；被拒/异常如实留痕不抛出）。

    仅动作于「in_progress 且无任何出单尝试留档」的单；其余一律跳过。
    返回动作摘要：``{"action": "exported"|"skipped"|"failed", ...}``。
    """
    if DemandStatus(demand.status) is not DemandStatus.in_progress:
        return {"action": "skipped", "reason": "not_in_progress"}
    if _has_export_attempt(list(demand.pipeline_events or [])):
        return {"action": "skipped", "reason": "already_attempted"}
    try:
        result = export_workorder_service(db, demand, trigger=AUTO_TRIGGER)
        logger.info(
            "auto export ok: demand=%s ticket=%s",
            demand.code,
            result.get("ticket_path"),
        )
        return {"action": "exported", "ticket_path": result.get("ticket_path")}
    except WorkorderExportError as exc:
        _record_failure(db, demand, reason=exc.detail, status_code=exc.status_code)
        return {"action": "failed", "status_code": exc.status_code}
    except Exception:  # noqa: BLE001 - 单点异常不炸循环，如实留痕
        logger.exception("auto export crashed: demand=%s", demand.code)
        _record_failure(
            db,
            demand,
            reason="自动出单执行异常（详见服务端日志）",
            status_code=None,
        )
        return {"action": "failed", "status_code": None}


def _record_failure(
    db: Session, demand: Demand, *, reason: str, status_code: int | None
) -> None:
    """落 ``auto_export_failed`` 事件（零半写：仅追加事件，状态不迁移）。"""
    try:
        event: dict = {
            "type": EVENT_AUTO_EXPORT_FAILED,
            "trigger": AUTO_TRIGGER,
            "reason": reason,
        }
        if status_code is not None:
            event["status_code"] = status_code
        demand.append_pipeline_event(event)
        db.commit()
        db.refresh(demand)
    except Exception:  # noqa: BLE001 - 留痕失败也不外抛（下轮补扫兜底）
        logger.exception("auto export failure note crashed: demand=%s", demand.code)
        db.rollback()


def sweep_missing_exports(db: Session) -> dict:
    """一轮补扫：in_progress 单逐单自动出单（单点失败不中断其余单）。

    返回摘要 ``{"checked": n, "exported": n, "failed": n, "skipped": n}``。
    """
    stmt = (
        select(Demand)
        .where(Demand.status == DemandStatus.in_progress.value)
        .order_by(Demand.id)
    )
    # 物化列表：循环内逐单 commit（出单/留痕），惰性游标跨 commit 在
    # PG 服务端游标下不可靠（SQLAlchemy 默认缓冲可用，但不赌方言行为）。
    demands = list(db.scalars(stmt).all())
    summary = {"checked": 0, "exported": 0, "failed": 0, "skipped": 0}
    for demand in demands:
        summary["checked"] += 1
        try:
            outcome = auto_export_demand(db, demand)
        except Exception:  # noqa: BLE001 - 兜底：单点异常不中断本轮
            logger.exception("auto export sweep item crashed: demand=%s", demand.id)
            summary["failed"] += 1
            continue
        summary[outcome["action"]] += 1
    return summary


# ---------------------------------------------------------------- lifespan 驱动
# （orchestration_sync.py 同构：async 循环 tick、阻塞 IO 下放线程、异常只
# 记日志下轮再试。）


_task: asyncio.Task[None] | None = None


def _interval_seconds() -> float:
    raw = os.environ.get("DEMAND_AUTO_EXPORT_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def _sweeper_enabled() -> bool:
    return os.environ.get("DEMAND_AUTO_EXPORT_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _tick_once() -> dict[str, int] | None:
    """一轮补扫（阻塞 IO）：自开 demands 会话；返回摘要。"""
    init_db()
    session = SessionLocal()
    try:
        return sweep_missing_exports(session)
    finally:
        session.close()


async def _loop() -> None:
    interval = _interval_seconds()
    while True:
        await asyncio.sleep(interval)
        try:
            # sweep_missing_exports 是阻塞 IO（httpx slug 探测 + SQLAlchemy）
            # ——下放线程
            await asyncio.to_thread(_tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 失败只记日志，等下一轮
            logger.exception(
                "auto export sweep tick crashed; will retry next interval"
            )


def start_auto_export_sweeper() -> None:
    """启动自动出单补扫循环（幂等；需在事件循环内；DEMAND_AUTO_EXPORT_ENABLED=false 关闭）。"""
    global _task
    if not _sweeper_enabled():
        logger.info("auto export sweeper disabled by env")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; auto export sweeper not started")
        return
    _task = loop.create_task(_loop())
    logger.info("auto export sweeper started (interval=%ss)", _interval_seconds())


def stop_auto_export_sweeper() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
