"""产物物化计量的运行时配置（wire-platform-v1 任务 5.6）。

全部 env 可重指（与仓级「借用契约」纪律一致）：

- ``MATERIALIZATION_DATABASE_URL`` / ``DATABASE_URL``：物化表所在库。
  显式给前者则用之，否则跟随平台库（与 admin 切片同约定——billing 与
  metering 生产上同库，物化的硬停检查需要只读联查它们）。
- ``FD_WIRE_DATABASE_URL``：元数据库收敛开关（wire-dedicated-postgres）
  ——设了即压过上面两条（effective_database_url 的最高优先级）；PG
  历史形态（``postgres://`` 等）统一改写为 ``postgresql+psycopg://``。
- ``WIRE_ENTRY_URL``：wire 入口（registry 侧）基地址——物化读取逐项
  经它发出。未配置即 fail-closed（绝不猜测端点、绝不直连 business-mcp）。
- ``CUSTOMER_CREDENTIAL_MAP``：customer_id → 客户凭据（key）映射，
  ``cust-001=wsk-xxx,cust-002=wsk-yyy`` 形式（也接受 JSON 对象）。
  **缺映射即缺凭据：fail-closed 拒绝执行**，绝不静默回落平台服务凭据
  （那是 AI 探索的不计费轨道，混用会让物化行数从客户账单上消失）。
- ``MATERIALIZATION_INTERVAL``：调度扫描间隔秒，默认 60。
- ``MATERIALIZATION_SCHEDULER_ENABLED``：调度循环开关（默认开；进程内
  后台任务，见 scheduler.py）。
- ``MATERIALIZATION_INTERNAL_TOKEN``：内部端点（promote 等）的
  ``X-Internal-Token``；未配置即 fail-closed 拒绝全部内部端点。
- ``MATERIALIZATION_AUTO_CREATE_TABLES``：启动 ``create_all`` 开发兜底
  （默认 true；生产走 Alembic 0009）。
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import normalize_database_url

_DEFAULT_PLATFORM_DB = "sqlite:///./fd_wire_dev.db"
#: MATERIALIZATION_INTERVAL 下限（0/负值会让循环退化成忙轮询）
_MIN_INTERVAL_SECONDS = 1.0


def parse_credential_map(raw: str | dict[str, str] | None) -> dict[str, str]:
    """把 env 串（``cust-001=wsk-xxx,cust-002=...`` 或 JSON 对象）解析为
    customer_id → 客户凭据映射。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    text = raw.strip()
    if text.startswith("{"):
        return {str(k): str(v) for k, v in json.loads(text).items()}
    pairs: dict[str, str] = {}
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"CUSTOMER_CREDENTIAL_MAP 片段缺少 '=': {chunk!r}")
        key, _, value = chunk.partition("=")
        if not value.strip():
            raise ValueError(f"CUSTOMER_CREDENTIAL_MAP 片段凭据为空: {chunk!r}")
        pairs[key.strip()] = value.strip()
    return pairs


class MaterializationSettings(BaseSettings):
    """物化计量设置：同名环境变量优先。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 元数据库收敛开关（wire-dedicated-postgres）：设了即压过下面两条
    fd_wire_database_url: str | None = Field(
        default=None, validation_alias="FD_WIRE_DATABASE_URL"
    )
    materialization_database_url: str | None = None
    # 平台库（alembic 迁移链 0009 落物化表于此； billing/metering 联查同库）
    database_url: str | None = None
    # wire 入口（借用契约端点；空 = fail-closed 拒绝物化读取）
    wire_entry_url: str = ""
    # 客户凭据映射（缺即 fail-closed，绝不回落平台服务凭据）
    customer_credential_map: str = ""
    # 调度
    materialization_interval: float = 60.0
    materialization_scheduler_enabled: bool = True
    # 内部端点 token
    materialization_internal_token: str = ""
    # 建表兜底
    materialization_auto_create_tables: bool = True

    @property
    def effective_database_url(self) -> str:
        for candidate in (
            self.fd_wire_database_url,
            self.materialization_database_url,
            self.database_url,
        ):
            if candidate:
                return normalize_database_url(candidate)
        return _DEFAULT_PLATFORM_DB

    @property
    def interval_seconds(self) -> float:
        return max(_MIN_INTERVAL_SECONDS, self.materialization_interval)

    def credential_map(self) -> dict[str, str]:
        """customer_id → 客户凭据（解析后的 dict）。"""
        return parse_credential_map(self.customer_credential_map)


@lru_cache(maxsize=1)
def get_settings() -> MaterializationSettings:
    """进程内单例；测试用 ``reset_materialization_runtime()`` 后重设 env 再取。"""
    return MaterializationSettings()
