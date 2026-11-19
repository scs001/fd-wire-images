"""contact 切片（商留通道表单化 add-contact-intake）的数据库基础设施。

库归属照 admin 先例：``contact_messages`` 落**平台库**（元数据库收敛开关
优先），与 admin/billing 同库——admin contact-messages 面经本包会话直查。
生产走 alembic 0051；``init_db`` 的 create_all 只是开发兜底
（``FD_WIRE_CONTACT_AUTO_CREATE_TABLES=0`` 关闭）。

连接串优先级：``FD_WIRE_DATABASE_URL``（元数据库收敛开关，
wire-dedicated-postgres）> ``FD_WIRE_CONTACT_DATABASE_URL`` > ``DATABASE_URL``
（平台库，与 alembic env 同源）> 本地开发默认 sqlite 文件。PG 历史形态
（``postgres://`` 等）统一改写为 ``postgresql+psycopg://``。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..config import canonical_metadata_database_url, normalize_database_url
from ..db_policy import boot_auto_create_enabled, verify_schema

_DEFAULT_PLATFORM_DB = "sqlite:///./fd_wire_dev.db"


def contact_database_url() -> str:
    """contact 库连接串（contact_messages 落平台库，与 admin 同源同库）。"""
    return normalize_database_url(
        canonical_metadata_database_url()
        or os.getenv("FD_WIRE_CONTACT_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or _DEFAULT_PLATFORM_DB
    )


def _engine_kwargs(url: str) -> dict:
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return kwargs


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """contact 引擎（env 链按调用时解析——测试可经 reset 后重指）。"""
    url = contact_database_url()
    return create_engine(url, **_engine_kwargs(url))


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
    )


class ContactBase(DeclarativeBase):
    """contact 包自有表（contact_messages）的声明基类（生产经 alembic 0051）。"""


_init_lock = threading.Lock()
_initialized = False


def _auto_create_enabled() -> bool:
    """逐模块开关优先于启动总开关；见 db_policy 的三级解析。"""
    return boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")


def init_db() -> None:
    """幂等建 contact_messages（开发兜底；生产走 alembic 0051）。"""
    global _initialized
    if _initialized:
        return
    if not _auto_create_enabled():
        from . import models  # noqa: F401 —— 注册映射后再校验
        verify_schema("contact", ContactBase.metadata.tables, get_engine())
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        ContactBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_contact_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/建表标志）——测试切换库用。"""
    global _initialized
    _initialized = False
    get_engine.cache_clear()
    _session_factory.cache_clear()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个 Session（提交在 service 内显式完成）。"""
    init_db()
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()
