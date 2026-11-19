"""team spaces（wire-team-spaces：成员制/邀请码/审计三表）

三张表落**元数据库**（与 billing/console_onboardings 同链）：

- ``customer_members``：被邀成员（owner = billing_customers.logto_sub 本人，
  不落本表）。``logto_sub`` **全局唯一**（一人至多一队）；与任何
  ``Customer.logto_sub`` 的互斥由应用层校验（跨表约束管不到，兑换入口
  resolve_member_role 的 owner 优先解析已覆盖该面）。
- ``customer_invites``：一次性邀请码（``wsi-``+24hex，24h 过期，兑换置
  used_by_sub/used_at）。
- ``team_events``：append-only 审计（只 INSERT；谁/何时/何事/对象）。

链位说明：单一迁移链线性追加（down_revision=0026 当前链头，照 0026
entities 的挂法）；双 PG 由既有 ``upgrade head`` 机制覆盖，两库
alembic_version 同收敛到 0027。

Revision ID: 0027_team_spaces
Revises: 0026_entity_layer
Create Date: 2026-10-12

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_team_spaces"
down_revision: Union[str, None] = "0026_entity_layer"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "customer_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("logto_sub", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invited_by_sub", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_customer_members_customer_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_members")),
        sa.UniqueConstraint("logto_sub", name=op.f("uq_customer_members_logto_sub")),
    )
    # 按客户列成员（GET /api/console/team）
    op.create_index(op.f("ix_customer_members_customer_id"), "customer_members", ["customer_id"])
    op.create_table(
        "customer_invites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_by_sub", sa.String(length=64), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_sub", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_customer_invites_customer_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_invites")),
        sa.UniqueConstraint("code", name=op.f("uq_customer_invites_code")),
    )
    # 按客户列待用/已用邀请 + 兑换路径按码点查（code 唯一约束已覆盖，
    # 此索引服务列表查询）
    op.create_index(op.f("ix_customer_invites_customer_id"), "customer_invites", ["customer_id"])
    op.create_table(
        "team_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("actor_sub", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_team_events_customer_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team_events")),
    )
    # 按客户拉审计时间线（append-only）
    op.create_index(op.f("ix_team_events_customer_id"), "team_events", ["customer_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_team_events_customer_id"), table_name="team_events")
    op.drop_table("team_events")
    op.drop_index(op.f("ix_customer_invites_customer_id"), table_name="customer_invites")
    op.drop_table("customer_invites")
    op.drop_index(op.f("ix_customer_members_customer_id"), table_name="customer_members")
    op.drop_table("customer_members")
