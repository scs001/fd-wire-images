"""add provenance ledger table（wire-provenance-ledger 任务组 2，契约 §D1/D2）

溯源账本单表 ``ledger_events``（链 0011 → 0012）：

- 信封全字段（v 内嵌于信封哈希可复算性，不另存列）+ 入库时间。
- ``ledger`` = customer_id：账本按企业划分，``seq`` 企业内单调。
- 不可变双保险之一：BEFORE UPDATE/DELETE 触发器 RAISE（权限回收在
  部署侧配合；触发器保证任何直连会话也改不动已入账事件）。
- SQLite / PostgreSQL 双方言触发器（方言分支）。

Revision ID: 0012_provenance_ledger
Revises: 0011_datasets
Create Date: 2026-10-07

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_provenance_ledger"
down_revision: Union[str, None] = "0011_datasets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IMMUTABLE_MSG = "ledger_events is append-only (wire-provenance-ledger)"


def _guard_triggers(table: str, msg: str) -> None:
    """为 ``table`` 建 BEFORE UPDATE/DELETE 拒绝触发器（双方言）。"""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        fn = f"_fd_wire_{table}_immutable"
        op.execute(
            f'CREATE FUNCTION {fn}() RETURNS trigger AS $$\n'
            f"BEGIN\n RAISE EXCEPTION '{msg}';\n"
            f"END\n$$ LANGUAGE plpgsql"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE OR DELETE ON {table}\n"
            f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
        )
    else:
        # SQLite 单触发器只支持单事件：拆 update/delete 两个。
        for ev in ("update", "delete"):
            op.execute(
                f"CREATE TRIGGER ledger_events_no_{ev} BEFORE {ev.upper()}"
                " ON ledger_events\n"
                f"BEGIN\n SELECT RAISE(ABORT, '{msg}');\nEND"
            )


def _drop_triggers(table: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS _fd_wire_{table}_immutable()")
    else:
        for ev in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{ev}")


def upgrade() -> None:
    op.create_table(
        "ledger_events",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("ledger", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=100), nullable=False),
        sa.Column("ts_platform", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False),
        sa.Column("subject", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=80), nullable=False),
        sa.Column("nonce", sa.String(length=40), nullable=False),
        sa.Column("prev_hash", sa.String(length=80), nullable=False),
        sa.Column("algo", sa.String(length=16), nullable=False),
        sa.Column("event_hash", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_events")),
        sa.UniqueConstraint("ledger", "seq", name="uq_ledger_events_ledger_seq"),
    )
    op.create_index("ix_ledger_events_ledger", "ledger_events", ["ledger"])
    op.create_index("ix_ledger_events_type", "ledger_events", ["type"])
    _guard_triggers("ledger_events", _IMMUTABLE_MSG)


def downgrade() -> None:
    _drop_triggers("ledger_events")
    op.drop_index("ix_ledger_events_type", table_name="ledger_events")
    op.drop_index("ix_ledger_events_ledger", table_name="ledger_events")
    op.drop_table("ledger_events")
