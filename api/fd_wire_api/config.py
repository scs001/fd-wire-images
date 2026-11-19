"""Env 可重指的借用契约端点（borrowed-contract endpoints）+ 存储双 URL 语义。

平台对不归本仓的能力（business-mcp / registry / sub2api / token 网关）
只经显式 HTTP 契约访问：每个端点都是占位默认值，可被同名环境变量
覆盖——这是跨线互联而不互相阻塞发布的纪律（README「借用契约」）。

存储双 URL（wire-dedicated-postgres）：

- ``FD_WIRE_DATABASE_URL``：**元数据库**（PG 部署的 ``wire_platform``）——
  metering / catalog / demands / billing / admin / materialization / console
  等平台切片的收敛开关：设了即全取（优先于各模块细分 env 与旧
  ``DATABASE_URL``）。当前部署形态=与计量同库的 SQLite（PVC），
  迁 PG 时只改这一个值。
- ``FD_WIRE_DATASETS_DATABASE_URL``：**数据集库**（``wire_datasets``）——
  datasets 切片既有 env 名即此名（兼容保持，见 datasets/settings.py）；
  provenance 账本同库。
- 两个 URL 均可指 PostgreSQL（``postgresql+psycopg://`` 形态；历史形态
  ``postgres://`` / ``postgresql://`` / ``postgresql+psycopg2://`` 由
  ``normalize_database_url`` 统一改写——SQLAlchemy 2.x 对无驱动后缀的
  PG URL 回落 psycopg2，已知断，舰队教训）。
- 未设时各模块回落既有 SQLite 文件默认——dev 行为不变（双方言是常态
  而非迁移特例，design D5）。
"""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

#: PG URL 历史形态 → SQLAlchemy 2.x + psycopg3 规范形态（顺序即匹配优先，
#: ``postgresql+psycopg2://`` 必须先于 ``postgres://`` 检查避免误截断）。
_PG_URL_REWRITES: tuple[tuple[str, str], ...] = (
    ("postgresql+psycopg2://", "postgresql+psycopg://"),
    ("postgres://", "postgresql+psycopg://"),
    ("postgresql://", "postgresql+psycopg://"),
)


def normalize_database_url(url: str) -> str:
    """连接串规范化：PG 一律 psycopg3 驱动形态；其余（sqlite:// 等）原样。"""
    for legacy, canonical in _PG_URL_REWRITES:
        if url.startswith(legacy):
            return canonical + url[len(legacy) :]
    return url


def canonical_metadata_database_url() -> str | None:
    """元数据库收敛入口：返回规范化后的 ``FD_WIRE_DATABASE_URL``；未设为 None。

    各平台切片在既有 env 链最前面接上本函数的结果——生产只设这一个
    env 即把全部元数据切片切到 ``wire_platform``（PG），dev 不设则各回
    各的 SQLite 文件默认。
    """
    raw = os.getenv("FD_WIRE_DATABASE_URL")
    return normalize_database_url(raw) if raw else None


class Settings(BaseSettings):
    """运行时配置：同名环境变量优先（如 REGISTRY_URL=...）。

    统一刷新调度器（wire-refresh-scheduler）的环境项不在本类——归
    ``schedules/settings.py`` 自有单例：``FD_WIRE_REFRESH_SCHEDULER_ENABLED``
    （默认 true）、``FD_WIRE_REFRESH_SCHEDULER_TICK_SECONDS``（默认 60）、
    ``FD_WIRE_SCHEDULE_MIN_INTERVAL_DATASET``（默认 60 分钟）、
    ``FD_WIRE_SCHEDULE_MIN_INTERVAL_ARTIFACT``（默认 360 分钟——AI 刷新耗
    token，地板封无人值守成本）、``FD_WIRE_SCHEDULE_MAX_PER_CUSTOMER``
    （默认 10）。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 五库联邦数据读取工具（rows_returned 计量戳在此盖）
    business_mcp_url: str = "http://business-mcp.fd-prod.svc/execution"
    # 谦面 registry：wire 入口（鉴权/逐调用审计/转发）
    registry_url: str = "https://mcp.finddatatech.cloud"
    # sub2api 计费面：客户账户代管/余额/结算
    sub2api_base: str = "https://router.finddatatech.cloud"
    # token 网关：AI 构建循环
    token_gateway_url: str = "https://token.finddatatech.cloud"
    # 存储（wire-dedicated-postgres）：双 URL 声明位——消费方经上面的
    # canonical_metadata_database_url() / datasets 切片自身的
    # FD_WIRE_DATASETS_DATABASE_URL 读取；None=未设（回落 SQLite 默认）。
    fd_wire_database_url: str | None = None
    fd_wire_datasets_database_url: str | None = None

    def borrowed_contracts(self) -> dict[str, str]:
        """全部借用契约端点一览（示例路由与健康检查消费）。"""
        return {
            "business_mcp_url": self.business_mcp_url,
            "registry_url": self.registry_url,
            "sub2api_base": self.sub2api_base,
            "token_gateway_url": self.token_gateway_url,
        }


settings = Settings()
