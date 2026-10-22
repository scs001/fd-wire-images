"""萬星 A2A 直连客户端测试（wire-wanxing-direct-dispatch 任务 1）。

httpx.MockTransport 注入（零网络）：覆盖冻结调用形状（端点/头/体）、
回执解析（多 text part 拼接、messageId 回执优先）、失败映射矩阵
（401 unauthorized / 403 forbidden / 网络 unreachable / 非 JSON protocol /
result 缺席 protocol / 其余 >=400 原文）、依赖出口（键未配置 None、
工厂注入/重置），以及红线：**键值绝不入任何异常文案**。
"""

from __future__ import annotations

import json
import os

# 在导入 app 前把默认引擎指向内存库（与 test_demands_workorder 同款纪律；
# 只在导入期间生效，导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import httpx
import pytest

from fd_wire_api.demands.wanxing import (
    WANXING_A2A_BASE_ENV,
    WANXING_AGENT_SLUG_ENV,
    WANXING_CALLER_KEY_ENV,
    HttpWanxingClient,
    WanxingInvokeError,
    get_wanxing_client,
    reset_wanxing_client_factory,
    set_wanxing_client_factory,
)

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]

BASE = "https://wanxing.example"
SLUG = "packs-test-spider-heal"
#: 假键（红线断言的对象——任何真实凭据绝不入测试）。
FAKE_KEY = "sk-fake-wanxing-test-key"

ENDPOINT = f"{BASE}/api/wanxing/v1/a2a/{SLUG}"


@pytest.fixture(autouse=True)
def _clean_factory_and_env(monkeypatch):
    """每用例从干净出口出发：工厂清零、wanxing env 全清（防本机泄漏）。"""
    reset_wanxing_client_factory()
    for name in (
        WANXING_CALLER_KEY_ENV,
        WANXING_A2A_BASE_ENV,
        WANXING_AGENT_SLUG_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    reset_wanxing_client_factory()


def make_client(handler, **overrides) -> HttpWanxingClient:
    """MockTransport 注入的门面（测试自持有底层 client 的生命周期）。"""
    return HttpWanxingClient(
        base_url=BASE,
        caller_key=FAKE_KEY,
        agent_slug=SLUG,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **overrides,
    )


def ok_response(*texts: str, message_id: str | None = "recv-1") -> httpx.Response:
    """成功回执：result.parts 里每个 text 一段。"""
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                **({"messageId": message_id} if message_id else {}),
                "parts": [{"kind": "text", "text": t} for t in texts],
            },
        },
    )


# ---------------------------------------------------------------- 成功路径


def test_send_message_request_shape_and_reply_parsing():
    """冻结调用形状（端点/三头/JSON-RPC 体）+ 回执解析（多 text part 拼接、
    messageId 回执优先）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["idem"] = request.headers.get("Idempotency-Key")
        captured["ctype"] = request.headers.get("Content-Type")
        captured["body"] = json.loads(request.content)
        return ok_response("收到，", "开始处理")

    client = make_client(handler)
    try:
        receipt = client.send_message(
            "工单 YAML 全文", idempotency_key="wire-DE-X-abc123", rpc_id="rpc-1"
        )
    finally:
        client.close()

    assert captured["url"] == ENDPOINT
    assert captured["auth"] == f"Bearer {FAKE_KEY}"
    assert captured["idem"] == "wire-DE-X-abc123"
    assert captured["ctype"] == "application/json"
    body = captured["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "rpc-1"
    assert body["method"] == "message/send"
    message = body["params"]["message"]
    assert message["role"] == "user"
    assert message["kind"] == "message"
    assert message["messageId"]  # 请求侧 uuid4 恒在
    assert message["parts"] == [{"kind": "text", "text": "工单 YAML 全文"}]

    assert receipt == {
        "http_status": 200,
        "message_id": "recv-1",  # 回执带回 messageId → 优先回执
        "reply_text": "收到，开始处理",  # 多 text part 按序拼接
    }


def test_send_message_defaults_rpc_id_and_falls_back_to_request_uuid():
    """rpc_id 缺省自动生成 uuid；回执不带 messageId → 用请求侧 uuid。"""

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = json.loads(request.content)["id"]
        assert rpc_id  # 缺省也要有可追溯 id
        return ok_response("ok", message_id=None)

    client = make_client(handler)
    try:
        receipt = client.send_message("text", idempotency_key="k-1")
    finally:
        client.close()
    assert receipt["message_id"]  # 回执缺席 → 请求侧 uuid 兜底
    assert receipt["reply_text"] == "ok"


# ---------------------------------------------------------------- 失败映射


def _invoke(handler) -> WanxingInvokeError:
    client = make_client(handler)
    try:
        client.send_message("text", idempotency_key="k-1")
    except WanxingInvokeError as exc:
        return exc
    raise AssertionError("应当抛 WanxingInvokeError")


def _assert_no_key_leak(exc: WanxingInvokeError) -> None:
    """红线：键值绝不出现在异常的任何呈现面。"""
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in repr(exc)
    assert all(FAKE_KEY not in str(a) for a in exc.args)


def test_401_maps_unauthorized():
    exc = _invoke(
        lambda req: httpx.Response(
            401, json={"error": {"code": "INVALID_KEY", "message": "bad key"}}
        )
    )
    assert exc.reason == "unauthorized"
    assert exc.status_code == 401
    assert "401" in str(exc) and "INVALID_KEY" in str(exc)
    _assert_no_key_leak(exc)


def test_403_maps_forbidden():
    exc = _invoke(
        lambda req: httpx.Response(
            403, json={"error": {"code": "CALLER_NOT_AUTHORIZED", "message": "no"}}
        )
    )
    assert exc.reason == "forbidden"
    assert exc.status_code == 403
    assert "允许清单" in str(exc)
    _assert_no_key_leak(exc)


def test_network_error_maps_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    exc = _invoke(handler)
    assert exc.reason == "unreachable"
    assert exc.status_code is None
    _assert_no_key_leak(exc)


def test_non_json_maps_protocol():
    exc = _invoke(
        lambda req: httpx.Response(
            200, text="<html>gateway</html>", headers={"Content-Type": "text/html"}
        )
    )
    assert exc.reason == "protocol"
    assert exc.status_code == 200
    _assert_no_key_leak(exc)


def test_result_missing_maps_protocol():
    """JSON-RPC error 回执（result 缺席）= 协议错，绝不当成功。"""
    exc = _invoke(
        lambda req: httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "x", "error": {"code": -32000, "message": "boom"}},
        )
    )
    assert exc.reason == "protocol"
    _assert_no_key_leak(exc)


def test_result_parts_malformed_maps_protocol():
    exc = _invoke(
        lambda req: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "x", "result": {"parts": "not-a-list"}}
        )
    )
    assert exc.reason == "protocol"
    # 无任何 text part 同样形态不合
    exc2 = _invoke(
        lambda req: httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "x",
                "result": {"parts": [{"kind": "data", "data": "x"}]},
            },
        )
    )
    assert exc2.reason == "protocol"


def test_other_http_error_keeps_status_and_body():
    exc = _invoke(lambda req: httpx.Response(500, text="upstream down"))
    assert exc.reason is None  # 其余 >=400：按状态码原文，不猜域
    assert exc.status_code == 500
    assert "500" in str(exc) and "upstream down" in str(exc)
    _assert_no_key_leak(exc)


# ---------------------------------------------------------------- 依赖出口


def test_unconfigured_key_returns_none():
    assert get_wanxing_client() is None  # 键缺席 = 未配置（503 口径依据）


def test_blank_key_returns_none(monkeypatch):
    monkeypatch.setenv(WANXING_CALLER_KEY_ENV, "   ")
    assert get_wanxing_client() is None  # 空白键等同未配置


def test_configured_env_builds_http_client(monkeypatch):
    monkeypatch.setenv(WANXING_CALLER_KEY_ENV, FAKE_KEY)
    client = get_wanxing_client()
    assert isinstance(client, HttpWanxingClient)
    client.close()


def test_factory_inject_and_reset(monkeypatch):
    class FakeClient:
        def send_message(self, text, *, idempotency_key, rpc_id=None):
            return {"http_status": 200, "message_id": "m-1", "reply_text": "ok"}

    fake = FakeClient()
    set_wanxing_client_factory(lambda: fake)
    monkeypatch.setenv(WANXING_CALLER_KEY_ENV, FAKE_KEY)
    assert get_wanxing_client() is fake  # 注入优先于 env

    reset_wanxing_client_factory()
    assert get_wanxing_client() is not fake  # 重置回 env 出口
    reset_wanxing_client_factory()  # set(None) 同义
    assert isinstance(get_wanxing_client(), HttpWanxingClient)
