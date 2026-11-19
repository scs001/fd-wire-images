"""数据集级访问授权判定（wire-dataset-acl D2：单一实现，一处真源）。

判定序（design D2，逐字）：

1. ``actor_sub == customer.logto_sub`` → 通过（owner 旁路；owner 不在成员表）；
2. ``dataset.acl_mode == "team"`` → 通过（存量语义，默认值即零迁移兼容面）；
3. ``restricted`` → 查 ``dataset_grants``：``need="read"`` 要 view|edit 任一，
   ``need="write"`` 要 edit；无行 → 403（detail 含数据集名与「无访问授权」）。

**禁止第二份授权语义**：所有拦截点（控制台读/写面、数据面 console 轨道、
实体对象面）都调本模块的 ``can_access`` / ``can_view``；``role`` 参数为
统一签名保留（全局角色门禁仍在 ``require_console_customer`` 等依赖层，
本 helper 不重复裁决）。

边界（design D6）：

- ``wdk-`` data-key 轨道不承载成员身份（``DataPlanePrincipal.member_sub``
  恒 None）→ 不拦；给成员发 key 是 v2 的显式扩面；
- 系统合成 principal（scheduler / 内部登记）同样无 member_sub → 不拦；
- 跨企业数据集走市场可见性域（``market.browse``）→ 不属本授权域，不拦
  （队内跨企业请求仍由既有 404 语义先行拒）。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DatasetGrant

#: 访问模式值域（应用层校验，同 storage_tier 口径）
ACL_MODE_TEAM = "team"
ACL_MODE_RESTRICTED = "restricted"
ACL_MODES: tuple[str, ...] = (ACL_MODE_TEAM, ACL_MODE_RESTRICTED)

#: 授权权限值域
PERMISSION_VIEW = "view"
PERMISSION_EDIT = "edit"
PERMISSIONS: tuple[str, ...] = (PERMISSION_VIEW, PERMISSION_EDIT)

NEED_READ = "read"
NEED_WRITE = "write"
_NEEDS: tuple[str, ...] = (NEED_READ, NEED_WRITE)

_NEED_LABEL = {
    NEED_READ: "查看（view）",
    NEED_WRITE: "编辑（edit）",
}


def _grant_permissions(db: Session, *, dataset_id: str, actor_sub: str | None) -> set[str]:
    """取成员在该数据集的授权权限集（无行 → 空集）。"""
    if not actor_sub:
        return set()
    rows = db.execute(
        select(DatasetGrant.permission).where(
            DatasetGrant.dataset_id == dataset_id,
            DatasetGrant.logto_sub == actor_sub,
        )
    ).scalars().all()
    return {row for row in rows if row}


def _decide(
    db: Session,
    *,
    customer: Any,
    actor_sub: str | None,
    role: str | None,
    dataset: Any,
    need: str,
) -> bool:
    """单一判定（内部；对外只暴露 can_access/can_view）。"""
    if need not in _NEEDS:
        raise ValueError(f"need 必须是 read 或 write（收到 {need!r}）")
    owner_sub = getattr(customer, "logto_sub", None)
    if owner_sub and actor_sub and actor_sub == owner_sub:
        return True  # owner 旁路（owner 不在成员表）
    mode = getattr(dataset, "acl_mode", None) or ACL_MODE_TEAM
    if mode != ACL_MODE_RESTRICTED:
        return True  # team（默认/存量）直通
    permissions = _grant_permissions(db, dataset_id=dataset.id, actor_sub=actor_sub)
    if need == NEED_READ:
        return bool(permissions & {PERMISSION_VIEW, PERMISSION_EDIT})
    return PERMISSION_EDIT in permissions


def _denied(dataset: Any, need: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"数据集「{getattr(dataset, 'name', dataset.id)}」为受限数据集，"
            f"你无访问授权（需要{_NEED_LABEL[need]}权限；请联系数据集 owner 授权）"
        ),
    )


def can_view(
    db: Session,
    *,
    customer: Any,
    actor_sub: str | None,
    role: str | None = None,
    dataset: Any,
) -> bool:
    """只读可见性谓词（列表过滤用——不抛 403，只回答是/否）。"""
    return _decide(
        db,
        customer=customer,
        actor_sub=actor_sub,
        role=role,
        dataset=dataset,
        need=NEED_READ,
    )


def can_access(
    db: Session,
    *,
    customer: Any,
    actor_sub: str | None,
    role: str | None = None,
    dataset: Any,
    need: str = NEED_READ,
) -> bool:
    """唯一授权判定入口：通过返回 True；无授权抛 403（可读中文原因）。

    ``role`` 为统一签名保留参数（全局角色门禁在依赖层，本 helper 不裁决）。
    """
    if _decide(
        db,
        customer=customer,
        actor_sub=actor_sub,
        role=role,
        dataset=dataset,
        need=need,
    ):
        return True
    raise _denied(dataset, need)


def can_access_principal(
    db: Session,
    *,
    principal: Any,
    dataset: Any,
    need: str = NEED_READ,
) -> bool:
    """数据面 principal 适配（design D6 边界集中在此）：

    - 非 console 轨道（data-key / scheduler 合成身份）→ 不拦；
    - 非本队数据集（市场可见性域）→ 不拦；
    - console 轨道且本队 → 走同一 ``can_access``。
    """
    if getattr(principal, "channel", None) != "console":
        return True
    member_sub = getattr(principal, "member_sub", None)
    if member_sub is None:
        return True  # data-key / 系统身份：不承载成员身份（D6）
    if getattr(dataset, "customer_id", None) != getattr(principal, "customer_id", None):
        return True  # 市场授予的他企数据集：独立可见性域
    customer = getattr(principal, "console_customer", None)
    if customer is None:  # pragma: no cover - console 轨道恒注入客户行
        return True
    return can_access(
        db,
        customer=customer,
        actor_sub=member_sub,
        role=getattr(principal, "role", None),
        dataset=dataset,
        need=need,
    )


__all__ = [
    "ACL_MODES",
    "ACL_MODE_RESTRICTED",
    "ACL_MODE_TEAM",
    "NEED_READ",
    "NEED_WRITE",
    "PERMISSIONS",
    "PERMISSION_EDIT",
    "PERMISSION_VIEW",
    "can_access",
    "can_access_principal",
    "can_view",
]
