"""HttpRegistryClient 的 MCP streamable-http 全 mock 测试（list_registry_entries 通道）。

线格式按 fastmcp 3.4.4 / mcp 1.28.1 实测（2026-10）模拟，全程
httpx.MockTransport，无真实网络：
- initialize → 200 + ``text/event-stream``（``event: message`` / ``data:`` 行）
  + ``Mcp-Session-Id`` 响应头；
- notifications/initialized → 202 无体；
- tools/call ``list_registry_entries`` → 200 + SSE，结果在
  ``structuredContent.result``（fastmcp 对 list 返回值的包装），
  ``content[0].text`` 同载荷。
覆盖：握手→tools/call→解析→映射到 ConceptRecord/IndicatorSnapshot 字段、
limit/offset 翻页、Bearer 凭据、端点归一、错误面、新标识方案
（indicator_id = source_db:native_code）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from fd_wire_api.catalog.client import HttpRegistryClient, normalize_record, utcnow


def entry_row(source_db: str = "world_bank", native_code: str = "WB.COAL", **overrides) -> dict:
    """list_registry_entries 权威行（固定契约的 9 字段）。"""
    row = {
        "source_db": source_db,
        "native_code": native_code,
        "semantic_code": "coal_output",
        "name_zh": f"注册-{native_code}",
        "name_en": f"Registry {native_code}",
        "unit": "万吨",
        "frequency": "annual",
        "domain": "energy",
        "verified": True,
    }
    row.update(overrides)
    return row


def sse(payload: dict) -> httpx.Response:
    """以 fastmcp streamable-http 的 SSE 框架应答。"""
    body = f"event: message\ndata: {json.dumps(payload)}\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


class FakeAuthority:
    """按真实线格式应答的 mock 权威源（记录全部请求供断言）。"""

    def __init__(self, rows: list[dict] | None = None, *, token: str = "svc-token"):
        self.rows = list(rows or [])
        self.expected_token = token
        self.requests: list[httpx.Request] = []
        # 注入失败模式：("rpc_error", msg) / ("tool_is_error", msg) / ("http_401",)
        self.fail_mode: tuple | None = None
        self.reject_first_versions = 0  # 前 N 次 initialize 以 400 版本错拒绝
        self.json_body = False  # True = 以纯 JSON（非 SSE）应答 RPC

    def _rpc_ok(self, rpc_id: int, result: dict) -> httpx.Response:
        payload = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        if self.json_body:
            return httpx.Response(200, json=payload)
        return sse(payload)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)

        # 网关/服务端 Bearer 校验：凭据不匹配 → 401（与公网入口行为一致）
        auth = request.headers.get("authorization", "")
        if self.expected_token and auth != f"Bearer {self.expected_token}":
            return httpx.Response(401, json={"error": "unauthorized"})

        message = json.loads(request.content)
        method = message.get("method")

        if method == "initialize":
            if self.reject_first_versions > 0:
                self.reject_first_versions -= 1
                return httpx.Response(
                    400, text="Bad Request: Unsupported protocol version"
                )
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "fd-open-data-mcp", "version": "3.4.4"},
            }
            return httpx.Response(
                200,
                headers={
                    "Mcp-Session-Id": "sess-abc-123",
                    "content-type": "text/event-stream",
                },
                text=f'event: message\ndata: {json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})}\n\n',
            )

        if method == "notifications/initialized":
            return httpx.Response(202)

        if method == "tools/call":
            if self.fail_mode and self.fail_mode[0] == "rpc_error":
                return sse(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32602, "message": self.fail_mode[1]},
                    }
                )
            args = message["params"]["arguments"]
            offset, limit = int(args.get("offset", 0)), int(args.get("limit", 500))
            page = self.rows[offset : offset + limit]
            if self.fail_mode and self.fail_mode[0] == "tool_is_error":
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": self.fail_mode[1]}],
                }
            else:
                result = {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(page)}],
                    "structuredContent": {"result": page},
                }
            return self._rpc_ok(message["id"], result)

        return httpx.Response(404, text=f"unexpected method {method}")

    def calls(self, method: str) -> list[httpx.Request]:
        out = []
        for req in self.requests:
            if req.method != "POST":
                continue
            body = json.loads(req.content)
            if body.get("method") == method:
                out.append(req)
        return out


def make_client(authority: FakeAuthority, **kwargs) -> HttpRegistryClient:
    kwargs.setdefault("token", authority.expected_token)
    kwargs.setdefault("base_url", "https://mcp.example/fd-open-data-mcp")
    return HttpRegistryClient(transport=httpx.MockTransport(authority.handler), **kwargs)


# ── 握手 → tools/call → 解析 → 映射 ────────────────────────────────────────


def test_handshake_then_list_registry_entries_and_field_mapping():
    authority = FakeAuthority(
        rows=[
            entry_row("world_bank", "WB.COAL"),
            entry_row("gta", "GDP_TOTAL", verified=False, unit=None),
        ]
    )
    client = make_client(authority)

    batch = client.fetch_concepts()

    # 握手序列：initialize（无会话头）→ initialized（带会话头）
    init = authority.calls("initialize")
    assert len(init) == 1
    assert init[0].headers.get("mcp-session-id") is None
    assert init[0].headers.get("authorization") == "Bearer svc-token"
    init_params = json.loads(init[0].content)["params"]
    assert init_params["protocolVersion"] == "2025-06-18"
    assert len(authority.calls("notifications/initialized")) == 1
    assert authority.calls("notifications/initialized")[0].headers.get("mcp-session-id") == "sess-abc-123"

    # tools/call：工具名 list_registry_entries + limit/offset 入参 + 会话头；
    # 会话用完 DELETE 关闭
    calls = authority.calls("tools/call")
    assert len(calls) == 1
    assert json.loads(calls[0].content)["params"] == {
        "name": "list_registry_entries",
        "arguments": {"limit": 500, "offset": 0},
    }
    assert calls[0].headers.get("mcp-session-id") == "sess-abc-123"
    assert any(r.method == "DELETE" for r in authority.requests)

    # 映射到 ConceptRecord（IndicatorSnapshot 的同步字段来源）
    assert len(batch.items) == 2
    wb, gta = batch.items
    assert wb.indicator_id == "world_bank:WB.COAL"  # source_db:native_code
    assert wb.source_db == "world_bank"
    assert wb.native_code == "WB.COAL"
    assert wb.code == "coal_output"  # code ← semantic_code（展示属性）
    assert wb.name_zh == "注册-WB.COAL" and wb.name_en == "Registry WB.COAL"
    assert wb.unit == "万吨"
    assert wb.domain == "energy"
    assert wb.frequency == "annual"
    assert wb.status == "verified"
    # unverified → registered（如实两级口径）
    assert gta.indicator_id == "gta:GDP_TOTAL"
    assert gta.status == "registered"
    assert batch.full is True


def test_pagination_advances_offset_until_short_page():
    rows = [entry_row("world_bank", f"WB.C{i:03d}") for i in range(5)]
    authority = FakeAuthority(rows=rows)
    client = make_client(authority, page_size=2)

    batch = client.fetch_concepts()

    calls = authority.calls("tools/call")
    offsets = [json.loads(c.content)["params"]["arguments"]["offset"] for c in calls]
    # 翻页：0,2（满页）→ 4（短页 1 行 < 2 即停，无需第 4 次探底）
    assert offsets == [0, 2, 4]
    assert len(batch.items) == 5
    assert {r.indicator_id for r in batch.items} == {
        f"world_bank:WB.C{i:03d}" for i in range(5)
    }


def test_exact_multiple_pages_stops_on_empty_page():
    rows = [entry_row("gta", f"C{i}") for i in range(4)]
    authority = FakeAuthority(rows=rows)
    client = make_client(authority, page_size=2)

    batch = client.fetch_concepts()

    offsets = [
        json.loads(c.content)["params"]["arguments"]["offset"]
        for c in authority.calls("tools/call")
    ]
    assert offsets == [0, 2, 4]  # 末页恰好排空 → 一次空页确认终止
    assert len(batch.items) == 4


def test_watermark_falls_back_to_fetch_time():
    """登记行不携带 updated_at（现契约）：水位退化为拉取时刻。"""
    authority = FakeAuthority(rows=[entry_row()])
    before = utcnow()
    batch = make_client(authority).fetch_concepts()
    assert batch.next_watermark is not None
    assert batch.next_watermark >= before


# ── 凭据与端点归一 ──────────────────────────────────────────────────────────


def test_missing_token_sends_no_authorization_header():
    authority = FakeAuthority(rows=[entry_row()], token="")
    client = make_client(authority, token="")
    batch = client.fetch_concepts()
    assert len(batch.items) == 1
    assert all(
        "authorization" not in r.headers for r in authority.requests if r.method == "POST"
    )


def test_wrong_token_surfaces_as_http_401():
    authority = FakeAuthority(rows=[entry_row()], token="right-token")
    client = make_client(authority, token="wrong-token")
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        client.fetch_concepts()


def test_endpoint_normalization_appends_mcp_suffix():
    for base, expected in [
        ("https://mcp.finddatatech.cloud/fd-open-data-mcp",
         "https://mcp.finddatatech.cloud/fd-open-data-mcp/mcp"),
        ("https://mcp.finddatatech.cloud/fd-open-data-mcp/",
         "https://mcp.finddatatech.cloud/fd-open-data-mcp/mcp"),
        ("https://mcp.finddatatech.cloud/fd-open-data-mcp/mcp",
         "https://mcp.finddatatech.cloud/fd-open-data-mcp/mcp"),
        ("http://fd-open-data-mcp.mcp.svc:8899",
         "http://fd-open-data-mcp.mcp.svc:8899/mcp"),
    ]:
        authority = FakeAuthority(rows=[entry_row()])
        client = make_client(authority, base_url=base)
        client.fetch_concepts()
        assert authority.requests[0].url == expected


def test_token_defaults_to_settings(monkeypatch):
    from fd_wire_api.catalog import settings as catalog_settings_module

    authority = FakeAuthority(rows=[entry_row()], token="from-settings")
    monkeypatch.setattr(
        catalog_settings_module.settings, "indicator_registry_token", "from-settings"
    )
    client = HttpRegistryClient(
        base_url="https://mcp.example/fd-open-data-mcp",
        transport=httpx.MockTransport(authority.handler),
    )
    batch = client.fetch_concepts()
    assert len(batch.items) == 1


# ── 协议变体与错误面 ────────────────────────────────────────────────────────


def test_protocol_version_fallback_on_400():
    authority = FakeAuthority(rows=[entry_row()])
    authority.reject_first_versions = 1  # 2025-06-18 被拒 → 回落 2025-03-26
    client = make_client(authority)

    batch = client.fetch_concepts()

    inits = authority.calls("initialize")
    versions = [json.loads(r.content)["params"]["protocolVersion"] for r in inits]
    assert versions == ["2025-06-18", "2025-03-26"]
    assert len(batch.items) == 1


def test_plain_json_body_response_path():
    authority = FakeAuthority(rows=[entry_row()])
    authority.json_body = True  # 服务端以纯 JSON（非 SSE）应答
    batch = make_client(authority).fetch_concepts()
    assert len(batch.items) == 1


def test_text_block_payload_path_without_structured_content():
    """structuredContent 缺失时从 content[0].text 解析（与 fastmcp 兼容兜底）。"""
    authority = FakeAuthority(rows=[entry_row()])

    original_handler = authority.handler

    def text_only_handler(request: httpx.Request) -> httpx.Response:
        response = original_handler(request)
        if request.method == "POST":
            message = json.loads(request.content)
            if message.get("method") == "tools/call":
                payload = json.loads(response.text.split("data: ", 1)[1])
                payload["result"].pop("structuredContent")
                return sse(payload)
        return response

    client = HttpRegistryClient(
        base_url="https://mcp.example/fd-open-data-mcp",
        token="svc-token",
        transport=httpx.MockTransport(text_only_handler),
    )
    batch = client.fetch_concepts()
    assert len(batch.items) == 1
    assert batch.items[0].code == "coal_output"


def test_rpc_error_payload_raises():
    authority = FakeAuthority(rows=[entry_row()])
    authority.fail_mode = ("rpc_error", "unknown tool")
    with pytest.raises(RuntimeError, match="RPC error -32602: unknown tool"):
        make_client(authority).fetch_concepts()


def test_tool_is_error_result_raises():
    authority = FakeAuthority(rows=[entry_row()])
    authority.fail_mode = ("tool_is_error", "database unreadable")
    with pytest.raises(RuntimeError, match="database unreadable"):
        make_client(authority).fetch_concepts()


# ── 行映射单元（normalize_record：新标识方案） ──────────────────────────────


def test_row_without_source_db_or_native_code_raises():
    with pytest.raises(ValueError, match="without usable source_db/native_code"):
        normalize_record(entry_row(source_db=None))
    with pytest.raises(ValueError, match="without usable source_db/native_code"):
        normalize_record(entry_row(native_code=""))
    with pytest.raises(ValueError, match="without usable source_db/native_code"):
        normalize_record({"semantic_code": "orphan"})


def test_cross_source_same_semantic_code_distinct_ids():
    """跨源等价条目（语义码相同）标识不同——语义码不得当主键。"""
    a = normalize_record(entry_row("gta", "GDP", semantic_code="gdp"))
    b = normalize_record(entry_row("world_bank", "WB.GDP", semantic_code="gdp"))
    assert a.indicator_id == "gta:GDP"
    assert b.indicator_id == "world_bank:WB.GDP"
    assert a.indicator_id != b.indicator_id
    assert a.code == b.code == "gdp"  # 语义码只作展示/搜索属性


def test_identity_immune_to_display_field_changes():
    """名称/单位被权威库修正 → indicator_id 不变（原地更新语义）。"""
    before = normalize_record(entry_row("gta", "CPI", name_zh="旧名", unit="指数点"))
    after = normalize_record(entry_row("gta", "CPI", name_zh="新名", unit="%"))
    assert before.indicator_id == after.indicator_id == "gta:CPI"
    assert after.name_zh == "新名" and after.unit == "%"


def test_long_indicator_id_gets_hash_suffix_without_collision():
    long_native = "N" * 200
    record_a = normalize_record(entry_row("src", long_native))
    record_b = normalize_record(entry_row("src", "X" + long_native))
    assert len(record_a.indicator_id) == 128
    assert len(record_b.indicator_id) == 128
    assert record_a.indicator_id != record_b.indicator_id  # 同前缀不同行不可撞


def test_verified_string_true_tolerated_but_conservative_default():
    """契约 verified 为 bool；字符串 "true" 容错，其余一律保守落 registered。"""
    assert normalize_record(entry_row(verified=True)).status == "verified"
    assert normalize_record(entry_row(verified="true")).status == "verified"
    assert normalize_record(entry_row(verified=False)).status == "registered"
    assert normalize_record(entry_row(verified=None)).status == "registered"
    assert normalize_record(entry_row(verified="yes")).status == "registered"
