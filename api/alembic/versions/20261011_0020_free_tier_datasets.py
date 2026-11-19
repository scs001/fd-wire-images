"""free tier datasets（wire-free-tier-data-gate 1.1：api 侧轻量存储层）

- ``datasets.storage_tier``：存储分层标记（``api``=api 层轻量存储 /
  ``cell``=cell 引擎物化）。建集时刻按客户档位落值（免费→api、付费→cell）；
  存量行经 server_default 得 ``cell``——付费存量全是 cell 层，语义上无需
  回填（design D1）。
- ``dataset_columns``：api 层数据集列元数据（dataset_id/name/dtype/ordinal，
  同一数据集内列名唯一）。
- ``dataset_rows``：api 层数据集行存储（dataset_id/row_index/data JSON；
  ``data`` 经 ``JSON().with_variant(JSONB)`` 保持 SQLite/PG 双方言——PG 端
  JSONB，design D2）。

幂等性：DDL-only，upgrade/downgrade 往返安全（downgrade 撤列撤表）。

Revision ID: 0020_free_tier_datasets
Revises: 0019_demand_three_state
Create Date: 2026-10-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_free_tier_datasets"
down_revision: Union[str, None] = "0019_demand_three_state"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None

#: JSON 行存储的双方言形态：SQLite 用 JSON（SQLite 对 JSON 函数开放）、
#: PG 用 JSONB（可索引、与 wire-dedicated-postgres 方向 A 对齐）。
_JSON_DUAL = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # 存量行落 cell（server_default 单语句补值；付费存量本就全是 cell 层）
    op.add_column(
        "datasets",
        sa.Column(
            "storage_tier", sa.String(length=16), server_default="cell", nullable=False
        ),
    )
    op.create_table(
        "dataset_columns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("dtype", sa.String(length=16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_columns")),
        sa.UniqueConstraint("dataset_id", "name", name="uq_dataset_columns_dataset_name"),
    )
    op.create_index(
        "ix_dataset_columns_dataset_id", "dataset_columns", ["dataset_id"]
    )
    op.create_table(
        "dataset_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("data", _JSON_DUAL, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_rows")),
        sa.UniqueConstraint(
            "dataset_id", "row_index", name="uq_dataset_rows_dataset_index"
        ),
    )
    op.create_index("ix_dataset_rows_dataset_id", "dataset_rows", ["dataset_id"])


def downgrade() -> None:
    op.drop_index("ix_dataset_rows_dataset_id", table_name="dataset_rows")
    op.drop_table("dataset_rows")
    op.drop_index("ix_dataset_columns_dataset_id", table_name="dataset_columns")
    op.drop_table("dataset_columns")
    op.drop_column("datasets", "storage_tier")
