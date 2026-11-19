"""源包路由（wire-source-packs）。

- 公开（不登录，读走公开目录通道、不过计量轨道）：
  ``GET /api/v1/packs``（包列表+各包实时指标数）、
  ``GET /api/v1/packs/{pack_id}``（详情+分页指标清单，page/page_size）。
- 登录（Bearer ID token → customer，与 artifacts/datasets 切片同款身份键
  ``require_console_customer_id``）：
  ``GET  /api/v1/packs/subscriptions``（我的订阅 pack_id 列表）、
  ``POST/DELETE /api/v1/packs/{pack_id}/subscribe|unsubscribe``（订阅/退订，
  幂等；未知包 404）。订阅免费——任何档位（含免费档）可用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..console.auth import require_console_customer_id
from . import service
from .db import get_db
from .definitions import get_pack

router = APIRouter(prefix="/api/v1/packs", tags=["packs"])


class PackOut(BaseModel):
    """包卡片（列表项）：双语名称/描述 + 实时指标数。"""

    id: str
    name_zh: str
    name_en: str
    desc: str
    indicator_count: int


class PackIndicatorOut(BaseModel):
    """包详情清单项：目录行的最小展示字段（点击跳目录详情用）。"""

    code: str
    name_zh: str | None = None
    domain: str | None = None


class PackDetailOut(BaseModel):
    """包详情：元信息 + 分页指标清单。"""

    id: str
    name_zh: str
    name_en: str
    desc: str
    indicator_count: int
    items: list[PackIndicatorOut]
    total: int
    page: int
    page_size: int


class SubscriptionResultOut(BaseModel):
    """订阅/退订结果：操作后的订阅态（幂等——重复操作不报错）。"""

    pack_id: str
    subscribed: bool


def _pack_or_404(pack_id: str):
    pack = get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="pack not found")
    return pack


@router.get("", response_model=list[PackOut])
def list_packs() -> list[PackOut]:
    """包列表（公开匿名）：各包指标数按 filter 实时算（目录即真相）。"""
    return [PackOut.model_validate(item) for item in service.list_packs_with_counts()]


# 「我的订阅」必须先于 /{pack_id} 声明，否则会被包 id 路径吞掉
@router.get("/subscriptions", response_model=list[str])
def my_subscriptions(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[str]:
    """我的订阅包 id 列表（登录；订阅时间正序）。"""
    return service.PackSubscriptionService().list_pack_ids(db, customer_id=customer_id)


@router.get("/{pack_id}", response_model=PackDetailOut)
def pack_detail(
    pack_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PackDetailOut:
    """包详情（公开匿名）：元信息 + 分页指标清单（排序 domain, code）。"""
    pack = _pack_or_404(pack_id)
    result: dict[str, Any] = service.PackQueryService().indicators(
        pack, page=page, page_size=page_size
    )
    return PackDetailOut(
        id=pack.id,
        name_zh=pack.name_zh,
        name_en=pack.name_en,
        desc=pack.desc,
        indicator_count=result["total"],
        items=[PackIndicatorOut.model_validate(i) for i in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


def _set_subscription(
    pack_id: str, *, want: bool, customer_id: str, db: Session
) -> SubscriptionResultOut:
    """订阅/退订端点共用骨架：未知包 404 → 幂等切换 → 返回当前订阅态。"""
    _pack_or_404(pack_id)
    svc = service.PackSubscriptionService()
    if want:
        svc.subscribe(db, customer_id=customer_id, pack_id=pack_id)
    else:
        svc.unsubscribe(db, customer_id=customer_id, pack_id=pack_id)
    return SubscriptionResultOut(
        pack_id=pack_id,
        subscribed=pack_id in svc.list_pack_ids(db, customer_id=customer_id),
    )


@router.post("/{pack_id}/subscribe", response_model=SubscriptionResultOut)
def subscribe_pack(
    pack_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> SubscriptionResultOut:
    """订阅源包（登录；重复订阅幂等 200；订阅免费）。"""
    return _set_subscription(pack_id, want=True, customer_id=customer_id, db=db)


@router.delete("/{pack_id}/subscribe", response_model=SubscriptionResultOut)
def cancel_subscription(
    pack_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> SubscriptionResultOut:
    """退订（DELETE /subscribe 形态；本就未订阅幂等 200）。"""
    return _set_subscription(pack_id, want=False, customer_id=customer_id, db=db)


@router.post("/{pack_id}/unsubscribe", response_model=SubscriptionResultOut)
def unsubscribe_pack(
    pack_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> SubscriptionResultOut:
    """退订（POST /unsubscribe 形态；幂等）。"""
    return _set_subscription(pack_id, want=False, customer_id=customer_id, db=db)


@router.delete("/{pack_id}/unsubscribe", response_model=SubscriptionResultOut)
def delete_unsubscribe_pack(
    pack_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> SubscriptionResultOut:
    """退订（DELETE /unsubscribe 形态；幂等）。"""
    return _set_subscription(pack_id, want=False, customer_id=customer_id, db=db)
