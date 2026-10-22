"""quality 切片运行时配置（wire-asset-catalog 任务组 4）。

全部 env 可覆盖（与仓级「借用契约」纪律一致）：

- ``FD_WIRE_QUALITY_HISTORY_RETENTION``：每数据集快照历史保留条数
  （默认 30；插入后裁剪更旧行——append-only 语义下的保留期清理）。
- ``FD_WIRE_QUALITY_STALE_MULTIPLIER``：新鲜度容许倍数（默认 3；
  实际刷新间隔 > 声明节奏 × 倍数 → 「过期」，design D4）。
- ``FD_WIRE_QUALITY_UNSUPPORTED_BACKOFF_HOURS``：引擎不支持（旧 cell 无
  质量工具）的自动重试退避窗口小时数（默认 24——自动触发至多每日一次，
  手动触发不受限；design D6「不重试轰炸」）。
- ``FD_WIRE_QUALITY_RULES_MAX_PER_DATASET``：每数据集质量规则上限
  （默认 20，含停用；wire-quality-rules）。
- ``FD_WIRE_QUALITY_RULE_RESULTS_RETENTION``：每规则求值结果历史保留条数
  （默认 50；插入后裁剪更旧行）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class QualitySettings(BaseSettings):
    """quality 切片设置：FD_WIRE_* 环境变量优先。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    quality_history_retention: int = Field(
        default=30, validation_alias="FD_WIRE_QUALITY_HISTORY_RETENTION"
    )
    quality_stale_multiplier: int = Field(
        default=3, validation_alias="FD_WIRE_QUALITY_STALE_MULTIPLIER"
    )
    quality_unsupported_backoff_hours: int = Field(
        default=24, validation_alias="FD_WIRE_QUALITY_UNSUPPORTED_BACKOFF_HOURS"
    )
    #: 每数据集规则上限（含停用；wire-quality-rules spec「超上限被拒」）
    quality_rules_max_per_dataset: int = Field(
        default=20, validation_alias="FD_WIRE_QUALITY_RULES_MAX_PER_DATASET"
    )
    #: 每规则结果历史保留条数（append-only 语义下的保留期清理，同快照）
    quality_rule_results_retention: int = Field(
        default=50, validation_alias="FD_WIRE_QUALITY_RULE_RESULTS_RETENTION"
    )


@lru_cache(maxsize=1)
def get_settings() -> QualitySettings:
    """进程内单例；测试用 ``monkeypatch.setattr(get_settings(), ...)`` 就地改。"""
    return QualitySettings()
