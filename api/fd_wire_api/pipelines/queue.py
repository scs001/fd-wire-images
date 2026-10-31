"""租约队列 worker（wire-dag-executor D3/D5）。

单实现、零外部基础设施：``POST /run`` 只落 ``queued`` run（``service.enqueue_run``），
本模块的进程内 asyncio 后台任务领取并执行：

- **领取**：``claimed_by=<worker uuid>`` + ``heartbeat_at``；PG 走
  ``SELECT … FOR UPDATE SKIP LOCKED``，sqlite 事务内 select-then-update；
- **心跳**：每 ``PIPELINE_HEARTBEAT_SECONDS``（缺省 10s，契约 ≤10s）刷新
  ``heartbeat_at``；
- **租约**：``heartbeat_at`` 超 ``PIPELINE_LEASE_SECONDS``（缺省 60s）视为死
  租约 → 其他 worker 可重领取；重领取时已 ``succeeded`` 节点跳过、遗留
  ``running`` 节点记「worker 中断」并在重试预算内重新调度（断点恢复）；
- **短等待窗**：``POST /run`` 等 ``PIPELINE_RUN_WAIT_SECONDS``（缺省 30s）；
  窗口内完成 → 返回既有 run+steps；超窗 → 返回 queued/running run + 运行 id。

**无 worker 时的请求内联执行**：测试环境（pytest 在进程内）与未启 worker 的
单进程部署里，``wait_for_run`` 发现无在进程 worker 就自领自跑——调用的是同
一个 ``execute_claimed_run``（唯一执行语义），不新增第二套调度；否则既有
同步调用方（测试直调 ``POST /run``）会永远等不到人领取。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..datasets.db import get_session
from ..datasets.ingest import get_engine_client_factory
from . import executor
from .executor import (
    NODE_QUEUED,
    NODE_RUNNING,
    RUN_CANCELLED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_TERMINAL,
    _skip_node,
    _utcnow_naive,
)
from .models import PipelineRun, TransformPipeline
from .settings import get_settings

logger = logging.getLogger(__name__)

#: 等待轮询间隔（短等待窗内的状态轮询）
POLL_INTERVAL_SECONDS = 0.2

_task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------- 测试环境判定


def _tests_active() -> bool:
    """pytest 进程内判定（worker 在测试里默认 no-op，显式 env 才启动）。"""
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def worker_active() -> bool:
    """本进程内 worker 循环是否在跑（请求路径据此决定是否内联自领）。"""
    return _task is not None and not _task.done()


# ---------------------------------------------------------------- 领取与执行


def _claimable():
    """可领取条件：queued/running 且无主或心跳过期（死租约）。"""
    cutoff = _utcnow_naive() - timedelta(seconds=get_settings().lease_seconds)
    return (
        PipelineRun.status.in_([RUN_QUEUED, RUN_RUNNING]),
        or_(
            PipelineRun.claimed_by.is_(None),
            PipelineRun.heartbeat_at.is_(None),
            PipelineRun.heartbeat_at < cutoff,
        ),
    )


def claim_next_run(
    db: Session, worker_id: str, *, now=None
) -> PipelineRun | None:
    """领取一条待执行 run（最早入队优先）；无可领取返回 None。"""
    moment = now or _utcnow_naive()
    status_cond, lease_cond = _claimable()
    stmt = (
        select(PipelineRun)
        .where(status_cond, lease_cond)
        .order_by(PipelineRun.id.asc())
        .limit(1)
        # PG: SKIP LOCKED 让多 worker 不互等；sqlite 忽略该子句（单进程）
        .with_for_update(skip_locked=True)
    )
    run = db.execute(stmt).scalars().first()
    if run is None:
        db.rollback()
        return None
    run.claimed_by = worker_id
    run.heartbeat_at = moment
    db.commit()
    db.refresh(run)
    return run


def claim_run(db: Session, run_id: int, worker_id: str, *, now=None) -> PipelineRun | None:
    """定向领取（请求内联执行用）：同租约条件，仅限指定 run id。"""
    moment = now or _utcnow_naive()
    status_cond, lease_cond = _claimable()
    run = (
        db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id, status_cond, lease_cond)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .first()
    )
    if run is None:
        db.rollback()
        return None
    run.claimed_by = worker_id
    run.heartbeat_at = moment
    db.commit()
    db.refresh(run)
    return run


def _heartbeat_loop(run_id: int, worker_id: str, stop: threading.Event) -> None:
    """心跳线程：定期刷新 heartbeat_at（失败只记日志，绝不中断执行）。"""
    interval = max(1.0, float(get_settings().heartbeat_seconds))
    session = get_session()
    try:
        while not stop.wait(interval):
            try:
                run = session.get(PipelineRun, run_id)
                if run is None or run.status in RUN_TERMINAL:
                    return
                if run.claimed_by != worker_id:
                    # 租约已被他人接手（本 worker 失去所有权）：停心跳、如实退出
                    logger.warning(
                        "run %s 租约已转移（%s → %s），停止心跳",
                        run_id,
                        worker_id,
                        run.claimed_by,
                    )
                    return
                run.heartbeat_at = _utcnow_naive()
                session.commit()
            except Exception:  # noqa: BLE001 - 心跳失败不炸执行
                logger.exception("run %s 心跳失败", run_id)
                session.rollback()
    finally:
        session.close()


def execute_claimed_run(
    db: Session,
    run: PipelineRun,
    *,
    engine_factory=None,
    actor: str | None = None,
    worker_id: str | None = None,
    heartbeat: bool = True,
) -> PipelineRun:
    """执行已领取的 run（唯一执行语义入口）。"""
    from .service import PipelineVersionNotFound, _version_row  # noqa: PLC0415

    pipeline = db.get(TransformPipeline, run.pipeline_id)
    if pipeline is None:
        run.status = executor.RUN_FAILED
        run.finished_at = _utcnow_naive()
        run.error_text = f"管线 {run.pipeline_id} 不存在（运行记录悬空）"
        db.commit()
        return run
    try:
        version_row = _version_row(db, pipeline, run.version)
    except PipelineVersionNotFound as exc:
        run.status = executor.RUN_FAILED
        run.finished_at = _utcnow_naive()
        run.error_text = str(exc)
        db.commit()
        return run

    stop = threading.Event()
    heartbeat_thread = None
    if heartbeat and worker_id:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(run.id, worker_id, stop),
            daemon=True,
            name=f"pipeline-heartbeat-{run.id}",
        )
        heartbeat_thread.start()
    try:
        executor.execute_run(
            db,
            run,
            pipeline,
            version_row,
            engine_factory=engine_factory or get_engine_client_factory(),
            actor=actor,
            worker_id=worker_id,
        )
    finally:
        stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
    db.expire_all()
    refreshed = db.get(PipelineRun, run.id)
    return refreshed or run


def worker_tick(
    worker_id: str,
    *,
    engine_factory=None,
    now=None,
    execute=None,
) -> dict:
    """worker 一轮：领取一条并同步执行（返回本轮计数摘要）。

    ``execute`` 可注入（测试替身）；缺省走 ``execute_claimed_run``。
    """
    db = get_session()
    try:
        run = claim_next_run(db, worker_id, now=now)
        if run is None:
            return {"claimed": 0}
        (execute or execute_claimed_run)(
            db, run, engine_factory=engine_factory, worker_id=worker_id
        )
        return {"claimed": 1, "run_id": run.id}
    finally:
        db.close()


# ---------------------------------------------------------------- 短等待窗


def wait_for_run(
    db: Session,
    run_id: int,
    *,
    wait_seconds: float | None = None,
    engine_factory=None,
    actor: str | None = None,
) -> PipelineRun:
    """等待 run 收敛（窗口内）；无 worker 时自领自跑（见模块 docstring）。"""
    settings = get_settings()
    window = settings.run_wait_seconds if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + max(0.0, float(window))
    inline_worker_id = f"pipeline-inline-{uuid.uuid4().hex[:12]}"
    while True:
        db.expire_all()
        run = db.get(PipelineRun, run_id)
        if run is None:
            raise LookupError(f"运行 {run_id} 不存在")
        if run.status in RUN_TERMINAL:
            return run
        if not worker_active():
            claimed = claim_run(db, run_id, inline_worker_id)
            if claimed is not None:
                return execute_claimed_run(
                    db,
                    claimed,
                    engine_factory=engine_factory,
                    actor=actor,
                    worker_id=inline_worker_id,
                )
        if time.monotonic() >= deadline:
            return run
        time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------- 取消


def request_cancel(db: Session, run: PipelineRun) -> PipelineRun:
    """取消请求（design D5）：置位；队列态无人领取时就地收敛。"""
    if run.status in RUN_TERMINAL:
        return run
    if run.cancel_requested_at is None:
        run.cancel_requested_at = _utcnow_naive()
        db.commit()
    db.refresh(run)
    if run.status == RUN_QUEUED and not worker_active():
        for row in executor.node_rows(db, run.id):
            if row.status in (NODE_QUEUED, NODE_RUNNING):
                _skip_node(row)
        run.status = RUN_CANCELLED
        run.finished_at = _utcnow_naive()
        run.error_text = "运行已取消（未启动节点已 skipped）"
        db.commit()
        db.refresh(run)
    return run


# ---------------------------------------------------------------- 后台循环


async def _loop() -> None:
    worker_id = f"pipeline-worker-{uuid.uuid4().hex[:12]}"
    poll = max(0.2, float(get_settings().worker_poll_seconds))
    while True:
        try:
            # worker_tick 是阻塞执行（引擎 MCP HTTP）——下放线程，不堵事件循环
            await asyncio.to_thread(worker_tick, worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单轮失败只记日志，等下一轮
            logger.exception("pipeline worker tick crashed; will retry next interval")
        await asyncio.sleep(poll)


def start_pipeline_worker() -> None:
    """启动租约队列 worker（幂等；需在事件循环内；可经 env 关闭）。

    测试环境（pytest 在进程内）默认 no-op——显式设 ``PIPELINE_WORKER_ENABLED``
    才启动，避免后台 worker 与既有同步断言抢跑。
    """
    global _task
    settings = get_settings()
    if not settings.worker_enabled:
        logger.info("pipeline worker disabled by settings")
        return
    if _tests_active() and os.environ.get("PIPELINE_WORKER_ENABLED") is None:
        logger.info("pipeline worker no-op under tests (set PIPELINE_WORKER_ENABLED=1 to enable)")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; pipeline worker not started")
        return
    _task = loop.create_task(_loop())
    logger.info(
        "pipeline worker started (poll=%ss, concurrency=%s)",
        settings.worker_poll_seconds,
        settings.effective_concurrency,
    )


def stop_pipeline_worker() -> None:
    """停 worker（app lifespan 收尾调用；幂等）。"""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
