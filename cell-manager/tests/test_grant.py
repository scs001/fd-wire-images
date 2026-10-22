"""cell 授权束单测（add-customer-onboarding-automation 任务 1.1/1.4/1.5/1.6）。

覆盖：身份映射解析与命名派生、grant 三步（scope/条目可见性/预检映射写入，失败即中止）、
JWT-claims 铸造通道（claims 形状/secret 装载/明文不落审计）、退租编排顺序与幂等。
全测试无网：registry 用 Fake，key 通道注入桩。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from cell_manager import (
    CellGrant,
    CellManager,
    CellManagerConfig,
    FakeDriver,
    FakeRegistryClient,
)
from cell_manager.config import CustomerIdentity
from cell_manager.errors import ConfigError, InvalidCustomerIdError
from cell_manager.grant import decommission
from cell_manager.keys import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    MINT_TTL_SECONDS,
    PatchKeyChannel,
    load_secret_key,
    sign_self_signed_jwt,
)
from cell_manager.registry.base import ScopeDoc, ServerEntry

# ---- 测试配置（含身份映射） ----


def grant_config(home: Path, identity: bool = True) -> CellManagerConfig:
    env = {
        "CELL_MANAGER_HOME": str(home),
        "CELL_IMAGE": "fd-wire/cell-daas:test",
        "CELL_GATEWAY_URL": "https://wire.example.test/mcp",
        "CELL_REGISTRY_URL": "https://registry.example.test",
        "CELL_DRIVER": "fake",
        "CELL_REGISTRY_MODE": "fake",
    }
    if identity:
        env["CELL_CUSTOMER_IDENTITY_MAP"] = json.dumps(
            {
                "acme": {"username": "acme-user", "sub2api_user_id": "701"},
                "globex": {"username": "globex-user", "sub2api_user_id": "702"},
            }
        )
    return CellManagerConfig.from_env(env=env)


@pytest.fixture
def config(tmp_path: Path) -> CellManagerConfig:
    return grant_config(tmp_path / "cell-home")


@pytest.fixture
def registry() -> FakeRegistryClient:
    return FakeRegistryClient()


@pytest.fixture
def grant_face(config, registry) -> CellGrant:
    return CellGrant(registry=registry, config=config)


class StubKeyChannel:
    """PatchKeyChannel 桩：记录 mint 调用形状，回放可控结果。"""

    def __init__(self) -> None:
        self.mint_calls: list[dict] = []
        self.revoke_calls: list[dict] = []
        self.mint_result = {"key_id": "k-001", "key": "wgk-PLAINTEXT"}
        self.revoke_result = "revoked"

    def mint(self, *, sub, groups, name):
        self.mint_calls.append({"sub": sub, "groups": list(groups), "name": name})
        return dict(self.mint_result)

    def revoke(self, *, sub, groups, key_id):
        self.revoke_calls.append({"sub": sub, "groups": list(groups), "key_id": key_id})
        return self.revoke_result


# ---- 1.1 身份映射与命名派生 ----


def test_identity_map_parse_and_derivations(tmp_path):
    cfg = grant_config(tmp_path)
    assert cfg.customer_identity_map["acme"] == CustomerIdentity(
        username="acme-user", sub2api_user_id="701"
    )
    # 授权束命名单一来源
    assert cfg.cell_group("acme") == "wire-cust-acme"
    assert cfg.scope_name("acme") == "mcp-wire-cell-acme"
    assert cfg.customer_key_groups("acme") == ["wire-customers", "wire-cust-acme"]


@pytest.mark.parametrize(
    "raw",
    [
        "[]",  # 非对象
        '{"acme": "str"}',  # 值非对象
        '{"acme": {"username": "u"}}',  # 缺 sub2api_user_id
        '{"acme": {"sub2api_user_id": "7"}}',  # 缺 username
        '{"acme": {"username": "", "sub2api_user_id": "7"}}',  # 空 username
        '{"bad/id": {"username": "u", "sub2api_user_id": "7"}}',  # 非法 customer_id
        "{not json",
    ],
)
def test_identity_map_bad_shapes_fail_fast(tmp_path, raw):
    with pytest.raises((ConfigError, InvalidCustomerIdError)):
        CellManagerConfig.from_env(
            env={
                "CELL_MANAGER_HOME": str(tmp_path),
                "CELL_CUSTOMER_IDENTITY_MAP": raw,
                "CELL_DRIVER": "fake",
                "CELL_REGISTRY_MODE": "fake",
            }
        )


# ---- 1.5/1.6 grant 三步 ----


def test_grant_upserts_scope_and_reregisters_with_visibility(config, registry, grant_face):
    detail = grant_face.grant("acme")

    scope = registry.scope("mcp-wire-cell-acme")
    assert scope is not None
    assert scope.server == "fd-wire-cell-acme"
    assert scope.group == "wire-cust-acme"
    entry = registry.entry("fd-wire-cell-acme")
    assert entry is not None
    assert entry.visibility == "group-restricted"
    assert entry.allowed_groups == ("wire-cust-acme",)
    # 预检映射：写入正道（开户漏配即 402 不可用——失败必须中止，不静默跳过）
    assert detail["preflight_map"]["status"] == "written"
    assert detail["preflight_map"]["username"] == "acme-user"
    assert ("put_caller_map", "acme-user") in registry.calls
    # 幂等重跑无副作用差异
    grant_face.grant("acme")
    assert registry.calls.count(("upsert_scope", "mcp-wire-cell-acme")) == 2  # 宽幂等


def test_grant_without_identity_fails_fast(config, registry, grant_face):
    config = CellManagerConfig.from_env(
        env={  # 无身份映射
            "CELL_MANAGER_HOME": str(config.home),
            "CELL_DRIVER": "fake",
            "CELL_REGISTRY_MODE": "fake",
        }
    )
    face = CellGrant(registry=registry, config=config)
    with pytest.raises(ConfigError, match="CELL_CUSTOMER_IDENTITY_MAP"):
        face.grant("acme")


# ---- 1.4 铸 key：明文纪律 + 簿记 ----


def test_mint_customer_key_records_id_and_never_logs_plaintext(config, registry, grant_face):
    stub = StubKeyChannel()
    grant_face._key_channel = stub  # noqa: SLF001 —— 注入桩（生产由 secret 文件构造）

    result = grant_face.mint_customer_key("acme")

    # 铸造形状：sub=客户 username、组快照=共享组+合成组、名字带 customer_id
    assert stub.mint_calls == [
        {
            "sub": "acme-user",
            "groups": ["wire-customers", "wire-cust-acme"],
            "name": "acme-customer-track",
        }
    ]
    # 返回值带明文（仅一次交付）；审计行只带 key_id
    assert result["key_id"] == "k-001"
    assert result["key"] == "wgk-PLAINTEXT"
    audit_text = config.audit_log.read_text(encoding="utf-8")
    assert "k-001" in audit_text
    assert "wgk-PLAINTEXT" not in audit_text


def test_mint_customer_key_without_secret_file_is_config_error(tmp_path, registry):
    cfg = grant_config(tmp_path)  # 未配 CELL_REGISTRY_SECRET_KEY_FILE
    face = CellGrant(registry=registry, config=cfg)
    with pytest.raises(ConfigError, match="SECRET_KEY_FILE"):
        face.mint_customer_key("acme")


def test_mint_records_key_id_into_cell_state(config, registry, grant_face, tmp_path):
    stub = StubKeyChannel()
    grant_face._key_channel = stub  # noqa: SLF001
    from cell_manager.state import StateStore

    # cell 已 provision → key_id 进簿记（退租吊销依据）
    store = StateStore(config.state_file)
    store.put(
        {
            "customer_id": "acme",
            "state": "active",
            "container_name": "fd-wire-cell-acme",
            "volume_path": str(tmp_path),
            "registry_path": "fd-wire-cell-acme",
            "image": "img",
            "created_at": "t",
            "updated_at": "t",
            "purge_at": None,
        }
    )
    grant_face.mint_customer_key("acme")
    assert store.get("acme")["customer_key_ids"] == ["k-001"]


# ---- 1.4 JWT-claims 通道（零依赖实现） ----


def _jwt_payload(token: str) -> dict:
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_sign_self_signed_jwt_claim_shape():
    token = sign_self_signed_jwt(
        "s3cret", sub="acme-user", groups=["wire-customers", "wire-cust-acme"]
    )
    payload = _jwt_payload(token)
    assert payload["iss"] == JWT_ISSUER == "mcp-auth-server"
    assert payload["aud"] == JWT_AUDIENCE == "mcp-registry"
    assert payload["sub"] == "acme-user"
    assert payload["groups"] == ["wire-customers", "wire-cust-acme"]
    assert payload["token_use"] == "access"
    assert payload["token_kind"] == "user"
    assert payload["exp"] - payload["iat"] == MINT_TTL_SECONDS == 600
    # HS256 签名可验证（同 secret 再算一遍比对）
    import hmac as _hmac
    import hashlib as _hashlib

    head, body, sig = token.split(".")
    expected = base64.urlsafe_b64encode(
        _hmac.new(b"s3cret", f"{head}.{body}".encode(), _hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert sig == expected


def test_load_secret_key_env_format_and_bare_file(tmp_path):
    env_file = tmp_path / "registry.env"
    env_file.write_text('OTHER=1\nSECRET_KEY="abcd-efgh"\n', encoding="utf-8")
    assert load_secret_key(env_file) == "abcd-efgh"

    bare = tmp_path / "secret.txt"
    bare.write_text("  raw-secret\n", encoding="utf-8")
    assert load_secret_key(bare) == "raw-secret"

    junk = tmp_path / "multi.txt"
    junk.write_text("line1\nline2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_secret_key(junk)

    with pytest.raises(ConfigError):
        load_secret_key(tmp_path / "missing")


def test_patch_key_channel_mint_parses_key_and_id(monkeypatch):
    """mint：Bearer 是现签 JWT（claims=入参），响应对 {key, info:{key_id}}；缺字段拒绝。"""
    import urllib.request

    from test_registry_http import FakeResponse, UrlopenSpy

    spy = UrlopenSpy(FakeResponse(201, b'{"key": "wgk-x", "info": {"key_id": "k-9"}}'))
    monkeypatch.setattr(urllib.request, "urlopen", spy)

    channel = PatchKeyChannel("https://registry.example.test", "s3cret")
    minted = channel.mint(sub="acme-user", groups=["wire-customers"], name="n")

    assert minted == {"key_id": "k-9", "key": "wgk-x"}
    req = spy.last
    assert req.full_url == "https://registry.example.test/api/patch-keys"
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode()) == {"name": "n"}
    # Bearer JWT 的 claims 即铸造身份与组快照
    payload = _jwt_payload(req.headers["Authorization"].split(" ", 1)[1])
    assert payload["sub"] == "acme-user"
    assert payload["groups"] == ["wire-customers"]

    # 响应缺 key/key_id → 拒绝（宁可失败不猜）
    monkeypatch.setattr(
        urllib.request, "urlopen", UrlopenSpy(FakeResponse(201, b'{"info": {}}'))
    )
    with pytest.raises(Exception, match="missing key"):
        channel.mint(sub="u", groups=[], name="n")


def test_patch_key_channel_revoke_outcome_mapping(monkeypatch):
    """revoke：200→revoked / 404→not_found / 409→already_revoked（幂等语义）。"""
    import urllib.request

    from test_registry_http import FakeResponse, UrlopenSpy, http_error

    channel = PatchKeyChannel("https://registry.example.test", "s3cret")

    monkeypatch.setattr(urllib.request, "urlopen", UrlopenSpy(FakeResponse(200)))
    assert (
        channel.revoke(sub="acme-user", groups=["wire-customers"], key_id="k-1") == "revoked"
    )

    monkeypatch.setattr(urllib.request, "urlopen", UrlopenSpy(http_error(404)))
    assert (
        channel.revoke(sub="acme-user", groups=["wire-customers"], key_id="k-1")
        == "not_found"
    )

    monkeypatch.setattr(urllib.request, "urlopen", UrlopenSpy(http_error(409)))
    assert (
        channel.revoke(sub="acme-user", groups=["wire-customers"], key_id="k-1")
        == "already_revoked"
    )


# ---- 1.6 退租编排：顺序与幂等 ----


def _manager(config, registry, driver) -> CellManager:
    return CellManager(driver=driver, registry=registry, config=config)


def test_decommission_revoke_first_then_teardown_then_retention(config, registry):
    driver = FakeDriver()
    manager = _manager(config, registry, driver)
    grant_face = CellGrant(registry=registry, config=config)
    stub = StubKeyChannel()
    grant_face._key_channel = stub  # noqa: SLF001

    manager.provision("acme")
    grant_face.mint_customer_key("acme")
    registry.calls.clear()

    result = decommission(manager, grant_face, "acme")

    # 顺序（D6）：吊 key → 删 scope → 注销条目；生命周期进 retention
    assert stub.revoke_calls == [
        {
            "sub": "acme-user",
            "groups": ["wire-customers", "wire-cust-acme"],
            "key_id": "k-001",
        }
    ]
    assert registry.calls == [
        ("delete_scope", "mcp-wire-cell-acme"),
        ("delete_caller_map", "acme-user"),
        ("unregister", "fd-wire-cell-acme"),
    ]
    assert result["state"] == "retention"
    assert result["warnings"] == []


def test_decommission_is_idempotent_on_rerun(config, registry):
    driver = FakeDriver()
    manager = _manager(config, registry, driver)
    grant_face = CellGrant(registry=registry, config=config)
    manager.provision("acme")

    first = decommission(manager, grant_face, "acme")
    assert first["state"] == "retention"

    registry.calls.clear()
    second = decommission(manager, grant_face, "acme")  # 重跑不报错
    assert second["idempotent_rerun"] is True
    assert second["state"] == "retention"
    # 授权面再清一遍（宽幂等：已不存在 → 服务端/假体不报错）
    assert registry.calls == [
        ("delete_scope", "mcp-wire-cell-acme"),
        ("delete_caller_map", "acme-user"),
        ("unregister", "fd-wire-cell-acme"),
    ]


def test_decommission_without_keys_tears_down_anyway(config, registry):
    """无已记录 key（或通道未配）→ warning 留痕，授权面照收（fail-safe 而非 fail-block）。"""
    driver = FakeDriver()
    manager = _manager(config, registry, driver)
    grant_face = CellGrant(registry=registry, config=config)

    manager.provision("acme")
    result = decommission(manager, grant_face, "acme")

    assert result["state"] == "retention"
    assert any("no recorded customer keys" in w for w in result["warnings"])
    # grant 从未跑过 → 无 scope 可删（宽幂等 False 是正解）；条目照注销
    assert result["teardown"]["scope_deleted"] is False
    assert result["teardown"]["registry_unregistered"] is True


def test_grant_writes_preflight_map_when_configured(tmp_path, registry):
    cfg_env = {
        "CELL_MANAGER_HOME": str(tmp_path),
        "CELL_DRIVER": "fake",
        "CELL_REGISTRY_MODE": "fake",
        "CELL_CUSTOMER_IDENTITY_MAP": '{"acme": {"username": "acme-user", "sub2api_user_id": "701"}}',
        "CELL_PREFLIGHT_MAP_PATH": "/api/sub2api-caller-map",
    }
    cfg = CellManagerConfig.from_env(env=cfg_env)
    face = CellGrant(registry=registry, config=cfg)
    detail = face.grant("acme")
    assert detail["preflight_map"] == {"status": "written", "username": "acme-user"}
    assert registry.caller_map == {"acme-user": "701"}


def test_mint_channel_prefers_admin_api_over_jwt(monkeypatch, tmp_path, registry):
    """CELL_REGISTRY_TOKEN 在场 → AdminMintChannel（代铸 API），不读 SECRET_KEY 文件。"""
    import cell_manager.grant as grant_mod
    from cell_manager.keys import AdminMintChannel

    cfg_env = {
        "CELL_MANAGER_HOME": str(tmp_path),
        "CELL_DRIVER": "fake",
        "CELL_REGISTRY_MODE": "fake",
        "CELL_CUSTOMER_IDENTITY_MAP": '{"acme": {"username": "acme-user", "sub2api_user_id": "701"}}',
    }
    cfg = CellManagerConfig.from_env(env=cfg_env)
    face = CellGrant(registry=registry, config=cfg)
    monkeypatch.setenv("CELL_REGISTRY_TOKEN", "wgk-admin")
    created = []

    class Spy(AdminMintChannel):
        def __init__(self, *a, **k):
            created.append(True)

    import cell_manager.keys as keys_mod

    monkeypatch.setattr(keys_mod, "AdminMintChannel", Spy)  # 函数内 import 从源模块解析
    channel = face._key_channel_or_fail()
    assert created, "admin API channel not selected"
    assert isinstance(channel, Spy)


def test_grant_aborts_when_caller_map_write_unavailable(home):
    """预检映射写不了 → 开户中止（不静默跳过）。

    隐患闭环：skipped 的后果是新客户从第一次入口调用起被 402
    CALLER_NOT_MAPPED 挡住（生产实弹），即「开户成功却不可用」。
    """
    from cell_manager.registry.base import ServerEntry

    class NoCallerMapRegistry(FakeRegistryClient):
        put_caller_map = None  # 客户端不支持写入（如旧版 Http）

    cfg = grant_config(home)
    grant_face = CellGrant(registry=NoCallerMapRegistry(), config=cfg)
    with pytest.raises(ConfigError, match="put_caller_map"):
        grant_face.grant("acme")


def test_teardown_surfaces_scope_residue_without_aborting(config, registry, grant_face):
    """删 scope 失败（如边缘拦 DELETE→405）：残留如实入 detail，注销照做。

    没有这条防护时，deprovision 会整步失败——容器还在跑、条目还在线、
    映射还在库（2026-10-08 生产实证），退租卡死在半途。
    """
    from cell_manager.errors import RegistryError

    class FailingScopeRegistry(FakeRegistryClient):
        def delete_scope(self, scope_name):  # type: ignore[no-untyped-def]
            raise RegistryError("registry DELETE /api/iam/groups/x -> HTTP 405")

    reg = FailingScopeRegistry()
    reg.register(
        ServerEntry(path="fd-wire-cell-acme", target_url="http://x:8000/mcp")
    )
    face = CellGrant(registry=reg, config=config)
    detail = face.teardown_registry_face("acme")

    assert detail["scope_deleted"] is False
    assert "405" in (detail["scope_error"] or "")
    # 注销照做：不因 scope 失败而放弃条目回收
    assert detail["registry_unregistered"] is True


class TestInternalScopeDeleter:
    """R5：scope 删除通道——internal JWT 形状 + IdP 跳过参数。"""

    def test_delete_posts_internal_jwt_and_skips_idp(self, monkeypatch):
        import base64 as _b64
        import json as _json
        import urllib.error
        import urllib.request

        from cell_manager.keys import INTERNAL_JWT_AUDIENCE, INTERNAL_JWT_ISSUER, InternalScopeDeleter

        captured = {}

        class _Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data.decode()
            captured["auth"] = req.headers.get("Authorization")
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        ok = InternalScopeDeleter("https://registry.example.test", "s3cret").delete(
            "mcp-wire-cell-acme"
        )
        assert ok is True
        assert captured["url"].endswith("/api/internal/delete-group")
        assert "group_name=mcp-wire-cell-acme" in captured["body"]
        assert "delete_from_keycloak=false" in captured["body"]  # 不碰 Logto
        token = captured["auth"].split(" ", 1)[1]
        payload_b64 = token.split(".")[1]
        payload = _json.loads(_b64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        assert payload["iss"] == INTERNAL_JWT_ISSUER == "mcp-auth-server"
        assert payload["aud"] == INTERNAL_JWT_AUDIENCE == "mcp-internal"
        assert payload["purpose"] == "scope-teardown"

    def test_delete_404_is_false(self, monkeypatch):
        import urllib.error
        import urllib.request

        from cell_manager.keys import InternalScopeDeleter

        def _urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 404, "nope", None, None  # type: ignore[arg-type]
            )

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        assert InternalScopeDeleter("https://r.example", "s").delete("x") is False


def test_teardown_removes_caller_map(config, registry, grant_face):
    """退租摘预检映射（与开户写入成对）：残留 = username 永久占位。

    2026-10-09 实证：e2e001 退租后映射残留（teardown/purge 都没删）。
    """
    registry.put_caller_map("acme-user", "701")
    assert registry.caller_map == {"acme-user": "701"}

    detail = grant_face.teardown_registry_face("acme")

    assert detail["caller_map_deleted"] is True
    assert detail["caller_map_error"] is None
    assert registry.caller_map == {}
    assert ("delete_caller_map", "acme-user") in registry.calls


def test_teardown_caller_map_failure_surfaces_not_aborts(config, registry, grant_face):
    """摘映射失败：map_error 如实留痕、其余步骤照做（同 scope 残留语义）。"""
    registry.put_caller_map("acme-user", "701")
    registry.register(ServerEntry(path="fd-wire-cell-acme", target_url="http://x:8000/mcp"))

    def boom(username):  # type: ignore[no-untyped-def]
        raise RuntimeError("caller-map API 503")

    registry.delete_caller_map = boom  # type: ignore[method-assign]
    detail = grant_face.teardown_registry_face("acme")

    assert detail["caller_map_deleted"] is False
    assert "503" in (detail["caller_map_error"] or "")
    assert detail["registry_unregistered"] is True
