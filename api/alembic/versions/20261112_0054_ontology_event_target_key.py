"""事件信封补语义键 ``target_key``（establish-ontology-kernel 6.3 / design D6）。

**纯增量、零回填**：``ontology_events`` 加一列 ``target_key``（String(256)
nullable；null=换底前写的行）。加列沿 0050 ``_add_missing`` 幂等先例。

**为什么加这一列**：事件流目前只有代理键寻址（``target_kind`` +
``target_id``），换一台机器、换一个库就失去了「这是在说哪个实体」的能力
——自增 id 只在本库内有意义。内核的信封同时收语义键（``target_key``，跨环境
可移植）与代理键（``target_ref``）：知验台只填前者，wire 两个都填。已有行保持
null 且**不回填**——历史事件的语义键只能靠当时的名字，而现在反解出的名字是
「今天的名字」，写进历史行会让台账撒谎；读侧由适配器从 ``target_id`` 反解。

**填法**（``versioning.record_event``，本改动同批）：
- 实体条：``<实体名>``（``uq_entities_customer_name``：客户内唯一，故可作键）；
- 关系条：``<from_entity_name>-><kind>:<to_entity_name>``（存储方向；声明方向
  在 ``declared_kind`` 里，不进键）。

**不建索引**：这一列目前只被适配器逐行读与折叠核对用，没有按它过滤的查询；
真出现按语义键检索的需求再单独加索引（本迁移不做投机索引）。

Revision ID: 0054_ontology_event_target_key
Revises: 0053_cell_provision
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054_ontology_event_target_key"
down_revision: str | None = "0053_cell_provision"
branch_labels: str | (Sequence[str] | None) = None
depends_on: str | (Sequence[str] | None) = None

TABLE = "ontology_events"
COLUMN = "target_key"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        col["name"] == column
        for col in sa.inspect(op.get_bind()).get_columns(table)
    )


def upgrade() -> None:
    # create_all 兜底路径下模型可能已经建好这一列（0049 同法容错），加列必须幂等。
    if not _has_table(TABLE):
        return
    if _has_column(TABLE, COLUMN):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.String(length=256), nullable=True))


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    if not _has_column(TABLE, COLUMN):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
