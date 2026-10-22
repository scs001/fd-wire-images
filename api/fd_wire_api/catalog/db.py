"""镜像库引擎与「仅同步管道可写」的模型层 guard。

三个层次的写防护：
1. **引擎层**：``get_read_engine()`` 返回的只读引擎在驱动连接建立时即请求
   只读语义（SQLite ``PRAGMA query_only=ON``、Postgres
   ``SET default_transaction_read_only = on``，best-effort），并在
   ``before_cursor_execute`` 钩子里拦截一切写动词语句——包括绕过 ORM
   flush 的 Core 级 ``session.execute(update/delete/insert)``。
2. **ORM 层**：``CatalogSession`` 在 flush 前检查 ``info["mirror_readonly"]``，
   只读 session 上的 ``add/flush`` 直接抛 ``MirrorWriteBlocked``。
3. **模块结构**：仓库内唯一的镜像写入口是 ``sync.IndicatorRegistrySync``
   （读写 session 工厂不导出给查询侧；查询服务只拿得到只读 session）。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .settings import settings


class MirrorWriteBlocked(Exception):
    """试图对指标镜像进行同步管道之外的写入时抛出。"""

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "indicator mirror is read-only: only the sync pipeline "
            "(IndicatorRegistrySync) may write to it"
        )


# 经 SQLAlchemy 执行流可见的写动词；合法的连接初始化语句（如 SELECT、
# BEGIN、SAVEPOINT）不在其中，我们在 connect 事件里用裸 DBAPI 执行的
# 只读设置不会经过这些钩子。
_WRITE_VERBS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
        "MERGE",
        "TRUNCATE",
        "CREATE",
        "DROP",
        "ALTER",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "SET",
        "GRANT",
        "REVOKE",
        "COPY",
        "REINDEX",
        "VACUUM",
    }
)


def _first_verb(statement: str) -> str:
    return statement.strip("( \n\r\t[").split(None, 1)[0].upper() if statement.strip() else ""


def _engine_connect_args(url: str) -> dict[str, Any]:
    if url.startswith("sqlite"):
        # FastAPI 多 worker 线程共享引擎
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}
    return {}


def _make_readonly_driver(dbapi_connection: Any, url: str) -> None:
    """在裸 DBAPI 连接上请求只读语义（不经过 SQLAlchemy 事件流）。"""
    try:
        if url.startswith("sqlite"):
            dbapi_connection.execute("PRAGMA query_only = ON")
        else:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET default_transaction_read_only = on")
            finally:
                cursor.close()
    except Exception:  # pragma: no cover - 驱动不支持时退回语句级 guard
        pass


def _guard_cursor(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
    if _first_verb(statement) in _WRITE_VERBS:
        raise MirrorWriteBlocked(
            f"read-only mirror session attempted a write statement: "
            f"{statement.strip()[:80]!r}"
        )


def _build_engine(url: str, *, readonly: bool) -> Engine:
    engine = create_engine(url, **_engine_connect_args(url))
    if readonly:
        event.listen(engine, "connect", _make_readonly_driver)
        event.listen(engine, "before_cursor_execute", _guard_cursor)
    return engine


class CatalogSession(Session):
    """带 flush 前 guard 的 session（配合 ``info['mirror_readonly']``）。"""


@event.listens_for(CatalogSession, "before_flush")
def _guard_flush(session: CatalogSession, flush_context: Any, instances: Any) -> None:
    if session.info.get("mirror_readonly"):
        raise MirrorWriteBlocked()


_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def _engine_for(role: str, readonly: bool) -> Engine:
    url = settings.effective_database_url
    key = f"{role}:{url}"
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = _build_engine(url, readonly=readonly)
            _engines[key] = engine
        return engine


def get_rw_engine() -> Engine:
    """同步管道专用的读写引擎（唯一写入口所在）。"""
    return _engine_for("rw", readonly=False)


def get_read_engine() -> Engine:
    """查询侧专用只读引擎：驱动只读 + 语句级 guard。"""
    return _engine_for("ro", readonly=True)


def reset_engines() -> None:
    """测试便利：丢弃缓存的引擎（不关闭生产引擎语义）。"""
    with _lock:
        _engines.clear()


def _make_session_factory(engine: Engine, *, readonly: bool) -> sessionmaker:
    return sessionmaker(
        bind=engine,
        class_=CatalogSession,
        expire_on_commit=False,
        autoflush=False,
        info={"mirror_readonly": readonly},
    )


def sync_session() -> Session:
    """同步管道的读写 session（仅供 sync.py 使用）。"""
    return _make_session_factory(get_rw_engine(), readonly=False)()


@contextlib.contextmanager
def readonly_session() -> Iterator[Session]:
    """查询侧只读 session：任何写尝试都会被 guard 拒绝。

    正常退出只 close（不 rollback）——rollback 会清空已加载属性，导致
    调用方拿到 DetachedInstanceError；guard 拦下的写语句本身不会产生
    待提交状态。
    """
    factory = _make_session_factory(get_read_engine(), readonly=True)
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """建表（无 alembic 链：模型 + create_all；幂等）。"""
    Base.metadata.create_all(get_rw_engine())
