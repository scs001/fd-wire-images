"""xingyan 快照三表（xingyan-content-supply）

落元数据库（与 billing/packs/refresh_schedules 同一条 alembic 链）。
三表=条目快照 / claim 骨架 / 摄取水位；写入口唯一是摄取管道
（``fd_wire_api.xingyan.sync.XingyanFeedSync``），请求路径只拿只读会话。

Revision ID: 0046_xingyan_content_supply
Revises: 0045_domain_instance_result_ref
Create Date: 2026-10-31

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0046_xingyan_content_supply"
down_revision: Union[str, None] = "0045_domain_instance_result_ref"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def _has_table(name: str) -> bool:
    """create_all 兜底可能已抢建过表——存在即跳过（可重跑）。"""
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("xingyan_items"):
        op.create_table(
            "xingyan_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("slug", sa.String(length=200), nullable=False),
            sa.Column("zone", sa.String(length=32), nullable=False),
            sa.Column("item_type", sa.String(length=64), nullable=False),
            sa.Column("item_order", sa.Integer(), nullable=True),
            sa.Column("title_zh", sa.String(length=512), nullable=False),
            sa.Column("title_en", sa.String(length=512), nullable=False),
            sa.Column("summary_zh", sa.Text(), nullable=False),
            sa.Column("summary_en", sa.Text(), nullable=False),
            sa.Column("published_at", sa.String(length=32), nullable=True),
            sa.Column("updated_at", sa.String(length=32), nullable=True),
            sa.Column("tags_json", sa.JSON(), nullable=False),
            sa.Column("body_zh", sa.Text(), nullable=False),
            sa.Column("body_en", sa.Text(), nullable=False),
            sa.Column("sources_json", sa.JSON(), nullable=False),
            sa.Column("claim_count", sa.Integer(), nullable=False),
            sa.Column("url_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("source_sha", sa.String(length=64), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_xingyan_items")),
            sa.UniqueConstraint("slug", name="uq_xingyan_items_slug"),
        )
        op.create_index("ix_xingyan_items_zone", "xingyan_items", ["zone"])
        op.create_index("ix_xingyan_items_status", "xingyan_items", ["status"])
        op.create_index("ix_xingyan_items_published", "xingyan_items", ["published_at"])

    if not _has_table("xingyan_claims"):
        op.create_table(
            "xingyan_claims",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("slug", sa.String(length=200), nullable=False),
            sa.Column("claim_id", sa.String(length=16), nullable=False),
            sa.Column("claim_type", sa.String(length=16), nullable=False),
            sa.Column("section_zh", sa.String(length=512), nullable=False),
            sa.Column("section_en", sa.String(length=512), nullable=False),
            sa.Column("evidence_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("source_sha", sa.String(length=64), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_xingyan_claims")),
            sa.UniqueConstraint("slug", "claim_id", name="uq_xingyan_claims_slug_claim"),
        )
        op.create_index("ix_xingyan_claims_claim_id", "xingyan_claims", ["claim_id"])
        op.create_index("ix_xingyan_claims_status", "xingyan_claims", ["status"])

    if not _has_table("xingyan_sync_state"):
        op.create_table(
            "xingyan_sync_state",
            sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
            sa.Column("source_sha", sa.String(length=64), nullable=False),
            sa.Column("schema_version", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("last_success_at", sa.DateTime(), nullable=True),
            sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("items_total", sa.Integer(), nullable=False),
            sa.Column("claims_total", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_xingyan_sync_state")),
        )


def downgrade() -> None:
    op.drop_table("xingyan_sync_state")
    op.drop_index("ix_xingyan_claims_status", table_name="xingyan_claims")
    op.drop_index("ix_xingyan_claims_claim_id", table_name="xingyan_claims")
    op.drop_table("xingyan_claims")
    op.drop_index("ix_xingyan_items_published", table_name="xingyan_items")
    op.drop_index("ix_xingyan_items_status", table_name="xingyan_items")
    op.drop_index("ix_xingyan_items_zone", table_name="xingyan_items")
    op.drop_table("xingyan_items")
