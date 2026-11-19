"""告警评估引擎配置（wire-alert-evaluation，全部 env 可覆盖）。

- ``FD_WIRE_ALERTS_ENABLED``：评估器总开关（lifespan 后台循环），默认
  true；false 时 start 直接跳过。
- ``FD_WIRE_ALERT_TICK_SECONDS``：tick 间隔秒，默认 30。
- ``FD_WIRE_ALERTS_AUTO_CREATE_TABLES``：启动 create_all 兜底开关（默认
  true；生产走 alembic 0028 并可关闭）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AlertSettings(BaseSettings):
    """告警评估运行时配置：同名环境变量优先（照 schedules/settings 范式）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alerts_enabled: bool = Field(
        default=True, validation_alias="FD_WIRE_ALERTS_ENABLED"
    )
    alert_tick_seconds: int = Field(
        default=30, validation_alias="FD_WIRE_ALERT_TICK_SECONDS"
    )
    alerts_auto_create_tables: bool = Field(
        default=True, validation_alias="FD_WIRE_ALERTS_AUTO_CREATE_TABLES"
    )


@lru_cache(maxsize=1)
def get_settings() -> AlertSettings:
    """进程内单例；测试用 :func:`reset_alert_runtime` 清缓存后重设 env。"""
    return AlertSettings()
