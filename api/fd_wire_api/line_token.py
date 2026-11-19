"""门 K 换发令牌的线侧离线校验（change converge-line-identity-and-keys 8.1）。

网关把客户的**词元** `sk-` 换成一枚 `aud`=本线（wire）、带 `source`、`exp` ≤600 秒
的 JWT（paas `gateway/line-token-exchange.js`）。本模块只做**离线**校验：公钥来自
本地 PEM 或 JWKS，**不回调网关、不逐请求问计费**——网关挂了只影响"换新票"，已经
换到手里的票照样能用。这是这条车道比"线直连计费服务"强的地方，也是
「别人也能只使用那条子线、而不和平台绑定」在 wire 侧的可执行形态。

与门 A 成员级断言（`assertion.py`）的三处差别，都不是笔误：

1. **TTL 上限 600 秒而不是 300**：门 K 说的是"这一小段时间内这个客户可以打这条
   线"，门 A 说的是"这一次代做"。容忍窗口本就不同。
2. **不做 `jti` 一次性**。同一枚票在有效期内会被多次调用复用；一次性会让第二次
   调用就 401。一次性只属于门 A（那里一枚票对应一次代做动作）。
3. **校验 `source`**。签名合法、`aud` 合法，但票是给别的线签的，仍然要拒——
   `source` 是"这次换票为哪条线发的"，既是计量归属的依据，也是跨线复用票的闸。

**拒绝一律给稳定原因码**，且对外只说"令牌不对"这一层（`wrong_source` 与
`wrong_audience` 在响应里同码，不告诉调用方是哪一步没过——门 A 的既有口径）。

样板来源：`fd-lex` `server/fd_lex_api/line_token.py`（8.2）。本文件是该家族的
第二份落点，**逻辑逐条对齐**，只改线的名字与文档；两份若分叉，以「门 K 票的
形状」为准——那是 paas 网关发的，不是线侧定的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

__all__ = [
    "MAX_TTL_SECONDS",
    "LineTokenIdentity",
    "LineTokenRejected",
    "LineTokenSettings",
    "CustomerResolver",
    "verify_line_token",
]

#: 门 K 令牌的有效期上限（秒）。网关侧同样硬夹 600，两边各守一次。
MAX_TTL_SECONDS = 600


class LineTokenRejected(Exception):
    """校验失败。``reason`` 是稳定码，供调用方与测试断言（不要断言文案）。"""

    NOT_CONFIGURED = "not_configured"
    TOKEN_MALFORMED = "token_malformed"
    BAD_SIGNATURE = "bad_signature"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    WRONG_SOURCE = "wrong_source"
    EXPIRED = "expired"
    TTL_TOO_LONG = "ttl_too_long"
    MISSING_CLAIM = "missing_claim"

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class LineTokenIdentity:
    """已校验的门 K 身份。

    ``subject`` 是网关解析出的**计费侧主体**（sub2api 用户 id 的字符串形态），
    **不是** Logto 用户名——两者的映射在库里（``billing_customers.sub2api_user_id``），
    所以这里只认这个键。叫错名字会让人去拿它查 Logto 表，然后得出"票是好的但
    解析不到客户"的错觉。
    """

    subject: str
    email: str | None
    scope: tuple[str, ...]
    source: str
    expires_at: int
    jti: str


class CustomerResolver(Protocol):
    """把计费侧主体解析成本线客户。

    刻意做成协议而不是在这里查库：校验模块保持**零依赖**（可离线单测，不需要
    数据库、不需要 app 上下文），落库查询由调用方注入。返回 ``None`` 表示"这个人
    本线没有客户记录"——**不得自动建档**（身份永不自动创建，与
    ``authorize_delegation`` 同一条铁律）。
    """

    def resolve(self, subject: str) -> Any | None: ...


@dataclass(frozen=True)
class LineTokenSettings:
    issuer: str
    line: str
    public_key_pem: str = ""
    jwks_uri: str = ""
    algorithms: tuple[str, ...] = ("ES256", "RS256", "EdDSA")
    max_ttl_seconds: int = MAX_TTL_SECONDS

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.line and (self.jwks_uri or self.public_key_pem))


def verify_line_token(
    token: str,
    *,
    settings: LineTokenSettings,
    jwks_client_factory: Callable[[str], Any] | None = None,
) -> LineTokenIdentity:
    """离线校验一枚门 K 令牌。失败一律抛 :class:`LineTokenRejected`。

    顺序与门 A 一致：先验签（最贵的一步先做，后面每步都便宜），再判 iss/aud，
    再判 ``source``，最后判有效期窗口。**不写任何状态**——门 K 的票可复用，
    所以这里没有重放台账（``now`` 保留给调用方注入时钟，判 TTL 用）。
    """
    if not settings.configured:
        raise LineTokenRejected(LineTokenRejected.NOT_CONFIGURED, "issuer/line/key incomplete")
    if not isinstance(token, str) or not token.strip():
        raise LineTokenRejected(LineTokenRejected.TOKEN_MALFORMED, "empty token")

    try:
        import jwt as pyjwt
    except ImportError as exc:  # pragma: no cover - 依赖缺失属部署错误
        raise LineTokenRejected(LineTokenRejected.NOT_CONFIGURED, "PyJWT not installed") from exc

    key: Any
    if settings.public_key_pem:
        key = settings.public_key_pem
    elif settings.jwks_uri and jwks_client_factory is not None:
        try:
            key = jwks_client_factory(settings.jwks_uri).get_signing_key_from_jwt(token).key
        except Exception as exc:
            raise LineTokenRejected(
                LineTokenRejected.BAD_SIGNATURE, f"jwks lookup failed: {type(exc).__name__}"
            ) from exc
    else:
        raise LineTokenRejected(LineTokenRejected.NOT_CONFIGURED, "no verification key source")

    try:
        claims = pyjwt.decode(
            token,
            key,
            algorithms=list(settings.algorithms),
            issuer=settings.issuer,
            audience=settings.line,
            options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise LineTokenRejected(LineTokenRejected.EXPIRED) from exc
    except pyjwt.InvalidAudienceError as exc:
        raise LineTokenRejected(LineTokenRejected.WRONG_AUDIENCE, f"expected {settings.line}") from exc
    except pyjwt.InvalidIssuerError as exc:
        raise LineTokenRejected(LineTokenRejected.WRONG_ISSUER, f"expected {settings.issuer}") from exc
    except pyjwt.InvalidSignatureError as exc:
        # 必须排在 DecodeError 之前：InvalidSignatureError 是 DecodeError 的子类，
        # 顺序反了会把它报成 token_malformed —— 原因码指错地方，排查就会往"票格式"
        # 上跑，而真问题是签错了钥（lex 侧 2026-11-12 由跨线用例撞出，此处照抄）。
        raise LineTokenRejected(LineTokenRejected.BAD_SIGNATURE, type(exc).__name__) from exc
    except pyjwt.DecodeError as exc:
        raise LineTokenRejected(LineTokenRejected.TOKEN_MALFORMED) from exc
    except Exception as exc:  # 算法不符等
        raise LineTokenRejected(LineTokenRejected.BAD_SIGNATURE, type(exc).__name__) from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise LineTokenRejected(LineTokenRejected.MISSING_CLAIM, "sub")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti.strip():
        raise LineTokenRejected(LineTokenRejected.MISSING_CLAIM, "jti")
    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, int):
        raise LineTokenRejected(LineTokenRejected.MISSING_CLAIM, "exp")

    if isinstance(iat, int) and exp - iat > settings.max_ttl_seconds:
        # PyJWT 只管"有没有过期"，不管"给的时间太长"——短命约束要线侧自己守。
        raise LineTokenRejected(
            LineTokenRejected.TTL_TOO_LONG, f"{exp - iat}s > {settings.max_ttl_seconds}s"
        )

    # source 是"这张票为哪条线发的"。签到本线才算数：签名与 aud 都合法但票是
    # 别的线的，照样拒（它会把别线的计量记到这里，也是跨线复用票的入口）。
    source = claims.get("source")
    if not isinstance(source, str) or not source.strip():
        raise LineTokenRejected(LineTokenRejected.MISSING_CLAIM, "source")
    if source.strip() != settings.line:
        raise LineTokenRejected(LineTokenRejected.WRONG_SOURCE, f"expected {settings.line}")

    scope_value = claims.get("scope") or ""
    scope = tuple(str(scope_value).split()) if scope_value else ()
    email = claims.get("email")
    return LineTokenIdentity(
        subject=subject.strip(),
        email=str(email) if isinstance(email, str) and email else None,
        scope=scope,
        source=source.strip(),
        expires_at=exp,
        jti=jti,
    )
