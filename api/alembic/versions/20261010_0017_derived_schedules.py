"""derived dataset schedules（wire-customer-data-engine 6.1）

materialized_artifacts 加 ``ref_key``（非草稿产物关联键：derived_dataset →
datasets.id）。artifact_id 的唯一约束对 NULL 天然放行（SQLite 多 NULL 不冲突），
ref_key 上加唯一索引防重复登记调度。

Revision ID: 0017_derived_schedules
Revises: 0016_data_plane_keys
Create Date: 2026-10-10

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_derived_schedules"
down_revision: Union[str, None] = "0016_data_plane_keys"
branch_labels: Union[str | Sequence[str] | None] = None
depends_on: Union[str | Sequence[str] | None] = None


def upgrade() -> None:
    with op.batch_alter_table("materialized_artifacts") as batch:
        batch.alter_column("artifact_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("ref_key", sa.String(length=36), nullable=True))
    op.create_index(
        "uq_materialized_artifacts_ref_key",
        "materialized_artifacts",
        ["ref_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_materialized_artifacts_ref_key", table_name="materialized_artifacts")
    with op.batch_alter_table("materialized_artifacts") as batch:
        batch.drop_column("ref_key")
        batch.alter_column("artifact_id", existing_type=sa.Integer(), nullable=False)
