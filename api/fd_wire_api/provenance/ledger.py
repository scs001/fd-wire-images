"""账本服务：追加（幂等）与链核验（wire-provenance-ledger 任务 2.2/2.3）。

事务边界归调用方：``append_event`` 只 ``flush`` 不 ``commit``——供
"数据变更与事件入账同事务"（D3：WAL 转发、uploads 落行同事务入账）
复用；并发追加同账本时 ``uq(ledger, seq)`` 兜底（冲突即 IntegrityError，
调用方重试即可，不产生重复入账）。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import envelope
from .models import LedgerEvent


def new_event_id() -> str:
    """事件 id：``evt_<uuid4hex>``（36 字符；跨系统转发按此幂等去重）。"""
    return f"evt_{uuid4().hex}"


def append_event(
    db: Session,
    *,
    ledger: str,
    type: str,
    actor: dict,
    subject: dict,
    payload: dict,
    event_id: str | None = None,
) -> LedgerEvent:
    """追加事件入账并返回账本行（只 flush 不 commit，事务边界归调用方）。

    - 幂等：同 ``event_id`` 已存在直接返回既有行，不重复入账
      （D3 转发"重复拉取零重复入账"依赖此语义）。
    - ``seq`` = 该账本当前 max(seq)+1；``prev_hash`` = 上一条
      （seq-1）的 event_hash，首条为创世哈希。
    - ``ts_platform`` 秒级 UTC；``nonce`` = 16 字节随机 hex；
      ``payload_hash``/``event_hash`` 由 ``envelope`` 规范化计算。
    """
    event_id = event_id or new_event_id()
    existing = db.get(LedgerEvent, event_id)
    if existing is not None:
        return existing

    last = (
        db.execute(
            select(LedgerEvent)
            .where(LedgerEvent.ledger == ledger)
            .order_by(LedgerEvent.seq.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    seq = last.seq + 1 if last is not None else 1
    prev_hash = last.event_hash if last is not None else envelope.GENESIS_PREV_HASH
    ts_platform = envelope.utc_now_iso_z()
    nonce = secrets.token_hex(16)
    env = envelope.build_envelope(
        ledger=ledger,
        seq=seq,
        type=type,
        actor=actor,
        subject=subject,
        payload=payload,
        event_id=event_id,
        ts_platform=ts_platform,
        nonce=nonce,
        prev_hash=prev_hash,
        algo=envelope.HASH_ALGO,
    )
    row = LedgerEvent(
        id=event_id,
        ledger=ledger,
        seq=seq,
        type=type,
        ts_platform=ts_platform,
        actor=actor,
        subject=subject,
        payload=payload,
        payload_hash=env["payload_hash"],
        nonce=nonce,
        prev_hash=prev_hash,
        algo=envelope.HASH_ALGO,
        event_hash=envelope.event_hash(env),
    )
    db.add(row)
    db.flush()  # 立即落 INSERT：uq(ledger, seq) 冲突在此暴露给调用方
    return row


@dataclass
class VerifyResult:
    """核验结论：通过/失败 + 已核验事件数 + 断裂 seq 与原因（只读、无修复）。"""

    ok: bool
    checked: int
    broken_at: int | None = None
    reason: str | None = None


def verify_interval(events: list[LedgerEvent]) -> VerifyResult:
    """核验给定事件构成的区间（证据包核验复用）：逐事件验哈希、相邻验链接。

    - 逐事件：以行字段重建信封复算 ``event_hash`` 比对；
    - 相邻：``prev_hash`` 衔接前条 ``event_hash``、``seq`` 连续（+1）；
    - 区间首条 seq==1 时额外校验创世 ``prev_hash``；否则其 prev_hash 指向
      区间外，本区间内无法核验（证据包自 seq 1 起全量导出则无此盲区）。

    传入乱序时按 seq 升序核验；``checked`` = 通过核验的事件数（不含断裂那条）。
    """
    ordered = sorted(events, key=lambda e: e.seq)
    checked = 0
    prev: LedgerEvent | None = None
    for event in ordered:
        if envelope.event_hash(envelope.envelope_from_row(event)) != event.event_hash:
            return VerifyResult(
                ok=False,
                checked=checked,
                broken_at=event.seq,
                reason=f"event_hash 复算不符（seq={event.seq}, id={event.id}）",
            )
        if prev is None:
            if event.seq == 1 and event.prev_hash != envelope.GENESIS_PREV_HASH:
                return VerifyResult(
                    ok=False,
                    checked=checked,
                    broken_at=event.seq,
                    reason="首条事件 prev_hash 不是创世哈希",
                )
        else:
            if event.seq != prev.seq + 1:
                return VerifyResult(
                    ok=False,
                    checked=checked,
                    broken_at=event.seq,
                    reason=f"seq 不连续（{prev.seq} → {event.seq}）",
                )
            if event.prev_hash != prev.event_hash:
                return VerifyResult(
                    ok=False,
                    checked=checked,
                    broken_at=event.seq,
                    reason=f"prev_hash 与前条 event_hash 不衔接（seq={event.seq}）",
                )
        checked += 1
        prev = event
    return VerifyResult(ok=True, checked=checked)


def verify_chain(db: Session, ledger: str, upto_seq: int | None = None) -> VerifyResult:
    """全链核验：自 seq 1 起连续核验该账本（可选截至 ``upto_seq``）。"""
    stmt = (
        select(LedgerEvent)
        .where(LedgerEvent.ledger == ledger)
        .order_by(LedgerEvent.seq)
    )
    if upto_seq is not None:
        stmt = stmt.where(LedgerEvent.seq <= upto_seq)
    events = list(db.execute(stmt).scalars())
    if not events:
        return VerifyResult(ok=True, checked=0)
    if events[0].seq != 1:
        return VerifyResult(
            ok=False,
            checked=0,
            broken_at=events[0].seq,
            reason=f"账本头部缺失（首条 seq={events[0].seq}，应为 1）",
        )
    return verify_interval(events)
