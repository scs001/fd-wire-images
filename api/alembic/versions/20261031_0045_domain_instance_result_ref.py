"""域动作实例结果引用列（wire-entity-write-surface 任务 2.3）。

``domain_action_instances`` 增 ``result_ref``（varchar(128) 可空）：
metadata 效果（内置声明动作 ``entity-declare`` / ``entity-relation-declare``）
执行成功后回填结果引用——实体 id 或关系 id 的字符串形态；insert|update 效果
恒 None（仍走 ``correction_id``）。

声明落元数据不入修正层（ADR-0006 修正层=数据行 overlay 语义，实体/关系是
客户域元数据）——执行结果如实可追溯是台账完整性要求（wire-domain-actions
「审计台账」修订段）。

存量行 NULL（零回填义务）；回滚整列删除。

Revision ID: 0045_domain_instance_result_ref
Revises: 0044_dataset_source_config
Create Date: 2026-10-31

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_domain_instance_result_ref"
down_revision: Union[str, None] = "0044_dataset_source_config"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    if "domain_action_instances" not in inspect.get_table_names():
        return
    existing = {c["name"] for c in inspect.get_columns("domain_action_instances")}
    if "result_ref" in existing:
        return
    with op.batch_alter_table("domain_action_instances") as batch:
        batch.add_column(
            sa.Column("result_ref", sa.String(length=128), nullable=True)
        )


def downgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    if "domain_action_instances" not in inspect.get_table_names():
        return
    existing = {c["name"] for c in inspect.get_columns("domain_action_instances")}
    if "result_ref" not in existing:
        return
    with op.batch_alter_table("domain_action_instances") as batch:
        batch.drop_column("result_ref")
