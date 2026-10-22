"""数据面 key 管理面与边界路由（wire-customer-data-engine 5.2/5.5）。

- console 会话管理 key（成员可铸可吊，Q18）；明文仅铸造响应一次。
- 内部后台只读视图（X-Admin-Token，沿既有后台边界：零管理操作）。
- 平台指标数据的显式拒绝路由（Q8 边界的自文档化）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..admin.auth import require_admin_access
from ..console.auth import require_console_customer_id
from ..datasets.db import get_db
from ..datasets.models import DataPlaneKey, Dataset
from . import service as key_service

router = APIRouter(tags=["data-plane-keys"])


class KeyCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    #: 空/缺省 = 本 cell 全部数据集
    dataset_ids: list[str] = Field(default_factory=list, max_length=200)


class KeyOut(BaseModel):
    id: str
    label: str
    fingerprint: str
    dataset_ids: list[str]
    status: str
    created_at: str
    revoked_at: Optional[str] = None
    last_used_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: DataPlaneKey) -> "KeyOut":
        fmt = lambda v: v.isoformat() if v else None  # noqa: E731
        return cls(
            id=row.id,
            label=row.label,
            fingerprint=row.fingerprint,
            dataset_ids=list(row.allowlist_json or []),
            status=row.status,
            created_at=fmt(row.created_at),
            revoked_at=fmt(row.revoked_at),
            last_used_at=fmt(row.last_used_at),
        )


class KeyCreatedOut(KeyOut):
    #: 明文仅此一次出现（控制台提示抄存；落库的只有 sha256）
    key: str


def _validate_allowlist(db: Session, customer_id: str, dataset_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    for dataset_id in dataset_ids:
        if dataset_id in seen:
            raise HTTPException(status_code=400, detail=f"允许清单重复: {dataset_id}")
        seen.add(dataset_id)
        found = db.get(Dataset, dataset_id)
        if found is None or found.customer_id != customer_id:
            raise HTTPException(
                status_code=404, detail=f"允许清单引用了不存在或非本企业的数据集: {dataset_id}"
            )
    return list(dataset_ids)


@router.get("/api/v1/data-plane-keys", response_model=list[KeyOut])
def list_data_plane_keys(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[KeyOut]:
    """本企业 key 列表（指纹与状态；明文不可再取回）。"""
    return [KeyOut.from_row(r) for r in key_service.list_keys(db, customer_id=customer_id)]


@router.post("/api/v1/data-plane-keys", response_model=KeyCreatedOut, status_code=201)
def create_data_plane_key(
    body: KeyCreateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> KeyCreatedOut:
    """铸造：明文只在本次响应返回一次，请立即抄存到服务端密管。"""
    dataset_ids = _validate_allowlist(db, customer_id, body.dataset_ids)
    row, plaintext = key_service.mint(
        db, customer_id=customer_id, label=body.label, dataset_ids=dataset_ids
    )
    out = KeyCreatedOut(**KeyOut.from_row(row).model_dump(), key=plaintext)
    return out


@router.delete("/api/v1/data-plane-keys/{key_id}", response_model=KeyOut)
def revoke_data_plane_key(
    key_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> KeyOut:
    """吊销即时生效（验证路径无缓存层）。"""
    return KeyOut.from_row(key_service.revoke(db, customer_id=customer_id, key_id=key_id))


# ── 内部后台只读视图（沿后台边界：零管理操作，Q18）──────────────────


admin_router = APIRouter(
    prefix="/internal/admin/data-plane-keys",
    tags=["admin-data-plane-keys"],
    dependencies=[Depends(require_admin_access)],
)


@admin_router.get("")
def admin_list_data_plane_keys(customer: str = "") -> list[dict]:
    """按客户过滤的只读视图（指纹/标签/状态/允许清单/最近使用）。"""
    from sqlalchemy import select

    db = next(get_db())
    try:
        q = select(DataPlaneKey).order_by(DataPlaneKey.created_at.desc())
        if customer:
            q = q.where(DataPlaneKey.customer_id == customer)
        rows = db.execute(q).scalars().all()
        return [KeyOut.from_row(r).model_dump() for r in rows]
    finally:
        db.close()


# ── 平台数据边界（Q8）：显式拒绝并指路 ────────────────────────────────


@router.api_route(
    "/api/v1/platform-indicators/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def platform_data_boundary(path: str) -> dict:
    """平台指标数据不经 REST 数据面：走 MCP 调用面（wgk-）或派生物化。"""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "平台指标数据不提供 REST 数据面端点——请经 MCP 调用面"
            "（wire 入口 + 长命 key）读取，或用指标连接派生"
            "（POST /api/v1/datasets/{id}/join）把结果物化成客户数据集后查询"
        ),
    )
