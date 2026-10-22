"""物化切片数据库基础设施（平台库 + 跨域只读联查会话）。

库归属约定（与 admin 切片同款，生产对齐 alembic 链）：
- 物化自有表（v1 仅 ``materialized_artifacts``）落平台库，迁移走
  alembic 0009；``init_db`` 的 create_all 只是开发兜底（生产可用
  ``MATERIALIZATION_AUTO_CREATE_TABLES=0`` 关闭）。
- 物化会话是**跨域联查会话**：硬停检查（quota.py）只读 billing
  （billing_customers / billing_plans）与 metering（metering_records）
  的表——生产上同库（alembic 0001/0004），本包只做 SELECT，不改它们
  各自模块的内部逻辑。
- 产物草稿（ai_draft_artifacts）归 ai 切片的引擎与会话纪律
  （``fd_wire_api.ai.db.get_session``），promote 时经依赖注入的 loader
  读取，不经本会话。

连接串优先级：``MATERIALIZATION_DATABASE_URL`` > ``DATABASE_URL``（平台库）
> 本地开发默认 sqlite 文件。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .settings import get_settings


def utc_now_naive() -> datetime:
    """统一存 naive UTC（与 ai/metering 切片同口径；cron 也在 naive UTC 上评估）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MatBase(DeclarativeBase):
    """物化切片全部 ORM 模型的声明基类。"""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_settings().effective_database_url
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
    """新建会话（调度器与测试用）；请求路径用 ``get_db`` 依赖。"""
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
    return get_settings().materialization_auto_create_tables


def init_db() -> None:
    """幂等建物化自有表（开发兜底；生产走 alembic 0009）。"""
    global _initialized
    if _initialized or not _auto_create_enabled():
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        MatBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_materialization_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/配置/建表标记）——测试切换库用。"""
    global _initialized
    _initialized = False
    get_engine.cache_clear()
    _session_factory.cache_clear()
    get_settings.cache_clear()
