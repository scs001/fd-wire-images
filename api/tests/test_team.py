"""团队空间 API 测试（wire-team-spaces 任务 4：邀请全环 + 门禁矩阵）。

覆盖：
- 邀请全环：owner 生成（``wsi-``+24hex、24h）→ 新 sub 携码 session 兑换
  入队（共享邀请方客户、key_once=null、不动 sub2api/不建新客户）→ 码置
  used（再兑拒）→ team_events('member.joined') 留痕；
- 兑换拒绝面：已归属身份（owner/成员）携码 502、码不存在、码过期
  （expires_at 手改）、码已被使用；
- 门禁矩阵：viewer 变更类 403（POST /api/v1/entities）+ 读面 200；editor
  实体变更 201；密钥物面（/api/v1/data-plane-keys 全方法 +
  /api/v1/connectors/pairing-tokens）editor/viewer 403、owner 200/201；
- 团队管理 owner-only：成员 GET /api/console/team 403、生成邀请 403；
  撤 owner 409、未知成员 404、成员自撤允许、成员撤他人 403；
- 撤销即失效：被撤成员下一次请求 401（解析不到归属）；
- 无码首登零回归：不携码的 session 行为照旧（sub2api 代建 + caller-map +
  铸 key，key_once 非 null）。

wire-team-spaces-v2（tasks 5.1-5.3）：
- 5.1 解析层：resolve_member_role 三序全分支（指针→成员行/自有队两形态、
  owner 回退、首队回退取 id 最小成员行、全无 None）、悬空指针同事务自愈
  （删行+回退他队/owner/None）、guesting owner 密钥物面 403/切回恢复、
  成员被撤后指针重指剩余 id 最小成员行或删指针；
- 5.2 兑换与席位：多队解锁（成员/owner 兑新队码、指针不动、首队落指针）、
  同队重复拒（码保活）、生成闸（409 含档位与数字、不产码）、limits_json
  seats 覆盖缺省、兑换闸码保活（腾位后同码可再兑）、免费档升级引导、
  grandfather 超配如实展示且不阻断移除；
- 5.3 切换与角色：teams 清单（自有在前/角色/is_active 唯一真）、switch
  404 与数据面联动（datasets 随活跃队切换）、角色变更四场景
  （提权即生效/非 owner 403/owner 行 409/非本队成员 404 + team_events
  old/new 断言 + 同值幂等不留痕）。

环境：平台库（admin/billing/console 同一临时文件库，console_fixture）+
 entities/datasets 内存库依赖覆写（照 test_entities 先例——门禁矩阵需要
 既有变更端点 POST /api/v1/entities 与密钥物面真路由）；sub2api/ceremony
 外部通道假件（test_billing_common.FakeSub2Api + FakeRegistryFace/
 FakeMintChannel），绝不打真网。
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm.session import Session
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app
from fd_wire_api.billing.models import Customer as BillingCustomer
from fd_wire_api.billing.models import Plan
from fd_wire_api.console.auth import resolve_member_role
from fd_wire_api.console.models import CustomerInvite, CustomerMember, MemberActiveTeam, TeamEvent
from fd_wire_api.console.onboarding import onboard_console_customer  # noqa: F401 —— 覆写锚
from fd_wire_api.console.routes import get_console_sub2api
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.entities.db import EntitiesBase, get_db as get_entities_db
from fd_wire_api.entities.models import Entity

import fd_wire_api.console.onboarding as onboarding
from console_fixture import (
    FakeMintChannel,
    FakeRegistryFace,
    bearer,
    install_fake_jwks,
    make_console_env,
    mint_id_token,
)
from test_billing_common import FakeSub2Api

SUB_OWNER = "sub-team-owner"
SUB_OTHER = "sub-other-owner"
SUB_EDITOR = "sub-team-editor"
SUB_VIEWER = "sub-team-viewer"
SUB_NEW = "sub-team-newcomer"
# v2 用例内自建身份（用例内插客户/成员行，不经首登 ceremony）
SUB_THIRD = "sub-third-owner"  # 用例内第三客户的 owner（仅落库，多数场景不鉴权）
SUB_FREE = "sub-free-owner"  # 免费档客户 owner（免费档引导用例）
SUB_MULTI = "sub-multi-owner"  # 自有队 + 双成员队身份（teams 清单用例）

CUST_NAME = "team-cust"  # Customer.name = 客户面命名空间键（entities/keys）
OTHER_CUST_NAME = "other-cust"

OWNER = bearer(mint_id_token(SUB_OWNER))
OTHER = bearer(mint_id_token(SUB_OTHER))
EDITOR = bearer(mint_id_token(SUB_EDITOR))
VIEWER = bearer(mint_id_token(SUB_VIEWER))
NEWCOMER = bearer(mint_id_token(SUB_NEW))
FREE_OWNER = bearer(mint_id_token(SUB_FREE))
MULTI = bearer(mint_id_token(SUB_MULTI))

ORDER_COLUMNS = [
    {"name": "order_id", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
]

CODE_RE = re.compile(r"^wsi-[0-9a-f]{24}$")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """平台库（团队三表 + 双客户种子）+ entities/datasets 内存库覆写 +
    sub2api/ceremony 外部通道假件的 TestClient。"""
    install_fake_jwks(monkeypatch)
    # plan_code="basic"（v2 席位语义）：free=1 席（仅 owner，邀请生成 409
    # 引导升级）；本文件邀请/兑换全环用例需可用席位（basic=3）。
    console_env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_OWNER, CUST_NAME), (SUB_OTHER, "other-cust")],
        plan_code="basic",
    )

    # entities + datasets 内存库（门禁矩阵的既有变更端点 / 密钥物面路由）
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    EntitiesBase.metadata.create_all(bind=engine)
    DatasetsBase.metadata.create_all(bind=engine)
    from sqlalchemy.orm import sessionmaker

    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db_session = TestingSession()
    seed_dataset(db_session, key="orders")
    seed_dataset(db_session, key="shipments", name="物流数据源")

    def override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_entities_db] = override_db
    app.dependency_overrides[get_datasets_db] = override_db

    # sub2api / ceremony 外部通道假件（无码首登零回归 + 兑换不触外部通道断言）
    fake_sub2api = FakeSub2Api()
    sub2api_client = fake_sub2api.client()

    def override_sub2api():
        yield sub2api_client

    app.dependency_overrides[get_console_sub2api] = override_sub2api
    registry_face = FakeRegistryFace()
    mint_channel = FakeMintChannel()
    monkeypatch.setattr(onboarding, "default_registry_face", lambda: registry_face)
    monkeypatch.setattr(onboarding, "default_mint_channel", lambda: mint_channel)

    client = TestClient(app)
    try:
        yield SimpleNamespaceShim(
            platform=console_env,
            client=client,
            sub2api=fake_sub2api,
            face=registry_face,
            mint=mint_channel,
            meta=db_session,
        )
    finally:
        app.dependency_overrides.pop(get_entities_db, None)
        app.dependency_overrides.pop(get_datasets_db, None)
        app.dependency_overrides.pop(get_console_sub2api, None)
        db_session.close()
        client.close()
        console_env.engine.dispose()


class SimpleNamespaceShim:
    """fixture 产物打包（避免 dataclass 字段与 fixture 局部重名噪音）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def seed_dataset(session: Session, *, customer_id: str = CUST_NAME, key: str = "orders", name: str | None = None) -> str:
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name or f"{key} 数据源",
        source_kind="upload",
        storage_tier="api",
        columns_json=ORDER_COLUMNS,
        row_count=10,
    )
    session.add(dataset)
    session.commit()
    return dataset.id


def owner_headers() -> dict:
    return dict(OWNER)


def make_invite(client: TestClient, *, role: str = "editor", headers: dict | None = None) -> dict:
    resp = client.post(
        "/api/console/team/invites", json={"role": role}, headers=headers or OWNER
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def redeem(client: TestClient, sub: str, code: str):
    return client.post(
        "/api/console/session",
        json={"id_token": mint_id_token(sub), "invite_code": code},
    )


def seed_member(env, sub: str, role: str, *, customer: str = CUST_NAME) -> None:
    """直插成员行（门禁矩阵聚焦门禁本身；兑换入队另有全环测试）。"""
    customer_id = env.platform.subs[SUB_OWNER if customer == CUST_NAME else SUB_OTHER]
    with env.platform.session() as sess:
        sess.add(
            CustomerMember(
                customer_id=customer_id,
                logto_sub=sub,
                role=role,
                invited_by_sub=SUB_OWNER,
            )
        )
        sess.commit()


def create_entity(client: TestClient, headers: dict, dataset_id: str, name: str = "订单"):
    return client.post(
        "/api/v1/entities",
        json={"name": name, "dataset_id": dataset_id, "pk_columns": ["order_id"]},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# v2 多队用例的直插/直查工具（fixture 客户之外自建客户、指针行与解析观察）
# ---------------------------------------------------------------------------


def insert_member(env, customer_id: int, sub: str, role: str) -> None:
    """按 customer_id 直插成员行（seed_member 只认 fixture 两客户，此处任意队）。"""
    with env.platform.session() as sess:
        sess.add(
            CustomerMember(
                customer_id=customer_id,
                logto_sub=sub,
                role=role,
            )
        )
        sess.commit()


def seed_pointer(env, sub: str, customer_id: int) -> None:
    """upsert 活跃队指针行（member_active_team 每 sub 至多一行）。"""
    with env.platform.session() as sess:
        row = sess.get(MemberActiveTeam, sub)
        if row is None:
            sess.add(MemberActiveTeam(logto_sub=sub, customer_id=customer_id))
        else:
            row.customer_id = customer_id
        sess.commit()


def drop_pointer(env, sub: str) -> None:
    with env.platform.session() as sess:
        row = sess.get(MemberActiveTeam, sub)
        if row is not None:
            sess.delete(row)
            sess.commit()


def pointer_customer_id(env, sub: str) -> int | None:
    """当前指针指向的客户 id（无指针行返回 None）。"""
    with env.platform.session() as sess:
        row = sess.get(MemberActiveTeam, sub)
        return row.customer_id if row is not None else None


def resolved_role(env, sub: str) -> tuple[int, str] | None:
    """直接调解析层：sub → (customer_id, role)；悬空自愈写随本事务提交
    （对齐请求级 get_db 的成功提交面）。"""
    with env.platform.session() as sess:
        result = resolve_member_role(sess, sub)
        sess.commit()
        if result is None:
            return None
        customer, role = result
        return customer.id, role


def create_customer(env, *, name: str, owner_sub: str, plan_code: str = "basic") -> int:
    """用例内自建客户（档位行取 seed_plans 已播的 plan_code 档）。"""
    with env.platform.session() as sess:
        plan = sess.execute(select(Plan).where(Plan.code == plan_code)).scalar_one()
        customer = BillingCustomer(
            name=name,
            sub2api_user_id=f"caller-{name}",
            logto_sub=owner_sub,
            plan_id=plan.id,
        )
        sess.add(customer)
        sess.flush()
        customer_id = customer.id
        sess.commit()
        return customer_id


def set_plan_limits(env, plan_code: str, limits: dict | None) -> None:
    """用例内改档位行 limits_json（后台可配优先的席位覆盖面）。"""
    with env.platform.session() as sess:
        plan = sess.execute(select(Plan).where(Plan.code == plan_code)).scalar_one()
        plan.limits_json = limits
        sess.commit()


# ---------------------------------------------------------------------------
# 邀请全环：生成 → 兑换入队 → 码失效 → 再兑拒
# ---------------------------------------------------------------------------


def test_invite_full_loop(env):
    invite = make_invite(env.client, role="editor")
    assert CODE_RE.match(invite["code"]), invite["code"]
    assert invite["role"] == "editor"
    expires_at = invite["expires_at"]
    assert expires_at  # 24h 窗口（形状断言：ISO 时间串）

    # 兑换前 sub2api 零调用基线
    users_before = len(env.sub2api.users)

    resp = redeem(env.client, SUB_NEW, invite["code"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    # 客户 = 邀请方（不建新客户）；key_once 恒 null（不铸 key）
    assert body["customer"]["name"] == CUST_NAME
    assert body["customer"]["id"] == env.platform.subs[SUB_OWNER]
    assert body["key_once"] is None
    # 不动 sub2api、不写 caller-map、不铸 key（成员共享邀请方面）
    assert len(env.sub2api.users) == users_before
    assert env.face.caller_map == {}
    assert env.mint.mint_count == 0

    # 落库断言：成员行（角色=码角色、invited_by=码生成者）+ 码置 used + 留痕
    with env.platform.session() as sess:
        member = sess.execute(
            select(CustomerMember).where(CustomerMember.logto_sub == SUB_NEW)
        ).scalar_one()
        assert member.customer_id == env.platform.subs[SUB_OWNER]
        assert member.role == "editor"
        assert member.invited_by_sub == SUB_OWNER
        used = sess.execute(
            select(CustomerInvite).where(CustomerInvite.code == invite["code"])
        ).scalar_one()
        assert used.used_by_sub == SUB_NEW
        assert used.used_at is not None
        events = (
            sess.execute(
                select(TeamEvent).where(TeamEvent.action == "member.joined")
            )
            .scalars()
            .all()
        )
        assert [(e.actor_sub, e.target) for e in events] == [(SUB_NEW, SUB_NEW)]
        assert events[0].detail["role"] == "editor"

    # 码已用：另一新身份再兑 → 502 failed 带原因
    resp2 = redeem(env.client, "sub-team-late", invite["code"])
    assert resp2.status_code == 502
    assert resp2.json() == {"status": "failed", "reason": "邀请码已被使用"}

    # 成员视角：GET /api/console/team 403（团队管理 owner-only）
    assert env.client.get("/api/console/team", headers=NEWCOMER).status_code == 403


def test_get_team_lists_owner_row_and_invites(env):
    invite = make_invite(env.client, role="viewer")
    resp = env.client.get("/api/console/team", headers=OWNER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["me"] == {"sub": SUB_OWNER, "role": "owner"}
    members = body["members"]
    # 隐含 owner 行：logto_sub=Customer.logto_sub
    assert members[0]["logto_sub"] == SUB_OWNER
    assert members[0]["role"] == "owner"
    assert [i["id"] for i in body["invites"]] == [invite["id"]]
    assert body["invites"][0]["code"] == invite["code"]
    assert body["invites"][0]["role"] == "viewer"


def test_get_team_lists_only_pending_invites(env):
    """待用邀请列表：已用/过期码不回（spec「待用邀请列表」；历史在 team_events）。"""
    pending = make_invite(env.client, role="viewer")
    used = make_invite(env.client, role="editor")
    expired = make_invite(env.client, role="editor")
    # used：新身份真实兑换入队（走全环路径置 used）
    assert redeem(env.client, SUB_NEW, used["code"]).status_code == 200
    # expired：expires_at 手改到过去（库面直改，免时钟 monkeypatch）
    with env.platform.session() as sess:
        row = sess.execute(
            select(CustomerInvite).where(CustomerInvite.code == expired["code"])
        ).scalar_one()
        row.expires_at = row.created_at.replace(tzinfo=None) - timedelta(seconds=1)
        sess.commit()

    body = env.client.get("/api/console/team", headers=OWNER).json()
    assert [i["id"] for i in body["invites"]] == [pending["id"]]


# ---------------------------------------------------------------------------
# 兑换拒绝面：已归属 / 码不存在 / 码过期
# ---------------------------------------------------------------------------


def test_redeem_rejects_identity_with_existing_customer(env):
    invite = make_invite(env.client)
    # owner 本人携本队码：诚实拒（v2 多队语义：同队 owner 不重复入队，
    # 旧「已有客户归属」全局拒已随多队解锁退役）
    resp = redeem(env.client, SUB_OWNER, invite["code"])
    assert resp.status_code == 502
    assert resp.json() == {
        "status": "failed",
        "reason": "已是该团队成员（owner），无需重复加入",
    }
    # 已是成员的身份携同队另一码：同样拒（同 (customer, sub) 409 面）
    seed_member(env, SUB_EDITOR, "editor")
    invite2 = make_invite(env.client)
    resp2 = redeem(env.client, SUB_EDITOR, invite2["code"])
    assert resp2.status_code == 502
    assert resp2.json()["reason"] == "已是该团队成员，无需重复加入"


def test_redeem_unknown_code(env):
    resp = redeem(env.client, SUB_NEW, "wsi-" + "0" * 24)
    assert resp.status_code == 502
    assert resp.json() == {"status": "failed", "reason": "邀请码不存在"}


def test_redeem_expired_code(env):
    invite = make_invite(env.client)
    # 时钟注入替代：expires_at 手改到过去（库面直改，免时钟 monkeypatch）
    with env.platform.session() as sess:
        row = sess.execute(
            select(CustomerInvite).where(CustomerInvite.code == invite["code"])
        ).scalar_one()
        row.expires_at = row.created_at.replace(tzinfo=None) - timedelta(seconds=1)
        sess.commit()
    resp = redeem(env.client, SUB_NEW, invite["code"])
    assert resp.status_code == 502
    assert resp.json() == {"status": "failed", "reason": "邀请码已过期"}
    # 过期码不入队、不置 used
    with env.platform.session() as sess:
        assert (
            sess.execute(
                select(CustomerMember).where(CustomerMember.logto_sub == SUB_NEW)
            ).scalar_one_or_none()
            is None
        )


# ---------------------------------------------------------------------------
# 门禁矩阵：viewer 只读 / editor 变更通 / 密钥物 owner-only / owner 全通
# ---------------------------------------------------------------------------


@pytest.fixture()
def matrix(env):
    """团队三席就位 + 两个种子数据集 id（owner/editor 各建一体，一集一体）。"""
    seed_member(env, SUB_EDITOR, "editor")
    seed_member(env, SUB_VIEWER, "viewer")
    datasets = env.meta.execute(select(Dataset).order_by(Dataset.key)).scalars().all()
    return SimpleNamespaceShim(env=env, dataset_id=datasets[0].id, dataset2_id=datasets[1].id)


def test_gate_viewer_read_ok_write_403(matrix):
    env, dataset_id = matrix.env, matrix.dataset_id
    # 读面与成员一致
    assert env.client.get("/api/v1/entities", headers=VIEWER).status_code == 200
    # 变更类一律 403（统一依赖内，角色判定只来自服务端解析）
    resp = env.client.post(
        "/api/v1/entities",
        json={"name": "订单", "dataset_id": dataset_id, "pk_columns": ["order_id"]},
        headers=VIEWER,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "团队成员权限不足（viewer 只读）"
    # PUT/DELETE 同拒（用既有实体端点形态：先由 owner 建一个实体）
    created = create_entity(env.client, OWNER, dataset_id)
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]
    put = env.client.put(
        f"/api/v1/entities/{entity_id}",
        json={"name": "订单改名", "dataset_id": dataset_id, "pk_columns": ["order_id"]},
        headers=VIEWER,
    )
    assert put.status_code == 403
    assert env.client.delete(f"/api/v1/entities/{entity_id}", headers=VIEWER).status_code == 403


def test_gate_key_surface_owner_only(matrix):
    env = matrix.env
    # owner：密钥物面全通（GET 列表 200 / POST 铸造 201 / 配对令牌 201）
    assert env.client.get("/api/v1/data-plane-keys", headers=OWNER).status_code == 200
    minted = env.client.post(
        "/api/v1/data-plane-keys", json={"label": "生产"}, headers=OWNER
    )
    assert minted.status_code == 201, minted.text
    assert (
        env.client.post("/api/v1/connectors/pairing-tokens", headers=OWNER).status_code == 201
    )
    # editor：密钥物面一律 403（含读面——密钥管理仅 owner）
    for method, path in (
        ("get", "/api/v1/data-plane-keys"),
        ("post", "/api/v1/data-plane-keys"),
        ("delete", f"/api/v1/data-plane-keys/{minted.json()['id']}"),
        ("post", "/api/v1/connectors/pairing-tokens"),
    ):
        resp = getattr(env.client, method)(path, headers=EDITOR)
        assert resp.status_code == 403, (method, path, resp.text)
        assert resp.json()["detail"] == "团队成员权限不足（密钥管理仅 owner）"
    # viewer 同拒（GET 也拒：密钥物面 owner-only 盖过 viewer 读面）
    resp = env.client.get("/api/v1/data-plane-keys", headers=VIEWER)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "团队成员权限不足（密钥管理仅 owner）"


def test_gate_editor_mutations_allowed(matrix):
    env, dataset_id = matrix.env, matrix.dataset_id
    # editor 实体变更通（editor=除团队管理与密钥物外全权）
    resp = create_entity(env.client, EDITOR, dataset_id, name="编辑者实体")
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "编辑者实体"
    # editor 团队管理仍 403
    assert (
        env.client.post(
            "/api/console/team/invites", json={"role": "viewer"}, headers=EDITOR
        ).status_code
        == 403
    )
    # owner 全通：实体变更（一集一体——owner 用第二个数据集）
    assert (
        create_entity(env.client, OWNER, matrix.dataset2_id, name="owner 实体").status_code
        == 201
    )


def test_gate_rejects_bad_invite_role(env):
    resp = env.client.post(
        "/api/console/team/invites", json={"role": "owner"}, headers=OWNER
    )
    assert resp.status_code == 422  # role 只 editor|viewer（pydantic pattern）


# ---------------------------------------------------------------------------
# 成员撤销：撤即 401 / 撤 owner 409 / 未知 404 / 自撤允许 / 成员撤他人 403
# ---------------------------------------------------------------------------


def test_member_revoked_next_request_401(env):
    seed_member(env, SUB_EDITOR, "editor")
    headers = EDITOR
    assert env.client.get("/api/v1/entities", headers=headers).status_code == 200
    resp = env.client.delete(f"/api/console/team/members/{SUB_EDITOR}", headers=OWNER)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    # 撤销即失效：下一次请求 401（解析不到归属）
    assert env.client.get("/api/v1/entities", headers=headers).status_code == 401
    # 留痕
    with env.platform.session() as sess:
        events = (
            sess.execute(
                select(TeamEvent).where(
                    TeamEvent.action == "member.removed", TeamEvent.target == SUB_EDITOR
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].actor_sub == SUB_OWNER


def test_remove_owner_409_and_unknown_404(env):
    resp = env.client.delete(f"/api/console/team/members/{SUB_OWNER}", headers=OWNER)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "owner 不可被撤销"
    assert (
        env.client.delete("/api/console/team/members/sub-ghost", headers=OWNER).status_code
        == 404
    )


def test_member_self_removal_allowed(env):
    seed_member(env, SUB_EDITOR, "editor")
    seed_member(env, SUB_VIEWER, "viewer")
    # 成员撤他人 403（viewer 不能撤 editor——团队管理仅 owner）
    resp = env.client.delete(f"/api/console/team/members/{SUB_EDITOR}", headers=VIEWER)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "团队成员权限不足（团队管理仅 owner）"
    # 成员自撤允许（团队管理 owner-only 的唯一例外）
    resp2 = env.client.delete(f"/api/console/team/members/{SUB_EDITOR}", headers=EDITOR)
    assert resp2.status_code == 200
    assert resp2.json() == {"deleted": True}
    assert env.client.get("/api/v1/entities", headers=EDITOR).status_code == 401
    # 留痕：自撤事件 actor=本人
    with env.platform.session() as sess:
        events = (
            sess.execute(
                select(TeamEvent).where(TeamEvent.action == "member.removed")
            )
            .scalars()
            .all()
        )
        assert [(e.actor_sub, e.target, e.detail) for e in events] == [
            (SUB_EDITOR, SUB_EDITOR, {"self": True})
        ]


def test_invite_revocation(env):
    invite = make_invite(env.client)
    resp = env.client.delete(f"/api/console/team/invites/{invite['id']}", headers=OWNER)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    # 再删 404；兑换按不存在拒
    assert (
        env.client.delete(f"/api/console/team/invites/{invite['id']}", headers=OWNER).status_code
        == 404
    )
    assert redeem(env.client, SUB_NEW, invite["code"]).status_code == 502
    # 他企 owner 不可见/不可删本队邀请（客户维度隔离 → 404 不泄露）
    invite2 = make_invite(env.client)
    assert (
        env.client.delete(
            f"/api/console/team/invites/{invite2['id']}", headers=OTHER
        ).status_code
        == 404
    )
    # 留痕
    with env.platform.session() as sess:
        actions = [
            e.action
            for e in sess.execute(select(TeamEvent)).scalars().all()
        ]
        assert "invite.created" in actions
        assert "invite.revoked" in actions


# ---------------------------------------------------------------------------
# 无码首登零回归
# ---------------------------------------------------------------------------


def test_no_code_first_login_unchanged(env):
    """无码路径与现状逐字节一致：sub2api 代建 + caller-map + 铸 key。"""
    users_before = len(env.sub2api.users)
    resp = env.client.post(
        "/api/console/session", json={"id_token": mint_id_token("sub-fresh-login")}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["key_once"] is not None  # 首登完成开户：明文 key 一次性交付
    assert len(env.sub2api.users) == users_before + 1  # 代建发生
    assert "sub-fresh-login" in env.face.caller_map  # registry 授权束写入
    assert env.mint.mint_count == 1
    with env.platform.session() as sess:
        customer = sess.execute(
            select(BillingCustomer).where(
                BillingCustomer.logto_sub == "sub-fresh-login"
            )
        ).scalar_one()
        assert customer.name == "sub-fresh-login"  # display_name=email 缺省=sub


def test_member_shares_owner_customer_namespace(env):
    """成员与 owner 同一客户命名空间（require_console_customer_id 返回
    Customer.name——成员读写落在邀请方空间）。"""
    seed_member(env, SUB_VIEWER, "viewer")
    seeded = env.meta.execute(select(Dataset)).scalars().first()
    # viewer 只读：列表可见 owner 客户的实体空间（空但 200）
    assert env.client.get("/api/v1/entities", headers=VIEWER).json() == {"entities": []}
    # owner 建实体后 viewer 可见（同空间）
    created = create_entity(env.client, OWNER, seeded.id)
    assert created.status_code == 201
    listing = env.client.get("/api/v1/entities", headers=VIEWER).json()
    assert [e["id"] for e in listing["entities"]] == [created.json()["id"]]
    # 实体表落行断言：customer_id = 邀请方命名空间
    row = env.meta.execute(select(Entity)).scalar_one()
    assert row.customer_id == CUST_NAME


# ---------------------------------------------------------------------------
# 5.1 解析层（wire-team-spaces-v2）：三序全分支 / 悬空自愈 / guesting owner /
# 成员被撤后回退
# ---------------------------------------------------------------------------


def test_resolve_priority_all_branches(env):
    """三序全分支：指针有效（成员行/自有队两形态）→ 无指针 owner 回退 →
    无指针首队回退（多成员行取 id 最小）→ 全无 None。

    - 指针指他队成员行 → 按成员行角色解析（owner 回退被指针压制）；
    - 指针指自有队 → 'owner'（owner 自切形态）；
    - 无指针但自有客户存在 → owner 回退（存量 owner 零感知）；
    - 无自有客户、多条成员行 → 按**成员行 id 最小**回退（特意让 customer_id
      更大的队行 id 更小，区分「行 id 序」与「customer_id 序」）；
    - 无任何归属 → None（401 面）。
    """
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]
    third_id = create_customer(env, name="third-cust", owner_sub=SUB_THIRD)

    # —— 指针优先：owner 兼任他队成员（editor），指针 → 他队按成员角色解析
    insert_member(env, other_id, SUB_OWNER, "editor")
    seed_pointer(env, SUB_OWNER, other_id)
    assert resolved_role(env, SUB_OWNER) == (other_id, "editor")

    # —— 指针优先（自有队形态）：指针切回自有队 → 'owner'
    seed_pointer(env, SUB_OWNER, own_id)
    assert resolved_role(env, SUB_OWNER) == (own_id, "owner")

    # —— owner 回退：无指针时自有客户命中
    drop_pointer(env, SUB_OWNER)
    assert resolved_role(env, SUB_OWNER) == (own_id, "owner")

    # —— 首队回退：无自有客户的 sub，多条成员行取 id 最小（third 行先插
    #    行 id 小、但 customer_id 比 other 大——证明取的是行 id 序）
    insert_member(env, third_id, "sub-plain-member", "viewer")
    insert_member(env, other_id, "sub-plain-member", "editor")
    assert resolved_role(env, "sub-plain-member") == (third_id, "viewer")

    # —— 全无：未映射 sub → None
    assert resolved_role(env, "sub-never-mapped") is None


def test_resolve_dangling_pointer_self_heals(env):
    """悬空指针同事务自愈：指针指向已删成员行（或已删客户）→ 解析按
    owner/首队回退重解析，且指针行被删（不留腐化行）。

    三形态：回退他队成员行、全无归属回 None、回退自有队 owner。
    """
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]

    # —— 形态一：成员行被删（绕过端点的直删）且另有成员行 → 回退他队
    insert_member(env, own_id, SUB_EDITOR, "editor")
    insert_member(env, other_id, SUB_EDITOR, "viewer")
    seed_pointer(env, SUB_EDITOR, own_id)
    with env.platform.session() as sess:
        row = sess.execute(
            select(CustomerMember).where(
                CustomerMember.customer_id == own_id,
                CustomerMember.logto_sub == SUB_EDITOR,
            )
        ).scalar_one()
        sess.delete(row)
        sess.commit()
    assert resolved_role(env, SUB_EDITOR) == (other_id, "viewer")
    assert pointer_customer_id(env, SUB_EDITOR) is None  # 悬空指针行已被清除

    # —— 形态二：成员行被删且无其他归属 → None（401 面）
    insert_member(env, own_id, SUB_VIEWER, "viewer")
    seed_pointer(env, SUB_VIEWER, own_id)
    with env.platform.session() as sess:
        row = sess.execute(
            select(CustomerMember).where(
                CustomerMember.customer_id == own_id,
                CustomerMember.logto_sub == SUB_VIEWER,
            )
        ).scalar_one()
        sess.delete(row)
        sess.commit()
    assert resolved_role(env, SUB_VIEWER) is None
    assert pointer_customer_id(env, SUB_VIEWER) is None

    # —— 形态三：指针指向已删客户行（customer_id 悬空）→ 回退自有队 owner
    seed_pointer(env, SUB_OWNER, 999999)
    assert resolved_role(env, SUB_OWNER) == (own_id, "owner")
    assert pointer_customer_id(env, SUB_OWNER) is None


def test_guesting_owner_key_surface_403_and_restore(env):
    """guesting owner：owner 切指针到 guest 队后，密钥物面（owner-only，
    KEY_SURFACE_PREFIXES 的 /api/v1/data-plane-keys）与自家团队管理面均
    403（按 guest 成员角色解析）；切回自有队即恢复 200——刻意不做
    「密钥面永远解析自有队」特例，避免一个请求双客户上下文。"""
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]
    insert_member(env, other_id, SUB_OWNER, "editor")

    resp = env.client.post(
        "/api/console/team/switch", json={"customer_id": other_id}, headers=OWNER
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"active_customer_id": other_id}

    # 密钥物面读面也 owner-only：guesting 中 GET 列表 403（读面非 viewer
    # 方法门问题，纯密钥物 owner-only 门）
    resp = env.client.get("/api/v1/data-plane-keys", headers=OWNER)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "团队成员权限不足（密钥管理仅 owner）"
    assert (
        env.client.post(
            "/api/v1/data-plane-keys", json={"label": "铸造"}, headers=OWNER
        ).status_code
        == 403
    )
    # 自家团队管理面同步暂时失权（require_console_owner 按 guest 角色拒）
    assert env.client.get("/api/console/team", headers=OWNER).status_code == 403

    # 切回自有队：密钥物面与团队管理面即时恢复（解析层每请求读库）
    resp = env.client.post(
        "/api/console/team/switch", json={"customer_id": own_id}, headers=OWNER
    )
    assert resp.status_code == 200
    assert env.client.get("/api/v1/data-plane-keys", headers=OWNER).status_code == 200
    assert env.client.get("/api/console/team", headers=OWNER).status_code == 200


def test_removed_member_pointer_repoints_or_deletes(env):
    """成员被撤后的指针处置（remove_member 同事务面）：

    - 指针指本队且另有成员行 → 重指**剩余 id 最小**成员行（third 行先插
      行 id 小、customer_id 比 other 大——区分行 id 序与 customer_id 序）；
    - 指针指本队且无剩余 → 删指针行，被撤者下一次请求 401；
    - 端点观察：重指后 teams 清单 active_customer_id = 重指队。
    """
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]
    third_id = create_customer(env, name="third-cust", owner_sub=SUB_THIRD)

    # —— 重指剩余 id 最小成员行
    insert_member(env, third_id, SUB_EDITOR, "editor")  # 行 id 最小
    insert_member(env, other_id, SUB_EDITOR, "viewer")
    insert_member(env, own_id, SUB_EDITOR, "editor")
    seed_pointer(env, SUB_EDITOR, own_id)
    resp = env.client.delete(f"/api/console/team/members/{SUB_EDITOR}", headers=OWNER)
    assert resp.status_code == 200, resp.text
    assert pointer_customer_id(env, SUB_EDITOR) == third_id
    assert resolved_role(env, SUB_EDITOR) == (third_id, "editor")
    body = env.client.get("/api/console/teams", headers=EDITOR).json()
    assert body["active_customer_id"] == third_id

    # —— 无剩余成员行：删指针 → 401
    insert_member(env, own_id, SUB_VIEWER, "viewer")
    seed_pointer(env, SUB_VIEWER, own_id)
    resp = env.client.delete(f"/api/console/team/members/{SUB_VIEWER}", headers=OWNER)
    assert resp.status_code == 200
    assert pointer_customer_id(env, SUB_VIEWER) is None
    assert env.client.get("/api/v1/entities", headers=VIEWER).status_code == 401


# ---------------------------------------------------------------------------
# 5.2 兑换与席位（wire-team-spaces-v2）：多队解锁 / 同队拒 / 双闸 /
# limits_json 覆盖 / 码保活 / 免费档引导 / grandfather
# ---------------------------------------------------------------------------


def test_redeem_multi_team_unlock_and_pointer_untouched(env):
    """多队解锁：已有队成员/owner 兑换**新队**码成功（v2 移除全局归属拒）——
    各自原解析不变、已有指针不动；无指针的新 sub 首次兑换即落指针
    （首队落指针语义）。"""
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]

    # —— 已有队成员兑新队码：入 other-cust 成功，指针与原解析不动
    insert_member(env, own_id, SUB_EDITOR, "editor")
    seed_pointer(env, SUB_EDITOR, own_id)
    invite_other = make_invite(env.client, headers=OTHER)
    resp = redeem(env.client, SUB_EDITOR, invite_other["code"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["customer"]["name"] == OTHER_CUST_NAME
    assert body["key_once"] is None
    with env.platform.session() as sess:
        rows = (
            sess.execute(
                select(CustomerMember).where(CustomerMember.logto_sub == SUB_EDITOR)
            )
            .scalars()
            .all()
        )
        assert {(r.customer_id, r.role) for r in rows} == {
            (own_id, "editor"),
            (other_id, "editor"),  # 角色=码的 role（editor 缺省）
        }
    assert pointer_customer_id(env, SUB_EDITOR) == own_id  # 指针不动
    assert resolved_role(env, SUB_EDITOR) == (own_id, "editor")  # 原解析不变

    # —— owner 兑他队码：owner 身份不再是全局拒（只拒同队重复）
    seed_pointer(env, SUB_OWNER, own_id)
    invite_for_owner = make_invite(env.client, headers=OTHER)
    resp = redeem(env.client, SUB_OWNER, invite_for_owner["code"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer"]["id"] == other_id
    assert pointer_customer_id(env, SUB_OWNER) == own_id
    assert resolved_role(env, SUB_OWNER) == (own_id, "owner")

    # —— 首队落指针：无任何归属的新 sub 首次兑换 → 新队即活跃队
    invite_own = make_invite(env.client, headers=OWNER)
    resp = redeem(env.client, SUB_NEW, invite_own["code"])
    assert resp.status_code == 200, resp.text
    assert pointer_customer_id(env, SUB_NEW) == own_id
    body = env.client.get("/api/console/teams", headers=NEWCOMER).json()
    assert body["active_customer_id"] == own_id


def test_redeem_same_team_rejected_keeps_code_alive(env):
    """同队重复拒：成员再兑同队码 → 502「已是该团队成员」；邀请方 owner
    本人兑自家码 → 502（owner 形态文案）。拒绝不消耗码（码保活）。"""
    seed_member(env, SUB_EDITOR, "editor")
    invite = make_invite(env.client)

    resp = redeem(env.client, SUB_EDITOR, invite["code"])
    assert resp.status_code == 502
    assert resp.json() == {"status": "failed", "reason": "已是该团队成员，无需重复加入"}
    resp = redeem(env.client, SUB_OWNER, invite["code"])
    assert resp.status_code == 502
    assert resp.json() == {
        "status": "failed",
        "reason": "已是该团队成员（owner），无需重复加入",
    }

    # 码保活：未置 used、待用列表仍含该码、无 member.joined 留痕
    with env.platform.session() as sess:
        row = sess.execute(
            select(CustomerInvite).where(CustomerInvite.code == invite["code"])
        ).scalar_one()
        assert row.used_by_sub is None and row.used_at is None
        joined = (
            sess.execute(select(TeamEvent).where(TeamEvent.action == "member.joined"))
            .scalars()
            .all()
        )
        assert joined == []
    body = env.client.get("/api/console/team", headers=OWNER).json()
    assert [i["id"] for i in body["invites"]] == [invite["id"]]


def test_invite_generation_seat_gate(env):
    """生成闸：seats_used（成员行数+1）≥ seat_limit（basic=3）→ 409，
    detail 含档位 code 与席位数字及升级指引；不产码（库内无行）。"""
    insert_member(env, env.platform.subs[SUB_OWNER], SUB_EDITOR, "editor")
    insert_member(env, env.platform.subs[SUB_OWNER], SUB_VIEWER, "viewer")  # 3/3 满

    resp = env.client.post(
        "/api/console/team/invites", json={"role": "editor"}, headers=OWNER
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "basic" in detail
    assert "已用 3/3 席" in detail
    assert "升级套餐" in detail

    # 不产码：无邀请行 + 待用列表空；席位用量如实
    with env.platform.session() as sess:
        count = sess.execute(
            select(func.count()).select_from(CustomerInvite).where(
                CustomerInvite.customer_id == env.platform.subs[SUB_OWNER]
            )
        ).scalar_one()
        assert count == 0
    body = env.client.get("/api/console/team", headers=OWNER).json()
    assert body["invites"] == []
    assert body["seats_used"] == 3
    assert body["seats_limit"] == 3


def test_seat_limit_limits_json_override(env):
    """limits_json.seats 覆盖缺省：basic 缺省 3 时 used=2 可产码；改
    basic 档 limits_json={"seats":2} 后同状态 409（文案 2/2）、GET team
    seats_limit=2；腾位后（移除成员 used=1）恢复产码——后台可配即时生效
    不走 seed。"""
    own_id = env.platform.subs[SUB_OWNER]
    insert_member(env, own_id, SUB_EDITOR, "editor")  # used=2

    # 缺省 basic=3：未满 → 产码
    assert make_invite(env.client)  # 内部断言 201

    # 覆盖为 2：used=2 ≥ 2 → 409
    set_plan_limits(env, "basic", {"seats": 2})
    resp = env.client.post(
        "/api/console/team/invites", json={"role": "viewer"}, headers=OWNER
    )
    assert resp.status_code == 409
    assert "已用 2/2 席" in resp.json()["detail"]
    body = env.client.get("/api/console/team", headers=OWNER).json()
    assert body["seats_limit"] == 2
    assert body["seats_used"] == 2

    # 腾位后恢复：used=1 < 2 → 201
    assert (
        env.client.delete(f"/api/console/team/members/{SUB_EDITOR}", headers=OWNER).status_code
        == 200
    )
    assert make_invite(env.client, role="viewer")


def test_redeem_seat_gate_keeps_code_alive(env):
    """兑换闸+码保活：满员时兑换 → 502 failed 语义拒（reason 含档位与
    席位数字），码不置 used（待用列表仍可见）；腾位后**同码**兑换成功。"""
    own_id = env.platform.subs[SUB_OWNER]
    insert_member(env, own_id, SUB_EDITOR, "editor")  # used=2/3
    invite = make_invite(env.client)  # 未满时产码
    insert_member(env, own_id, SUB_VIEWER, "viewer")  # 直插至 3/3 满员

    resp = redeem(env.client, SUB_NEW, invite["code"])
    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "failed"
    assert "席位已满" in body["reason"]
    assert "basic" in body["reason"]
    assert "3/3" in body["reason"]

    # 码保活：未置 used，待用邀请列表仍含该码
    with env.platform.session() as sess:
        row = sess.execute(
            select(CustomerInvite).where(CustomerInvite.code == invite["code"])
        ).scalar_one()
        assert row.used_by_sub is None
    listing = env.client.get("/api/console/team", headers=OWNER).json()
    assert [i["id"] for i in listing["invites"]] == [invite["id"]]

    # 腾位后同码兑换成功：入队 + 码置 used
    assert (
        env.client.delete(f"/api/console/team/members/{SUB_VIEWER}", headers=OWNER).status_code
        == 200
    )
    resp = redeem(env.client, SUB_NEW, invite["code"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer"]["id"] == own_id
    with env.platform.session() as sess:
        member = sess.execute(
            select(CustomerMember).where(CustomerMember.logto_sub == SUB_NEW)
        ).scalar_one()
        assert (member.customer_id, member.role) == (own_id, "editor")
        row = sess.execute(
            select(CustomerInvite).where(CustomerInvite.code == invite["code"])
        ).scalar_one()
        assert row.used_by_sub == SUB_NEW


def test_free_plan_upgrade_guidance(env):
    """免费档引导：free 档（席位 1=仅 owner）生成邀请 → 409 且 detail 含
    档位、1/1 数字与升级指引文案（团队功能付费解锁）。"""
    create_customer(env, name="free-cust", owner_sub=SUB_FREE, plan_code="free")

    resp = env.client.post(
        "/api/console/team/invites", json={"role": "editor"}, headers=FREE_OWNER
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "free" in detail
    assert "已用 1/1 席" in detail
    assert "升级套餐" in detail
    body = env.client.get("/api/console/team", headers=FREE_OWNER).json()
    assert (body["seats_used"], body["seats_limit"]) == (1, 1)


def test_grandfather_overlimit_shown_and_removal_not_blocked(env):
    """grandfather：超配存量成员行（直插 4 行，basic=3）如实展示
    seats_used=5 > seats_limit=3；生成闸照拦（5/3）；移除成员不受阻。"""
    own_id = env.platform.subs[SUB_OWNER]
    overlimit_subs = [f"sub-overlimit-{i}" for i in range(4)]
    for sub in overlimit_subs:
        insert_member(env, own_id, sub, "viewer")

    body = env.client.get("/api/console/team", headers=OWNER).json()
    assert body["seats_used"] == 5
    assert body["seats_limit"] == 3
    resp = env.client.post(
        "/api/console/team/invites", json={"role": "editor"}, headers=OWNER
    )
    assert resp.status_code == 409
    assert "已用 5/3 席" in resp.json()["detail"]

    # 超配不阻断移除：撤一个成员照常成功，用量随之回落
    resp = env.client.delete(
        f"/api/console/team/members/{overlimit_subs[0]}", headers=OWNER
    )
    assert resp.status_code == 200, resp.text
    body = env.client.get("/api/console/team", headers=OWNER).json()
    assert body["seats_used"] == 4


# ---------------------------------------------------------------------------
# 5.3 切换与角色（wire-team-spaces-v2）：teams 清单 / switch 404+数据面联动 /
# 角色变更四场景
# ---------------------------------------------------------------------------


def test_teams_listing_own_first_roles_and_active(env):
    """teams 清单：多队身份（自有 + 两成员队）自有队在前、成员队按
    customer_id 排序（与成员行插入序相反以示区分）、role 正确、is_active
    唯一真（无指针时 owner 回退默认；指针切换后跟随指针且排序不变）。"""
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]
    multi_id = create_customer(env, name="multi-cust", owner_sub=SUB_MULTI)
    # 成员行插入序 other → own（行 id 序），customer_id 序 own(1) < other(2)
    insert_member(env, other_id, SUB_MULTI, "viewer")
    insert_member(env, own_id, SUB_MULTI, "editor")

    resp = env.client.get("/api/console/teams", headers=MULTI)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    teams = body["teams"]
    assert [t["customer_id"] for t in teams] == [multi_id, own_id, other_id]
    assert [t["customer_name"] for t in teams] == ["multi-cust", CUST_NAME, OTHER_CUST_NAME]
    assert [t["role"] for t in teams] == ["owner", "editor", "viewer"]
    # 无指针：活跃队 = owner 回退（自有队），is_active 唯一真
    assert [t["is_active"] for t in teams] == [True, False, False]
    assert body["active_customer_id"] == multi_id

    # 指针切到成员队：is_active 跟随指针（唯一真移位），排序不变
    assert (
        env.client.post(
            "/api/console/team/switch", json={"customer_id": other_id}, headers=MULTI
        ).status_code
        == 200
    )
    body = env.client.get("/api/console/teams", headers=MULTI).json()
    assert [t["customer_id"] for t in body["teams"]] == [multi_id, own_id, other_id]
    assert [t["is_active"] for t in body["teams"]] == [False, False, True]
    assert body["active_customer_id"] == other_id


def test_switch_404_and_data_plane_follows(env):
    """switch：目标非自有非成员 → 404（真实他队 id 不泄露 + 荒谬 id 同拒）
    且不动指针；切到成员队后数据面（GET /api/v1/datasets，
    require_console_customer_id=Customer.name）返回**目标队**数据集；
    同值幂等；切回原队恢复。"""
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]
    foreign_id = create_customer(env, name="switch-foreign", owner_sub=SUB_THIRD)
    insert_member(env, own_id, SUB_EDITOR, "editor")  # 行 id 小 → 默认活跃队=本队
    insert_member(env, other_id, SUB_EDITOR, "editor")
    seed_dataset(env.meta, customer_id=OTHER_CUST_NAME, key="other-orders", name="他队数据源")

    def dataset_keys() -> list[str]:
        resp = env.client.get("/api/v1/datasets", headers=EDITOR)
        assert resp.status_code == 200, resp.text
        return sorted(d["key"] for d in resp.json())

    # 默认活跃队 = 首队回退（本队）：见本队两数据集
    assert dataset_keys() == ["orders", "shipments"]

    # 非自有非成员 → 404（含真实他队 id：不泄露存在性）；指针不动
    for target in (foreign_id, 999999):
        resp = env.client.post(
            "/api/console/team/switch", json={"customer_id": target}, headers=EDITOR
        )
        assert resp.status_code == 404, (target, resp.text)
    assert dataset_keys() == ["orders", "shipments"]

    # 切到成员队：数据面随指针切到目标队命名空间
    resp = env.client.post(
        "/api/console/team/switch", json={"customer_id": other_id}, headers=EDITOR
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"active_customer_id": other_id}
    assert dataset_keys() == ["other-orders"]

    # 幂等：同值再切 → 200，数据面不变
    assert (
        env.client.post(
            "/api/console/team/switch", json={"customer_id": other_id}, headers=EDITOR
        ).status_code
        == 200
    )
    assert dataset_keys() == ["other-orders"]

    # 切回原队：恢复本队数据面
    assert (
        env.client.post(
            "/api/console/team/switch", json={"customer_id": own_id}, headers=EDITOR
        ).status_code
        == 200
    )
    assert dataset_keys() == ["orders", "shipments"]


def test_change_member_role_scenarios(env):
    """角色变更四场景（PATCH members/{sub}，owner-only）：

    - owner 改 viewer→editor 后该成员原 403 的 POST 端点放行（即改即生效）；
    - 非 owner（editor）调 PATCH → 403；
    - owner 本人行 → 409「owner 角色不可经本端点变更」；
    - 非本队成员（含他队成员 sub）→ 404；
    - team_events 恰一条 member.role_changed（actor/target/old/new 正确）；
    - 同值幂等：200 不留痕（events 数不增）。
    """
    own_id = env.platform.subs[SUB_OWNER]
    other_id = env.platform.subs[SUB_OTHER]
    insert_member(env, own_id, SUB_VIEWER, "viewer")
    insert_member(env, own_id, SUB_EDITOR, "editor")
    dataset_id = env.meta.execute(select(Dataset).order_by(Dataset.key)).scalars().all()[0].id

    # 提升前：viewer 变更类 403
    assert create_entity(env.client, VIEWER, dataset_id, name="viewer 实体").status_code == 403

    # owner 改角色 viewer → editor：响应体回显新角色
    resp = env.client.patch(
        f"/api/console/team/members/{SUB_VIEWER}",
        json={"role": "editor"},
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["logto_sub"] == SUB_VIEWER
    assert resp.json()["role"] == "editor"
    # 即改即生效：同 token 下一次请求按新角色门禁放行
    created = create_entity(env.client, VIEWER, dataset_id, name="viewer 实体")
    assert created.status_code == 201, created.text

    # 非 owner（editor）调 PATCH → 403
    assert (
        env.client.patch(
            f"/api/console/team/members/{SUB_VIEWER}",
            json={"role": "viewer"},
            headers=EDITOR,
        ).status_code
        == 403
    )

    # owner 本人行 → 409
    resp = env.client.patch(
        f"/api/console/team/members/{SUB_OWNER}",
        json={"role": "viewer"},
        headers=OWNER,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "owner 角色不可经本端点变更"

    # 非本队成员 → 404（他队成员行与陌生 sub 同面，不泄露他队归属）
    insert_member(env, other_id, "sub-foreign-member", "viewer")
    for sub in ("sub-foreign-member", "sub-ghost"):
        assert (
            env.client.patch(
                f"/api/console/team/members/{sub}",
                json={"role": "editor"},
                headers=OWNER,
            ).status_code
            == 404
        ), sub

    # 同值幂等：200 返回现角色且不留痕（events 数不增）
    resp = env.client.patch(
        f"/api/console/team/members/{SUB_VIEWER}",
        json={"role": "editor"},
        headers=OWNER,
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "editor"

    # team_events：恰一条 member.role_changed，old/new 与 actor/target 正确
    with env.platform.session() as sess:
        events = (
            sess.execute(
                select(TeamEvent).where(TeamEvent.action == "member.role_changed")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert (event.customer_id, event.actor_sub, event.target) == (
            own_id,
            SUB_OWNER,
            SUB_VIEWER,
        )
        assert event.detail == {"sub": SUB_VIEWER, "old": "viewer", "new": "editor"}
