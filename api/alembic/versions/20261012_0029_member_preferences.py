"""member preferences（wire-personal-preferences：个人偏好一人一行）

``member_preferences`` 落**元数据库**（与 billing/console/team 表同链，
挂 ConsoleBase）：每 Logto sub 一行的个人 UI 指针——

- ``sub``：Logto sub 字符串，**主键**（一人一行，owner 与团队成员通用）；
- ``ui_locale``：zh|en|null（应用层校验，DB 不设 CHECK——值域演进免迁移）；
- ``landing_tab``：控制台九页签|null（同上应用层校验）；
- ``pinned_dashboard_id``：置顶看板产物 id，**裸整型不建外键**——产物表
  ``artifacts`` 挂 AiBase（ai 库），跨 Base 不声明 ForeignKey（同
  CustomerMember 对 billing_customers 的纪律，且产物可归档/删除，死链由
  GET 读时自愈清列兜底，DB 约束反而会把归档变成写失败）；
- ``updated_at``：每次写刷新（服务层显式赋值）。

偏好是 per-sub 个人 UI 指针而非客户业务数据：不经溯源账本、不进
team_events。链位说明：单一迁移链线性追加（down_revision=0028 当前
链头，照 0028 alert_evaluation 的挂法）；双 PG 由既有 ``upgrade head``
机制覆盖，两库 alembic_version 同收敛到 0029。

Revision ID: 0029_member_preferences
Revises: 0028_alert_evaluation
Create Date: 2026-10-12

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_member_preferences"
down_revision: Union[str, None] = "0028_alert_evaluation"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "member_preferences",
        sa.Column("sub", sa.String(length=64), nullable=False),
        sa.Column("ui_locale", sa.String(length=16), nullable=True),
        sa.Column("landing_tab", sa.String(length=16), nullable=True),
        sa.Column("pinned_dashboard_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sub", name=op.f("pk_member_preferences")),
    )


def downgrade() -> None:
    op.drop_table("member_preferences")
