"""HttpRegistryClient 单测：mock urllib，覆盖真实 fork API 契约（零网络）。

契约对齐对象 mcp-gateway-registry（行号见 registry/http.py 模块注释）：
- POST /api/servers/register（form 表单、overwrite=true 幂等、201）
- POST /api/servers/remove（form 表单 path、200 摘除 / 404 不存在）
- GET  /api/servers（只读探测）
- 鉴权：Bearer JWT（无 CSRF）与控制台会话（Cookie + X-CSRF-Token）
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from pathlib import Path
import sys

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from cell_manager import CellManagerConfig
from cell_manager.errors import ConfigError, RegistryError
from cell_manager.registry import HttpRegistryClient, auth_from_env, build_registry_client
from cell_manager.registry.base import ServerEntry
from cell_manager.registry.http import (
    BearerTokenAuth,
    ConsoleSessionAuth,
    DEFAULT_DESCRIPTION,
)

BASE = "https://mcp.example.test"
ENTRY = ServerEntry(
    path="fd-wire-cell-acme",
    target_url="http://fd-wire-cell-acme:8000/mcp",
    display_name="柏讯 cell · acme",
)


class FakeResponse:
    """urlopen 返回值的最小替身：状态码 + 字节体。"""

    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UrlopenSpy:
    """记录 Request 并按脚本回放响应/异常；被 monkeypatch 到 urlopen 位。"""

    def __init__(self, script) -> None:
        #: script: 单个响应/异常，或其列表（每次调用依次取用，重复用最后一个）
        self.script = script if isinstance(script, list) else [script]
        self.requests: list[urllib.request.Request] = []
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def last(self) -> urllib.request.Request:
        return self.requests[-1]

    def form(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qs(self.last.data.decode("utf-8")))


@pytest.fixture
def client() -> HttpRegistryClient:
    return HttpRegistryClient(BASE, auth=BearerTokenAuth("jwt-123"))


def http_error(status: int, payload: dict | None = None) -> urllib.error.HTTPError:
    body = json.dumps(payload or {}).encode("utf-8")
    return urllib.error.HTTPError("url", status, "boom", None, io_bytes(body))  # type: ignore[arg-type]


def io_bytes(body: bytes):
    import io

    return io.BytesIO(body)


# ---- register ----


def test_register_posts_form_then_enables(monkeypatch, client):
    """register = 注册（form/overwrite/201）+ 启用（toggle new_state=true）。

    控制台注册创建的条目默认 disabled（nginx location 整条不生成）——
    开通即可用语义要求显式启用（10-07 生产实证）。"""
    spy = UrlopenSpy([FakeResponse(201, b'{"path": "/fd-wire-cell-acme"}'), FakeResponse(200)])
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    client.register(ENTRY)

    req = spy.requests[0]
    assert req.full_url == f"{BASE}/api/servers/register"
    assert req.get_method() == "POST"
    assert req.headers["Content-type"] == "application/x-www-form-urlencoded"
    assert req.headers["Authorization"] == "Bearer jwt-123"
    form = dict(urllib.parse.parse_qs(req.data.decode("utf-8")))
    assert form["name"] == ["柏讯 cell · acme"]
    assert form["description"] == [DEFAULT_DESCRIPTION]
    assert form["path"] == ["fd-wire-cell-acme"]  # 无前导斜杠：服务端补齐
    assert form["proxy_pass_url"] == ["http://fd-wire-cell-acme:8000/mcp"]
    assert form["overwrite"] == ["true"]  # 幂等键：服务端覆写语义

    tog = spy.requests[1]
    assert tog.full_url == f"{BASE}/api/servers/toggle"
    assert tog.get_method() == "POST"
    tform = dict(urllib.parse.parse_qs(tog.data.decode("utf-8")))
    assert tform == {"path": ["fd-wire-cell-acme"], "new_state": ["true"]}


def test_register_toggle_failure_surfaces(monkeypatch, client):
    """启用步失败不吞：RegistryError 上抛（provision 原子回滚据此触发）。"""
    spy = UrlopenSpy([FakeResponse(201), http_error(500, {"detail": "toggle exploded"})])
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    with pytest.raises(RegistryError, match="HTTP 500"):
        client.register(ENTRY)


def test_register_idempotent_retry_same_payload(monkeypatch, client):
    # overwrite=true 下重复注册是服务端干净覆写：两次均成功、载荷一致；
    # 启用步幂等（true→true 无害）——每次 register 共 2 个请求（注册+启用）
    spy = UrlopenSpy([FakeResponse(201), FakeResponse(200)])
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    client.register(ENTRY)
    client.register(ENTRY)

    assert spy.calls == 4
    first_reg = spy.requests[0]
    second_reg = spy.requests[2]
    assert first_reg.full_url.endswith("/api/servers/register")
    assert second_reg.full_url.endswith("/api/servers/register")
    assert first_reg.data == second_reg.data  # 同形载荷
    first_tog = spy.requests[1]
    second_tog = spy.requests[3]
    assert first_tog.data == second_tog.data
    assert first_tog.full_url.endswith("/api/servers/toggle")


def test_register_name_falls_back_to_path(monkeypatch, client):
    spy = UrlopenSpy([FakeResponse(201), FakeResponse(200)])
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    client.register(ServerEntry(path="/x/y", target_url="http://x:8000/mcp"))

    reg_form = dict(urllib.parse.parse_qs(spy.requests[0].data.decode("utf-8")))
    assert reg_form["name"] == ["x/y"]


def test_register_409_conflict_raises_with_reason(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        UrlopenSpy(http_error(409, {"reason": "already exists with version v1.0.0"})),
    )
    with pytest.raises(RegistryError, match="HTTP 409"):
        client.register(ENTRY)


def test_register_auth_failure_401_raises(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        UrlopenSpy(http_error(401, {"detail": "Authentication required"})),
    )
    with pytest.raises(RegistryError) as excinfo:
        client.register(ENTRY)
    assert "HTTP 401" in str(excinfo.value)
    assert "Authentication required" in str(excinfo.value)


def test_register_no_permission_403_raises(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        UrlopenSpy(http_error(403, {"reason": "no register_service permission"})),
    )
    with pytest.raises(RegistryError, match="register_service"):
        client.register(ENTRY)


# ---- unregister ----


def test_unregister_200_returns_true(monkeypatch, client):
    spy = UrlopenSpy(FakeResponse(200, b'{"message": "removed"}'))
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    assert client.unregister("fd-wire-cell-acme") is True

    assert spy.last.full_url == f"{BASE}/api/servers/remove"
    assert spy.form()["path"] == ["fd-wire-cell-acme"]


def test_unregister_404_returns_false_not_raise(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        UrlopenSpy(http_error(404, {"reason": "No service registered at path"})),
    )
    assert client.unregister("fd-wire-cell-gone") is False


def test_unregister_500_raises(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request, "urlopen", UrlopenSpy(http_error(500, {"reason": "boom"}))
    )
    with pytest.raises(RegistryError, match="HTTP 500"):
        client.unregister("fd-wire-cell-acme")


# ---- 网络层错误（不可达 / 超时）----


def test_unreachable_raises_with_reason(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        UrlopenSpy(urllib.error.URLError("connection refused")),
    )
    with pytest.raises(RegistryError, match="unreachable: connection refused"):
        client.register(ENTRY)


def test_timeout_raises_with_reason(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request, "urlopen", UrlopenSpy(TimeoutError("timed out"))
    )
    with pytest.raises(RegistryError, match="unreachable"):
        client.register(ENTRY)


def test_error_body_plain_text_still_reported(monkeypatch, client):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        UrlopenSpy(
            urllib.error.HTTPError("u", 502, "bad gw", None, io_bytes(b"<html>oops"))  # type: ignore[arg-type]
        ),
    )
    with pytest.raises(RegistryError) as excinfo:
        client.unregister("x")
    assert "HTTP 502" in str(excinfo.value)
    assert "oops" in str(excinfo.value)


# ---- 鉴权策略 ----


def test_bearer_auth_header_applied(monkeypatch):
    spy = UrlopenSpy([FakeResponse(201), FakeResponse(200)])
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    HttpRegistryClient(BASE, auth=BearerTokenAuth("t0k")).register(ENTRY)
    assert spy.last.headers["Authorization"] == "Bearer t0k"


def test_console_session_headers_applied(monkeypatch):
    spy = UrlopenSpy(FakeResponse(200))
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    client = HttpRegistryClient(
        BASE,
        auth=ConsoleSessionAuth("mcp_gateway_session=abc", "csrf-tok"),
    )
    client.unregister("fd-wire-cell-acme")
    assert spy.last.headers["Cookie"] == "mcp_gateway_session=abc"
    assert spy.last.headers["X-csrf-token"] == "csrf-tok"


def test_anonymous_client_sends_no_auth_headers(monkeypatch):
    spy = UrlopenSpy([FakeResponse(201), FakeResponse(200)])
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    HttpRegistryClient(BASE).register(ENTRY)
    assert "Authorization" not in spy.last.headers
    assert "Cookie" not in spy.last.headers


# ---- auth_from_env ----


def test_auth_from_env_token_autoselects_bearer():
    auth = auth_from_env({"CELL_REGISTRY_TOKEN": "jwt"})
    assert isinstance(auth, BearerTokenAuth)


def test_auth_from_env_cookie_and_csrf_autoselects_console_session():
    auth = auth_from_env(
        {"CELL_REGISTRY_COOKIE": "mcp_gateway_session=x", "CELL_REGISTRY_CSRF": "c"}
    )
    assert isinstance(auth, ConsoleSessionAuth)


def test_auth_from_env_explicit_mode_wins_over_token():
    auth = auth_from_env(
        {
            "CELL_REGISTRY_TOKEN": "jwt",
            "CELL_REGISTRY_AUTH": "console-session",
            "CELL_REGISTRY_COOKIE": "mcp_gateway_session=x",
            "CELL_REGISTRY_CSRF": "c",
        }
    )
    assert isinstance(auth, ConsoleSessionAuth)


def test_auth_from_env_none_when_empty():
    assert auth_from_env({}) is None
    assert auth_from_env({"CELL_REGISTRY_AUTH": "none", "CELL_REGISTRY_TOKEN": "t"}) is None


def test_auth_from_env_bearer_without_token_is_config_error():
    with pytest.raises(ConfigError):
        auth_from_env({"CELL_REGISTRY_AUTH": "bearer"})


def test_auth_from_env_console_missing_csrf_is_config_error():
    with pytest.raises(ConfigError):
        auth_from_env({"CELL_REGISTRY_AUTH": "console-session", "CELL_REGISTRY_COOKIE": "c"})


def test_auth_from_env_unknown_mode_is_config_error():
    with pytest.raises(ConfigError):
        auth_from_env({"CELL_REGISTRY_AUTH": "hmac"})


# ---- 只读探测 ----


def test_list_servers_probe_parses_json(monkeypatch, client):
    body = {"servers": [{"path": "/a"}], "total_count": 1}
    spy = UrlopenSpy(FakeResponse(200, json.dumps(body).encode("utf-8")))
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    assert client.list_servers() == body
    assert spy.last.full_url.startswith(f"{BASE}/api/servers?")
    assert "limit=1" in spy.last.full_url
    assert spy.last.get_method() == "GET"


# ---- 工厂：env 接线 ----


def _http_config(home: Path) -> CellManagerConfig:
    return CellManagerConfig.from_env(
        env={
            "CELL_MANAGER_HOME": str(home),
            "CELL_REGISTRY_URL": BASE,
            "CELL_REGISTRY_MODE": "http",
            "CELL_DRIVER": "fake",
        }
    )


def test_factory_wires_bearer_auth_from_env(tmp_path):
    registry = build_registry_client(
        _http_config(tmp_path), env={"CELL_REGISTRY_TOKEN": "jwt-xyz"}
    )
    assert isinstance(registry, HttpRegistryClient)
    assert isinstance(registry._auth, BearerTokenAuth)


def test_factory_wires_console_session_and_custom_paths(tmp_path):
    registry = build_registry_client(
        _http_config(tmp_path),
        env={
            "CELL_REGISTRY_COOKIE": "mcp_gateway_session=s",
            "CELL_REGISTRY_CSRF": "c",
            "CELL_REGISTRY_REGISTER_PATH": "/api/custom/register",
            "CELL_REGISTRY_TIMEOUT": "5.5",
        },
    )
    assert isinstance(registry._auth, ConsoleSessionAuth)
    assert registry._register_path == "/api/custom/register"
    assert registry._timeout == pytest.approx(5.5)


def test_factory_bad_timeout_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        build_registry_client(_http_config(tmp_path), env={"CELL_REGISTRY_TIMEOUT": "abc"})


# ---- cell 授权束（add-customer-onboarding-automation 任务 1.2/1.3） ----


def test_register_includes_group_restricted_visibility_fields(monkeypatch, client):
    """带 allowed_groups 的条目：form 必须带 visibility=group-restricted + 逗号组。"""
    entry = ServerEntry(
        path="fd-wire-cell-acme",
        target_url="http://fd-wire-cell-acme:8000/mcp",
        display_name="柏讯 cell · acme",
        visibility="group-restricted",
        allowed_groups=("wire-cust-acme",),
    )
    spy = UrlopenSpy([FakeResponse(201), FakeResponse(200)])
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    client.register(entry)

    form = dict(urllib.parse.parse_qs(spy.requests[0].data.decode("utf-8")))
    assert form["visibility"] == ["group-restricted"]
    assert form["allowed_groups"] == ["wire-cust-acme"]


def test_register_without_groups_sends_no_visibility_fields(monkeypatch, client):
    """无 allowed_groups（默认 public）：不带 visibility/allowed_groups 字段（服务端默认 public）。"""
    spy = UrlopenSpy([FakeResponse(201), FakeResponse(200)])
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    client.register(ENTRY)

    form = dict(urllib.parse.parse_qs(spy.requests[0].data.decode("utf-8")))
    assert "visibility" not in form
    assert "allowed_groups" not in form


def test_upsert_scope_posts_import_payload_with_create_in_idp_false(monkeypatch, client):
    """upsert_scope = POST /api/servers/groups/import 的全量 upsert；关键：
    create_in_idp 必须 false（默认 true 会去 IdP 建 scope_name 组）。"""
    spy = UrlopenSpy(FakeResponse(200, b'{"ok": true}'))
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    from cell_manager.registry.base import ScopeDoc

    client.upsert_scope(
        ScopeDoc(scope_name="mcp-wire-cell-acme", server="fd-wire-cell-acme", group="wire-cust-acme")
    )

    req = spy.last
    assert req.full_url == f"{BASE}/api/servers/groups/import"
    assert req.get_method() == "POST"
    assert req.headers["Content-type"] == "application/json"
    body = json.loads(req.data.decode("utf-8"))
    assert body == {
        "scope_name": "mcp-wire-cell-acme",
        "scope_type": "server_scope",
        "server_access": [
            {"server": "fd-wire-cell-acme", "methods": ["all"], "tools": ["all"]}
        ],
        "group_mappings": ["wire-cust-acme"],
        "ui_permissions": {"list_service": ["fd-wire-cell-acme"]},
        "create_in_idp": False,
    }


def test_delete_scope_true_on_200_false_on_404(monkeypatch, client):
    """delete_scope 宽幂等：200/204 → True；404（不存在）→ False 不报错。"""
    spy = UrlopenSpy(FakeResponse(200))
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    assert client.delete_scope("mcp-wire-cell-acme") is True
    assert spy.last.full_url == f"{BASE}/api/iam/groups/mcp-wire-cell-acme"
    assert spy.last.get_method() == "DELETE"

    monkeypatch.setattr(urllib.request, "urlopen", UrlopenSpy(http_error(404)))
    assert client.delete_scope("mcp-wire-cell-acme") is False


# ---- caller-map（预检计费映射）----


def test_put_caller_map_puts_json(monkeypatch, client_with_caller_map):
    """开户写入计费映射：PUT + JSON body {username, value}（admin API 契约）。

    不写这条 = 新客户在 deny 策略下从第一次入口调用起被 402 挡住。"""
    spy = UrlopenSpy(FakeResponse(200, b'{"ok": true}'))
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    client_with_caller_map.put_caller_map("acme-user", "701")

    req = spy.last
    assert req.get_method() == "PUT"
    assert req.full_url.endswith("/api/sub2api-caller-map")
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data.decode("utf-8")) == {
        "username": "acme-user",
        "value": "701",
    }


def test_put_caller_map_unavailable_raises(monkeypatch, client):
    """未配置 CELL_PREFLIGHT_MAP_PATH → 显式报错（开户不得静默跳过）。

    cell-manager 侧在 grant 前把「客户端缺 put_caller_map」也转成 ConfigError，
    与此处同款 fail-closed（见 test_grant.py 的中止用例）。
    """
    monkeypatch.setattr(urllib.request, "urlopen", UrlopenSpy(FakeResponse(200)))
    with pytest.raises((ConfigError, RegistryError), match="caller-map write unavailable|not configured"):
        client.put_caller_map("acme-user", "701")


def test_put_caller_map_non_admin_403_surfaces(monkeypatch, client_with_caller_map):
    """非 admin 凭据写映射 → RegistryError（服务端 403 如实上抛）。"""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        UrlopenSpy(http_error(403, {"detail": "Administrator privileges required"})),
    )
    with pytest.raises(RegistryError, match="HTTP 403"):
        client_with_caller_map.put_caller_map("acme-user", "701")


@pytest.fixture
def client_with_caller_map() -> HttpRegistryClient:
    return HttpRegistryClient(
        BASE,
        auth=BearerTokenAuth("jwt-123"),
        caller_map_path="/api/sub2api-caller-map",
    )
