"""市场面 ORM 模型（wire-dataset-marketplace，alembic 0034）。

三张表都挂 ``DatasetsBase``（与 datasets / assets 切片同库同引擎——
post-PG-consolidation 为同一平台元数据库；生产走 alembic 0034，
``create_all`` 只是开发兜底）。**刻意不复用 PacksBase**：packs 0022 当年
自建 Base 是因为订阅表落在元数据库收敛开关下的独立连接串；本切片的
三张表与 ``dataset_asset_meta``（可见性字段所在表）必须同库同事务——
一次「设可见性 + 写留痕 + 增删 grant」要在同一个会话里原子提交，跨库
会破坏这个不变量。故照 assets 先例挂 DatasetsBase。

- ``dataset_market_subscriptions``：客户 × 已发布数据集 的订阅关系
  （仿 ``pack_subscriptions``：``UniqueConstraint(customer_id, dataset_id)``
  保证订阅幂等，退订 = 删行）。订阅即获得数据面访问权（来源标注
  ``source=market_subscription``，归并行代理 B 接线）。
- ``dataset_share_grants``：定向档（visibility=directed）的被授权客户清
  单。一行 = 一个数据集授权给一个客户；``UniqueConstraint(dataset_id,
  customer_id)`` 保证重复授权不落第二行。**无 grant 行的 directed 数据
  集视为不可见**（诚实处理——绝不退化成"全可见"）。
- ``dataset_access_requests``：approval 模式下的准入请求（pending →
  approved|rejected）。``UniqueConstraint(dataset_id, requester_customer_id)``
  保证同一客户对同一数据集至多一条在途请求（重复请求复用原行）。
  **本表只落模型**——审批端点归并行代理 B，本切片不写状态机。

``customer_id`` 一律是客户面命名空间键（``billing_customers.name``，与
datasets/console/packs 四切片同口径），不设 DB 级外键（跨库/跨 Base 引用
由路由层校验）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..datasets.db import DatasetsBase


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


#: 准入请求状态（状态机归并行代理 B；此处只定值域供模型与查询共用）
REQUEST_STATUS_PENDING = "pending"
REQUEST_STATUS_APPROVED = "approved"
REQUEST_STATUS_REJECTED = "rejected"
REQUEST_STATUSES: tuple[str, ...] = (
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_REJECTED,
)


class MarketSubscription(DatasetsBase):
    """一条市场订阅（订阅幂等；退订 = 删行，无软删）。"""

    __tablename__ = "dataset_market_subscriptions"
    __table_args__ = (
        # 同一客户对同一数据集至多一条：订阅幂等的库级保证（照 packs 先例）
        UniqueConstraint(
            "customer_id", "dataset_id", name="uq_market_subscriptions_customer_dataset"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 订阅方（消费方）客户面命名空间键
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 被订阅的数据集 id（逻辑外联 datasets.id；有效性由路由层校验）
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<MarketSubscription {self.customer_id} -> {self.dataset_id}>"


class DatasetShareGrant(DatasetsBase):
    """一条定向授权（visibility=directed 的被授权方清单行）。"""

    __tablename__ = "dataset_share_grants"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "customer_id", name="uq_dataset_share_grants_dataset_customer"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: 被授权方客户面命名空间键（billing_customers.name）
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<DatasetShareGrant {self.dataset_id} -> {self.customer_id}>"


class DatasetAccessRequest(DatasetsBase):
    """一条准入请求（approval 模式；状态机归并行代理 B，本切片只落表）。"""

    __tablename__ = "dataset_access_requests"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "requester_customer_id",
            name="uq_dataset_access_requests_dataset_requester",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: 请求方客户面命名空间键
    requester_customer_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    #: pending|approved|rejected（值域见 REQUEST_STATUSES）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REQUEST_STATUS_PENDING)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive
    )
    #: 决策时刻（pending 为 NULL）
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 决策人 Logto sub（服务端身份解析；pending 为 NULL）
    decided_by_sub: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<DatasetAccessRequest {self.dataset_id} <- {self.requester_customer_id} {self.status}>"
