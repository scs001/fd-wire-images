"""控制台面（wire-self-serve-launch 4.2/4.3/6.1）：登录配置下发、首登自动
开户、客户自助用量。身份 = Bearer ID token 本地校验 → logto_sub → Customer
（auth.py）；X-Customer-Id 占位头退位。
"""

from __future__ import annotations

from .db import init_db, reset_console_runtime
from .routes import router

__all__ = ["init_db", "reset_console_runtime", "router"]
