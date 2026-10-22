"""客户长命 key 铸造/吊销：registry 自签 JWT 通道（add-customer-onboarding-automation 1.4）。

机制（fd-wire docs/service-credentials.md §2.1，cheap-1 实测配方）：用 registry
的 SECRET_KEY 签一把**短命**自签 JWT（``groups`` claims 即铸出 key 的组快照来源），
Bearer 调 ``POST /api/patch-keys``——明文只在响应中出现一次。信任等级与现状相同：
cell-manager 本就持 admin wgk- 且与 registry 栈同机；根信任材料不出主机
（``CELL_REGISTRY_SECRET_KEY_FILE`` 只在 cheap-1 配置）。

切片二（registry 代铸 API 落地）后本通道退役为备选——届时铸 key 走显式
``{username?, groups?}`` admin API（任务 4.1/4.3）。

吊销现状是 owner 限定（跨用户 404 防探测）——admin 代吊属 registry 侧改动
（任务 4.2）；在那之前以**客户身份自吊**（同通道签 sub=客户 username 的 JWT
调 DELETE）实现退租自动化，语义等同客户自操作。

stdlib 全量（hmac/hashlib/base64），零第三方依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from cell_manager.errors import ConfigError, RegistryError
from cell_manager.registry.http import DEFAULT_TIMEOUT, HttpRegistryClient

#: §2.1 配方的 claims 常量（registry /validate 自签 JWT 的期望形状）
JWT_ISSUER = "mcp-auth-server"
JWT_AUDIENCE = "mcp-registry"
MINT_TTL_SECONDS = 600  # 自签 JWT 只活 10 分钟、只用于一次调用

DEFAULT_PATCH_KEYS_PATH = "/api/patch-keys"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_self_signed_jwt(
    secret: str, *, sub: str, groups: list[str], ttl: int = MINT_TTL_SECONDS
) -> str:
    """签 HS256 自签 JWT（claims 形状=§2.1：iss/aud/sub/groups/token_use/token_kind）。"""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": sub,
        "groups": list(groups),
        "token_use": "access",
        "token_kind": "user",
        "iat": now,
        "exp": now + ttl,
    }
    signing_input = (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def load_secret_key(path: str | Path) -> str:
    """读根信任材料：registry .env（摘 ``SECRET_KEY=`` 行）或裸 secret 文件。

    支持 .env 引号形态（``SECRET_KEY="..."``）；多行文件里没有该行 → ConfigError
    （宁可不工作，不猜错材料）。裸文件=整个文件内容 strip 即 secret。
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"CELL_REGISTRY_SECRET_KEY_FILE not found: {p}")
    text = p.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("SECRET_KEY="):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    whole = text.strip()
    if whole and "\n" not in whole:
        return whole.strip('"').strip("'")
    raise ConfigError(
        f"no SECRET_KEY=<value> line and not a bare secret file: {p}"
    )


class _JwtAuth:
    """HttpRegistryClient 的动态鉴权：每次调用现签短命 JWT（复用错误处理）。"""

    def __init__(self, secret: str, *, sub: str, groups: list[str]) -> None:
        self._secret = secret
        self._sub = sub
        self._groups = list(groups)

    def headers(self) -> dict[str, str]:
        token = sign_self_signed_jwt(self._secret, sub=self._sub, groups=self._groups)
        return {"Authorization": f"Bearer {token}"}


class PatchKeyChannel:
    """铸/吊客户 key 的薄封装（借 HttpRegistryClient 的鉴权/错误/超时管线）。

    每次 mint/revoke 现签短命 JWT；明文 key 只在 mint 返回值里走一趟，
    调用方（grant.py/CLI）负责立即交付（0600 文件/一次性 stdout）。
    """

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        patch_keys_path: str = DEFAULT_PATCH_KEYS_PATH,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url
        self._secret = secret
        self._path = patch_keys_path
        self._timeout = timeout

    def mint(self, *, sub: str, groups: list[str], name: str) -> dict[str, Any]:
        """铸 key；返回 ``{"key_id", "key"}``（key=明文 wgk-，仅此一次）。"""
        client = HttpRegistryClient(
            self._base_url,
            auth=_JwtAuth(self._secret, sub=sub, groups=groups),
            timeout=self._timeout,
        )
        body = client._request(  # noqa: SLF001 —— 复用管线是本类存在的理由
            "POST",
            self._path,
            data={"name": name},
            ok=(200, 201),
            read_json=True,
        )
        doc = body if isinstance(body, dict) else {}
        plaintext = doc.get("key")
        info = doc.get("info") or {}
        key_id = info.get("key_id") or doc.get("key_id")
        if not isinstance(plaintext, str) or not plaintext or not key_id:
            raise RegistryError(
                f"patch-key mint response missing key/key_id: {sorted(doc)}"
            )
        return {"key_id": str(key_id), "key": plaintext}

    def revoke(self, *, sub: str, groups: list[str], key_id: str) -> str:
        """以 owner（sub+其组）身份吊 key；返回 revoked | already_revoked | not_found。

        groups 传该客户的真实组（对齐 §2.1 配方的 claims 形状——/validate 对
        自签 JWT 的空组行为未实证，不冒险）。
        """
        client = HttpRegistryClient(
            self._base_url,
            auth=_JwtAuth(self._secret, sub=sub, groups=groups),
            timeout=self._timeout,
        )
        status = client._request(  # noqa: SLF001 —— 同上
            "DELETE",
            f"{self._path}/{key_id}",
            ok=(200, 404, 409),
        )
        if status == 404:
            return "not_found"
        if status == 409:
            return "already_revoked"
        return "revoked"


class AdminMintChannel:
    """铸/吊客户 key 的正式通道（registry 切片二 4.1/4.2 落地后）：

    以平台 admin 凭据（CELL_REGISTRY_TOKEN 的 wgk-）调 ``POST /api/patch-keys``
    的 ``{username, groups}`` 代铸字段与 DELETE 的 admin 旁路——取代 §2.1
    自签 JWT 通道（根信任材料不再进 cell-manager 内存）。同接口形状，
    与 :class:`PatchKeyChannel` 可互换。
    """

    def __init__(self, base_url: str, admin_token: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        from cell_manager.registry.http import BearerTokenAuth, HttpRegistryClient

        self._path = DEFAULT_PATCH_KEYS_PATH
        self._client = HttpRegistryClient(base_url, auth=BearerTokenAuth(admin_token), timeout=timeout)

    def mint(self, *, sub: str, groups: list[str], name: str) -> dict[str, Any]:
        body = self._client._request(  # noqa: SLF001 —— 复用管线是本类存在的理由
            "POST",
            self._path,
            data={"name": name, "username": sub, "groups": list(groups)},
            ok=(200, 201),
            read_json=True,
        )
        doc = body if isinstance(body, dict) else {}
        plaintext = doc.get("key")
        info = doc.get("info") or {}
        key_id = info.get("key_id") or doc.get("key_id")
        if not isinstance(plaintext, str) or not plaintext or not key_id:
            raise RegistryError(f"patch-key mint response missing key/key_id: {sorted(doc)}")
        return {"key_id": str(key_id), "key": plaintext}

    def revoke(self, *, sub: str, groups: list[str], key_id: str) -> str:
        """admin 旁路吊销（服务端 owner miss + admin → 不限 owner）。"""
        status = self._client._request(  # noqa: SLF001 —— 同上
            "DELETE", f"{self._path}/{key_id}", ok=(200, 404, 409)
        )
        if status == 404:
            return "not_found"
        if status == 409:
            return "already_revoked"
        return "revoked"


#: registry 内部 JWT 常量（registry/auth/internal.py 的同形状复刻——
#: iss/aud 与 60s TTL 必须逐字一致，否则 validate_internal_auth 拒收）
INTERNAL_JWT_ISSUER = "mcp-auth-server"
INTERNAL_JWT_AUDIENCE = "mcp-internal"
INTERNAL_JWT_TTL_SECONDS = 60


def sign_internal_jwt(secret: str, *, subject: str, purpose: str, instance: str = "cell-manager") -> str:
    """签 registry internal JWT（/api/internal/* 端点的 validate_internal_auth 认它）。

    R5（2026-10-09）：退租删授权 scope 走 POST /api/internal/delete-group，
    其 IdP 跳过参数 delete_from_keycloak=false 使合成组轨道完全不碰 Logto。
    """
    import uuid

    # 签名密钥派生（registry/auth/internal.py 同式）：HMAC-SHA256(SECRET_KEY,
    # "mcp-internal-token-v1") —— 裸 SECRET_KEY 签的 user token 在此验不过。
    signing_key = hmac.new(secret.encode(), b"mcp-internal-token-v1", hashlib.sha256).digest()

    now = int(time.time())
    payload = {
        "iss": INTERNAL_JWT_ISSUER,
        "aud": INTERNAL_JWT_AUDIENCE,
        "sub": f"{subject}@{instance}",
        "instance_id": instance,
        "purpose": purpose,
        "token_use": "access",
        # 纵深守卫（Security Finding 1）：必须是显式 internal-service token，
        # user/resource-kind 一律拒
        "token_kind": "internal-service",
        # 单次使用防重放（registry validate_internal_auth fail-closed：无 jti 即拒）
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + INTERNAL_JWT_TTL_SECONDS,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    )
    signature = hmac.new(signing_key, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


class InternalScopeDeleter:
    """R5 的 scope 删除通道：POST /api/internal/delete-group（公网精确 location）。

    与 HttpRegistryClient.delete_scope（/api/management 面，对合成组因 IdP
    同步删除而 502、且旧路径 405）不同，本通道带 delete_from_keycloak=false
    完全跳过 IdP——只删 Mongo scope 文档，正是合成组轨道要的语义。
    鉴权：SECRET_KEY 现签短命 internal JWT（材料不出 cheap-1）。
    """

    def __init__(self, base_url: str, secret: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url
        self._secret = secret
        self._timeout = timeout

    def delete(self, scope_name: str) -> bool:
        """删 scope；404 形态（组不存在）→ False，其余失败抛 RegistryError。"""
        import urllib.parse

        from cell_manager.registry.http import BearerTokenAuth, HttpRegistryClient

        token = sign_internal_jwt(self._secret, subject="cell-manager", purpose="scope-teardown")
        client = HttpRegistryClient(
            self._base_url, auth=BearerTokenAuth(token), timeout=self._timeout
        )
        data = urllib.parse.urlencode(
            {"group_name": scope_name, "delete_from_keycloak": "false"}
        ).encode("utf-8")
        status = client._request(  # noqa: SLF001 —— 复用管线是本类存在的理由
            "POST",
            "/api/internal/delete-group",
            data=data,
            ok=(200, 404),
            content_type="application/x-www-form-urlencoded",
        )
        return status != 404
