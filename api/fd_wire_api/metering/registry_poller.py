"""registry Mongo 直读轮询（可选摄取路径）。

从 registry 的 Mongo 审计库直接拉取 ``log_type == "mcp_server_access"`` 的
事件（MCPServerAccessRecord 原文），交给与 HTTP 推送完全相同的规范化层。
水位取库内已知最大事件时间（records ∪ anomalies），轮询带可配置重叠窗，
幂等键保证重叠不双记。pymongo 惰性导入：未配置/未安装时该路径不可用。

**翻页语义（2026-11-15 修）**：重叠窗是**回看**，不是游标。只按
``timestamp > 水位-重叠窗`` 升序取一批时，一旦「重叠窗内的事件数 ≥ 批上限」，
每一批都被已入库的旧事件填满、``accepted`` 恒为 0、水位永不前进 —— 计量静默
停摆而 ``driver.running`` 一直是 true（生产实测：窗口内恰好 500 条、批上限也是
500，于是从 11-08 起 7 天 64,532 条事件全部卡在门外）。所以一轮 poll 必须在
**同一个 tick 内翻页**，且游标要带 ``_id``（同一毫秒内的事件只按 timestamp
切不开）。
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

#: 单轮最多翻几页（页大小 = ``METERING_POLL_LIMIT``）。
#: 1 = 每轮只取一页（旧行为，遇满窗会永久停滞）；上限存在的意义是给单轮
#: 圈定工作量，别让一次 tick 无限拉取。
DEFAULT_MAX_PAGES = 20


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


def fetch_batch(
    collection: Any,
    since: Any | None,
    limit: int,
    after: tuple[Any, Any] | None = None,
) -> list[dict[str, Any]]:
    """从 Mongo 拉一批 mcp_server_access 事件（时间升序）。

    ``after=(timestamp, _id)`` 是**复合游标**，优先于 ``since``：它把「同一个
    时间戳里还没取过的那些」也切开，这是满窗停滞的根因所在。排序键必须与
    游标同序（``timestamp`` 再 ``_id``），否则翻页会漏。
    """
    query: dict[str, Any] = {"log_type": "mcp_server_access"}
    if after is not None:
        ts, oid = after
        query["$or"] = [
            {"timestamp": {"$gt": ts}},
            {"timestamp": ts, "_id": {"$gt": oid}},
        ]
    elif since is not None:
        query["timestamp"] = {"$gt": since}
    cursor = collection.find(query, sort=[("timestamp", 1), ("_id", 1)], limit=limit)
    return list(cursor)


def _cursor_of(doc: dict[str, Any]) -> tuple[Any, Any] | None:
    """取翻页游标；缺 ``_id``（测试假件或异常文档）时返回 None，调用方据此停止。"""
    if not isinstance(doc, dict):
        return None
    ts = doc.get("timestamp")
    oid = doc.get("_id")
    if ts is None or oid is None:
        return None
    return (ts, oid)


def _merge(total: IngestResult, part: IngestResult) -> None:
    """把一页的计数并入整轮计数（IngestResult 是槽位 dataclass，没有 __iadd__）。"""
    total.received += part.received
    total.accepted += part.accepted
    total.duplicates += part.duplicates
    total.anomalies += part.anomalies
    total.anomaly_duplicates += part.anomaly_duplicates
    total.skipped += part.skipped


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
    max_pages: int | None = None,
) -> PollResult:
    """执行一轮「拉取 → 规范化 → 幂等落库」；调用方负责 commit。

    一轮 = 从 ``水位-重叠窗`` 起**翻页**取到追平（或到页数上限）。翻页是必需的：
    只取一页时，若重叠窗里的旧事件 ≥ 批上限，全部被判重复、水位不动，此后每轮
    都重读同一页 —— 那就是「驱动在跑但计量停摆」的形状。
    """
    settings = get_settings()
    own_connection = collection is None
    if own_connection:
        collection = connect_collection()

    limit = max(1, int(settings.metering_poll_limit))
    pages_cap = max(1, int(max_pages if max_pages is not None else settings.metering_poll_max_pages))

    watermark = _watermark(db)
    since: Any | None = None
    if watermark is not None and overlap.total_seconds() > 0:
        since = to_utc(watermark) - overlap

    fetched = 0
    ingest = IngestResult()
    after: tuple[Any, Any] | None = None
    for _ in range(pages_cap):
        batch = fetch_batch(collection, since, limit, after=after)
        if not batch:
            break
        fetched += len(batch)
        _merge(ingest, ingest_events(db, batch))
        if len(batch) < limit:
            break  # 取不满一页 = 已追平
        nxt = _cursor_of(batch[-1])
        if nxt is None or nxt == after:
            break  # 没有可用游标（或缺 _id）：停在这里，绝不空转
        after = nxt
        since = None  # 之后的页只认复合游标，别让 since 与 $or 同时出现
    return PollResult(fetched=fetched, ingest=ingest)
