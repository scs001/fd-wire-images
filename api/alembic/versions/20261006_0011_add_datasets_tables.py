"""add customer dataset tables（wire-customer-local-data 任务组 2，契约 §3.2）

本切片六表（链 0010 → 0011）：

- ``connectors`` / ``connector_keys`` / ``connector_pairing_tokens``：
  出站连接器身份——配对令牌 15 分钟单次（只存 sha256）、连接器 key
  scope 仅推送 + 拉配置、吊销即时生效。
- ``datasets``：客户数据集登记（1:1 源表/文件；元数据在 api，有效数据
  在 cell 引擎侧，本表绝不存客户源 DSN/账密）。
- ``dataset_syncs``：同步历史（终态 ok|failed|drift|quota；syncing 为
  在飞行标记）。
- ``uploads``：上传原件登记（CSV 原样存 / Excel 转 CSV 暂存）。

Revision ID: 0011_datasets
Revises: 0010_catalog_source_db
Create Date: 2026-10-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_datasets"
down_revision: Union[str, None] = "0010_catalog_source_db"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connectors")),
    )
    op.create_index("ix_connectors_customer_id", "connectors", ["customer_id"])

    op.create_table(
        "connector_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_keys")),
    )
    op.create_index("ix_connector_keys_connector_id", "connector_keys", ["connector_id"])
    op.create_index("ix_connector_keys_key_hash", "connector_keys", ["key_hash"])

    op.create_table(
        "connector_pairing_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_pairing_tokens")),
    )
    op.create_index(
        "ix_connector_pairing_tokens_customer_id",
        "connector_pairing_tokens",
        ["customer_id"],
    )
    op.create_index(
        "ix_connector_pairing_tokens_token_hash",
        "connector_pairing_tokens",
        ["token_hash"],
    )

    op.create_table(
        "datasets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=True),
        sa.Column("source_name", sa.String(length=200), nullable=True),
        sa.Column("source_ref", sa.String(length=512), nullable=True),
        sa.Column(
            "columns_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sync_cron", sa.String(length=64), nullable=True),
        sa.Column("sync_status", sa.String(length=16), nullable=False),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
        sa.UniqueConstraint("customer_id", "key", name="uq_datasets_customer_key"),
    )
    op.create_index("ix_datasets_customer_id", "datasets", ["customer_id"])
    op.create_index("ix_datasets_key", "datasets", ["key"])
    op.create_index("ix_datasets_connector_id", "datasets", ["connector_id"])

    op.create_table(
        "dataset_syncs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_syncs")),
    )
    op.create_index("ix_dataset_syncs_dataset_id", "dataset_syncs", ["dataset_id"])

    op.create_table(
        "uploads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column(
            "columns_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stored_path", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
    )
    op.create_index("ix_uploads_customer_id", "uploads", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_uploads_customer_id", table_name="uploads")
    op.drop_table("uploads")
    op.drop_index("ix_dataset_syncs_dataset_id", table_name="dataset_syncs")
    op.drop_table("dataset_syncs")
    op.drop_index("ix_datasets_connector_id", table_name="datasets")
    op.drop_index("ix_datasets_key", table_name="datasets")
    op.drop_index("ix_datasets_customer_id", table_name="datasets")
    op.drop_table("datasets")
    op.drop_index(
        "ix_connector_pairing_tokens_token_hash",
        table_name="connector_pairing_tokens",
    )
    op.drop_index(
        "ix_connector_pairing_tokens_customer_id",
        table_name="connector_pairing_tokens",
    )
    op.drop_table("connector_pairing_tokens")
    op.drop_index("ix_connector_keys_key_hash", table_name="connector_keys")
    op.drop_index("ix_connector_keys_connector_id", table_name="connector_keys")
    op.drop_table("connector_keys")
    op.drop_index("ix_connectors_customer_id", table_name="connectors")
    op.drop_table("connectors")