#!/usr/bin/env python3
"""证据包离线核验脚本（wire-provenance-ledger 任务 7.2）。

自包含：仅 Python 标准库（checkpoint 的 ed25519 验签在缺 ``cryptography``
库时如实跳过并提示）。客户离开平台后凭本脚本 + 证据包 + 外部凭证（公链
数据或自存检查点）即可独立核验。

证据包布局（导出 API 产出的 zip 解开后）::

    ledger.jsonl                       # 全量信封事件（每行一个 JSON 对象，
                                       #   含回执回写事件 ledger.epoch.sealed）
    checkpoints/epoch-<N>.json         # 客户自持检查点（anchor-customer 模式，
                                       #   含平台签名）
    anchors/                           # 外部锚定凭据（OTS 文件等，本脚本
                                       #   不联网核验，仅列出供人工/工具复核）

核验三件事：
1. 链连续：逐事件重算 event_hash、prev_hash 链接、seq 连续（按 ledger 分组）；
2. 区间构成：每个 ledger.epoch.sealed 回执事件重算区间 Merkle 根并比对；
3. 检查点签名：存在 checkpoints/ 时验 ed25519 签名（需要公钥文件或
   ``--pubkey`` 参数；缺库/缺钥如实跳过）。

哈希口径（与平台封存器一致，勿改其一）：
- canonical JSON：UTF-8、键排序、无空白分隔（``separators=(",", ":")``）
- payload_hash = "sha256:" + hex(sha256(canonical_json(payload)))
- event_hash   = "sha256:" + hex(sha256(canonical_json(信封去 event_hash/created_at)))
- Merkle 叶    = sha256(sha256(payload_hash 字符串的 UTF-8 字节)) 的 hex（无前缀）
- Merkle 结点  = sha256(左叶hex字符串 + 右叶hex字符串 的 UTF-8 字节)；
  奇数叶复制最后一叶（BTC 风格）；区间为空 → 根 = 64 个 "0"

用法::

    python3 evidence_verify.py <bundle_dir> [--pubkey <ed25519_pub.pem>]
    python3 evidence_verify.py --selftest      # 自检（合成包：通过+两态篡改）

退出码：0 通过；1 发现篡改/不完整；2 用法/结构错误。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_USAGE = "usage: evidence_verify.py <bundle_dir> [--pubkey <pem>] | --selftest"

# 信封里参与 event_hash 的字段（顺序无关——canonical 会排序）
_ENVELOPE_HASH_FIELDS = (
    "v",
    "ledger",
    "seq",
    "id",
    "type",
    "ts_platform",
    "actor",
    "subject",
    "payload",
    "payload_hash",
    "nonce",
    "prev_hash",
    "algo",
)
RECEIPT_TYPE = "ledger.epoch.sealed"


# ---------------- canonical / hashing ----------------

def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_hash_of(payload: dict) -> str:
    return "sha256:" + sha256_hex(canonical_json(payload))


def event_hash_of(envelope: dict) -> str:
    basis = {k: envelope[k] for k in _ENVELOPE_HASH_FIELDS if k in envelope}
    return "sha256:" + sha256_hex(canonical_json(basis))


def merkle_leaf(payload_hash: str) -> str:
    raw = payload_hash.encode("utf-8")
    return sha256_hex(sha256_hex(raw).encode("utf-8"))


def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return "0" * 64
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [
            sha256_hex((layer[i] + layer[i + 1]).encode("utf-8"))
            for i in range(0, len(layer), 2)
        ]
    return layer[0]


# ---------------- 核验 ----------------

class Findings:
    def __init__(self) -> None:
        self.ok = True
        self.lines: list[str] = []

    def fail(self, msg: str) -> None:
        self.ok = False
        self.lines.append(f"FAIL {msg}")

    def info(self, msg: str) -> None:
        self.lines.append(f"ok   {msg}")


def verify_bundle(bundle: Path, pubkey_pem: Path | None) -> Findings:
    f = Findings()
    ledger_file = bundle / "ledger.jsonl"
    if not ledger_file.exists():
        f.fail("缺 ledger.jsonl（证据包不完整）")
        return f
    events = [
        json.loads(ln)
        for ln in ledger_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if not events:
        f.fail("ledger.jsonl 为空")
        return f

    # 1. 链连续（按 ledger 分组）
    by_ledger: dict[str, list[dict]] = {}
    for ev in events:
        by_ledger.setdefault(ev.get("ledger", "?"), []).append(ev)
    receipts: list[dict] = []
    for ledger, evs in sorted(by_ledger.items()):
        evs.sort(key=lambda e: e.get("seq", 0))
        prev = "sha256:" + "0" * 64
        for ev in evs:
            seq = ev.get("seq")
            if event_hash_of(ev) != ev.get("event_hash"):
                f.fail(f"[{ledger}] seq={seq} event_hash 不匹配（内容被改动）")
                return f
            if ev.get("prev_hash") != prev:
                f.fail(
                    f"[{ledger}] seq={seq} prev_hash 断裂"
                    f"（期望 {prev[:16]}…，实际 {str(ev.get('prev_hash'))[:16]}…）"
                )
                return f
            if ev.get("payload_hash") != payload_hash_of(ev.get("payload", {})):
                f.fail(f"[{ledger}] seq={seq} payload_hash 不匹配")
                return f
            prev = ev["event_hash"]
            if ev.get("type") == RECEIPT_TYPE:
                receipts.append(ev)
        f.info(f"[{ledger}] 链连续 {len(evs)} 条（seq 1..{evs[-1].get('seq')}）")

    # 2. 区间构成：回执的 merkle_root 必须能由区间事件重算
    for rc in receipts:
        p = rc.get("payload", {})
        ledger = rc.get("ledger")
        first, last, claimed = p.get("first_seq"), p.get("last_seq"), p.get("merkle_root")
        if not all(isinstance(x, int) for x in (first, last)) or not claimed:
            f.fail(f"[{ledger}] 回执 {rc.get('id')} 区间字段缺失/非法")
            continue
        leaves = [
            merkle_leaf(ev["payload_hash"])
            for ev in by_ledger.get(ledger, [])
            if first <= ev.get("seq", -1) <= last and ev.get("type") != RECEIPT_TYPE
        ]
        actual = merkle_root(leaves)
        if actual != claimed:
            f.fail(
                f"[{ledger}] epoch={p.get('epoch')} merkle_root 不匹配"
                f"（回执 {claimed[:16]}…，重算 {actual[:16]}…）"
            )
        else:
            f.info(
                f"[{ledger}] epoch={p.get('epoch')} 区间 [{first},{last}]"
                f" {len(leaves)} 叶 Merkle 根一致（backend={p.get('backend')}）"
            )

    # 3. 检查点签名（可选、如实跳过）
    cps = sorted((bundle / "checkpoints").glob("epoch-*.json")) if (bundle / "checkpoints").exists() else []
    if not cps:
        f.info("无 checkpoints/（跳过签名验证）")
    elif pubkey_pem is None:
        f.info(f"发现 {len(cps)} 个检查点但未提供 --pubkey（跳过签名验证）")
    else:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:
            f.info("缺 cryptography 库（跳过签名验证；pip install cryptography 后可复验）")
        else:
            pub = serialization.load_pem_public_key(pubkey_pem.read_bytes())
            assert isinstance(pub, Ed25519PublicKey)
            for cp_file in cps:
                cp = json.loads(cp_file.read_text(encoding="utf-8"))
                sig = bytes.fromhex(cp.pop("signature", ""))
                if pub.verify(sig, canonical_json(cp)) is None:
                    f.info(f"检查点 epoch={cp.get('epoch')} 签名有效")
                else:  # pragma: no cover —— verify 失败走异常
                    f.fail(f"检查点 epoch={cp.get('epoch')} 签名无效")

    anchors = bundle / "anchors"
    if anchors.exists():
        f.info(f"anchors/ 含 {len(list(anchors.iterdir()))} 个外部凭据（不联网核验，供复核）")
    return f


# ---------------- selftest ----------------

def _selftest() -> int:
    import tempfile

    def build_bundle(tamper: str | None) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="ev-"))
        ledger: list[dict] = []
        prev = "sha256:" + "0" * 64
        # epoch 1：三条业务事件 + 一条回执
        for i in range(1, 4):
            payload = {"op": "update", "anchor": f"hash:{i:064d}", "n": i}
            ev = {
                "v": 1,
                "ledger": "cust-st",
                "seq": i,
                "id": f"evt_{i:032d}",
                "type": "dataset.correction.added",
                "ts_platform": "2026-10-07T00:00:0%dZ" % (i % 10),
                "actor": {"kind": "customer", "id": "wgk_test"},
                "subject": {"kind": "dataset", "key": "ds1"},
                "payload": payload,
                "payload_hash": payload_hash_of(payload),
                "nonce": f"{i:032x}",
                "prev_hash": prev,
                "algo": "sha256",
            }
            ev["event_hash"] = event_hash_of(ev)
            prev = ev["event_hash"]
            ledger.append(ev)
        root = merkle_root([merkle_leaf(e["payload_hash"]) for e in ledger])
        rc_payload = {
            "epoch": 1,
            "first_seq": 1,
            "last_seq": 3,
            "merkle_root": root,
            "backend": "customer",
            "external_ref": "epoch-1",
            "ts_external": "2026-10-07T01:00:00Z",
            "algo": "sha256",
        }
        rc = {
            "v": 1,
            "ledger": "cust-st",
            "seq": 4,
            "id": "evt_" + "f" * 32,
            "type": RECEIPT_TYPE,
            "ts_platform": "2026-10-07T01:00:01Z",
            "actor": {"kind": "platform", "id": "sealer"},
            "subject": {"kind": "ledger", "id": "cust-st"},
            "payload": rc_payload,
            "payload_hash": payload_hash_of(rc_payload),
            "nonce": "a" * 32,
            "prev_hash": prev,
            "algo": "sha256",
        }
        rc["event_hash"] = event_hash_of(rc)
        ledger.append(rc)

        if tamper == "event":
            ledger[1]["payload"]["n"] = 999  # 改中段事件负载
            ledger[1]["payload_hash"] = payload_hash_of(ledger[1]["payload"])
            ledger[1]["event_hash"] = event_hash_of(ledger[1])  # 全链重算伪装
            for j in range(2, len(ledger)):
                ledger[j]["prev_hash"] = ledger[j - 1]["event_hash"]
                ledger[j]["event_hash"] = event_hash_of(ledger[j])
        elif tamper == "receipt":
            ledger[-1]["payload"]["merkle_root"] = "f" * 64  # 伪造回执根
            ledger[-1]["payload_hash"] = payload_hash_of(ledger[-1]["payload"])
            ledger[-1]["event_hash"] = event_hash_of(ledger[-1])  # 链上自洽的伪装

        (tmp / "ledger.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in ledger),
            encoding="utf-8",
        )
        return tmp

    cases = [("通过", None, True), ("篡改事件被发现", "event", False), ("伪造回执被发现", "receipt", False)]
    for name, tamper, expect_ok in cases:
        f = verify_bundle(build_bundle(tamper), None)
        status = "PASS" if f.ok == expect_ok else "SELFTEST-BROKEN"
        print(f"[{status}] {name}: ok={f.ok}")
        for ln in f.lines:
            print("   ", ln)
        if f.ok != expect_ok:
            return 1
    print("selftest 全部通过")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    # 解析 --pubkey <path>（成对取值，避免把值误计为位置参数）
    pubkey: Path | None = None
    positional: list[str] = []
    i = 1
    while i < len(argv):
        if argv[i] == "--pubkey":
            if i + 1 >= len(argv):
                print(_USAGE, file=sys.stderr)
                return 2
            pubkey = Path(argv[i + 1])
            i += 2
            continue
        if argv[i].startswith("--"):
            i += 1
            continue
        positional.append(argv[i])
        i += 1
    if len(positional) != 1:
        print(_USAGE, file=sys.stderr)
        return 2
    bundle = Path(positional[0])
    if not bundle.is_dir():
        print(f"不是目录: {bundle}", file=sys.stderr)
        return 2
    f = verify_bundle(bundle, pubkey)
    for ln in f.lines:
        print(ln)
    print("结论：", "通过" if f.ok else "发现篡改或不完整")
    return 0 if f.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
