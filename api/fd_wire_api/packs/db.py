"""packs 切片的数据库基础设施（订阅表，落元数据库）。

库归属：``pack_subscriptions`` 落**元数据库**——连接串解析照 billing 模块
（``canonical_metadata_database_url()`` 即 ``FD_WIRE_DATABASE_URL`` 收敛开关
优先，压过 ``FD_WIRE_PACKS_DATABASE_URL`` / ``DATABASE_URL`` / 本地开发
SQLite 默认）。生产走 alembic 0022；``init_db`` 的 create_all 只是开发兜底。

包的指标清单/计数不经本引擎——那是对目录镜像（catalog.db.readonly_session）
的只读查询，见 service.py。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import canonical_metadata_database_url, normalize_database_url

_DEFAULT_PACKS_DB = "sqlite:///./fd_wire_packs.db"


def packs_database_url() -> str:
    """订阅库连接串（env 可重指；测试指向临时文件库）。"""
    return normalize_database_url(
        canonical_metadata_database_url()
        or os.getenv("FD_WIRE_PACKS_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or _DEFAULT_PACKS_DB
    )


class PacksBase(DeclarativeBase):
    """packs 包自有表（pack_subscriptions）的声明基类（生产经 alembic 0022）。"""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = packs_database_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, future=True, **kwargs)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
    )


def get_session() -> Session:
    """显式会话（建表与测试种子用）；请求路径用 ``get_db``。"""
    return _session_factory()()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话，成功提交、异常回滚。"""
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
    return os.getenv("FD_WIRE_PACKS_AUTO_CREATE_TABLES", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def init_db() -> None:
    """幂等建订阅表（开发兜底；生产走 alembic 0022）。"""
    global _initialized
    if _initialized or not _auto_create_enabled():
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        PacksBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_packs_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/建表标志）——测试切换库用。"""
    get_engine.cache_clear()
    _session_factory.cache_clear()
    global _initialized
    _initialized = False
