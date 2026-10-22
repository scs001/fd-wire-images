"""fd_mcp_client 契约钉住（wire-native-channel-cutover）。

覆盖：
- native 单模式线协议：initialize 握手序列（→ Mcp-Session-Id →
  notifications/initialized → tools/call 带会话头）、惰性握手与会话
  复用、list_tools、SSE 应答解码、协议版本回退、close 释放会话；
- gateway 模式不存在（退役钉住）：包公开面无任何简化报文客户端；
- 错误体系：传输层失败 → ``McpTransportError``；工具报错/契约不符 →
  ``McpCallError``；
- ingest 经 native 客户端：env 解析复用（map 覆盖优先/约定派生）、
  工具新名（无 ``daas_`` 前缀）调用序列、会话复用、
  ``EngineUnavailable``/``EngineCallError`` 包装与握手期 404/405 的
  「cell 未开通」语义。

外部 HTTP 全部经注入的 httpx.MockTransport mock，绝不打真网。
"""

from __future__ import annotations

import json

import httpx
import pytest

import fd_mcp_client
from fd_mcp_client import (
    McpCallError,
    McpTransportError,
    NativeMcpClient,
    unwrap_tool_result,
)
from fd_wire_api.datasets.ingest import (
    TOOL_INGEST_BEGIN,
    TOOL_PREVIEW,
    EngineCallError,
    EngineToolClient,
    EngineUnavailable,
)


# ------------------------------------------------------------------ 退役钉住


def test_gateway_mode_is_gone() -> None:
    """gateway 简化模式退役：包公开面（与模块面）无简化报文客户端。"""
    assert not hasattr(fd_mcp_client, "GatewayMcpClient")
    import fd_mcp_client.native as native_mod

    assert not hasattr(native_mod, "GatewayMcpClient")


# ------------------------------------------------------------------ unwrap


def test_unwrap_tool_result_forms() -> None:
    """解包契约：structuredContent 优先、content 文本 JSON 兜底、信封语义。"""
    assert unwrap_tool_result({"result": {"structuredContent": {"rows": 2}}}) == {"rows": 2}
    assert unwrap_tool_result(
        {
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"success": True, "rows": 3})}
                ],
                "isError": False,
            }
        }
    ) == {"rows": 3}  # success 信封剥掉
    assert unwrap_tool_result({"rows": 1}) == {"rows": 1}
    with pytest.raises(McpCallError, match="缺少结果内容"):
        unwrap_tool_result({"result": {"content": []}})
    with pytest.raises(McpCallError, match="结果非 JSON"):
        unwrap_tool_result({"result": {"content": [{"type": "text", "text": "nope"}]}})
    with pytest.raises(McpCallError, match="不合契约"):
        unwrap_tool_result([1, 2])


# ------------------------------------------------------------------ native


class FakeNativeServer:
    """有状态 native 服务端假实现：记录请求序列，按 MCP streamable-http 应答。"""

    def __init__(self, *, session_id: str = "sess-abc123", sse: bool = False) -> None:
        self.requests: list[dict] = []
        self.session_id = session_id
        self.sse = sse  # tools/call 以 SSE 应答（其余 JSON）

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":  # 会话释放（无体）
            self.requests.append(
                {"method": "DELETE", "headers": dict(request.headers), "body": {}}
            )
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body.get("method")
        self.requests.append(
            {"method": method, "headers": dict(request.headers), "body": body}
        )
        rpc_id = body.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": self.session_id},
                json={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            payload = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"structuredContent": {"ok": True, "rows": 3}},
            }
            if self.sse:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    text=f"event: message\ndata: {json.dumps(payload)}\n\n",
                )
            return httpx.Response(200, json=payload)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "tools": [
                            {
                                "name": "customer_dataset_overview",
                                "description": "概览",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        return httpx.Response(404, text="unknown")


def _native(server: FakeNativeServer, endpoint: str = "http://cell-1") -> NativeMcpClient:
    return NativeMcpClient(
        endpoint,
        credential="platform-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(server.handler)),
    )


def test_native_handshake_sequence_then_call_with_session_header() -> None:
    """握手序列钉住：initialize → notifications/initialized（带会话头）→
    tools/call（带会话头）；端点自动补 /mcp；结果经统一解包。"""
    server = FakeNativeServer()
    client = _native(server)  # 故意不给 /mcp 后缀
    try:
        result = client.call("customer_dataset_overview", {"dataset_key": "d1"})
        assert result == {"ok": True, "rows": 3}
        assert [r["method"] for r in server.requests] == [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]
        init = server.requests[0]
        assert init["body"]["jsonrpc"] == "2.0"
        assert init["body"]["params"]["protocolVersion"] == "2025-06-18"
        assert init["body"]["params"]["clientInfo"]["name"] == "fd-wire-api"
        # 端点归一：/mcp 自动补齐
        assert client.endpoint == "http://cell-1/mcp"
        assert str(server.requests[0].get("headers", {}))  # headers 记录存在
        # initialized 通知与会话头
        note = server.requests[1]
        assert note["headers"].get("mcp-session-id") == "sess-abc123"
        assert "id" not in note["body"]  # 通知无 id
        # tools/call 带会话头 + 工具名进 JSON-RPC params
        call = server.requests[2]
        assert call["headers"].get("mcp-session-id") == "sess-abc123"
        assert call["body"]["params"] == {
            "name": "customer_dataset_overview",
            "arguments": {"dataset_key": "d1"},
        }
        assert client.session_id == "sess-abc123"
    finally:
        client.close()


def test_native_lazy_handshake_and_session_reuse() -> None:
    """惰性握手：构造不发包；多次调用只握手一次（会话复用）。"""
    server = FakeNativeServer()
    client = _native(server, endpoint="http://cell-1/mcp")
    assert server.requests == []  # 构造零请求
    client.call("t1", {})
    client.call("t2", {})
    client.list_tools()
    inits = [r for r in server.requests if r["method"] == "initialize"]
    assert len(inits) == 1
    assert client.session_id == "sess-abc123"
    client.close()


def test_native_list_tools() -> None:
    server = FakeNativeServer()
    client = _native(server)
    try:
        tools = client.list_tools()
        assert tools == [
            {
                "name": "customer_dataset_overview",
                "description": "概览",
                "inputSchema": {"type": "object"},
            }
        ]
        assert server.requests[-1]["method"] == "tools/list"
        assert server.requests[-1]["headers"].get("mcp-session-id") == "sess-abc123"
    finally:
        client.close()


def test_native_accept_header_pinned() -> None:
    """请求头契约：``Accept: application/json, text/event-stream``（既有一致）。"""
    server = FakeNativeServer()
    client = _native(server)
    try:
        client.call("t", {})
    finally:
        client.close()
    for record in server.requests:
        if record["method"] == "DELETE":  # 会话释放请求无 JSON-RPC 头契约
            continue
        assert record["headers"].get("accept") == "application/json, text/event-stream"
        assert record["headers"].get("authorization") == "Bearer platform-secret"


def test_native_sse_response_decoded() -> None:
    """streamable-http SSE 应答（data: 行）解出同一 JSON-RPC 消息。"""
    server = FakeNativeServer(sse=True)
    client = _native(server)
    try:
        assert client.call("t", {}) == {"ok": True, "rows": 3}
    finally:
        client.close()


def test_native_protocol_version_fallback() -> None:
    """首档协议版本被拒（400 + protocol 字样）→ 自动试下一档。"""
    server = FakeNativeServer()
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            version = body["params"]["protocolVersion"]
            attempts.append(version)
            if version == "2025-06-18":
                return httpx.Response(400, text="unsupported protocol version")
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-fb"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"structuredContent": {"ok": True}},
            },
        )

    client = NativeMcpClient(
        "http://cell-1/mcp",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        client.call("t", {})
        assert attempts == ["2025-06-18", "2025-03-26"]
        assert client.session_id == "sess-fb"
    finally:
        client.close()


def test_native_transport_errors() -> None:
    """握手不可达 / 非 2xx / 缺会话头 → McpTransportError。"""
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    def _client(handler) -> NativeMcpClient:
        return NativeMcpClient(
            "http://cell-1/mcp",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    with pytest.raises(McpTransportError, match="MCP 端点不可达"):
        _client(unreachable).call("t", {})
    with pytest.raises(McpTransportError, match="initialize 返回 503"):
        _client(lambda r: httpx.Response(503, text="down")).call("t", {})
    with pytest.raises(McpTransportError, match="Mcp-Session-Id"):
        _client(
            lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        ).call("t", {})


def test_native_tool_error_is_mcp_call_error() -> None:
    """tools/call 的 JSON-RPC error 对象 / isError → McpCallError。"""
    def rpc_error(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "s"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": "tool exploded"},
            },
        )

    client = NativeMcpClient(
        "http://cell-1/mcp", http_client=httpx.Client(transport=httpx.MockTransport(rpc_error))
    )
    with pytest.raises(McpCallError, match="tool exploded"):
        client.call("t", {})
    client.close()


def test_native_close_deletes_session_and_is_idempotent() -> None:
    server = FakeNativeServer()
    client = _native(server)
    client.call("t", {})
    client.close()
    client.close()  # 幂等
    deletes = [r for r in server.requests if r["method"] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0]["headers"].get("mcp-session-id") == "sess-abc123"


# ------------------------------------------------------------------ ingest 经 native 客户端


@pytest.fixture()
def engine_env(monkeypatch):
    """配好 cell 端点映射与平台凭据（env 复用纪律：解析留在调用方）。"""
    monkeypatch.setenv(
        "AI_CELL_ENDPOINT_MAP", "cust-001=http://registry.example/fd-wire-cell-cust-001/mcp"
    )
    monkeypatch.setenv("AI_PLATFORM_CREDENTIAL", "platform-service-credential")
    from fd_wire_api.ai.settings import get_settings as ai_get_settings

    ai_get_settings.cache_clear()
    yield
    ai_get_settings.cache_clear()


def _mcp_engine_server(seen: list[dict]) -> httpx.MockTransport:
    """记录请求序列的 native 引擎假实现（握手+tools/call）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("Authorization"),
                "method": body.get("method"),
                "body": body,
            }
        )
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-engine"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        result_text = json.dumps({"success": True, "ingest_id": "ing-1", "rows": 2})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            },
        )

    return httpx.MockTransport(handler)


def test_engine_client_call_sequence_with_renamed_tools(engine_env) -> None:
    """ingest 走 native 客户端：工具新名（无 daas_ 前缀）+ 标准握手序列 +
    平台凭据 + env 解析；引擎 success 信封剥掉；会话跨调用复用。"""
    seen: list[dict] = []
    client = EngineToolClient(
        "cust-001", http_client=httpx.Client(transport=_mcp_engine_server(seen))
    )
    try:
        begin = client.call(TOOL_INGEST_BEGIN, {"dataset_key": "d1", "columns": []})
        assert TOOL_INGEST_BEGIN == "customer_dataset_ingest_begin"
        assert begin == {"ingest_id": "ing-1", "rows": 2}
        client.call(TOOL_PREVIEW, {"dataset_key": "d1", "limit": 10})
        methods = [c["method"] for c in seen]
        assert methods == [
            "initialize",
            "notifications/initialized",
            "tools/call",
            "tools/call",
        ]
        assert seen[0]["url"] == "http://registry.example/fd-wire-cell-cust-001/mcp"
        assert seen[0]["auth"] == "Bearer platform-service-credential"
        calls = [c for c in seen if c["method"] == "tools/call"]
        assert [c["body"]["params"]["name"] for c in calls] == [
            "customer_dataset_ingest_begin",
            "customer_dataset_preview",
        ]
        assert calls[0]["body"]["params"]["arguments"] == {"dataset_key": "d1", "columns": []}
        assert not any("daas_" in json.dumps(c["body"]) for c in seen)
    finally:
        client.close()


def test_engine_client_endpoint_convention_derivation(engine_env, monkeypatch) -> None:
    """约定派生：未配 map 时按 ``<base>/fd-wire-cell-<cid>/mcp`` 派生端点。"""
    monkeypatch.delenv("AI_CELL_ENDPOINT_MAP", raising=False)
    from fd_wire_api.ai.settings import get_settings as ai_get_settings

    ai_get_settings.cache_clear()
    try:
        seen: list[dict] = []
        client = EngineToolClient(
            "cust-042", http_client=httpx.Client(transport=_mcp_engine_server(seen))
        )
        try:
            client.call(TOOL_PREVIEW, {"dataset_key": "d1", "limit": 5})
            assert seen[0]["url"] == "https://mcp.finddatatech.cloud/fd-wire-cell-cust-042/mcp"
        finally:
            client.close()
    finally:
        ai_get_settings.cache_clear()


def test_engine_client_map_miss_is_unprovisioned(engine_env) -> None:
    """显式部署口径：map 已配且未命中 → EngineUnavailable（未开通）。"""
    with pytest.raises(EngineUnavailable, match="无映射"):
        EngineToolClient("cust-999").call("t", {})


def test_engine_client_handshake_404_405_is_unprovisioned(engine_env, monkeypatch) -> None:
    """派生端点握手期 404/405 → 「cell 未开通」语义（不静默降级）。"""
    monkeypatch.delenv("AI_CELL_ENDPOINT_MAP", raising=False)
    from fd_wire_api.ai.settings import get_settings as ai_get_settings

    ai_get_settings.cache_clear()
    try:
        for status in (404, 405):
            client = EngineToolClient(
                "cust-nada",
                http_client=httpx.Client(
                    transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s, text="no such server"))
                ),
            )
            with pytest.raises(EngineUnavailable, match="cell 未开通"):
                client.call("t", {})
            client.close()
    finally:
        ai_get_settings.cache_clear()


def test_engine_client_error_mapping_unchanged(engine_env) -> None:
    """错误包装不回归：传输层/工具报错 → EngineCallError。"""
    def handshake_then(status: int, body: dict | None) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "s"},
                    json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
                )
            if payload.get("method") == "notifications/initialized":
                return httpx.Response(202)
            if body is not None:
                return httpx.Response(200, json=body)
            return httpx.Response(status, text="boom")

        return httpx.MockTransport(handler)

    def _client(handler) -> EngineToolClient:
        return EngineToolClient(
            "cust-001", http_client=httpx.Client(transport=handler)
        )

    with pytest.raises(EngineCallError, match="返回 500"):
        _client(handshake_then(500, None)).call("t", {})
    with pytest.raises(EngineCallError, match="引擎工具报错: 行数超过上限"):
        _client(
            handshake_then(200, {"result": {"structuredContent": {"success": False, "error": "行数超过上限"}}})
        ).call("t", {})


def test_engine_client_refuses_without_platform_credential(engine_env, monkeypatch) -> None:
    monkeypatch.delenv("AI_PLATFORM_CREDENTIAL", raising=False)
    from fd_wire_api.ai.settings import get_settings as ai_get_settings

    ai_get_settings.cache_clear()
    try:
        client = EngineToolClient(
            "cust-001",
            endpoint="http://cell-1/mcp",
            http_client=httpx.Client(),
        )
        with pytest.raises(EngineUnavailable, match="AI_PLATFORM_CREDENTIAL"):
            client.call("t", {})
    finally:
        ai_get_settings.cache_clear()
