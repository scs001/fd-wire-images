"""entities 切片的数据库基础设施（实体语义层，wire-entity-layer）。

库归属：``entities`` / ``entity_relationships`` 两表落**元数据库**
（生产 PG 的 ``wire_platform``）——连接串解析照 packs 模块
（``canonical_metadata_database_url()`` 即 ``FD_WIRE_DATABASE_URL`` 收敛
开关优先，压过 ``FD_WIRE_ENTITIES_DATABASE_URL`` / ``DATABASE_URL`` /
本地开发 SQLite 默认）。生产走 alembic 0026；``init_db`` 的 create_all
只是开发兜底。

``dataset_id`` 是跨库弱引用（datasets 表在 wire_datasets 库），不设外键
——属主与列校验在 service 层经 datasets 库会话显式完成（照
refresh_schedules 的 target_id 先例）；级联删除走 service 显式删，
不依赖 DB CASCADE。
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

_DEFAULT_ENTITIES_DB = "sqlite:///./fd_wire_entities.db"


def entities_database_url() -> str:
    """实体库连接串（env 可重指；测试指向临时/内存库）。"""
    return normalize_database_url(
        canonical_metadata_database_url()
        or os.getenv("FD_WIRE_ENTITIES_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or _DEFAULT_ENTITIES_DB
    )


class EntitiesBase(DeclarativeBase):
    """entities 包自有表（entities / entity_relationships）的声明基类
    （生产经 alembic 0026）。"""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = entities_database_url()
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
    return os.getenv("FD_WIRE_ENTITIES_AUTO_CREATE_TABLES", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def init_db() -> None:
    """幂等建实体两表（开发兜底；生产走 alembic 0026）。"""
    global _initialized
    if _initialized or not _auto_create_enabled():
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        EntitiesBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_entities_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/建表标志）——测试切换库用。"""
    get_engine.cache_clear()
    _session_factory.cache_clear()
    global _initialized
    _initialized = False
