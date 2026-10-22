"""AI 构建循环切片的数据库基础设施。

Alembic 迁移链在 ``api/alembic/``（0001 计量、0002 目录镜像、0003 需求单、
0004 计费、0005 本切片三表：``ai_conversations`` / ``ai_messages`` /
``ai_draft_artifacts``）；``create_all`` 作为开发兜底，生产经
``AI_AUTO_CREATE_TABLES=0`` 关闭后完全走迁移。

引擎惰性创建（``get_engine`` 带 lru_cache），测试通过
``reset_ai_runtime()`` 清缓存后指向临时库。
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .settings import get_settings


class AiBase(DeclarativeBase):
    """AI 对话构建切片全部 ORM 模型的声明基类。"""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """按 ``AI_DATABASE_URL`` 创建引擎（内存库需 StaticPool 共享连接）。"""
    url = get_settings().ai_database_url
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return create_engine(url, future=True, **kwargs)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
    )


def get_session() -> Session:
    """新建会话（测试与后台任务用）；请求路径用 ``get_db`` 依赖。"""
    return _session_factory()()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个 Session。"""
    init_db()
    db = get_session()
    try:
        yield db
    finally:
        db.close()


_init_lock = threading.Lock()
_initialized = False


def _auto_create_enabled() -> bool:
    return get_settings().ai_auto_create_tables


def init_db() -> None:
    """幂等建表（迁移链落地后的开发兜底；AI_AUTO_CREATE_TABLES=0 关闭）。"""
    global _initialized
    if _initialized or not _auto_create_enabled():
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        AiBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_ai_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/配置/建表标记）——测试切换 env 用。"""
    global _initialized
    _initialized = False
    get_engine.cache_clear()
    _session_factory.cache_clear()
    from .settings import get_settings as _get_settings

    _get_settings.cache_clear()
