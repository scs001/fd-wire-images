"""artifact drafts（wire-rich-text-docs design D3）：doc 产物草稿层。

doc（富文本文档）产物的自动保存落点——与版本链分离的**工作区**：

- 版本链（``artifact_versions``）是 append-only 的对外契约（分享钉版、
  市场快照、物化引用都依赖它）；自动保存若进版本链，敲字期间会产生几百个
  版本，把历史炸掉、把分享钉版语义搅浑。分离后：草稿层高频**覆盖写**，
  版本链只在显式「保存版本」时追加。
- 草稿层一对一**连产物**（``artifact_id`` 唯一——工作区不需要历史，多行
  历史即版本链的退化复制品）。
- 独立表而非 ``artifacts`` 加列：产物表是全系统最热的表（列表/工作台/市场
  都扫），为只有一个 kind 需要的功能给全局表加列是污染；独立表让热点路径
  零变化。

新表一张（挂 AiBase，与 ai 切片同库）：

- ``artifact_drafts``：``artifact_id``（唯一外键 + ``ON DELETE CASCADE``
  ——产物删除草稿行随删，不留孤儿工作区）、``text``（Markdown 源文本，
  非空，Python 侧缺省 ``""``）、``updated_at``、``updated_by``
  （console:<customer_id> 留痕）。

本表列定义与 0021 的 ``artifacts`` 同款：Python 侧 default 不落
server_default（写入永远经 ORM 带值，列上无需 DB 缺省——0021 先例）。

Revision ID: 0038_artifact_drafts
Revises: 0037_billing_customer_name_uq
Create Date: 2026-10-17

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_artifact_drafts"
down_revision: Union[str, None] = "0037_billing_customer_name_uq"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "artifact_drafts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_artifact_drafts_artifact_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_drafts")),
    )
    # 唯一索引（artifact_id）——一对一工作区语义（同列无需另设普通索引）
    op.create_index(
        op.f("ix_artifact_drafts_artifact_id"),
        "artifact_drafts",
        ["artifact_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_artifact_drafts_artifact_id"), table_name="artifact_drafts"
    )
    op.drop_table("artifact_drafts")
