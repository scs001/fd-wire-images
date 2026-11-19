"""本体内核投影的附加属性袋 ``kernel_attrs``（establish-ontology-kernel 6.4）。

**纯增量、零回填**：``entities`` 与 ``entity_relationships`` 各加一列
``kernel_attrs``（JSON nullable；null=没有附加属性）。加列沿 0050 ``_add_missing``
幂等先例。

**为什么需要它**：内核的 ``Unit`` 带一袋 ``attrs``，其中有些键在 fd-wire 的列里
没有落点——最典型的是**归名（folding）时的原写法** ``aliases``：写
「中华人民共和国合同法」被规则折叠成「合同法」后，原写法必须能读回来，否则
「同一实体的多种写法」这条信息就丢了。关系的 ``declared_kind`` 已有专列，但将来
同性质的键会不断出现，所以给一个**明确的附加袋**比每来一个键就加一列更可控。

**边界（重要）**：本列只放**内核投影的附加属性**（``aliases`` / ``folded_by`` /
``tags`` 之类），不放业务字段。业务字段有自己的列且被 service 校验；写业务语义
进这个袋子等于绕过校验，读侧也没有消费者。

Revision ID: 0055_ontology_kernel_attrs
Revises: 0054_ontology_event_target_key
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0055_ontology_kernel_attrs"
down_revision: str | None = "0054_ontology_event_target_key"
branch_labels: str | (Sequence[str] | None) = None
depends_on: str | (Sequence[str] | None) = None

TABLES: tuple[str, ...] = ("entities", "entity_relationships")
COLUMN = "kernel_attrs"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        col["name"] == column
        for col in sa.inspect(op.get_bind()).get_columns(table)
    )


def upgrade() -> None:
    for table in TABLES:
        # create_all 兜底路径下模型可能已建好该列（0049/0054 同法容错），加列必须幂等。
        if not _has_table(table) or _has_column(table, COLUMN):
            continue
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        if not _has_table(table) or not _has_column(table, COLUMN):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column(COLUMN)
