"""实体语义层 v2 列（wire-entity-v2 任务 1.1）。

- ``entities`` 增 ``display_column`` / ``column_notes``（可选属性元数据；
  存量行空值零漂移——不带新键的 v0 读取形状逐字不变）。
- ``entity_relationships`` 增 ``declared_kind``（1:n 声明的归一化方向标记）
  与 ``junction_dataset_id`` / ``junction_source_column`` /
  ``junction_target_column``（m:n 经中间数据集声明的三键）。

全部可空列，无回填；回滚逐列删除。

Revision ID: 0042_entity_v2_columns
Revises: 0041_dataset_parent_refs
Create Date: 2026-10-29

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_entity_v2_columns"
down_revision: Union[str, None] = "0041_dataset_parent_refs"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_missing(table: str, columns: list[sa.Column]) -> None:
    """只补库里缺的列（create_all 兜底可能已抢先建列）。"""
    inspect = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspect.get_columns(table)}
    missing = [c for c in columns if c.name not in existing]
    if not missing:
        return
    with op.batch_alter_table(table) as batch:
        for column in missing:
            batch.add_column(column)


def upgrade() -> None:
    _add_missing(
        "entities",
        [
            sa.Column("display_column", sa.String(length=200), nullable=True),
            sa.Column("column_notes", sa.JSON(), nullable=True),
        ],
    )
    _add_missing(
        "entity_relationships",
        [
            sa.Column("declared_kind", sa.String(length=8), nullable=True),
            sa.Column("junction_dataset_id", sa.String(length=36), nullable=True),
            sa.Column(
                "junction_source_column", sa.String(length=200), nullable=True
            ),
            sa.Column(
                "junction_target_column", sa.String(length=200), nullable=True
            ),
        ],
    )


def downgrade() -> None:
    with op.batch_alter_table("entity_relationships") as batch:
        batch.drop_column("junction_target_column")
        batch.drop_column("junction_source_column")
        batch.drop_column("junction_dataset_id")
        batch.drop_column("declared_kind")
    with op.batch_alter_table("entities") as batch:
        batch.drop_column("column_notes")
        batch.drop_column("display_column")
