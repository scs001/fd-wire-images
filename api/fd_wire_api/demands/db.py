"""demands 切片的数据库基础设施。

Alembic 迁移链在 ``api/alembic/``（0001 计量表建链、0002 本切片的
``demands`` 表）；``create_all`` 作为开发兜底，生产经
``DEMANDS_AUTO_CREATE_TABLES=0`` 关闭后完全走迁移。

连接串可经环境变量 ``FD_WIRE_DATABASE_URL`` 重指（元数据库收敛开关，
wire-dedicated-postgres；默认本地 SQLite 文件——PG URL 历史形态在此
规范化为 ``postgresql+psycopg://``）。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..config import normalize_database_url

DATABASE_URL = normalize_database_url(
    os.getenv("FD_WIRE_DATABASE_URL", "sqlite:///./fd_wire_demands.db")
)



def _build_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # FastAPI 多线程访问 SQLite 需关掉同线程校验；
        # 内存库（测试）需 StaticPool 保证同一连接共享。
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


class Base(DeclarativeBase):
    """demands（及后续 wire 业务表）的声明基类。"""


engine: Engine = _build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

_init_lock = threading.Lock()
_initialized = False


def _auto_create_enabled() -> bool:
    """与 metering 的 METERING_AUTO_CREATE_TABLES 同款开关：生产走 Alembic。"""
    return os.getenv("DEMANDS_AUTO_CREATE_TABLES", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def init_db() -> None:
    """幂等建表（迁移链落地后的开发兜底；可用 DEMANDS_AUTO_CREATE_TABLES=0 关闭）。"""
    global _initialized
    if _initialized or not _auto_create_enabled():
        return
    with _init_lock:
        if _initialized:
            return
        Base.metadata.create_all(bind=engine)
        _initialized = True


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个 Session。"""
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
