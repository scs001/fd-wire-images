"""摄取服务：批量审计事件 → 幂等落库（HTTP 推送与 Mongo 轮询共用）。

幂等语义：``request_id + server_name`` 已存在（或同批次内重复）即跳过，
不产生第二条计量记录；异常留痕以 ``request_id + server_name + reason``
幂等。批量结束一次性提交。

竞态安全：预检（SELECT）与落库（INSERT）之间存在事务间隙，并发摄取
同键事件时唯一约束兜底——落库语句为 ``ON CONFLICT DO NOTHING``（PG 与
SQLite 双方言同 API），冲突被原子吸收并计为重复，绝不炸批回滚。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utc_now
from .models import MeteringAnomaly, MeteringRecord
from .normalize import (
    NormalizedAnomaly,
    NormalizedRecord,
    SkippedEvent,
    normalize_event,
)


@dataclass(slots=True)
class IngestResult:
    """一批事件的摄取结果计数。"""

    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    anomalies: int = 0
    anomaly_duplicates: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "anomalies": self.anomalies,
            "anomaly_duplicates": self.anomaly_duplicates,
            "skipped": self.skipped,
        }


def _insert_on_conflict_do_nothing(
    db: Session,
    model: type,
    values: dict[str, Any],
    index_elements: list[str],
) -> int:
    """按方言构造 INSERT ... ON CONFLICT DO NOTHING 并执行，返回 rowcount。

    rowcount 为 1 表示插入成功，0 表示唯一约束吸收了并发同键（PG 与
    SQLite 的 on_conflict_do_nothing 同 API；本服务只跑这两种方言）。
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(model).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=index_elements)
    return db.execute(stmt).rowcount


def ingest_events(db: Session, events: Iterable[dict[str, Any]]) -> IngestResult:
    """把一批原始审计事件规范化并幂等写入；调用方负责 commit。"""
    result = IngestResult()
    seen_records: set[tuple[str, str]] = set()
    seen_anomalies: set[tuple[str, str, str]] = set()
    now = utc_now()

    for raw in events:
        result.received += 1
        normalized = normalize_event(raw if isinstance(raw, dict) else {})

        if isinstance(normalized, SkippedEvent):
            result.skipped += 1
        elif isinstance(normalized, NormalizedRecord):
            _ingest_record(db, normalized, seen_records, result, now)
        elif isinstance(normalized, NormalizedAnomaly):
            _ingest_anomaly(db, normalized, seen_anomalies, result, now)

    return result


def _ingest_record(
    db: Session,
    record: NormalizedRecord,
    seen: set[tuple[str, str]],
    result: IngestResult,
    now: Any,
) -> None:
    key = (record.request_id, record.server_name)
    if key in seen:  # 同批次内重复
        result.duplicates += 1
        return
    exists = db.execute(
        select(MeteringRecord.id).where(
            MeteringRecord.request_id == record.request_id,
            MeteringRecord.server_name == record.server_name,
        )
    ).first()
    if exists is not None:  # 历史已摄取（重放）
        result.duplicates += 1
        seen.add(key)
        return
    inserted = _insert_on_conflict_do_nothing(
        db,
        MeteringRecord,
        {
            "event_timestamp": record.event_timestamp,
            "request_id": record.request_id,
            "caller": record.caller,
            "caller_principal": record.caller_principal,
            "credential_track": record.credential_track,
            "server_name": record.server_name,
            "server_path": record.server_path,
            "tool_name": record.tool_name,
            "rows_returned": record.rows_returned,
            "source": record.source,
            "created_at": now,
        },
        index_elements=["request_id", "server_name"],
    )
    seen.add(key)
    if inserted:
        result.accepted += 1
    else:  # 预检未见但并发事务已提交同键：约束吸收，计重复
        result.duplicates += 1


def _ingest_anomaly(
    db: Session,
    anomaly: NormalizedAnomaly,
    seen: set[tuple[str, str, str]],
    result: IngestResult,
    now: Any,
) -> None:
    key = (
        anomaly.request_id or "",
        anomaly.server_name or "",
        anomaly.reason,
    )
    if key in seen:
        result.anomaly_duplicates += 1
        return
    if anomaly.request_id and anomaly.server_name:
        exists = db.execute(
            select(MeteringAnomaly.id).where(
                MeteringAnomaly.request_id == anomaly.request_id,
                MeteringAnomaly.server_name == anomaly.server_name,
                MeteringAnomaly.reason == anomaly.reason,
            )
        ).first()
        if exists is not None:
            result.anomaly_duplicates += 1
            seen.add(key)
            return
    if anomaly.request_id and anomaly.server_name:
        inserted = _insert_on_conflict_do_nothing(
            db,
            MeteringAnomaly,
            {
                "reason": anomaly.reason,
                "request_id": anomaly.request_id,
                "server_name": anomaly.server_name,
                "tool_name": anomaly.tool_name,
                "caller": anomaly.caller,
                "credential_track": anomaly.credential_track,
                "detail": json.dumps(anomaly.detail, ensure_ascii=False, default=str),
                "event_timestamp": anomaly.event_timestamp,
                "created_at": now,
            },
            index_elements=["request_id", "server_name", "reason"],
        )
        seen.add(key)
        if inserted:
            result.anomalies += 1
        else:  # 竞态吸收：并发事务已留痕同键异常
            result.anomaly_duplicates += 1
        return
    db.add(  # 无仲裁键（NULL 互不相等）：本就不去重，维持直插
        MeteringAnomaly(
            reason=anomaly.reason,
            request_id=anomaly.request_id,
            server_name=anomaly.server_name,
            tool_name=anomaly.tool_name,
            caller=anomaly.caller,
            credential_track=anomaly.credential_track,
            detail=json.dumps(anomaly.detail, ensure_ascii=False, default=str),
            event_timestamp=anomaly.event_timestamp,
            created_at=now,
        )
    )
    seen.add(key)
    result.anomalies += 1
