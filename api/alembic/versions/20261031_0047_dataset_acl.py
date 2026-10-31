"""数据集级访问授权（wire-dataset-acl D1）。

- ``datasets.acl_mode`` varchar(16) NOT NULL DEFAULT 'team'——默认值即零迁移
  兼容面（存量全部按「本队全员可见」语义，行为逐字节不变）；
- ``dataset_grants``：成员粒度授权名单（view|edit），
  ``UNIQUE(dataset_id, logto_sub)``；无 FK（成员表在平台库、本表在数据集库，
  跨库以 sub 弱引用，服务层校验）；
- ``dataset_acl_events``：append-only 审计（mode_change|grants_replace，
  payload_json 存旧/新模式与名单快照）。

双方言（sqlite + PG）：字符串默认值沿用 ``sa.text("'team'")`` 写法（0034
先例）；payload_json 用 ``JSON().with_variant(JSONB, 'postgresql')``（0020
先例）。迁移幂等：create_all 兜底可能已抢建表/列，按对象存在性分支
（0041–0046 容错化先例）。

Revision ID: 0047_dataset_acl
Revises: 0046_xingyan_content_supply
Create Date: 2026-10-31

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_dataset_acl"
down_revision: Union[str, None] = "0046_xingyan_content_supply"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None

#: payload_json 双方言类型：SQLite 用 JSON、PG 用 JSONB（0020 同款）
_JSON_DUAL = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _has_table(name: str) -> bool:
    """create_all 兜底可能已抢建过表——存在即跳过（可重跑）。"""
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _covered_indexes(table: str) -> set[tuple[str, ...]]:
    return {
        tuple(ix["column_names"])
        for ix in sa.inspect(op.get_bind()).get_indexes(table)
    }


def upgrade() -> None:
    # ── 1. datasets.acl_mode（存量行由 server_default 回填 'team'）─────────
    if "acl_mode" not in _columns("datasets"):
        op.add_column(
            "datasets",
            sa.Column(
                "acl_mode",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'team'"),
            ),
        )

    # ── 2. dataset_grants（授权名单；唯一约束 + dataset_id 索引）─────────
    if not _has_table("dataset_grants"):
        op.create_table(
            "dataset_grants",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("dataset_id", sa.String(length=36), nullable=False),
            sa.Column("logto_sub", sa.String(length=64), nullable=False),
            sa.Column("permission", sa.String(length=16), nullable=False),
            sa.Column("created_by_sub", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "dataset_id", "logto_sub", name="uq_dataset_grants_dataset_sub"
            ),
        )
    if ("dataset_id",) not in _covered_indexes("dataset_grants"):
        op.create_index(
            "ix_dataset_grants_dataset_id", "dataset_grants", ["dataset_id"]
        )

    # ── 3. dataset_acl_events（append-only 审计）─────────────────────────
    if not _has_table("dataset_acl_events"):
        op.create_table(
            "dataset_acl_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("dataset_id", sa.String(length=36), nullable=False),
            sa.Column("actor_sub", sa.String(length=64), nullable=False),
            sa.Column("action", sa.String(length=16), nullable=False),
            sa.Column("payload_json", _JSON_DUAL, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if ("dataset_id",) not in _covered_indexes("dataset_acl_events"):
        op.create_index(
            "ix_dataset_acl_events_dataset_id", "dataset_acl_events", ["dataset_id"]
        )


def downgrade() -> None:
    if _has_table("dataset_acl_events"):
        op.drop_table("dataset_acl_events")
    if _has_table("dataset_grants"):
        op.drop_table("dataset_grants")
    if "acl_mode" in _columns("datasets"):
        op.drop_column("datasets", "acl_mode")
