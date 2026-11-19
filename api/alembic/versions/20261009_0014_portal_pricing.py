"""portal pricing & console identity（wire-self-serve-launch 1.1/1.2）

- ``billing_customers.logto_sub``：Logto 登录身份 → Customer 的唯一映射
  （可空，存量行如实 NULL）。
- ``billing_plans.is_public``：公开档位标记；存量行（旧三档）回填
  ``is_public=1``（公开端点由 ``is_public && is_active`` 双条件过滤，
  旧三档随后置禁用即不出现）。
- 四档 seed（与 ``fd_wire_api.billing.plans.SEEDS`` 同源）：free（¥0/
  1,000 行）、basic（¥10/100,000 行）、standard（¥50/1,000,000 行），
  全部零超额单价；旧三档 starter/professional/enterprise 置
  ``is_active=0`` 禁用退场（行保留可审计，无存量客户零迁移）。

幂等口径：列添加与回填为一次性 DDL；seed 为 insert-if-missing，
``alembic upgrade head`` 重跑安全（alembic 版本位不动则整个 upgrade
为 no-op；down→up 往返后 seed 仍按存在性判定不重复插）。

Revision ID: 0014_portal_pricing
Revises: 0013_provenance_foundation
Create Date: 2026-10-09

"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_portal_pricing"
down_revision: Union[str, None] = "0013_provenance_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 四档 seed + 旧三档禁用（与 fd_wire_api.billing.plans.SEEDS 同源；
#: code, name, monthly_price_micro, included_lines, overage_unit_price_micro,
#: is_public, is_active）
_PLAN_SEEDS = [
    ("free", "免费版", 0, 1_000, 0, True, True),
    ("basic", "基础版", 10_000_000, 100_000, 0, True, True),
    ("standard", "标准版", 50_000_000, 1_000_000, 0, True, True),
    ("starter", "起步版", 1_999_000_000, 500_000, 4_000, True, False),
    ("professional", "专业版", 6_999_000_000, 3_000_000, 3_000, True, False),
    ("enterprise", "旗舰版", 19_999_000_000, 12_000_000, 2_000, True, False),
]

plans_table = sa.table(
    "billing_plans",
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("monthly_price_micro", sa.BigInteger),
    sa.column("included_lines", sa.Integer),
    sa.column("overage_unit_price_micro", sa.BigInteger),
    sa.column("is_active", sa.Boolean),
    sa.column("is_public", sa.Boolean),
    sa.column("created_at", sa.DateTime),
)


def upgrade() -> None:
    # 1.1 客户登录身份映射列（唯一可空）。
    # 唯一性以 UNIQUE INDEX 落（而非 UNIQUE 约束）：SQLite 不支持 ALTER 加
    # 约束（batch mode 重表才能做），唯一索引跨方言等价且 NULL 可重复。
    op.add_column(
        "billing_customers",
        sa.Column("logto_sub", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_billing_customers_logto_sub", "billing_customers", ["logto_sub"], unique=True
    )
    # 1.1 公开档位标记列（存量行回填 is_public=1，经 server_default 一次到位）
    op.add_column(
        "billing_plans",
        sa.Column(
            "is_public", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )

    # 1.2 四档 seed：insert-if-missing（幂等；后台改价行不被覆盖）
    bind = op.get_bind()
    existing = {
        row.code for row in bind.execute(sa.select(plans_table.c.code))
    }
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    pending = [
        {
            "code": code,
            "name": name,
            "monthly_price_micro": price,
            "included_lines": lines,
            "overage_unit_price_micro": unit,
            "is_public": is_public,
            "is_active": is_active,
            "created_at": now,
        }
        for code, name, price, lines, unit, is_public, is_active in _PLAN_SEEDS
        if code not in existing
    ]
    if pending:
        op.bulk_insert(plans_table, pending)

    # 旧三档禁用退场（终态：存量行压回 is_active=0）
    bind.execute(
        plans_table.update()
        .where(plans_table.c.code.in_(["starter", "professional", "enterprise"]))
        .values(is_active=False)
    )


def downgrade() -> None:
    op.drop_column("billing_plans", "is_public")
    op.drop_index("uq_billing_customers_logto_sub", table_name="billing_customers")
    op.drop_column("billing_customers", "logto_sub")
