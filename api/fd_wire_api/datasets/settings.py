"""datasets 切片运行时配置（wire-customer-local-data 任务组 2）。

全部 env 可覆盖（与仓级「借用契约」纪律一致）：

- ``FD_WIRE_DATASETS_DATABASE_URL``：数据集登记/同步/修正/上传登记表连接串，
  默认本地 SQLite 文件；测试指向临时库。
- ``FD_WIRE_DATASETS_AUTO_CREATE_TABLES``：启动 ``create_all`` 开发兜底
  （默认 true；生产走 Alembic 迁移链 0011）。
- ``FD_WIRE_DATASETS_UPLOAD_DIR``：上传原件目录（CSV 原样存；Excel 转 CSV 暂存）。
- ``FD_WIRE_DATASET_STORAGE_LIMIT_BYTES``：每客户存储配额（默认 1 GiB）——
  超限 fail-closed（拒收新同步与新数据集）；恢复途径 = 删除数据集或升配。
- ``FD_WIRE_DATASET_MAX_ROWS``：单数据集行上限（默认 500000，与引擎
  ``CUSTOMER_DATASET_MAX_ROWS`` 同默认）；超限拒收并如实提示。
- ``FD_WIRE_DATASET_MIN_SYNC_MINUTES``：同步频次下限（默认 15 分钟），
  随连接器配置下发（本地调度不得更频）。
- ``FD_WIRE_DATASET_CONNECTOR_POLL_SECONDS``：连接器心跳/轮询周期
  （默认 60s）；离线判定 = now - last_heartbeat_at > 3×该值（默认 180s）。
- ``FD_WIRE_FREE_MAX_DATASETS``：免费档（api 层）数据集个数上限（默认 3，
  wire-free-tier-data-gate D3）；建集超限 fail-closed（429 带余量）。
- ``FD_WIRE_FREE_MAX_ROWS``：免费档单数据集行数上限（默认 10000）；
  写入超限 fail-closed（429 带上限与实际行数，不留半写状态）。

引擎通道凭据**复用 AI 轨道**（见 ``ingest.py``）：``AI_CELL_ENDPOINT_MAP``
（customer_id → cell 的 registry wire 入口）与 ``AI_PLATFORM_CREDENTIAL``。
本切片自身不持有、不落任何 cell 访问凭据。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatasetsSettings(BaseSettings):
    """datasets 切片设置：契约 §3.1 的 FD_WIRE_* 环境变量优先。

    字段名与 env 名不同名（``FD_WIRE_DATASET_*`` 前缀）——统一用
    ``validation_alias`` 显式绑定，避免依赖 pydantic-settings 的字段名推导。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    datasets_database_url: str = Field(
        default="sqlite:///./fd_wire_datasets.db",
        validation_alias="FD_WIRE_DATASETS_DATABASE_URL",
    )
    datasets_auto_create_tables: bool = Field(
        default=True, validation_alias="FD_WIRE_DATASETS_AUTO_CREATE_TABLES"
    )
    datasets_upload_dir: str = Field(
        default="./fd_wire_dataset_uploads",
        validation_alias="FD_WIRE_DATASETS_UPLOAD_DIR",
    )
    dataset_storage_limit_bytes: int = Field(
        default=1073741824, validation_alias="FD_WIRE_DATASET_STORAGE_LIMIT_BYTES"
    )
    dataset_max_rows: int = Field(
        default=500000, validation_alias="FD_WIRE_DATASET_MAX_ROWS"
    )
    dataset_min_sync_minutes: int = Field(
        default=15, validation_alias="FD_WIRE_DATASET_MIN_SYNC_MINUTES"
    )
    dataset_connector_poll_seconds: int = Field(
        default=60, validation_alias="FD_WIRE_DATASET_CONNECTOR_POLL_SECONDS"
    )
    free_tier_max_datasets: int = Field(
        default=3, validation_alias="FD_WIRE_FREE_MAX_DATASETS"
    )
    free_tier_max_rows: int = Field(
        default=10000, validation_alias="FD_WIRE_FREE_MAX_ROWS"
    )

    @property
    def heartbeat_offline_seconds(self) -> int:
        """离线阈值：3×心跳周期（连接器状态由心跳推导，不落库）。"""
        return 3 * self.dataset_connector_poll_seconds


@lru_cache(maxsize=1)
def get_settings() -> DatasetsSettings:
    """进程内单例；测试用 ``reset_datasets_runtime()`` 后重设 env 再取。"""
    return DatasetsSettings()