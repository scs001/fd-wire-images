"""镜像表模型（wire-indicator-catalog：注册目录为权威镜像）。

- ``IndicatorSnapshot``：权威库指标的每日只读镜像行。字段集即规格规定的
  全部展示字段；不含任何观测数值列——镜像只承载「目录」，不承载数据。
- ``CatalogSyncState``：同步管道自身的游标/水位状态（单行），同样只有
  同步管道可写。

写路径纪律：本模块只声明模型；唯一的 ORM 写入口是
``fd_wire_api.catalog.sync.IndicatorRegistrySync``（同步管道）。查询侧一律
使用 ``fd_wire_api.catalog.db.readonly_session()``（模型层 guard 会拒绝
任何写语句，见 db.py）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """统一的 aware-UTC 时间列。

    SQLite 方言不往返 tzinfo（存 naive、读 naive），统一在这里绑定前归 UTC、
    读回补 UTC——增量水位的 aware/naive 比较才不会 TypeError。Postgres 侧
    落 timestamptz。
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(DateTime())
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    """目录镜像 schema 根。"""


class IndicatorSnapshot(Base):
    """统一指标注册目录的镜像行（权威库主键唯一）。"""

    __tablename__ = "indicator_snapshots"
    __table_args__ = (
        # 权威库主键唯一约束：镜像与权威库一一对应
        UniqueConstraint("indicator_id", name="uq_indicator_snapshots_indicator_id"),
        # 状态只可能是权威库两级：verified | registered
        CheckConstraint(
            "status IN ('verified', 'registered')",
            name="ck_indicator_snapshots_status",
        ),
        Index("ix_indicator_snapshots_domain", "domain"),
        Index("ix_indicator_snapshots_status", "status"),
        Index("ix_indicator_snapshots_code", "code"),
        Index("ix_indicator_snapshots_source_db", "source_db"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 权威库主键（同步 upsert 锚点）＝ source_db:native_code
    indicator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 来源库（indicator_id 的前半边；按源过滤与 stats GROUP BY 用，不解析 ID）
    source_db: Mapped[str | None] = mapped_column(String(128))
    code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    name_zh: Mapped[str | None] = mapped_column(String(512))
    name_en: Mapped[str | None] = mapped_column(String(512))
    description_zh: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(128))
    frequency: Mapped[str | None] = mapped_column(String(64))
    # 计量单位（登记行直映；条目属性呈现，不进标识）
    unit: Mapped[str | None] = mapped_column(String(256))
    time_span_start: Mapped[str | None] = mapped_column(String(64))
    time_span_end: Mapped[str | None] = mapped_column(String(64))
    entity_coverage_count: Mapped[int | None] = mapped_column(Integer)
    sample_value: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")
    # 本行最近一次被同步管道刷新的时间
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<IndicatorSnapshot {self.indicator_id} status={self.status}>"


class CatalogSyncState(Base):
    """同步水位（单行，id 恒为 1），只有同步管道可写。"""

    __tablename__ = "catalog_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 增量游标：上次成功同步拉到的权威库水位（权威侧 updated_at 口径）
    last_watermark: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
