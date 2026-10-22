"""内部后台鉴权（wire-self-serve-launch 5.1 admin 双闸）：

1. **X-Admin-Token 服务闸**（原样保留）：与 env ``FD_WIRE_ADMIN_TOKEN``
   常量时间比对。fail-closed 纪律（与 metering ingest 令牌同款）：
   env 未配置（缺省/空串）→ 一律 403，绝不无鉴权放行；缺失/不匹配 → 401。
2. **Logto Bearer 角色闸**（叠加）：有效 ID token（本地校验，见
   console/auth.py）且 ``roles`` claim 含 env ``FD_WIRE_ADMIN_ROLE``
   （默认 ``wire-admin``）；由 env ``FD_WIRE_ADMIN_BEARER_GATE`` 控制
   （默认 on；``"off"`` 仅测试/开发用，退回旧行为=仅 X-Admin-Token）。

裁决矩阵（gate=on）：有角色无令牌 → 401（缺 X-Admin-Token）；有令牌无角色
→ 403；令牌无效 → 401；双全 → 放行；Logto 未配置 → 503（fail-closed，
绝不因未配置而只靠单闸放行）。
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, Request, status

from ..console.auth import verify_id_token

#: 角色闸开关 env（"off" 仅测试/开发用）
BEARER_GATE_ENV = "FD_WIRE_ADMIN_BEARER_GATE"
DEFAULT_ADMIN_ROLE = "wire-admin"


def bearer_gate_enabled() -> bool:
    """FD_WIRE_ADMIN_BEARER_GATE（默认 on；仅字面 off/false/0 关闭）。"""
    return os.getenv(BEARER_GATE_ENV, "on").strip().lower() not in {"off", "false", "0"}


def admin_role() -> str:
    """要求的管理员角色（FD_WIRE_ADMIN_ROLE，默认 wire-admin）。"""
    return os.getenv("FD_WIRE_ADMIN_ROLE", DEFAULT_ADMIN_ROLE).strip() or DEFAULT_ADMIN_ROLE


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_admin_access(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """全部 /internal/admin/* 端点的双闸鉴权依赖。"""
    # ---- 第一道：X-Admin-Token（原样保留） ---------------------------------
    expected = os.getenv("FD_WIRE_ADMIN_TOKEN")
    if not expected:
        # 未配置令牌：拒绝一切访问而不是无鉴权放行（fail-closed）
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin token not configured (FD_WIRE_ADMIN_TOKEN)",
        )
    if not x_admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-Admin-Token（内部后台鉴权头）",
        )
    if not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 X-Admin-Token",
        )

    # ---- 第二道：Logto Bearer 角色闸（gate 控制，默认 on） -----------------
    if not bearer_gate_enabled():
        return
    token = _bearer_token(request)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin 双闸：缺少 Bearer ID token（须携带含管理角色的登录令牌）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    identity = verify_id_token(token)
    if not identity.has_role(admin_role()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"admin 角色 {admin_role()!r} 要求未满足（roles claim 不含该角色）"
            ),
        )


def require_admin_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """仅 X-Admin-Token 单闸（历史入口保留；路由已切双闸 require_admin_access）。"""
    expected = os.getenv("FD_WIRE_ADMIN_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin token not configured (FD_WIRE_ADMIN_TOKEN)",
        )
    if not x_admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-Admin-Token（内部后台鉴权头）",
        )
    if not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 X-Admin-Token",
        )
