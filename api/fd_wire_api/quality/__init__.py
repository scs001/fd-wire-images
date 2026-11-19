"""quality 包：数据集质量快照通道（wire-asset-catalog 任务组 4）。

- 快照计算在 cell 引擎侧（``customer_dataset_quality`` 工具），平台侧只
  经 :class:`~fd_wire_api.datasets.ingest.EngineToolClient` 收聚合计数与
  比率落 ``dataset_quality_snapshots``（append-only；生产走 alembic 0031，
  ``create_all`` 为开发兜底——本包模型挂 ``DatasetsBase``，datasets 切片
  的 ``init_db`` 建表时自然带上）。
- 新鲜度读取时计算（``freshness_statuses``——跨切片契约：资产目录列表
  端点直接导入该函数），不落库、节奏未知恒「未知」。
- 自动触发钩子（建档/同步/派生）经 ``notify_dataset_changed``——失败
  隔离，绝不影响主流程。

接线（FastAPI）：
```
from fd_wire_api.quality import router
app.include_router(router)
```
"""

from __future__ import annotations

from .models import DatasetQualitySnapshot
from .routes import router
from .service import (
    capture_snapshot,
    freshness_statuses,
    notify_dataset_changed,
    snapshot_to_dict,
)

__all__ = [
    "DatasetQualitySnapshot",
    "capture_snapshot",
    "freshness_statuses",
    "notify_dataset_changed",
    "router",
    "snapshot_to_dict",
]
