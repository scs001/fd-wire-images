"""add admin catalog_review_notes（wire-platform-v1 任务 7.1）

内部后台目录治理审核意见表（admin 包自有，落平台库）：
- ``catalog_review_notes``：镜像指标的人工审核意见留档，append-only
  （无 update/delete 入口）；不设外键——权威库侧指标移除后留档仍可查。
- 纪律：镜像表（indicator_snapshots）仅同步管道可写，本表只承载
  admin 侧意见，绝不改镜像数据。

Revision ID: 0007_admin
Revises: 0006_demand_eval
Create Date: 2026-10-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_admin"
down_revision: Union[str, None] = "0006_demand_eval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_review_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "author",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'admin-console'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_review_notes_indicator_id",
        "catalog_review_notes",
        ["indicator_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catalog_review_notes_indicator_id", table_name="catalog_review_notes"
    )
    op.drop_table("catalog_review_notes")
