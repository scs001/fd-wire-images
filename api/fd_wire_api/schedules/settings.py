"""统一刷新调度器配置（wire-refresh-scheduler，全部 env 可覆盖）。

环境变量一览（同名 env 优先；``FD_WIRE_`` 前缀项经 validation_alias 对齐）：

- ``FD_WIRE_REFRESH_SCHEDULER_ENABLED``：调度器总开关（lifespan 后台循环），
  默认 true；false 时 start 直接跳过。
- ``FD_WIRE_REFRESH_SCHEDULER_TICK_SECONDS``：tick 间隔秒，默认 60。
- ``FD_WIRE_SCHEDULE_MIN_INTERVAL_DATASET``：数据集调度最小间隔（分钟），
  默认 60——零 LLM 成本，地板只防滥用。
- ``FD_WIRE_SCHEDULE_MIN_INTERVAL_ARTIFACT``：看板调度最小间隔（分钟），
  默认 360——AI 刷新每轮耗 token，地板封无人值守烧钱面。
- ``FD_WIRE_SCHEDULE_MAX_PER_CUSTOMER``：每客户调度行数配额，默认 10。
- ``FD_WIRE_SCHEDULES_DATABASE_URL``：调度表库连接串（未设回落
  ``FD_WIRE_DATABASE_URL`` 收敛开关 / ``DATABASE_URL`` / 本地开发 SQLite）。
- ``FD_WIRE_SCHEDULES_AUTO_CREATE_TABLES``：启动 create_all 兜底开关
  （默认 true；生产走 alembic 0023 并可关闭）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import normalize_database_url


class ScheduleSettings(BaseSettings):
    """刷新调度器运行时配置：同名环境变量优先（照 metering/settings 范式）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 元数据库收敛开关（wire-dedicated-postgres）：设了即压过细分 env
    fd_wire_database_url: str | None = Field(
        default=None, validation_alias="FD_WIRE_DATABASE_URL"
    )
    # 本切片细分连接串（测试指向临时库）
    fd_wire_schedules_database_url: str | None = Field(
        default=None, validation_alias="FD_WIRE_SCHEDULES_DATABASE_URL"
    )
    database_url: str = "sqlite:///./fd_wire_schedules.db"

    refresh_scheduler_enabled: bool = Field(
        default=True, validation_alias="FD_WIRE_REFRESH_SCHEDULER_ENABLED"
    )
    refresh_scheduler_tick_seconds: int = Field(
        default=60, validation_alias="FD_WIRE_REFRESH_SCHEDULER_TICK_SECONDS"
    )
    min_interval_dataset_minutes: int = Field(
        default=60, validation_alias="FD_WIRE_SCHEDULE_MIN_INTERVAL_DATASET"
    )
    min_interval_artifact_minutes: int = Field(
        default=360, validation_alias="FD_WIRE_SCHEDULE_MIN_INTERVAL_ARTIFACT"
    )
    max_per_customer: int = Field(
        default=10, validation_alias="FD_WIRE_SCHEDULE_MAX_PER_CUSTOMER"
    )
    schedules_auto_create_tables: bool = Field(
        default=True, validation_alias="FD_WIRE_SCHEDULES_AUTO_CREATE_TABLES"
    )

    @property
    def effective_database_url(self) -> str:
        """调度表实际连接串：收敛开关 > 细分 env > DATABASE_URL（均规范化）。"""
        url = (
            self.fd_wire_database_url
            or self.fd_wire_schedules_database_url
            or self.database_url
        )
        return normalize_database_url(url)


@lru_cache(maxsize=1)
def get_settings() -> ScheduleSettings:
    """进程内单例；测试用 ``reset_schedules_runtime()`` 后重设 env 再取。"""
    return ScheduleSettings()
