"""assets 包：客户侧数据资产目录（wire-asset-catalog）。

对外暴露 ``router``（挂到 app 即得 /api/v1/assets 与
/api/v1/datasets/{id}/asset-meta 全部端点）。两张表
（``dataset_asset_meta`` / ``asset_meta_events``）挂 ``DatasetsBase``——
与 datasets 切片同库同引擎（post-PG-consolidation 为同一平台元数据库），
建表走 datasets 切片的 ``init_db`` create_all 兜底（本模块 models 被
router 导入即注册进 DatasetsBase.metadata），生产走 alembic 0030。
"""

from .models import AssetMeta, AssetMetaEvent, SENSITIVITY_DEFAULT, SENSITIVITY_LEVELS
from .routes import router

__all__ = [
    "AssetMeta",
    "AssetMetaEvent",
    "SENSITIVITY_DEFAULT",
    "SENSITIVITY_LEVELS",
    "router",
]
