"""词元客户轨的**接线**（converge-line-identity-and-keys 8.1）。

`test_line_token.py` 证的是「这枚票本身合不合法」；本文件证的是**票真的能走进
路由**——两条必须分开测，因为规则层全绿而接线层断是这类改动最典型的假绿
（依赖没换到、或在另一个依赖上重复判了一次 Logto）。

被测的接缝只有一处：`console.auth.resolve_actor()`。它同时供
`require_console_customer` / `require_console_customer_actor` /
`require_console_customer_id`（约 20 个路由模块共用）与 data-plane 的
`require_data_plane_principal` 使用——所以这里一个 200 就代表整面通了，
但要另外钉住三条边界：写 scope、密钥物面、以及「未配置时行为逐字节不变」。
"""

from __future__ import annotations

import pytest
from console_fixture import _rsa_keypair
from test_datasets_common import CUST_HEADERS, datasets_env  # noqa: F401
from test_line_token import ISSUER, _public_pem, mint_line_token

ISSUER_ENV = "FD_WIRE_LINE_TOKEN_ISSUER"
LINE_ENV = "FD_WIRE_LINE_TOKEN_LINE"
PEM_ENV = "FD_WIRE_LINE_TOKEN_PUBLIC_KEY_PEM"

#: 种子客户 cust-001 的计费侧主体（`make_console_env` 按 `caller-<n>` 落库）
SUB2API_CUST_001 = "caller-1"


@pytest.fixture()
def line_env(monkeypatch):
    """把线侧配成「词元轨可用」——这是生产注入 Secret 之后的形状。"""
    monkeypatch.setenv(ISSUER_ENV, ISSUER)
    monkeypatch.setenv(LINE_ENV, "wire")
    monkeypatch.setenv(PEM_ENV, _public_pem())
    return monkeypatch


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


DEMAND_BODY = {
    "description": "词元轨接线用例：公开源需求单",
    "contact_email": "line-track@example.com",
    "source_urls": ["https://example.com/dataset"],
    "public_data_asserted": True,
}


def test_line_token_reaches_console_route(datasets_env, line_env):  # noqa: F811
    """词元票（客户级）→ 需求单列表 200，且**只看见本客户那条**。

    200 单独还不够：必须确认解析到的是 cust-001，而不是"某个能过的客户"——
    所以先用两个客户的 Logto 身份各建一条，再用 cust-001 的词元票读，只许看见自己那条。
    """
    mine = datasets_env.client.post(
        "/api/v1/demands", headers=CUST_HEADERS["cust-001"], json=DEMAND_BODY
    )
    assert mine.status_code in (200, 201), mine.text
    other = datasets_env.client.post(
        "/api/v1/demands", headers=CUST_HEADERS["cust-002"], json=DEMAND_BODY
    )
    assert other.status_code in (200, 201), other.text

    token = mint_line_token(sub=SUB2API_CUST_001)
    resp = datasets_env.client.get("/api/v1/demands", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert isinstance(items, list)
    # 需求单库是 cwd 下的共享文件（fixture 只隔离平台库），历次用例会留行——
    # 所以断言必须与顺序无关：自己那条在、别家那条不在、且**每条都属于 cust-001**。
    ids = [d["id"] for d in items]
    assert mine.json()["id"] in ids
    assert other.json()["id"] not in ids
    assert {d["customer_id"] for d in items} == {"cust-001"}


def test_line_token_reaches_data_plane_route(datasets_env, line_env):  # noqa: F811
    """同一枚票也要能打数据集面（channel=line-token，不做成员级判定）。"""
    token = mint_line_token(sub=SUB2API_CUST_001)
    resp = datasets_env.client.get("/api/v1/datasets", headers=_bearer(token))
    assert resp.status_code == 200, resp.text


def test_unknown_subject_is_403_and_creates_no_customer(datasets_env, line_env):  # noqa: F811
    """票验过但本线没这个客户 → 403 unknown_identity，**不建行**。"""
    token = mint_line_token(sub="caller-does-not-exist")
    resp = datasets_env.client.get("/api/v1/demands", headers=_bearer(token))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["reason"] == "unknown_identity"

    from fd_wire_api.admin.db import get_session

    from fd_wire_api.billing.models import Customer

    with get_session() as sess:
        assert (
            sess.query(Customer).filter(Customer.sub2api_user_id == "caller-does-not-exist").count()
            == 0
        )


def test_read_only_scope_cannot_write(datasets_env, line_env):  # noqa: F811
    """没有 `line:write` 的票按 viewer 解析：变更类方法 403（与成员 viewer 同门禁）。

    否则半枚票（line:read）在写面上等于一把客户级写钥匙。
    """
    token = mint_line_token(sub=SUB2API_CUST_001, scope="line:read")
    resp = datasets_env.client.post(
        "/api/v1/demands", headers=_bearer(token), json={"title": "x"}
    )
    assert resp.status_code == 403, resp.text
    assert "viewer" in resp.text


def test_write_scope_passes_viewer_gate(datasets_env, line_env):  # noqa: F811
    """有 `line:write` 就不该被 viewer 门拒（业务校验另说，但**不是** 403 权限面）。"""
    token = mint_line_token(sub=SUB2API_CUST_001, scope="line:read line:write")
    resp = datasets_env.client.post(
        "/api/v1/demands", headers=_bearer(token), json={"title": "x"}
    )
    assert resp.status_code != 403, resp.text


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_key_surface_is_owner_only_for_line_tokens(datasets_env, line_env, method):  # noqa: F811
    """密钥物面（铸造/吊销/列出数据面 key）→ 403。

    词元票是**短命**客户级凭据；允许它铸 `wdk-` 长命钥匙等于把短命约束绕开
    （lex 侧 8.2 用 `line:admin` + consent 行守同一件事，这里直接用「非 owner」）。
    """
    token = mint_line_token(sub=SUB2API_CUST_001, scope="line:read line:write")
    kw = {"headers": _bearer(token)}
    if method == "POST":
        kw["json"] = {"label": "from-line-token", "dataset_ids": []}
    resp = datasets_env.client.request(method, "/api/v1/data-plane-keys", **kw)
    assert resp.status_code == 403, resp.text
    assert "owner" in resp.text


def test_unconfigured_line_track_is_byte_identical_to_logto_only(datasets_env, monkeypatch):  # noqa: F811
    """未配置 `FD_WIRE_LINE_TOKEN_*` 时，坏凭据看到的是**原来那条** Logto 401。

    这条钉的是「新轨不改变老客户端看到的错误」：状态码与 detail 都必须与
    改动前一致，否则"多了一条轨"这件事会以错误文案的形状漏给所有人。
    """
    for env in (ISSUER_ENV, LINE_ENV, PEM_ENV):
        monkeypatch.delenv(env, raising=False)

    garbage = "definitely-not-a-token"
    got = datasets_env.client.get("/api/v1/demands", headers=_bearer(garbage))
    plain = datasets_env.client.get(
        "/api/v1/demands", headers={"Authorization": f"Bearer {garbage}"}
    )
    assert got.status_code == plain.status_code == 401
    assert got.json() == plain.json()

    # 与此同时：一枚**合法**的门 K 票也不该被认（这条路不存在）
    token = mint_line_token(sub=SUB2API_CUST_001)
    assert datasets_env.client.get("/api/v1/demands", headers=_bearer(token)).status_code == 401


def test_logto_route_still_works_when_line_track_enabled(datasets_env, line_env):  # noqa: F811
    """开了词元轨不影响 Logto：老路径必须照旧 200（8.1 的「保留原有轨」）。"""
    resp = datasets_env.client.get("/api/v1/demands", headers=CUST_HEADERS["cust-001"])
    assert resp.status_code == 200, resp.text


def test_line_track_ignores_wrong_line_token(datasets_env, line_env, monkeypatch):  # noqa: F811
    """别的线的票（source=lex）在 wire 上必须 401，而且回的是 Logto 原文案。"""
    token = mint_line_token(sub=SUB2API_CUST_001, source="lex")
    resp = datasets_env.client.get("/api/v1/demands", headers=_bearer(token))
    assert resp.status_code == 401
