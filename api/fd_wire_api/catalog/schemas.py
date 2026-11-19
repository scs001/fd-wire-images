"""目录 API 的响应模型（pydantic v2）。

注意：任何响应都不含观测数值——镜像表本身就没有观测列，schema 层再
收口一次（字段集即全部目录展示字段）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .models import IndicatorSnapshot


class IndicatorListItem(BaseModel):
    """目录列表项（精简展示）。"""

    indicator_id: str
    source_db: str | None = None
    code: str
    name_zh: str | None = None
    name_en: str | None = None
    domain: str | None = None
    frequency: str | None = None
    unit: str | None = None
    status: str


class DemandPrefill(BaseModel):
    """按需开通导流：数据需求单预填标识（指标 code）。"""

    indicator_code: str


class IndicatorDetail(IndicatorListItem):
    """目录详情：全部展示字段 + registered 导流字段。"""

    description_zh: str | None = None
    description_en: str | None = None
    time_span_start: str | None = None
    time_span_end: str | None = None
    entity_coverage_count: int | None = None
    sample_value: str | None = None
    synced_at: datetime
    # 导流：仅 registered（按需开通）为 true，并携带需求单预填标识
    on_demand: bool = False
    demand_prefill: DemandPrefill | None = None


class IndicatorListResponse(BaseModel):
    items: list[IndicatorListItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class SyncTriggerRequest(BaseModel):
    force_full: bool = False  # 兼容保留；同步已恒为单事务全量重刷


class SyncResultResponse(BaseModel):
    mode: str
    fetched: int
    created: int
    updated: int
    removed: int
    total_in_mirror: int
    watermark: datetime | None = None


class CatalogSourceStats(BaseModel):
    """按源分列计数（source_db 为空的历史行归入空串组）。"""

    source_db: str
    registered: int = Field(ge=0)
    verified: int = Field(ge=0)


class CatalogStatsResponse(BaseModel):
    """目录计数（活取口径）：registered＝登记总量（全量，含 verified）。"""

    registered: int = Field(ge=0)
    verified: int = Field(ge=0)
    by_source: list[CatalogSourceStats] = Field(default_factory=list)


def detail_from_row(row: IndicatorSnapshot) -> dict[str, Any]:
    """镜像行 → 详情 payload（含 registered 导流字段；无任何观测数值）。"""
    on_demand = row.status == "registered"
    return {
        "indicator_id": row.indicator_id,
        "source_db": row.source_db,
        "code": row.code,
        "name_zh": row.name_zh,
        "name_en": row.name_en,
        "description_zh": row.description_zh,
        "description_en": row.description_en,
        "domain": row.domain,
        "frequency": row.frequency,
        "unit": row.unit,
        "time_span_start": row.time_span_start,
        "time_span_end": row.time_span_end,
        "entity_coverage_count": row.entity_coverage_count,
        "sample_value": row.sample_value,
        "status": row.status,
        "synced_at": row.synced_at,
        "on_demand": on_demand,
        "demand_prefill": {"indicator_code": row.code} if on_demand else None,
    }


def list_item_from_row(row: IndicatorSnapshot) -> dict[str, Any]:
    return {
        "indicator_id": row.indicator_id,
        "source_db": row.source_db,
        "code": row.code,
        "name_zh": row.name_zh,
        "name_en": row.name_en,
        "domain": row.domain,
        "frequency": row.frequency,
        "unit": row.unit,
        "status": row.status,
    }
