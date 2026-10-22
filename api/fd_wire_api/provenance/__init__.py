"""provenance 包：平台侧溯源账本（wire-provenance-ledger 任务组 2）。

- 账本表 ``ledger_events`` 与 datasets 同库（D1：复用 ``..datasets.db``
  的引擎/会话与 ``DatasetsBase``，迁移链 0012 建表 + 禁改禁删触发器）。
- ``init_provenance_db``：开发兜底 = create_all 建账本表 + 不可变触发器
  （生产走 Alembic 0012；建表开关复用 datasets 的 AUTO_CREATE_TABLES——
  两表同库同策略）。
- ``router``：客户面概览/时间线/状态/调档 + 内部 ingest 入账端点。

接线（FastAPI）：
```
from fd_wire_api.provenance import router
app.include_router(router)
# 首个请求经 get_provenance_db 幂等兜底建表+触发器（生产走迁移，无需此步）
```
"""

from __future__ import annotations

import threading

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..datasets.db import get_engine
from ..datasets.settings import get_settings as _get_datasets_settings
from .models import LedgerEvent
from .routes import router
from .settings import get_settings

__all__ = [
    "LedgerEvent",
    "get_settings",
    "init_provenance_db",
    "reset_provenance_runtime",
    "router",
]

#: 与迁移 0012 同一条拒绝消息（触发器双保险中的数据库侧）
_IMMUTABLE_MSG = "ledger_events is append-only (wire-provenance-ledger)"

_bootstrap_lock = threading.Lock()
_bootstrap_done = False


def _install_immutable_triggers(engine: Engine) -> None:
    """BEFORE UPDATE/DELETE 拒绝触发器（与迁移 0012 同构，双方言、幂等）。

    SQLite 单触发器只支持单事件：拆 update/delete 两个；PostgreSQL 用
    函数 + 组合触发器。CREATE ... IF NOT EXISTS / OR REPLACE 保证幂等。
    """
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                text(
                    "CREATE OR REPLACE FUNCTION _fd_wire_ledger_events_immutable()"
                    " RETURNS trigger AS $$\n"
                    f"BEGIN\n RAISE EXCEPTION '{_IMMUTABLE_MSG}';\nEND\n"
                    "$$ LANGUAGE plpgsql"
                )
            )
            conn.execute(
                text("DROP TRIGGER IF EXISTS ledger_events_no_update ON ledger_events")
            )
            conn.execute(
                text(
                    "CREATE TRIGGER ledger_events_no_update"
                    " BEFORE UPDATE OR DELETE ON ledger_events"
                    " FOR EACH ROW"
                    " EXECUTE FUNCTION _fd_wire_ledger_events_immutable()"
                )
            )
        else:
            for event in ("update", "delete"):
                conn.execute(
                    text(
                        f"CREATE TRIGGER IF NOT EXISTS ledger_events_no_{event}"
                        f" BEFORE {event.upper()} ON ledger_events\n"
                        f"BEGIN\n SELECT RAISE(ABORT, '{_IMMUTABLE_MSG}');\nEND"
                    )
                )


def init_provenance_db() -> None:
    """幂等建账本表 + 不可变触发器（开发兜底；生产走 Alembic 0012）。

    建表开关复用 ``FD_WIRE_DATASETS_AUTO_CREATE_TABLES``（同库同策略：
    生产关闭 create_all、完全走迁移链，触发器随 0012 建）。
    """
    global _bootstrap_done
    if not _get_datasets_settings().datasets_auto_create_tables or _bootstrap_done:
        return
    with _bootstrap_lock:
        if _bootstrap_done:
            return
        engine = get_engine()
        LedgerEvent.__table__.create(bind=engine, checkfirst=True)
        _install_immutable_triggers(engine)
        _bootstrap_done = True


def reset_provenance_runtime() -> None:
    """清空本切片可变缓存（触发器就绪标记/设置单例）——测试切换 env 用。

    引擎/会话缓存属 datasets 运行时（``reset_datasets_runtime()``），同库
    共享，由 datasets 侧测试基建统一重置。
    """
    global _bootstrap_done
    _bootstrap_done = False
    get_settings.cache_clear()
