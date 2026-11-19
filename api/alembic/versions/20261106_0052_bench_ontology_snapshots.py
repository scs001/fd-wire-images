"""知验台本体拉取快照两表（add-bench-ontology-pull-client）。

落元数据库（与 demands/contact 同一条 alembic 链）。两表=发布快照本体 /
拉取日志：

- ``bench_ontology_snapshots``：law-bench 分发面（C3）拉到的发布快照
  **整包 JSONB 冻结**——append-only，无 UPDATE/DELETE 路径；幂等键
  ``UNIQUE (domain_key, digest)``（同 digest 重复拉取零新行）；版本与
  digest 随快照冻结不可变。**平台级数据**：刻意不挂 customer_id/tenant，
  与 wire 客户租户表分离（design D2）；读面仅内部端点（X-Internal-Token，
  沿 demands 先例）。
- ``bench_pull_log``：拉取史（成败都记，append-only）——失败如实留痕
  （error 摘要脱敏），绝不误标成功；索引 (domain_key, pulled_at) 承载
  运营排查「最近一次拉到哪版/为何失败」。

``payload`` 双方言类型：SQLite 用 JSON、PG 用 JSONB（0020/0047 同款）；
``id`` 同理 SQLite INTEGER / PG BIGINT（BIGSERIAL）。写入口唯一是
``fd_wire_api.bench_pull.store.save_snapshot``（ON CONFLICT DO NOTHING +
SELECT 既有行，幂等）。

Revision ID: 0052_bench_ontology_snapshots
Revises: 0051_contact_intake
Create Date: 2026-11-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0052_bench_ontology_snapshots"
down_revision: Union[str, None] = "0051_contact_intake"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None

#: payload 双方言类型：SQLite 用 JSON、PG 用 JSONB（0020/0047 同款）。
_JSON_DUAL = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
#: id 双方言：SQLite 用 INTEGER（rowid 别名要求类型名恰为 INTEGER），
#: PG 用 BIGINT（BIGSERIAL，快照表体量大预留）。
_ID_DUAL = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _has_table(name: str) -> bool:
    """create_all 兜底可能已抢建过表——存在即跳过（可重跑）。"""
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("bench_ontology_snapshots"):
        op.create_table(
            "bench_ontology_snapshots",
            sa.Column("id", _ID_DUAL, autoincrement=True, nullable=False),
            sa.Column("domain_key", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("digest", sa.Text(), nullable=False),
            sa.Column("payload", _JSON_DUAL, nullable=False),
            sa.Column(
                "pulled_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_bench_ontology_snapshots")),
            sa.UniqueConstraint(
                "domain_key",
                "digest",
                name="uq_bench_ontology_snapshots_domain_digest",
            ),
        )
        op.create_index(
            "ix_bench_ontology_snapshots_domain_key",
            "bench_ontology_snapshots",
            ["domain_key"],
        )

    if not _has_table("bench_pull_log"):
        op.create_table(
            "bench_pull_log",
            sa.Column("id", _ID_DUAL, autoincrement=True, nullable=False),
            sa.Column("domain_key", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=True),
            sa.Column("digest", sa.Text(), nullable=True),
            sa.Column("ok", sa.Boolean(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "pulled_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_bench_pull_log")),
        )
        op.create_index(
            "ix_bench_pull_log_domain_pulled",
            "bench_pull_log",
            ["domain_key", "pulled_at"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_bench_pull_log_domain_pulled", table_name="bench_pull_log"
    )
    op.drop_table("bench_pull_log")
    op.drop_index(
        "ix_bench_ontology_snapshots_domain_key",
        table_name="bench_ontology_snapshots",
    )
    op.drop_table("bench_ontology_snapshots")
