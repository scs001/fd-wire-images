"""provenance foundation columns & sync history immutability（任务组 1/6）

链 0012 → 0013：

- ``uploads.content_sha256``：原始文件内容哈希（存量行无原件、如实置
  NULL，不伪造基线）——契约 wire-customer-datasets 增量"不可变登记基线"。
- ``datasets.provenance_tier``：溯源档位 off|chain_only|anchored_batch|
  anchored_realtime，存量置 chain_only（溯源起点=启用时点）。
- ``dataset_syncs`` 终态留痕触发器：在飞行 syncing 行允许迁移到终态
  （BEGIN 前守卫 OLD.status='syncing'），终态行禁改禁删——中止 SHALL 落
  终态行而非删除。

Revision ID: 0013_provenance_foundation
Revises: 0012_provenance_ledger
Create Date: 2026-10-07

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_provenance_foundation"
down_revision: Union[str, None] = "0012_provenance_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYNC_MSG = "dataset_syncs terminal rows are immutable (wire-provenance-ledger)"


def upgrade() -> None:
    op.add_column(
        "uploads",
        sa.Column("content_sha256", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "datasets",
        sa.Column(
            "provenance_tier",
            sa.String(length=24),
            server_default="chain_only",
            nullable=False,
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION _fd_wire_dataset_syncs_guard() RETURNS trigger AS $$\n"
            "BEGIN\n"
            "  IF TG_OP = 'DELETE' THEN\n"
            f"    RAISE EXCEPTION '{_SYNC_MSG}';\n"
            "  END IF;\n"
            "  IF OLD.status <> 'syncing' THEN\n"
            f"    RAISE EXCEPTION '{_SYNC_MSG}';\n"
            "  END IF;\n"
            "  RETURN NEW;\n"
            "END\n"
            "$$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER dataset_syncs_terminal_guard BEFORE UPDATE OR DELETE"
            " ON dataset_syncs\n"
            "FOR EACH ROW EXECUTE FUNCTION _fd_wire_dataset_syncs_guard()"
        )
    else:
        # SQLite 无法在单触发器内按 TG_OP 分支：拆 update/delete 两个触发器。
        op.execute(
            "CREATE TRIGGER dataset_syncs_terminal_guard_update BEFORE UPDATE"
            " ON dataset_syncs\n"
            "BEGIN\n"
            "  SELECT RAISE(ABORT, 'update blocked: terminal sync rows are"
            " immutable (wire-provenance-ledger)')\n"
            "  WHERE OLD.status <> 'syncing';\n"
            "END"
        )
        op.execute(
            "CREATE TRIGGER dataset_syncs_terminal_guard_delete BEFORE DELETE"
            " ON dataset_syncs\n"
            "BEGIN\n"
            "  SELECT RAISE(ABORT, 'delete blocked: sync history is append-only"
            " (wire-provenance-ledger)');\n"
            "END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS dataset_syncs_terminal_guard ON dataset_syncs")
        op.execute("DROP FUNCTION IF EXISTS _fd_wire_dataset_syncs_guard()")
    else:
        op.execute("DROP TRIGGER IF EXISTS dataset_syncs_terminal_guard_update")
        op.execute("DROP TRIGGER IF EXISTS dataset_syncs_terminal_guard_delete")
    op.drop_column("datasets", "provenance_tier")
    op.drop_column("uploads", "content_sha256")
