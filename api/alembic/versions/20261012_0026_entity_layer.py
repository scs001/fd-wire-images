"""entity layer（wire-entity-layer：轻量实体语义层 v0）

两张表落**元数据库**（与 billing/packs/schedules 同链）：

- ``entities``：实体声明 = 业务名（客户内唯一 ``(customer_id, name)``）+
  绑定本企业数据集（一集一体 ``(customer_id, dataset_id)``）+ 主键列
  ``pk_columns``（JSON 非空数组，声明时校验 ⊆ dataset.columns_json，
  之后漂移不追改——列真相在 datasets 库）+ 可选描述。
  ``dataset_id`` 是跨库弱引用（datasets 表在 wire_datasets），不设外键。
- ``entity_relationships``：源实体列 → 目标实体**主键列**的有向关系
  （kind ∈ n:1|1:1），四元组（源实体/源列/目标实体/目标列）唯一；
  级联删除走 service 显式（不依赖 DB CASCADE——SQLite/PG 双方言一致）。

链位说明：单一迁移链线性追加（down_revision=0025 当前链头，照 0022
packs 的挂法）；0025（datasets 列变更）与 0026（本迁移）在生产各自
对 wire_platform / wire_datasets 跑 ``upgrade head`` 时先后经过，
两库 alembic_version 同收敛到 0026。

Revision ID: 0026_entity_layer
Revises: 0025_connector_incremental_sync
Create Date: 2026-10-12

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_entity_layer"
down_revision: Union[str, None] = "0025_connector_incremental_sync"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("pk_columns", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
        sa.UniqueConstraint("customer_id", "name", name="uq_entities_customer_name"),
        sa.UniqueConstraint(
            "customer_id", "dataset_id", name="uq_entities_customer_dataset"
        ),
    )
    # 客户维度隔离查询（列表/图谱）
    op.create_index(op.f("ix_entities_customer_id"), "entities", ["customer_id"])
    # 一集一体唯一检查与摘要组装的探测查询
    op.create_index(op.f("ix_entities_dataset_id"), "entities", ["dataset_id"])
    op.create_table(
        "entity_relationships",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("from_entity_id", sa.Integer(), nullable=False),
        sa.Column("from_column", sa.String(length=200), nullable=False),
        sa.Column("to_entity_id", sa.Integer(), nullable=False),
        sa.Column("to_column", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_relationships")),
        sa.UniqueConstraint(
            "from_entity_id",
            "from_column",
            "to_entity_id",
            "to_column",
            name="uq_entity_relationships_quad",
        ),
    )
    # 客户维度图谱边查询 + 级联删的两端命中
    op.create_index(
        op.f("ix_entity_relationships_customer_id"),
        "entity_relationships",
        ["customer_id"],
    )
    op.create_index(
        op.f("ix_entity_relationships_from_entity_id"),
        "entity_relationships",
        ["from_entity_id"],
    )
    op.create_index(
        op.f("ix_entity_relationships_to_entity_id"),
        "entity_relationships",
        ["to_entity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_entity_relationships_to_entity_id"),
        table_name="entity_relationships",
    )
    op.drop_index(
        op.f("ix_entity_relationships_from_entity_id"),
        table_name="entity_relationships",
    )
    op.drop_index(
        op.f("ix_entity_relationships_customer_id"),
        table_name="entity_relationships",
    )
    op.drop_table("entity_relationships")
    op.drop_index(op.f("ix_entities_dataset_id"), table_name="entities")
    op.drop_index(op.f("ix_entities_customer_id"), table_name="entities")
    op.drop_table("entities")
