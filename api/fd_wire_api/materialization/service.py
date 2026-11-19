"""物化 service 层（wire-copilot-action-gate D5 抽取）。

``promote_draft`` 从既有 internal 路由的行内逻辑抽取为唯一实现——
internal 端点与动作闸执行器（``ai/actions.py``）走同一条转正路径，避免
两条实现漂移。语义零漂移：kind 门 / cron 校验 / 唯一冲突 409 的错误码
与文案与原路由逐字一致（既有测试为证）。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..ai.models import AIDraftArtifact
from .cron import parse_cron
from .models import MaterializedArtifact
from .specs import MATERIALIZABLE_KINDS


def promote_draft(
    db: Session, *, draft: AIDraftArtifact, schedule: str | None = None
) -> MaterializedArtifact:
    """draft 转正：拷贝 spec 快照 + 登记调度（同一草稿只允许转正一次）。

    调用方负责草稿归属校验（闸的 preflight / internal 端点的 loader）。
    """
    if draft.kind not in MATERIALIZABLE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"产物类型 {draft.kind!r} 暂不支持物化读取"
                f"（支持: {sorted(MATERIALIZABLE_KINDS)}；alert_rule/refresh_schedule"
                " 不产生计费读取）"
            ),
        )
    if schedule is not None:
        try:
            parse_cron(schedule)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
    artifact = MaterializedArtifact(
        artifact_id=draft.id,
        customer_id=draft.customer_id,
        kind=draft.kind,
        spec=dict(draft.spec or {}),
        schedule=schedule,
        enabled=True,
    )
    db.add(artifact)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"产物草稿 {draft.id} 已转正过（artifact_id 唯一）",
        ) from exc
    db.commit()
    db.refresh(artifact)
    return artifact
