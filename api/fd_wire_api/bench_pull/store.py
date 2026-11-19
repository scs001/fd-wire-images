"""知验台本体快照落库（store）+ 本切片数据库基础设施。

库归属照 contact 先例：``bench_ontology_snapshots`` / ``bench_pull_log``
落**元数据库**（连接串解析优先级见 ``bench_pull_database_url``），生产走
alembic 0052；``init_db`` 的 create_all 只是开发兜底
（``FD_WIRE_BENCH_PULL_AUTO_CREATE_TABLES=0`` 关闭）。

写保护是**结构性**的（xingyan 同款纪律）：快照表 append-only，无
UPDATE/DELETE 路径；幂等靠 ``UNIQUE (domain_key, digest)`` + ON CONFLICT
DO NOTHING（同 digest 重复拉取返回既有行，spec 场景「重复拉取幂等」）。
写入口唯一是本模块（路由与调度器都经 ``pull_and_store``）。

store 函数沿用 demands/service.py 惯例：第一参 ``conn``（SQLAlchemy
Session），事务由函数内显式 commit 收口。
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from functools import lru_cache

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, Index, Integer, Text, UniqueConstraint, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from ..config import canonical_metadata_database_url, normalize_database_url
from ..db_policy import boot_auto_create_enabled, verify_schema

_DEFAULT_PLATFORM_DB = "sqlite:///./fd_wire_dev.db"

#: payload 双方言类型：SQLite 用 JSON、PG 用 JSONB（0020/0047 同款）。
_PAYLOAD_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
#: id 双方言：SQLite INTEGER（rowid 别名要求类型名恰为 INTEGER）、PG BIGINT。
_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


def bench_pull_database_url() -> str:
    """bench 快照两表所在库连接串（落元数据库，与 demands/contact 同源）。"""
    return normalize_database_url(
        canonical_metadata_database_url()
        or os.getenv("FD_WIRE_BENCH_PULL_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or _DEFAULT_PLATFORM_DB
    )


def _engine_kwargs(url: str) -> dict:
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return kwargs


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """bench 切片引擎（env 链按调用时解析——测试可经 reset 后重指）。"""
    url = bench_pull_database_url()
    return sa.create_engine(url, **_engine_kwargs(url))


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
    )


class BenchPullBase(DeclarativeBase):
    """bench_pull 包自有表（快照 + 拉取日志）的声明基类（生产经 alembic 0052）。"""


def _utcnow() -> datetime:
    """tz-aware UTC 当前时间（pulled_at 与 TIMESTAMPTZ 列语义一致）。"""
    return datetime.now(timezone.utc)


class BenchOntologySnapshot(BenchPullBase):
    """一份知验台发布快照（键=(domain_key, digest)，内容整包冻结）。"""

    __tablename__ = "bench_ontology_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "domain_key", "digest", name="uq_bench_ontology_snapshots_domain_digest"
        ),
        Index("ix_bench_ontology_snapshots_domain_key", "domain_key"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    #: 域键（law-bench workspace domain_key）
    domain_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: 发布版本（单调递增；随快照冻结）
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 发布 digest（对端 canonical manifest sha256；幂等键一半）
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    #: 载荷整包（8 顶层键；历史版本冻结不可变——无 UPDATE 路径）
    payload: Mapped[dict] = mapped_column(_PAYLOAD_TYPE, nullable=False)
    pulled_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"<BenchOntologySnapshot {self.domain_key} "
            f"v{self.version} {str(self.digest)[:12]}>"
        )


class BenchPullLog(BenchPullBase):
    """一次拉取尝试的留痕（成败都记；失败如实不误标——spec 红线）。"""

    __tablename__ = "bench_pull_log"
    __table_args__ = (
        Index("ix_bench_pull_log_domain_pulled", "domain_key", "pulled_at"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    domain_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: 成功时=落库版本；失败时=请求版本（未知则 NULL）
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 成功时=落库 digest；失败时 NULL
    digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: 失败摘要（已脱敏——token 永不落库）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pulled_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<BenchPullLog {self.domain_key} ok={self.ok}>"


# ---------------------------------------------------------------- 建表兜底

_init_lock = threading.Lock()
_initialized = False


def _auto_create_enabled() -> bool:
    """逐模块开关优先于启动总开关；见 db_policy 的三级解析。"""
    return boot_auto_create_enabled("bench_pull", legacy_env="FD_WIRE_BENCH_PULL_AUTO_CREATE_TABLES")


def init_db() -> None:
    """幂等建快照两表（开发兜底；生产走 alembic 0052）。"""
    global _initialized
    if _initialized:
        return
    if not _auto_create_enabled():
        verify_schema("bench_pull", BenchPullBase.metadata.tables, get_engine())
        return
    with _init_lock:
        if _initialized:
            return
        BenchPullBase.metadata.create_all(bind=get_engine())
        _initialized = True


def reset_bench_pull_runtime() -> None:
    """清空惰性缓存（引擎/会话工厂/建表标志）——测试切换库用。"""
    global _initialized
    _initialized = False
    get_engine.cache_clear()
    _session_factory.cache_clear()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个 Session（写事务在 store 函数内显式 commit）。"""
    init_db()
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------- 存取函数


def _row_dict(row: BenchOntologySnapshot) -> dict:
    """ORM 行 → 纯 dict（payload 已是反序列化后的 JSON 树）。"""
    return {
        "id": row.id,
        "domain_key": row.domain_key,
        "version": row.version,
        "digest": row.digest,
        "payload": row.payload,
        "pulled_at": row.pulled_at,
    }


def save_snapshot(
    conn: Session,
    *,
    domain_key: str,
    version: int,
    digest: str,
    payload: dict,
) -> dict:
    """幂等落一份快照：ON CONFLICT (domain_key, digest) DO NOTHING 后
    SELECT 既有行返回（存在即返回旧行，spec「同 digest 重复拉取零新行」）。

    返回纯 dict（id/domain_key/version/digest/payload/pulled_at）。
    """
    dialect = conn.bind.dialect.name if conn.bind is not None else ""
    values = dict(
        domain_key=domain_key, version=version, digest=digest, payload=payload
    )
    if dialect == "postgresql":
        stmt = (
            postgresql.insert(BenchOntologySnapshot)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["domain_key", "digest"])
        )
    elif dialect == "sqlite":
        stmt = (
            sqlite.insert(BenchOntologySnapshot)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["domain_key", "digest"])
        )
    else:  # pragma: no cover - 其余方言走通用 INSERT（无冲突语义）
        stmt = insert(BenchOntologySnapshot).values(**values)
    conn.execute(stmt)
    conn.commit()
    row = conn.scalars(
        select(BenchOntologySnapshot)
        .where(
            BenchOntologySnapshot.domain_key == domain_key,
            BenchOntologySnapshot.digest == digest,
        )
        .order_by(BenchOntologySnapshot.id.desc())
    ).first()
    if row is None:  # pragma: no cover - 防御：插入后必可查回
        raise RuntimeError(f"snapshot insert succeeded but row missing: {domain_key}/{digest}")
    return _row_dict(row)


def log_pull(
    conn: Session,
    *,
    domain_key: str,
    version: int | None = None,
    digest: str | None = None,
    ok: bool,
    error: str | None = None,
) -> None:
    """追加一条拉取日志（append-only；失败如实留痕，绝不误标成功）。"""
    conn.add(
        BenchPullLog(
            domain_key=domain_key,
            version=version,
            digest=digest,
            ok=ok,
            error=error,
        )
    )
    conn.commit()


def list_snapshots(conn: Session, *, domain_key: str) -> list[dict]:
    """某域全部快照（version 倒序；平局按 id 倒序保持确定性）。"""
    rows = conn.scalars(
        select(BenchOntologySnapshot)
        .where(BenchOntologySnapshot.domain_key == domain_key)
        .order_by(
            BenchOntologySnapshot.version.desc(),
            BenchOntologySnapshot.id.desc(),
        )
    ).all()
    return [_row_dict(row) for row in rows]


def get_latest_snapshot(conn: Session, *, domain_key: str) -> dict | None:
    """某域最新版本快照；从未拉取过返回 None（空域如实）。"""
    rows = list_snapshots(conn, domain_key=domain_key)
    return rows[0] if rows else None


def get_snapshot(conn: Session, *, domain_key: str, version: int) -> dict | None:
    """某域指定版本快照；不存在返回 None。"""
    row = conn.scalars(
        select(BenchOntologySnapshot)
        .where(
            BenchOntologySnapshot.domain_key == domain_key,
            BenchOntologySnapshot.version == version,
        )
        .order_by(BenchOntologySnapshot.id.desc())
    ).first()
    return _row_dict(row) if row is not None else None


# ---------------------------------------------------------------- 拉取编排
# 写入口唯一收口：拉取 → 落库 → 记日志（成功 ok=True；任何失败 ok=False
# 摘要脱敏后原样再抛）。路由与调度器都走这里，纪律不漂移。


def _scrub_token(message: str) -> str:
    """env 里有 token 而消息里意外带出时替换为 ``***``（纵深防御）。

    与 client._redact 同纪律：只对 ≥8 字符的真令牌生效（短占位串子串替换
    只会误伤正常消息；token 本就绝不进异常文本——头部传递）。
    """
    token = os.getenv("BENCH_CONSUMER_TOKEN", "")
    if token and len(token) >= 8 and token in message:
        return message.replace(token, "***")
    return message


def pull_and_store(
    conn: Session, *, domain_key: str, version: int | None = None
) -> dict:
    """拉一次 → 幂等落库 → 记成功日志；任何失败先记失败日志再原样抛出。

    失败日志的 ``error`` 取 ``str(exc)`` 截断并脱敏（client 的异常消息本就
    不含 token，这里再兜一道）。抛出的异常由调用方映射（路由 → HTTP 状态，
    调度器 → 记日志后继续下一域）。
    """
    from .client import pull_ontology  # 局部导入：client 不依赖本模块，避免环

    try:
        payload = pull_ontology(domain_key=domain_key, version=version)
        snapshot = save_snapshot(
            conn,
            domain_key=domain_key,
            version=int(payload["publication_version"]),
            digest=str(payload["digest"]),
            payload=payload,
        )
    except Exception as exc:
        with contextlib.suppress(Exception):  # 留痕失败不掩盖原始异常
            log_pull(
                conn,
                domain_key=domain_key,
                version=version,
                ok=False,
                error=_scrub_token(str(exc))[:2000],
            )
        raise
    log_pull(
        conn, domain_key=domain_key, version=snapshot["version"], digest=snapshot["digest"], ok=True
    )
    return snapshot


__all__ = [
    "BenchOntologySnapshot",
    "BenchPullBase",
    "BenchPullLog",
    "bench_pull_database_url",
    "get_db",
    "get_engine",
    "get_latest_snapshot",
    "get_snapshot",
    "init_db",
    "list_snapshots",
    "log_pull",
    "pull_and_store",
    "reset_bench_pull_runtime",
    "save_snapshot",
]
