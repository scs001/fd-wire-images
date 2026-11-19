"""商留公开提交端点（add-contact-intake 任务 1.2）：POST /api/v1/contact。

公开无认证（同源调用，无 CORS 放开）：成功 202 ``{received: true}``，
不回显任何内部标识（含 ip_hash——红线）。来源 IP 取 X-Forwarded-For
第一跳（部署在 nginx/网关后，沿 market/showcase_routes 的反代口径），
无头回落 request.client.host——IP 原值仅用于计算哈希，绝不落库。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from . import notifications, service
from .db import get_db
from .schemas import ContactIntakeIn

router = APIRouter(prefix="/api/v1", tags=["contact"])


def _client_ip(request: Request) -> str:
    """限流/哈希用来源 IP：优先反代写入的 X-Forwarded-For 第一跳，回落
    套接字地址（与 market/showcase_routes._client_key 同款口径——部署在
    nginx/网关后，直连套接字地址是网关而非访客）。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client is not None else "unknown"


@router.post("/contact", status_code=202)
def submit_contact(
    payload: ContactIntakeIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """公开商留表单提交：落库 + fire-and-forget 运营通知。

    成功 202 ``{received: true}``（提交被受理的如实反馈；处置结果不承诺
    时限语义由前端文案承接）；校验 400 逐字段定位、 honeypot 400 不落库、
    防灌水超限 429——全部细节在 service 层（:mod:`.service`）。
    """
    row = service.intake(db, payload, source_ip=_client_ip(request))
    # 通知与提交解耦（notify 自身绝不抛异常）：失败仅日志，不影响 202。
    notifications.notify_new_contact_message(row)
    return {"received": True}
