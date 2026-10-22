"""指标目录镜像的运行时配置。

全部 env 可重指（与仓级「借用契约」纪律一致）：
- ``INDICATOR_REGISTRY_URL``：权威源（fd-open-data-mcp 侧
  ``list_registry_entries``，登记表全量枚举）基地址；MCP streamable-http
  的 ``/mcp`` 后缀由客户端自动补齐。
- ``INDICATOR_REGISTRY_TOKEN``：权威源 Bearer 凭据（公网 MCP 入口鉴权）；空 = 匿名。
- ``CATALOG_DATABASE_URL``：镜像库专用覆盖（缺省落平台库，见下）。
- ``DATABASE_URL``：平台库（计量迁移链 ``api/alembic`` 管理的库）；
  未显式给 ``CATALOG_DATABASE_URL`` 时镜像表建在这里。
- ``FD_WIRE_DATABASE_URL``：元数据库收敛开关（wire-dedicated-postgres）
  ——设了即压过上面两条（effective_database_url 的最高优先级）；PG
  历史形态（``postgres://`` 等）统一改写为 ``postgresql+psycopg://``。
- ``CATALOG_INTERNAL_TOKEN``：内部手动触发同步的 token；为空时触发端点一律拒绝。
- ``CATALOG_SYNC_MIN_TOTAL``：单次拉取最小可信条数（缺省 1,000）——低于它
  视为上游异常，同步中止且不提交，旧镜像原样保留。
- ``CATALOG_SYNC_INTERVAL_SECONDS``：同步循环间隔，默认 24h。
- ``CATALOG_SYNC_ENABLED`` / ``CATALOG_SYNC_ON_STARTUP``：调度开关与启动即同步。
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import normalize_database_url

_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60  # 每日
_DEFAULT_PLATFORM_DB = "sqlite:///./fd_wire_dev.db"
# 上游登记表常态规模 5 万+；1,000 是「工具明显异常」的保守下限
_DEFAULT_MIN_TOTAL = 1000


class CatalogSettings(BaseSettings):
    """目录镜像设置：同名环境变量优先。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 元数据库收敛开关（wire-dedicated-postgres）：设了即压过下面两条
    fd_wire_database_url: str | None = Field(
        default=None, validation_alias="FD_WIRE_DATABASE_URL"
    )
    # 权威源（统一指标注册目录 / fd-open-data-mcp 的 list_registry_entries）。
    # 缺省指向网内权威副本（与权威库同一 Postgres）；公网入口
    # https://mcp.finddatatech.cloud/fd-open-data-mcp 亦可（走网关鉴权，
    # 需 INDICATOR_REGISTRY_TOKEN）。/mcp 后缀由客户端自动补齐。
    indicator_registry_url: str = "http://fd-open-data-mcp.fd-prod.svc:8300"
    # 权威源 Bearer 凭据（公网 MCP 入口鉴权；空 = 匿名，网内副本即匿名）
    indicator_registry_token: str = ""
    # 镜像库连接串：显式给 CATALOG_DATABASE_URL 则用之；否则跟随平台库 DATABASE_URL
    catalog_database_url: str | None = None
    # 平台库（alembic 迁移链 0002_catalog 落镜像表于此）
    database_url: str | None = None
    # 手动触发同步的 internal token
    catalog_internal_token: str = ""
    # 单次拉取最小可信条数：低于它视为上游异常，中止且不提交（保旧镜像）
    catalog_sync_min_total: int = _DEFAULT_MIN_TOTAL
    # 调度
    catalog_sync_interval_seconds: float = float(_DEFAULT_INTERVAL_SECONDS)
    catalog_sync_enabled: bool = True
    catalog_sync_on_startup: bool = True
    # 启动即同步前的小额宽限（避开极短生命周期的进程）
    catalog_sync_startup_grace_seconds: float = 10.0

    @property
    def sync_interval_seconds(self) -> float:
        return max(1.0, self.catalog_sync_interval_seconds)

    @property
    def startup_grace_seconds(self) -> float:
        return max(0.0, self.catalog_sync_startup_grace_seconds)

    @property
    def effective_database_url(self) -> str:
        for candidate in (self.fd_wire_database_url, self.catalog_database_url, self.database_url):
            if candidate:
                return normalize_database_url(candidate)
        return _DEFAULT_PLATFORM_DB


settings = CatalogSettings()
