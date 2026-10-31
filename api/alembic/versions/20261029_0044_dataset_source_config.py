"""REST 源声明式配置（wire-rest-connector 任务 2.1）。

``datasets`` 增 ``source_config_json``：连接器源（``source_kind=connector``）的
**非敏感**请求形状声明（URL/method/分页/投影/水位响应路径/自定义非敏感头），
由 api 登记、随 ``GET /api/v1/connector/config`` 下发给客户内网连接器。

红线（ADR-0004 云端零凭据）：此列 MUST NOT 承载任何认证凭据——登记端点在
写入前拒绝凭据类头名（authorization/cookie/proxy-authorization 等），凭据
只存连接器本地配置文件（0600）。

存量行 NULL（非 rest 源零漂移）；回滚整列删除。

Revision ID: 0044_dataset_source_config
Revises: 0043_domain_actions
Create Date: 2026-10-29

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_dataset_source_config"
down_revision: Union[str, None] = "0043_domain_actions"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspect.get_columns("datasets")}
    if "source_config_json" in existing:
        return
    with op.batch_alter_table("datasets") as batch:
        batch.add_column(sa.Column("source_config_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch:
        batch.drop_column("source_config_json")
