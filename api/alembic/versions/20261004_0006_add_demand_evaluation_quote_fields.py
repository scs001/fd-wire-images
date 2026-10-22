"""add demand evaluation/quote fields（wire-platform-v1 任务 6.4）

需求单 AI 预评估与人工报价流落库列：
- ``evaluation_detail``：AI 预评估内部全文 JSON（客户不可见）；
- ``quote_amount_micro``：报价金额微元整数（账内权威单位）；
- ``quote_valid_until``：报价有效期（客户视图随金额展示）；
- ``quote_operator``：报价操作人（内部留档，客户不可见）；
- ``quote_history``：报价历史 append-only JSON。

Revision ID: 0006_demand_eval
Revises: 0005_ai
Create Date: 2026-10-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_demand_eval"
down_revision: Union[str, None] = "0005_ai"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("demands", sa.Column("evaluation_detail", sa.JSON(), nullable=True))
    op.add_column("demands", sa.Column("quote_amount_micro", sa.BigInteger(), nullable=True))
    op.add_column("demands", sa.Column("quote_valid_until", sa.DateTime(), nullable=True))
    op.add_column("demands", sa.Column("quote_operator", sa.String(length=64), nullable=True))
    op.add_column(
        "demands",
        sa.Column(
            "quote_history",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("demands", "quote_history")
    op.drop_column("demands", "quote_operator")
    op.drop_column("demands", "quote_valid_until")
    op.drop_column("demands", "quote_amount_micro")
    op.drop_column("demands", "evaluation_detail")
