"""schedules 切片的数据库基础设施（refresh_schedules 表，落元数据库）。

库归属照 packs/billing 先例：``refresh_schedules`` 落**元数据库**——连接串
解析 ``FD_WIRE_DATABASE_URL`` 收敛开关优先，压过
``FD_WIRE_SCHEDULES_DATABASE_URL`` / ``DATABASE_URL`` / 本地开发 SQLite
默认。生产走 alembic 0023；``init_db`` 的 create_all 只是开发兜底。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .settings import get_settings


class SchedulesBase(DeclarativeBase):
    """schedules 切片自有表（refresh_schedules）的声明基类（生产经 alembic 0023）。"""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """按 env 链创建引擎（内存库需 StaticPool 共享连接，照 datasets 先例）。"""
    url = get_settings().effective_database_url
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return create_engine(url, future=True, **kwargs)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
    )


def get_session() -> Session:
    """显式会话（调度循环/服务内部用）；请求路径用 ``get_db`` 依赖。"""
    return _session_factory()()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话，成功提交、异常回滚（照 packs 先例）。"""
    init_db()
    db = get_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


_init_lock = threading.Lock()
_initialized = False


def _auto_create_enabled() -> bool:
    return get_settings().schedules_auto_create_tables


def init_db() -> None:
    """幂等建调度表（开发兜底；生产走 alembic 0023）。"""
    global _initialized
    if _initialized or not _auto_create_enabled():
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        SchedulesBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_schedules_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/建表标志/配置单例）——测试切换库用。"""
    global _initialized
    _initialized = False
    get_engine.cache_clear()
    _session_factory.cache_clear()
    get_settings.cache_clear()
