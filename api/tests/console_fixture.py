"""console 身份/平台库测试共享设施（非 conftest，避免并行代理争 conftest.py）。

- RSA 测试密钥 + 伪造 JWKS + ``mint_id_token``（照 Logto ID token claims
  形状：iss=<base>/oidc、aud=client_id、sub/roles/email/exp/iat）；
- ``make_console_env``：平台库（admin/billing/console 同一临时文件库）
  初始化 + 档位播种 + logto_sub 客户种子；
- ``FakeRegistryFace`` / ``FakeMintChannel``：开户 ceremony 外部通道假件
  （wire-self-serve-launch 4.3「mock 外部通道」纪律）。

用法（测试内自建 fixture）::

    @pytest.fixture()
    def env(tmp_path, monkeypatch):
        install_fake_jwks(monkeypatch)
        return make_console_env(tmp_path, monkeypatch, customers=[("sub-a", "客户A")])
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import jwt as pyjwt
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ISSUER_BASE = "https://auth.test"
CLIENT_ID = "wire-spa-test"


# ---------------------------------------------------------------------------
# RSA 测试密钥与 token 铸造
# ---------------------------------------------------------------------------

_KEY_CACHE: dict[str, tuple] = {}


def _rsa_keypair(tag: str = "default"):
    """测试 RSA 密钥对（进程内缓存；cryptography 生成，PyJWT 可直接用）。"""
    if tag in _KEY_CACHE:
        return _KEY_CACHE[tag]
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _KEY_CACHE[tag] = (private_key, private_key.public_key())
    return _KEY_CACHE[tag]


class FakeJwksClient:
    """PyJWKClient 假件：get_signing_key_from_jwt → 测试公钥。"""

    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=self._public_key)


def install_fake_jwks(monkeypatch, tag: str = "default") -> None:
    """把 console.auth 的 JWKS 客户端替换为测试公钥假件（签名验签闭环，
    不向 IdP 发网络调用）。"""
    from fd_wire_api.console import auth as console_auth

    _, public_key = _rsa_keypair(tag)
    monkeypatch.setattr(console_auth, "_jwks_client", lambda url: FakeJwksClient(public_key))


def mint_id_token(
    sub: str,
    *,
    tag: str = "default",
    roles: list[str] | None = None,
    email: str | None = None,
    expired: bool = False,
    wrong_aud: bool = False,
    wrong_iss: bool = False,
    issued_in_future: bool = False,
) -> str:
    """按 Logto ID token 形状签一把测试 token（RS256，iss=<base>/oidc）。"""
    private_key, _ = _rsa_keypair(tag)
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER_BASE + ("/wrong" if wrong_iss else "/oidc"),
        "aud": "another-app" if wrong_aud else CLIENT_ID,
        "sub": sub,
        "roles": list(roles or []),
        "iat": now + (3600 if issued_in_future else 0),
        "exp": now - 600 if expired else now + 600,
    }
    if email:
        payload["email"] = email
    return pyjwt.encode(payload, private_key, algorithm="RS256")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# ceremony 外部通道假件
# ---------------------------------------------------------------------------


@dataclass
class FakeMintChannel:
    """铸 key 通道假件：返回确定性明文（测试断言 only），记录调用。"""

    calls: list[dict] = field(default_factory=list)
    fail_first: int = 0  # 前 N 次 mint 抛错（模拟 registry 不可达）

    def mint(self, *, sub: str, groups: list[str], name: str) -> dict[str, Any]:
        self.calls.append({"sub": sub, "groups": list(groups), "name": name})
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("registry mint unavailable (simulated)")
        return {"key_id": f"kid-{len(self.calls)}", "key": f"wgk-test-{len(self.calls)}"}

    @property
    def mint_count(self) -> int:
        return len(self.calls)


@dataclass
class FakeRegistryFace:
    """registry 授权束面假件：记录 caller-map 与 cell 面调用。"""

    caller_map: dict[str, str] = field(default_factory=dict)
    cell_grants: list[dict] = field(default_factory=list)
    fail_caller_map: bool = False

    def write_caller_map(self, username: str, value: str) -> None:
        if self.fail_caller_map:
            raise RuntimeError("registry caller-map unreachable (simulated)")
        self.caller_map[username] = value

    def grant_cell_face(self, *, customer_ref: str, target_url: str, registry_path: str) -> None:
        self.cell_grants.append(
            {"customer_ref": customer_ref, "target_url": target_url, "registry_path": registry_path}
        )


# ---------------------------------------------------------------------------
# 平台库环境（admin/billing/console 同库）
# ---------------------------------------------------------------------------


@dataclass
class ConsoleEnv:
    """平台库测试环境：临时文件库 + 档位播种 + logto_sub 客户种子。"""

    db_url: str
    engine: Any
    subs: dict[str, int] = field(default_factory=dict)  # sub → customer_id

    def headers_for(self, sub: str, *, roles: list[str] | None = None) -> dict[str, str]:
        return bearer(mint_id_token(sub, roles=roles))

    def session(self) -> Session:
        from fd_wire_api.admin.db import get_session

        return get_session()


def make_console_env(
    tmp_path,
    monkeypatch,
    *,
    customers: list[tuple[str, str]] | None = None,
    logto: bool = True,
    gate: str | None = None,
    admin_role: str = "wire-admin",
    db_filename: str = "platform.db",
    internal_token: str | None = "wire-internal-dev",
    plan_code: str = "free",
) -> ConsoleEnv:
    """建平台库环境（admin/billing/console 同一临时文件库，静态池单文件）。

    customers: (sub, name) 列表 → billing_customers 种子（plan_code 档 +
    sub2api_user_id=caller-<n>；缺省 free。datasets 切面按档位分流建集
    tier——走 cell 链路的用例应传 "basic" 等付费档，wire-free-tier-data-gate）。
    logto=False 时清 Logto env（未配置阻塞态用例）；gate 传 "off" 关
    Bearer 角色闸（admin 单闸回归）。
    """
    from fd_wire_api.admin import reset_admin_runtime
    from fd_wire_api.admin.db import AdminBase, get_session
    from fd_wire_api.billing import Base as BillingBase, seed_plans
    from fd_wire_api.billing.models import Customer, Plan
    from fd_wire_api.console import reset_console_runtime
    from fd_wire_api.console.models import ConsoleBase

    db_url = f"sqlite:///{tmp_path/db_filename}"
    monkeypatch.setenv("FD_WIRE_ADMIN_DATABASE_URL", db_url)
    # billing 模块的 init_billing（app lifespan）有独立引擎：不钉住就会落到
    # cwd 的 ./billing.db 滞留文件（跨运行残留 schema），与 admin 库同源隔离
    monkeypatch.setenv("BILLING_DATABASE_URL", db_url)
    monkeypatch.setenv("FD_WIRE_ADMIN_TOKEN", "test-admin-token")
    if gate is not None:
        monkeypatch.setenv("FD_WIRE_ADMIN_BEARER_GATE", gate)
    else:
        monkeypatch.delenv("FD_WIRE_ADMIN_BEARER_GATE", raising=False)
    monkeypatch.setenv("FD_WIRE_ADMIN_ROLE", admin_role)
    if logto:
        monkeypatch.setenv("FD_WIRE_LOGTO_ISSUER", ISSUER_BASE)
        monkeypatch.setenv("FD_WIRE_LOGTO_CLIENT_ID", CLIENT_ID)
    else:
        monkeypatch.delenv("FD_WIRE_LOGTO_ISSUER", raising=False)
        monkeypatch.delenv("FD_WIRE_LOGTO_CLIENT_ID", raising=False)
    if internal_token is not None:
        monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", internal_token)
    # 免得 ceremony 通道摸真网：默认无 admin 凭据（用例按需注入假件）
    monkeypatch.delenv("CELL_REGISTRY_TOKEN", raising=False)

    reset_admin_runtime()
    reset_console_runtime()

    engine = create_engine(
        db_url, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AdminBase.metadata.create_all(engine)
    BillingBase.metadata.create_all(engine)
    ConsoleBase.metadata.create_all(engine)
    with engine.begin() as conn:
        from fd_wire_api.billing import ensure_metering_records_table

        ensure_metering_records_table(conn)

    subs: dict[str, int] = {}
    with get_session() as sess:
        seed_plans(sess)
        sess.commit()
        seed_plan = sess.execute(
            select(Plan).where(Plan.code == plan_code)
        ).scalar_one()
        for n, (sub, name) in enumerate(customers or [], start=1):
            customer = Customer(
                name=name,
                sub2api_user_id=f"caller-{n}",
                logto_sub=sub,
                plan_id=seed_plan.id,
            )
            sess.add(customer)
            sess.flush()
            subs[sub] = customer.id
        sess.commit()

    return ConsoleEnv(db_url=db_url, engine=engine, subs=subs)
