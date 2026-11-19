"""demand compliance-gate columns（wire-demand-generation-orchestration 1.1）

意向单合规门两入参落库（表 ``demands`` 加两列，均可空起步）：
- ``source_urls``：数据源地址（公开源 URL 列表，JSON list[str]）——
  服务端已硬校验 http(s) only、无内嵌账密；旧单无值=None；
- ``public_data_asserted``：公开数据声明（必勾=True 才放行提交）——
  旧单无值=None。

存量兼容：两列 nullable，旧单读路径原样 None；新单必存两字段。

Revision ID: 0024_demand_source_public
Revises: 0023_refresh_schedules
Create Date: 2026-10-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_demand_source_public"
down_revision: Union[str, None] = "0023_refresh_schedules"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.add_column("demands", sa.Column("source_urls", sa.JSON(), nullable=True))
    op.add_column(
        "demands", sa.Column("public_data_asserted", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("demands", "public_data_asserted")
    op.drop_column("demands", "source_urls")
