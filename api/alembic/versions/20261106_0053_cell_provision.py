"""cell 开通自助化两表（add-self-serve-cell-provision 任务 1.1/2.1）。

落元数据库（与 demands/contact/bench_pull 同一条 alembic 链）。两表=
客户 cell 入口注册表 / 配给任务队列：

- ``customer_cell_endpoints``：自助配给链回写的客户 cell 入口（AI 侧
  ``resolve_cell_endpoint`` 的 DB 兜底源，env map 之后的第二级）——
  ``customer_id`` 唯一（幂等 upsert；解析键=``billing_customers.name``），
  ``endpoint_url`` 为 registry 入口形态（容器直连地址会让 AI 探索绕过
  计量门——红线见 ai/executor 模块头注）。
- ``cell_provision_requests``：配给任务队列（cheap1 cell-manager runner
  经内部端点领任务/回写）——``customer_id`` 幂等唯一（升档重触发不重复
  建行；failed 复位=重入队）；``steps`` 为 JSON 文本（runner 执行步骤
  留痕，失败时承载 step 痕供运营排查）；``state`` 索引承载队头扫描。

Revision ID: 0053_cell_provision
Revises: 0052_bench_ontology_snapshots
Create Date: 2026-11-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0053_cell_provision"
down_revision: Union[str, None] = "0052_bench_ontology_snapshots"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def _has_table(name: str) -> bool:
    """create_all 兜底可能已抢建过表——存在即跳过（可重跑）。"""
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("customer_cell_endpoints"):
        op.create_table(
            "customer_cell_endpoints",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("customer_id", sa.String(length=120), nullable=False),
            sa.Column("endpoint_url", sa.Text(), nullable=False),
            sa.Column(
                "state",
                sa.String(length=16),
                server_default=sa.text("'done'"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_cell_endpoints")),
            sa.UniqueConstraint(
                "customer_id", name="uq_customer_cell_endpoints_customer_id"
            ),
        )

    if not _has_table("cell_provision_requests"):
        op.create_table(
            "cell_provision_requests",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("customer_id", sa.String(length=120), nullable=False),
            sa.Column(
                "state",
                sa.String(length=16),
                server_default=sa.text("'queued'"),
                nullable=False,
            ),
            sa.Column("steps", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_cell_provision_requests")),
            sa.UniqueConstraint(
                "customer_id", name="uq_cell_provision_requests_customer_id"
            ),
        )
        op.create_index(
            "ix_cell_provision_requests_state",
            "cell_provision_requests",
            ["state"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_cell_provision_requests_state", table_name="cell_provision_requests"
    )
    op.drop_table("cell_provision_requests")
    op.drop_table("customer_cell_endpoints")
