"""指标目录镜像（wire-indicator-catalog，任务 6.1）。

统一指标注册目录的每日只读镜像：
- 模型：``models``（迁移链 ``api/alembic`` 的 ``0002_catalog`` 落表、
  ``0010_catalog_source_db`` 加 source_db/unit 列；启动时另有幂等
  ``init_db``/create_all 兜底）
- 唯一写路径：``sync.IndicatorRegistrySync``（权威源 list_registry_entries
  全量枚举 → 单事务全量重刷）
- 调度：``scheduler``（asyncio 循环，间隔 env 可配，默认 24h）
- API：``routes``（公开查询 + internal 手动同步触发）

接线（FastAPI ≥0.142 无 add_event_handler，一律 lifespan）：
```
app = FastAPI(lifespan=lifespan)
def lifespan(app):
    register_catalog(app)      # 或模块导入期直接调用
    startup_catalog()          # 幂等建表 + 启动每日同步循环
    yield
    shutdown_catalog()
```
"""

from __future__ import annotations

from fastapi import FastAPI

from .db import MirrorWriteBlocked, init_db, reset_engines
from .routes import internal_router, public_router
from .scheduler import start_sync_scheduler, stop_sync_scheduler
from .settings import settings as catalog_settings

__all__ = [
    "MirrorWriteBlocked",
    "catalog_settings",
    "init_db",
    "internal_router",
    "public_router",
    "register_catalog",
    "reset_engines",
    "shutdown_catalog",
    "startup_catalog",
]


def register_catalog(app: FastAPI) -> None:
    """把目录镜像路由挂到应用（生命周期经 ``startup_catalog``/``shutdown_catalog``）。"""
    app.include_router(public_router)
    app.include_router(internal_router)


def startup_catalog() -> None:
    """幂等建表 + 启动每日同步循环（在事件循环内调用）。"""
    init_db()
    start_sync_scheduler()


def shutdown_catalog() -> None:
    stop_sync_scheduler()
