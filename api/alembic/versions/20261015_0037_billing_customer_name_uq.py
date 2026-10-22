"""billing_customers.name 唯一化（wire-console-resilience 任务 2.3/D2）

生产事故（2026-10-15 E2E 实测）：admin 手工建档孤儿行（``logto_sub``
NULL）与自开户真户同 ``name`` → ``plan_code_for``/``storage_limit_for``
的 ``scalar_one_or_none`` 抛 MultipleResultsFound（被吞成 None → fail-closed
到未知档）。当日手工把孤儿行改名 ``<name>#orphan-std`` 止血。

本迁移把止血规则固化：

1. **自愈改名**：按 name 分组扫重名组——组内保留「``logto_sub`` 非空优先
   （有则 id 最小）」一行原名（与 datasets/service._resolve_customer_row 的
   确定性解析规则一致），其余行逐行改 ``<name>#orphan-<id>``。用 ORM 风格
   逐行 UPDATE（sa.table 轻量定义，照 0004 先例）：不经 SQL 方言 ``||``，
   PG/SQLite 双方言同形；后缀超 ``String(128)`` 上限时截基名保后缀完整。
2. **唯一索引** ``uq_billing_customers_name``：以 INDEX 落（照 0014 的
   ``uq_billing_customers_logto_sub`` 先例）——SQLite 不支持 ALTER 加约束
   （batch 重表才能做），唯一索引跨方言等价且 downgrade 简单。

幂等口径：alembic 版本位驱动，重跑整个 upgrade 为 no-op；对生产已手工
改名的 ``<name>#orphan-std`` 形态天然容忍（无重名组 → 零改名 → 只建索引）。

downgrade 只删索引、**不改名**——``#orphan-`` 改名不可逆（改名前的原名
未留痕），降级库回到「重名可存在」的旧缺陷态，如实不再重演自愈。

Revision ID: 0037_billing_customer_name_uq
Revises: 0036_artifact_marketplace
Create Date: 2026-10-15

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_billing_customer_name_uq"
down_revision: Union[str, None] = "0036_artifact_marketplace"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None

#: name 列宽（billing_customers.name String(128)）
_NAME_MAX = 128

customers_table = sa.table(
    "billing_customers",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("logto_sub", sa.String),
)


def _orphan_name(name: str, row_id: int) -> str:
    """孤儿行新名：``<name>#orphan-<id>``（与生产手工止血同款后缀）。
    基名逼近列宽上限时截断基名，保证后缀（含 id）完整可辨。"""
    suffix = f"#orphan-{row_id}"
    base = name[: _NAME_MAX - len(suffix)]
    return base + suffix


def _heal_duplicate_names() -> None:
    """重名组自愈：组内保留 sub 非空（有则 id 最小）一行原名，其余改名。

    全组皆孤儿（无 sub）时保留 id 最小一行——与解析助手「无 sub 时按 id
    最小收敛」同向，改名只发生在**多余**行上。
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            customers_table.c.id,
            customers_table.c.name,
            customers_table.c.logto_sub,
        )
    ).fetchall()

    groups: dict[str, list[sa.Row]] = {}
    for row in rows:
        groups.setdefault(row.name, []).append(row)

    for members in groups.values():
        if len(members) == 1:
            continue
        keep = sorted(
            members, key=lambda r: (r.logto_sub is None, r.id)
        )[0]
        for row in members:
            if row.id == keep.id:
                continue
            bind.execute(
                customers_table.update()
                .where(customers_table.c.id == row.id)
                .values(name=_orphan_name(row.name, row.id))
            )


def upgrade() -> None:
    _heal_duplicate_names()
    op.create_index(
        "uq_billing_customers_name", "billing_customers", ["name"], unique=True
    )


def downgrade() -> None:
    # 只删索引不改名：#orphan-<id> 改名不可逆（原名未留痕），降级库回到
    # 「重名可存在」的旧缺陷态（解析侧 _resolve_customer_row 仍确定性收敛）。
    op.drop_index("uq_billing_customers_name", table_name="billing_customers")
