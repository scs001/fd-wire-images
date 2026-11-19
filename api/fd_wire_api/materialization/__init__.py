"""materialization 包：产物物化计量（wire-platform-v1 任务 5.6）。

把已转正产物（dashboard / composite_indicator / research_report）的定时
刷新/组合计算/报告出数跑在**客户凭据**轨道上：逐项读数经 wire 入口
（``WIRE_ENTRY_URL``）发出 → registry audit 侧自动计量进客户账单——与
AI 探索的平台服务凭据轨道（ai/executor.py）相反，见 credentials.py 的
双凭据边界说明。

入口一览：
- 内部端点：``router``（promote 转正 + 运行摘要查询）
- 运行器：``MaterializationRunner.run(customer_id, artifact_id)``
- 调度：``scheduler``（asyncio 循环，``MATERIALIZATION_INTERVAL`` 扫描）
- 硬停：``quota.SqlQuotaGuard``（billing 聚合口径只读，超限 skip）
- 建表：``init_db``（开发兜底；生产走 alembic 0009）
"""

from .credentials import (
    CustomerCredentialMissingError,
    resolve_customer_credential,
)
from .cron import CronSchedule, parse_cron, validate_cron
from .db import (
    MatBase,
    get_db,
    get_engine,
    get_session,
    init_db,
    reset_materialization_runtime,
    utc_now_naive,
)
from .models import MaterializedArtifact
from .quota import (
    STATUS_QUOTA_EXHAUSTED,
    STATUS_QUOTA_UNVERIFIABLE,
    AllowAllQuotaGuard,
    QuotaGuard,
    QuotaVerdict,
    SqlQuotaGuard,
)
from .runner import (
    STATUS_FAILED,
    STATUS_FAILED_NO_CREDENTIAL,
    STATUS_FAILED_NO_WIRE_ENTRY,
    STATUS_FAILED_SPEC,
    STATUS_PARTIAL_FAILURE,
    STATUS_SUCCESS,
    ArtifactNotFoundError,
    ItemOutcome,
    MaterializationRunner,
    RunResult,
)
from .scheduler import (
    start_materialization_scheduler,
    stop_materialization_scheduler,
    tick_once,
)
from .specs import (
    MATERIALIZABLE_KINDS,
    ReadItem,
    SpecReadListError,
    extract_read_list,
)
from .routes import router
from .settings import get_settings, parse_credential_map

__all__ = [
    "STATUS_FAILED",
    "STATUS_FAILED_NO_CREDENTIAL",
    "STATUS_FAILED_NO_WIRE_ENTRY",
    "STATUS_FAILED_SPEC",
    "STATUS_PARTIAL_FAILURE",
    "STATUS_QUOTA_EXHAUSTED",
    "STATUS_QUOTA_UNVERIFIABLE",
    "STATUS_SUCCESS",
    "AllowAllQuotaGuard",
    "ArtifactNotFoundError",
    "CronSchedule",
    "CustomerCredentialMissingError",
    "ItemOutcome",
    "MATERIALIZABLE_KINDS",
    "MatBase",
    "MaterializationRunner",
    "MaterializedArtifact",
    "QuotaGuard",
    "QuotaVerdict",
    "ReadItem",
    "RunResult",
    "SpecReadListError",
    "SqlQuotaGuard",
    "extract_read_list",
    "get_db",
    "get_engine",
    "get_session",
    "get_settings",
    "init_db",
    "parse_credential_map",
    "parse_cron",
    "reset_materialization_runtime",
    "resolve_customer_credential",
    "router",
    "start_materialization_scheduler",
    "stop_materialization_scheduler",
    "tick_once",
    "utc_now_naive",
    "validate_cron",
]
