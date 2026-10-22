"""add ai console tables（wire-platform-v1 任务 5.4：AI 对话构建循环）

AI 控制台三表：对话会话 ``ai_conversations``、会话消息 ``ai_messages``
（user/assistant/tool_trace 三角色，append-only——tool_trace 留痕每次
工具调用的名字/入参/结果）、产物暂存 ``ai_draft_artifacts``（五类产物
spec 的 draft 落点；真正物化到企业 cell 是任务 5.3/5.5 的活）。

Revision ID: 0005_ai
Revises: 0004_billing
Create Date: 2026-10-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_ai"
down_revision: Union[str, None] = "0004_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_conversations")),
    )
    op.create_index(
        op.f("ix_ai_conversations_customer_id"),
        "ai_conversations",
        ["customer_id"],
        unique=False,
    )
    op.create_table(
        "ai_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["ai_conversations.id"],
            name=op.f("fk_ai_messages_conversation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_messages")),
    )
    op.create_index(
        op.f("ix_ai_messages_conversation_id"),
        "ai_messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_table(
        "ai_draft_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="draft"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["ai_conversations.id"],
            name=op.f("fk_ai_draft_artifacts_conversation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_draft_artifacts")),
    )
    op.create_index(
        op.f("ix_ai_draft_artifacts_conversation_id"),
        "ai_draft_artifacts",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_draft_artifacts_customer_id"),
        "ai_draft_artifacts",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_draft_artifacts_kind"),
        "ai_draft_artifacts",
        ["kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_draft_artifacts_status"),
        "ai_draft_artifacts",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_draft_artifacts_status"), table_name="ai_draft_artifacts")
    op.drop_index(op.f("ix_ai_draft_artifacts_kind"), table_name="ai_draft_artifacts")
    op.drop_index(
        op.f("ix_ai_draft_artifacts_customer_id"), table_name="ai_draft_artifacts"
    )
    op.drop_index(
        op.f("ix_ai_draft_artifacts_conversation_id"), table_name="ai_draft_artifacts"
    )
    op.drop_table("ai_draft_artifacts")
    op.drop_index(op.f("ix_ai_messages_conversation_id"), table_name="ai_messages")
    op.drop_table("ai_messages")
    op.drop_index(
        op.f("ix_ai_conversations_customer_id"), table_name="ai_conversations"
    )
    op.drop_table("ai_conversations")
