"""cell 配给的内部领任务/回写端点（add-self-serve-cell-provision 任务 2.3）。

鉴权：``X-Internal-Token`` 闸——**复用 demands 的 internal 依赖**
（:func:`fd_wire_api.demands.routes._require_internal`，与
``FD_WIRE_INTERNAL_TOKEN`` 常量时间比对；cell-manager runner 侧同一
env 部署面，contact 模式复用既有 INTERNAL_TOKEN、无新 env）。

- ``POST /internal/cell-provision/next``：领队头 queued→running；
  无任务 204（runner 轮询的静默出口，绝不虚构任务）。
- ``POST /internal/cell-provision/{id}/result``：done（必带 endpoint_url，
  落 endpoints 表）/ failed（必带 error）。404=行不存在；409=状态闸
  （仅 running 可回写——重复回写/对 queued 行回写被拒，防覆盖真相）；
  422=done/failed 体缺失对应字段。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

# 闸从叶子模块取，不走 ``..demands.routes``：后者初始化链经 console→admin 又回到
# 本模块，构成 demands↔cell_provision 的导入环（2026-11-13 实测：干净检出上
# ``pytest tests/test_wanxing_client.py`` 单独收集即 ImportError）。语义未变。
from ..internal_auth import require_internal as _require_internal
from . import service
from .db import get_db
from .schemas import ClaimOut, ResultIn, ResultOut

router = APIRouter(
    prefix="/internal/cell-provision",
    tags=["cell-provision-internal"],
    dependencies=[Depends(_require_internal)],
)


@router.post("/next", response_model=ClaimOut, status_code=status.HTTP_200_OK)
def claim_next(db: Session = Depends(get_db)) -> ClaimOut | Response:
    """cheap1 runner 领任务：队头 queued→running；无任务 204。"""
    row = service.claim_next(db)
    if row is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return ClaimOut(id=row.id, customer_id=row.customer_id, state=row.state)


def _translate_service_errors(exc: Exception) -> HTTPException:
    """service 层异常 → HTTP 语义：NotFound→404 / InvalidState→409。"""
    if isinstance(exc, service.RequestNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post(
    "/{request_id}/result", response_model=ResultOut, status_code=status.HTTP_200_OK
)
def post_result(
    request_id: int, payload: ResultIn, db: Session = Depends(get_db)
) -> ResultOut:
    """runner 回写执行结果：done（endpoints 表落库）/ failed（error 留痕）。"""
    if payload.status == "done":
        if not (payload.endpoint_url or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="done 回写必须携带 endpoint_url",
            )
        try:
            row = service.complete(
                db,
                request_id,
                endpoint_url=payload.endpoint_url.strip(),
                steps=payload.steps,
            )
        except (service.RequestNotFound, service.InvalidState) as exc:
            raise _translate_service_errors(exc) from exc
    elif payload.status == "failed":
        if not (payload.error or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="failed 回写必须携带 error",
            )
        try:
            row = service.fail(
                db, request_id, error=payload.error.strip(), steps=payload.steps
            )
        except (service.RequestNotFound, service.InvalidState) as exc:
            raise _translate_service_errors(exc) from exc
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status 必须是 done 或 failed",
        )
    return ResultOut(id=row.id, customer_id=row.customer_id, state=row.state)
