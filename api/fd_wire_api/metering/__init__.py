"""计量管道：registry audit 事件 → 平台计量记录（wire-platform-v1 任务 4.1）。

两条摄取入口（HTTP 推送 / registry Mongo 直读轮询）落到同一规范化层：

- 每条 registry 审计事件（MCPServerAccessRecord）规范化为一条
  ``metering_records`` 记录：身份、目标 server、工具、rows_returned、
  凭据轨道（platform|customer）与来源标记。
- ``request_id + server`` 为幂等键：重复摄取不产生重复记录。
- 缺 rows_returned 的事件不进计量，写 ``metering_anomalies`` 留痕
  （计费异常不静默——wire-facade-metering「缺戳告警」）。
"""

from .db import ensure_schema, get_engine, reset_metering_runtime
from .ingest import IngestResult, ingest_events
from .settings import get_settings

__all__ = [
    "IngestResult",
    "ensure_schema",
    "get_engine",
    "get_settings",
    "ingest_events",
    "reset_metering_runtime",
]
