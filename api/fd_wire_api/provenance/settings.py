"""provenance 切片运行时配置（wire-provenance-ledger 任务组 2）。

全部 env 可覆盖（``FD_WIRE_PROVENANCE_`` 前缀；字段名与 env 名不同前缀，
统一用 ``validation_alias`` 显式绑定——学 datasets/settings.py 的写法）：

- ``FD_WIRE_PROVENANCE_ANCHOR_BACKEND``：锚定后端
  ``none|ots|customer|consortium|rfc3161``（默认 none——chain_only 观察
  期；后端实现在任务组 5，本配置先行就绪即插）。
- ``FD_WIRE_PROVENANCE_EPOCH_HOURS``：封存周期小时数（默认 1）。
- ``FD_WIRE_PROVENANCE_INGEST_TOKEN``：内部入账端点令牌；非空时
  ``POST /api/v1/provenance/ingest`` 要求 ``X-Provenance-Token`` 头匹配。
- ``FD_WIRE_PROVENANCE_SIGNING_KEY_PATH``：客户自持检查点签名钥匙 PEM
  路径（生产经 OpenBAO 交付；空 = 未配置，anchor-customer 后端启用时生效）。
- ``FD_WIRE_PROVENANCE_CHECKPOINT_DIR``：客户自持检查点文件落盘目录
  （默认 ``./provenance_checkpoints``，按账本名分子目录；任务组 5）。
- ``FD_WIRE_PROVENANCE_OTS_CALENDARS``：OTS calendar URL 逗号分隔覆盖
  （默认官方聚合池若干；测试 monkeypatch 用，任务组 5）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 可插拔锚定后端（D5：v1 实装 ots/customer，consortium/rfc3161 留位）
AnchorBackend = Literal["none", "ots", "customer", "consortium", "rfc3161"]


class ProvenanceSettings(BaseSettings):
    """provenance 切片设置：env 优先，未配置取安全默认（不锚定、不设令牌）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anchor_backend: AnchorBackend = Field(
        default="none", validation_alias="FD_WIRE_PROVENANCE_ANCHOR_BACKEND"
    )
    epoch_hours: int = Field(
        default=1, ge=1, validation_alias="FD_WIRE_PROVENANCE_EPOCH_HOURS"
    )
    ingest_token: str = Field(
        default="", validation_alias="FD_WIRE_PROVENANCE_INGEST_TOKEN"
    )
    signing_key_path: str = Field(
        default="", validation_alias="FD_WIRE_PROVENANCE_SIGNING_KEY_PATH"
    )
    checkpoint_dir: str = Field(
        default="provenance_checkpoints",
        validation_alias="FD_WIRE_PROVENANCE_CHECKPOINT_DIR",
    )
    ots_calendars: str = Field(
        default="", validation_alias="FD_WIRE_PROVENANCE_OTS_CALENDARS"
    )


@lru_cache(maxsize=1)
def get_settings() -> ProvenanceSettings:
    """进程内单例；测试用 ``reset_provenance_runtime()`` 清缓存后重设 env。"""
    return ProvenanceSettings()
