"""dataset marketplace（wire-dataset-marketplace）：可见性阶梯 + 三张关系表。

修订形态说明：本迁移替代最初"布尔 published 开关"方案——用户澄清后定为
**四档可见性阶梯**（private 默认 / link / directed / market）+ 开放模式
（open / approval）。`dataset_asset_meta` 增四列：

- ``visibility``：默认 ``'private'``——**存量全部保持私有**（无需回填：
  默认值即私有语义，这正是"默认不分享"的红线保证）；
- ``access_mode``：默认 ``'open'``（仅 directed/market 档有效）；
- ``published_at`` / ``published_version``：进入非 private 档位时刻的
  时间戳与版本锚点（撤回时清空——见 market/service.set_visibility）。

新表三张（均挂 DatasetsBase，与 assets/datasets 切片同库）：

- ``dataset_market_subscriptions``：订阅关系（customer_id × dataset_id 唯一）；
- ``dataset_share_grants``：定向授权名单（dataset_id × customer_id 唯一）；
- ``dataset_access_requests``：审批流（pending|approved|rejected，同一
  (dataset_id, requester) 唯一）。

字符串列默认值的引号写法照 0003/0008 既有先例（``sa.text("'private'")``
——PG 与 SQLite 双方言同形）。本迁移无布尔列，不涉布尔默认值陷阱。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_dataset_marketplace"
down_revision: Union[str, None] = "0033_team_spaces_v2"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    # ── 1. dataset_asset_meta 增四列（字符串默认双方言同形）────────────
    op.add_column(
        "dataset_asset_meta",
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
    )
    op.add_column(
        "dataset_asset_meta",
        sa.Column(
            "access_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
    )
    op.add_column(
        "dataset_asset_meta",
        sa.Column("published_at", sa.DateTime(timezone=False), nullable=True),
    )
    op.add_column(
        "dataset_asset_meta",
        sa.Column("published_version", sa.String(length=64), nullable=True),
    )

    # ── 2. 订阅表（仿 pack_subscriptions 形状）─────────────────────────
    op.create_table(
        "dataset_market_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_market_subscriptions")),
        sa.UniqueConstraint(
            "customer_id", "dataset_id", name="uq_market_subscriptions_customer_dataset"
        ),
    )
    op.create_index(
        op.f("ix_dataset_market_subscriptions_customer_id"),
        "dataset_market_subscriptions",
        ["customer_id"],
    )
    op.create_index(
        op.f("ix_dataset_market_subscriptions_dataset_id"),
        "dataset_market_subscriptions",
        ["dataset_id"],
    )

    # ── 3. 定向授权名单 ────────────────────────────────────────────────
    op.create_table(
        "dataset_share_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_share_grants")),
        sa.UniqueConstraint(
            "dataset_id", "customer_id", name="uq_dataset_share_grants_dataset_customer"
        ),
    )
    op.create_index(
        op.f("ix_dataset_share_grants_dataset_id"), "dataset_share_grants", ["dataset_id"]
    )

    # ── 4. 访问申请（审批流）──────────────────────────────────────────
    op.create_table(
        "dataset_access_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("requester_customer_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("decided_by_sub", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_access_requests")),
        sa.UniqueConstraint(
            "dataset_id",
            "requester_customer_id",
            name="uq_dataset_access_requests_dataset_requester",
        ),
    )
    op.create_index(
        op.f("ix_dataset_access_requests_dataset_id"),
        "dataset_access_requests",
        ["dataset_id"],
    )
    op.create_index(
        op.f("ix_dataset_access_requests_requester_customer_id"),
        "dataset_access_requests",
        ["requester_customer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dataset_access_requests_requester_customer_id"),
        table_name="dataset_access_requests",
    )
    op.drop_index(
        op.f("ix_dataset_access_requests_dataset_id"), table_name="dataset_access_requests"
    )
    op.drop_table("dataset_access_requests")
    op.drop_index(
        op.f("ix_dataset_share_grants_dataset_id"), table_name="dataset_share_grants"
    )
    op.drop_table("dataset_share_grants")
    op.drop_index(
        op.f("ix_dataset_market_subscriptions_dataset_id"),
        table_name="dataset_market_subscriptions",
    )
    op.drop_index(
        op.f("ix_dataset_market_subscriptions_customer_id"),
        table_name="dataset_market_subscriptions",
    )
    op.drop_table("dataset_market_subscriptions")

    op.drop_column("dataset_asset_meta", "published_version")
    op.drop_column("dataset_asset_meta", "published_at")
    op.drop_column("dataset_asset_meta", "access_mode")
    op.drop_column("dataset_asset_meta", "visibility")
