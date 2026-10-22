"""market 包：数据集市场（wire-dataset-marketplace）。

对外暴露 ``router``（挂到 app 即得 /api/v1/market/* 与
/api/v1/datasets/{id}/visibility|grants|access-requests 全部端点）。

三张表（``dataset_market_subscriptions`` / ``dataset_share_grants`` /
``dataset_access_requests``）与可见性四列都挂 ``DatasetsBase``——与
datasets / assets 切片同库同引擎（post-PG-consolidation 为同一平台元数
据库），建表走 datasets 切片 ``init_db`` 的 create_all 兜底（本包 models
被 router 导入即注册进 DatasetsBase.metadata），生产走 alembic 0034。

职责分层：
- ``service.py``：可见性唯一写入口（闸门 + 留痕 + 幂等；发布方写侧）；
- ``browse.py``：市场浏览/检索/订阅/申请的查询与访问判定（消费侧）；
- ``routes.py``：把上面两层接成 FastAPI 端点（鉴权 + 404 + commit 纪律）。

产物市场（wire-artifact-marketplace）住同包：``artifact_service.py`` /
``artifact_browse.py`` / ``artifact_routes.py``（``artifact_router``）——
表分库（AiBase），代码同构不共表。
"""

from __future__ import annotations

from .artifact_routes import router as artifact_router
from .models import DatasetAccessRequest, DatasetShareGrant, MarketSubscription
from .routes import router

__all__ = [
    "DatasetAccessRequest",
    "DatasetShareGrant",
    "MarketSubscription",
    "artifact_router",
    "router",
]
