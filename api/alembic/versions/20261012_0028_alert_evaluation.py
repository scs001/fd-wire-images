"""alert evaluation（wire-alert-evaluation：告警评估状态/事件两表）

两表落 **ai 库**（与 artifacts 同引擎同链，挂 AiBase）：

- ``alert_states``：每条 alert_rule 产物一行的持久状态（artifact_id 主键
  即唯一；state ok|firing、last_value、last_evaluated_at〔due 判定驱动列，
  error 路径同样推进〕、last_notified_at〔冷却再通知基准〕、updated_at）。
  ``artifact_id`` 真外键引用 ``artifacts.id``（同库约束成立）。
- ``alert_events``：append-only 事件流（firing/resolved/error/notify_failed
  四类，带值与阈值；detail 存错误/投递失败原因 ≤500 截断）。倒序时间线
  索引 (customer_id, id desc) 与 (artifact_id, id desc)。

链位说明：单一迁移链线性追加（down_revision=0027 当前链头，照 0027
team_spaces 的挂法）；运行时 create_all 兜底见 alerts/db.init_db。

Revision ID: 0028_alert_evaluation
Revises: 0027_team_spaces
Create Date: 2026-10-12

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_alert_evaluation"
down_revision: Union[str, None] = "0027_team_spaces"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "alert_states",
        sa.Column("artifact_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_alert_states_artifact_id"),
        ),
        sa.PrimaryKeyConstraint("artifact_id", name=op.f("pk_alert_states")),
    )
    op.create_table(
        "alert_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_alert_events_artifact_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_events")),
    )
    # 倒序时间线两索引：(customer_id, id desc) 与 (artifact_id, id desc)
    op.create_index(
        op.f("ix_alert_events_customer_id"),
        "alert_events",
        ["customer_id", sa.text("id DESC")],
    )
    op.create_index(
        op.f("ix_alert_events_artifact_id"),
        "alert_events",
        ["artifact_id", sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_alert_events_artifact_id"), table_name="alert_events")
    op.drop_index(op.f("ix_alert_events_customer_id"), table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_table("alert_states")
