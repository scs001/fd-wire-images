"""pack subscriptions（wire-source-packs：源包订阅）

单表 ``pack_subscriptions``（落元数据库，与 billing/admin 同链）：
客户 × 源包 的订阅关系——一行 = 一个客户订阅一个包，
(customer_id, pack_id) 唯一保证订阅幂等（重复订阅不落第二行）。
包本身不落库（静态配置见 fd_wire_api/packs/definitions.py），pack_id 是
配置 id 的引用，路由层校验未知包 404，不设外键。

Revision ID: 0022_pack_subscriptions
Revises: 0021_artifact_lifecycle
Create Date: 2026-10-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_pack_subscriptions"
down_revision: Union[str, None] = "0021_artifact_lifecycle"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "pack_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("pack_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pack_subscriptions")),
        sa.UniqueConstraint("customer_id", "pack_id", name="uq_pack_subscriptions_customer_pack"),
    )
    op.create_index(
        op.f("ix_pack_subscriptions_customer_id"), "pack_subscriptions", ["customer_id"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_pack_subscriptions_customer_id"), table_name="pack_subscriptions"
    )
    op.drop_table("pack_subscriptions")
