"""与 metering 模块的共享读取契约（耦合点，4.1 ↔ 4.2 接口）。

`metering_records` 表归 metering 模块所有（ORM：
``fd_wire_api.metering.models.MeteringRecord``，建表见 alembic 0001），
billing 只读。真实表的关键列与计费口径：

- ``caller``：调用者身份（identity.username）——结算聚合的「客户身份映射」键，
  billing 侧由 resolver 映射到 Customer（v1 暂用 sub2api_user_id 承载该映射，
  正式的 Logto username ↔ Customer 映射落地前可注入自定义 resolver）
- ``event_timestamp``：period（YYYY-MM）由其派生（双方言：SQLite strftime /
  PG to_char，经 ``dbcompat.period_expression``——审计清单 dialect-01）
- ``credential_track``：'customer' 才计费（platform=平台服务凭据/AI 探索
  归集，不进客户账单——spec wire-billing「AI 探索不计费」+ 8.2 归集不结算）
- ``rows_returned``：计费行数唯一依据

真实表没有批次（batch_ref）列：SqlMeteringSource 只支持整月聚合域
（batch_ref=None）。MeteringSource 协议保留 batch_ref 供增量/水位线实现
（StaticMeteringSource 等），批次切分收敛前勿对真实表按批结算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.engine import Connection, Engine

from ..dbcompat import period_expression

#: metering_records 的只读表定义（独立 MetaData：billing 侧 create_all/迁移不碰它；
#: 列类型与 metering.models.MeteringRecord 对齐——那侧改表须同步这里）
metering_records = Table(
    "metering_records",
    MetaData(),
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_timestamp", DateTime(timezone=True), nullable=False),
    Column("request_id", String(200), nullable=False),
    Column("caller", String(255), nullable=False, default="anonymous"),
    Column("caller_principal", String(255), nullable=True),
    Column("credential_track", String(16), nullable=False),
    Column("server_name", String(255), nullable=False),
    Column("server_path", String(512), nullable=True),
    Column("tool_name", String(255), nullable=True),
    Column("rows_returned", Integer, nullable=False),
    Column("source", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("request_id", "server_name", name="uq_metering_request_server"),
)

#: SQLite 专用建表 DDL（历史保留；PG/双方言路径一律用
#: ``ensure_metering_records_table``，见审计清单 dialect-02）
METERING_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS metering_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_timestamp TIMESTAMP NOT NULL,
    request_id VARCHAR(200) NOT NULL,
    caller VARCHAR(255) NOT NULL DEFAULT 'anonymous',
    caller_principal VARCHAR(255),
    credential_track VARCHAR(16) NOT NULL,
    server_name VARCHAR(255) NOT NULL,
    server_path VARCHAR(512),
    tool_name VARCHAR(255),
    rows_returned INTEGER NOT NULL,
    source VARCHAR(32) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_metering_request_server UNIQUE (request_id, server_name)
)
"""


def ensure_metering_records_table(bind: Engine | Connection) -> None:
    """双方言建同构 metering_records 表（测试/临时环境专用，幂等）。

    生产表归 metering 模块的 alembic 0001 建；这里经 SQLAlchemy Core
    ``Table.create(checkfirst=True)`` 在 SQLite 与 PG 下都能建出等价表
    （替换原 SQLite 专用的 METERING_RECORDS_DDL 裸 DDL）。
    """
    metering_records.create(bind=bind, checkfirst=True)

CUSTOMER_TRACK = "customer"
PLATFORM_TRACK = "platform"


@dataclass(frozen=True)
class MeteringUsage:
    """单客户在一个聚合域内的计费行数（customer_ref=caller 身份键）。"""

    customer_ref: str
    rows: int


class MeteringSource(Protocol):
    """结算的计量读取接口：按 (客户身份, period[, batch_ref]) 聚合计费行数。"""

    def aggregates(self, period: str, batch_ref: str | None = None) -> list[MeteringUsage]:
        ...


class StaticMeteringSource:
    """测试/演练用：直接给定聚合结果（batch_ref 语义由调用方自证）。"""

    def __init__(self, usages: list[MeteringUsage]) -> None:
        self._usages = usages

    def aggregates(self, period: str, batch_ref: str | None = None) -> list[MeteringUsage]:
        return list(self._usages)


class SqlMeteringSource:
    """从真实 metering_records 表读聚合：只客户凭据轨道、period 派生自时间戳。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def aggregates(self, period: str, batch_ref: str | None = None) -> list[MeteringUsage]:
        if batch_ref is not None:
            raise ValueError(
                "真实 metering_records 无批次列：请按整月聚合域结算（batch_ref=None）；"
                "增量批次需水位线机制，由主代理收敛"
            )
        stmt = (
            select(
                metering_records.c.caller.label("customer_ref"),
                func.sum(metering_records.c.rows_returned).label("rows"),
            )
            .where(
                metering_records.c.credential_track == CUSTOMER_TRACK,
                period_expression(self._engine, metering_records.c.event_timestamp)
                == period,
            )
            .group_by(metering_records.c.caller)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [MeteringUsage(customer_ref=r.customer_ref, rows=int(r.rows)) for r in rows]
