"""商留通道的 ORM 模型（add-contact-intake 任务 1.1，Alembic 0051）。

单表 ``contact_messages``：官网「联系开通」表单卡（HomeView 内嵌）提交的
访客意向留档。红线（spec contact-intake 防灌水闸）：来源 IP 只以 SHA-256
截断哈希（16 hex）落 ``ip_hash``，**绝不存原始 IP**，响应面也绝不回显该列。

``status`` 是运营侧两态：``new``（未读，默认）/ ``read``（已读，admin
PATCH 标记）——已读动作留痕于状态列本身，不做删除/编辑面。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import ContactBase

#: 运营侧状态词表（new 默认落库；read 仅经 admin PATCH 可达）
MESSAGE_STATUSES = ("new", "read")


def utc_now_naive() -> datetime:
    """naive-UTC 当前时间（列形态与 xingyan/packs/alerts 同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContactMessage(ContactBase):
    """一条商留表单提交（访客意向；运营在 admin contacts 分区处置）。"""

    __tablename__ = "contact_messages"
    __table_args__ = (
        # 防灌水闸的计数维度（同 ip_hash 24h ≤5 按此列扫窗）
        Index("ix_contact_messages_ip_hash", "ip_hash"),
        # 未读数聚合（admin 分区入口徽标）与状态过滤
        Index("ix_contact_messages_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 访客姓名（≤80，表单必填）
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    #: 联系邮箱（≤120，表单必填，形态经服务端校验）
    email: Mapped[str] = mapped_column(String(120), nullable=False)
    #: 公司/团队名（可选 ≤120）
    company: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: 需求描述全文（≤2000，表单必填）
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: 来源 IP 的 SHA-256 截断哈希（16 hex）——防灌水计数维度；原始 IP 不落库
    ip_hash: Mapped[str] = mapped_column(String(16), nullable=False)
    #: new | read（运营已读留痕）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<ContactMessage {self.id} {self.email} {self.status}>"


__all__ = ["MESSAGE_STATUSES", "ContactMessage"]
