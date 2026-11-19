"""transform pipelines（wire-transform-pipelines：管线五表）

五张表挂 **datasets 库**（与 datasets/assets/quality 同链——输出槽位绑定
输出数据集登记须与 ``datasets`` 同事务，design D6）：

- ``transform_pipelines``：管线元信息 + current_version 指针；
- ``transform_pipeline_versions``：版本步骤定义（append-only，无 UPDATE
  代码路径；UNIQUE(pipeline_id, version)）；
- ``pipeline_runs`` / ``pipeline_run_steps``：run/step 两级运行留痕
  （只追加；失败步骤后的剩余步骤无行——失败隔离在应用层保证）；
- ``transform_pipeline_slots``：输出槽位（管线, 版本, 步骤序号）→ 稳定
  输出数据集（UNIQUE(pipeline_id, version, seq)）。

本迁移无布尔列（布尔 server_default 的 ``sa.text("false")`` 纪律此处
无涉）；``steps_json`` 沿 0011 ``columns_json`` 先例用 sa.JSON（ORM 侧
JSONB 变体为 create_all 兜底形态，双方言由既有 ``upgrade head`` 覆盖）。

链位说明：单一迁移链线性追加（down_revision=0031 当前链头）。

Revision ID: 0032_transform_pipelines
Revises: 0031_dataset_quality
Create Date: 2026-10-13

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_transform_pipelines"
down_revision: Union[str, None] = "0031_dataset_quality"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "transform_pipelines",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("base_dataset_id", sa.String(length=36), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transform_pipelines")),
    )
    op.create_index(
        op.f("ix_transform_pipelines_customer_id"),
        "transform_pipelines",
        ["customer_id"],
    )
    op.create_table(
        "transform_pipeline_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_dataset_id", sa.String(length=36), nullable=False),
        sa.Column("steps_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transform_pipeline_versions")),
        sa.UniqueConstraint(
            "pipeline_id", "version", name="uq_pipeline_versions_id_ver"
        ),
    )
    op.create_index(
        op.f("ix_transform_pipeline_versions_pipeline_id"),
        "transform_pipeline_versions",
        ["pipeline_id"],
    )
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_runs")),
    )
    op.create_index(op.f("ix_pipeline_runs_pipeline_id"), "pipeline_runs", ["pipeline_id"])
    op.create_table(
        "pipeline_run_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("op", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("output_dataset_id", sa.String(length=36), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_run_steps")),
    )
    op.create_index(
        op.f("ix_pipeline_run_steps_run_id"), "pipeline_run_steps", ["run_id"]
    )
    op.create_table(
        "transform_pipeline_slots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transform_pipeline_slots")),
        sa.UniqueConstraint(
            "pipeline_id", "version", "seq", name="uq_pipeline_slots_pipe_ver_seq"
        ),
    )
    op.create_index(
        op.f("ix_transform_pipeline_slots_pipeline_id"),
        "transform_pipeline_slots",
        ["pipeline_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_transform_pipeline_slots_pipeline_id"),
        table_name="transform_pipeline_slots",
    )
    op.drop_table("transform_pipeline_slots")
    op.drop_index(op.f("ix_pipeline_run_steps_run_id"), table_name="pipeline_run_steps")
    op.drop_table("pipeline_run_steps")
    op.drop_index(op.f("ix_pipeline_runs_pipeline_id"), table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
    op.drop_index(
        op.f("ix_transform_pipeline_versions_pipeline_id"),
        table_name="transform_pipeline_versions",
    )
    op.drop_table("transform_pipeline_versions")
    op.drop_index(
        op.f("ix_transform_pipelines_customer_id"), table_name="transform_pipelines"
    )
    op.drop_table("transform_pipelines")
