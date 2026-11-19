"""add materialization table（wire-platform-v1 任务 5.6：产物物化计量）

``materialized_artifacts``：draft 转正登记——从 ``ai_draft_artifacts``
拷贝的 spec 快照 + 五字段 cron 调度（naive UTC）+ 最近一次物化运行摘要
（last_status / last_rows / last_run_detail 逐项成败）。物化读取逐项以
客户凭据经 wire 入口发出，行数由 registry audit 侧计量进客户账单
（本表只存运行摘要，不做二次记账）。``artifact_id`` 唯一——同一草稿
只允许转正一次。

Revision ID: 0009_materialization
Revises: 0008_demand_pipeline
Create Date: 2026-10-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_materialization"
down_revision: Union[str, None] = "0008_demand_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "materialized_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("schedule", sa.String(length=64), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_run_detail", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_materialized_artifacts")),
        sa.UniqueConstraint("artifact_id", name="uq_materialized_artifacts_artifact"),
    )
    op.create_index(
        op.f("ix_materialized_artifacts_artifact_id"),
        "materialized_artifacts",
        ["artifact_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_materialized_artifacts_customer_id"),
        "materialized_artifacts",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_materialized_artifacts_kind"),
        "materialized_artifacts",
        ["kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_materialized_artifacts_kind"), table_name="materialized_artifacts"
    )
    op.drop_index(
        op.f("ix_materialized_artifacts_customer_id"),
        table_name="materialized_artifacts",
    )
    op.drop_index(
        op.f("ix_materialized_artifacts_artifact_id"),
        table_name="materialized_artifacts",
    )
    op.drop_table("materialized_artifacts")
