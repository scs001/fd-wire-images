"""知验台本体拉取切片（add-bench-ontology-pull-client）。

wire 作为知验台发布本体的消费者（第一个，非唯一）：拉取客户端、本地快照
落库（幂等）、只读投影查询面、可选定时拉取。松耦合铁律的消费端兑现——
拉到、存好、可查，升级自主。

模块：

- ``client``：``pull_ontology``（三态错误映射 + 凭据脱敏）；
- ``store``：快照两表模型 + 幂等落库/拉取日志/快照读 + 拉取编排收口；
- ``projection``：只读投影（版本/实体/关系 + 关系身份重建）；
- ``routes``：内部端点（X-Internal-Token，沿 demands 先例）；
- ``scheduler``：可选定时拉取（BENCH_PULL_ENABLED 缺省 false）。

表落元数据库（生产迁移 alembic 0052）；平台级数据，刻意不挂客户租户。
"""

from __future__ import annotations

from . import client, projection, routes, scheduler, store
from .store import (
    BenchOntologySnapshot,
    BenchPullBase,
    BenchPullLog,
    bench_pull_database_url,
    get_db,
    get_engine,
    get_latest_snapshot,
    get_snapshot,
    init_db,
    list_snapshots,
    log_pull,
    pull_and_store,
    reset_bench_pull_runtime,
    save_snapshot,
)

__all__ = [
    "BenchOntologySnapshot",
    "BenchPullBase",
    "BenchPullLog",
    "bench_pull_database_url",
    "client",
    "get_db",
    "get_engine",
    "get_latest_snapshot",
    "get_snapshot",
    "init_db",
    "list_snapshots",
    "log_pull",
    "projection",
    "pull_and_store",
    "reset_bench_pull_runtime",
    "routes",
    "save_snapshot",
    "scheduler",
    "store",
]
