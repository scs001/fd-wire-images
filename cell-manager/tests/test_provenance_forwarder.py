"""provenance 转发器测试（wire-provenance-ledger 任务 4.2）。

覆盖：env 禁用、outbox 持久与重试、成功出队、失败保留、生命周期集成
（api 不可达不打断操作、成功 op 入账）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cell_manager.config import CellManagerConfig
from cell_manager.manager import CellManager
from cell_manager.provenance import ProvenanceForwarder


def _make_forwarder(tmp_path: Path, url: str) -> ProvenanceForwarder:
    return ProvenanceForwarder(
        url=url, token="tok", outbox_path=tmp_path / "audit" / "provenance_outbox.jsonl"
    )


def test_from_env_disabled_without_url(tmp_path: Path) -> None:
    assert ProvenanceForwarder.from_env(env={}, home=tmp_path) is None
    fwd = ProvenanceForwarder.from_env(
        env={"CELL_PROVENANCE_URL": "http://api/provenance/ingest", "CELL_PROVENANCE_TOKEN": "t"},
        home=tmp_path,
    )
    assert fwd is not None
    assert fwd.pending() == 0


def test_emit_unreachable_keeps_outbox(tmp_path: Path) -> None:
    fwd = _make_forwarder(tmp_path, "http://127.0.0.1:1/provenance/ingest")
    fwd.emit("provision", "cust1", {"irreversible": False})
    assert fwd.pending() == 1
    # outbox 内容可解析、字段齐
    line = json.loads(
        (tmp_path / "audit" / "provenance_outbox.jsonl").read_text(encoding="utf-8").strip()
    )
    assert line["type"] == "cell.lifecycle"
    assert line["ledger"] == "cust1"
    assert line["payload"]["action"] == "provision"
    assert line["event_id"].startswith("evt_")


def _serve(result: list[dict], status: int = 200) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            result.append(
                {"body": body, "token": self.headers.get("X-Provenance-Token")}
            )
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:  # 静默
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_flush_success_drains_and_failure_retains(tmp_path: Path) -> None:
    received: list[dict] = []
    server = _serve(received)
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/provenance/ingest"
        fwd = _make_forwarder(tmp_path, url)
        fwd.emit("provision", "cust1", {"a": 1})
        fwd.emit("pause", "cust1", None)
        assert fwd.pending() == 0
        assert len(received) == 2
        assert received[0]["token"] == "tok"
        assert received[0]["body"]["type"] == "cell.lifecycle"
        assert received[0]["body"]["ts_platform"].endswith("Z")
    finally:
        server.shutdown()

    # api 再不可达时：新事件留 outbox，旧的不丢
    fwd2 = _make_forwarder(tmp_path, "http://127.0.0.1:1/provenance/ingest")
    fwd2.emit("resume", "cust1", None)
    assert fwd2.pending() == 1


def test_flush_non_2xx_retains(tmp_path: Path) -> None:
    received: list[dict] = []
    server = _serve(received, status=500)
    try:
        url = f"http://127.0.0.1:{server.server_port}/ingest"
        fwd = _make_forwarder(tmp_path, url)
        fwd.emit("purge", "cust1", {"irreversible": True})
        assert fwd.pending() == 1
    finally:
        server.shutdown()


def test_lifecycle_integration_forwarder_failure_does_not_break_ops(
    tmp_path: Path, fake_driver, fake_registry, config: CellManagerConfig
) -> None:
    fwd = _make_forwarder(tmp_path, "http://127.0.0.1:1/provenance/ingest")
    manager = CellManager(
        driver=fake_driver,
        registry=fake_registry,
        config=config,
        provenance_forwarder=fwd,
    )
    status = manager.provision("cust-int")  # api 不可达也必须成功
    assert status.state == "active"
    assert fwd.pending() == 1  # 事件留 outbox 待重试

    received: list[dict] = []
    server = _serve(received)
    try:
        ok_fwd = ProvenanceForwarder(
            url=f"http://127.0.0.1:{server.server_port}/ingest",
            token="",
            outbox_path=fwd._outbox,
        )
        manager2 = CellManager(
            driver=fake_driver,
            registry=fake_registry,
            config=config,
            provenance_forwarder=ok_fwd,
        )
        manager2.pause("cust-int")
        # 旧 pending（provision）与新事件（pause）都被清空
        assert ok_fwd.pending() == 0
        actions = [r["body"]["payload"]["action"] for r in received]
        assert "provision" in actions and "pause" in actions
    finally:
        server.shutdown()
