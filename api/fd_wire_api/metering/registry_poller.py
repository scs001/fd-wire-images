"""registry Mongo 直读轮询（可选摄取路径）。

从 registry 的 Mongo 审计库直接拉取 ``log_type == "mcp_server_access"`` 的
事件（MCPServerAccessRecord 原文），交给与 HTTP 推送完全相同的规范化层。
水位取库内已知最大事件时间（records ∪ anomalies），轮询带可配置重叠窗，
幂等键保证重叠不双记。pymongo 惰性导入：未配置/未安装时该路径不可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import to_utc
from .ingest import IngestResult, ingest_events
from .models import MeteringAnomaly, MeteringRecord
from .settings import get_settings

#: 重叠窗：水位回看的余量，容忍 registry 侧乱序写入
DEFAULT_OVERLAP = timedelta(minutes=5)


class AuditCollection(Protocol):
    """pymongo collection 的最小协议（测试注入假实现）。"""

    def find(
        self, filter: dict[str, Any], sort: list[tuple[str, int]], limit: int
    ) -> Any: ...  # pragma: no cover - 协议声明


def _watermark(db: Session) -> Any | None:
    """库内已知最大事件时间（计量记录与异常留痕取大者）。

    sqlite 的 ``func.max`` 可能按存储串返回，这里统一折算为 aware UTC
    datetime，供 Mongo ``$gt`` 查询使用。
    """
    from datetime import datetime

    def _coerce(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return to_utc(value)
        try:
            return to_utc(datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00")))
        except ValueError:
            return None

    record_max = _coerce(db.execute(select(func.max(MeteringRecord.event_timestamp))).scalar())
    anomaly_max = _coerce(db.execute(select(func.max(MeteringAnomaly.event_timestamp))).scalar())
    values = [v for v in (record_max, anomaly_max) if v is not None]
    return max(values) if values else None


def fetch_batch(collection: Any, since: Any | None, limit: int) -> list[dict[str, Any]]:
    """从 Mongo 拉一批 mcp_server_access 事件（时间升序）。"""
    query: dict[str, Any] = {"log_type": "mcp_server_access"}
    if since is not None:
        query["timestamp"] = {"$gt": since}
    cursor = collection.find(query, sort=[("timestamp", 1)], limit=limit)
    return list(cursor)


def connect_collection() -> Any:
    """按 env 建立 Mongo 连接并返回审计集合；未配置/未安装时抛错。"""
    settings = get_settings()
    if not settings.registry_mongo_uri or not settings.registry_mongo_db:
        raise RuntimeError("registry Mongo 直读未配置（需要 REGISTRY_MONGO_URI 与 REGISTRY_MONGO_DB）")
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise RuntimeError(
            "pymongo 未安装（可选依赖：uv pip install 'fd-wire-api[mongo]'）"
        ) from exc
    client: Any = MongoClient(settings.registry_mongo_uri)
    return client[settings.registry_mongo_db][settings.registry_mongo_collection]


@dataclass(slots=True)
class PollResult:
    """一次轮询的结果：拉取条数 + 摄取计数。"""

    fetched: int
    ingest: IngestResult

    def as_dict(self) -> dict[str, Any]:
        payload = self.ingest.as_dict()
        payload["fetched"] = self.fetched
        return payload


def poll_registry_once(
    db: Session,
    collection: Any | None = None,
    overlap: timedelta = DEFAULT_OVERLAP,
) -> PollResult:
    """执行一轮「拉取 → 规范化 → 幂等落库」；调用方负责 commit。"""
    settings = get_settings()
    own_connection = collection is None
    if own_connection:
        collection = connect_collection()

    watermark = _watermark(db)
    since = None
    if watermark is not None and overlap.total_seconds() > 0:
        since = to_utc(watermark) - overlap

    batch = fetch_batch(collection, since, settings.metering_poll_limit)
    ingest = ingest_events(db, batch)
    return PollResult(fetched=len(batch), ingest=ingest)
