"""team spaces v2（wire-team-spaces-v2：多队解锁 + 活跃团队指针）

成员唯一键 v0→v2 + 指针表（元数据库，与 0027 team 三表同链）：

- ``customer_members`` 唯一键 ``uq_customer_members_logto_sub``（一人
  一队）→ ``uq_customer_members_customer_sub (customer_id, logto_sub)``
  （同一 sub 可属多个客户，一队至多一行）；
- ``member_active_team``：per-sub 活跃团队指针（logto_sub 主键，鉴权
  解析域独立成表的理由见 change design.md D1），解析序指针优先→owner
  回退→首队回退（console/auth.py）；
- **回填**：每条存量成员行 INSERT 一条指针（存量单队成员解析结果迁移
  前后逐 sub 一致——指针=现队，零感知）；owner 不落指针（owner 回退
  天然覆盖，存量 owner 亦零感知）。

双方言（design D7）：PG 直接 DROP/ADD CONSTRAINT；SQLite 无 DROP
CONSTRAINT——batch_alter_table 照抄 0027 建表定义（copy_from，唯一键
换成新组合）重建。回填幂等：PG ``ON CONFLICT (logto_sub) DO NOTHING``、
SQLite ``INSERT OR IGNORE``（方言分支——PG 不认 INSERT OR IGNORE）。

downgrade 注意：若回填后一 sub 已入多队，旧全局唯一约束装不回——先按
id 最小保留一条删多余行再换回约束（多队数据不可逆，downgrade 是降级
路径，保留首队=迁移前的解析结果）。

Revision ID: 0033_team_spaces_v2
Revises: 0032_transform_pipelines
Create Date: 2026-10-13

"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_team_spaces_v2"
down_revision: Union[str, None] = "0032_transform_pipelines"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def _customer_members_v0() -> sa.Table:
    """0027 的完整建表定义（照抄 20261012_0027_team_spaces.py，SQLite
    batch 重建的 copy_from 源——SQLite 反射拿不到具名唯一约束，须显式给
    全表形状；索引也要带上，否则重建即丢）。"""
    return sa.Table(
        "customer_members",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("logto_sub", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invited_by_sub", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name="fk_customer_members_customer_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customer_members"),
        sa.UniqueConstraint("logto_sub", name="uq_customer_members_logto_sub"),
        sa.Index("ix_customer_members_customer_id", "customer_id"),
    )


def _customer_members_v2() -> sa.Table:
    """v2 形状（唯一键=customer_id+logto_sub）——downgrade 的 batch 源。"""
    return sa.Table(
        "customer_members",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("logto_sub", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invited_by_sub", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name="fk_customer_members_customer_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customer_members"),
        sa.UniqueConstraint(
            "customer_id", "logto_sub", name="uq_customer_members_customer_sub"
        ),
        sa.Index("ix_customer_members_customer_id", "customer_id"),
    )


def _swap_unique_to_v2() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "uq_customer_members_logto_sub", "customer_members", type_="unique"
        )
        op.create_unique_constraint(
            "uq_customer_members_customer_sub",
            "customer_members",
            ["customer_id", "logto_sub"],
        )
    else:
        # SQLite 无 DROP CONSTRAINT：batch 重建（copy_from=v0 全表形状，
        # 换唯一键后 alembic 自动重建表并回拷行）。
        with op.batch_alter_table(
            "customer_members", copy_from=_customer_members_v0()
        ) as batch:
            batch.drop_constraint("uq_customer_members_logto_sub", type_="unique")
            batch.create_unique_constraint(
                "uq_customer_members_customer_sub", ["customer_id", "logto_sub"]
            )


def _swap_unique_to_v0() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "uq_customer_members_customer_sub", "customer_members", type_="unique"
        )
        op.create_unique_constraint(
            "uq_customer_members_logto_sub", "customer_members", ["logto_sub"]
        )
    else:
        with op.batch_alter_table(
            "customer_members", copy_from=_customer_members_v2()
        ) as batch:
            batch.drop_constraint("uq_customer_members_customer_sub", type_="unique")
            batch.create_unique_constraint("uq_customer_members_logto_sub", ["logto_sub"])


def upgrade() -> None:
    _swap_unique_to_v2()

    op.create_table(
        "member_active_team",
        sa.Column("logto_sub", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("logto_sub", name=op.f("pk_member_active_team")),
    )
    op.create_index(
        op.f("ix_member_active_team_customer_id"),
        "member_active_team",
        ["customer_id"],
    )

    # 回填：每条存量成员行一条指针（v0 全局唯一保证一 sub 至多一行，
    # 天然无主键冲突；ON CONFLICT/OR IGNORE 只是幂等保险——v0 数据下
    # 本就不冲突，防的是异常态下的半回填重放）。
    now = datetime.now(timezone.utc)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "INSERT INTO member_active_team (logto_sub, customer_id, updated_at)"
                " SELECT logto_sub, customer_id, :now FROM customer_members"
                " ON CONFLICT (logto_sub) DO NOTHING"
            ),
            {"now": now},
        )
    else:
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO member_active_team"
                " (logto_sub, customer_id, updated_at)"
                " SELECT logto_sub, customer_id, :now FROM customer_members"
            ),
            {"now": now},
        )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_member_active_team_customer_id"), table_name="member_active_team"
    )
    op.drop_table("member_active_team")

    # 一 sub 多队则旧全局唯一约束装不回：按 id 最小保留一条（=迁移前
    # 的解析队），删 v2 期间入的其余队——多队数据在降级路径不可逆。
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM customer_members WHERE id NOT IN"
            " (SELECT MIN(id) FROM customer_members GROUP BY logto_sub)"
        )
    )
    _swap_unique_to_v0()
