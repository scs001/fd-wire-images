"""dataset quality snapshots（wire-asset-catalog 任务组 4：质量快照表）

单表 ``dataset_quality_snapshots``（挂 datasets 库，与 datasets 切片同链）：

- append-only：应用层只 INSERT + 保留期裁剪（默认每数据集留 30 条，
  ``FD_WIRE_QUALITY_HISTORY_RETENTION`` 可调），无 UPDATE 路径、无改删
  端点——历史快照不可改（spec「快照历史与趋势」）。
- 只承载聚合计数与比率（行数/键唯一性比例/按列样本空值率/抽样超限标
  记），无任何数据值列——质量计算在 cell 引擎侧（红线 2）。
- ``status`` ∈ {ok, failed, unsupported}：引擎不支持（旧 cell 无质量
  工具）落 unsupported 降级态，失败如实落 failed + error_text。

链位说明：单一迁移链线性追加（down_revision=0030_asset_catalog——姊妹
切片的资产元数据表迁移，同批合入；双方言 sqlite/PG 由既有 ``upgrade
head`` 机制覆盖）。

Revision ID: 0031_dataset_quality
Revises: 0030_asset_catalog
Create Date: 2026-10-13

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_dataset_quality"
down_revision: Union[str, None] = "0030_asset_catalog"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "dataset_quality_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("triggered_by", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("pk_unique_ratio", sa.Float(), nullable=True),
        sa.Column(
            "pk_declared", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("null_rates_json", sa.JSON(), nullable=True),
        sa.Column(
            "sample_limit_hit",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_quality_snapshots")),
    )
    # 快照历史按数据集拉取（保留期裁剪也按 dataset_id 扫）
    op.create_index(
        op.f("ix_dataset_quality_snapshots_dataset_id"),
        "dataset_quality_snapshots",
        ["dataset_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dataset_quality_snapshots_dataset_id"),
        table_name="dataset_quality_snapshots",
    )
    op.drop_table("dataset_quality_snapshots")
