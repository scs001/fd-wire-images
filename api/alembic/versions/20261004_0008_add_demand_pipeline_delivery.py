"""add demand pipeline/delivery fields（wire-platform-v1 任务 6.5+6.6）

需求单流水线衔接与私有 scope 交付落库列：
- ``pipeline_events``：流水线事件 append-only JSON（pipeline_run / gate_result，
  任务 6.5——building→delivery_acceptance 的 gate 门禁以其中的通过记录为前置）；
- ``delivery_events``：交付事件 append-only JSON（scope_created /
  scope_create_failed / publish_authorization，任务 6.6）；
- ``scope_status``：私有 scope 交付状态（None / ok / pending）；
- ``scope_name``：已建客户私有 scope 名（``demand-<code>``）。

Revision ID: 0008_demand_pipeline
Revises: 0007_admin
Create Date: 2026-10-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_demand_pipeline"
down_revision: Union[str, None] = "0007_admin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "demands",
        sa.Column(
            "pipeline_events",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column(
        "demands",
        sa.Column(
            "delivery_events",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column("demands", sa.Column("scope_status", sa.String(length=32), nullable=True))
    op.add_column("demands", sa.Column("scope_name", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("demands", "scope_name")
    op.drop_column("demands", "scope_status")
    op.drop_column("demands", "delivery_events")
    op.drop_column("demands", "pipeline_events")
