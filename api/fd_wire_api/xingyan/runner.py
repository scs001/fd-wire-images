"""摄取循环（进程内 tick，照 alerts/evaluator 与 catalog/scheduler 先例）。

为什么不做 K8s CronJob：本仓的周期任务统一走 lifespan 内循环，只有需要
外部补跑时才加 ``/internal`` 端点（本切片两者都有：循环 + 手动触发）。
CronJob 停机窗口错过一轮不补跑的教训（2026-10-26）在这里由「对账脚本
发现落后」兜底，而不是叠一层补偿调度。

阻塞 IO（HTTP 拉取 + SQLite 写）走 ``asyncio.to_thread``，不堵事件循环；
tick 异常只记日志不断环。
"""

from __future__ import annotations

import asyncio
import logging

from .settings import get_settings
from .sync import XingyanFeedSync

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
# 停止信号必须**在运行中的事件循环内**创建：模块级 asyncio.Event 会在 import
# 时绑定当时（或无）循环，测试里换循环即 "bound to a different event loop"。
# 循环变了就重建——语义上等价于「新循环新停止信号」。
_stop: asyncio.Event | None = None
_stop_loop: object | None = None


def _stop_event() -> asyncio.Event:
    global _stop, _stop_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _stop is None or _stop_loop is not loop:
        _stop = asyncio.Event()
        _stop_loop = loop
    return _stop


def _tick() -> None:
    result = XingyanFeedSync().run()
    if result.status == "ok":
        logger.info(
            "xingyan sync ok: sha=%s items=%s claims=%s withdrawn=%s/%s",
            result.source_sha,
            result.items,
            result.claims,
            result.withdrawn_items,
            result.withdrawn_claims,
        )
    elif result.status == "skipped":
        logger.debug("xingyan sync skipped: 同指纹 %s", result.source_sha)
    else:
        # 不冒充成功：旧快照继续服务，staleness 由读面暴露给消费者
        logger.warning("xingyan sync %s: %s", result.status, result.detail[:300])


async def _loop(interval: float, startup_grace: float) -> None:
    if startup_grace:
        try:
            await asyncio.wait_for(_stop_event().wait(), timeout=startup_grace)
            return
        except asyncio.TimeoutError:
            pass
    while not _stop_event().is_set():
        try:
            await asyncio.to_thread(_tick)
        except Exception as exc:  # noqa: BLE001 —— 断环即永久停摆
            logger.warning("xingyan sync tick failed: %s: %s", type(exc).__name__, exc)
        try:
            await asyncio.wait_for(_stop_event().wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


def start_sync_loop() -> None:
    """启动摄取循环（``XINGYAN_SYNC_ENABLED=false`` 时不动）。"""
    global _task
    if _task is not None and not _task.done():
        return
    settings = get_settings()
    if not settings.sync_enabled:
        logger.debug("xingyan sync loop disabled")
        return
    if not settings.sync_on_startup:
        # 只跳过启动即跑，循环照常（首次 tick 在 interval 之后）
        _stop_event().clear()
        _task = asyncio.create_task(_loop(settings.interval_seconds, settings.interval_seconds))
    else:
        _stop_event().clear()
        _task = asyncio.create_task(
            _loop(settings.interval_seconds, settings.startup_grace_seconds)
        )


async def stop_sync_loop() -> None:
    global _task
    _stop_event().set()
    task, _task = _task, None
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
