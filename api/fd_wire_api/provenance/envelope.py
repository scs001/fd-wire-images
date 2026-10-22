"""事件信封与规范化哈希（纯函数、无 IO；wire-provenance-ledger D2，任务 2.2）。

信封结构（哈希覆盖 ``v``..``algo``，不含 ``event_hash`` 与 ``created_at``）::

    {"v": 1, "ledger": "<customer_id>", "seq": 1042, "id": "evt_...",
     "type": "dataset.correction.added", "ts_platform": "...",
     "actor": {"kind": "...", "id": "...", "sig": null},
     "subject": {"kind": "dataset", "key": "..."}, "payload": {...},
     "payload_hash": "sha256:...", "nonce": "<16B hex>",
     "prev_hash": "sha256:...", "algo": "sha256"}

哈希对象为规范化 JSON（UTF-8、键排序、无冗余空白）整体；``v``/``algo``
留格式与算法演化门，``actor.sig`` 留客户不可否认门（v2）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

#: 信封版本（格式/算法演化门；当前表世代的核验复算恒代入此值）
EVENT_VERSION = 1
#: 算法敏捷位取值（v1 恒 sha256）
HASH_ALGO = "sha256"
#: 创世前哈希：各账本首条事件的 prev_hash
GENESIS_PREV_HASH = "sha256:" + "0" * 64


def canonical_json(value: Any) -> bytes:
    """规范化 JSON：UTF-8、键排序（sort_keys）、分隔符无空白、非 ASCII 不转义。"""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """SHA-256 摘要的哈希字符串（带 ``sha256:`` 前缀）。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def payload_hash(payload: dict) -> str:
    """负载哈希：对负载规范化 JSON 做 sha256（前缀形式）。"""
    return sha256_hex(canonical_json(payload))


def utc_now_iso_z() -> str:
    """平台时间：UTC ISO-8601 秒级带 Z（入信封、入哈希）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(
    *,
    ledger: str,
    seq: int,
    type: str,
    actor: dict,
    subject: dict,
    payload: dict,
    event_id: str,
    ts_platform: str,
    nonce: str,
    prev_hash: str,
    algo: str = HASH_ALGO,
) -> dict:
    """构造待哈希信封（含 prev_hash；不含 event_hash/created_at——两者不属于哈希对象）。"""
    return {
        "v": EVENT_VERSION,
        "ledger": ledger,
        "seq": seq,
        "id": event_id,
        "type": type,
        "ts_platform": ts_platform,
        "actor": actor,
        "subject": subject,
        "payload": payload,
        "payload_hash": payload_hash(payload),
        "nonce": nonce,
        "prev_hash": prev_hash,
        "algo": algo,
    }


def event_hash(envelope: dict) -> str:
    """信封哈希：对含 prev_hash、不含 event_hash/created_at 的信封规范化哈希。"""
    return sha256_hex(canonical_json(envelope))


def envelope_from_row(row) -> dict:
    """从账本行重建待哈希信封（核验复算用；``row`` 只需具备同名属性）。

    ``v`` 非落库列，按当前表世代代入 ``EVENT_VERSION``。
    """
    return {
        "v": EVENT_VERSION,
        "ledger": row.ledger,
        "seq": row.seq,
        "id": row.id,
        "type": row.type,
        "ts_platform": row.ts_platform,
        "actor": row.actor,
        "subject": row.subject,
        "payload": row.payload,
        "payload_hash": row.payload_hash,
        "nonce": row.nonce,
        "prev_hash": row.prev_hash,
        "algo": row.algo,
    }


# ---- epoch Merkle（封存器/自检/证据包核验共用；口径唯一权威 =
# fd-wire/scripts/evidence_verify.py，两处必须逐字等价，改一处必改另一处）----


def merkle_leaf(payload_hash_str: str) -> str:
    """Merkle 叶：hex(sha256(hex(sha256(payload_hash 字符串 UTF-8 字节)).encode()))。

    注意内层先取 hex **字符串**再编码回字节（与核验脚本一致，非对摘要字节直接二次哈希）。
    """
    inner = hashlib.sha256(payload_hash_str.encode("utf-8")).hexdigest()
    return hashlib.sha256(inner.encode("utf-8")).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    """Merkle 根：叶子 hex 字符串两两拼接（UTF-8 字节）再哈希；奇数复制最后一叶（BTC 风格）。

    空区间根 = 64 个 "0"（与证据包核验脚本一致）。
    """
    if not leaves:
        return "0" * 64
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [
            hashlib.sha256((layer[i] + layer[i + 1]).encode("utf-8")).hexdigest()
            for i in range(0, len(layer), 2)
        ]
    return layer[0]
