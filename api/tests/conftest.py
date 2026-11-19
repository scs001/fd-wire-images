"""全局测试默认值（离线纪律）。

测试一律不联网、不落盘：
- ``XINGYAN_SYNC_ENABLED=0``：星研摄取循环在 lifespan 里默认启动，测试环境
  不应触发对公网官网的拉取（需要测循环的用例自行 monkeypatch 打开）。
- ``XINGYAN_AUTO_CREATE_TABLES=1``：内存/临时库直接建表，不依赖 alembic。
- ``ONTOLOGY_SNAPSHOT_ENABLED=0``：本体版本日快照循环同样在 lifespan 启动；
  测试关掉后台循环，需要测日快照的用例直接驱动 ``snapshots.tick_once``
  （注入确定时钟）。
- ``FD_WIRE_INTERNAL_TOKEN``：内部端点令牌。生产由 Secret 注入；源码里刻意没有
  缺省值（漏配必须 503，不能降级成公开口令），所以测试必须自己给一个。
  它只是一个测试值，不作为任何部署的缺省。
"""

from __future__ import annotations

import os

os.environ.setdefault("XINGYAN_SYNC_ENABLED", "0")
os.environ.setdefault("XINGYAN_AUTO_CREATE_TABLES", "1")
os.environ.setdefault("ONTOLOGY_SNAPSHOT_ENABLED", "0")
os.environ.setdefault("FD_WIRE_INTERNAL_TOKEN", "test-internal-token")
