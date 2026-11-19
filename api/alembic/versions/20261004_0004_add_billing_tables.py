"""add billing tables（wire-platform-v1 任务 4.2：计费账本与 sub2api 结算）

计费域四表：套餐档位 ``billing_plans``（seed 三档裁决值）、客户与支付映射
``billing_customers``、权威账本 ``billing_ledger_entries``（idempotency_key
全表唯一——同键重放只记一条）、预警留档 ``billing_notices``（同客户同周期
同档位唯一）。金额一律整数微元（1 元 = 1,000,000 微元）。

Revision ID: 0004_billing
Revises: 0003
Create Date: 2026-10-04

"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_billing"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 已裁决默认档位（与 fd_wire_api.billing.plans.SEEDS 同源；后台改价不动 seed）
_PLAN_SEEDS = [
    ("starter", "起步版", 1_999_000_000, 500_000, 4_000),
    ("professional", "专业版", 6_999_000_000, 3_000_000, 3_000),
    ("enterprise", "旗舰版", 19_999_000_000, 12_000_000, 2_000),
]

plans_table = sa.table(
    "billing_plans",
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("monthly_price_micro", sa.BigInteger),
    sa.column("included_lines", sa.Integer),
    sa.column("overage_unit_price_micro", sa.BigInteger),
    sa.column("is_active", sa.Boolean),
    sa.column("created_at", sa.DateTime),
)


def upgrade() -> None:
    op.create_table(
        "billing_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("monthly_price_micro", sa.BigInteger(), nullable=False),
        sa.Column("included_lines", sa.Integer(), nullable=False),
        sa.Column("overage_unit_price_micro", sa.BigInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_plans")),
        sa.UniqueConstraint("code", name=op.f("uq_billing_plans_code")),
    )
    op.create_table(
        "billing_customers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("sub2api_user_id", sa.String(length=64), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["billing_plans.id"], name=op.f("fk_billing_customers_plan_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_customers")),
        sa.UniqueConstraint(
            "sub2api_user_id", name=op.f("uq_billing_customers_sub2api_user_id")
        ),
    )
    op.create_table(
        "billing_ledger_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("idempotency_key", sa.String(length=191), nullable=False),
        sa.Column("source_ref", sa.String(length=191), nullable=True),
        sa.Column(
            "sub2api_applied", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_billing_ledger_entries_customer_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_ledger_entries")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_billing_ledger_entries_idempotency_key")
        ),
    )
    op.create_index(
        op.f("ix_billing_ledger_entries_customer_id"),
        "billing_ledger_entries",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_billing_ledger_entries_period"),
        "billing_ledger_entries",
        ["period"],
        unique=False,
    )
    op.create_table(
        "billing_notices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("threshold_pct", sa.Integer(), nullable=False),
        sa.Column("channels", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_billing_notices_customer_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_notices")),
        sa.UniqueConstraint(
            "customer_id",
            "period",
            "kind",
            "threshold_pct",
            name="uq_notice_once_per_tier",
        ),
    )
    op.create_index(
        op.f("ix_billing_notices_customer_id"),
        "billing_notices",
        ["customer_id"],
        unique=False,
    )
    op.bulk_insert(
        plans_table,
        [
            {
                "code": code,
                "name": name,
                "monthly_price_micro": price,
                "included_lines": lines,
                "overage_unit_price_micro": unit,
                "is_active": True,
                "created_at": datetime(2026, 10, 4, tzinfo=timezone.utc),
            }
            for code, name, price, lines, unit in _PLAN_SEEDS
        ],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_billing_notices_customer_id"), table_name="billing_notices"
    )
    op.drop_table("billing_notices")
    op.drop_index(
        op.f("ix_billing_ledger_entries_period"), table_name="billing_ledger_entries"
    )
    op.drop_index(
        op.f("ix_billing_ledger_entries_customer_id"),
        table_name="billing_ledger_entries",
    )
    op.drop_table("billing_ledger_entries")
    op.drop_table("billing_customers")
    op.drop_table("billing_plans")
