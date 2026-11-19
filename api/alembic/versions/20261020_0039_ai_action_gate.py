"""ai action gate tables（wire-copilot-action-gate design D4）。

动作闸两张表（挂 AiBase，与 ai 切片同库）：

- ``ai_action_proposals``：副驾写操作提议——state 机
  pending_confirm / pending_approval → executed / rejected / expired / failed。
  参数只存 sha256 指纹 + 脱敏摘要（全文 spec 的正本在 ai_draft_artifacts，
  凭据永不入本表）。``expires_at`` 仅 async（publish）档使用（24h）。
- ``ai_action_audit``：执行审计台账（append-only，成败都记一行），
  检索维度 customer_id / action_id / created_at。

列定义与 0005/0038 同款约定：Python 侧 default 不落 server_default
（写入永远经 ORM 带值）。

Revision ID: 0039_ai_action_gate
Revises: 0038_artifact_drafts
Create Date: 2026-10-20

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_ai_action_gate"
down_revision: Union[str, None] = "0038_artifact_drafts"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "ai_action_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("args_digest", sa.String(length=64), nullable=False),
        sa.Column("args_summary", sa.Text(), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("impact_text", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.String(length=128), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["ai_conversations.id"],
            name=op.f("fk_ai_action_proposals_conversation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_action_proposals")),
    )
    for column in (
        "customer_id",
        "action_id",
        "state",
        "expires_at",
        "conversation_id",
    ):
        op.create_index(
            op.f(f"ix_ai_action_proposals_{column}"),
            "ai_action_proposals",
            [column],
        )

    op.create_table(
        "ai_action_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["ai_action_proposals.id"],
            name=op.f("fk_ai_action_audit_proposal_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_action_audit")),
    )
    for column in ("proposal_id", "customer_id", "action_id"):
        op.create_index(
            op.f(f"ix_ai_action_audit_{column}"), "ai_action_audit", [column]
        )


def downgrade() -> None:
    for column in ("proposal_id", "customer_id", "action_id"):
        op.drop_index(op.f(f"ix_ai_action_audit_{column}"), table_name="ai_action_audit")
    op.drop_table("ai_action_audit")
    for column in (
        "conversation_id",
        "expires_at",
        "state",
        "action_id",
        "customer_id",
    ):
        op.drop_index(
            op.f(f"ix_ai_action_proposals_{column}"),
            table_name="ai_action_proposals",
        )
    op.drop_table("ai_action_proposals")
