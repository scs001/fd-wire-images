"""packs 包：源包订阅服务端（wire-source-packs）。

对外暴露 ``router``（挂到 app 即得 /api/v1/packs 全部端点）。包定义是
静态配置（definitions.py）；指标切片实时查目录镜像（不落库）；
订阅关系落元数据库（alembic 0022，create_all 由 init_db 兜底）。
"""

from .db import PacksBase, get_db, get_engine, init_db, reset_packs_runtime
from .definitions import PACKS, PACKS_BY_ID, PackDefinition, get_pack
from .models import PackSubscription
from .routes import router

__all__ = [
    "PACKS",
    "PACKS_BY_ID",
    "PackDefinition",
    "PackSubscription",
    "PacksBase",
    "get_db",
    "get_engine",
    "get_pack",
    "init_db",
    "reset_packs_runtime",
    "router",
]
