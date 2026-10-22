"""目录路由：公开查询 API + internal 手动同步触发端点。

- 公开（不登录）：``GET /api/v1/catalog/indicators``（分页+关键词+domain+
  status 过滤）、``GET /api/v1/catalog/indicators/{id}``（详情 + registered
  导流字段）、``GET /api/v1/catalog/stats``（目录计数：登记总量/verified
  总量/按源分列，镜像 GROUP BY 现算——对外文案的数字一律活取于此）。
  不返回任何观测数值。
- 内部（X-Internal-Token 保护）：``POST /internal/catalog/sync`` 手动触发
  同步（单事务全量重刷；force_full 参数兼容保留）。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import OperationalError

from .client import HttpRegistryClient, RegistryClient
from .db import MirrorWriteBlocked
from .schemas import (
    CatalogStatsResponse,
    IndicatorDetail,
    IndicatorListResponse,
    SyncResultResponse,
    SyncTriggerRequest,
    detail_from_row,
    list_item_from_row,
)
from .service import CatalogQueryService
from .settings import settings
from .sync import IndicatorRegistrySync

public_router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])
internal_router = APIRouter(prefix="/internal/catalog", tags=["catalog-internal"])


def get_query_service() -> CatalogQueryService:
    return CatalogQueryService()


def get_registry_client() -> RegistryClient:
    """权威源客户端（测试用 dependency_overrides 替换为假实现）。"""
    return HttpRegistryClient()


def get_sync_service(client: RegistryClient = Depends(get_registry_client)) -> IndicatorRegistrySync:
    return IndicatorRegistrySync(client=client)


def _require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    expected = settings.catalog_internal_token
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="internal token not configured (CATALOG_INTERNAL_TOKEN)",
        )
    if x_internal_token is None or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="invalid internal token")


@public_router.get("/indicators", response_model=IndicatorListResponse)
def list_indicators(
    q: str | None = Query(default=None, max_length=200, description="名称/描述/代码模糊搜索"),
    domain: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, pattern="^(verified|registered)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: CatalogQueryService = Depends(get_query_service),
) -> IndicatorListResponse:
    """全量指标目录搜索（不登录；含 registered）。不返回观测数值。"""
    result = service.search(q=q, domain=domain, status=status, page=page, page_size=page_size)
    return IndicatorListResponse(
        items=[list_item_from_row(row) for row in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@public_router.get("/stats", response_model=CatalogStatsResponse)
def catalog_stats(
    service: CatalogQueryService = Depends(get_query_service),
) -> CatalogStatsResponse:
    """目录计数（公开匿名）：镜像 GROUP BY source_db 现算，如实反映镜像。

    ``registered``＝登记总量（全量，含 verified）；首页等对外文案的指标
    数字一律从此活取，MUST NOT 手写。
    """
    return CatalogStatsResponse.model_validate(service.stats())


@public_router.get("/indicators/{indicator_ref}", response_model=IndicatorDetail)
def get_indicator(
    indicator_ref: str,
    service: CatalogQueryService = Depends(get_query_service),
) -> IndicatorDetail:
    """指标详情：全部展示字段；registered 附按需开通导流（需求单预填 code）。"""
    row = service.get(indicator_ref)
    if row is None:
        raise HTTPException(status_code=404, detail="indicator not found")
    return IndicatorDetail.model_validate(detail_from_row(row))


@internal_router.post(
    "/sync",
    response_model=SyncResultResponse,
    dependencies=[Depends(_require_internal_token)],
)
def trigger_sync(
    payload: SyncTriggerRequest | None = None,
    syncer: IndicatorRegistrySync = Depends(get_sync_service),
) -> SyncResultResponse:
    """手动触发镜像同步（默认增量；force_full 全量对账）。仅同步管道写镜像。"""
    try:
        result = syncer.run(force_full=bool(payload.force_full) if payload else False)
    except MirrorWriteBlocked as exc:  # pragma: no cover - 防御：sync 不应触发 guard
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OperationalError as exc:  # 镜像库不可达等
        raise HTTPException(status_code=503, detail=f"mirror db error: {exc}") from exc
    except Exception as exc:  # 权威源拉取失败
        raise HTTPException(status_code=502, detail=f"registry sync failed: {exc}") from exc
    return SyncResultResponse(
        mode=result.mode,
        fetched=result.fetched,
        created=result.created,
        updated=result.updated,
        removed=result.removed,
        total_in_mirror=result.total_in_mirror,
        watermark=result.watermark,
    )
