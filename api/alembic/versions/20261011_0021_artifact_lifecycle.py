"""artifact lifecycle（wire-artifacts-persistence：产物转正）

产物一等实体两表（与 ai 切片同引擎，运行时 create_all 兜底、生产走本迁移）：

- ``artifacts``：跨会话持久化的产物记录——kind 五类（dashboard /
  composite_indicator / research_report / alert_rule / refresh_schedule）、
  title、状态机（draft/published/archived）、版本计数、来源会话指针
  （origin_conversation_id 可空，逻辑引用 ai_conversations 不设外键）、
  状态迁移操作记录（status_history，append-only）。
- ``artifact_versions``：spec 版本链（append-only——每次保存追加一行，
  (artifact_id, version) 唯一；历史版本只读，当前 spec = 最大 version）。

Revision ID: 0021_artifact_lifecycle
Revises: 0020_free_tier_datasets
Create Date: 2026-10-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_artifact_lifecycle"
down_revision: Union[str, None] = "0020_free_tier_datasets"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("origin_conversation_id", sa.Integer(), nullable=True),
        sa.Column("status_history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    op.create_index(op.f("ix_artifacts_customer_id"), "artifacts", ["customer_id"])
    op.create_index(op.f("ix_artifacts_kind"), "artifacts", ["kind"])
    op.create_index(op.f("ix_artifacts_status"), "artifacts", ["status"])
    op.create_index(
        op.f("ix_artifacts_origin_conversation_id"),
        "artifacts",
        ["origin_conversation_id"],
    )
    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_artifact_versions_artifact_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_versions")),
        sa.UniqueConstraint("artifact_id", "version", name="uq_artifact_versions_version"),
    )
    op.create_index(
        op.f("ix_artifact_versions_artifact_id"),
        "artifact_versions",
        ["artifact_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_artifact_versions_artifact_id"), table_name="artifact_versions"
    )
    op.drop_table("artifact_versions")
    op.drop_index(
        op.f("ix_artifacts_origin_conversation_id"), table_name="artifacts"
    )
    op.drop_index(op.f("ix_artifacts_status"), table_name="artifacts")
    op.drop_index(op.f("ix_artifacts_kind"), table_name="artifacts")
    op.drop_index(op.f("ix_artifacts_customer_id"), table_name="artifacts")
    op.drop_table("artifacts")
