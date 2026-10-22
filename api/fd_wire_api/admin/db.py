"""admin 切片（内部后台）的数据库基础设施。

库归属约定（与生产 alembic 链一致，见 api/alembic/versions/0007）：
- admin 自己的表（v1 仅 ``catalog_review_notes``）落平台库，迁移走
  alembic 0007；``init_db`` 的 create_all 只是开发兜底（生产可用
  ``FD_WIRE_ADMIN_AUTO_CREATE_TABLES=0`` 关闭）。
- admin 会话是**跨域联查会话**：billing（billing_customers /
  billing_plans / billing_ledger_entries）与 metering（metering_records）
  的表生产上同库（alembic 0001/0004），本包直接复用其 ORM 模型做
  只读查询与开户记录，不改它们各自的模块内部逻辑。
- 需求单（demands）与目录镜像（catalog）各有独立引擎与会话纪律
  （镜像只读 guard），admin 侧分别复用 ``demands.db.get_db`` 与
  ``catalog.db.readonly_session``，不经本会话。

连接串优先级：``FD_WIRE_DATABASE_URL``（元数据库收敛开关，
wire-dedicated-postgres）> ``FD_WIRE_ADMIN_DATABASE_URL`` > ``DATABASE_URL``
（平台库，与 alembic env 同源）> 本地开发默认 sqlite 文件。PG 历史形态
（``postgres://`` 等）统一改写为 ``postgresql+psycopg://``。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import canonical_metadata_database_url, normalize_database_url

_DEFAULT_PLATFORM_DB = "sqlite:///./fd_wire_dev.db"


def admin_database_url() -> str:
    """admin 联查库连接串（env 可重指；测试指向临时文件库）。"""
    return normalize_database_url(
        canonical_metadata_database_url()
        or os.getenv("FD_WIRE_ADMIN_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or _DEFAULT_PLATFORM_DB
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = admin_database_url()
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
    """显式会话（初始化建表与测试种子用）；请求路径用 ``get_db``。"""
    return _session_factory()()


class AdminBase(DeclarativeBase):
    """admin 包自有表的声明基类（生产经 alembic 0007 落平台库）。"""


_init_lock = threading.Lock()
_initialized = False


def _auto_create_enabled() -> bool:
    return os.getenv("FD_WIRE_ADMIN_AUTO_CREATE_TABLES", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def init_db() -> None:
    """幂等建 admin 自有表（开发兜底；生产走 alembic 0007）。"""
    global _initialized
    if _initialized or not _auto_create_enabled():
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        AdminBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_admin_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/建表标志）——测试切换库用。"""
    get_engine.cache_clear()
    _session_factory.cache_clear()
    global _initialized
    _initialized = False


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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
