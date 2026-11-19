"""商留通道表单化（add-contact-intake 任务 1.1）：contact_messages 表。

**纯增量**：官网「联系开通」表单卡（HomeView 内嵌）的公开提交落库表——
POST /api/v1/contact（公开无认证）+ 防灌水闸（同 ip_hash 24h ≤5 / 同
email 24h ≤3）+ 运营邮件通知 + admin contacts 分区。

- ``ip_hash``：来源 IP 的 SHA-256 截断哈希（16 hex），建索引承载 24h 窗口
  计数；**原始 IP 不落库**（spec 红线），库内任何位置都不存在 IP 原文；
- ``status``：``new``（默认，运营未读）/ ``read``（admin PATCH 标记已读），
  建索引承载未读数聚合；
- ``company`` 可空（表单选填），其余四字段必填（name/email/message）。

Revision ID: 0051_contact_intake
Revises: 0050_entity_tier_sensitivity
Create Date: 2026-11-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0051_contact_intake"
down_revision: Union[str, None] = "0050_entity_tier_sensitivity"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "contact_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("company", sa.String(length=120), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("ip_hash", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'new'"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_messages")),
    )
    op.create_index(
        "ix_contact_messages_ip_hash", "contact_messages", ["ip_hash"], unique=False
    )
    op.create_index(
        "ix_contact_messages_status", "contact_messages", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_contact_messages_status", table_name="contact_messages")
    op.drop_index("ix_contact_messages_ip_hash", table_name="contact_messages")
    op.drop_table("contact_messages")
