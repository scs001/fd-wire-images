"""数据面双凭据鉴权（wire-customer-data-engine 5.3）。

同一族数据端点接受两种凭据，产生统一 principal：

- ``Authorization: Bearer wdk-…`` → 数据面 key 轨道：channel=data-key，
  actor_id=指纹，allowlist 生效，per-key 限流生效；
- console 会话（Logto ID token，经 datasets 既有 require_console_customer_id）
  → channel=console，actor_id=客户命名空间键（人工网格编辑语义）。

401/403 语义如实区分：无凭据/坏凭据 401；allowlist 越界 403。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..billing.models import Customer
from ..console.auth import (
    get_platform_db,
    require_console_customer_actor,
)
from ..datasets.db import get_db
from . import service as key_service


@dataclass(frozen=True)
class DataPlanePrincipal:
    """统一调用身份：channel 决定编辑记录归属与 allowlist/限流是否生效。

    ``member_sub``（wire-dataset-acl D2）：console 轨道旁取已校验成员 sub，
    供数据集级授权判定；``data-key`` 轨道恒 None（key 是客户级凭据、不承载
    成员身份——D6 边界，不拦）。``console_customer`` 是同一轨道解析出的客户
    行（owner 旁路比对需 ``logto_sub``；跨库无法在 datasets 会话里查）。
    """

    customer_id: str
    channel: str  # console | data-key
    actor_id: Optional[str] = None
    actor_label: Optional[str] = None
    allowlist: tuple[str, ...] = field(default_factory=tuple)  # 空 = 全部
    member_sub: Optional[str] = None
    console_customer: Optional[Customer] = None
    role: Optional[str] = None

    def allows_dataset(self, dataset_id: str) -> bool:
        return not self.allowlist or dataset_id in self.allowlist


def _bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return None


def require_data_plane_principal(
    request: Request,
    db: Session = Depends(get_db),
    platform_db: Session = Depends(get_platform_db),
) -> DataPlanePrincipal:
    token = _bearer(request)
    if token is not None and token.startswith(key_service.KEY_PREFIX):
        try:
            row = key_service.verify(db, token)
        except key_service.KeyRevokedError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc
        key_service.check_rate_limit(row.id)
        row.last_used_at = key_service._utcnow_naive()
        db.commit()
        return DataPlanePrincipal(
            customer_id=row.customer_id,
            channel="data-key",
            actor_id=row.fingerprint,
            actor_label=row.label,
            allowlist=tuple(row.allowlist_json or []),
        )
    # 无 wdk- 凭据 → console 轨道（Logto ID token 验签 + sub→Customer，
    # 失败语义沿用 console-auth：401/503 如实）。member_sub 供数据集级授权
    # 判定（wire-dataset-acl D2）；同一依赖的门禁（viewer 方法级只读、
    # 密钥物面 owner-only）原样生效。
    customer, member_sub, role = require_console_customer_actor(
        request, platform_db
    )
    return DataPlanePrincipal(
        customer_id=str(customer.name),
        channel="console",
        actor_id=str(customer.name),
        member_sub=member_sub,
        console_customer=customer,
        role=role,
    )


def ensure_dataset_allowed(
    principal: DataPlanePrincipal, dataset_id: str
) -> None:
    """allowlist 越界 → 403（spec：被拒且留痕——HTTP 层如实，引擎零触达）。"""
    if not principal.allows_dataset(dataset_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"该 key 的数据集允许清单不含 {dataset_id}"
                "（铸造时未列入；如需访问请在控制台调整允许清单或换用全量 key）"
            ),
        )
