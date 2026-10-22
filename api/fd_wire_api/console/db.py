"""控制台域数据库基础设施（复用平台库引擎，admin 切片同源）。

console_* 表与 billing_* 同库（平台库）：首登开户 ceremony 要在同一个
数据库事务面上联写 ``billing_customers.logto_sub`` 与断点簿记行。
连接串与引擎完全复用 ``admin.db``（FD_WIRE_ADMIN_DATABASE_URL >
DATABASE_URL > 本地开发默认 sqlite）；本模块只管理自己的建表兜底
（生产走 alembic 0015_console_onboarding）。
"""

from __future__ import annotations

import threading

from ..admin.db import get_engine, reset_admin_runtime


def reset_console_runtime() -> None:
    """清空控制台域建表标记（引擎缓存归 admin 域管理，一并重置）。"""
    reset_admin_runtime()
    global _initialized
    _initialized = False


_init_lock = threading.Lock()
_initialized = False


def init_db() -> None:
    """幂等建 console_* 表（开发兜底；生产走 alembic 0015）。"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        from . import models  # noqa: F401 —— 注册映射后再 create_all

        from .models import ConsoleBase

        ConsoleBase.metadata.create_all(bind=get_engine())
        _initialized = True
