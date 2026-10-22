"""私有化形态端到端验收（wire-provenance-ledger 任务 10.2）。

无公网出站（anchor-customer 后端）的完整链路在一个进程内走通：

    上传事件入账 → 修正/回滚事件入账（模拟转发器已入账的引擎事件）
    → epoch 封存（客户自持检查点落盘）→ 证据包导出
    → scripts/evidence_verify.py 离线核验通过（含检查点验签）

外部依赖：仅本仓代码与标准库 + cryptography（验签）。零网络。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fd_wire_api.provenance import envelope, init_provenance_db, ledger, reset_provenance_runtime
from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.provenance.anchors import customer_backend
from fd_wire_api.provenance.export import build_evidence_bundle
from fd_wire_api.provenance.ledger import append_event
from fd_wire_api.provenance.sealing import seal_ledger

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "evidence_verify.py"

LEDGER = "cust-private-e2e"


@pytest.fixture()
def private_env(tmp_path, monkeypatch):
    """客户自持检查点模式：无网络后端 + 独立检查点目录。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", f"sqlite:///{tmp_path}/e2e.db")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_PROVENANCE_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("FD_WIRE_PROVENANCE_ANCHOR_BACKEND", "customer")
    reset_datasets_runtime()
    reset_provenance_runtime()
    init_provenance_db()
    yield tmp_path
    reset_provenance_runtime()
    reset_datasets_runtime()


def _verify_bundle(dir_: Path, pubkey: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(VERIFY_SCRIPT), str(dir_)]
    if pubkey is not None:
        cmd += ["--pubkey", str(pubkey)]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_private_mode_full_chain(private_env, tmp_path) -> None:
    db = get_session()

    # ① 上传登记事件（任务 4.1 形态）
    append_event(
        db,
        ledger=LEDGER,
        type="dataset.upload.registered",
        actor={"kind": "customer", "id": "wgk_private"},
        subject={"kind": "upload", "id": "up-1"},
        payload={"filename": "sales.csv", "content_sha256": "sha256:" + "ab" * 32,
                 "size_bytes": 1024, "row_count": 10},
        event_id="evt_e2e_upload",
    )
    # ② 引擎修正事件（转发器入账形态，任务 3.3）
    append_event(
        db,
        ledger=LEDGER,
        type="dataset.correction.added",
        actor={"kind": "customer", "id": "wgk_private"},
        subject={"kind": "dataset", "key": "sales_2026"},
        payload={"dataset_key": "sales_2026", "op": "update",
                 "anchor": "hash:" + "cd" * 32, "correction_id": 1,
                 "actor": "wgk_private"},
        event_id="evt_e2e_correction",
    )
    db.commit()

    # ③ epoch 封存（anchor-customer：签名检查点落盘，零网络）
    sealed = seal_ledger(db, LEDGER)
    db.commit()
    assert sealed is not None and sealed["backend"] == "customer"

    checkpoint_dir = private_env / "checkpoints" / LEDGER
    checkpoints = sorted(checkpoint_dir.glob("epoch-*.json"))
    assert len(checkpoints) == 1, "客户自持检查点应恰好一份"
    backend = customer_backend  # 模块级单例实例
    verify = backend.verify_local(sealed)  # sealed 即回执 payload（含 external_ref）
    assert verify["status"] == "verified", verify

    # ④ 证据包导出 → ⑤ 离线核验（含检查点验签，走修好的 --pubkey CLI 路径）
    bundle_dir = tmp_path / "bundle"
    out = build_evidence_bundle(db, LEDGER, bundle_dir)
    assert (out / "ledger.jsonl").exists()

    from cryptography.hazmat.primitives import serialization

    checkpoint_json = eval_checkpoint(checkpoints[0])  # noqa: S307 —— 自产 JSON
    pub = serialization.load_pem_public_key(checkpoint_json["pubKeyPem"].encode())
    pub_pem = tmp_path / "pub.pem"
    pub_pem.write_bytes(public_pem_bytes(pub))

    result = _verify_bundle(bundle_dir, pub_pem)
    assert result.returncode == 0, f"离线核验未通过:\n{result.stdout}\n{result.stderr}"
    assert "通过" in result.stdout
    assert "签名有效" in result.stdout  # 检查点验签真的跑了

    # ⑥ 篡改兜底：包内改一条事件后核验必须失败
    lines = (bundle_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    tampered = lines[0].replace("sales.csv", "forged.csv")
    (bundle_dir / "ledger.jsonl").write_text(
        tampered + "\n" + "\n".join(lines[1:]), encoding="utf-8"
    )
    bad = _verify_bundle(bundle_dir, pub_pem)
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout
    db.close()


def eval_checkpoint(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def public_pem_bytes(pub) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
