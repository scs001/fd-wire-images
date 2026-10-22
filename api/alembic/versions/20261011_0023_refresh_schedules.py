"""refresh_schedules（wire-refresh-scheduler：统一刷新调度器）

单表 ``refresh_schedules``（落元数据库，与 billing/packs 同链）：
一条 = 某客户为某属主目标（派生数据集 / dashboard 产物）登记的周期刷新。
``target_id`` 是跨库弱引用（数据集 uuid / 产物 id 串），不设外键——
目标失存由调度执行器自动清行兜底（spec：目标已删/归档 → 移除调度行）。

Revision ID: 0023_refresh_schedules
Revises: 0022_pack_subscriptions
Create Date: 2026-10-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_refresh_schedules"
down_revision: Union[str, None] = "0022_pack_subscriptions"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "refresh_schedules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("last_status", sa.String(length=16), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_schedules")),
    )
    # 客户维度隔离查询（列表/配额计数）
    op.create_index(
        op.f("ix_refresh_schedules_customer_id"), "refresh_schedules", ["customer_id"]
    )
    # due 查询驱动列（next_run_at <= now 且未 paused，按 next_run_at 排序）
    op.create_index(
        op.f("ix_refresh_schedules_next_run_at"), "refresh_schedules", ["next_run_at"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_refresh_schedules_next_run_at"), table_name="refresh_schedules"
    )
    op.drop_index(
        op.f("ix_refresh_schedules_customer_id"), table_name="refresh_schedules"
    )
    op.drop_table("refresh_schedules")
