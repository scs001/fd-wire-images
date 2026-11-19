"""plan limits（wire-customer-data-engine 6.4）

billing_plans 加 ``limits_json``：数据面分层限额（standard 起业务档
500 万行/数据集 + 5GB 存储；缺省档 NULL=沿用 datasets 侧缺省）。

Revision ID: 0018_plan_limits
Revises: 0017_derived_schedules
Create Date: 2026-10-10

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_plan_limits"
down_revision: Union[str, None] = "0017_derived_schedules"
branch_labels: Union[str | Sequence[str] | None] = None
depends_on: Union[str | Sequence[str] | None] = None


def upgrade() -> None:
    op.add_column("billing_plans", sa.Column("limits_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("billing_plans", "limits_json")
