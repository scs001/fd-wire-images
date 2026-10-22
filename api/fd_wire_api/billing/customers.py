"""客户开户：Customer 行 + sub2api 代建账户（幂等）。"""

from __future__ import annotations

import hashlib
import re
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Customer, CustomerStatus
from .plans import get_plan
from .sub2api import Sub2ApiClient


def _default_idempotency_key(name: str) -> str:
    """缺省幂等键：确定性派生（重试同键）且恒 ASCII（Idempotency-Key 是
    HTTP 头，中文名直接上头会被 httpx 以 UnicodeEncodeError 拒绝）。"""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return f"customer:{digest}"


def _default_username(name: str) -> str:
    """客户名 → 可打印 ASCII 用户名（中文名降级为散列短 slug）。"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if len(slug) >= 3:
        return slug[:64]
    return "wire-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def _default_email(name: str, plan_code: str) -> str:
    """未给邮箱时的确定性占位（运营侧最好传客户真实邮箱以对齐 SSO）。"""
    digest = hashlib.sha256(f"{name}:{plan_code}".encode("utf-8")).hexdigest()[:16]
    return f"customer-{digest}@customers.wire.internal"


def ensure_customer(
    session: Session,
    sub2api: Sub2ApiClient,
    *,
    name: str,
    plan_code: str,
    email: str | None = None,
    username: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[Customer, bool]:
    """建客户（幂等）：已存在（同名，任意档位）直接返回；否则 sub2api
    认领/建户后落库。

    同名守卫（wire-console-resilience D3）：查重键从 ``(name, plan_id)``
    放宽为单 ``name``——跨档同名即幂等复用既有行，不再造第二行
    （``billing_customers.name`` 唯一索引前的行为防线；``logto_sub``
    由 onboarding 终态步骤写入既有行）。
    sub2api 真契约（2026-10-09 实测）：email 必填唯一、create 面无幂等头——
    重复建户由 find_user_by_email 前置消解；password 仅满足建户约束不留存。
    缺省幂等键按客户名确定性派生且恒 ASCII。
    """
    plan = get_plan(session, plan_code)
    existing = session.execute(
        select(Customer).where(Customer.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    key = idempotency_key or _default_idempotency_key(f"{name}:{plan.code}")
    resolved_email = (email or "").strip() or _default_email(name, plan.code)
    user = sub2api.find_user_by_email(resolved_email)
    if user is None:
        user = sub2api.create_user(
            email=resolved_email,
            password=secrets.token_urlsafe(24),
            username=(username or "").strip() or _default_username(name),
            notes=f"wire customer: {name} (plan={plan.code})",
            idempotency_key=key,
        )

    # 同键并发重试：对手可能已插入同 sub2api_user_id 的行
    dup = session.execute(
        select(Customer).where(Customer.sub2api_user_id == user.id)
    ).scalar_one_or_none()
    if dup is not None:
        return dup, False

    customer = Customer(
        name=name,
        sub2api_user_id=user.id,
        plan_id=plan.id,
        status=CustomerStatus.ACTIVE.value,
    )
    try:
        with session.begin_nested():
            session.add(customer)
            session.flush()
    except IntegrityError:
        # 唯一索引兜底（name 或 sub2api_user_id 冲突）：同名（跨档）幂等
        # 返回既有行；非同名冲突如实上抛。
        existing = session.execute(
            select(Customer).where(Customer.name == name)
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
    return customer, True
