"""artifact version source_ref（wire-content-fleet D6）：产物生产来源指针。

舰队岗位经产物构建 MCP 创建的产物，需要在版本行上留一条可审计的来源指针：
哪个部署、哪个岗位、哪个回合产出的。用途是把「agent 写的」与「人写的」
「控制台 AI 写的」在事后区分开——这是内容自迭代闭环的审计底座。

为什么**新增可空列**而不复用 ``operator``：``operator`` 是 console 身份
语义（``console:<customer_id>`` / ``migration:<id>``），混入部署/岗位/回合
三元组会让既有审计读错来源。两者语义正交，必须分开。

为什么在 ``artifact_versions`` 而非 ``artifacts``：来源随**版本**变化——
同一产物可能 v1 由人建、v2 由舰队补。放在版本行才是 append-only 的正确粒度。

列可空：存量行、人工创建、控制台 AI 创建一律为空，无需回填；回滚直接删列。

Revision ID: 0040_artifact_source_ref
Revises: 0039_ai_action_gate
Create Date: 2026-10-23

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040_artifact_source_ref"
down_revision: Union[str, None] = "0039_ai_action_gate"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    with op.batch_alter_table("artifact_versions") as batch:
        batch.add_column(sa.Column("source_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifact_versions") as batch:
        batch.drop_column("source_ref")
