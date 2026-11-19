"""DAG 执行器运行时配置（wire-dag-executor D2/D3/D4，全部 env 可覆盖）。

环境变量一览（同名 env 优先；沿用 schedules/settings 的 pydantic-settings
范式，测试经 ``reset_pipeline_settings()`` 清缓存后重设 env）：

- ``PIPELINE_RUN_CONCURRENCY``：每 run 并发上限，缺省 2（钳制 1..8）——独立
  分支真并行，引擎调用是独立 HTTP，无共享事务；
- ``PIPELINE_NODE_TIMEOUT_SECONDS``：单节点超时，缺省 600s（超时=失败）；
- ``PIPELINE_RUN_BUDGET_SECONDS``：run 级墙钟预算，缺省 1800s（超预算→未
  启动节点 skipped、run 记 failed「运行超预算」）；
- ``PIPELINE_RETRY_BACKOFF_SECONDS``：节点重试退避序列，缺省 ``"5,30"``
  （仅可重试错误生效）；
- ``PIPELINE_RUN_WAIT_SECONDS``：``POST /run`` 短等待窗，缺省 30s（窗口内
  完成返回既有形状，超窗返回 queued/running run + 运行 id）；
- ``PIPELINE_WORKER_ENABLED``：租约队列 worker 总开关，缺省 true；测试环境
  （pytest 在进程内）默认 no-op，显式设 true 才启动；
- ``PIPELINE_WORKER_POLL_SECONDS``：worker 轮询间隔，缺省 2s；
- ``PIPELINE_HEARTBEAT_SECONDS``：心跳间隔，缺省 10s（契约 ≤10s）；
- ``PIPELINE_LEASE_SECONDS``：租约有效期，缺省 60s（无心跳视为死租约可被
  其他 worker 重领取）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 并发硬边界（design D2：1..8）
CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 8


class PipelineSettings(BaseSettings):
    """DAG 执行器配置单例（env 同名覆盖）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    run_concurrency: int = Field(
        default=2, validation_alias="PIPELINE_RUN_CONCURRENCY"
    )
    node_timeout_seconds: float = Field(
        default=600.0, validation_alias="PIPELINE_NODE_TIMEOUT_SECONDS"
    )
    run_budget_seconds: float = Field(
        default=1800.0, validation_alias="PIPELINE_RUN_BUDGET_SECONDS"
    )
    retry_backoff: str = Field(
        default="5,30", validation_alias="PIPELINE_RETRY_BACKOFF_SECONDS"
    )
    run_wait_seconds: float = Field(
        default=30.0, validation_alias="PIPELINE_RUN_WAIT_SECONDS"
    )
    worker_enabled: bool = Field(
        default=True, validation_alias="PIPELINE_WORKER_ENABLED"
    )
    worker_poll_seconds: float = Field(
        default=2.0, validation_alias="PIPELINE_WORKER_POLL_SECONDS"
    )
    heartbeat_seconds: float = Field(
        default=10.0, validation_alias="PIPELINE_HEARTBEAT_SECONDS"
    )
    lease_seconds: float = Field(
        default=60.0, validation_alias="PIPELINE_LEASE_SECONDS"
    )

    @property
    def effective_concurrency(self) -> int:
        """实际并发：钳制 1..8（配置越界不炸 run，按边界如实执行）。"""
        return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, int(self.run_concurrency)))

    @property
    def retry_backoff_seconds(self) -> tuple[float, ...]:
        """退避序列（逗号分隔 env；非法项跳过，空则退回 5/30）。"""
        values: list[float] = []
        for chunk in str(self.retry_backoff).split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                values.append(max(0.0, float(chunk)))
            except ValueError:
                continue
        return tuple(values) or (5.0, 30.0)


@lru_cache(maxsize=1)
def get_settings() -> PipelineSettings:
    """进程内单例；测试 ``reset_pipeline_settings()`` 后重设 env 再取。"""
    return PipelineSettings()


def reset_pipeline_settings() -> None:
    """清配置缓存（测试切换 env 用）。"""
    get_settings.cache_clear()
