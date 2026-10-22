"""OpenBAO KV v2 客户端：本地 http.server 假实例（无网依赖）。

覆盖：put/get/delete 正常路径、KV v2 双层嵌套、404→None、token 头透传、
403/503/连接拒绝的指引消息、非 JSON 响应防御。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cell_manager.openbao import OpenbaoClient, OpenbaoError


class _VaultishState:
    """假 OpenBAO 的全部可控行为（每测试一台服务器、一份状态）。"""

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.requests: list[dict] = []  # {"method", "path", "token", "body"}
        # 状态覆写（None=正常路由）：非 2xx 时直接以该码返回空 body
        self.status_override: int | None = None
        self.raw_override: bytes | None = None


def _start_fake_vault(state: _VaultishState) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _record(self, body: dict | None) -> None:
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "token": self.headers.get("X-Vault-Token"),
                    "body": body,
                }
            )

        def _reply(self, status: int, payload: dict | None = None, raw: bytes | None = None) -> None:
            data = raw if raw is not None else (
                json.dumps(payload).encode("utf-8") if payload is not None else b""
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _handle(self, body: dict | None = None) -> None:
            self._record(body)
            if state.raw_override is not None:
                self._reply(200, raw=state.raw_override)
                return
            if state.status_override is not None:
                self._reply(state.status_override)
                return
            prefix = "/v1/secret/data/"
            assert self.path.startswith(prefix), self.path
            key = self.path[len(prefix):]
            if self.command == "PUT":
                state.entries[key] = dict(body["data"])  # KV v2 外层 {"data": ...}
                self._reply(200, {"request_id": "fake"})
            elif self.command == "GET":
                if key in state.entries:
                    self._reply(200, {"data": {"data": state.entries[key]}})
                else:
                    self._reply(404, {"errors": []})
            elif self.command == "DELETE":
                state.entries.pop(key, None)
                self._reply(204)
            else:  # pragma: no cover - 客户端不会发其它方法
                self._reply(405)

        def do_PUT(self) -> None:  # noqa: N802 - http.server 命名约定
            length = int(self.headers.get("Content-Length") or 0)
            self._handle(json.loads(self.rfile.read(length)) if length else None)

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle()

        def log_message(self, *args) -> None:  # 静音默认 stderr 日志
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def vault():
    state = _VaultishState()
    server = _start_fake_vault(state)
    client = OpenbaoClient(f"http://127.0.0.1:{server.server_address[1]}", "test-token-1")
    yield state, client
    server.shutdown()
    server.server_close()


class TestHappyPaths:
    def test_put_get_roundtrip_and_kv2_shape(self, vault):
        state, client = vault
        client.put("wire/cell-db/acme", {"url": "postgresql://x", "password": "pw1"})

        # PUT 请求体是 KV v2 形态 {"data": {...}}，路径带 secret/data 前缀
        put_req = state.requests[0]
        assert put_req["method"] == "PUT"
        assert put_req["path"] == "/v1/secret/data/wire/cell-db/acme"
        assert put_req["body"] == {"data": {"url": "postgresql://x", "password": "pw1"}}
        # GET 解开双层嵌套，拿到原始条目
        assert client.get("wire/cell-db/acme") == {"url": "postgresql://x", "password": "pw1"}

    def test_token_header_is_sent(self, vault):
        state, client = vault
        client.put("wire/cell-db/acme", {"password": "pw1"})
        client.get("wire/cell-db/acme")
        client.delete("wire/cell-db/acme")
        assert {r["token"] for r in state.requests} == {"test-token-1"}

    def test_get_missing_returns_none(self, vault):
        _, client = vault
        assert client.get("wire/cell-db/ghost") is None

    def test_delete_is_idempotent_on_404(self, vault):
        _, client = vault
        client.delete("wire/cell-db/never-existed")  # 不抛
        client.delete("wire/cell-db/never-existed")  # 再删一次也不抛

    def test_delete_removes_entry(self, vault):
        _, client = vault
        client.put("wire/cell-db/acme", {"password": "pw1"})
        client.delete("wire/cell-db/acme")
        assert client.get("wire/cell-db/acme") is None

    def test_path_leading_slash_tolerated(self, vault):
        state, client = vault
        client.put("/wire/cell-db/acme", {"password": "pw1"})
        assert state.requests[0]["path"] == "/v1/secret/data/wire/cell-db/acme"


class TestErrors:
    def test_403_mentions_token_guidance(self, vault):
        state, client = vault
        state.status_override = 403
        with pytest.raises(OpenbaoError, match="权限不足"):
            client.get("wire/cell-db/acme")
        with pytest.raises(OpenbaoError, match="wire-cell-db"):
            client.put("wire/cell-db/acme", {"password": "x"})

    def test_503_mentions_unseal_guidance(self, vault):
        state, client = vault
        state.status_override = 503
        with pytest.raises(OpenbaoError, match="解封"):
            client.get("wire/cell-db/acme")
        with pytest.raises(OpenbaoError, match="解封"):
            client.put("wire/cell-db/acme", {"password": "x"})
        with pytest.raises(OpenbaoError, match="解封"):
            client.delete("wire/cell-db/acme")

    def test_500_mentions_status(self, vault):
        state, client = vault
        state.status_override = 500
        with pytest.raises(OpenbaoError, match="HTTP 500"):
            client.put("wire/cell-db/acme", {"password": "x"})

    def test_connection_refused_mentions_addr_guidance(self):
        # 端口 1 无监听 → 连接拒绝 → 指引核对 CELL_OPENBAO_ADDR
        client = OpenbaoClient("http://127.0.0.1:1", "t")
        with pytest.raises(OpenbaoError, match="地址核对"):
            client.get("wire/cell-db/acme")
        with pytest.raises(OpenbaoError, match="CELL_OPENBAO_ADDR"):
            client.put("wire/cell-db/acme", {"password": "x"})

    def test_bad_addr_scheme_mentions_addr_guidance(self):
        client = OpenbaoClient("not-a-url", "t")
        with pytest.raises(OpenbaoError, match="地址"):
            client.get("wire/cell-db/acme")

    def test_malformed_json_raises(self, vault):
        state, client = vault
        state.raw_override = b"{not json"
        with pytest.raises(OpenbaoError, match="malformed JSON"):
            client.get("wire/cell-db/acme")

    def test_error_message_never_contains_token(self, vault):
        state, client = vault
        state.status_override = 403
        try:
            client.get("wire/cell-db/acme")
        except OpenbaoError as exc:
            assert "test-token-1" not in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected OpenbaoError")
