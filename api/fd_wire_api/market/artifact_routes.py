"""产物市场 API 路由（wire-artifact-marketplace 的 HTTP 面）。

与 dataset 版（``market/routes.py``）同构：本文件只做**鉴权 + 404 + 透传**，
闸门全在 ``artifact_service``（写侧）与 ``artifact_browse``（读侧）。

发布方（owner/editor）：

- ``PUT  /api/v1/artifacts/{id}/visibility``  四档 + 开放模式 + allow_copy
- ``GET  /api/v1/artifacts/{id}/visibility``  当前可见性读视图

消费方（任意登录客户）：

- ``GET  /api/v1/market/artifacts``                 市场检索（SQL 层 market 档）
- ``GET  /api/v1/market/artifacts/{id}``            详情（结构摘要，无 snapshot）
- ``POST/DELETE /api/v1/market/artifacts/{id}/subscribe``  订阅
- ``POST /api/v1/market/artifacts/{id}/access-requests``   申请（approval 档）
- ``GET  /api/v1/market/artifacts/{id}/render``     订阅者渲染（版本 spec + 快照时间）
- ``POST /api/v1/market/artifacts/{id}/copy``       复制（快照剥离 + 绑定重映射）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..artifacts.models import Artifact, ArtifactAccessRequest, ArtifactVersion
from ..assets.routes import require_asset_editor
from ..billing.models import Customer
from ..console.auth import require_console_customer_id
from ..ai.db import get_db
from . import artifact_browse as browse

from . import artifact_service as service

router = APIRouter(prefix="/api/v1", tags=["artifact-market"])


# ── 请求体 ────────────────────────────────────────────────────────────


class ArtifactVisibilityIn(BaseModel):
    visibility: str = Field(description="private|link|directed|market")
    access_mode: str | None = Field(default=None, description="open|approval")
    allow_copy: bool | None = Field(default=None, description="是否允许他人复制")
    grants: list[str] | None = Field(
        default=None,
        description="定向被授权客户 id 清单（仅 directed 档落行；整表替换语义）",
    )


class GrantsIn(BaseModel):
    """定向授权增量操作（POST 追加 / DELETE 移除单个）。"""

    customer_id: str = Field(min_length=1, max_length=64)


# ── 工具 ─────────────────────────────────────────────────────────────


def _artifact_or_404(db: Session, artifact_id: int, customer_id: str) -> Artifact:
    """产物属主校验（跨企业/不存在一律 404，不泄露存在性）。"""
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不存在")
    return artifact


def _artifact_any(db: Session, artifact_id: int) -> Artifact:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不存在")
    return artifact


def _market_error(exc: service.ArtifactMarketError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _former_consumer(db: Session, artifact_id: int, customer_id: str) -> bool:
    """caller 是否曾是该产物的消费方（订阅/授权/已批申请行仍在）。

    行不随撤回删除——用途：撤回/下架后对**原消费方**如实答 403（「已撤回」
    比「不存在」诚实），对无关客户仍 404（不泄露存在性）。
    """
    return (
        service.is_subscribed(db, artifact_id, customer_id)
        or service.has_grant(db, artifact_id, customer_id)
        or service.has_approved_request(db, artifact_id, customer_id)
    )


# ── 发布方：可见性 ───────────────────────────────────────────────────


@router.get("/artifacts/{artifact_id}/visibility")
def get_artifact_visibility(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    return {
        **service.visibility_view(artifact),
        "grants": service.list_grants(db, artifact.id)
        if (artifact.visibility or "") == "directed"
        else [],
    }


@router.put("/artifacts/{artifact_id}/visibility")
def put_artifact_visibility(
    artifact_id: int,
    body: ArtifactVisibilityIn,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    """设置可见性（owner/editor；viewer 403 由 require_asset_editor 拦）。"""
    customer, sub = principal
    artifact = _artifact_or_404(db, artifact_id, str(customer.name))
    try:
        service.set_visibility(
            db,
            artifact=artifact,
            visibility=body.visibility,
            access_mode=body.access_mode,
            allow_copy=body.allow_copy,
            grants=body.grants,
            actor_sub=sub,
        )
    except service.ArtifactMarketError as exc:
        raise _market_error(exc) from exc
    return {
        **service.visibility_view(artifact),
        "grants": service.list_grants(db, artifact.id)
        if (artifact.visibility or "") == "directed"
        else [],
    }


# ── 发布方：定向授权名单（与数据集市场 grants 端点同款增量封装） ───────


@router.get("/artifacts/{artifact_id}/grants")
def get_artifact_grants(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """定向授权名单（无 grant 的 directed = 无人可见，诚实返回空表）。"""
    _artifact_or_404(db, artifact_id, customer_id)
    return {"artifact_id": artifact_id, "grants": service.list_grants(db, artifact_id)}


@router.post("/artifacts/{artifact_id}/grants")
def add_artifact_grant(
    artifact_id: int,
    body: GrantsIn,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    """追加一个定向授权（读当前 → 加一个 → 整表写回；仅 directed 档有效）。"""
    customer, sub = principal
    artifact = _artifact_or_404(db, artifact_id, str(customer.name))
    if (artifact.visibility or "private") != "directed":
        raise HTTPException(
            status_code=422,
            detail="只有 visibility=directed 的产物才维护定向授权名单",
        )
    wanted = service.list_grants(db, artifact_id)
    if body.customer_id not in wanted:
        wanted.append(body.customer_id)
    try:
        service.set_visibility(
            db,
            artifact=artifact,
            visibility="directed",
            access_mode=artifact.access_mode,
            grants=wanted,
            actor_sub=sub,
        )
    except service.ArtifactMarketError as exc:
        raise _market_error(exc) from exc
    return {"artifact_id": artifact_id, "grants": service.list_grants(db, artifact_id)}


@router.delete("/artifacts/{artifact_id}/grants/{grant_customer_id}")
def remove_artifact_grant(
    artifact_id: int,
    grant_customer_id: str,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    """移除一个定向授权（同上增量封装：读 → 减一个 → 写回）。"""
    customer, sub = principal
    artifact = _artifact_or_404(db, artifact_id, str(customer.name))
    wanted = [
        g for g in service.list_grants(db, artifact_id) if g != grant_customer_id
    ]
    service.set_visibility(
        db,
        artifact=artifact,
        visibility="directed",
        access_mode=artifact.access_mode,
        grants=wanted,
        actor_sub=sub,
    )
    return {"artifact_id": artifact_id, "grants": service.list_grants(db, artifact_id)}


# ── 消费方：市场面 ───────────────────────────────────────────────────


@router.get("/market/artifacts")
def list_market_artifacts(
    kind: str | None = Query(default=None, description="dashboard|research_report"),
    q: str | None = Query(default=None, description="标题关键字"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """市场检索（SQL 层只取 market 档——默认私有，未开放永不出现）。"""
    return browse.list_market_artifacts(
        db, viewer_customer_id=customer_id, kind=kind, q=q, limit=limit, offset=offset
    )


@router.get("/market/artifacts/{artifact_id}")
def get_market_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """市场详情：元数据 + 结构摘要（Q8：无 snapshot/config/query）。

    准入（同数据集市场 market_detail）：自有 → 可看；market 档 → 任一
    登录客户可看（详情是发现面，订阅不是看详情的前置）；directed 档 →
    仅 grant 名单；其余（private/link/撤回）404 不泄露存在性。
    """
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不存在")
    if artifact.customer_id == customer_id:
        source = browse.SOURCE_OWN
    elif artifact.status != "published":
        # 归档/草稿即不应答（同渲染在架态：可见性档位不动，在架态说了算）
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不存在")
    elif artifact.visibility == "market":
        source = browse.SOURCE_SUBSCRIPTION if service.is_subscribed(
            db, artifact_id, customer_id
        ) else None
    elif artifact.visibility == "directed" and service.has_grant(
        db, artifact_id, customer_id
    ):
        source = browse.SOURCE_GRANT
    else:
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不存在")
    return {
        **browse.detail_view(db, artifact, viewer_customer_id=customer_id),
        "access_source": source,
    }


class _RequestIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/market/artifacts/{artifact_id}/subscribe")
def subscribe_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """订阅（open 档订阅即得；approval 档转发起使用申请——顺滑语义，同数据集面）。

    准入（同数据集市场 subscribe 端点）：仅 market 档可订阅——定向档访问
    只认 grant 名单（不经订阅机制）；撤回档 404 不泄露；自有产物幂等返回。
    """
    artifact = db.get(Artifact, artifact_id)
    if (
        artifact is None
        or (artifact.visibility or "private") != "market"
        or artifact.status != "published"
    ):
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不在市场")
    if artifact.customer_id == customer_id:
        return {"subscribed": True, "created": False, "note": "自有产物无需订阅"}
    if (artifact.access_mode or "open") == "approval":
        if service.is_subscribed(db, artifact_id, customer_id):
            return {"subscribed": True, "created": False}
        # 审批档：转发起使用申请（响应带 subscribed=False 与数据集面同形）
        return {**_submit_artifact_access_request(db, artifact, customer_id),
                "subscribed": False}
    return service.subscribe(db, artifact_id=artifact_id, customer_id=customer_id)


@router.delete("/market/artifacts/{artifact_id}/subscribe")
def unsubscribe_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    return service.unsubscribe(db, artifact_id=artifact_id, customer_id=customer_id)


@router.delete("/market/artifacts/{artifact_id}/unsubscribe")
def unsubscribe_artifact_alias(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """别名（与数据集市场端点命名对齐）。"""
    return service.unsubscribe(db, artifact_id=artifact_id, customer_id=customer_id)


def _submit_artifact_access_request(
    db: Session, artifact: Artifact, customer_id: str
) -> dict:
    """发起使用申请（幂等：已有 pending/approved 直接返回现状；rejected 可重提）。"""
    existing = db.execute(
        select(ArtifactAccessRequest).where(
            ArtifactAccessRequest.artifact_id == artifact.id,
            ArtifactAccessRequest.requester_customer_id == customer_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = ArtifactAccessRequest(
            artifact_id=artifact.id, requester_customer_id=customer_id
        )
        db.add(existing)
        db.flush()
        db.commit()
        return {
            "access_request": {"id": existing.id, "status": existing.status},
            "created": True,
        }
    if existing.status == "rejected":
        existing.status = "pending"
        existing.decided_at = None
        existing.decided_by = None
        db.commit()
    return {
        "access_request": {"id": existing.id, "status": existing.status},
        "created": False,
    }


@router.post("/market/artifacts/{artifact_id}/access-requests")
def create_artifact_access_request(
    artifact_id: int,
    body: _RequestIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """发起使用申请（幂等；被拒后可重提——spec 场景「拒绝留痕」）。

    仅 market 档可申请（同数据集市场：定向档经 grant 名单授予，不走申请流）。
    """
    artifact = _artifact_any(db, artifact_id)
    if artifact.visibility != "market" or artifact.status != "published":
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不存在")
    return _submit_artifact_access_request(db, artifact, customer_id)


@router.get("/artifacts/{artifact_id}/access-requests")
def list_artifact_access_requests(
    artifact_id: int,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    """申请列表（发布方；新→旧）。"""
    customer, _sub = principal
    _artifact_or_404(db, artifact_id, str(customer.name))
    rows = db.execute(
        select(ArtifactAccessRequest)
        .where(ArtifactAccessRequest.artifact_id == artifact_id)
        .order_by(ArtifactAccessRequest.id.desc())
    ).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "requester_customer_id": r.requester_customer_id,
                "status": r.status,
                "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
                "decided_at": r.decided_at.isoformat() + "Z" if r.decided_at else None,
            }
            for r in rows
        ]
    }


def _decide_artifact_request(
    db: Session,
    artifact_id: int,
    request_id: int,
    principal: tuple[Customer, str],
    *,
    approve: bool,
) -> dict:
    customer, sub = principal
    _artifact_or_404(db, artifact_id, str(customer.name))
    row = db.get(ArtifactAccessRequest, request_id)
    if row is None or row.artifact_id != artifact_id:
        raise HTTPException(status_code=404, detail=f"申请 {request_id} 不存在")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail=f"申请已是 {row.status} 状态")
    row.status = "approved" if approve else "rejected"
    from ..artifacts.models import utc_now_naive  # noqa: PLC0415

    row.decided_at = utc_now_naive()
    row.decided_by = sub[:64]
    db.commit()
    if approve:
        # 批准即幂等建立订阅（同数据集市场）
        service.subscribe(
            db, artifact_id=artifact_id,
            customer_id=row.requester_customer_id,
        )
    return {"id": row.id, "status": row.status}


@router.post("/artifacts/{artifact_id}/access-requests/{request_id}/approve")
def approve_artifact_request(
    artifact_id: int,
    request_id: int,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    return _decide_artifact_request(db, artifact_id, request_id, principal, approve=True)


@router.post("/artifacts/{artifact_id}/access-requests/{request_id}/reject")
def reject_artifact_request(
    artifact_id: int,
    request_id: int,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    return _decide_artifact_request(db, artifact_id, request_id, principal, approve=False)


# ── 订阅者：渲染与复制 ───────────────────────────────────────────────


@router.get("/market/artifacts/{artifact_id}/render")
def render_market_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """订阅者渲染素材（版本 spec 快照 + 快照时间；零引擎调用）。

    Q5 渲染点：绑定数据集事后改成敏感档 → 拒绝渲染（不静默出数据）。
    """
    artifact, source = browse.resolve_renderable(db, artifact_id, customer_id)
    if artifact is None:
        existing = db.get(Artifact, artifact_id)
        if existing is not None and existing.visibility == "market" and (
            existing.status == "published"
        ):
            # 市场档在架但无订阅/授权/已批申请 → 403 引导（spec：未订阅被拒）
            raise HTTPException(
                status_code=403,
                detail="未订阅该产物——请先订阅或获取授权后再渲染",
            )
        if existing is not None and _former_consumer(db, artifact_id, customer_id):
            # 原消费方 + 发现已撤回/下架 → 403 如实说明（比 404 诚实）
            raise HTTPException(
                status_code=403,
                detail="该产物已被发布方撤回或下架，暂不可渲染",
            )
        raise HTTPException(status_code=404, detail=f"产物 {artifact_id} 不存在")

    latest = db.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(status_code=404, detail="产物无版本可渲染")

    blocked = service.sensitive_bound_datasets(latest.spec)
    if blocked and artifact.customer_id != customer_id:
        raise HTTPException(
            status_code=403,
            detail="该产物绑定数据集已被发布方标记敏感——渲染不可用",
        )

    spec = latest.spec or {}
    refreshed_at = spec.get("refreshed_at") if isinstance(spec, dict) else None
    return {
        "artifact_id": artifact.id,
        "kind": artifact.kind,
        "title": artifact.title,
        "version": latest.version,
        "spec": spec,
        # Q3/Q9：快照时间如实呈现（缺失 → None，前端显「未知」），不自动刷新
        "refreshed_at": refreshed_at if isinstance(refreshed_at, str) else None,
        "access_source": source,
    }


@router.post("/market/artifacts/{artifact_id}/copy")
def copy_market_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """复制到自己账号（Q4 快照剥离 + 绑定重映射 + Q7 溯源指针）。"""
    artifact = _artifact_any(db, artifact_id)
    try:
        return service.copy_artifact(
            db, artifact=artifact, caller_customer_id=customer_id
        )
    except service.ArtifactMarketError as exc:
        if (
            exc.status_code == 404
            and _former_consumer(db, artifact_id, customer_id)
        ):
            raise HTTPException(
                status_code=403,
                detail="该产物已被发布方撤回或下架，暂不可复制",
            ) from exc
        raise _market_error(exc) from exc
