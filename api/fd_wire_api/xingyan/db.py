"""xingyan 切片数据库基础设施（快照三表，落元数据库）。

库归属照 catalog/packs 先例：``xingyan_items`` / ``xingyan_claims`` /
``xingyan_sync_state`` 落**元数据库**——连接串解析
``FD_WIRE_DATABASE_URL`` 收敛开关优先。生产走 alembic 0046；``init_db``
的 create_all 只是开发兜底。

写保护是**结构性的**而非运行时 guard：会话工厂只在 ``sync`` 内部使用，
路由层只拿 ``readonly_session``（只读引擎 + 写动词拦截）。这样查询面
即使在将来被改动，也不可能写出快照表。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .settings import get_settings


class XingyanBase(DeclarativeBase):
    """xingyan 切片自有表（快照三表）的声明基类（生产经 alembic 0046）。"""


def _engine_kwargs(url: str) -> dict:
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return kwargs


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """读引擎：按 env 链创建（同步管道也用它，写权限由语句类型决定）。"""
    return create_engine(get_settings().effective_database_url, **_engine_kwargs(
        get_settings().effective_database_url
    ))


# 只读引擎单列一条：供请求路径使用，连接建立时请求只读语义并拦截写动词
# （与 catalog 切片同款三防中的「引擎层」一条）。
@lru_cache(maxsize=1)
def get_read_engine() -> Engine:
    url = get_settings().effective_database_url
    engine = create_engine(url, **_engine_kwargs(url))
    event.listen(engine, "before_cursor_execute", _guard_write)
    return engine


def _guard_write(conn, cursor, statement: str, parameters, context, executemany) -> None:
    head = statement.lstrip("( \n\r\t[").split(None, 1)[0].upper() if statement.strip() else ""
    if head in _WRITE_VERBS:
        raise XingyanWriteBlocked(
            f"read-only xingyan session attempted a write statement: {head}"
        )


_WRITE_VERBS = frozenset(
    {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "TRUNCATE", "CREATE", "DROP", "ALTER"}
)


class XingyanWriteBlocked(Exception):
    """查询侧试图写星研快照表时抛出。"""


@lru_cache(maxsize=1)
def _read_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_read_engine(), autoflush=False, expire_on_commit=False, future=True
    )


@lru_cache(maxsize=1)
def _write_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Session:
    """读写会话（仅摄取管道内部用）。"""
    return _write_factory()()


def readonly_session() -> Iterator[Session]:
    """FastAPI 依赖：请求级只读会话（查询面唯一入口）。"""
    init_db()
    session = _read_factory()()
    try:
        yield session
    finally:
        session.close()


@contextlib.contextmanager
def readonly_scope() -> Iterator[Session]:
    """脚本/测试用的只读会话上下文（与依赖同一工厂，语义一致）。"""
    init_db()
    session = _read_factory()()
    try:
        yield session
    finally:
        session.close()


_init_lock = threading.Lock()
_initialized = False


def init_db() -> None:
    """幂等建快照三表（开发兜底；生产走 alembic 0046）。"""
    global _initialized
    if _initialized or not get_settings().auto_create_tables:
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        XingyanBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_xingyan_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/建表标志/配置单例）——测试切换库用。"""
    global _initialized
    _initialized = False
    get_engine.cache_clear()
    get_read_engine.cache_clear()
    _read_factory.cache_clear()
    _write_factory.cache_clear()
    get_settings.cache_clear()
