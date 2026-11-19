"""add metering tables（wire-platform-v1 任务 4.1 建链首迁）

计量管道两张表：

- ``metering_records``：逐调用计量记录，``request_id + server_name`` 唯一
  （重复摄取幂等）；
- ``metering_anomalies``：计费异常留痕（缺 rows_returned 等，不静默），
  ``request_id + server_name + reason`` 唯一。

Revision ID: 0001
Revises:
Create Date: 2026-10-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metering_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(length=200), nullable=False),
        sa.Column("caller", sa.String(length=255), nullable=False),
        sa.Column("caller_principal", sa.String(length=255), nullable=True),
        sa.Column("credential_track", sa.String(length=16), nullable=False),
        sa.Column("server_name", sa.String(length=255), nullable=False),
        sa.Column("server_path", sa.String(length=512), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column("rows_returned", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(
            "CURRENT_TIMESTAMP"
        ), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metering_records")),
        sa.UniqueConstraint("request_id", "server_name", name="uq_metering_request_server"),
    )
    op.create_index(
        "ix_metering_records_track_time",
        "metering_records",
        ["credential_track", "event_timestamp"],
        unique=False,
    )
    op.create_index(
        "ix_metering_records_caller_time",
        "metering_records",
        ["caller", "event_timestamp"],
        unique=False,
    )

    op.create_table(
        "metering_anomalies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=200), nullable=True),
        sa.Column("server_name", sa.String(length=255), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column("caller", sa.String(length=255), nullable=True),
        sa.Column("credential_track", sa.String(length=16), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text(
            "CURRENT_TIMESTAMP"
        ), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metering_anomalies")),
        sa.UniqueConstraint(
            "request_id", "server_name", "reason", name="uq_anomaly_request_reason"
        ),
    )
    op.create_index(
        "ix_metering_anomalies_time",
        "metering_anomalies",
        ["event_timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_metering_anomalies_time", table_name="metering_anomalies")
    op.drop_table("metering_anomalies")
    op.drop_index("ix_metering_records_caller_time", table_name="metering_records")
    op.drop_index("ix_metering_records_track_time", table_name="metering_records")
    op.drop_table("metering_records")
