"""Alembic 环境：目标元数据来自 fd_wire_api.metering 的 ORM 声明。

连接串优先级：``-x db_url=...`` > ``FD_WIRE_DATABASE_URL``（元数据库收敛
开关，wire-dedicated-postgres）> ``DATABASE_URL`` env > 默认 sqlite 开发库。
PG 历史形态（``postgres://`` 等）统一改写为 ``postgresql+psycopg://``。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from fd_wire_api.config import normalize_database_url
from fd_wire_api.metering.db import MeteringBase
from fd_wire_api.metering import models  # noqa: F401 —— 注册全部映射

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 让离线/在线模式共用同一 url 解析
x_args = context.get_x_argument(as_dictionary=True)
database_url = normalize_database_url(
    x_args.get("db_url")
    or os.environ.get("FD_WIRE_DATABASE_URL")
    or os.environ.get("DATABASE_URL", "sqlite:///./fd_wire_dev.db")
)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = MeteringBase.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 脚本，不连库。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直连目标库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
