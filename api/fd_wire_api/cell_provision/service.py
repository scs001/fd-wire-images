"""配给队列的 service 层（add-self-serve-cell-provision 任务 2.1/2.3）。

- ``enqueue(db, customer_id)``：幂等入队——同 customer_id 已有行且非
  failed → 原样返回（不重复建行，unique 红线在 service 先拦成可读语义）；
  failed 行 → 复位回 queued（升档重触发=重试，「已执行步骤幂等可重试」，
  spec 自动配给链）。
- ``claim_next(db)``：原子领队头——**单条 UPDATE**（id=队头子查询 且
  state='queued' 守卫），rowcount=1 才算领到。并发领取（runner 多实例/
  多线程同库）下写锁串行化，同一请求只可能被一个会话置 running；
  未抢到的会话在提交后重扫，直到领到下一 queued 或落空。无任务返回 None
  （路由层 204）。
- ``complete(db, request_id, endpoint_url, steps)``：running → done，
  同时幂等 upsert ``customer_cell_endpoints``（resolve 的 DB 兜底源）。
- ``fail(db, request_id, error, steps)``：running → failed，error + step
  痕留档（回退人工链的运营排查入口）。

状态闸：complete/fail 只接受 running（409 语义由路由层翻译）——queued/
done/failed 行不接受回写，防 runner 重复回写覆盖真相。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import CellProvisionRequest, CustomerCellEndpoint, utc_now_naive


class RequestNotFound(LookupError):
    """请求行不存在（路由层 404）。"""


class InvalidState(RuntimeError):
    """回写状态闸：请求不处于 running（路由层 409）。"""


@dataclass(frozen=True)
class EnqueueResult:
    """enqueue 结果：row=请求行；created=本次是否新建；requeued=failed 复位。"""

    row: CellProvisionRequest
    created: bool
    requeued: bool


def _steps_json(steps: list[dict] | None) -> str:
    """steps 列序列化（JSON 文本；None → 空数组）。"""
    return json.dumps(steps or [], ensure_ascii=False)


def enqueue(db: Session, customer_id: str) -> EnqueueResult:
    """幂等入队（见模块 docstring）：非 failed 原样返回 / failed 复位重入队。

    存在性判定走**列选**（id, state）而非实体选——同一会话 identity map 里
    的陈旧对象不会被普通 SELECT 刷新（autoflush=False 口径），列选绕开
    该坑；突变路径经 ``get(..., populate_existing=True)`` 取新态。
    """
    existing = db.execute(
        select(CellProvisionRequest.id, CellProvisionRequest.state).where(
            CellProvisionRequest.customer_id == customer_id
        )
    ).one_or_none()
    if existing is not None:
        existing_id, existing_state = existing
        if existing_state == "failed":
            row = db.get(CellProvisionRequest, existing_id, populate_existing=True)
            row.state = "queued"
            row.error = None
            db.commit()
            db.refresh(row)
            return EnqueueResult(row=row, created=False, requeued=True)
        row = db.get(CellProvisionRequest, existing_id, populate_existing=True)
        return EnqueueResult(row=row, created=False, requeued=False)
    row = CellProvisionRequest(
        customer_id=customer_id,
        state="queued",
        steps="[]",
        error=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return EnqueueResult(row=row, created=True, requeued=False)


#: 原子领取：单条 UPDATE（id=队头子查询 + state 守卫）RETURNING id。
#: RETURNING（PG 与 SQLite ≥3.35 双方言支持）让「抢到哪行」与「置
#: running」在同一条语句内裁决——竞态下只有一个连接能拿到行号，其余
#: 返回空（未抢到）。绝不先 SELECT 再 UPDATE（读-升级锁窗口在 sqlite
#: 回滚日志模式下会互等超时）。
_CLAIM_HEAD_SQL = text(
    "UPDATE cell_provision_requests SET state = 'running' "
    "WHERE id = (SELECT id FROM cell_provision_requests "
    "            WHERE state = 'queued' ORDER BY id LIMIT 1) "
    "  AND state = 'queued' "
    "RETURNING id"
)


def claim_next(db: Session) -> CellProvisionRequest | None:
    """原子领队头 queued → running（见模块 docstring 的并发语义）。"""
    claimed_id = db.execute(_CLAIM_HEAD_SQL).scalar_one_or_none()
    db.commit()
    if claimed_id is None:
        return None
    return db.get(CellProvisionRequest, claimed_id)


def get_request(db: Session, request_id: int) -> CellProvisionRequest:
    """按 id 取请求行；不存在 → RequestNotFound（路由层 404）。"""
    row = db.get(CellProvisionRequest, request_id)
    if row is None:
        raise RequestNotFound(f"cell provision request {request_id} 不存在")
    return row


def complete(
    db: Session,
    request_id: int,
    *,
    endpoint_url: str,
    steps: list[dict] | None = None,
) -> CellProvisionRequest:
    """runner 回写成功：request done + endpoints 表幂等 upsert（同事务）。"""
    row = get_request(db, request_id)
    if row.state != "running":
        raise InvalidState(
            f"request {request_id} 状态为 {row.state!r}（仅 running 可回写）"
        )
    row.state = "done"
    row.error = None
    if steps is not None:
        row.steps = _steps_json(steps)
    _upsert_endpoint(db, customer_id=row.customer_id, endpoint_url=endpoint_url)
    db.commit()
    db.refresh(row)
    return row


def fail(
    db: Session,
    request_id: int,
    *,
    error: str,
    steps: list[dict] | None = None,
) -> CellProvisionRequest:
    """runner 回写失败：request failed + error/step 痕留档（回退人工链）。"""
    row = get_request(db, request_id)
    if row.state != "running":
        raise InvalidState(
            f"request {request_id} 状态为 {row.state!r}（仅 running 可回写）"
        )
    row.state = "failed"
    row.error = error
    if steps is not None:
        row.steps = _steps_json(steps)
    db.commit()
    db.refresh(row)
    return row


def _upsert_endpoint(db: Session, *, customer_id: str, endpoint_url: str) -> None:
    """endpoints 表幂等 upsert（customer_id 唯一；done 落库）。"""
    row = db.execute(
        select(CustomerCellEndpoint).where(
            CustomerCellEndpoint.customer_id == customer_id
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            CustomerCellEndpoint(
                customer_id=customer_id,
                endpoint_url=endpoint_url,
                state="done",
                created_at=utc_now_naive(),
            )
        )
    else:
        row.endpoint_url = endpoint_url
        row.state = "done"


def list_requests(
    db: Session, *, limit: int = 200, offset: int = 0
) -> list[CellProvisionRequest]:
    """admin 状态页只读列表（新→旧分页；limit 收敛上限在路由层）。"""
    return list(
        db.execute(
            select(CellProvisionRequest)
            .order_by(CellProvisionRequest.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )


def lookup_registered_endpoint(customer_id: str) -> str | None:
    """resolve_cell_endpoint 的 DB 兜底读（独立短会话 + 全异常吞掉）。

    语义：state='done' 且 endpoint_url 非空才命中。**任何 DB 读失败
    （表未建/库不可达等）都静默返回 None**——调用方继续走约定派生，
    绝不因兜底源故障破坏既有免费路径（add-self-serve-cell-provision
    任务 2 的读路径红线）。ai.executor 惰性调用本函数（避免 import 期
    建引擎）；env map 显式口径的优先级由调用方保证（先 map 后本函数）。
    """
    from .db import new_session  # 局部 import：ai 侧零依赖启动路径

    try:
        db = new_session()
        try:
            row = db.execute(
                select(CustomerCellEndpoint.endpoint_url).where(
                    CustomerCellEndpoint.customer_id == customer_id,
                    CustomerCellEndpoint.state == "done",
                    CustomerCellEndpoint.endpoint_url.isnot(None),
                    CustomerCellEndpoint.endpoint_url != "",
                )
            ).scalar_one_or_none()
            return row or None
        finally:
            db.close()
    except Exception:  # noqa: BLE001 —— 兜底源任何故障都静默降级
        return None


__all__ = [
    "EnqueueResult",
    "InvalidState",
    "RequestNotFound",
    "claim_next",
    "complete",
    "enqueue",
    "fail",
    "get_request",
    "list_requests",
    "lookup_registered_endpoint",
]
