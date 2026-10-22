"""asset catalog（wire-asset-catalog：资产元数据 + 留痕两表）

- ``dataset_asset_meta``：数据集资产元数据补全层（design D1——独立表、
  懒初始化、**不做存量回填**：目录查询 LEFT JOIN + COALESCE 给默认值，
  缺行 = 未补全条目）。``dataset_id`` 主键逻辑外联 ``datasets.id``（同库
  不设 DB 级外键；数据集删除时元数据与留痕由调用方一并清理）。
  敏感度三级 常规|内部|敏感（design D3，刻意不含「公开」）——值域校验在
  路由层（422 带合法清单），DB 不设 CHECK（值域演进免迁移，照
  member_preferences 的纪律）。
- ``asset_meta_events``：元数据修改留痕（design D7——append-only 小表，
  复用 provenance 的 WAL 思路但不引入新审计系统）。只 INSERT（append-only
  由代码路径保证，与 team_events 同款）；政务版留存策略（≥3 年不可改删）
  未来叠加，不重建。

两表落 **datasets 库**（模型挂 DatasetsBase；与 datasets 六表同库同引擎）。

链位说明：单一迁移链线性追加（down_revision=0029 当前链头，照 0029
member_preferences 的挂法）；后续 0031 dataset_quality 链在本迁移之后。

Revision ID: 0030_asset_catalog
Revises: 0029_member_preferences
Create Date: 2026-10-13

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_asset_catalog"
down_revision: Union[str, None] = "0029_member_preferences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dataset_asset_meta",
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("cadence_hint", sa.String(length=16), nullable=True),
        sa.Column("owner_note", sa.String(length=200), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("dataset_id", name=op.f("pk_dataset_asset_meta")),
    )
    op.create_table(
        "asset_meta_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("actor_sub", sa.String(length=64), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_meta_events")),
    )
    # 留痕按 dataset 拉时间线（append-only 读面）
    op.create_index(op.f("ix_asset_meta_events_dataset_id"), "asset_meta_events", ["dataset_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_asset_meta_events_dataset_id"), table_name="asset_meta_events")
    op.drop_table("asset_meta_events")
    op.drop_table("dataset_asset_meta")
