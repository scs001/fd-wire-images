"""DAG 拓扑执行器（wire-dag-executor D2/D3/D4）。

## 职责边界

本模块是**唯一的执行语义实现**：同步核心 ``service.run_pipeline`` 与租约
队列 worker（``queue.py``）都经 ``execute_run`` 落地，不存在第二套调度语义。

- **线性版本（无 ``inputs``）**：走 ``_run_linear``——既有 v1 循环逐字保留
  （顺序、槽位、step 行、失败隔离全同），仅额外写节点行（新增簿记，不改
  既有响应形状）；
- **DAG 版本**：``_run_dag`` 就绪队列 + 拓扑序；某节点全部输入 succeeded
  才可调度；独立分支经线程池真并行（并发上限可配 1..8）；
- **fail-fast**：首个失败节点 → 停止调度、未启动节点记 ``skipped``、在飞
  节点跑完后如实落各自状态，run=failed（错误摘要指首个失败节点）；
- **节点身份**：槽位/节点行按 ``node_key``（=步骤名）定位，读路径同时兼容
  存量 ``seq`` 键行（0048 回填 ``step-<seq>``）；
- **重试**：仅引擎超时/连接错/5xx（``_is_retryable``），退避
  ``PIPELINE_RETRY_BACKOFF_SECONDS``（缺省 5s/30s）；配额硬停与校验错绝不
  重试；
- **超时**：单节点 ``PIPELINE_NODE_TIMEOUT_SECONDS``（缺省 600s）——经引擎
  调用包装器逐调用截止，超时=失败（不在引擎调用返回后才补记，避免「已产出
  却记失败」的假账）；
- **预算**：run 墙钟 ``PIPELINE_RUN_BUDGET_SECONDS``（缺省 1800s）——超预算
  停止调度、未启动节点 skipped、在飞节点跑完，run failed「运行超预算」；
- **断点恢复**：重领取时已 ``succeeded`` 节点跳过（输出与行数不变，不重算），
  遗留 ``running`` 节点记「worker 中断」并在重试预算内重新调度。

## 会话纪律

DAG 节点在独立线程执行，每个节点线程自建 Session（引擎调用是独立 HTTP，
无共享事务）；调度线程只写 run 行/节点状态。节点线程拿到的管线引用是
纯值快照（``_PipelineRef``），绝不跨线程携带 ORM 实例（``expire_all`` 后
的惰性加载会跨线程用调度会话，是线程安全裂缝）。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data_plane.auth import DataPlanePrincipal
from ..datasets import service as datasets_service
from ..datasets.db import get_session
from ..datasets.ingest import EngineCallError, EngineUnavailable
from ..datasets.models import Dataset
from .definition import BASE_NODE, Node, PipelineGraph, build_graph
from .models import (
    PipelineRun,
    PipelineRunNode,
    PipelineRunStep,
    TransformPipeline,
    TransformPipelineVersion,
)
from .settings import get_settings

logger = logging.getLogger(__name__)

#: run 状态（状态机 queued → running → success|failed|cancelled）
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_TERMINAL = frozenset({RUN_SUCCESS, RUN_FAILED, RUN_CANCELLED})

#: 节点状态（queued → running → succeeded|failed|skipped）
NODE_QUEUED = "queued"
NODE_RUNNING = "running"
NODE_SUCCEEDED = "succeeded"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"

#: 步骤级留痕状态（沿 v1 口径：success|failed|quota_exhausted）
STEP_SUCCESS = "success"
STEP_FAILED = "failed"
STEP_QUOTA_EXHAUSTED = "quota_exhausted"

#: worker 中断原因文案（断点恢复的事实标注）
INTERRUPTED_REASON = "worker 中断"


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NodeTimeout(RuntimeError):
    """节点超时（引擎调用超过单节点截止）——可重试错误。"""


class MissingInput(RuntimeError):
    """上游节点没有可用输出（定义/状态异常）——不调度，如实失败。"""


@dataclass
class NodeResult:
    """单节点执行结果（调度器只读它，DB 行由节点线程自己落）。"""

    node_key: str
    seq: int
    name: str
    op: str
    status: str
    output_dataset_id: str | None = None
    row_count: int | None = None
    duration_ms: int = 0
    error_text: str | None = None
    attempts: int = 1


@dataclass(frozen=True)
class _PipelineRef:
    """跨线程传递的管线纯值快照（不携带 ORM 会话绑定）。"""

    id: str
    name: str
    customer_id: str


# ---------------------------------------------------------------- 错误判定


def _is_retryable(exc: Exception) -> bool:
    """仅引擎超时/连接错/HTTP 5xx 可重试（design D4）。

    配额硬停（``QuotaExceeded``）、参数/校验错（4xx）与业务性
    ``StepFailure`` 一律不重试——重试只会重复烧配额或重复同一错误。
    """
    if isinstance(exc, (NodeTimeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, (EngineUnavailable, EngineCallError)):
        return True
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, HTTPException):
        return 500 <= int(exc.status_code) < 600
    return False


def _is_quota_hardstop(exc: Exception) -> bool:
    """配额硬停判定：``QuotaExceeded`` 或端点转出的 429（``quota: …``）。

    端点（data_plane/datasets）把 ``QuotaExceeded`` 转成 HTTP 429 后，节点
    线程看到的是 HTTPException——同样必须记 ``quota_exhausted`` 且绝不重试。
    """
    if isinstance(exc, datasets_service.QuotaExceeded):
        return True
    return isinstance(exc, HTTPException) and int(exc.status_code) == 429


class _DeadlineClient:
    """引擎客户端截止包装：每次 ``call`` 超期即抛 ``NodeTimeout``。

    引擎调用是阻塞 HTTP；``join`` 超时后本线程立刻失败，遗留的引擎线程只
    等 HTTP 返回（不写库）——不会出现「节点已记失败但稍后又绑槽位」的假账。
    """

    def __init__(self, client, deadline: float, label: str) -> None:
        self._client = client
        self._deadline = deadline
        self._label = label

    def call(self, tool: str, arguments: dict):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise NodeTimeout(
                f"节点 {self._label} 超时（引擎调用 {tool} 未在截止前发起）"
            )
        box: dict = {}

        def _target() -> None:
            try:
                box["result"] = self._client.call(tool, arguments)
            except BaseException as exc:  # noqa: BLE001 - 原样回抛到调用线程
                box["error"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(remaining)
        if thread.is_alive():
            raise NodeTimeout(
                f"节点 {self._label} 超时（引擎调用 {tool} 超过 {remaining:.1f}s）"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]

    def __getattr__(self, item):
        return getattr(self._client, item)


def _deadline_factory(engine_factory, deadline: float, label: str):
    """按客户 id 造带截止的引擎客户端工厂（形状同 ``EngineClientFactory``）。"""

    def make(customer_id: str):
        return _DeadlineClient(engine_factory(customer_id), deadline, label)

    return make


# ---------------------------------------------------------------- 节点行簿记


def _run_node_row(db: Session, run_id: int, node_key: str) -> PipelineRunNode | None:
    return db.execute(
        select(PipelineRunNode).where(
            PipelineRunNode.run_id == run_id,
            PipelineRunNode.node_key == node_key,
        )
    ).scalar_one_or_none()


def ensure_node_rows(
    db: Session, run_id: int, graph: PipelineGraph
) -> dict[str, PipelineRunNode]:
    """幂等建节点行（queued）——enqueue 时先建，执行时补齐。"""
    rows: dict[str, PipelineRunNode] = {}
    existing = {
        row.node_key: row
        for row in db.execute(
            select(PipelineRunNode).where(PipelineRunNode.run_id == run_id)
        ).scalars()
    }
    for node in graph.nodes:
        row = existing.get(node.key)
        if row is None:
            row = PipelineRunNode(
                run_id=run_id, node_key=node.key, op=node.op, status=NODE_QUEUED
            )
            db.add(row)
        rows[node.key] = row
    db.commit()
    return rows


def _resume_interrupted(
    db: Session, rows: dict[str, PipelineRunNode], graph: PipelineGraph
) -> None:
    """重领取时的断点恢复：running 行记「worker 中断」并重新排队。

    已 ``succeeded`` 行原样保留（不重算）；``failed``/``skipped`` 为既有
    终态，保留如实事实。中断**不消耗**节点重试预算（那次执行从未完成，
    不是一次失败尝试）；``attempts`` 在重跑时自增，中断事实记在
    ``error_text``（成功后保留为「worker 中断（已恢复重跑）」）。
    """
    by_key = graph.by_key()
    for key, row in rows.items():
        if row.status != NODE_RUNNING:
            continue
        node = by_key[key]
        if int(row.attempts or 0) > node.retries + 1:
            # 既有尝试次数已超预算：不再重跑，如实记 failed
            row.status = NODE_FAILED
            row.finished_at = _utcnow_naive()
            row.error_text = f"{INTERRUPTED_REASON}（重试预算已耗尽）"
        else:
            row.status = NODE_QUEUED
            row.started_at = None
            row.finished_at = None
            row.error_text = INTERRUPTED_REASON
    db.commit()


def _skip_node(row: PipelineRunNode) -> None:
    row.status = NODE_SKIPPED
    row.finished_at = _utcnow_naive()


# ---------------------------------------------------------------- 执行入口


def execute_new_run(
    db: Session,
    pipeline: TransformPipeline,
    *,
    version: int | None,
    trigger: str,
    engine_factory,
    actor: str | None,
) -> tuple[PipelineRun, list[PipelineRunStep]]:
    """同步核心：建 run 行后立即执行（``service.run_pipeline`` 的落地面）。"""
    from .service import _version_row  # noqa: PLC0415 - 避免模块级环

    version_no = pipeline.current_version if version is None else int(version)
    version_row = _version_row(db, pipeline, version_no)
    run = PipelineRun(
        pipeline_id=pipeline.id,
        version=version_no,
        trigger=trigger,
        status=RUN_RUNNING,
        started_at=_utcnow_naive(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    step_rows = execute_run(
        db,
        run,
        pipeline,
        version_row,
        engine_factory=engine_factory,
        actor=actor,
    )
    return run, step_rows


def execute_run(
    db: Session,
    run: PipelineRun,
    pipeline: TransformPipeline,
    version_row: TransformPipelineVersion,
    *,
    engine_factory,
    actor: str | None = None,
    worker_id: str | None = None,
) -> list[PipelineRunStep]:
    """按版本执行一遍（线性或 DAG），返回本次落库的 step 行（按 seq 升序）。"""
    graph = build_graph(list(version_row.steps_json or []))
    principal = DataPlanePrincipal(
        customer_id=pipeline.customer_id,
        channel="scheduler" if run.trigger == "schedule" else "console",
        actor_id=actor or pipeline.customer_id,
    )

    rows = ensure_node_rows(db, run.id, graph)
    _resume_interrupted(db, rows, graph)

    run.status = RUN_RUNNING
    if worker_id:
        run.claimed_by = worker_id
        run.heartbeat_at = _utcnow_naive()
    db.commit()

    if graph.is_linear:
        _run_linear(
            db,
            run,
            pipeline,
            version_row,
            graph,
            rows,
            engine_factory=engine_factory,
            principal=principal,
        )
    else:
        _run_dag(
            db,
            run,
            pipeline,
            version_row,
            graph,
            rows,
            engine_factory=engine_factory,
            principal=principal,
        )

    from .service import _prune_runs  # noqa: PLC0415 - 避免模块级环

    _prune_runs(db, pipeline.id)
    db.expire_all()
    return list(
        db.execute(
            select(PipelineRunStep)
            .where(PipelineRunStep.run_id == run.id)
            .order_by(PipelineRunStep.seq.asc())
        ).scalars()
    )


def node_rows(db: Session, run_id: int) -> list[PipelineRunNode]:
    """run 的节点行（按建行序 = 定义序）。"""
    db.expire_all()
    return list(
        db.execute(
            select(PipelineRunNode)
            .where(PipelineRunNode.run_id == run_id)
            .order_by(PipelineRunNode.id.asc())
        ).scalars()
    )


# ---------------------------------------------------------------- 线性（v1 逐字）


def _run_linear(
    db: Session,
    run: PipelineRun,
    pipeline: TransformPipeline,
    version_row: TransformPipelineVersion,
    graph: PipelineGraph,
    rows: dict[str, PipelineRunNode],
    *,
    engine_factory,
    principal: DataPlanePrincipal,
) -> None:
    """既有 v1 线性循环（顺序、槽位、step 行、失败隔离逐字保留）。

    与变更前的唯一差别是额外维护节点行（``queued → running → ...``）与按
    ``node_key`` 定位槽位（读路径兼容存量 ``seq`` 行）。
    """
    from .service import (
        StepFailure,
        _bind_slot,
        _create_step_output,
        _error_text,
        _slot_for,
    )

    steps = list(version_row.steps_json or [])
    version_no = version_row.version
    input_id = version_row.base_dataset_id
    run_error: str | None = None
    failed_index: int | None = None
    cancel_index: int | None = None

    # 断点恢复（D3）：线性链的已成功前缀不重算，从首个未完成节点继续
    start_index = 0
    for node in graph.nodes:
        row = rows.get(node.key)
        if (
            row is not None
            and row.status == NODE_SUCCEEDED
            and row.output_dataset_id
        ):
            input_id = row.output_dataset_id
            start_index += 1
        else:
            break

    for index, step in enumerate(steps, start=1):
        if index <= start_index:
            continue
        node = graph.nodes[index - 1]
        seq = node.seq
        step_name = str(step.get("name") or f"step-{seq}")
        op = str(step.get("op") or "")
        # 取消请求（D5）：v1 定义从不置位该列，无取消时与既有语义逐字一致
        db.refresh(run)
        if run.cancel_requested_at is not None:
            cancel_index = index - 1
            break
        row = rows.get(node.key)
        if row is not None:
            row.status = NODE_RUNNING
            row.attempts = max(1, int(row.attempts or 0) + 1)
            row.started_at = row.started_at or _utcnow_naive()
            db.commit()
        slot = _slot_for(db, pipeline.id, version_no, node.key, seq)
        started = time.monotonic()
        status = STEP_SUCCESS
        output_id: str | None = None
        row_count: int | None = None
        error: str | None = None
        try:
            if slot is not None:
                # 同版本重跑：原地刷新（统一路，含质量快照/级联语义——见
                # service 模块 docstring D5 段）。槽位悬空如实失败。
                dataset = db.get(Dataset, slot.dataset_id)
                if dataset is None or dataset.customer_id != pipeline.customer_id:
                    raise StepFailure(
                        f"步骤 {seq} 输出数据集已删除（槽位悬空: {slot.dataset_id}）"
                        "——请新建版本重建输出"
                    )
                _refresh(db, dataset, engine_factory, principal)
                output_id = dataset.id
                row_count = dataset.row_count
            else:
                source = db.get(Dataset, input_id)
                if source is None or source.customer_id != pipeline.customer_id:
                    raise StepFailure(f"步骤 {seq} 输入数据集不存在: {input_id}")
                child = _create_step_output(
                    db,
                    pipeline=pipeline,
                    version=version_no,
                    seq=seq,
                    step=step,
                    input_dataset=source,
                    engine_factory=engine_factory,
                    principal=principal,
                )
                _bind_slot(
                    db, pipeline.id, version_no, seq, child.id, node_key=node.key
                )
                output_id = child.id
                row_count = child.row_count
        except datasets_service.QuotaExceeded as exc:
            status = STEP_QUOTA_EXHAUSTED
            error = _error_text(exc)
            if slot is not None:
                output_id = slot.dataset_id  # 既有输出保留（引用如实）
        except (HTTPException, StepFailure, ValueError) as exc:
            status = STEP_FAILED
            error = _error_text(exc)
            if slot is not None:
                output_id = slot.dataset_id
        duration_ms = int((time.monotonic() - started) * 1000)
        step_row = PipelineRunStep(
            run_id=run.id,
            seq=seq,
            name=step_name,
            op=op,
            status=status,
            output_dataset_id=output_id,
            row_count=row_count,
            duration_ms=duration_ms,
            error_text=error,
        )
        db.add(step_row)
        db.commit()
        if row is not None:
            row.status = (
                NODE_SUCCEEDED if status == STEP_SUCCESS else NODE_FAILED
            )
            row.finished_at = _utcnow_naive()
            row.output_dataset_id = output_id
            row.row_count = row_count
            row.duration_ms = duration_ms
            row.error_text = error
            db.commit()
        if status != STEP_SUCCESS:
            run_error = f"步骤 {seq}（{step_name}/{op}）失败: {error}"
            failed_index = index
            break  # 失败隔离：剩余步骤不执行、不留 step 行
        input_id = output_id

    # 未启动节点如实记 skipped（节点行新增口径；step 行保持 v1「无记录」）
    stopped_at = failed_index if failed_index is not None else cancel_index
    if stopped_at is not None:
        for node in graph.nodes[stopped_at:]:
            row = rows.get(node.key)
            if row is not None and row.status in (NODE_QUEUED, NODE_RUNNING):
                _skip_node(row)
        db.commit()

    if run_error:
        run.status = RUN_FAILED
        run.error_text = run_error
    elif cancel_index is not None:
        run.status = RUN_CANCELLED
        run.error_text = "运行已取消（未启动节点已 skipped）"
    else:
        run.status = RUN_SUCCESS
        run.error_text = None
    run.finished_at = _utcnow_naive()
    db.commit()


def _refresh(db: Session, dataset: Dataset, engine_factory, principal) -> None:
    from ..data_plane.data_routes import refresh_derived_dataset  # noqa: PLC0415

    refresh_derived_dataset(db, dataset, engine_factory, principal)


# ---------------------------------------------------------------- DAG 调度


def _single_connection_pool() -> bool:
    """当前 datasets 引擎是否只有一个共享连接（SQLite StaticPool / 内存库）。

    并行节点在独立线程里各开 Session；共享单连接（``sqlite://`` 内存库）
    下并发 flush/commit 会互相打断（sqlite3.InterfaceError / 事务被回滚）。
    这种部署形态下如实降级为串行（并发 1），不伪造并行。
    """
    try:
        from ..datasets.db import get_engine  # noqa: PLC0415

        pool = get_engine().pool
    except Exception:  # noqa: BLE001 - 探测失败按可并行处理（PG 常态）
        return False
    return pool.__class__.__name__ == "StaticPool"


def _concurrency_for(settings) -> int:
    concurrency = settings.effective_concurrency
    if concurrency > 1 and _single_connection_pool():
        logger.warning(
            "datasets 库为单连接池（SQLite 内存库）——DAG 节点按 1 串行执行，"
            "并行需文件库或 PG"
        )
        return 1
    return concurrency


def _run_dag(
    db: Session,
    run: PipelineRun,
    pipeline: TransformPipeline,
    version_row: TransformPipelineVersion,
    graph: PipelineGraph,
    rows: dict[str, PipelineRunNode],
    *,
    engine_factory,
    principal: DataPlanePrincipal,
) -> None:
    """就绪队列 + 并发上限 + fail-fast（design D2）。"""
    settings = get_settings()
    concurrency = _concurrency_for(settings)
    budget = settings.run_budget_seconds
    exec_started = time.monotonic()
    base_dataset_id = version_row.base_dataset_id
    pipeline_ref = _PipelineRef(
        id=pipeline.id, name=pipeline.name, customer_id=pipeline.customer_id
    )

    states = {node.key: rows[node.key].status for node in graph.nodes}
    outputs: dict[str, str | None] = {
        node.key: rows[node.key].output_dataset_id for node in graph.nodes
    }
    # 重领取时既有 failed 行是上一轮的事实——首个失败即本次 run 的错误摘要
    prior_failure: NodeResult | None = None
    for node in graph.nodes:
        row = rows[node.key]
        if row.status == NODE_FAILED and prior_failure is None:
            prior_failure = NodeResult(
                node_key=node.key,
                seq=node.seq,
                name=node.name,
                op=node.op,
                status=STEP_FAILED,
                error_text=row.error_text or "节点此前已失败",
            )

    in_flight: dict[Future, Node] = {}
    first_failure: NodeResult | None = prior_failure
    cancel_requested = False
    budget_exceeded = False

    def _inputs_for(node: Node) -> list[str]:
        ids: list[str] = []
        for ref in node.inputs:
            if ref == BASE_NODE:
                ids.append(base_dataset_id)
                continue
            out = outputs.get(ref)
            if out is None:
                raise MissingInput(
                    f"步骤 {node.seq}（{node.name}/{node.op}）输入节点 {ref} 无可用输出"
                )
            ids.append(out)
        return ids

    pool = ThreadPoolExecutor(
        max_workers=max(1, concurrency), thread_name_prefix=f"pipeline-{run.id}"
    )
    try:
        while True:
            if first_failure is None and not cancel_requested and not budget_exceeded:
                db.expire_all()
                current = db.get(PipelineRun, run.id)
                if current is not None and current.cancel_requested_at is not None:
                    cancel_requested = True
                if time.monotonic() - exec_started > budget:
                    budget_exceeded = True

            stopped = bool(first_failure or cancel_requested or budget_exceeded)
            if not stopped:
                ready = [
                    node
                    for node in graph.nodes
                    if states[node.key] == NODE_QUEUED
                    and all(
                        ref == BASE_NODE or states.get(ref) == NODE_SUCCEEDED
                        for ref in node.inputs
                    )
                ]
                while ready and len(in_flight) < concurrency:
                    node = ready.pop(0)
                    try:
                        input_ids = _inputs_for(node)
                    except MissingInput as exc:
                        # 输入缺失是定义/状态异常：如实失败，不调度
                        row = rows[node.key]
                        row.status = NODE_FAILED
                        row.finished_at = _utcnow_naive()
                        row.error_text = str(exc)
                        db.commit()
                        states[node.key] = NODE_FAILED
                        if first_failure is None:
                            first_failure = NodeResult(
                                node_key=node.key,
                                seq=node.seq,
                                name=node.name,
                                op=node.op,
                                status=STEP_FAILED,
                                error_text=str(exc),
                            )
                        break
                    states[node.key] = NODE_RUNNING
                    in_flight[
                        pool.submit(
                            _execute_node,
                            node,
                            pipeline_ref=pipeline_ref,
                            version_no=version_row.version,
                            input_ids=input_ids,
                            run_id=run.id,
                            engine_factory=engine_factory,
                            principal=principal,
                        )
                    ] = node

            if not in_flight:
                break

            done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                node = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - 线程体异常如实落 failed
                    logger.exception("节点 %s 执行线程异常", node.key)
                    result = NodeResult(
                        node_key=node.key,
                        seq=node.seq,
                        name=node.name,
                        op=node.op,
                        status=STEP_FAILED,
                        error_text=str(exc),
                    )
                states[node.key] = (
                    NODE_SUCCEEDED if result.status == STEP_SUCCESS else NODE_FAILED
                )
                outputs[node.key] = result.output_dataset_id
                if result.status != STEP_SUCCESS and first_failure is None:
                    first_failure = result
    finally:
        pool.shutdown(wait=True)

    # 未启动节点如实 skipped（fail-fast / 取消 / 超预算同一口径）
    for node in graph.nodes:
        if states.get(node.key) != NODE_QUEUED:
            continue
        row = rows[node.key]
        _skip_node(row)
        states[node.key] = NODE_SKIPPED
    db.commit()

    elapsed = time.monotonic() - exec_started
    if first_failure is not None:
        run.status = RUN_FAILED
        run.error_text = (
            f"步骤 {first_failure.seq}（{first_failure.name}/{first_failure.op}）失败: "
            f"{first_failure.error_text}"
        )
    elif cancel_requested:
        run.status = RUN_CANCELLED
        run.error_text = "运行已取消（未启动节点已 skipped，在飞节点已跑完并如实落状态）"
    elif budget_exceeded:
        run.status = RUN_FAILED
        run.error_text = (
            f"运行超预算（墙钟 {elapsed:.1f}s 超过预算 {budget}s）"
            "——未启动节点已 skipped，在飞节点已跑完"
        )
    elif any(state != NODE_SUCCEEDED for state in states.values()):
        run.status = RUN_FAILED
        run.error_text = "调度未能收敛（存在未执行节点）——请检查定义与槽位"
    else:
        run.status = RUN_SUCCESS
    run.finished_at = _utcnow_naive()
    db.commit()


def _execute_node(
    node: Node,
    *,
    pipeline_ref: _PipelineRef,
    version_no: int,
    input_ids: list[str],
    run_id: int,
    engine_factory,
    principal: DataPlanePrincipal,
) -> NodeResult:
    """节点线程体：重试 / 超时 / 落节点行与 step 行（自带会话，线程安全）。"""
    settings = get_settings()
    session = get_session()
    started = time.monotonic()
    attempts = 0
    try:
        row = _run_node_row(session, run_id, node.key)
        if row is None:  # 防御：调度前已 ensure，缺失即如实失败
            raise RuntimeError(f"节点 {node.key} 无状态行（run {run_id}）")
        attempts = int(row.attempts or 0)
        if row.status == NODE_RUNNING:
            row.status = NODE_QUEUED
        while True:
            attempts += 1
            row.status = NODE_RUNNING
            row.started_at = row.started_at or _utcnow_naive()
            row.attempts = attempts
            session.commit()
            deadline = time.monotonic() + settings.node_timeout_seconds
            scoped_factory = _deadline_factory(
                engine_factory, deadline, f"{node.key}/{node.op}"
            )
            try:
                output_id, row_count = _run_node_body(
                    session,
                    node=node,
                    pipeline_ref=pipeline_ref,
                    version_no=version_no,
                    input_ids=input_ids,
                    engine_factory=scoped_factory,
                    principal=principal,
                )
            except Exception as exc:  # noqa: BLE001 - 一律如实落失败
                error = _error_text(exc)
                # 配额硬停（含端点把 QuotaExceeded 转成的 429）：不重试，如实
                # 记 quota_exhausted（spec「配额硬停不重试」）
                if _is_quota_hardstop(exc):
                    _finish_node_row(
                        session, row, STEP_QUOTA_EXHAUSTED, error, started, attempts
                    )
                    _write_step_row(
                        session,
                        run_id,
                        node,
                        STEP_QUOTA_EXHAUSTED,
                        None,
                        None,
                        error,
                        started,
                    )
                    return NodeResult(
                        node_key=node.key,
                        seq=node.seq,
                        name=node.name,
                        op=node.op,
                        status=STEP_QUOTA_EXHAUSTED,
                        error_text=error,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        attempts=attempts,
                    )
                if _is_retryable(exc) and attempts <= node.retries:
                    row.error_text = f"第 {attempts} 次尝试失败（可重试）: {error}"
                    session.commit()
                    backoff = settings.retry_backoff_seconds
                    delay = backoff[min(attempts - 1, len(backoff) - 1)]
                    time.sleep(delay)
                    continue
                _finish_node_row(session, row, STEP_FAILED, error, started, attempts)
                _write_step_row(
                    session, run_id, node, STEP_FAILED, None, None, error, started
                )
                return NodeResult(
                    node_key=node.key,
                    seq=node.seq,
                    name=node.name,
                    op=node.op,
                    status=STEP_FAILED,
                    error_text=error,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts,
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            _finish_node_row(
                session,
                row,
                STEP_SUCCESS,
                None,
                started,
                attempts,
                output_id=output_id,
                row_count=row_count,
            )
            _write_step_row(
                session, run_id, node, STEP_SUCCESS, output_id, row_count, None, started
            )
            return NodeResult(
                node_key=node.key,
                seq=node.seq,
                name=node.name,
                op=node.op,
                status=STEP_SUCCESS,
                output_dataset_id=output_id,
                row_count=row_count,
                duration_ms=duration_ms,
                attempts=attempts,
            )
    finally:
        session.close()


def _error_text(exc: Exception) -> str:
    from .service import _error_text as service_error_text  # noqa: PLC0415

    return service_error_text(exc)


def _run_node_body(
    session: Session,
    *,
    node: Node,
    pipeline_ref: _PipelineRef,
    version_no: int,
    input_ids: list[str],
    engine_factory,
    principal: DataPlanePrincipal,
) -> tuple[str, int | None]:
    """单节点一次尝试：槽位已绑定 → 统一路刷新；未绑定 → 既有原语创建。"""
    from .service import (
        StepFailure,
        _bind_slot,
        _create_step_output,
        _slot_for,
    )

    slot = _slot_for(session, pipeline_ref.id, version_no, node.key, node.seq)
    if slot is not None:
        dataset = session.get(Dataset, slot.dataset_id)
        if dataset is None or dataset.customer_id != pipeline_ref.customer_id:
            raise StepFailure(
                f"步骤 {node.seq} 输出数据集已删除（槽位悬空: {slot.dataset_id}）"
                "——请新建版本重建输出"
            )
        _refresh(session, dataset, engine_factory, principal)
        return dataset.id, dataset.row_count

    sources: list[Dataset] = []
    for dataset_id in input_ids:
        source = session.get(Dataset, dataset_id)
        if source is None or source.customer_id != pipeline_ref.customer_id:
            raise StepFailure(f"步骤 {node.seq} 输入数据集不存在: {dataset_id}")
        sources.append(source)
    if not sources:
        raise StepFailure(f"步骤 {node.seq} 无输入数据集")

    child = _create_step_output(
        session,
        pipeline=pipeline_ref,
        version=version_no,
        seq=node.seq,
        step={"name": node.name, "op": node.op, "params": dict(node.params)},
        input_dataset=sources[0],
        extra_parents=sources[1:] or None,
        engine_factory=engine_factory,
        principal=principal,
    )
    _bind_slot(
        session, pipeline_ref.id, version_no, node.seq, child.id, node_key=node.key
    )
    return child.id, child.row_count


def _finish_node_row(
    session: Session,
    row: PipelineRunNode,
    status: str,
    error: str | None,
    started: float,
    attempts: int,
    *,
    output_id: str | None = None,
    row_count: int | None = None,
) -> None:
    row.status = NODE_SUCCEEDED if status == STEP_SUCCESS else NODE_FAILED
    row.attempts = attempts
    row.finished_at = _utcnow_naive()
    row.duration_ms = int((time.monotonic() - started) * 1000)
    if status == STEP_SUCCESS and row.error_text == INTERRUPTED_REASON:
        # 中断后重跑成功：中断事实保留（attempts 与备注同时可见），不伪造成
        # 一次干净的成功
        row.error_text = f"{INTERRUPTED_REASON}（已恢复重跑）"
    else:
        row.error_text = error
    row.output_dataset_id = output_id
    row.row_count = row_count
    session.commit()


def _write_step_row(
    session: Session,
    run_id: int,
    node: Node,
    status: str,
    output_id: str | None,
    row_count: int | None,
    error: str | None,
    started: float,
) -> None:
    session.add(
        PipelineRunStep(
            run_id=run_id,
            seq=node.seq,
            name=node.name,
            op=node.op,
            status=status,
            output_dataset_id=output_id,
            row_count=row_count,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_text=error,
        )
    )
    session.commit()
