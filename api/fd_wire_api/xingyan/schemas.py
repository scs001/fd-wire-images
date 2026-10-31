"""星研供给面的响应模型。

每个响应顶层都带 ``snapshot``：消费者据此判断内容是否最新、能否把一次
检索结果与对账报告互相指认（spec「响应可归因」）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SnapshotMeta(BaseModel):
    source_sha: str = Field(default="", description="上游 manifest 指纹")
    schema_version: str = Field(default="", description="本次摄取的上游 feed 契约版本")
    status: str = Field(default="empty", description="ok | stale | schema_mismatch | upstream_error | empty")
    stale: bool = Field(default=True, description="内容是否落后于上游（故障时旧快照继续服务）")
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    last_error: str | None = None
    items: int = 0
    claims: int = 0


class ItemSummary(BaseModel):
    slug: str
    zone: str
    type: str | None = None
    order: int | None = None
    title: dict[str, str]
    summary: dict[str, str]
    published_at: str | None = None
    updated_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    claim_count: int = 0
    status: str = "active"
    url: dict[str, str] = Field(default_factory=dict)


class ItemListResponse(BaseModel):
    snapshot: SnapshotMeta
    items: list[ItemSummary]
    total: int
    page: int
    page_size: int


class ClaimSummary(BaseModel):
    claim_id: str
    global_id: str
    slug: str
    type: str
    section: dict[str, str]
    evidence: list[str] = Field(default_factory=list)
    status: str = "active"


class ItemDetail(ItemSummary):
    body: dict[str, str] = Field(default_factory=dict)
    sources: dict[str, Any] = Field(default_factory=dict)
    claims: list[ClaimSummary] = Field(default_factory=list)
    fetched_at: str | None = None


class EvidenceEntry(BaseModel):
    key: str
    resolved: bool = False
    label: str = ""
    url: str = ""


class ClaimResolveResponse(BaseModel):
    snapshot: SnapshotMeta
    global_id: str
    slug: str
    claim_id: str
    type: str
    section: dict[str, str]
    section_text: str = Field(default="", description="锚定小节的正文（按请求语言）")
    section_found: bool = Field(default=False, description="锚点标题是否在当前正文中找到")
    locale: str
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    item: dict[str, Any] = Field(default_factory=dict)


class ClaimWithdrawnResponse(BaseModel):
    snapshot: SnapshotMeta
    global_id: str
    slug: str
    claim_id: str
    type: str
    section: dict[str, str]
    evidence: list[str] = Field(default_factory=list)
    status: str = "withdrawn"
    detail: str = "该 claim 已被上游撤销；保留登记供溯源，不再提供服务"


class StatsResponse(BaseModel):
    snapshot: SnapshotMeta
    zones: dict[str, int] = Field(default_factory=dict)
    zones_withdrawn: dict[str, int] = Field(default_factory=dict)


class SyncResultResponse(BaseModel):
    status: str
    source_sha: str = ""
    schema_version: str = ""
    items: int = 0
    claims: int = 0
    withdrawn_items: int = 0
    withdrawn_claims: int = 0
    detail: str = ""
    zones: dict[str, int] = Field(default_factory=dict)
