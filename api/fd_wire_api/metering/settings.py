"""计量管道配置（全部 env 可覆盖，独立于 config.py 的借用契约端点）。

环境变量一览：

- ``FD_WIRE_DATABASE_URL``：元数据库收敛开关（wire-dedicated-postgres）——
  设了即优先于 ``DATABASE_URL``（引擎取 ``effective_database_url``）；PG
  历史形态（``postgres://`` 等）统一改写为 ``postgresql+psycopg://``。
- ``DATABASE_URL``：计量库连接串（既有 env，未设上一项时生效），默认
  ``sqlite:///./fd_wire_dev.db``；测试指向临时文件或内存库。
- ``METERING_INGEST_TOKEN``：``POST /internal/metering/events`` 与轮询触发
  端点的 bearer 校验令牌；未配置时推送端点 fail-closed（403），
  部署时必须显式提供。
- ``PLATFORM_IDENTITIES``：平台服务身份名单（逗号分隔的 username，
  大小写不敏感）。命中的调用 → credential_track=platform，
  否则 customer（AI 探索调用不计入客户账单的归属依据）。
- ``PLATFORM_IDENTITY_SOURCE_MAP``：platform 轨道的「调用身份 → 来源」
  映射，``yizuo-*=yizuo,wanxing-*=wanxing,platform-agent=platform-internal``
  形式（也接受 JSON 对象；解析口径同 ``METERING_SOURCE_MAP``）。命中
  （身份精确或 ``前缀*`` 通配，大小写不敏感）→ 对应 source，用于区分
  壹座 agent / 萬星 agent / 平台内部发起的平台轨调用；未命中回退
  ``platform-internal``。发放与回填联动见 ``docs/service-credentials.md``。
- ``METERING_SOURCE_MAP``：customer 轨道的「server → 来源」映射表，
  ``server=yizuo,wanxing-mcp=wanxing`` 形式（也接受 JSON 对象）。命中
  server（精确或前缀 ``name*``）→ 对应 source；platform 轨道不走本表
  （见 ``PLATFORM_IDENTITY_SOURCE_MAP``）；其余 ``customer``。
- ``REGISTRY_MONGO_URI`` / ``REGISTRY_MONGO_DB`` /
  ``REGISTRY_MONGO_COLLECTION``：registry Mongo 直读轮询（可选路径）；
  URI 未配置时该路径禁用。
- ``METERING_POLL_LIMIT``：单次轮询批量上限，默认 500。
- ``METERING_DRIVER_ENABLED``：入账驱动（poll→settle 周期循环，metering/driver.py）
  总开关，默认 true；false 时启动跳过。
- ``METERING_DRIVER_INTERVAL_SECONDS``：驱动 tick 间隔秒，默认 300。
- ``METERING_AUTO_CREATE_TABLES``：启动时 ``create_all``（默认 true，
  便于开发；生产用 Alembic 迁移并可关闭）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import normalize_database_url

#: source 标记的合法取值（wire-facade-metering 规格口径）
ALLOWED_SOURCES: tuple[str, ...] = ("yizuo", "wanxing", "platform-internal", "customer")

#: 凭据轨道的合法取值
ALLOWED_TRACKS: tuple[str, ...] = ("platform", "customer")


def _parse_source_map(
    raw: str | dict[str, str] | None,
    *,
    label: str = "METERING_SOURCE_MAP",
) -> dict[str, str]:
    """把 env 串（``a=x,b=y`` 或 JSON 对象）解析为键 → source 映射。

    ``label`` 仅用于错误信息指认来源 env（METERING_SOURCE_MAP /
    PLATFORM_IDENTITY_SOURCE_MAP 共用同一解析口径）。
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        pairs = raw
    else:
        text = raw.strip()
        if text.startswith("{"):
            pairs = json.loads(text)
        else:
            pairs = {}
            for chunk in text.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if "=" not in chunk:
                    raise ValueError(f"{label} 片段缺少 '=': {chunk!r}")
                key, _, value = chunk.partition("=")
                pairs[key.strip()] = value.strip()
    for key, value in pairs.items():
        if value not in ALLOWED_SOURCES:
            raise ValueError(
                f"{label}[{key!r}]={value!r} 不在允许集合 {ALLOWED_SOURCES}"
            )
    return dict(pairs)


class MeteringSettings(BaseSettings):
    """计量管道运行时配置：同名环境变量优先。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 元数据库收敛开关（wire-dedicated-postgres）：设了即压过 database_url
    fd_wire_database_url: str | None = Field(
        default=None, validation_alias="FD_WIRE_DATABASE_URL"
    )
    database_url: str = "sqlite:///./fd_wire_dev.db"
    metering_ingest_token: str | None = None
    platform_identities: str = ""
    platform_identity_source_map: str = ""
    metering_source_map: str = ""
    registry_mongo_uri: str | None = None
    registry_mongo_db: str | None = None
    registry_mongo_collection: str = "audit_events"
    metering_poll_limit: int = 500
    metering_driver_enabled: bool = True
    metering_driver_interval_seconds: int = 300
    metering_auto_create_tables: bool = True

    @field_validator("metering_source_map")
    @classmethod
    def _validate_source_map(cls, v: str) -> str:
        _parse_source_map(v)  # 仅校验，保留原始串
        return v

    @field_validator("platform_identity_source_map")
    @classmethod
    def _validate_identity_source_map(cls, v: str) -> str:
        _parse_source_map(v, label="PLATFORM_IDENTITY_SOURCE_MAP")  # 仅校验，保留原始串
        return v

    def platform_identity_set(self) -> frozenset[str]:
        """平台服务身份名单（小写精确匹配）。"""
        return frozenset(
            name.strip().lower() for name in self.platform_identities.split(",") if name.strip()
        )

    def identity_source_map(self) -> dict[str, str]:
        """platform 轨「调用身份 → 来源」映射（键统一小写，配合大小写
        不敏感匹配；``前缀*`` 通配条目保持尾部 ``*``）。"""
        parsed = _parse_source_map(self.platform_identity_source_map, label="PLATFORM_IDENTITY_SOURCE_MAP")
        return {key.lower(): value for key, value in parsed.items()}

    def source_map(self) -> dict[str, str]:
        """server → source 映射（解析后的 dict）。"""
        return _parse_source_map(self.metering_source_map)

    @property
    def effective_database_url(self) -> str:
        """引擎实际连接串：FD_WIRE_DATABASE_URL > DATABASE_URL（均规范化）。"""
        return normalize_database_url(self.fd_wire_database_url or self.database_url)


@lru_cache(maxsize=1)
def get_settings() -> MeteringSettings:
    """进程内单例；测试用 ``reset_metering_runtime()`` 后重设 env 再取。"""
    return MeteringSettings()


def describe() -> dict[str, Any]:
    """当前生效的计量配置一览（不含令牌明文，排障用）。"""
    s = get_settings()
    return {
        "database_url": s.effective_database_url,
        "ingest_token_configured": bool(s.metering_ingest_token),
        "platform_identities": sorted(s.platform_identity_set()),
        "platform_identity_source_map": s.identity_source_map(),
        "source_map": s.source_map(),
        "registry_mongo_configured": bool(s.registry_mongo_uri and s.registry_mongo_db),
        "registry_mongo_collection": s.registry_mongo_collection,
        "poll_limit": s.metering_poll_limit,
        "driver_enabled": s.metering_driver_enabled,
        "driver_interval_seconds": s.metering_driver_interval_seconds,
    }
