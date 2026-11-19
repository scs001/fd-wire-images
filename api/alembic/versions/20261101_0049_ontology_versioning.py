"""本体版本机制（wire-ontology-versioning 任务 1.1/1.2 / design D1、D4、D8）。

**纯增量**：只建两表与索引，零存量改写、零存量读面依赖。

- ``ontology_events``：本体变更事件流（append-only）。与本体同库同事务
  （design D1）——事件写失败即本体写失败。``(customer_id, seq)`` 建索引供
  客户内折叠与「未发布变更」计数；``(customer_id)`` 供按客户扫事件。
- ``ontology_snapshots``：版本快照（每客户版本号单调）。``(customer_id,
  version)`` 唯一；``(customer_id, created_at)`` 索引供时间线倒序读。

双方言（sqlite + PG）+ create_all 兜底幂等（沿 0041–0048 容错化先例）。

Revision ID: 0049_ontology_versioning
Revises: 0048_dag_executor_schema
Create Date: 2026-11-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_ontology_versioning"
down_revision: Union[str, None] = "0048_dag_executor_schema"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_index(table: str, name: str) -> bool:
    return any(
        ix["name"] == name
        for ix in sa.inspect(op.get_bind()).get_indexes(table)
    )


def upgrade() -> None:
    # ── 1. ontology_events：变更事件流（append-only）─────────────────────
    if not _has_table("ontology_events"):
        op.create_table(
            "ontology_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("customer_id", sa.String(length=64), nullable=False),
            #: 客户内顺序号（1 起单调；插入时 max+1）
            sa.Column("seq", sa.Integer(), nullable=False),
            #: create | update | delete
            sa.Column("op", sa.String(length=16), nullable=False),
            #: entity | relationship
            sa.Column("target_kind", sa.String(length=16), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=False),
            #: 变更前后全量（create 的 before / delete 的 after 为 null）
            sa.Column("before_json", sa.JSON(), nullable=True),
            sa.Column("after_json", sa.JSON(), nullable=True),
            sa.Column("actor", sa.String(length=200), nullable=True),
            #: action_gate | console | rollback
            sa.Column("via", sa.String(length=16), nullable=False),
            #: 舰队血缘（经闸执行时带入）
            sa.Column("source_ref", sa.String(length=256), nullable=True),
            sa.Column("source_instance_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if not _has_index("ontology_events", "ix_ontology_events_customer_seq"):
        op.create_index(
            "ix_ontology_events_customer_seq",
            "ontology_events",
            ["customer_id", "seq"],
        )

    # ── 2. ontology_snapshots：版本快照（客户内版本号单调）────────────────
    if not _has_table("ontology_snapshots"):
        op.create_table(
            "ontology_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("customer_id", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            #: 规范 manifest 的 sha256
            sa.Column("digest", sa.String(length=64), nullable=False),
            sa.Column("manifest_json", sa.JSON(), nullable=False),
            #: daily | manual
            sa.Column("trigger", sa.String(length=16), nullable=False),
            sa.Column("label", sa.String(length=80), nullable=True),
            sa.Column("created_by", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "customer_id",
                "version",
                name="uq_ontology_snapshots_customer_version",
            ),
        )
    if not _has_index("ontology_snapshots", "ix_ontology_snapshots_customer_created"):
        op.create_index(
            "ix_ontology_snapshots_customer_created",
            "ontology_snapshots",
            ["customer_id", "created_at"],
        )


def downgrade() -> None:
    if _has_table("ontology_snapshots"):
        op.drop_table("ontology_snapshots")
    if _has_table("ontology_events"):
        op.drop_table("ontology_events")
