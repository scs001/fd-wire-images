"""alerts 切片数据库基础设施（alert_states/alert_events 两表）。

两表挂 ``AiBase``（与产物同引擎同会话面）：引擎/会话直接复用
:mod:`fd_wire_api.ai.db`（``AI_DATABASE_URL`` 轨道），本模块只提供幂等
``init_db`` 建表兜底（生产走 alembic 0028；``FD_WIRE_ALERTS_AUTO_CREATE_
TABLES=0`` 关闭）与测试用运行时复位。请求路径的会话依赖同样复用
``ai.db.get_db``（artifacts 路由同款）。
"""

from __future__ import annotations

import threading

from ..ai.db import get_engine
from .settings import get_settings

_init_lock = threading.Lock()
_initialized = False


def init_db() -> None:
    """幂等建 alert 两表（开发兜底；生产走 alembic 0028）。

    只建本切片两表（checkfirst）——ai/artifacts 等其余 AiBase 表由各自
    切片的 init 覆盖，避免重复职责。
    """
    global _initialized
    if _initialized or not get_settings().alerts_auto_create_tables:
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再建表

        engine = get_engine()
        models.AlertState.__table__.create(bind=engine, checkfirst=True)
        models.AlertEvent.__table__.create(bind=engine, checkfirst=True)
        _initialized = True


def reset_alert_runtime() -> None:
    """清空惰性缓存（建表标志/配置单例）——测试切换 env 用。

    引擎/会话工厂属 ai 切片，由 ``reset_ai_runtime`` 复位（勿在此重复清，
    避免与 ai 切片测试的复位次序耦合）。
    """
    global _initialized
    _initialized = False
    get_settings.cache_clear()
