"""物化内部端点（wire-platform-v1 任务 5.6）：``/internal/materialization/*``。

- ``POST /internal/materialization/{artifact_id}/promote``：draft 转正——
  从 ``ai_draft_artifacts``（ai 切片引擎纪律）拷贝 spec 快照进
  ``materialized_artifacts`` + 登记 cron 调度（可空）。
- ``GET  /internal/materialization/{artifact_id}``：转正产物与最近一次
  运行摘要（last_status/last_rows/逐项成败——控制台如实展示用）。

鉴权：``X-Internal-Token`` 与 env ``MATERIALIZATION_INTERNAL_TOKEN`` 比对，
未配置 fail-closed（同 catalog 切片纪律）。
"""

from __future__ import annotations

import secrets
from typing import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.models import AIDraftArtifact
from . import service
from .db import get_db
from .models import MaterializedArtifact
from .schemas import MaterializedArtifactOut, PromoteIn, artifact_out
from .settings import get_settings

router = APIRouter(prefix="/internal/materialization", tags=["materialization-internal"])

DraftLoader = Callable[[int], AIDraftArtifact | None]


def _require_internal_token(
    x_internal_token: str | None = Header(default=None),
) -> None:
    """internal token 校验：未配置 fail-closed（与 catalog 切片同款）。"""
    expected = get_settings().materialization_internal_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal token not configured (MATERIALIZATION_INTERNAL_TOKEN)",
        )
    if x_internal_token is None or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid internal token")


def default_draft_loader(artifact_id: int) -> AIDraftArtifact | None:
    """默认草稿加载器：经 ai 切片自己的会话纪律读 ``ai_draft_artifacts``。

    物化会话是平台库跨域联查会话，ai 表归 ai 切片引擎——promote 跨切片
    只经本依赖读取（测试覆写注入内存版）。
    """
    from ..ai.db import get_session as get_ai_session

    session = get_ai_session()
    try:
        return session.get(AIDraftArtifact, artifact_id)
    finally:
        session.close()


def get_draft_loader() -> DraftLoader:
    """FastAPI 依赖：草稿加载器（测试覆写注入）。"""
    return default_draft_loader


def _registered_or_404(db: Session, artifact_id: int) -> MaterializedArtifact:
    artifact = db.execute(
        select(MaterializedArtifact).where(
            MaterializedArtifact.artifact_id == artifact_id
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"产物 {artifact_id} 尚未转正（未登记物化）",
        )
    return artifact


@router.post(
    "/{artifact_id}/promote",
    status_code=status.HTTP_201_CREATED,
    response_model=MaterializedArtifactOut,
    dependencies=[Depends(_require_internal_token)],
)
def promote(
    artifact_id: int,
    payload: PromoteIn,
    db: Session = Depends(get_db),
    load_draft: DraftLoader = Depends(get_draft_loader),
) -> MaterializedArtifactOut:
    """draft 转正：拷贝 spec 快照 + 登记调度（同一草稿只允许转正一次）。

    转正逻辑在 ``service.promote_draft``（wire-copilot-action-gate D5 抽取
    ——internal 端点与动作闸执行器共用同一条路径，零漂移）。
    """
    draft = load_draft(artifact_id)
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"产物草稿 {artifact_id} 不存在（ai_draft_artifacts）",
        )
    artifact = service.promote_draft(db, draft=draft, schedule=payload.schedule)
    return artifact_out(artifact)


@router.get(
    "/{artifact_id}",
    response_model=MaterializedArtifactOut,
    dependencies=[Depends(_require_internal_token)],
)
def get_registered(
    artifact_id: int,
    db: Session = Depends(get_db),
) -> MaterializedArtifactOut:
    """转正产物与最近一次运行摘要（last_rows = 最近一次成功行数合计）。"""
    return artifact_out(_registered_or_404(db, artifact_id))
