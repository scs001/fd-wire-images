"""admin 包自有 ORM 模型：``catalog_review_notes``（目录审核意见，append-only）。

纪律（spec wire-indicator-catalog：镜像仅同步管道可写）：
- 本表是 admin 自己的留档表——只追加审核意见，绝不改镜像数据；
- 无任何 update/delete 入口（append-only），历史即审计。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import AdminBase, utcnow


class CatalogReviewNote(AdminBase):
    """一条针对镜像指标的内部审核意见（只追加，不改不删）。"""

    __tablename__ = "catalog_review_notes"
    __table_args__ = (
        Index("ix_catalog_review_notes_indicator_id", "indicator_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 镜像指标主键（indicator_snapshots.indicator_id 的引用值；不设外键，
    #: 权威库侧指标移除后审核留档仍需可查）
    indicator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 审核意见正文（人工录入）
    note: Mapped[str] = mapped_column(Text, nullable=False)
    #: 录入人标识（内部留档）
    author: Mapped[str] = mapped_column(String(64), nullable=False, default="admin-console")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "indicator_id": self.indicator_id,
            "note": self.note,
            "author": self.author,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = ["CatalogReviewNote"]
