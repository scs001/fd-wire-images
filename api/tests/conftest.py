"""全局测试默认值（离线纪律）。

测试一律不联网、不落盘：
- ``XINGYAN_SYNC_ENABLED=0``：星研摄取循环在 lifespan 里默认启动，测试环境
  不应触发对公网官网的拉取（需要测循环的用例自行 monkeypatch 打开）。
- ``XINGYAN_AUTO_CREATE_TABLES=1``：内存/临时库直接建表，不依赖 alembic。
"""

from __future__ import annotations

import os

os.environ.setdefault("XINGYAN_SYNC_ENABLED", "0")
os.environ.setdefault("XINGYAN_AUTO_CREATE_TABLES", "1")
