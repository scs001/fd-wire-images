"""平台账本核心单测（wire-provenance-ledger 任务 2.1/2.2/2.3）。

覆盖：链组装（seq 单调 / prev_hash 链接 / event_hash 可复算）、event_id
幂等、全链与区间核验（含伪造 prev_hash 的断裂定位）、账本不可变触发器
（UPDATE/DELETE 被 RAISE 拒绝）、规范化哈希稳定性。

环境：内存 SQLite（StaticPool）+ create_all/触发器开发兜底（init_provenance_db）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.provenance import (
    envelope,
    init_provenance_db,
    ledger,
    reset_provenance_runtime,
)
from fd_wire_api.provenance.models import LedgerEvent


@pytest.fixture()
def db(monkeypatch):
    """隔离运行时（内存库）+ 兜底建表与触发器，返回直连 Session。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    reset_datasets_runtime()
    reset_provenance_runtime()
    init_provenance_db()
    session = get_session()
    yield session
    session.rollback()
    session.close()
    reset_datasets_runtime()
    reset_provenance_runtime()


def _append(db, *, ledger_name: str = "cust-001", event_id: str | None = None, **overrides):
    kwargs = dict(
        ledger=ledger_name,
        type="dataset.correction.added",
        actor={"kind": "customer", "id": ledger_name, "sig": None},
        subject={"kind": "dataset", "key": "sales_2026"},
        payload={"op": "update", "anchor": "pk:1", "correction_id": 7},
        event_id=event_id,
    )
    kwargs.update(overrides)
    return ledger.append_event(db, **kwargs)


# ------------------------------------------------------------- 信封与规范化


def test_canonical_json_stability() -> None:
    """规范化 JSON：键排序 + 无空白 + 非_ascii 不转义（与键序无关稳定）。"""
    a = envelope.canonical_json({"b": 1, "a": "中文"})
    b = envelope.canonical_json({"a": "中文", "b": 1})
    assert a == b
    assert a == b'{"a":"\xe4\xb8\xad\xe6\x96\x87","b":1}'
    assert envelope.sha256_hex(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()
    assert envelope.sha256_hex(b"x").startswith("sha256:")
    # 负数/嵌套结构同样确定性
    assert envelope.canonical_json({"z": {"y": 2, "x": -1}}) == b'{"z":{"x":-1,"y":2}}'


def test_genesis_prev_hash_shape() -> None:
    assert envelope.GENESIS_PREV_HASH == "sha256:" + "0" * 64


# ------------------------------------------------------------- append 组链


def test_append_builds_chain(db) -> None:
    """三条事件：seq 1/2/3 递增、prev_hash 链接、event_hash 可复算。"""
    rows = [_append(db) for _ in range(3)]
    db.commit()

    assert [r.seq for r in rows] == [1, 2, 3]
    assert rows[0].prev_hash == envelope.GENESIS_PREV_HASH
    assert rows[1].prev_hash == rows[0].event_hash
    assert rows[2].prev_hash == rows[1].event_hash
    for row in rows:
        assert row.algo == "sha256"
        assert row.event_hash.startswith("sha256:")
        assert len(row.nonce) == 32 and int(row.nonce, 16) >= 0
        assert row.ts_platform.endswith("Z") and len(row.ts_platform) == 20
        assert row.payload_hash == envelope.payload_hash(row.payload)
        # 行字段重建信封 → 复算哈希一致（核验不变式）
        assert envelope.event_hash(envelope.envelope_from_row(row)) == row.event_hash


def test_append_ledgers_independent(db) -> None:
    """账本按企业划分：另一企业首条事件 seq=1、创世 prev_hash。"""
    _append(db)  # cust-001 seq 1
    other = _append(db, ledger_name="cust-002")
    db.commit()
    assert other.seq == 1
    assert other.prev_hash == envelope.GENESIS_PREV_HASH


def test_append_idempotent_same_event_id(db) -> None:
    """同 event_id 重复 append：返回既有行、总数不变、序号不占用。"""
    first = _append(db, event_id="evt_dedup")
    again = _append(
        db, event_id="evt_dedup", payload={"op": "delete", "anchor": "pk:9"}
    )
    assert again.id == first.id and again.seq == first.seq == 1
    following = _append(db)
    db.commit()
    assert following.seq == 2  # 幂等返回未占号
    count = len(db.execute(select(LedgerEvent)).scalars().all())
    assert count == 2


# ------------------------------------------------------------- 核验


def test_verify_chain_ok_and_upto_seq(db) -> None:
    rows = [_append(db) for _ in range(3)]
    db.commit()

    result = ledger.verify_chain(db, "cust-001")
    assert result.ok and result.checked == 3 and result.broken_at is None

    partial = ledger.verify_chain(db, "cust-001", upto_seq=2)
    assert partial.ok and partial.checked == 2

    empty = ledger.verify_chain(db, "no-such-ledger")
    assert empty.ok and empty.checked == 0


def test_verify_chain_detects_forged_prev_hash(db) -> None:
    """手工 INSERT 自洽哈希但伪造 prev_hash 的行 → 断链定位 broken_at。"""
    first, second = _append(db), _append(db)
    db.commit()

    # 伪造 seq=3：event_hash 自洽（按信封复算），仅 prev_hash 指向假前驱
    forged_prev = "sha256:" + "f" * 64
    forged_actor = {"kind": "engine", "id": "cell-1"}
    forged_subject = {"kind": "dataset", "key": "sales_2026"}
    forged_payload = {"op": "insert", "anchor": "ins:1"}
    env = envelope.build_envelope(
        ledger="cust-001",
        seq=3,
        type="dataset.correction.added",
        actor=forged_actor,
        subject=forged_subject,
        payload=forged_payload,
        event_id="evt_forged",
        ts_platform="2026-10-07T00:00:00Z",
        nonce="00" * 16,
        prev_hash=forged_prev,
    )
    db.execute(
        text(
            "INSERT INTO ledger_events (id, ledger, seq, type, ts_platform, actor,"
            " subject, payload, payload_hash, nonce, prev_hash, algo, event_hash,"
            " created_at) VALUES (:id, :ledger, :seq, :type, :ts, :actor, :subject,"
            " :payload, :ph, :nonce, :prev, :algo, :eh, :created)"
        ),
        {
            "id": "evt_forged",
            "ledger": "cust-001",
            "seq": 3,
            "type": env["type"],
            "ts": env["ts_platform"],
            "actor": json.dumps(forged_actor),
            "subject": json.dumps(forged_subject),
            "payload": json.dumps(forged_payload),
            "ph": env["payload_hash"],
            "nonce": env["nonce"],
            "prev": forged_prev,
            "algo": env["algo"],
            "eh": envelope.event_hash(env),
            "created": "2026-10-07 00:00:00",
        },
    )
    db.commit()

    result = ledger.verify_chain(db, "cust-001")
    assert result.ok is False
    assert result.broken_at == 3
    assert result.reason is not None and "prev_hash" in result.reason
    assert result.checked == 2  # 断裂前的两条已核验通过

    # 截至断裂前仍完整
    assert ledger.verify_chain(db, "cust-001", upto_seq=2).ok

    # 另一企业账本不受影响
    assert ledger.verify_chain(db, "cust-002").ok


def test_verify_interval_slice(db) -> None:
    """区间核验：中段切片通过（首条非 seq=1 不校验创世）、空区间通过。"""
    rows = [_append(db) for _ in range(3)]
    db.commit()

    sliced = ledger.verify_interval(rows[1:])
    assert sliced.ok and sliced.checked == 2

    assert ledger.verify_interval([]).ok and ledger.verify_interval([]).checked == 0
    # 乱序传入按 seq 升序核验
    shuffled = ledger.verify_interval(list(reversed(rows)))
    assert shuffled.ok and shuffled.checked == 3


def test_verify_interval_detects_hash_tamper(db) -> None:
    """伪造行进入区间（event_hash 与信封不符）→ 复算不符定位断裂。"""
    first, second = _append(db), _append(db)
    db.commit()

    class ForgedRow:
        """与 second 同 seq 但 payload 被替换的伪行（模拟证据包篡改）。"""

        id = second.id
        ledger = second.ledger
        seq = second.seq
        type = second.type
        ts_platform = second.ts_platform
        actor = second.actor
        subject = second.subject
        payload = {"op": "delete", "anchor": "pk:999"}
        payload_hash = second.payload_hash
        nonce = second.nonce
        prev_hash = second.prev_hash
        algo = second.algo
        event_hash = second.event_hash

    result = ledger.verify_interval([first, ForgedRow()])
    assert result.ok is False
    assert result.broken_at == second.seq
    assert result.reason is not None and "event_hash" in result.reason


# ------------------------------------------------------------- 不可变触发器


def test_update_blocked_by_trigger(db) -> None:
    """UPDATE 已入账事件 → 触发器 RAISE（sqlite3.IntegrityError），行不变。"""
    row = _append(db)
    db.commit()

    with pytest.raises(IntegrityError) as excinfo:
        db.execute(
            text("UPDATE ledger_events SET type = 'x' WHERE id = :id"),
            {"id": row.id},
        )
    db.rollback()
    assert isinstance(excinfo.value.orig, sqlite3.IntegrityError)
    assert "append-only" in str(excinfo.value.orig)

    kept = db.execute(
        select(LedgerEvent).where(LedgerEvent.id == row.id)
    ).scalar_one()
    assert kept.type == row.type


def test_delete_blocked_by_trigger(db) -> None:
    """DELETE 已入账事件 → 触发器 RAISE（sqlite3.IntegrityError），行仍在。"""
    row = _append(db)
    db.commit()

    with pytest.raises(IntegrityError) as excinfo:
        db.execute(text("DELETE FROM ledger_events WHERE id = :id"), {"id": row.id})
    db.rollback()
    assert isinstance(excinfo.value.orig, sqlite3.IntegrityError)
    assert "append-only" in str(excinfo.value.orig)

    count = len(
        db.execute(select(LedgerEvent).where(LedgerEvent.id == row.id)).scalars().all()
    )
    assert count == 1
