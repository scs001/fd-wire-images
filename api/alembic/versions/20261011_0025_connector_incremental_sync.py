"""connector incremental sync（wire-connector-incremental-sync 3.1）

数据集级增量水位同步配置落库：

- ``datasets.sync_mode``：同步模式 ``snapshot``（默认，存量零变化）|
  ``incremental``（增量水位同步）。存量行经 server_default 落
  ``snapshot``——语义即现状（整表替换），无需回填。
- ``datasets.watermark_column``：增量水位列（须 ∈ columns_json 既有列；
  由 sync-settings 端点校验，库层不加约束——列基线本身在 JSON 里）。
- ``datasets.watermark_value``：最近水位值（TEXT 字符串原样存取——保持
  整型精度，比较语义交给 SQL 隐式转换；commit 上报捕获、config 下发）。
- ``dataset_syncs.mode``：同步历史行记本轮模式（begin 时按数据集配置
  落值；存量行 server_default ``snapshot`` 与既有全量历史一致）。

幂等性：DDL-only，upgrade/downgrade 往返安全（downgrade 撤列）。

Revision ID: 0025_connector_incremental_sync
Revises: 0024_demand_source_public
Create Date: 2026-10-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_connector_incremental_sync"
down_revision: Union[str, None] = "0024_demand_source_public"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    # 存量行落 snapshot（server_default 单语句补值——即现状全量语义）
    op.add_column(
        "datasets",
        sa.Column(
            "sync_mode", sa.String(length=16), server_default="snapshot", nullable=False
        ),
    )
    op.add_column(
        "datasets", sa.Column("watermark_column", sa.String(length=64), nullable=True)
    )
    op.add_column("datasets", sa.Column("watermark_value", sa.Text(), nullable=True))
    op.add_column(
        "dataset_syncs",
        sa.Column(
            "mode", sa.String(length=16), server_default="snapshot", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("dataset_syncs", "mode")
    op.drop_column("datasets", "watermark_value")
    op.drop_column("datasets", "watermark_column")
    op.drop_column("datasets", "sync_mode")
