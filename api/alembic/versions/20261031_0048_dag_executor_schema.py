"""DAG 执行器 schema 先行（wire-dag-executor 批 1 / design D1–D3、D8）。

本迁移**零行为变化**：只加列与表，既有 `run_pipeline` 同步线性执行路径逐字不变。

- ``pipeline_runs``：``claimed_by`` / ``heartbeat_at`` / ``cancel_requested_at``
  （租约队列与取消的簿记位；批 2 才被读写）；
- ``pipeline_run_nodes``：节点级状态（queued|running|succeeded|failed|skipped +
  尝试次数 + 起止时间 + 错误），DAG 执行与断点恢复的事实源；
- ``transform_pipeline_slots.node_key``：槽位键由 (管线,版本,seq) 扩为
  (管线,版本,node_key)；存量行回填 ``step-<seq>``（与缺省节点名逐字一致），
  既有唯一约束保留（批 2 再切键，避免本批改约束影响存量写入路径）。

双方言（sqlite + PG）+ create_all 兜底幂等（沿 0041–0047 容错化先例）。

Revision ID: 0048_dag_executor_schema
Revises: 0047_dataset_acl
Create Date: 2026-10-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0048_dag_executor_schema"
down_revision: Union[str, None] = "0047_dataset_acl"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # ── 1. pipeline_runs：租约/取消簿记位（批 2 才读写）──────────────────
    run_cols = _columns("pipeline_runs")
    for name, column in (
        ("claimed_by", sa.Column("claimed_by", sa.String(length=64), nullable=True)),
        ("heartbeat_at", sa.Column("heartbeat_at", sa.DateTime(), nullable=True)),
        (
            "cancel_requested_at",
            sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        ),
    ):
        if name not in run_cols:
            op.add_column("pipeline_runs", column)

    # ── 2. pipeline_run_nodes：节点级状态与断点恢复事实源 ────────────────
    if not _has_table("pipeline_run_nodes"):
        op.create_table(
            "pipeline_run_nodes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("node_key", sa.String(length=200), nullable=False),
            sa.Column("op", sa.String(length=16), nullable=False),
            #: queued | running | succeeded | failed | skipped
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("output_dataset_id", sa.String(length=36), nullable=True),
            sa.Column("row_count", sa.BigInteger(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error_text", sa.Text(), nullable=True),
        )
    if not any(
        ix["column_names"] == ["run_id"]
        for ix in sa.inspect(op.get_bind()).get_indexes("pipeline_run_nodes")
    ):
        op.create_index("ix_pipeline_run_nodes_run_id", "pipeline_run_nodes", ["run_id"])

    # ── 3. slots.node_key：存量回填 step-<seq>（与缺省节点名一致）────────
    slot_cols = _columns("transform_pipeline_slots")
    if "node_key" not in slot_cols:
        op.add_column(
            "transform_pipeline_slots",
            sa.Column("node_key", sa.String(length=200), nullable=True),
        )
        op.execute(
            "UPDATE transform_pipeline_slots SET node_key = 'step-' || seq "
            "WHERE node_key IS NULL"
        )


def downgrade() -> None:
    slot_cols = _columns("transform_pipeline_slots")
    if "node_key" in slot_cols:
        op.drop_column("transform_pipeline_slots", "node_key")
    if _has_table("pipeline_run_nodes"):
        op.drop_table("pipeline_run_nodes")
    run_cols = _columns("pipeline_runs")
    for name in ("cancel_requested_at", "heartbeat_at", "claimed_by"):
        if name in run_cols:
            op.drop_column("pipeline_runs", name)
