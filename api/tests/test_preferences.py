"""个人偏好 API 测试（wire-personal-preferences：GET/PUT me/preferences）。

覆盖（tasks 1.4）：
- GET 新 sub 三键全 null 200（**不 404**）；未映射身份 401；
- 部分更新：缺键不动、显式 null 清空一键、空 body 零改动；
- 值域：locale zh/en 全环 + 非法 422；九页签逐一合法 + 非法 422（逐字文案）；
- 置顶 happy path + 422 矩阵（不存在/他户**同文案**/kind≠dashboard/已归档）；
- per-sub 隔离：同客户两成员各自 PUT 各自回读；
- viewer 可 GET/PUT 自己的偏好（无方法级门禁，不 403）；
- 读时自愈：置顶看板归档 → GET null 且存储列被清；再 PUT 新看板成功；
- PUT 响应体 = 写后全量态（与紧随 GET 逐字节一致）；updated_at 每写刷新。

环境：平台库（admin/billing/console 同一临时文件库，console_fixture——
member_preferences 随 ConsoleBase 建表）+ ai 内存库覆写（照
test_artifacts_lifecycle 的 get_db 覆写手法直插 Artifact 行）；成员席
直插 CustomerMember（照 test_team.seed_member）。
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

# 在导入 app 前把默认 ai 引擎指向内存库（与 test_artifacts_lifecycle 同款，
# 防止测试机上落下 fd_wire_ai.db 文件；本文件实际走 get_db 覆写）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import Artifact
from fd_wire_api.console.models import CustomerMember, MemberPreference

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_OWNER = "sub-pref-owner"
SUB_MEMBER = "sub-pref-member"  # 同客户 editor 席（隔离对端）
SUB_VIEWER = "sub-pref-viewer"
SUB_OTHER = "sub-pref-other-owner"
SUB_GHOST = "sub-pref-ghost"  # 未映射身份

CUST = "pref-cust"
OTHER_CUST = "other-pref-cust"

OWNER = bearer(mint_id_token(SUB_OWNER))
MEMBER = bearer(mint_id_token(SUB_MEMBER))
VIEWER = bearer(mint_id_token(SUB_VIEWER))
OTHER = bearer(mint_id_token(SUB_OTHER))
GHOST = bearer(mint_id_token(SUB_GHOST))

PREFS_URL = "/api/console/me/preferences"
ALL_NULL = {"ui_locale": None, "landing_tab": None, "pinned_dashboard_id": None}

UNMAPPED_DETAIL = "登录身份尚未映射到客户（请先完成首次登录开户）"


def seed_artifact(
    ai_session, *, customer_id: str = CUST, kind: str = "dashboard",
    status: str = "published", title: str = "经营看板",
) -> int:
    """直插一行产物（偏好只看 artifacts 行面，无需版本链）。"""
    artifact = Artifact(customer_id=customer_id, kind=kind, title=title, status=status)
    ai_session.add(artifact)
    ai_session.commit()
    return artifact.id


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """平台库（双客户 + viewer/editor 成员席）+ ai 内存库（四枚产物）。"""
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_OWNER, CUST), (SUB_OTHER, OTHER_CUST)],
    )

    # 成员席：editor + viewer（同客户；owner = Customer.logto_sub 本人）
    customer_id = console_env.subs[SUB_OWNER]
    with console_env.session() as sess:
        sess.add(CustomerMember(
            customer_id=customer_id, logto_sub=SUB_MEMBER,
            role="editor", invited_by_sub=SUB_OWNER,
        ))
        sess.add(CustomerMember(
            customer_id=customer_id, logto_sub=SUB_VIEWER,
            role="viewer", invited_by_sub=SUB_OWNER,
        ))
        sess.commit()

    # ai 内存库：artifacts 表 + 四枚产物（合法看板 / form / 已归档 / 他户）
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AiBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    ai_session = TestingSession()
    artifacts = {
        "own_dashboard": seed_artifact(ai_session, title="经营看板"),
        "own_form": seed_artifact(ai_session, kind="form", title="登记表"),
        "own_archived": seed_artifact(
            ai_session, status="archived", title="旧看板"
        ),
        "other_dashboard": seed_artifact(
            ai_session, customer_id=OTHER_CUST, title="他户看板"
        ),
    }

    def override_ai_db():
        try:
            yield ai_session
        finally:
            pass

    app.dependency_overrides[get_ai_db] = override_ai_db
    client = TestClient(app)
    try:
        yield SimpleNamespace(
            platform=console_env, client=client, ai=ai_session, artifacts=artifacts
        )
    finally:
        app.dependency_overrides.pop(get_ai_db, None)
        ai_session.close()
        client.close()
        console_env.engine.dispose()


def stored_row(env, sub: str) -> MemberPreference | None:
    """平台库直读偏好行（断言存储列被清/更新的落库面）。"""
    with env.platform.session() as sess:
        return sess.get(MemberPreference, sub)


# ---------------------------------------------------------------------------
# GET：新 sub 全 null（不 404）/ 未映射 401
# ---------------------------------------------------------------------------


def test_get_fresh_sub_returns_all_nulls_not_404(env):
    resp = env.client.get(PREFS_URL, headers=OWNER)
    assert resp.status_code == 200
    assert resp.json() == ALL_NULL
    # 只读不落行：未写过的 sub 不产生 member_preferences 行
    assert stored_row(env, SUB_OWNER) is None


def test_unmapped_identity_401(env):
    resp = env.client.get(PREFS_URL, headers=GHOST)
    assert resp.status_code == 401
    assert resp.json()["detail"] == UNMAPPED_DETAIL
    assert env.client.put(PREFS_URL, json={"ui_locale": "zh"}, headers=GHOST).status_code == 401


# ---------------------------------------------------------------------------
# 部分更新：缺键不动 / null 清空 / 空 body 零改动
# ---------------------------------------------------------------------------


def test_put_partial_update_and_null_clear(env):
    client = env.client
    resp = client.put(
        PREFS_URL, json={"ui_locale": "zh", "landing_tab": "artifacts"}, headers=OWNER
    )
    assert resp.status_code == 200
    # 只动一键：未携键不受影响
    body = client.put(PREFS_URL, json={"ui_locale": "en"}, headers=OWNER).json()
    assert body == {"ui_locale": "en", "landing_tab": "artifacts", "pinned_dashboard_id": None}
    # 显式 null 清空一键；另一键不动
    body2 = client.put(PREFS_URL, json={"ui_locale": None}, headers=OWNER).json()
    assert body2 == {"ui_locale": None, "landing_tab": "artifacts", "pinned_dashboard_id": None}
    # 空 body：全不动
    body3 = client.put(PREFS_URL, json={}, headers=OWNER).json()
    assert body3 == body2


def test_put_refreshes_updated_at_on_same_value_rewrite(env):
    """updated_at 每写刷新：同值重写也要推进（不依赖净变更检测）。"""
    client = env.client
    client.put(PREFS_URL, json={"ui_locale": "zh"}, headers=OWNER)
    before = stored_row(env, SUB_OWNER).updated_at
    time.sleep(0.002)
    client.put(PREFS_URL, json={"ui_locale": "zh"}, headers=OWNER)  # 同值重写
    after = stored_row(env, SUB_OWNER).updated_at
    assert after > before


# ---------------------------------------------------------------------------
# 值域：locale zh|en + 十页签合法环（含 workbench）；非法值 422 逐字文案
# ---------------------------------------------------------------------------


def test_ui_locale_domain_cycle(env):
    client = env.client
    for locale in ("zh", "en"):
        resp = client.put(PREFS_URL, json={"ui_locale": locale}, headers=OWNER)
        assert resp.status_code == 200
        assert resp.json()["ui_locale"] == locale
    resp = client.put(PREFS_URL, json={"ui_locale": "ja"}, headers=OWNER)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "ui_locale 仅支持 zh/en"


def test_landing_tab_domain_cycle(env):
    client = env.client
    tabs = ("workbench", "chat", "artifacts", "datasets", "entities", "packs", "demands", "usage", "keys", "team")
    for tab in tabs:
        resp = client.put(PREFS_URL, json={"landing_tab": tab}, headers=OWNER)
        assert resp.status_code == 200, tab
        assert resp.json()["landing_tab"] == tab
    for bad in ("admin", "home", "console", "settings", "CHAT"):
        resp = client.put(PREFS_URL, json={"landing_tab": bad}, headers=OWNER)
        assert resp.status_code == 422, bad
        assert resp.json()["detail"] == "landing_tab 必须是控制台页签之一"


# ---------------------------------------------------------------------------
# 置顶：happy path + 422 矩阵（他户与不存在同文案）
# ---------------------------------------------------------------------------


def test_pinned_happy_path(env):
    dashboard_id = env.artifacts["own_dashboard"]
    resp = env.client.put(
        PREFS_URL, json={"pinned_dashboard_id": dashboard_id}, headers=OWNER
    )
    assert resp.status_code == 200
    assert resp.json() == {"ui_locale": None, "landing_tab": None, "pinned_dashboard_id": dashboard_id}
    # draft 看板同样可置顶（契约只排除 archived）
    draft_id = seed_artifact(env.ai, status="draft", title="草稿看板")
    resp2 = env.client.put(
        PREFS_URL, json={"pinned_dashboard_id": draft_id}, headers=OWNER
    )
    assert resp2.status_code == 200
    assert resp2.json()["pinned_dashboard_id"] == draft_id


def test_pinned_422_matrix(env):
    client = env.client
    # 前置合法态（422 不得半写破坏既有值）
    client.put(PREFS_URL, json={"ui_locale": "zh", "landing_tab": "team"}, headers=OWNER)
    cases = [
        (999999, "置顶看板不存在"),  # 不存在
        (env.artifacts["other_dashboard"], "置顶看板不存在"),  # 他户（同文案，不泄露存在性）
        (env.artifacts["own_form"], "置顶对象不是看板"),  # kind != dashboard
        (env.artifacts["own_archived"], "置顶看板已归档，不能设为默认"),  # archived
    ]
    for artifact_id, detail in cases:
        resp = client.put(PREFS_URL, json={"pinned_dashboard_id": artifact_id}, headers=OWNER)
        assert resp.status_code == 422, (artifact_id, resp.text)
        assert resp.json()["detail"] == detail
    # 零半写：既有偏好原样
    assert client.get(PREFS_URL, headers=OWNER).json() == {
        "ui_locale": "zh", "landing_tab": "team", "pinned_dashboard_id": None,
    }


# ---------------------------------------------------------------------------
# per-sub 隔离 / viewer 可写自己的偏好
# ---------------------------------------------------------------------------


def test_per_sub_isolation_within_same_customer(env):
    client = env.client
    assert client.put(
        PREFS_URL, json={"ui_locale": "zh", "landing_tab": "datasets"}, headers=OWNER
    ).status_code == 200
    assert client.put(
        PREFS_URL, json={"ui_locale": "en", "landing_tab": "usage"}, headers=MEMBER
    ).status_code == 200
    # 各自回读各自值，互不可见（一人一行按 sub 隔离）
    assert client.get(PREFS_URL, headers=OWNER).json() == {
        "ui_locale": "zh", "landing_tab": "datasets", "pinned_dashboard_id": None,
    }
    assert client.get(PREFS_URL, headers=MEMBER).json() == {
        "ui_locale": "en", "landing_tab": "usage", "pinned_dashboard_id": None,
    }


def test_viewer_can_read_and_write_own_preferences(env):
    client = env.client
    resp = client.put(
        PREFS_URL, json={"ui_locale": "en", "landing_tab": "usage"}, headers=VIEWER
    )
    assert resp.status_code == 200  # 不因 viewer 只读门禁 403
    assert client.get(PREFS_URL, headers=VIEWER).json() == {
        "ui_locale": "en", "landing_tab": "usage", "pinned_dashboard_id": None,
    }
    # viewer 也可置顶自己客户的看板（个人 UI 指针，非客户业务数据）
    pinned = client.put(
        PREFS_URL, json={"pinned_dashboard_id": env.artifacts["own_dashboard"]},
        headers=VIEWER,
    )
    assert pinned.status_code == 200
    assert pinned.json()["pinned_dashboard_id"] == env.artifacts["own_dashboard"]


# ---------------------------------------------------------------------------
# 置顶指针自愈：归档/删除 → GET null 且清列；重置合法看板恢复
# ---------------------------------------------------------------------------


def test_self_heal_archived_pinned(env):
    client = env.client
    dashboard_id = env.artifacts["own_dashboard"]
    assert client.put(
        PREFS_URL, json={"pinned_dashboard_id": dashboard_id}, headers=OWNER
    ).status_code == 200
    # 看板被归档后 GET：返回 null 且同一请求清列
    artifact = env.ai.get(Artifact, dashboard_id)
    artifact.status = "archived"
    env.ai.commit()
    resp = client.get(PREFS_URL, headers=OWNER)
    assert resp.status_code == 200
    assert resp.json()["pinned_dashboard_id"] is None
    assert stored_row(env, SUB_OWNER).pinned_dashboard_id is None
    # 再 PUT 重置合法看板成功（自愈不留下坏行）
    fresh_id = seed_artifact(env.ai, title="新看板")
    resp2 = client.put(
        PREFS_URL, json={"pinned_dashboard_id": fresh_id}, headers=OWNER
    )
    assert resp2.status_code == 200
    assert resp2.json()["pinned_dashboard_id"] == fresh_id
    assert client.get(PREFS_URL, headers=OWNER).json()["pinned_dashboard_id"] == fresh_id


def test_self_heal_deleted_pinned(env):
    client = env.client
    dashboard_id = env.artifacts["own_dashboard"]
    client.put(PREFS_URL, json={"pinned_dashboard_id": dashboard_id}, headers=OWNER)
    # 看板被删除后 GET：同样自愈（产物行没了，不留死链）
    env.ai.delete(env.ai.get(Artifact, dashboard_id))
    env.ai.commit()
    body = client.get(PREFS_URL, headers=OWNER).json()
    assert body["pinned_dashboard_id"] is None
    assert stored_row(env, SUB_OWNER).pinned_dashboard_id is None
    # 其他键不受自愈影响：locale 仍在， healed GET 三键如实
    client.put(PREFS_URL, json={"ui_locale": "en"}, headers=OWNER)
    healed = client.get(PREFS_URL, headers=OWNER).json()
    assert healed == {"ui_locale": "en", "landing_tab": None, "pinned_dashboard_id": None}


# ---------------------------------------------------------------------------
# PUT 响应 = 写后全量态
# ---------------------------------------------------------------------------


def test_put_response_equals_post_write_state(env):
    client = env.client
    dashboard_id = env.artifacts["own_dashboard"]
    resp = client.put(
        PREFS_URL,
        json={"ui_locale": "zh", "landing_tab": "artifacts", "pinned_dashboard_id": dashboard_id},
        headers=OWNER,
    )
    assert resp.status_code == 200
    expected = {
        "ui_locale": "zh", "landing_tab": "artifacts", "pinned_dashboard_id": dashboard_id,
    }
    assert resp.json() == expected
    # 与紧随的 GET 逐字节一致
    assert client.get(PREFS_URL, headers=OWNER).json() == expected
    # 部分更新后同样：响应=写后态
    resp2 = client.put(PREFS_URL, json={"ui_locale": "en"}, headers=OWNER)
    assert resp2.json() == {**expected, "ui_locale": "en"}
