"""console onboarding bookkeeping（wire-self-serve-launch 4.3）

``console_onboardings``：Logto sub → 首登开户 ceremony 的断点簿记
（状态机 pending → minted → done；每 sub 一行）。承载「铸 key 成功但映射
失败 → 重试不重复铸 key」的断点续作语义；明文 key 永不落库，簿记只落
key_id。详见 fd_wire_api/console/models.py 与 onboarding.py 的 docstring。

Revision ID: 0015_console_onboarding
Revises: 0014_portal_pricing
Create Date: 2026-10-09

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_console_onboarding"
down_revision: Union[str, None] = "0014_portal_pricing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "console_onboardings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("logto_sub", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("key_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_console_onboardings_customer_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_console_onboardings")),
        sa.UniqueConstraint("logto_sub", name=op.f("uq_console_onboardings_logto_sub")),
    )


def downgrade() -> None:
    op.drop_table("console_onboardings")
