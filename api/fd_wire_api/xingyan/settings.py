"""星研内容供给切片的运行时配置。

env 链与仓级纪律一致（「借用契约」+ 元数据库收敛开关）：
- ``XINGYAN_FEED_BASE_URL``：官网基地址（feed 与 manifest 同源）。
  生产走公网域名；集群内如走 mesh 内网地址亦由此覆盖。
- ``XINGYAN_FEED_SCHEMA_RANGE``：声明兼容的 feed 契约版本（逗号分隔，
  如 ``1`` 或 ``1,2``）。上游 ``schema_version`` 不在范围内即拒摄——
  宁僵不猜字段。
- ``XINGYAN_SYNC_INTERVAL_SECONDS``：摄取循环间隔，默认 24h。
- ``XINGYAN_SYNC_ENABLED`` / ``XINGYAN_SYNC_ON_STARTUP``：调度开关与
  启动即摄取。
- ``XINGYAN_INTERNAL_TOKEN``：``POST /internal/xingyan/sync`` 的凭据；
  为空时该端点一律 403（fail-closed）。
- ``XINGYAN_MIN_ZONE_ITEMS``：单区最小可信条数；低于它视为上游半版本，
  整体拒摄（与 catalog 的 ``CATALOG_SYNC_MIN_TOTAL`` 同口径）。
- 库连接：``FD_WIRE_DATABASE_URL``（元数据库收敛开关）>
  ``XINGYAN_DATABASE_URL`` > ``DATABASE_URL`` > 本地 dev SQLite。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import normalize_database_url

_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60  # 每日
_DEFAULT_PLATFORM_DB = "sqlite:///./fd_wire_dev.db"
# 三区当前规模 4/11/3；1 是"该区为空"的保守下限（空区是上游故障信号）
_DEFAULT_MIN_ZONE_ITEMS = 1


class XingyanSettings(BaseSettings):
    """星研供给切片设置：同名环境变量优先。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 元数据库收敛开关（wire-dedicated-postgres）：设了即压过下面两条
    fd_wire_database_url: str | None = Field(
        default=None, validation_alias="FD_WIRE_DATABASE_URL"
    )
    xingyan_database_url: str | None = Field(
        default=None, validation_alias="XINGYAN_DATABASE_URL"
    )
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")

    feed_base_url: str = Field(
        default="https://www.finddatatech.cloud",
        validation_alias="XINGYAN_FEED_BASE_URL",
    )
    feed_schema_range: str = Field(default="1", validation_alias="XINGYAN_FEED_SCHEMA_RANGE")
    sync_interval_seconds: float = Field(
        default=float(_DEFAULT_INTERVAL_SECONDS),
        validation_alias="XINGYAN_SYNC_INTERVAL_SECONDS",
    )
    sync_enabled: bool = Field(default=True, validation_alias="XINGYAN_SYNC_ENABLED")
    sync_on_startup: bool = Field(default=True, validation_alias="XINGYAN_SYNC_ON_STARTUP")
    sync_startup_grace_seconds: float = Field(
        default=10.0, validation_alias="XINGYAN_SYNC_STARTUP_GRACE_SECONDS"
    )
    internal_token: str = Field(default="", validation_alias="XINGYAN_INTERNAL_TOKEN")
    min_zone_items: int = Field(
        default=_DEFAULT_MIN_ZONE_ITEMS, validation_alias="XINGYAN_MIN_ZONE_ITEMS"
    )
    http_timeout_seconds: float = Field(
        default=30.0, validation_alias="XINGYAN_HTTP_TIMEOUT_SECONDS"
    )
    auto_create_tables: bool = Field(
        default=True, validation_alias="XINGYAN_AUTO_CREATE_TABLES"
    )

    @property
    def effective_database_url(self) -> str:
        for candidate in (
            self.fd_wire_database_url,
            self.xingyan_database_url,
            self.database_url,
        ):
            if candidate:
                return normalize_database_url(candidate)
        return _DEFAULT_PLATFORM_DB

    @property
    def compatible_schema_versions(self) -> frozenset[str]:
        """声明兼容的 feed 契约版本集（空串=无兼容版本，等价于拒摄一切）。"""
        return frozenset(
            part.strip() for part in self.feed_schema_range.split(",") if part.strip()
        )

    @property
    def interval_seconds(self) -> float:
        return max(60.0, self.sync_interval_seconds)

    @property
    def startup_grace_seconds(self) -> float:
        return max(0.0, self.sync_startup_grace_seconds)


@lru_cache(maxsize=1)
def get_settings() -> XingyanSettings:
    return XingyanSettings()
