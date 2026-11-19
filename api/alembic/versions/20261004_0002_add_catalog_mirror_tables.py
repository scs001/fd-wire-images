"""catalog mirror tables（wire-platform-v1 任务 6.1）：统一指标注册目录每日只读镜像。

- ``indicator_snapshots``：权威库指标镜像行（indicator_id 唯一；status 两级
  verified|registered；不含任何观测数值列）。
- ``catalog_sync_state``：同步管道水位/状态（单行）。

写路径纪律：仅同步管道（IndicatorRegistrySync）可写；查询侧只读 session
带模型层 guard（详见 fd_wire_api/catalog/db.py）。

Revision ID: 0002_catalog
Revises: 0001
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_catalog"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "indicator_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name_zh", sa.String(length=512), nullable=True),
        sa.Column("name_en", sa.String(length=512), nullable=True),
        sa.Column("description_zh", sa.Text(), nullable=True),
        sa.Column("description_en", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(length=128), nullable=True),
        sa.Column("frequency", sa.String(length=64), nullable=True),
        sa.Column("time_span_start", sa.String(length=64), nullable=True),
        sa.Column("time_span_end", sa.String(length=64), nullable=True),
        sa.Column("entity_coverage_count", sa.Integer(), nullable=True),
        sa.Column("sample_value", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("indicator_id", name="uq_indicator_snapshots_indicator_id"),
        sa.CheckConstraint("status IN ('verified', 'registered')", name="ck_indicator_snapshots_status"),
    )
    op.create_index("ix_indicator_snapshots_domain", "indicator_snapshots", ["domain"])
    op.create_index("ix_indicator_snapshots_status", "indicator_snapshots", ["status"])
    op.create_index("ix_indicator_snapshots_code", "indicator_snapshots", ["code"])

    op.create_table(
        "catalog_sync_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("last_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("catalog_sync_state")
    op.drop_index("ix_indicator_snapshots_code", table_name="indicator_snapshots")
    op.drop_index("ix_indicator_snapshots_status", table_name="indicator_snapshots")
    op.drop_index("ix_indicator_snapshots_domain", table_name="indicator_snapshots")
    op.drop_table("indicator_snapshots")
