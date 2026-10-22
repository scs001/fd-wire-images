"""控制台面与额度/公开档位端点测试（wire-self-serve-launch 1.3/2.2/3.1/4.2/4.3/4.6/6.1）。

覆盖：
- 4.2 ``GET /api/console/auth/config``：契约逐字（configured/issuer/client_id/
  scopes/organization_required）；未配置如实 configured=false；伪造/过期/
  错 aud / 错 iss ID token 一律 401（本地校验，不向 IdP 发网络调用）
- 4.3 ``POST /api/console/session``：首登自动开户（sub2api 代建 + caller-map
  + 铸 key，免费档跳过 cell）、key_once 仅完成开户那次返回、sub2api 不可达
  502 fail-closed 无半开户态（顶层 failed body）、断点幂等重试不重复铸 key、
  minted 断点续作
- 6.1 ``GET /api/console/usage``：当月已用/含额/余量契约 + 跨客户隔离 +
  X-Customer-Id 占位头退位（一律 401）
- 3.1 ``GET /internal/quota/{caller}``：X-Internal-Token 闸、契约字段、
  enforce_balance=超额单价>0、未知 caller 404、与 admin usage 聚合同源
- 1.3 ``GET /api/v1/plans``：公开三档契约信封 {"plans":[...]}（数字逐字）、
  旧三档与自定义档不出现
- 2.2 ``POST /internal/admin/plans``：自定义档建档（恒 is_public=false）→
  指派 → 额度按新档生效；code 冲突 409；禁用档不可指派 409
- 4.6 升档语义：改档后额度按新档即时生效；付费档 ceremony 可触发 cell 授权面
  （include_cell_face），免费档跳过

测试范式（仓内纪律）：临时文件平台库（admin/billing/console/metering 同库，
console_fixture.make_console_env）+ httpx.MockTransport 假 sub2api +
ceremony 外部通道假件（FakeRegistryFace/FakeMintChannel）；RSA 测试密钥
JWKS 假件闭环验签，绝不打真网。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from fd_wire_api.app import app
from fd_wire_api.billing.models import Customer as BillingCustomer
from fd_wire_api.billing.models import Plan
from fd_wire_api.billing.routes import current_period
from fd_wire_api.billing.sub2api import Sub2ApiClient
from fd_wire_api.console.models import ConsoleOnboarding
from fd_wire_api.console.routes import get_console_sub2api

from console_fixture import (
    CLIENT_ID,
    ISSUER_BASE,
    FakeMintChannel,
    FakeRegistryFace,
    bearer,
    install_fake_jwks,
    make_console_env,
    mint_id_token,
)
from test_billing_common import FakeSub2Api, insert_metering

INTERNAL = {"X-Internal-Token": "wire-internal-dev"}
SUB_A, SUB_B = "sub-cust-a", "sub-cust-b"

#: admin 双闸凭据（gate=on：X-Admin-Token + 含 wire-admin 角色的 Bearer）
ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"} | bearer(
    mint_id_token("sub-platform-admin", roles=["wire-admin"])
)


def _broken_sub2api() -> Sub2ApiClient:
    """sub2api 不可达假件：任何请求抛 ConnectError（fail-closed 断言用）。"""

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sub2api down (simulated)")

    return Sub2ApiClient(
        base_url="http://sub2api.test",
        admin_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(_boom)),
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """平台库 + 假外部通道 + 假 sub2api 的全 fixture（gate 默认 on：
    admin 调用走双闸凭据）。"""
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(
        tmp_path, monkeypatch, customers=[(SUB_A, "客户A"), (SUB_B, "客户B")]
    )
    # 收敛开关（wire-dedicated-postgres）= 同一平台临时库：不压过上面
    # FD_WIRE_ADMIN_DATABASE_URL / BILLING_DATABASE_URL 的同源语义，
    # demands 侧也不落本机文件（X-Customer-Id 退位用例防 import 副作用）。
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", console_env.db_url)

    fake_sub2api = FakeSub2Api()
    sub2api_client = fake_sub2api.client()

    def override_sub2api():
        yield sub2api_client

    registry_face = FakeRegistryFace()
    mint_channel = FakeMintChannel()

    import fd_wire_api.console.onboarding as onboarding

    monkeypatch.setattr(onboarding, "default_registry_face", lambda: registry_face)
    monkeypatch.setattr(onboarding, "default_mint_channel", lambda: mint_channel)

    app.dependency_overrides[get_console_sub2api] = override_sub2api
    client = TestClient(app)
    try:
        yield SimpleNamespace(
            env=console_env,
            client=client,
            sub2api=fake_sub2api,
            face=registry_face,
            mint=mint_channel,
            admin=ADMIN_HEADERS,
            original_sub2api_override=override_sub2api,
        )
    finally:
        app.dependency_overrides.pop(get_console_sub2api, None)
        sub2api_client.close()
        client.close()
        console_env.engine.dispose()


def _seed_current_month_usage(env, caller: str, rows: int) -> None:
    insert_metering(
        env.env.engine,
        customer_ref=caller,
        ts=f"{current_period()}-15T10:00:00+00:00",
        rows=rows,
    )


# ---------------------------------------------------------------------------
# 4.2 登录配置下发 + ID token 本地校验
# ---------------------------------------------------------------------------


def test_auth_config_contract_configured(env):
    """契约逐字：configured/issuer/client_id/scopes/organization_required。"""
    body = env.client.get("/api/console/auth/config").json()
    assert body == {
        "configured": True,
        "issuer": ISSUER_BASE,
        "client_id": CLIENT_ID,
        "scopes": ["openid", "profile", "email", "roles", "offline_access"],
        "organization_required": False,
    }


def test_auth_config_unconfigured_reports_false(tmp_path, monkeypatch):
    """未配置 Logto env：如实 configured=false（前端阻塞态、不发起假登录）；
    受保护端点 503 绝不假登录放行。"""
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(tmp_path, monkeypatch, logto=False)
    client = TestClient(app)
    try:
        assert client.get("/api/console/auth/config").json() == {"configured": False}
        assert (
            client.get(
                "/api/console/usage", headers=bearer(mint_id_token(SUB_A))
            ).status_code
            == 503
        )
    finally:
        client.close()
        console_env.engine.dispose()


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"expired": True},
        {"wrong_aud": True},
        {"wrong_iss": True},
        {"issued_in_future": True},
    ],
)
def test_session_rejects_invalid_id_tokens(env, token_kwargs):
    """伪造/过期/错受众/错签发者/未来签发的 ID token → 401（本地校验）。"""
    token = mint_id_token("sub-attacker", **token_kwargs)
    resp = env.client.post("/api/console/session", json={"id_token": token})
    assert resp.status_code == 401
    # 垃圾字符串同样 401
    resp = env.client.post("/api/console/session", json={"id_token": "garbage"})
    assert resp.status_code == 401
    # 缺 body 字段 → 422
    assert env.client.post("/api/console/session", json={}).status_code == 422


def test_protected_endpoints_reject_without_bearer(env):
    """X-Customer-Id 占位头退位：仅带它（无有效 Bearer）一律 401。"""
    only_header = {"X-Customer-Id": "cust-001"}  # 占位头内容不再被信任
    assert env.client.get("/api/console/usage", headers=only_header).status_code == 401
    assert (
        env.client.post(
            "/api/v1/demands",
            json={"title": "t", "intent": "i"},
            headers=only_header,
        ).status_code
        == 401
    )
    # 有效的 X-Customer-Id 也救不了无效令牌（身份只认 Bearer → logto_sub）
    assert (
        env.client.get(
            "/api/console/usage", headers={"X-Customer-Id": "cust-001"} | bearer("bad")
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# 4.3 首登自动开户（ceremony 全链 mock 外部通道）
# ---------------------------------------------------------------------------


def test_session_first_login_auto_onboarding(env):
    """新 sub 首登 → 200 ready + key_once 一次性交付；sub2api 账户代建、
    caller-map 写入、key 铸造、映射落库全链生效；免费档跳过 cell 配给。"""
    token = mint_id_token("sub-new", email="new@example.com")
    resp = env.client.post("/api/console/session", json={"id_token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 契约逐字：status/customer{id,name,plan_code}/key_once
    assert body["status"] == "ready"
    assert body["customer"]["name"] == "new@example.com"
    assert body["customer"]["plan_code"] == "free"
    assert body["key_once"].startswith("wgk-")
    assert set(body["customer"]) == {"id", "name", "plan_code"}

    # sub2api 代建恰一次；caller-map：username=Logto sub → sub2api_user_id
    assert len(env.sub2api.users) == 1
    caller = next(iter(env.sub2api.users))
    assert env.face.caller_map == {"sub-new": caller}
    # key 组快照：共享组 + 本客户合成组（升档后即期生效）
    ref = f"c{body['customer']['id']}"
    assert env.mint.calls == [
        {
            "sub": "sub-new",
            "groups": ["wire-customers", f"wire-cust-{ref}"],
            "name": f"{ref}-customer-track",
        }
    ]
    # 免费档跳过 cell 配给
    assert env.face.cell_grants == []

    # 映射落库
    with Session(env.env.engine) as sess:
        customer = sess.execute(
            select(BillingCustomer).where(BillingCustomer.logto_sub == "sub-new")
        ).scalar_one()
        assert customer.sub2api_user_id == caller

    # key_once 明文仅此一次：重复会话不再返回、不重复铸、不重复代建
    again = env.client.post("/api/console/session", json={"id_token": token})
    assert again.status_code == 200
    assert again.json()["key_once"] is None
    assert env.mint.mint_count == 1
    assert len(env.sub2api.users) == 1


def test_session_sub2api_down_fail_closed_then_recover(env):
    """sub2api 不可达 → 502 顶层 {"status":"failed","reason"}，无半开户态；
    恢复后重试幂等续作，铸 key 恰一次。"""

    def override_broken():
        yield _broken_sub2api()

    token = mint_id_token("sub-offline")
    app.dependency_overrides[get_console_sub2api] = override_broken
    try:
        resp = env.client.post("/api/console/session", json={"id_token": token})
    finally:
        app.dependency_overrides[get_console_sub2api] = env.original_sub2api_override
    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "failed"  # 顶层（非 detail 包裹）
    assert "reason" in body
    # 无半开户态：无客户行、无 caller-map、无铸 key
    assert env.sub2api.users == {}
    assert env.face.caller_map == {}
    assert env.mint.mint_count == 0

    # 恢复（fixture 原件）→ 重试成功且只铸一次
    ok = env.client.post("/api/console/session", json={"id_token": token})
    assert ok.status_code == 200, ok.text
    assert ok.json()["key_once"].startswith("wgk-")
    assert env.mint.mint_count == 1  # 断点续作只铸一次
    with Session(env.env.engine) as sess:
        row = sess.execute(
            select(BillingCustomer).where(BillingCustomer.logto_sub == "sub-offline")
        ).scalar_one()
        assert row.sub2api_user_id in env.sub2api.users


def test_session_mint_failure_resume_does_not_duplicate_mint(env, monkeypatch):
    """registry 铸 key 不可达 → 502（簿记停在 pending）；换可用通道重试 →
    成功铸 key 恰一次，sub2api 代建不重复。"""
    from typing import Any

    from fd_wire_api.console.onboarding import OnboardingError

    class FlakyMintChannel(FakeMintChannel):
        """前 N 次 mint 抛 OnboardingError（AdminKeyMintChannel 的失败形态，
        路由层据此转 502）。"""

        def mint(self, *, sub: str, groups: list[str], name: str) -> dict[str, Any]:
            self.calls.append({"sub": sub, "groups": list(groups), "name": name})
            if self.fail_first > 0:
                self.fail_first -= 1
                raise OnboardingError("registry mint unavailable (simulated)")
            return {"key_id": f"kid-{len(self.calls)}", "key": f"wgk-test-{len(self.calls)}"}

    flaky = FlakyMintChannel(fail_first=1)
    import fd_wire_api.console.onboarding as onboarding

    monkeypatch.setattr(onboarding, "default_mint_channel", lambda: flaky)
    token = mint_id_token("sub-flaky")
    first = env.client.post("/api/console/session", json={"id_token": token})
    assert first.status_code == 502
    assert first.json()["status"] == "failed"
    assert env.sub2api.users  # 代建已落地（幂等步骤不受铸 key 失败回滚）
    assert env.face.caller_map  # caller-map 已写入（幂等 upsert）

    good = FakeMintChannel()
    import fd_wire_api.console.onboarding as onboarding

    monkeypatch.setattr(onboarding, "default_mint_channel", lambda: good)
    second = env.client.post("/api/console/session", json={"id_token": token})
    assert second.status_code == 200
    assert second.json()["key_once"].startswith("wgk-")
    assert good.mint_count == 1  # 成功铸 key 恰一次
    assert len(env.sub2api.users) == 1  # 代建未重复


def test_session_minted_breakpoint_retry_never_rekeys(env):
    """铸 key 成功但映射失败（簿记 minted+key_id）→ 重试不重复铸 key，
    直接续作映射落库（key_once=None，明文已随失败请求丢失）。"""
    with Session(env.env.engine) as sess:
        free = sess.execute(select(Plan).where(Plan.code == "free")).scalar_one()
        customer = BillingCustomer(
            name="断点客户", sub2api_user_id="caller-minted", plan_id=free.id
        )
        sess.add(customer)
        sess.flush()
        sess.add(
            ConsoleOnboarding(
                logto_sub="sub-minted",
                customer_id=customer.id,
                status="minted",
                key_id="kid-already-minted",
            )
        )
        sess.commit()
        cid = customer.id

    token = mint_id_token("sub-minted")
    resp = env.client.post("/api/console/session", json={"id_token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_once"] is None  # 不重复交付明文
    assert env.mint.mint_count == 0  # 不重复铸 key
    assert body["customer"]["id"] == cid
    with Session(env.env.engine) as sess:
        row = sess.execute(
            select(ConsoleOnboarding).where(ConsoleOnboarding.logto_sub == "sub-minted")
        ).scalar_one()
        assert row.status == "done"
        assert sess.get(BillingCustomer, cid).logto_sub == "sub-minted"


# ---------------------------------------------------------------------------
# 6.1 控制台用量 + 3.1 内部额度查询（同源口径）
# ---------------------------------------------------------------------------


def test_console_usage_contract_and_isolation(env):
    """6.1 契约逐字：period/plan_code/included/used/remaining；身份仅来自
    Bearer（跨客户天然隔离）；未映射 sub 401。"""
    _seed_current_month_usage(env, "caller-1", 300)

    body = env.client.get(
        "/api/console/usage", headers=bearer(mint_id_token(SUB_A))
    ).json()
    assert body == {
        "period": current_period(),
        "plan_code": "free",
        "included_lines": 1000,
        "used_lines": 300,
        "remaining_lines": 700,
    }

    # 客户 B 只见自己的（0），请求面无从指向 A
    body_b = env.client.get(
        "/api/console/usage", headers=bearer(mint_id_token(SUB_B))
    ).json()
    assert body_b["used_lines"] == 0
    assert body_b["remaining_lines"] == 1000
    assert env.env.subs[SUB_A] != env.env.subs[SUB_B]

    # 未映射 sub → 401（先走首登开户）
    assert (
        env.client.get(
            "/api/console/usage", headers=bearer(mint_id_token("sub-ghost"))
        ).status_code
        == 401
    )


def test_internal_quota_contract(env):
    """3.1 契约逐字：X-Internal-Token 闸 + 七字段 + enforce_balance=超额>0；
    未知 caller 404。"""
    _seed_current_month_usage(env, "caller-1", 300)
    resp = env.client.get("/internal/quota/caller-1", headers=INTERNAL)
    assert resp.status_code == 200
    assert resp.json() == {
        "caller": "caller-1",
        "plan_code": "free",
        "included_lines": 1000,
        "used_lines": 300,
        "remaining_lines": 700,
        "period": current_period(),
        "enforce_balance": False,  # 免费档超额单价 0（余额门不适用）
    }
    # 未知 caller → 404（fail-closed：预检据此拒绝无主调用）
    assert (
        env.client.get("/internal/quota/no-such-caller", headers=INTERNAL).status_code
        == 404
    )
    # 内部令牌闸：缺失/错值 401
    assert env.client.get("/internal/quota/caller-1").status_code == 401
    assert (
        env.client.get(
            "/internal/quota/caller-1", headers={"X-Internal-Token": "wrong"}
        ).status_code
        == 401
    )


def test_quota_same_source_as_admin_usage(env):
    """3.1 同源断言：额度查询与 admin usage 聚合（当月）数字一致。"""
    _seed_current_month_usage(env, "caller-1", 300)
    _seed_current_month_usage(env, "caller-1", 120)  # 同月第二笔
    quota = env.client.get("/internal/quota/caller-1", headers=INTERNAL).json()
    usage = env.client.get(
        f"/internal/admin/customers/{env.env.subs[SUB_A]}/usage", headers=env.admin
    ).json()
    current = current_period()
    period_rows = {p["period"]: p for p in usage["periods"]}
    assert quota["used_lines"] == period_rows[current]["rows"] == 420
    assert quota["period"] == current


# ---------------------------------------------------------------------------
# 1.3 公开档位端点 + 2.2 自定义档建档与指派
# ---------------------------------------------------------------------------


def test_public_plans_contract(env):
    """契约逐字信封 {"plans":[...]}：三公开档数字、sort 1/2/3；旧三档与
    自定义档不出现。"""
    resp = env.client.get("/api/v1/plans")
    assert resp.status_code == 200
    assert resp.json() == {
        "plans": [
            {"code": "free", "monthly_price_yuan": 0, "included_lines": 1_000, "sort": 1},
            {"code": "basic", "monthly_price_yuan": 10, "included_lines": 100_000, "sort": 2},
            {
                "code": "standard",
                "monthly_price_yuan": 50,
                "included_lines": 1_000_000,
                "sort": 3,
            },
        ]
    }


def test_custom_plan_create_assign_quota_effect(env):
    """2.2：自定义档建档（恒 is_public=false，不进公开列表）→ 指派给客户 →
    行数门额度按新档生效；code 冲突 409；禁用档不可指派 409。"""
    resp = env.client.post(
        "/internal/admin/plans",
        json={
            "code": "vip-bespoke",
            "name": "定制档",
            "monthly_price_micro": 123_000_000,
            "included_lines": 42_000,
        },
        headers=env.admin,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["code"] == "vip-bespoke"
    assert created["is_public"] is False
    assert created["is_active"] is True

    # 不进公开档位列表
    plans = env.client.get("/api/v1/plans").json()["plans"]
    assert [p["code"] for p in plans] == ["free", "basic", "standard"]

    # 指派给免费档客户 A
    cid = env.env.subs[SUB_A]
    patched = env.client.patch(
        f"/internal/admin/customers/{cid}",
        json={"plan_code": "vip-bespoke"},
        headers=env.admin,
    )
    assert patched.status_code == 200
    assert patched.json()["plan_code"] == "vip-bespoke"

    # 额度按新档即时生效（行数门/用量同源）
    quota = env.client.get("/internal/quota/caller-1", headers=INTERNAL).json()
    assert quota["plan_code"] == "vip-bespoke"
    assert quota["included_lines"] == 42_000

    # code 冲突 → 409
    dup = env.client.post(
        "/internal/admin/plans",
        json={
            "code": "vip-bespoke",
            "name": "再建",
            "monthly_price_micro": 1,
            "included_lines": 1,
        },
        headers=env.admin,
    )
    assert dup.status_code == 409

    # 禁用档（旧三档）不可指派 → 409
    legacy = env.client.patch(
        f"/internal/admin/customers/{cid}",
        json={"plan_code": "enterprise"},
        headers=env.admin,
    )
    assert legacy.status_code == 409


# ---------------------------------------------------------------------------
# 4.6 升档语义（api 侧）
# ---------------------------------------------------------------------------


def test_upgrade_free_to_basic_quota_follows_new_plan(env):
    """免费客户升档 basic：改档后额度按新档即时生效（行数门/结算/物化
    同读 billing_plans，一处数据多处一致）。"""
    _seed_current_month_usage(env, "caller-1", 500)
    before = env.client.get("/internal/quota/caller-1", headers=INTERNAL).json()
    assert before["remaining_lines"] == 500

    cid = env.env.subs[SUB_A]
    assert (
        env.client.patch(
            f"/internal/admin/customers/{cid}",
            json={"plan_code": "basic"},
            headers=env.admin,
        ).status_code
        == 200
    )
    after = env.client.get("/internal/quota/caller-1", headers=INTERNAL).json()
    assert after["plan_code"] == "basic"
    assert after["included_lines"] == 100_000
    assert after["used_lines"] == 500
    assert after["remaining_lines"] == 99_500

    # 控制台用量同源一致
    usage = env.client.get(
        "/api/console/usage", headers=bearer(mint_id_token(SUB_A))
    ).json()
    assert usage["plan_code"] == "basic"
    assert usage["remaining_lines"] == 99_500


def test_paid_plan_ceremony_can_trigger_cell_face(tmp_path, monkeypatch):
    """4.6「免费→付费 cell 配给可触发」：升档通道（include_cell_face=True）
    走 registry cell 授权面（scope 文档+条目注册+启用，命名与 cell-manager
    逐字一致）；映射落库与铸 key 照常完成。"""
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(tmp_path, monkeypatch)
    face = FakeRegistryFace()
    channel = FakeMintChannel()
    fake_sub2api = FakeSub2Api()
    sub2api = fake_sub2api.client()

    from fd_wire_api.console.onboarding import onboard_console_customer

    try:
        with Session(console_env.engine) as sess:
            basic = sess.execute(select(Plan).where(Plan.code == "basic")).scalar_one()
            sess.add(
                BillingCustomer(
                    name="升级客户", sub2api_user_id="caller-up", plan_id=basic.id
                )
            )
            sess.commit()

        with Session(console_env.engine) as sess:
            outcome = onboard_console_customer(
                sess,
                sub2api,
                sub="sub-upgrade",
                display_name="升级客户",
                plan_code="basic",
                mint_channel=channel,
                registry_face=face,
                include_cell_face=True,  # 升档通道（免费档缺省不触发）
            )
            sess.commit()
            ref = f"c{outcome.customer.id}"
            key_once = outcome.key_once
        assert key_once == "wgk-test-1"
        # cell 授权面已配给：条目路径 = fd-wire-cell-<ref>，代理指向客户 cell
        (grant,) = face.cell_grants
        assert grant["customer_ref"] == ref
        assert grant["registry_path"] == f"fd-wire-cell-{ref}"
        assert grant["target_url"] == f"http://fd-wire-cell-{ref}:8000/mcp"
        # 映射落库完成
        with Session(console_env.engine) as sess:
            customer = sess.execute(
                select(BillingCustomer).where(BillingCustomer.logto_sub == "sub-upgrade")
            ).scalar_one()
            assert customer.name == "升级客户"
    finally:
        sub2api.close()
        console_env.engine.dispose()


def test_admin_mint_channel_posts_json_with_groups_list(monkeypatch) -> None:
    """HTTP 形状钉死：registry patch-keys 真契约=JSON 体 + groups 列表
    （form/逗号串形态曾在生产 422——cell_manager AdminMintChannel 同径）。"""
    from fd_wire_api.console.onboarding import AdminKeyMintChannel

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"key": "wgk-shape", "info": {"key_id": "kid-shape"}}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return _Resp()

    monkeypatch.setattr("fd_wire_api.console.onboarding.httpx.post", fake_post)
    out = AdminKeyMintChannel("http://reg.test/", "tok").mint(
        sub="s1", groups=["wire-customers", "wire-cust-c1"], name="n"
    )
    assert captured["url"] == "http://reg.test/api/patch-keys"
    assert captured["kw"]["json"] == {
        "name": "n", "username": "s1", "groups": ["wire-customers", "wire-cust-c1"]
    }
    assert captured["kw"]["headers"]["Authorization"] == "Bearer tok"
    assert out == {"key_id": "kid-shape", "key": "wgk-shape"}


def test_quota_suspended_customer_zeroed(env) -> None:
    """self-serve-onboarding「停用即断」额度侧：suspended 客户额度归零。"""
    from fd_wire_api.billing.models import Customer

    with Session(env.env.engine) as s:
        cust = s.query(BillingCustomer).filter_by(sub2api_user_id="caller-1").one()
        cust.status = "suspended"
        s.commit()
    resp = env.client.get("/internal/quota/caller-1", headers=INTERNAL)
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["included_lines"] == 0 and doc["remaining_lines"] == 0
    assert doc["enforce_balance"] is False


def test_patch_suspend_revokes_onboarded_keys(env, monkeypatch) -> None:
    """「停用即断」key 侧：PATCH status=suspended 触发在册 key 吊销
    （best-effort：吊销失败不阻塞停用）。"""
    import fd_wire_api.admin.routes as admin_routes
    from fd_wire_api.console.models import ConsoleOnboarding

    with Session(env.env.engine) as s:
        cust = s.query(BillingCustomer).filter_by(sub2api_user_id="caller-1").one()
        s.add(
            ConsoleOnboarding(
                logto_sub=SUB_A, customer_id=cust.id, status="done", key_id="kid-x"
            )
        )
        s.commit()
        cid = cust.id

    revoked: list[str] = []

    class FakeChannel:
        def revoke(self, key_id):
            revoked.append(key_id)
            return "revoked"

    monkeypatch.setattr(
        admin_routes, "_revoke_customer_keys_best_effort", lambda db, cid_: (
            revoked.append("hook")
        ),
    )
    # 直接驱动内部函数级语义：经 PATCH 端点（双闸凭据）
    resp = env.client.patch(
        f"/internal/admin/customers/{cid}",
        json={"status": "suspended"},
        headers=env.admin,
    )
    assert resp.status_code == 200 and resp.json()["status"] == "suspended"
    assert revoked == ["hook"]  # 吊销钩子被触发（真实吊销由 default_mint_channel）
