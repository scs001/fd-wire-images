"""知验台本体可选定时拉取循环（lifespan 后台 tick，照 orchestration_sync 先例）。

env 三键（缺省零行为变化，design D4——``BENCH_PULL_ENABLED`` 缺省 false，
未配置知验台的部署零改动零请求）：

- ``BENCH_PULL_ENABLED``：``1/true/yes/on`` 才启动（缺省关）；
- ``BENCH_PULL_DOMAINS``：逗号分隔域清单（空 → 无域可拉，循环空转）；
- ``BENCH_PULL_INTERVAL_SECONDS``：tick 间隔，缺省 3600（非法/非正值回落默认）。

失败纪律（spec「定时拉取与失败纪律」）：逐域**串行**拉取、单域 try/except
——失败记入 pull_log（``store.pull_and_store`` 收口，ok=False 脱敏摘要）+
logger.warning 后继续下一域；tick 级异常同样只记日志不断环。调度器异常
绝不影响应用主流程。
"""

from __future__ import annotations

import asyncio
import logging
import os

from .store import _session_factory, init_db, pull_and_store

logger = logging.getLogger(__name__)

#: 循环缺省 tick 间隔（秒）。
DEFAULT_INTERVAL_SECONDS = 3600.0

_task: asyncio.Task[None] | None = None


def _enabled() -> bool:
    """开关（缺省 false：零配置部署零行为变化）。"""
    return os.environ.get("BENCH_PULL_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _domains() -> list[str]:
    """逗号分隔域清单（去空、去重、保序）。"""
    seen: list[str] = []
    for part in os.environ.get("BENCH_PULL_DOMAINS", "").split(","):
        key = part.strip()
        if key and key not in seen:
            seen.append(key)
    return seen


def _interval_seconds() -> float:
    raw = os.environ.get("BENCH_PULL_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def _tick_once() -> dict[str, int]:
    """一轮：对配置域逐个串行拉最新版；单域失败记日志继续，绝不中断。"""
    domains = _domains()
    summary = {"ok": 0, "failed": 0}
    if not domains:
        return summary
    init_db()
    for domain_key in domains:
        db = _session_factory()()
        try:
            snapshot = pull_and_store(db, domain_key=domain_key)
            summary["ok"] += 1
            logger.info(
                "bench pull ok: %s v%s %s",
                domain_key,
                snapshot["version"],
                str(snapshot["digest"])[:12],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单域失败不中断其余域
            summary["failed"] += 1
            logger.warning(
                "bench pull failed: %s: %s", domain_key, exc
            )
        finally:
            db.close()
    return summary


async def _loop() -> None:
    interval = _interval_seconds()
    while True:
        await asyncio.sleep(interval)
        try:
            # 拉取是阻塞 IO（httpx + SQLAlchemy）——下放线程，不堵事件循环。
            await asyncio.to_thread(_tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 失败只记日志，等下一轮
            logger.exception("bench pull tick crashed; will retry next interval")


def start_bench_pull_scheduler() -> None:
    """启动定时拉取循环（幂等；需在事件循环内；BENCH_PULL_ENABLED 缺省关）。"""
    global _task
    if not _enabled():
        logger.debug("bench pull scheduler disabled by env")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; bench pull scheduler not started")
        return
    _task = loop.create_task(_loop())
    logger.info(
        "bench pull scheduler started (domains=%s interval=%ss)",
        ",".join(_domains()) or "-",
        _interval_seconds(),
    )


def stop_bench_pull_scheduler() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "start_bench_pull_scheduler",
    "stop_bench_pull_scheduler",
]
