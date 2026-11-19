"""商留提交的 service 层（add-contact-intake 任务 1.2）：校验 + 防灌水 + 落库。

契约（openspec contact-intake）：
- honeypot ``website`` 非空 → 400 拒绝（不落库、不提示是 honeypot、不提示
  原因——机器人闸的措辞必须与普通校验失败无异）；
- 四字段手工校验（name 必填 ≤80 / email 必填形态 ≤120 / company 可选 ≤120 /
  message 必填 ≤2000）→ 400 逐条如实定位（``detail`` 为 FastAPI 风格数组，
  ``loc=["body", 字段]``，``msg`` 为语言中性代码——前端按 loc+code 本地化）；
- 防灌水闸：同 ip_hash 24h ≤5、同 email 24h ≤3（DB 计数），超限 429 如实
  （带 Retry-After），被拒请求**不落库、不计数**；
- 来源 IP 只以 SHA-256 截断哈希（16 hex）落库——原始 IP 不落库，响应不回显
  ip_hash（红线）；
- 成功：落库（status=new）+ 提交后由路由层触发 fire-and-forget 运营通知。
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ContactMessage, utc_now_naive
from .schemas import ContactIntakeIn

#: 邮箱最小形态校验（与 web DemandNewView 的 EMAIL_RE 同口径——非空、恰一个
#: @、@ 前后无空白、域名带点）。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: 字段上限（spec 公开提交契约的冻结数值）
NAME_MAX = 80
EMAIL_MAX = 120
COMPANY_MAX = 120
MESSAGE_MAX = 2000

#: 防灌水闸（spec：同 ip_hash 24h ≤5、同 email 24h ≤3）
_WINDOW_HOURS = 24
_IP_WINDOW_LIMIT = 5
_EMAIL_WINDOW_LIMIT = 3

#: 429 的 Retry-After 提示秒数（一个窗口，粒度如实即可）
_RETRY_AFTER_SECONDS = _WINDOW_HOURS * 3600


def ip_hash_of(source_ip: str) -> str:
    """来源 IP → SHA-256 截断哈希（16 hex）——原始 IP 绝不落库（红线）。"""
    return hashlib.sha256(source_ip.encode("utf-8")).hexdigest()[:16]


def _field_error(field: str, code: str) -> dict:
    """一条 400 校验错误（FastAPI 422 形状；msg 为语言中性代码）。"""
    return {"loc": ["body", field], "msg": code, "type": "value_error"}


def _validation(errors: list[dict]) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=errors)


def _validate_fields(payload: ContactIntakeIn) -> tuple[str, str, str | None, str]:
    """四字段手工校验：返回 (name, email, company, message)；非法即 400。

    错误逐条收集（一次回显全部问题，不挤牙膏）；msg 是语言中性代码
    （required / invalid / too_long），前端按 loc+code 本地化呈现。
    """
    errors: list[dict] = []

    name = (payload.name or "").strip()
    if not name:
        errors.append(_field_error("name", "required"))
    elif len(name) > NAME_MAX:
        errors.append(_field_error("name", "too_long"))

    email = (payload.email or "").strip()
    if not email:
        errors.append(_field_error("email", "required"))
    elif len(email) > EMAIL_MAX:
        errors.append(_field_error("email", "too_long"))
    elif not _EMAIL_RE.match(email):
        errors.append(_field_error("email", "invalid"))

    company = (payload.company or "").strip()
    if company and len(company) > COMPANY_MAX:
        errors.append(_field_error("company", "too_long"))

    message = (payload.message or "").strip()
    if not message:
        errors.append(_field_error("message", "required"))
    elif len(message) > MESSAGE_MAX:
        errors.append(_field_error("message", "too_long"))

    if errors:
        raise _validation(errors)
    return name, email, (company or None), message


def _gate_or_raise(db: Session, ip_hash: str, email: str) -> None:
    """防灌水闸：ip_hash/email 双维度 24h 窗口 DB 计数，超限 429。

    被拒请求在此**提前抛出**——不落库，也不计入后续配额（计数只看已
    落库行）。email 计数按不区分大小写口径（访客大小写笔误不绕开闸）。
    """
    window_start = utc_now_naive() - timedelta(hours=_WINDOW_HOURS)
    ip_count = db.execute(
        select(func.count())
        .select_from(ContactMessage)
        .where(
            ContactMessage.ip_hash == ip_hash,
            ContactMessage.created_at >= window_start,
        )
    ).scalar_one()
    if ip_count >= _IP_WINDOW_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
        )
    email_count = db.execute(
        select(func.count())
        .select_from(ContactMessage)
        .where(
            func.lower(ContactMessage.email) == email.lower(),
            ContactMessage.created_at >= window_start,
        )
    ).scalar_one()
    if email_count >= _EMAIL_WINDOW_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
        )


def intake(
    db: Session, payload: ContactIntakeIn, *, source_ip: str
) -> ContactMessage:
    """公开提交入口：honeypot 闸 → 字段校验 → 防灌水闸 → 落库（status=new）。

    成功即提交（commit 后返回持久化行）；通知由路由层在成功落库后
    fire-and-forget 触发（与 demands 同款解耦，失败不影响 202）。
    """
    # honeypot 闸（先于一切校验与计数——机器人零收益）：非空 400，措辞
    # 与普通失败无异，绝不提示是 honeypot。
    if (payload.website or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rejected",
        )

    name, email, company, message = _validate_fields(payload)
    ip_hash = ip_hash_of(source_ip)
    _gate_or_raise(db, ip_hash, email)

    row = ContactMessage(
        name=name,
        email=email,
        company=company,
        message=message,
        ip_hash=ip_hash,
        status="new",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
