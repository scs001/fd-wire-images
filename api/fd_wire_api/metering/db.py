"""计量库引擎与会话：SQLAlchemy 2.x，连接串来自 ``DATABASE_URL``。

引擎惰性创建（``get_engine`` 带 lru_cache），测试通过
``reset_metering_runtime()`` 清缓存后指向临时库。
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import get_settings


class MeteringBase(DeclarativeBase):
    """计量管道全部 ORM 模型的声明基类。"""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """按 effective URL 建引擎（FD_WIRE_DATABASE_URL > DATABASE_URL；sqlite
    默认开启外键与 NATIVE 日期）。"""
    url = get_settings().effective_database_url
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
    return create_engine(url, future=True, **kwargs)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Session:
    """新建会话（测试与后台任务用）；请求路径用 ``get_db`` 依赖。"""
    return _session_factory()()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话，结束即提交/回滚。"""
    db = get_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_schema() -> None:
    """幂等建表（开发便利）；生产迁移走 Alembic（``alembic/``）。"""
    from . import models  # noqa: F401 —— 注册映射后再 create_all

    MeteringBase.metadata.create_all(bind=get_engine())


def reset_metering_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/配置）——测试切换 DATABASE_URL 用。"""
    get_engine.cache_clear()
    _session_factory.cache_clear()
    from .settings import get_settings as _get_settings

    _get_settings.cache_clear()


def utc_now() -> datetime:
    """带 UTC 时区的当前时刻（存储统一为 aware UTC）。"""
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """统一时区：naive 视为 UTC，aware 折算为 UTC（sqlite 字符串比较安全）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
