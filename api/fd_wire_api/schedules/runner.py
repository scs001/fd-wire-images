"""调度循环（wire-refresh-scheduler 1.4）：lifespan 后台 tick 任务。

照 metering/driver.py 范式（模块级 ``_task`` 句柄 + start/stop）：

- ``FD_WIRE_REFRESH_SCHEDULER_ENABLED`` 默认 true；false 时 start 直接跳过；
- ``FD_WIRE_REFRESH_SCHEDULER_TICK_SECONDS`` tick 间隔，默认 60；
- 每 tick 取 due 且未 paused 的调度**逐个串行**执行（单飞全局串行——同一
  tick 内不并发、不重入）；执行体是阻塞 IO（引擎 MCP / LLM 网关 / SQL），
  经 ``asyncio.to_thread`` 下放线程；
- 单调度执行失败 → ``last_status=failed + last_error``（不阻断同轮其余）；
  tick 级异常只记日志不断环；
- 时钟可注入（``tick_once(now=...)``）与执行器可注入（``execute=...``），
  便于测试无等待驱动。
"""

from __future__ import annotations

import asyncio
import logging

from . import service
from .db import get_session
from .executor import TargetGone, execute_schedule
from .models import utc_now_naive
from .settings import get_settings

logger = logging.getLogger(__name__)

_task: asyncio.Task[None] | None = None


def tick_once(
    *,
    now=None,
    execute=None,
) -> dict[str, int]:
    """一轮调度：取 due 行逐个执行（前置 running → 后置 ok/failed + 清行）。

    - ``now``：可注入时钟（测试固定时刻；naive UTC）；
    - ``execute``：可注入执行器（测试假件；缺省走 executor.execute_schedule）；
    - 执行抛 :class:`TargetGone` → 删除调度行（目标失存自动清理）；
    - 其余异常 → failed + last_error（失败隔离，同轮后续照常）。

    返回本轮计数摘要。
    """
    moment = now or utc_now_naive()
    stats = {"due": 0, "ok": 0, "failed": 0, "removed": 0}
    session = get_session()
    try:
        for row in service.due_schedules(session, moment):
            stats["due"] += 1
            service.mark_running(session, row)
            try:
                (execute or execute_schedule)(row)
            except TargetGone as exc:
                logger.warning("调度 %s 目标失存，自动移除：%s", row.id, exc)
                service.remove_schedule_row(session, row)
                stats["removed"] += 1
            except Exception as exc:  # noqa: BLE001 - 单调度失败不断环
                logger.exception("调度 %s 执行失败（记 failed 后继续）", row.id)
                service.mark_finished(
                    session, row, ok=False, error=str(exc), now=moment
                )
                stats["failed"] += 1
            else:
                service.mark_finished(session, row, ok=True, now=moment)
                stats["ok"] += 1
    finally:
        session.close()
    return stats


async def _loop() -> None:
    interval = get_settings().refresh_scheduler_tick_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            # tick_once 是阻塞 IO（引擎 MCP + LLM 网关 + SQL）——下放线程
            await asyncio.to_thread(tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 失败只记日志，等下一轮
            logger.exception("refresh scheduler tick crashed; will retry next interval")


def start_refresh_scheduler() -> None:
    """启动调度循环（幂等；需在事件循环内；ENABLED=false 关闭）。"""
    global _task
    if not get_settings().refresh_scheduler_enabled:
        logger.info("refresh scheduler disabled by settings")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; refresh scheduler not started")
        return
    _task = loop.create_task(_loop())
    logger.info(
        "refresh scheduler started (interval=%ss)",
        get_settings().refresh_scheduler_tick_seconds,
    )


def stop_refresh_scheduler() -> None:
    """停调度循环（app lifespan 收尾调用；幂等）。"""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
