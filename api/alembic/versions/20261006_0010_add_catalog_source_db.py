"""catalog mirror: add source_db / unit columns（indicator-caliber-unification 任务 2.1）。

指标目录镜像换源 `list_registry_entries`（登记表全量枚举）后：

- ``source_db``：indicator_id = ``source_db:native_code`` 的来源半边。按源
  搜索/过滤与 /v1/catalog/stats 的 GROUP BY 都需要独立列（不解析 ID），
  可空起步——存量旧格式行由换源后的首次全量重刷一次性退役。
- ``unit``：登记行携带的单位直映落列（spec：单位作为条目属性呈现，
  不再进标识拼接）。

Revision ID: 0010_catalog_source_db
Revises: 0009_materialization
Create Date: 2026-10-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_catalog_source_db"
down_revision: Union[str, None] = "0009_materialization"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "indicator_snapshots",
        sa.Column("source_db", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "indicator_snapshots",
        sa.Column("unit", sa.String(length=256), nullable=True),
    )
    op.create_index(
        "ix_indicator_snapshots_source_db", "indicator_snapshots", ["source_db"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_indicator_snapshots_source_db", table_name="indicator_snapshots"
    )
    op.drop_column("indicator_snapshots", "unit")
    op.drop_column("indicator_snapshots", "source_db")
