"""订阅表 ORM 模型（wire-source-packs，Alembic 0022）。

``pack_subscriptions``：客户 × 源包 的订阅关系（多对多）。一行 = 一个
客户订阅一个包；(customer_id, pack_id) 唯一保证订阅幂等；退订 = 删行。
包本身不落库（静态配置见 definitions.py），pack_id 仅是配置 id 的引用
（不设外键——目标是代码内常量，DB 侧由路由层 404 闸校验）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import PacksBase


def utc_now_naive() -> datetime:
    """naive-UTC 当前时间（列形态与 artifacts 0021 同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PackSubscription(PacksBase):
    """一条源包订阅（客户维度隔离；订阅免费、不过计量轨道）。"""

    __tablename__ = "pack_subscriptions"
    __table_args__ = (
        # 同一客户对同一包至多一条：订阅幂等的库级保证
        UniqueConstraint("customer_id", "pack_id", name="uq_pack_subscriptions_customer_pack"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 客户面命名空间键（billing_customers.name，与其他客户面切片一致）
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 源包 id（definitions.PACKS 的配置 id；路由层校验未知包 404）
    pack_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<PackSubscription {self.customer_id} -> {self.pack_id}>"
