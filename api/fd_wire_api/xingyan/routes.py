"""星研供给面的路由。

公开（不登录）只读面 —— 内容本身就是官网公开件的镜像，与
``/api/v1/catalog/*`` 同口径：公开分区内容不需要客户身份即可发现
（「匿名档不泄露」在此不构成越权：快照里物理上没有客户数据）。

- ``GET /api/v1/xingyan/items``：目录列举/检索（分区、关键词、分页）。
- ``GET /api/v1/xingyan/items/{slug}``：条目详情（含正文、来源、claim 集）。
- ``GET /api/v1/xingyan/claims/{slug}/{claim_id}``：claim 级全局 ID 解析
  （骨架 + 锚小节正文 + 逐条可解析证据；撤销 claim 回 200 显式撤销态）。
- ``GET /api/v1/xingyan/stats``：分区计数（含撤销数）。

内部（X-Internal-Token 保护）：
- ``POST /internal/xingyan/sync``：手动触发摄取（``force`` 跳指纹短路）。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from . import service
from .client import ZONES
from .db import XingyanWriteBlocked, readonly_session
from .schemas import (
    ClaimResolveResponse,
    ClaimWithdrawnResponse,
    ItemDetail,
    ItemListResponse,
    SnapshotMeta,
    StatsResponse,
    SyncResultResponse,
)
from .settings import get_settings
from .sync import XingyanFeedSync

public_router = APIRouter(prefix="/api/v1/xingyan", tags=["xingyan"])
internal_router = APIRouter(prefix="/internal/xingyan", tags=["xingyan-internal"])


def _snapshot(session: Session) -> SnapshotMeta:
    return SnapshotMeta.model_validate(service.snapshot_meta(session))


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    expected = get_settings().internal_token
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="internal token not configured (XINGYAN_INTERNAL_TOKEN)",
        )
    if x_internal_token is None or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="invalid internal token")


@public_router.get("/items", response_model=ItemListResponse)
def list_items(
    zone: str | None = Query(default=None, description="briefings | academy | research"),
    q: str | None = Query(default=None, max_length=200, description="双语标题/摘要/slug 模糊搜索"),
    status: str = Query(default="active", pattern="^(active|withdrawn)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(readonly_session),
) -> ItemListResponse:
    """星研目录列举（公开只读）：分区过滤 + 关键词 + 分页，携带快照指纹。"""
    if zone is not None and zone not in ZONES:
        raise HTTPException(status_code=422, detail=f"zone 必须是 {ZONES} 之一")
    result = service.list_items(
        session, zone=zone, q=q, status=status, page=page, page_size=page_size
    )
    return ItemListResponse(snapshot=_snapshot(session), **result)


@public_router.get("/stats", response_model=StatsResponse)
def stats(session: Session = Depends(readonly_session)) -> StatsResponse:
    """分区计数（含撤销数）：对外文案数字一律活取，不手写。"""
    from sqlalchemy import func, select

    from . import models

    zones = dict(
        session.execute(
            select(models.XingyanItem.zone, func.count())
            .where(models.XingyanItem.status == "active")
            .group_by(models.XingyanItem.zone)
        ).all()
    )
    withdrawn = dict(
        session.execute(
            select(models.XingyanItem.zone, func.count())
            .where(models.XingyanItem.status == "withdrawn")
            .group_by(models.XingyanItem.zone)
        ).all()
    )
    return StatsResponse(snapshot=_snapshot(session), zones=zones, zones_withdrawn=withdrawn)


@public_router.get("/items/{slug}", response_model=ItemDetail)
def get_item(slug: str, session: Session = Depends(readonly_session)) -> ItemDetail:
    """条目详情：正文、来源与 claim 集；不在快照中即 404。"""
    try:
        payload = service.get_item(session, slug)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ItemDetail(snapshot=_snapshot(session), **payload)


@public_router.get(
    "/claims/{slug}/{claim_id}",
    response_model=ClaimResolveResponse | ClaimWithdrawnResponse,
)
def resolve_claim(
    slug: str,
    claim_id: str,
    locale: str = Query(default="zh", pattern="^(zh|en)$"),
    session: Session = Depends(readonly_session),
):
    """claim 级全局 ID 解析。

    撤销态**不是**未找到：回 200 + ``status="withdrawn"`` + 原登记信息
    （spec「撤销态显式」）。真正不存在才 404。
    """
    try:
        payload = service.resolve_claim(session, slug, claim_id, locale)
    except service.ClaimWithdrawn as exc:
        c = exc.claim
        return ClaimWithdrawnResponse(
            snapshot=_snapshot(session),
            global_id=f"{c.slug}/{c.claim_id}",
            slug=c.slug,
            claim_id=c.claim_id,
            type=c.claim_type,
            section={"zh": c.section_zh, "en": c.section_en},
            evidence=list(c.evidence_json or []),
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ClaimResolveResponse(snapshot=_snapshot(session), **payload)


@internal_router.post(
    "/sync",
    response_model=SyncResultResponse,
    dependencies=[Depends(require_internal_token)],
)
def trigger_sync(force: bool = Query(default=False)) -> SyncResultResponse:
    """手动触发摄取。``force=true`` 跳过同指纹短路（运维补跑用）。"""
    try:
        result = XingyanFeedSync().run(force=force)
    except XingyanWriteBlocked as exc:  # pragma: no cover - 防御
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return SyncResultResponse(
        status=result.status,
        source_sha=result.source_sha,
        schema_version=result.schema_version,
        items=result.items,
        claims=result.claims,
        withdrawn_items=result.withdrawn_items,
        withdrawn_claims=result.withdrawn_claims,
        detail=result.detail,
        zones=result.zones,
    )
