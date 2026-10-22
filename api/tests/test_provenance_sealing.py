"""epoch 封存与可插拔锚定后端单测（wire-provenance-ledger 任务 5.1~5.6）。

覆盖：回执结构与 Merkle 根口径（与 envelope 权威一致）、二轮封存区间
不重叠不遗漏、后端失败绝不半封存、回执冒充可检知（重算区间根 +
fd-wire/scripts/evidence_verify.py 实弹核验）、customer 签名检查点往返、
ots 后端（monkeypatch 网络，测试不联网）、留位后端、空区间与封存循环、
链上零明文红线。

环境：内存 SQLite（StaticPool）+ create_all/触发器开发兜底——与
test_provenance_ledger.py 同款纪律。
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
from opentimestamps.core.notary import PendingAttestation
from opentimestamps.core.serialize import BytesDeserializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
from sqlalchemy import select

from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.provenance import (
    anchors,
    envelope,
    init_provenance_db,
    ledger,
    reset_provenance_runtime,
    sealing,
)
from fd_wire_api.provenance.models import LedgerEvent
from fd_wire_api.provenance.settings import get_settings

#: 证据包核验脚本（哈希口径唯一权威——回执/检查点必须能被它核验）
EVIDENCE_VERIFY = Path(__file__).resolve().parents[2] / "scripts" / "evidence_verify.py"


# ------------------------------------------------------------- 基建

@pytest.fixture()
def db(monkeypatch, tmp_path):
    """隔离运行时（内存库 + 临时检查点/钥匙目录），返回直连 Session。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_PROVENANCE_CHECKPOINT_DIR", str(tmp_path / "cps"))
    monkeypatch.setenv("FD_WIRE_PROVENANCE_SIGNING_KEY_PATH", str(tmp_path / "signing.pem"))
    reset_datasets_runtime()
    reset_provenance_runtime()
    anchors._KEY_CACHE.clear()
    init_provenance_db()
    session = get_session()
    yield session
    session.rollback()
    session.close()
    reset_datasets_runtime()
    reset_provenance_runtime()
    anchors._KEY_CACHE.clear()


def _append(db, *, ledger_name: str = "cust-001", payload: dict | None = None, **overrides):
    kwargs = dict(
        ledger=ledger_name,
        type="dataset.correction.added",
        actor={"kind": "customer", "id": ledger_name, "sig": None},
        subject={"kind": "dataset", "key": "sales_2026"},
        payload=payload or {"op": "update", "anchor": "pk:1", "correction_id": 7},
    )
    kwargs.update(overrides)
    return ledger.append_event(db, **kwargs)


def _receipts(db, ledger_name: str = "cust-001") -> list[LedgerEvent]:
    return list(
        db.execute(
            select(LedgerEvent)
            .where(
                LedgerEvent.ledger == ledger_name,
                LedgerEvent.type == sealing.RECEIPT_TYPE,
            )
            .order_by(LedgerEvent.seq)
        )
        .scalars()
    )


class StubBackend:
    """可编程桩后端：记录调用，可注入前 N 次失败。"""

    name = "stub"

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[dict] = []
        self.fail_times = fail_times

    def submit(self, ledger_name: str, root: str, meta: dict) -> dict:
        self.calls.append({"ledger": ledger_name, "root": root, "meta": dict(meta)})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("stub 锚定后端不可用")
        return {
            "backend": "stub",
            "external_ref": f"stub-ref-epoch-{meta['epoch']}",
            "ts_external": "2026-10-07T01:00:00Z",
        }

    def verify_local(self, receipt_payload: dict) -> dict:
        return {"status": "verified", "detail": "stub"}


def _export_bundle(db, ledger_name: str, bundle_dir: Path) -> Path:
    """把账本行导出为 evidence_verify.py 的 ledger.jsonl 布局。"""
    rows = list(
        db.execute(
            select(LedgerEvent).where(LedgerEvent.ledger == ledger_name).order_by(LedgerEvent.seq)
        )
        .scalars()
    )
    bundle_dir.mkdir(parents=True, exist_ok=True)
    with (bundle_dir / "ledger.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            ev = envelope.envelope_from_row(row)
            ev["event_hash"] = row.event_hash
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return bundle_dir


def _run_evidence_verify(bundle_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(EVIDENCE_VERIFY), str(bundle_dir), *extra],
        capture_output=True,
        text=True,
    )


def _evidence_verify_module():
    """进程内加载 evidence_verify.py（其 CLI 的 --pubkey 解析有缺陷：值被计为
    位置参数、恒 usage 退出 2——任务 7.2 侧待修；核验逻辑本身经此入口复用）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("evidence_verify", EVIDENCE_VERIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------- 1. 回执结构

def test_seal_one_epoch_receipt_structure(db) -> None:
    """三事件封存一个 epoch：回执在场、payload 字段齐、根与手工重算一致、seq 紧随。"""
    rows = [_append(db) for _ in range(3)]
    db.commit()
    stub = StubBackend()

    receipt = sealing.seal_ledger(db, "cust-001", backend=stub)

    assert receipt is not None
    assert set(receipt) == {
        "epoch", "first_seq", "last_seq", "merkle_root",
        "backend", "external_ref", "ts_external", "algo",
    }
    assert receipt["epoch"] == 1 and receipt["first_seq"] == 1 and receipt["last_seq"] == 3
    assert receipt["backend"] == "stub" and receipt["algo"] == "sha256"
    # 手工重算（不经 sealing.interval_root，直接用 envelope 权威口径）
    manual_root = envelope.merkle_root(
        [envelope.merkle_leaf(r.payload_hash) for r in rows]
    )
    assert receipt["merkle_root"] == manual_root

    saved = _receipts(db)
    assert len(saved) == 1
    row = saved[0]
    assert row.seq == 4  # 回执 seq 紧随区间（区间 [1,3] → 回执 4）
    assert row.payload == receipt
    assert row.actor == {"kind": "platform", "id": "sealer"}
    assert row.subject == {"kind": "ledger", "id": "cust-001"}
    assert ledger.verify_chain(db, "cust-001").ok  # 回执入链后链仍完整


# ------------------------------------------------------------- 2. 二轮封存

def test_second_epoch_intervals_no_gap_no_overlap(db) -> None:
    """二轮封存：epoch 递增、区间衔接无重叠无遗漏（上一回执 seq 被下区间覆盖但不入叶）。"""
    for _ in range(3):
        _append(db)
    db.commit()
    stub = StubBackend()
    sealing.seal_ledger(db, "cust-001", backend=stub)

    later = [_append(db) for _ in range(2)]  # seq 5,6（seq 4 是 epoch1 回执）
    db.commit()
    second = sealing.seal_ledger(db, "cust-001", backend=stub)

    assert second["epoch"] == 2
    assert second["first_seq"] == 4 and second["last_seq"] == 6
    # 区间衔接：epoch2.first == epoch1.last + 1（业务事件 1..6 全覆盖、零遗漏零重叠）
    first_receipt = _receipts(db)[0].payload
    assert second["first_seq"] == first_receipt["last_seq"] + 1
    # 根只覆盖区间内业务事件（seq 5,6），不含任何回执
    manual = envelope.merkle_root(
        [envelope.merkle_leaf(r.payload_hash) for r in later]
    )
    assert second["merkle_root"] == manual
    assert manual != first_receipt["merkle_root"]
    assert _receipts(db)[1].seq == 7
    assert ledger.verify_chain(db, "cust-001").ok


# ------------------------------------------------------------- 3. 后端失败

def test_backend_failure_never_half_sealed(db) -> None:
    """后端失败 → 无回执产生、无状态写入；下轮成功后区间完整。"""
    for _ in range(3):
        _append(db)
    db.commit()
    stub = StubBackend(fail_times=1)

    with pytest.raises(RuntimeError, match="stub 锚定后端不可用"):
        sealing.seal_ledger(db, "cust-001", backend=stub)

    assert _receipts(db) == []  # 绝不半封存
    assert len(list(db.execute(select(LedgerEvent)).scalars())) == 3

    receipt = sealing.seal_ledger(db, "cust-001", backend=stub)  # 下轮重试成功
    assert receipt["first_seq"] == 1 and receipt["last_seq"] == 3  # 区间完整无丢失


def test_seal_all_isolates_per_ledger_failure(db) -> None:
    """seal_all：单账本后端失败不阻断其余账本（失败如实记 error）。"""
    for _ in range(2):
        _append(db, ledger_name="cust-a")
    for _ in range(2):
        _append(db, ledger_name="cust-b")
    db.commit()
    bad, good = StubBackend(fail_times=99), StubBackend()

    class _Routing:
        name = "routing"

        def submit(self, ledger_name, root, meta):
            return bad.submit(ledger_name, root, meta) if ledger_name == "cust-a" else good.submit(ledger_name, root, meta)

        def verify_local(self, receipt_payload):
            return {"status": "verified", "detail": "stub"}

    results = sealing.seal_all(db, backend=_Routing())

    assert "error" in results["cust-a"] and "stub 锚定后端不可用" in results["cust-a"]["error"]
    assert results["cust-b"]["epoch"] == 1
    assert len(_receipts(db, "cust-a")) == 0
    assert len(_receipts(db, "cust-b")) == 1


# ------------------------------------------------------------- 4. 回执冒充

def test_receipt_impostor_detected_by_root_recompute(db, tmp_path) -> None:
    """冒充回执（假根入链）→ 区间根重算 ≠ 回执根，检知。"""
    for _ in range(3):
        _append(db)
    db.commit()
    stub = StubBackend()

    # 封存时 merkle_root 被偷换（模拟"别区间的根塞进回执"的链上自洽伪装）
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(envelope, "merkle_root", lambda leaves: "f" * 64)
        forged = sealing.seal_ledger(db, "cust-001", backend=stub)
    assert forged["merkle_root"] == "f" * 64
    assert ledger.verify_chain(db, "cust-001").ok  # 链本身自洽——冒充藏在根里

    # 检知面一：重算区间根比对不等
    recomputed = sealing.interval_root(db, "cust-001", 1, 3)
    assert recomputed != forged["merkle_root"]
    honest = envelope.merkle_root(
        [envelope.merkle_leaf(r.payload_hash) for r in
         db.execute(select(LedgerEvent).where(LedgerEvent.ledger == "cust-001",
                                              LedgerEvent.seq <= 3)).scalars()]
    )
    assert honest == recomputed  # 重算函数本身口径正确

    # 检知面二：证据包核验脚本（唯一 Merkle 权威）实弹判定失败
    bundle = _export_bundle(db, "cust-001", tmp_path / "bundle_impostor")
    proc = _run_evidence_verify(bundle)
    assert proc.returncode == 1
    assert "merkle_root 不匹配" in proc.stdout


def test_honest_receipt_passes_evidence_verify(db, tmp_path) -> None:
    """诚实封存的账本导出证据包 → evidence_verify.py 全绿（回执可被权威核验）。"""
    for _ in range(3):
        _append(db)
    db.commit()
    sealing.seal_ledger(db, "cust-001", backend=StubBackend())
    for _ in range(2):
        _append(db)
    db.commit()
    sealing.seal_ledger(db, "cust-001", backend=StubBackend())

    assert ledger.verify_chain(db, "cust-001").ok
    proc = _run_evidence_verify(_export_bundle(db, "cust-001", tmp_path / "bundle_honest"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "通过" in proc.stdout and proc.stdout.count("Merkle 根一致") == 2


# ------------------------------------------------------------- 5. customer 后端

def test_customer_backend_sign_verify_roundtrip(db, tmp_path) -> None:
    """customer：签名+验签往返、检查点自包含公钥、缺文件/坏签名如实 failed。"""
    backend = anchors.customer_backend
    root = envelope.merkle_root([envelope.merkle_leaf("sha256:" + "1" * 64)])
    meta = {"epoch": 3, "first_seq": 10, "last_seq": 20, "ts": "2026-10-07T01:00:00Z"}

    receipt = backend.submit("cust-001", root, meta)

    assert receipt["backend"] == "customer"
    cp_path = tmp_path / "cps" / "cust-001" / "epoch-3.json"
    assert cp_path.is_file()
    assert receipt["external_ref"] == "cust-001/epoch-3.json"
    cp = json.loads(cp_path.read_text(encoding="utf-8"))
    assert set(cp) == {
        "ledger", "epoch", "first_seq", "last_seq", "merkle_root", "ts",
        "key_id", "pubKeyPem", "signature",
    }
    assert cp["pubKeyPem"].startswith("-----BEGIN PUBLIC KEY-----")  # 自包含
    assert cp["merkle_root"] == root

    full = {**receipt, "epoch": 3, "merkle_root": root}
    verified = backend.verify_local(full)
    assert verified["status"] == "verified" and "签名有效" in verified["detail"]
    # 检查点被篡改（换根）：与回执对不上 → failed；对得上但签名失效 → 亦 failed
    cp["merkle_root"] = "e" * 64
    cp_path.write_text(json.dumps(cp), encoding="utf-8")
    assert backend.verify_local(full)["status"] == "failed"
    assert backend.verify_local({**full, "merkle_root": "e" * 64})["status"] == "failed"
    # 检查点缺失 → failed 如实
    assert backend.verify_local({**full, "external_ref": "cust-001/epoch-9.json"})["status"] == "failed"


def test_customer_mode_full_chain(db, tmp_path, monkeypatch) -> None:
    """客户模式 seal 全链：事件 → seal → 检查点文件在场且 verify_local 通过。"""
    monkeypatch.setenv("FD_WIRE_PROVENANCE_ANCHOR_BACKEND", "customer")
    reset_provenance_runtime()
    for _ in range(3):
        _append(db)
    db.commit()

    receipt = sealing.seal_ledger(db, "cust-001")  # settings 已指 customer

    assert receipt["backend"] == "customer"
    cp_path = tmp_path / "cps" / "cust-001" / "epoch-1.json"
    assert cp_path.is_file()
    result = anchors.customer_backend.verify_local(receipt)
    assert result["status"] == "verified"
    # 二轮：epoch 递增、新检查点落盘
    _append(db)
    db.commit()
    second = sealing.seal_ledger(db, "cust-001")
    assert second["epoch"] == 2
    assert (tmp_path / "cps" / "cust-001" / "epoch-2.json").is_file()
    # 检查点文件可被 evidence_verify.py 以 --pubkey 验签（证据包布局核验）
    bundle = _export_bundle(db, "cust-001", tmp_path / "bundle_customer")
    (bundle / "checkpoints").mkdir()
    (bundle / "checkpoints" / "epoch-1.json").write_text(cp_path.read_text(encoding="utf-8"))
    (bundle / "checkpoints" / "epoch-2.json").write_text(
        (tmp_path / "cps" / "cust-001" / "epoch-2.json").read_text(encoding="utf-8")
    )
    pub_pem = bundle / "pub.pem"
    pub_pem.write_text(json.loads(cp_path.read_text(encoding="utf-8"))["pubKeyPem"], encoding="utf-8")
    findings = _evidence_verify_module().verify_bundle(bundle, pub_pem)
    assert findings.ok, "\n".join(findings.lines)
    assert "\n".join(findings.lines).count("签名有效") == 2


# ------------------------------------------------------------- 6. ots 后端

class _FakeCalendar:
    """离线假 calendar：直接返回带 PendingAttestation 的 Timestamp（不联网）。"""

    seen_urls: list[str] = []

    def __init__(self, url: str) -> None:
        self.url = url
        _FakeCalendar.seen_urls.append(url)

    def submit(self, digest: bytes, timeout=None) -> Timestamp:
        stamp = Timestamp(digest)
        # 每 calendar 一个可区分的 URI（Timestamp.merge 按证明集合去重）
        stamp.attestations.add(PendingAttestation(f"https://fake.calendar.test/{self.url}"))
        return stamp


@pytest.fixture()
def fake_calendars(monkeypatch, db):
    _FakeCalendar.seen_urls = []
    monkeypatch.setattr(anchors, "RemoteCalendar", _FakeCalendar)
    monkeypatch.setenv("FD_WIRE_PROVENANCE_OTS_CALENDARS", "https://cal.one.test, https://cal.two.test")
    reset_provenance_runtime()
    yield
    _FakeCalendar.seen_urls = []


def test_ots_backend_offline_submit_and_pending(db, fake_calendars) -> None:
    """ots：submit → base64 external_ref + calendar env 覆盖生效；verify_local 如实 pending。"""
    root = envelope.merkle_root([envelope.merkle_leaf("sha256:" + "2" * 64)])
    meta = {"epoch": 1, "first_seq": 1, "last_seq": 2, "ts": "2026-10-07T01:00:00Z"}

    receipt = anchors.ots_backend.submit("cust-001", root, meta)

    assert receipt["backend"] == "ots"
    assert _FakeCalendar.seen_urls == ["https://cal.one.test", "https://cal.two.test"]
    ots_bytes = base64.b64decode(receipt["external_ref"], validate=True)
    detached = DetachedTimestampFile.deserialize(BytesDeserializationContext(ots_bytes))
    assert detached.file_digest == bytes.fromhex(root)  # 凭据承诺的摘要 = 区间根
    attestations = [a for _, a in detached.timestamp.all_attestations()]
    assert len(attestations) == 2  # 两个 calendar 的 pending 证明都并入

    result = anchors.ots_backend.verify_local({**receipt, "epoch": 1, "merkle_root": root})
    assert result["status"] == "pending"  # 无 BTC 确认 → 如实 pending
    assert "pending" in result["detail"]
    # 凭据摘要与回执根不符（冒充/错配）→ failed
    mismatch = anchors.ots_backend.verify_local({**receipt, "epoch": 1, "merkle_root": "0" * 64})
    assert mismatch["status"] == "failed"
    # 凭据损坏 → failed
    corrupt = anchors.ots_backend.verify_local(
        {"external_ref": base64.b64encode(b"garbage").decode(), "merkle_root": root}
    )
    assert corrupt["status"] == "failed"


def test_ots_backend_all_calendars_down_raises(db, monkeypatch) -> None:
    """全部 calendar 失败 → submit 抛出（上层本轮不封存，下轮重试）。"""
    class _Down:
        def __init__(self, url: str) -> None:
            pass

        def submit(self, digest: bytes, timeout=None) -> Timestamp:
            raise TimeoutError("network unreachable")

    monkeypatch.setattr(anchors, "RemoteCalendar", _Down)
    with pytest.raises(RuntimeError, match="全部 calendar 提交失败"):
        anchors.ots_backend.submit(
            "cust-001", "3" * 64, {"epoch": 1, "first_seq": 1, "last_seq": 1, "ts": "t"}
        )


# ------------------------------------------------------------- 7. 留位后端

def test_placeholder_backends_and_settings_accept_names(db, monkeypatch) -> None:
    """consortium/rfc3161：配置即在、submit NotImplementedError；未知名清晰报错。"""
    for name in ("consortium", "rfc3161"):
        backend = anchors.get_backend(name)
        assert backend.name == name
        with pytest.raises(NotImplementedError, match="即插"):
            backend.submit("cust-001", "4" * 64, {"epoch": 1, "first_seq": 1, "last_seq": 1, "ts": "t"})
        assert backend.verify_local({})["status"] == "unsupported"

    # settings 校验允许留位配置名（配置面就绪）
    monkeypatch.setenv("FD_WIRE_PROVENANCE_ANCHOR_BACKEND", "consortium")
    reset_provenance_runtime()
    assert get_settings().anchor_backend == "consortium"

    with pytest.raises(ValueError, match="未知锚定后端"):
        anchors.get_backend("bogus")


def test_null_backend_defensive(db) -> None:
    """none 后端：防御性抛错（chain_only 世界不该调到封存）。"""
    with pytest.raises(RuntimeError, match="chain_only"):
        anchors.null_backend.submit("cust-001", "5" * 64, {"epoch": 1, "first_seq": 1, "last_seq": 1, "ts": "t"})
    assert anchors.get_backend("none") is anchors.null_backend
    assert anchors.null_backend.verify_local({})["status"] == "unsupported"


# ------------------------------------------------------------- 8. 空区间 / 循环

def test_empty_interval_returns_none(db) -> None:
    """空区间（无事件 / 已全封存）→ seal 返回 None，不产生回执。"""
    assert sealing.seal_ledger(db, "cust-empty", backend=StubBackend()) is None

    for _ in range(2):
        _append(db)
    db.commit()
    stub = StubBackend()
    assert sealing.seal_ledger(db, "cust-001", backend=stub)["epoch"] == 1
    assert sealing.seal_ledger(db, "cust-001", backend=stub) is None  # 已全封存
    assert stub.calls.__len__() == 1  # 空区间根本不触后端
    assert len(_receipts(db)) == 1

    # seal_all：含无事件/已封存账本时逐个尝试、空区间跳过
    _append(db, ledger_name="cust-b")
    db.commit()
    results = sealing.seal_all(db, backend=stub)
    assert set(results) == {"cust-001", "cust-b"}
    assert results["cust-001"] is None  # 已全封存 → 跳过
    assert results["cust-b"]["epoch"] == 1


def test_sealer_loop_start_stop_clean(db) -> None:
    """封存循环：起/停干净（cancel 收殓）、none 后端空转、真后端轮内完成封存。"""

    async def scenario() -> None:
        # backend=none（默认 env）→ 空转不封存、循环稳定存活
        task = sealing.start_sealer_loop(0.02)
        await asyncio.sleep(0.08)
        assert not task.done()
        sealing.stop_sealer_loop()
        with pytest.raises(asyncio.CancelledError):
            await task
        sealing.stop_sealer_loop()  # 重复 stop 安全
        assert _receipts(db) == []

    asyncio.run(scenario())


def test_sealer_loop_seals_with_configured_backend(db, monkeypatch) -> None:
    """循环 + 已配置后端：轮内完成封存（下轮空转）——接线面（app.py）归主代理。"""
    monkeypatch.setenv("FD_WIRE_PROVENANCE_ANCHOR_BACKEND", "customer")
    reset_provenance_runtime()
    for _ in range(3):
        _append(db)
    db.commit()

    async def scenario() -> None:
        task = sealing.start_sealer_loop(0.02)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if _receipts(db):
                break
        sealing.stop_sealer_loop()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    receipts = _receipts(db)
    assert len(receipts) == 1
    assert receipts[0].payload["backend"] == "customer"
    assert (Path(get_settings().checkpoint_dir) / "cust-001" / "epoch-1.json").is_file()


# ------------------------------------------------------------- 9. 链上零明文

def test_zero_plaintext_redline(db, tmp_path, monkeypatch, fake_calendars) -> None:
    """红线：customer/ots 回执与检查点内容除哈希/序号/时间/文件名外无业务明文。"""
    monkeypatch.setenv("FD_WIRE_PROVENANCE_ANCHOR_BACKEND", "customer")
    reset_provenance_runtime()
    sentinels = [
        "SENTINEL_ROW_ANCHOR_XYZ",
        "SENTINEL_COLUMN_SALARY",
        "SENTINEL_明文值九阴真经",
    ]
    for i, sent in enumerate(sentinels):
        _append(db, payload={"op": "update", "anchor": sent, "column": sentinels[1], "value": sentinels[2], "n": i})
    db.commit()

    customer_receipt = sealing.seal_ledger(db, "cust-001")  # settings=customer → epoch 1
    for i in range(2):  # epoch 2 走 ots：再入两条带哨兵的事件
        _append(db, payload={"op": "insert", "anchor": sentinels[0], "value": sentinels[2], "n": 10 + i})
    db.commit()
    ots_receipt = sealing.seal_ledger(db, "cust-001", backend=anchors.ots_backend)
    assert customer_receipt["epoch"] == 1 and ots_receipt["epoch"] == 2
    assert ots_receipt["last_seq"] - ots_receipt["first_seq"] >= 1  # ots 区间含真实事件

    for receipt, blob_desc in (
        (customer_receipt, "customer 回执"),
        (ots_receipt, "ots 回执"),
    ):
        text = json.dumps(receipt, ensure_ascii=False)
        for sent in sentinels:
            assert sent not in text, f"{blob_desc} 泄漏业务明文：{sent}"
        # 回执字段白名单（哈希/序号/时间/后端标识/外部凭证/算法位）
        assert set(receipt) <= {
            "epoch", "first_seq", "last_seq", "merkle_root",
            "backend", "external_ref", "ts_external", "algo",
        }

    # 检查点文件内容零明文（自包含公钥/签名是密钥材料，非业务值）
    for cp_file in (tmp_path / "cps" / "cust-001").glob("epoch-*.json"):
        cp_text = cp_file.read_text(encoding="utf-8")
        for sent in sentinels:
            assert sent not in cp_text, f"检查点 {cp_file.name} 泄漏业务明文：{sent}"

    # OTS 凭据字节同样零明文（ASCII 与 UTF-8 两种形态都查）
    ots_bytes = base64.b64decode(ots_receipt["external_ref"], validate=True)
    for sent in sentinels:
        assert sent.encode("utf-8") not in ots_bytes
