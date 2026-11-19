"""物化调度器：简单 asyncio 循环 + cron 命中判定（不引第三方调度依赖）。

- 每 ``MATERIALIZATION_INTERVAL`` 秒（默认 60）扫一遍 ``materialized_artifacts``
  里 enabled 且带 schedule 的产物；
- 命中判定见 :func:`should_run`：cron 匹配当前（naive UTC）分钟，且该
  分钟尚未跑过（``last_run_at`` 截断到分钟早于当前分钟）——同一命中分钟
  内多次扫描只触发一次；扫描间隔大于 1 分钟时可能跳过中间命中分钟
  （间隔配置与 cron 粒度匹配，见 cron.py 模块 docstring）；
- 运行体（runner：httpx + SQLAlchemy 阻塞 IO）经 ``asyncio.to_thread``
  下放线程，避免卡事件循环；单个产物失败只记日志，不影响同轮其它产物；
- 时钟可注入（``now_fn``）：测试与演示用 fake 时钟驱动 ``tick_once``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Sequence

from sqlalchemy import select

from .cron import CronSchedule, parse_cron
from .db import get_session, utc_now_naive
from .models import MaterializedArtifact
from .runner import MaterializationRunner
from .settings import get_settings

logger = logging.getLogger("fd_wire_api.materialization.scheduler")

#: 调度循环里每个产物运行的 runner 工厂（测试可替换）
RunnerFactory = Callable[[], MaterializationRunner]

_task: asyncio.Task | None = None

#: 调度侧默认 runner 工厂：平台库会话 + 生产 httpx 客户端
def default_runner_factory() -> MaterializationRunner:
    return MaterializationRunner(get_session())


def should_run(
    artifact: MaterializedArtifact,
    moment: datetime,
    *,
    schedules: dict[str, CronSchedule] | None = None,
) -> bool:
    """该产物在此刻（naive UTC，分钟粒度）是否该跑。

    cron 表达式解析失败（登记时未校验 / 事后手改坏）→ 不跑、记日志由
    调用方处理——调度器绝不带着读不懂的 schedule 硬跑。
    """
    if not artifact.enabled or not artifact.schedule:
        return False
    schedule = (schedules or {}).get(artifact.schedule)
    if schedule is None:
        try:
            schedule = parse_cron(artifact.schedule)
        except ValueError:
            logger.warning(
                "materialized artifact %s 的 schedule 不可解析: %r——跳过",
                artifact.id, artifact.schedule,
            )
            return False
        if schedules is not None:
            schedules[artifact.schedule] = schedule
    if not schedule.matches(moment):
        return False
    # 同一命中分钟至多一次：last_run_at（分钟截断）早于当前分钟才跑
    if artifact.last_run_at is not None:
        last_minute = artifact.last_run_at.replace(second=0, microsecond=0)
        current_minute = moment.replace(second=0, microsecond=0)
        if last_minute >= current_minute:
            return False
    return True


def due_artifacts(
    artifacts: Sequence[MaterializedArtifact], moment: datetime
) -> list[MaterializedArtifact]:
    """批量命中判定（cron 解析结果在批内复用）。"""
    schedules: dict[str, CronSchedule] = {}
    return [a for a in artifacts if should_run(a, moment, schedules=schedules)]


def tick_once(
    *,
    now: datetime | None = None,
    runner_factory: RunnerFactory = default_runner_factory,
    session_factory: Callable[[], object] | None = None,
) -> list[tuple[int, str, int]]:
    """扫一轮：取 enabled+带 schedule 的产物 → 命中即跑 runner。

    返回 [(artifact_row_id, status, rows)]。调度循环与测试/演示共用本入口
    （fake 时钟经 ``now`` 注入）。runner 内部自会做额度/凭据/清单检查并
    把 last_run_at 推进——同分钟重复扫描天然去重。
    """
    moment = now or utc_now_naive()
    make_session = session_factory or get_session
    session = make_session()
    try:
        artifacts = list(
            session.execute(
                select(MaterializedArtifact).where(
                    MaterializedArtifact.enabled.is_(True),
                    MaterializedArtifact.schedule.is_not(None),
                )
            ).scalars().all()
        )
        hits = due_artifacts(artifacts, moment)
    finally:
        if hasattr(session, "close"):
            session.close()
    outcomes: list[tuple[int, str, int]] = []
    if not hits:
        return outcomes
    runner = runner_factory()
    try:
        for artifact in hits:
            try:
                result = runner.run_artifact_row(artifact, now=moment)
            except Exception:  # noqa: BLE001 - 单产物失败不殃及同轮其它产物
                logger.exception(
                    "materialization run crashed for artifact %s", artifact.artifact_id
                )
                continue
            outcomes.append((artifact.artifact_id, result.status, result.rows))
            logger.info(
                "materialization run artifact=%s customer=%s status=%s rows=%s",
                artifact.artifact_id, artifact.customer_id, result.status, result.rows,
            )
    finally:
        runner.close()
        runner.release_db()
    return outcomes


async def _loop() -> None:
    interval = get_settings().interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            # tick_once 是阻塞 IO（httpx + SQLAlchemy）——下放线程
            await asyncio.to_thread(tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 失败只记日志，等下一轮
            logger.exception("materialization tick failed; will retry next interval")


def start_materialization_scheduler() -> None:
    """启动物化调度循环（幂等；需在事件循环内；MATERIALIZATION_SCHEDULER_ENABLED=false 关闭）。"""
    global _task
    if not get_settings().materialization_scheduler_enabled:
        logger.info("materialization scheduler disabled by settings")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; materialization scheduler not started")
        return
    _task = loop.create_task(_loop())
    logger.info(
        "materialization scheduler started (interval=%ss)", get_settings().interval_seconds
    )


def stop_materialization_scheduler() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
