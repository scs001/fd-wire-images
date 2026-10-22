"""data plane keys（wire-customer-data-engine 5.2）

``data_plane_keys``：REST 数据面调用凭据（wdk- 前缀，sha256 落库、明文仅
铸造响应一次；per-cell + 可选数据集允许清单；吊销即时生效）。表落 datasets
库（数据面端点与数据集域同库同迁移链）。详见 fd_wire_api/data_plane/。

Revision ID: 0016_data_plane_keys
Revises: 0015_console_onboarding
Create Date: 2026-10-10

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_data_plane_keys"
down_revision: Union[str, None] = "0015_console_onboarding"
branch_labels: Union[str | Sequence[str] | None] = None
depends_on: Union[str | Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "data_plane_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=16), nullable=False),
        sa.Column("allowlist_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_data_plane_key_hash"),
    )
    op.create_index("ix_data_plane_keys_customer_id", "data_plane_keys", ["customer_id"])
    op.create_index("ix_data_plane_keys_key_hash", "data_plane_keys", ["key_hash"])


def downgrade() -> None:
    op.drop_index("ix_data_plane_keys_key_hash", table_name="data_plane_keys")
    op.drop_index("ix_data_plane_keys_customer_id", table_name="data_plane_keys")
    op.drop_table("data_plane_keys")
