"""add demands table（wire-platform-v1 任务 6.3：数据需求单状态机）

需求单表 ``demands``：八态状态机（submitted→…→delivered/rejected），
``status_history`` 为 append-only JSON 时间线（审计），报价与拒绝理由留档。

Revision ID: 0003
Revises: 0002_catalog
Create Date: 2026-10-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002_catalog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "demands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("entities_hint", sa.JSON(), nullable=True),
        sa.Column("frequency_expect", sa.String(length=64), nullable=True),
        sa.Column(
            "linked_indicators",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "status_history",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("evaluation_summary", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("quote_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("quote_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demands")),
    )
    op.create_index("ix_demands_code", "demands", ["code"], unique=True)
    op.create_index("ix_demands_customer_id", "demands", ["customer_id"], unique=False)
    op.create_index("ix_demands_status", "demands", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_demands_status", table_name="demands")
    op.drop_index("ix_demands_customer_id", table_name="demands")
    op.drop_index("ix_demands_code", table_name="demands")
    op.drop_table("demands")
