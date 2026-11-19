"""每日同步调度：简单 asyncio 循环（间隔 env 可配，默认 24h）。

不引第三方调度依赖；进程内以后台任务运行，``stop_sync_scheduler`` 取消。
同步体是阻塞 IO（httpx + SQLAlchemy），经 ``asyncio.to_thread`` 下放线程，
避免卡事件循环。失败只记日志，等待下一个周期。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from .client import HttpRegistryClient
from .settings import settings
from .sync import IndicatorRegistrySync

logger = logging.getLogger("fd_wire_api.catalog.scheduler")

_task: asyncio.Task | None = None


def build_default_sync() -> IndicatorRegistrySync:
    return IndicatorRegistrySync(client=HttpRegistryClient())


async def _run_once() -> None:
    """在守护线程里跑一次同步（阻塞 IO 不卡事件循环，进程退出不被拖住）。"""
    syncer = build_default_sync()
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def _work() -> None:
        try:
            outcome, error = syncer.run(), None
        except Exception as exc:  # noqa: BLE001 - 线程边界汇总
            outcome, error = None, exc
        def _deliver() -> None:
            if not done.done():
                if error is not None:
                    done.set_exception(error)
                else:
                    done.set_result(outcome)
        loop.call_soon_threadsafe(_deliver)

    threading.Thread(target=_work, name="catalog-sync", daemon=True).start()
    result = await done
    logger.info(
        "catalog mirror sync done: mode=%s fetched=%s created=%s updated=%s removed=%s total=%s",
        result.mode,
        result.fetched,
        result.created,
        result.updated,
        result.removed,
        result.total_in_mirror,
    )


async def _loop() -> None:
    try:
        if settings.catalog_sync_on_startup:
            # 小额宽限：避开进程极短生命周期（测试/探活）里的立即外呼
            await asyncio.sleep(settings.startup_grace_seconds)
            await _run_once()
        while True:
            await asyncio.sleep(settings.sync_interval_seconds)
            await _run_once()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("catalog mirror sync failed; will retry next interval")


def start_sync_scheduler() -> None:
    """启动每日同步循环（幂等；需在事件循环内调用；可经 CATALOG_SYNC_ENABLED=false 关闭）。"""
    global _task
    if not settings.catalog_sync_enabled:
        logger.info("catalog sync scheduler disabled by settings")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; catalog sync scheduler not started")
        return
    _task = loop.create_task(_loop())
    logger.info("catalog sync scheduler started (interval=%ss)", settings.sync_interval_seconds)


def stop_sync_scheduler() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
