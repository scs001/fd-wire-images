"""控制台端点（wire-self-serve-launch 4.2/4.3/6.1）。

- ``GET  /api/console/auth/config``：登录配置运行时下发（未配置如实
  ``configured=false``，前端呈现阻塞态、不发起假登录）。
- ``POST /api/console/session``：body ``{"id_token"}`` → 本地校验后查
  logto_sub 映射；无映射则内联触发首登开户 ceremony（onboarding.py，
  免费档跳过 cell 配给）。成功 200 ``{"status":"ready","customer":{...},
  "key_once":"wgk-…"}``（key_once 仅本次完成开户非 null）；外部通道失败
  502 ``{"status":"failed","reason":str}``；令牌无效 401。wire-team-spaces：
  body 可携 ``invite_code``（新身份兑换入队，见 team.py redeem_invite）。
- ``GET  /api/console/usage``：Bearer 身份 → 当月已用/含额/余量（与额度
  查询、admin usage 聚合同源，billing.routes.monthly_used_lines）。

会话：平台库（admin 域同源）；sub2api 客户端请求级构造（测试以
dependency_overrides 注入假件）。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..admin.db import get_db as get_console_db
from ..billing.routes import current_period, monthly_used_lines
from ..billing.sub2api import Sub2ApiClient, Sub2ApiError
from . import auth as console_auth
from .onboarding import OnboardingError, onboard_console_customer
from .team import InviteRedeemError, redeem_invite

router = APIRouter(tags=["console"])


def get_console_sub2api() -> Iterator[Sub2ApiClient]:
    """sub2api 客户端请求级依赖（用完即关；线程不共享）。"""
    client = Sub2ApiClient()
    try:
        yield client
    finally:
        client.close()


def require_console_customer(
    request: Request, db: Session = Depends(get_console_db)
) -> object:
    """Bearer ID token → 映射客户（console_auth.require_console_customer 的
    会话绑定版：平台库会话经本模块依赖注入，测试可覆写）。"""
    return console_auth.require_console_customer(request, db)

class IdTokenIn(BaseModel):
    """首登/换会话请求体（ID token 由 SPA 经 Logto PKCE 换得，只发同源）。

    ``invite_code``（wire-team-spaces）：新 sub 携有效码加入既有客户为
    成员（不建新客户/不动 sub2api/不铸 key）；缺省走原首登开户路径。
    """

    id_token: str = Field(min_length=1)
    invite_code: str | None = None


class SessionCustomerOut(BaseModel):
    id: int
    name: str
    plan_code: str


class SessionReadyOut(BaseModel):
    status: str = "ready"
    customer: SessionCustomerOut
    key_once: str | None = None


class SessionFailedOut(BaseModel):
    status: str = "failed"
    reason: str


class UsageOut(BaseModel):
    """客户自助用量（对外契约逐字：period/plan_code/included/used/remaining）。"""

    period: str
    plan_code: str
    included_lines: int
    used_lines: int
    remaining_lines: int


@router.get("/api/console/auth/config")
def console_auth_config() -> dict:
    """登录配置下发（无鉴权；未配置时前端阻塞态）。"""
    return console_auth.auth_config()


@router.post(
    "/api/console/session",
    response_model=SessionReadyOut,
    responses={502: {"model": SessionFailedOut}},
)
def console_session(
    payload: IdTokenIn,
    db: Session = Depends(get_console_db),
    sub2api: Sub2ApiClient = Depends(get_console_sub2api),
) -> SessionReadyOut:
    """首登自动开户 / 已开户换会话（身份来自 body 内 ID token，本地校验）。

    失败 502 的响应体是顶层 ``{"status":"failed","reason":...}``（对外契约
    逐字；故用 JSONResponse 直出而非 HTTPException——后者会把 body 包进
    ``detail`` 键）。

    wire-team-spaces：body 携 ``invite_code`` 时走邀请兑换分支——sub 已有
    归属（owner 或成员）诚实拒 502；码无效（不存在/已用/过期）502 带原因；
    有效则同事务入队（member.joined）返回 ready（customer=邀请方，
    key_once=null）。无码路径行为与既有逐字节一致。
    """
    identity = console_auth.verify_id_token(payload.id_token)
    if payload.invite_code:
        try:
            customer = redeem_invite(db, sub=identity.sub, code=payload.invite_code)
        except InviteRedeemError as exc:
            failed = SessionFailedOut(reason=str(exc))
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY, content=failed.model_dump()
            )
        plan = customer.plan
        return SessionReadyOut(
            status="ready",
            customer=SessionCustomerOut(
                id=customer.id,
                name=customer.name,
                plan_code=plan.code if plan else "",
            ),
            key_once=None,
        )
    try:
        outcome = onboard_console_customer(
            db,
            sub2api,
            sub=identity.sub,
            display_name=identity.email or identity.sub,
        )
    except Sub2ApiError as exc:
        failed = SessionFailedOut(reason=f"sub2api 代建账户失败: {exc}")
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content=failed.model_dump())
    except OnboardingError as exc:
        failed = SessionFailedOut(reason=f"registry 授权面失败: {exc}")
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content=failed.model_dump())
    customer = outcome.customer
    plan = customer.plan
    return SessionReadyOut(
        status="ready",
        customer=SessionCustomerOut(
            id=customer.id,
            name=customer.name,
            plan_code=plan.code if plan else "",
        ),
        key_once=outcome.key_once,
    )


@router.get("/api/console/usage", response_model=UsageOut)
def console_usage(
    customer=Depends(require_console_customer),
    db: Session = Depends(get_console_db),
) -> UsageOut:
    """登录客户当月已用/含额/余量（身份仅来自 Bearer，天然跨客户隔离）。"""
    plan = customer.plan
    included = plan.included_lines if plan else 0
    period = current_period()
    used = monthly_used_lines(db, customer.sub2api_user_id, period)
    return UsageOut(
        period=period,
        plan_code=plan.code if plan else "",
        included_lines=included,
        used_lines=used,
        remaining_lines=max(0, included - used),
    )
