"""provenance API 请求/响应模型（pydantic v2；wire-provenance-ledger）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import envelope

#: 溯源档位（D6：off 不入账 | chain_only 仅哈希链 | anchored_batch 周期锚定 |
#: anchored_realtime 事件级即时锚定；调档不影响已入账事件）
ProvenanceTier = Literal["off", "chain_only", "anchored_batch", "anchored_realtime"]


class IntegrityOut(BaseModel):
    """链完整性：结论 + 已核验事件数（如实呈现，无修复能力）。"""

    ok: bool
    checked: int


class AnchoredThroughOut(BaseModel):
    """数据集视图的锚定落点：最近回执的 epoch 与后端（账本级）。"""

    epoch: int
    backend: str


class AnchoringOut(BaseModel):
    """锚定进度：后端、最近已封存 epoch、滞后事件数（未覆盖事件如实计数）。"""

    backend: str
    last_sealed_epoch: int | None = None
    lag_events: int = 0


class OverviewOut(BaseModel):
    """企业级溯源概览（GET /api/v1/provenance/overview）。"""

    ledger: str
    last_seq: int
    integrity: IntegrityOut
    anchoring: AnchoringOut


class TimelineEventOut(BaseModel):
    """时间线事件（倒序分页；字段即信封核心字段）。"""

    seq: int
    type: str
    ts_platform: str
    actor: dict[str, Any]
    subject: dict[str, Any]
    payload: dict[str, Any]
    event_id: str

    @classmethod
    def from_row(cls, row) -> "TimelineEventOut":
        return cls(
            seq=row.seq,
            type=row.type,
            ts_platform=row.ts_platform,
            actor=row.actor,
            subject=row.subject,
            payload=row.payload,
            event_id=row.id,
        )


class DatasetStatusOut(BaseModel):
    """数据集溯源状态（GET /api/v1/provenance/datasets/{key}/status）。"""

    tier: ProvenanceTier
    integrity: IntegrityOut
    #: 已锚定至的 epoch 与后端（最近回执推导；无回执如实为 None）
    anchored_through: "AnchoredThroughOut | None" = None
    #: 溯源起点（首条事件平台时间；启用前无历史、不伪造）
    first_event_ts: str | None = None
    note: str | None = None


class TierIn(BaseModel):
    """调档请求：即时生效于后续事件，已入账事件不动（契约"溯源档位"）。"""

    tier: ProvenanceTier


class TierOut(BaseModel):
    """调档回执（回读自 datasets 行）。"""

    key: str
    tier: ProvenanceTier


class IngestIn(BaseModel):
    """内部入账请求：信封业务字段（seq/哈希链由平台组装；event_id 幂等键）。"""

    ledger: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=100)
    actor: dict[str, Any]
    subject: dict[str, Any]
    payload: dict[str, Any]
    event_id: str | None = Field(default=None, min_length=1, max_length=40)


class EventEnvelopeOut(BaseModel):
    """完整事件信封（ingest 回执；与账本行逐字段一致，可独立复算哈希）。"""

    v: int
    ledger: str
    seq: int
    id: str
    type: str
    ts_platform: str
    actor: dict[str, Any]
    subject: dict[str, Any]
    payload: dict[str, Any]
    payload_hash: str
    nonce: str
    prev_hash: str
    algo: str
    event_hash: str
    created_at: datetime

    @classmethod
    def from_row(cls, row) -> "EventEnvelopeOut":
        return cls(
            v=envelope.EVENT_VERSION,
            ledger=row.ledger,
            seq=row.seq,
            id=row.id,
            type=row.type,
            ts_platform=row.ts_platform,
            actor=row.actor,
            subject=row.subject,
            payload=row.payload,
            payload_hash=row.payload_hash,
            nonce=row.nonce,
            prev_hash=row.prev_hash,
            algo=row.algo,
            event_hash=row.event_hash,
            created_at=row.created_at,
        )
