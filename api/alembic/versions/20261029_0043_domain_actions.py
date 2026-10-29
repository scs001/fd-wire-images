"""客户域动作类型与实例（wire-domain-actions 任务 1.1）。

三表挂 AiBase（与动作闸提议/审计同库——域动作是闸的动态注册源，生命周期
与提议管线同域）：

- ``domain_action_types``：动作类型声明（挂实体；name 实体内唯一）。
  ``dataset_id``/``pk_columns_json`` 为**保存时刻快照**（实体绑定数据集与
  主键——声明时校验一致、之后漂移在提议/执行处如实拒绝），效果执行零
  实体库会话依赖（GateSessions 无 entities 会话——快照是刻意的解耦）。
- ``domain_action_type_events``：类型声明/变更/启停留痕（append-only，
  照 asset_meta_events 纪律：只 INSERT 无改删代码路径）。
- ``domain_action_instances``：动作实例台账（append-only 状态机
  pending→awaiting_confirmation→executed/rejected/failed/disabled）；
  ``args_json`` 存**全量表单值**（执行要全文——闸提议表的摘要纪律不适用
  此处；实例是执行的事实源，跨进程/重启存活）。``source_ref`` 为调用方
  原样透传的岗位来源标注（≤256 字符，壹座身份行 fleet=/post=/title=/
  target=，turn= 字段位预留——不解析，paas 评审 R1 对齐）。

另：``ai_action_proposals.conversation_id`` 转 **nullable**——域动作提议
来自实体页/入口工具面（无副驾会话上下文），闸管线复用要求提议行允许
无会话（副驾通道行为不变：有会话照旧落值）。

Revision ID: 0043_domain_actions
Revises: 0042_entity_v2_columns
Create Date: 2026-10-29

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_domain_actions"
down_revision: Union[str, None] = "0042_entity_v2_columns"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "domain_action_types",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("entity_id", sa.Integer(), nullable=False, index=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("form_schema", sa.JSON(), nullable=False),
        sa.Column("effect", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        #: 保存时刻快照（实体绑定数据集 + 主键列）
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("pk_columns_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "customer_id", "entity_id", "name", name="uq_domain_action_types_name"
        ),
    )
    op.create_table(
        "domain_action_type_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("type_id", sa.Integer(), nullable=False, index=True),
        sa.Column("actor_sub", sa.String(length=64), nullable=False),
        sa.Column(
            "at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
    )
    op.create_table(
        "domain_action_instances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("type_id", sa.Integer(), nullable=False, index=True),
        sa.Column("customer_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("proposal_id", sa.Integer(), nullable=True, index=True),
        sa.Column("proposer_sub", sa.String(length=64), nullable=False),
        #: console（实体页/副驾）| mcp（wire_action_propose）
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=256), nullable=True),
        #: 全量表单值（执行事实源；闸提议表只落摘要——两处口径不同）
        sa.Column("args_json", sa.JSON(), nullable=False),
        #: pending→awaiting_confirmation→executed/rejected/failed/disabled
        sa.Column("status", sa.String(length=24), nullable=False, index=True),
        sa.Column("confirmed_by", sa.String(length=64), nullable=True),
        sa.Column("correction_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # 域动作提议无副驾会话——conversation_id 转可空（副驾通道行为不变）
    with op.batch_alter_table("ai_action_proposals") as batch:
        batch.alter_column(
            "conversation_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("ai_action_proposals") as batch:
        batch.alter_column(
            "conversation_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
    op.drop_table("domain_action_instances")
    op.drop_table("domain_action_type_events")
    op.drop_table("domain_action_types")
