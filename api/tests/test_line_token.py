"""门 K 换发令牌的离线校验（converge-line-identity-and-keys 8.1）。

只测 `fd_wire_api.line_token` 本身：不装数据库、不起 app、不联网。
接线（票进来能不能过到路由）在 `test_line_token_track.py`，两层分开——
规则层绿而接线层断，是这类改动最常见的假绿。

断言一律认**稳定原因码**（`LineTokenRejected.reason`），不认文案：
文案会随人改，原因码是契约。
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from console_fixture import _rsa_keypair

from fd_wire_api.line_token import (
    MAX_TTL_SECONDS,
    LineTokenRejected,
    LineTokenSettings,
    verify_line_token,
)

ISSUER = "https://platform.finddatatech.cloud"
LINE = "wire"


def _public_pem(tag: str = "line") -> str:
    from cryptography.hazmat.primitives import serialization

    _, public_key = _rsa_keypair(tag)
    return public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _settings(**over) -> LineTokenSettings:
    base = dict(issuer=ISSUER, line=LINE, public_key_pem=_public_pem(), max_ttl_seconds=MAX_TTL_SECONDS)
    base.update(over)
    return LineTokenSettings(**base)


def mint_line_token(
    *,
    sub: str = "caller-1",
    aud: str = LINE,
    source: str = LINE,
    iss: str = ISSUER,
    scope: str = "line:read line:write",
    ttl: int = 300,
    email: str | None = None,
    tag: str = "line",
    drop: tuple[str, ...] = (),
    key=None,
) -> str:
    private_key, _ = _rsa_keypair(tag)
    now = int(time.time())
    payload = {
        "iss": iss,
        "aud": aud,
        "source": source,
        "sub": sub,
        "jti": f"jti-{sub}-{now}",
        "scope": scope,
        "iat": now,
        "exp": now + ttl,
    }
    if email:
        payload["email"] = email
    for claim in drop:
        payload.pop(claim, None)
    return pyjwt.encode(payload, key or private_key, algorithm="RS256")


def test_valid_token_yields_identity():
    ident = verify_line_token(mint_line_token(email="a@b.c"), settings=_settings())
    assert ident.subject == "caller-1"
    assert ident.email == "a@b.c"
    assert ident.scope == ("line:read", "line:write")
    assert ident.source == LINE
    assert ident.jti


def test_not_configured_when_issuer_or_key_missing():
    for settings in (
        _settings(issuer=""),
        _settings(public_key_pem=""),
        _settings(line=""),
    ):
        with pytest.raises(LineTokenRejected) as exc:
            verify_line_token(mint_line_token(), settings=settings)
        assert exc.value.reason == LineTokenRejected.NOT_CONFIGURED


def test_empty_token_is_malformed():
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token("   ", settings=_settings())
    assert exc.value.reason == LineTokenRejected.TOKEN_MALFORMED


def test_garbage_token_is_malformed():
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token("not-a-jwt", settings=_settings())
    assert exc.value.reason == LineTokenRejected.TOKEN_MALFORMED


def test_signed_by_another_key_is_bad_signature():
    """原因码必须指「签错了钥」，不能报成 token_malformed。

    PyJWT 的 `InvalidSignatureError` 是 `DecodeError` 的子类，except 顺序反了
    就会把它吞成 token_malformed —— 排查会往"票格式"上跑，而真问题是钥匙不对
    （lex 侧 2026-11-12 由跨线用例撞出，本仓同款判据）。
    """
    _, _ = _rsa_keypair("line")
    other_private, _ = _rsa_keypair("line-other")
    token = mint_line_token(key=other_private)
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token(token, settings=_settings())
    assert exc.value.reason == LineTokenRejected.BAD_SIGNATURE


def test_wrong_issuer_rejected():
    token = mint_line_token(iss=ISSUER + "/wrong")
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token(token, settings=_settings())
    assert exc.value.reason == LineTokenRejected.WRONG_ISSUER


def test_wrong_audience_rejected():
    token = mint_line_token(aud="lex")
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token(token, settings=_settings())
    assert exc.value.reason == LineTokenRejected.WRONG_AUDIENCE


def test_other_line_source_rejected_even_with_right_audience():
    """跨线复用票的闸：签名与 aud 都合法、但票是给别的线发的，必须拒。

    这条是计量归属的边界——放过去就会把 lex 的用量记到 wire 头上。
    """
    token = mint_line_token(source="lex")
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token(token, settings=_settings())
    assert exc.value.reason == LineTokenRejected.WRONG_SOURCE


def test_expired_rejected():
    token = pyjwt.encode(
        {
            "iss": ISSUER,
            "aud": LINE,
            "source": LINE,
            "sub": "caller-1",
            "jti": "j1",
            "scope": "line:read",
            "iat": int(time.time()) - 1200,
            "exp": int(time.time()) - 600,
        },
        _rsa_keypair("line")[0],
        algorithm="RS256",
    )
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token(token, settings=_settings())
    assert exc.value.reason == LineTokenRejected.EXPIRED


def test_ttl_over_ceiling_rejected():
    """PyJWT 只管有没有过期，不管给的时间太长——短命约束要线侧自己守。"""
    token = mint_line_token(ttl=MAX_TTL_SECONDS + 1)
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token(token, settings=_settings())
    assert exc.value.reason == LineTokenRejected.TTL_TOO_LONG


@pytest.mark.parametrize("claim", ["sub", "jti", "exp", "source"])
def test_missing_claim_rejected(claim):
    token = mint_line_token(drop=(claim,))
    with pytest.raises(LineTokenRejected) as exc:
        verify_line_token(token, settings=_settings())
    assert exc.value.reason == LineTokenRejected.MISSING_CLAIM


def test_jwks_uri_path_works():
    """PEM 为空时走 JWKS：工厂注入假件（离线、不联网）。"""
    from types import SimpleNamespace

    _, public_key = _rsa_keypair("line")
    settings = _settings(public_key_pem="", jwks_uri=ISSUER + "/jwks")
    ident = verify_line_token(
        mint_line_token(),
        settings=settings,
        jwks_client_factory=lambda url: SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key)
        ),
    )
    assert ident.subject == "caller-1"


def test_scope_is_split_on_whitespace():
    ident = verify_line_token(mint_line_token(scope="line:read   line:write"), settings=_settings())
    assert ident.scope == ("line:read", "line:write")


def test_empty_scope_is_empty_tuple():
    ident = verify_line_token(mint_line_token(scope=""), settings=_settings())
    assert ident.scope == ()
