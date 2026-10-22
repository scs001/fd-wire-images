"""artifact marketplace（wire-artifact-marketplace）：产物可见性阶梯 + 三张关系表。

用户裁决（2026-10-13）「规则引擎和公开分享的数据集和看板是必须做的」——
本迁移补齐第三件：看板与研究报告进入市场（与 dataset 市场同构四档可见性 +
订阅/定向/审批），并新增产物特有的 **allow_copy**（可复制开关）与
**copied_from_artifact_id**（溯源指针）。

`artifacts` 增五列（均挂 AiBase，与 ai 切片同库）：

- ``visibility``：默认 ``'private'``——**存量全部保持私有**（无回填：默认值
  即私有语义，这是「默认不分享」红线的又一次落死）；
- ``access_mode``：默认 ``'open'``（仅 market/directed 档有效）；
- ``published_at`` / ``published_version``：进入非 private 档位时刻的时间戳
  与版本锚点（撤回时清空，见 market/artifact_service.set_visibility）；
- ``allow_copy``：默认 ``false``——发布方须显式允许才可被复制（Q6/Q4 裁决：
  复制=分析结构，快照剥离）；
- ``copied_from_artifact_id``：复制溯源指针（Q7，审计用，无更新提示）。

新表三张（同 dataset 市场三表结构）：

- ``artifact_market_subscriptions``：订阅（customer_id × artifact_id 唯一）；
- ``artifact_share_grants``：定向授权名单（artifact_id × customer_id 唯一）；
- ``artifact_access_requests``：审批流（pending|approved|rejected，
  同一 (artifact_id, requester) 唯一）。

字符串默认引号与布尔默认写法照 0003/0008/0034/0035 既有先例
（``sa.text("'private'")`` / ``sa.text("false")``——PG 与 SQLite 双方言同形）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_artifact_marketplace"
down_revision: Union[str, None] = "0035_quality_rules"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    # ── 1. artifacts 增五列（字符串/布尔默认双方言同形）──────────────
    op.add_column(
        "artifacts",
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "access_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
    )
    op.add_column(
        "artifacts", sa.Column("published_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "artifacts",
        sa.Column("published_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "allow_copy",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "artifacts",
        sa.Column("copied_from_artifact_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_artifacts_visibility", "artifacts", ["visibility"]
    )

    # ── 2. 订阅表 ─────────────────────────────────────────────────────
    op.create_table(
        "artifact_market_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_artifact_market_subscriptions")
        ),
        sa.UniqueConstraint(
            "customer_id",
            "artifact_id",
            name="uq_artifact_market_subscriptions_customer_artifact",
        ),
    )
    op.create_index(
        "ix_artifact_market_subscriptions_artifact_id",
        "artifact_market_subscriptions",
        ["artifact_id"],
    )
    op.create_index(
        "ix_artifact_market_subscriptions_customer_id",
        "artifact_market_subscriptions",
        ["customer_id"],
    )

    # ── 3. 定向授权表 ─────────────────────────────────────────────────
    op.create_table(
        "artifact_share_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_share_grants")),
        sa.UniqueConstraint(
            "artifact_id",
            "customer_id",
            name="uq_artifact_share_grants_artifact_customer",
        ),
    )
    op.create_index(
        "ix_artifact_share_grants_artifact_id",
        "artifact_share_grants",
        ["artifact_id"],
    )

    # ── 4. 审批流表 ───────────────────────────────────────────────────
    op.create_table(
        "artifact_access_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("requester_customer_id", sa.String(length=64), nullable=False),
        # pending | approved | rejected
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_artifact_access_requests")
        ),
        sa.UniqueConstraint(
            "artifact_id",
            "requester_customer_id",
            name="uq_artifact_access_requests_artifact_requester",
        ),
    )
    op.create_index(
        "ix_artifact_access_requests_artifact_id",
        "artifact_access_requests",
        ["artifact_id"],
    )
    op.create_index(
        "ix_artifact_access_requests_requester_customer_id",
        "artifact_access_requests",
        ["requester_customer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifact_access_requests_requester_customer_id",
        table_name="artifact_access_requests",
    )
    op.drop_index(
        "ix_artifact_access_requests_artifact_id",
        table_name="artifact_access_requests",
    )
    op.drop_table("artifact_access_requests")
    op.drop_index(
        "ix_artifact_share_grants_artifact_id",
        table_name="artifact_share_grants",
    )
    op.drop_table("artifact_share_grants")
    op.drop_index(
        "ix_artifact_market_subscriptions_customer_id",
        table_name="artifact_market_subscriptions",
    )
    op.drop_index(
        "ix_artifact_market_subscriptions_artifact_id",
        table_name="artifact_market_subscriptions",
    )
    op.drop_table("artifact_market_subscriptions")
    op.drop_index("ix_artifacts_visibility", table_name="artifacts")
    op.drop_column("artifacts", "copied_from_artifact_id")
    op.drop_column("artifacts", "allow_copy")
    op.drop_column("artifacts", "published_version")
    op.drop_column("artifacts", "published_at")
    op.drop_column("artifacts", "access_mode")
    op.drop_column("artifacts", "visibility")
