"""数据市场 API 路由（wire-dataset-marketplace 任务组 2/3/4 的 HTTP 面）。

本文件只做**鉴权 + 404 + 透传**：闸门全在 ``service.set_visibility``（发布方
写侧），查询与访问判定全在 ``browse``（消费方读侧）——路由层不重复实现任何
业务规则，避免"换个端点就绕过"。

发布方（owner/editor，``require_asset_editor``）：

- ``PUT  /api/v1/datasets/{id}/visibility``   四档可见性 + 开放模式 + 定向授权
- ``GET  /api/v1/datasets/{id}/visibility``   当前可见性读视图
- ``POST /api/v1/datasets/{id}/market/publish``    薄封装（→ market）
- ``POST /api/v1/datasets/{id}/market/unpublish``  薄封装（→ private，即时失效）
- ``GET/POST/DELETE /api/v1/datasets/{id}/grants`` 定向授权名单
- ``GET  /api/v1/datasets/{id}/access-requests``   申请列表（新→旧）
- ``POST /api/v1/datasets/{id}/access-requests/{req_id}/approve|reject``

消费方（任意登录客户，``require_console_customer_id``）：

- ``GET  /api/v1/market/datasets``              市场检索（SQL 层只查 market 档）
- ``GET  /api/v1/market/datasets/{id}``         市场详情（白名单视图，无数据值）
- ``POST/DELETE /api/v1/market/datasets/{id}/subscribe``  订阅（approval 档转申请）
- ``POST /api/v1/market/datasets/{id}/access-requests``   发起申请（幂等）

数据层未落地（并行窗口）时：订阅/审批端点显式 503，浏览面恒空——
安全方向，绝不因 ImportError 让整个 api 起不来（browse 的惰性解析纪律）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..assets.models import ACCESS_MODE_DEFAULT
from ..assets.routes import _dataset_or_404, require_asset_editor
from ..billing.models import Customer
from ..console.auth import require_console_customer_id
from ..datasets.db import get_db as get_datasets_db
from ..datasets.models import Dataset
from . import browse, service

router = APIRouter(prefix="/api/v1", tags=["market"])

#: 订阅/审批依赖的数据层未就绪时的显式降级文案（不假装成功）
_DATA_LAYER_503 = "市场订阅数据层尚未就绪（稍后重试）"


# ── 请求体 ────────────────────────────────────────────────────────────


class VisibilityIn(BaseModel):
    """可见性设置（四档 + 开放模式 + 定向授权名单）。"""

    visibility: str = Field(description="private|link|directed|market")
    access_mode: str | None = Field(
        default=None, description="open|approval（缺省沿用当前值；private/link 忽略）"
    )
    grants: list[str] | None = Field(
        default=None,
        description="定向被授权客户 id 清单（仅 directed 档落行；整表替换语义）",
    )


class GrantsIn(BaseModel):
    """定向授权增量操作（POST 追加 / DELETE 移除单个）。"""

    customer_id: str = Field(min_length=1, max_length=64)


def _visibility_payload(meta) -> dict:
    """可见性读视图 + 定向名单（路由层组装，service/browse 各自单一职责）。"""
    view = service.visibility_view(meta)
    return view


# ── 发布方：可见性设置 ────────────────────────────────────────────────


@router.put("/datasets/{dataset_id}/visibility")
def put_visibility(
    dataset_id: str,
    payload: VisibilityIn,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """设置可见性（唯一写入口的 HTTP 面；闸门在 service，422 直接透传）。

    - 敏感度「敏感」→ 只允许 private（service 硬闸，422）；
    - 空数据集 → 非 private 档 422；
    - 幂等：设为当前值不产生重复留痕。
    """
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    meta = service.set_visibility(
        db,
        dataset=dataset,
        visibility=payload.visibility,
        access_mode=payload.access_mode,
        grants=payload.grants,
        actor_sub=sub,
    )
    db.commit()
    db.refresh(meta)
    return _visibility_payload(meta)


@router.get("/datasets/{dataset_id}/visibility")
def get_visibility(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """当前可见性读视图（缺行 = 默认 private + open）。"""
    _dataset_or_404(db, dataset_id, customer_id)
    from ..assets.models import AssetMeta

    return _visibility_payload(db.get(AssetMeta, dataset_id))


@router.post("/datasets/{dataset_id}/market/publish")
def publish_to_market(
    dataset_id: str,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """一键发布到市场（薄封装：visibility=market + open；闸门/留痕同 service）。"""
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    meta = service.publish_dataset(db, dataset=dataset, actor_sub=sub)
    db.commit()
    db.refresh(meta)
    return _visibility_payload(meta)


@router.post("/datasets/{dataset_id}/market/unpublish")
def unpublish_from_market(
    dataset_id: str,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """撤回（薄封装：visibility=private，清锚点，消费方访问权即时失效）。"""
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    meta = service.unpublish_dataset(db, dataset=dataset, actor_sub=sub)
    db.commit()
    db.refresh(meta)
    return _visibility_payload(meta)


# ── 发布方：定向授权名单 ──────────────────────────────────────────────


@router.get("/datasets/{dataset_id}/grants")
def get_grants(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """定向授权名单（无 grant 的 directed = 无人可见，诚实返回空表）。"""
    _dataset_or_404(db, dataset_id, customer_id)
    return {"dataset_id": dataset_id, "grants": service.list_grants(db, dataset_id)}


@router.post("/datasets/{dataset_id}/grants")
def add_grant(
    dataset_id: str,
    payload: GrantsIn,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """追加一个定向授权（整表替换语义的增量封装：读当前 → 加一个 → 写回）。

    仅在 ``visibility=directed`` 档落行；其他档位 service 会清空 grant 表，
    故非 directed 档调用本端点等于无效操作（如实返回当前名单）。
    """
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    from ..assets.models import AssetMeta

    meta = db.get(AssetMeta, dataset_id)
    current_visibility = (getattr(meta, "visibility", None) or "private") if meta else "private"
    if current_visibility != "directed":
        raise HTTPException(
            status_code=422,
            detail="只有 visibility=directed 的数据集才维护定向授权名单",
        )
    wanted = service.list_grants(db, dataset_id)
    if payload.customer_id not in wanted:
        wanted.append(payload.customer_id)
    meta = service.set_visibility(
        db,
        dataset=dataset,
        visibility="directed",
        access_mode=service.visibility_view(meta)["access_mode"] or ACCESS_MODE_DEFAULT,
        grants=wanted,
        actor_sub=sub,
    )
    db.commit()
    return {"dataset_id": dataset_id, "grants": service.list_grants(db, dataset_id)}


@router.delete("/datasets/{dataset_id}/grants/{grant_customer_id}")
def remove_grant(
    dataset_id: str,
    grant_customer_id: str,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """移除一个定向授权（同上增量封装：读 → 减一个 → 写回）。"""
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    from ..assets.models import AssetMeta

    meta = db.get(AssetMeta, dataset_id)
    wanted = [g for g in service.list_grants(db, dataset_id) if g != grant_customer_id]
    service.set_visibility(
        db,
        dataset=dataset,
        visibility="directed",
        access_mode=service.visibility_view(meta)["access_mode"] or ACCESS_MODE_DEFAULT,
        grants=wanted,
        actor_sub=sub,
    )
    db.commit()
    return {"dataset_id": dataset_id, "grants": service.list_grants(db, dataset_id)}


# ── 消费方：市场浏览 ──────────────────────────────────────────────────


@router.get("/market/datasets")
def list_market(
    q: str | None = Query(default=None, max_length=200, description="关键字（名称/描述/标签）"),
    domain: str | None = Query(default=None, max_length=64, description="业务域"),
    tag: str | None = Query(default=None, max_length=64, description="标签"),
    limit: int = Query(default=20, ge=1, le=browse.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """市场列表：SQL 层只查 visibility=market（红线，browse 保证）。"""
    items, total = browse.list_market_datasets(
        db,
        viewer_customer_id=customer_id,
        q=q,
        domain=domain,
        tag=tag,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/market/datasets/{dataset_id}")
def market_detail(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """市场详情（白名单视图：元数据 + 列 schema + 行数，绝无数据值）。

    非 market 档 / 未知 id 一律 404（不泄露存在性）；定向档走
    ``directed_dataset_or_none``（grant 名单内可见，不进市场列表）。
    """
    view = browse.market_dataset_or_none(db, dataset_id, viewer_customer_id=customer_id)
    if view is None:
        view = browse.directed_dataset_or_none(db, dataset_id, viewer_customer_id=customer_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不在市场")
    return view


# ── 消费方：订阅 / 申请 ───────────────────────────────────────────────


def _require_market_access_ready() -> None:
    if not browse.data_layer_ready():
        raise HTTPException(status_code=503, detail=_DATA_LAYER_503)


@router.post("/market/datasets/{dataset_id}/subscribe")
def subscribe_market_dataset(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """订阅（幂等）。approval 档不直接订阅——转为发起访问申请（顺滑语义）。"""
    _require_market_access_ready()
    from ..assets.models import AssetMeta

    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不在市场")
    meta = db.get(AssetMeta, dataset_id)
    if browse._visibility_of(meta) != browse.VISIBILITY_MARKET:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不在市场")
    if browse._access_mode_of(meta) == browse.ACCESS_MODE_APPROVAL:
        view, created = browse.create_access_request(
            db, dataset_id=dataset_id, requester_customer_id=customer_id
        )
        return {"subscribed": False, "access_request": view, "created": created}
    browse.subscribe(db, dataset_id=dataset_id, customer_id=customer_id)
    return {"subscribed": True, "access_request": None, "created": False}


@router.delete("/market/datasets/{dataset_id}/unsubscribe")
def unsubscribe_market_dataset_alias(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """退订别名（路径与 packs 的 `/unsubscribe` 惯例一致；语义同 DELETE /subscribe）。"""
    return unsubscribe_market_dataset(dataset_id=dataset_id, customer_id=customer_id, db=db)


@router.delete("/market/datasets/{dataset_id}/subscribe")
def unsubscribe_market_dataset(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """退订（幂等；退订后访问权即时失效——browse 实时查表无缓存）。"""
    _require_market_access_ready()
    browse.unsubscribe(db, dataset_id=dataset_id, customer_id=customer_id)
    return {"subscribed": False}


@router.post("/market/datasets/{dataset_id}/access-requests")
def create_access_request(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """发起访问申请（幂等：已有 pending/approved 返回既有行）。

    最近一条为 rejected 时 409（发布方已拒绝，不可重复骚扰——如需再申请
    应由发布方先改口径；这是诚实拒而非静默建单）。
    """
    _require_market_access_ready()
    from ..assets.models import AssetMeta

    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不在市场")
    meta = db.get(AssetMeta, dataset_id)
    if browse._visibility_of(meta) != browse.VISIBILITY_MARKET:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不在市场")
    latest = browse.latest_request(db, dataset_id, customer_id)
    if latest is not None and getattr(latest, "status", None) == browse.REQUEST_REJECTED:
        raise HTTPException(status_code=409, detail="该数据集的访问申请已被拒绝")
    view, created = browse.create_access_request(
        db, dataset_id=dataset_id, requester_customer_id=customer_id
    )
    return {"access_request": view, "created": created}


# ── 发布方：审批 ──────────────────────────────────────────────────────


@router.get("/datasets/{dataset_id}/access-requests")
def list_dataset_access_requests(
    dataset_id: str,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """发布方视角：该数据集的全部申请（新→旧）。"""
    _require_market_access_ready()
    customer, _sub = principal
    _dataset_or_404(db, dataset_id, str(customer.name))
    return {"dataset_id": dataset_id, "requests": browse.list_access_requests(db, dataset_id=dataset_id)}


@router.post("/datasets/{dataset_id}/access-requests/{request_id}/approve")
def approve_access_request(
    dataset_id: str,
    request_id: int,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """批准（置 approved + 幂等写订阅行，消费方立即获得访问权）。"""
    return _decide(db, dataset_id, request_id, principal, approve=True)


@router.post("/datasets/{dataset_id}/access-requests/{request_id}/reject")
def reject_access_request(
    dataset_id: str,
    request_id: int,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """拒绝（置 rejected 留档，不可访问）。"""
    return _decide(db, dataset_id, request_id, principal, approve=False)


def _decide(
    db: Session,
    dataset_id: str,
    request_id: int,
    principal: tuple[Customer, str],
    *,
    approve: bool,
) -> dict:
    _require_market_access_ready()
    customer, sub = principal
    _dataset_or_404(db, dataset_id, str(customer.name))
    try:
        view = browse.decide_access_request(
            db, dataset_id=dataset_id, request_id=request_id, approve=approve, decided_by_sub=sub
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"申请 {request_id} 不存在") from None
    return {"access_request": view}
