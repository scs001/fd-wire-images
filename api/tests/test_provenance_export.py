"""证据包导出测试（wire-provenance-ledger 任务 7.1）。

核心用例：**导出 → 离线核验闭环**——真实账本（含手工构造的合法回执）
经 ``export.build_evidence_bundle`` 组包后，subprocess 运行格式权威
``fd-wire/scripts/evidence_verify.py`` 退出码 0；篡改三态（改中段事件
payload / 伪装回执 merkle_root / 删事件）逐一被检出（退出码 1 + FAIL
定位）。另覆盖 zip 下载路由与检查点路由（200/404）。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.provenance import (
    envelope,
    export,
    init_provenance_db,
    ledger,
    reset_provenance_runtime,
)

#: 离线核验脚本（格式权威）：api/tests/ → 上两级 = fd-wire 根
EVIDENCE_VERIFY = Path(__file__).resolve().parents[2] / "scripts" / "evidence_verify.py"

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_A = "sub-cust-001"
CUSTOMER_A = bearer(mint_id_token(SUB_A))

#: 信封哈希基字段（重组 event_hash 用，与 envelope.envelope_from_row 同构）
_ENV_KEYS = (
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


@pytest.fixture()
def db(monkeypatch):
    """隔离运行时（内存库）+ 直连 Session（与 test_provenance_ledger 同款）。"""
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


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    """隔离运行时 + 全量 app TestClient（与 test_provenance_routes 同款）。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("FD_WIRE_PROVENANCE_INGEST_TOKEN", raising=False)
    install_fake_jwks(monkeypatch)
    make_console_env(tmp_path, monkeypatch, customers=[(SUB_A, "cust-001")])
    reset_datasets_runtime()
    reset_provenance_runtime()
    with TestClient(fastapi_app) as client:
        yield SimpleNamespace(
            client=client, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
    reset_provenance_runtime()
    reset_datasets_runtime()


# ------------------------------------------------------------- 造数 / 工具


def _seed(
    db,
    *,
    ledger_name: str = "cust-001",
    n_events: int = 3,
    merkle_override: str | None = None,
):
    """n 条业务事件 + 1 条封存回执（区间 Merkle 根按真实事件重算）。"""
    rows = []
    for i in range(1, n_events + 1):
        rows.append(
            ledger.append_event(
                db,
                ledger=ledger_name,
                type="dataset.correction.added",
                actor={"kind": "customer", "id": ledger_name, "sig": None},
                subject={"kind": "dataset", "key": "sales_2026"},
                payload={"op": "update", "anchor": f"pk:{i}", "n": i},
            )
        )
    root = merkle_override or envelope.merkle_root(
        [envelope.merkle_leaf(r.payload_hash) for r in rows]
    )
    receipt = ledger.append_event(
        db,
        ledger=ledger_name,
        type="ledger.epoch.sealed",
        actor={"kind": "platform", "id": "sealer"},
        subject={"kind": "ledger", "id": ledger_name},
        payload={
            "epoch": 1,
            "first_seq": 1,
            "last_seq": n_events,
            "merkle_root": root,
            "backend": "customer",
            "external_ref": "epoch-1",
            "ts_external": "2026-10-07T01:00:00Z",
            "algo": "sha256",
        },
    )
    db.commit()
    return rows, receipt


def _read_lines(bundle: Path) -> list[dict]:
    return [
        json.loads(ln)
        for ln in (bundle / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def _write_lines(bundle: Path, lines: list[dict]) -> None:
    (bundle / "ledger.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in lines),
        encoding="utf-8",
    )


def _reseal(line: dict) -> dict:
    """就地重算 payload_hash/event_hash（伪装"链上自洽"的篡改）。"""
    line["payload_hash"] = envelope.payload_hash(line["payload"])
    line["event_hash"] = envelope.event_hash({k: line[k] for k in _ENV_KEYS})
    return line


def run_verify(bundle: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(EVIDENCE_VERIFY), str(bundle)],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


# ------------------------------------------------------------- 导出 → 离线核验闭环


def test_bundle_layout(db, tmp_path) -> None:
    """布局：ledger.jsonl 全量信封行（v/event_hash/created_at）+ 目录结构。"""
    rows, receipt = _seed(db)
    bundle = export.build_evidence_bundle(db, "cust-001", tmp_path / "bundle")

    lines = _read_lines(bundle)
    assert [ln["seq"] for ln in lines] == [1, 2, 3, 4]
    assert lines[-1]["id"] == receipt.id
    assert lines[-1]["payload"]["last_seq"] == len(rows)
    assert all(ln["v"] == 1 and ln["event_hash"].startswith("sha256:") for ln in lines)
    assert all(ln["created_at"] for ln in lines)
    assert lines[-1]["type"] == "ledger.epoch.sealed"
    # 检查点未配置 → checkpoints/ 留空；anchors/ 有 README
    assert (bundle / "checkpoints").is_dir()
    assert not list((bundle / "checkpoints").iterdir())
    assert (bundle / "anchors" / "README.txt").is_file()
    assert bundle == tmp_path / "bundle"


def test_export_then_offline_verify_passes(db, tmp_path) -> None:
    """核心闭环：未篡改证据包 → 核验退出码 0、结论通过。"""
    _seed(db)
    bundle = export.build_evidence_bundle(db, "cust-001", tmp_path / "bundle")

    rc, out = run_verify(bundle)
    assert rc == 0, out
    assert "FAIL" not in out
    assert "通过" in out
    assert "Merkle 根一致" in out  # 回执区间被真核验


def test_tamper_event_payload_detected(db, tmp_path) -> None:
    """篡改态 1：改中段事件 payload → 退出码 1 + event_hash FAIL。"""
    _seed(db)
    bundle = export.build_evidence_bundle(db, "cust-001", tmp_path / "bundle")

    lines = _read_lines(bundle)
    lines[1]["payload"]["n"] = 999  # 只改负载，哈希不动
    _write_lines(bundle, lines)

    rc, out = run_verify(bundle)
    assert rc == 1
    assert "FAIL" in out and "event_hash" in out


def test_tamper_receipt_merkle_root_detected(db, tmp_path) -> None:
    """篡改态 2：伪装回执 merkle_root（链上自洽重哈希）→ 区间重算检出。"""
    _seed(db)
    bundle = export.build_evidence_bundle(db, "cust-001", tmp_path / "bundle")

    lines = _read_lines(bundle)
    lines[-1]["payload"]["merkle_root"] = "f" * 64
    _reseal(lines[-1])  # 重算 payload_hash/event_hash：链连续伪装到位
    _write_lines(bundle, lines)

    rc, out = run_verify(bundle)
    assert rc == 1
    assert "FAIL" in out and "merkle_root" in out


def test_tamper_delete_event_detected(db, tmp_path) -> None:
    """篡改态 3：删一条中段事件 → 后继 prev_hash 断裂检出。"""
    _seed(db)
    bundle = export.build_evidence_bundle(db, "cust-001", tmp_path / "bundle")

    lines = _read_lines(bundle)
    del lines[1]  # 删 seq=2
    _write_lines(bundle, lines)

    rc, out = run_verify(bundle)
    assert rc == 1
    assert "FAIL" in out and "prev_hash" in out


def test_bundle_copies_checkpoints(db, tmp_path, monkeypatch) -> None:
    """配置 checkpoint_dir 时 epoch-*.json 全部拷入包内（离线可验签）。"""
    cp_root = tmp_path / "cps"
    (cp_root / "cust-001").mkdir(parents=True)
    (cp_root / "cust-001" / "epoch-1.json").write_text(
        json.dumps({"epoch": 1, "merkle_root": "0" * 64}), encoding="utf-8"
    )
    (cp_root / "cust-001" / "epoch-2.json").write_text("{}", encoding="utf-8")
    (cp_root / "cust-001" / "notes.txt").write_text("不拷", encoding="utf-8")
    monkeypatch.setattr(
        export, "get_settings", lambda: SimpleNamespace(checkpoint_dir=str(cp_root))
    )

    _seed(db)
    bundle = export.build_evidence_bundle(db, "cust-001", tmp_path / "bundle")
    copied = sorted(p.name for p in (bundle / "checkpoints").glob("epoch-*.json"))
    assert copied == ["epoch-1.json", "epoch-2.json"]
    assert not (bundle / "checkpoints" / "notes.txt").exists()


# ------------------------------------------------------------- zip 下载路由


def test_evidence_package_route_streams_zip(client_env) -> None:
    """200 + Content-Disposition + 体内为合法 zip（含 ledger.jsonl）。"""
    client = client_env.client
    for _ in range(2):
        resp = client.post(
            "/api/v1/provenance/ingest",
            json={
                "ledger": "cust-001",
                "type": "dataset.correction.added",
                "actor": {"kind": "customer", "id": "cust-001", "sig": None},
                "subject": {"kind": "dataset", "key": "sales_2026"},
                "payload": {"op": "update", "anchor": "pk:1"},
            },
        )
        assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/provenance/evidence-package", headers=CUSTOMER_A)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="evidence-package-cust-001-{stamp}.zip"'
    )

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert "ledger.jsonl" in names
        assert "anchors/README.txt" in names
        lines = [
            json.loads(ln)
            for ln in zf.read("ledger.jsonl").decode("utf-8").splitlines()
            if ln.strip()
        ]
        assert [ln["seq"] for ln in lines] == [1, 2]


def test_evidence_package_route_auth(client_env) -> None:
    """缺客户头 401；账本隔离：他人账本事件不进本客户包。"""
    client = client_env.client
    no_header = client.get("/api/v1/provenance/evidence-package")
    assert no_header.status_code == 401

    client.post(
        "/api/v1/provenance/ingest",
        json={
            "ledger": "cust-002",
            "type": "dataset.correction.added",
            "actor": {"kind": "customer", "id": "cust-002", "sig": None},
            "subject": {"kind": "dataset", "key": "sales_2026"},
            "payload": {"op": "update", "anchor": "pk:1"},
        },
    )
    resp = client.get("/api/v1/provenance/evidence-package", headers=CUSTOMER_A)
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        lines = [
            json.loads(ln)
            for ln in zf.read("ledger.jsonl").decode("utf-8").splitlines()
            if ln.strip()
        ]
        assert lines == []  # cust-001 账本为空：不夹带他账本事件


# ------------------------------------------------------------- 检查点单文件路由


def test_checkpoint_route_404_without_config(client_env) -> None:
    """未配置 checkpoint_dir（或文件不存在）→ 404，不伪造。"""
    client = client_env.client
    resp = client.get("/api/v1/provenance/checkpoints/1", headers=CUSTOMER_A)
    assert resp.status_code == 404
    bad_epoch = client.get("/api/v1/provenance/checkpoints/0", headers=CUSTOMER_A)
    assert bad_epoch.status_code == 404
    no_header = client.get("/api/v1/provenance/checkpoints/1")
    assert no_header.status_code == 401


def test_checkpoint_route_serves_file(client_env) -> None:
    """配置 checkpoint_dir 后：存在 → 200 附件下载；不存在 → 404。"""
    cp_root = client_env.tmp_path / "cps"
    (cp_root / "cust-001").mkdir(parents=True)
    body = json.dumps({"epoch": 1, "merkle_root": "0" * 64}, ensure_ascii=False)
    (cp_root / "cust-001" / "epoch-1.json").write_text(body, encoding="utf-8")
    client_env.monkeypatch.setattr(
        export, "get_settings", lambda: SimpleNamespace(checkpoint_dir=str(cp_root))
    )

    client = client_env.client
    resp = client.get("/api/v1/provenance/checkpoints/1", headers=CUSTOMER_A)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == (
        'attachment; filename="epoch-1.json"'
    )
    assert json.loads(resp.content) == json.loads(body)

    missing = client.get("/api/v1/provenance/checkpoints/9", headers=CUSTOMER_A)
    assert missing.status_code == 404
