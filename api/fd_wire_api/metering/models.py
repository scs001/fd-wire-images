"""计量 ORM 模型：``metering_records``（计量记录）与 ``metering_anomalies``（计费异常）。

字段口径见 wire-platform-v1 / wire-facade-metering 规格：

- 幂等键 ``request_id + server_name``（同一审计事件重放不双记）；
- ``rows_returned`` 为计费唯一依据，缺失的事件不进本表，落 anomalies；
- anomalies 同样以 ``request_id + server_name + reason`` 幂等。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import MeteringBase, utc_now


class MeteringRecord(MeteringBase):
    """一条工具调用的计量记录（由一条 MCPServerAccessRecord 规范化而来）。"""

    __tablename__ = "metering_records"
    __table_args__ = (
        UniqueConstraint("request_id", "server_name", name="uq_metering_request_server"),
        Index("ix_metering_records_track_time", "credential_track", "event_timestamp"),
        Index("ix_metering_records_caller_time", "caller", "event_timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 审计事件时间（registry 侧 timestamp，aware UTC）
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 幂等键之一：registry 请求标识
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 调用者身份（identity.username）
    caller: Mapped[str] = mapped_column(String(255), nullable=False, default="anonymous")
    #: 可读 principal（upn/email），可缺
    caller_principal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 凭据轨道：platform|customer
    credential_track: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 目标 server（mcp_server.name）
    server_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 目标 server 路由（mcp_server.path），可缺
    server_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 工具名（mcp_request.tool_name，非 tools/call 可缺）
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 计费行数唯一依据
    rows_returned: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 来源标记：yizuo|wanxing|platform-internal|customer
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 平台侧入库时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_timestamp": self.event_timestamp.isoformat(),
            "request_id": self.request_id,
            "caller": self.caller,
            "caller_principal": self.caller_principal,
            "credential_track": self.credential_track,
            "server_name": self.server_name,
            "server_path": self.server_path,
            "tool_name": self.tool_name,
            "rows_returned": self.rows_returned,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class MeteringAnomaly(MeteringBase):
    """计费异常留痕：缺 rows_returned（或不可解析）的审计事件，不静默。"""

    __tablename__ = "metering_anomalies"
    __table_args__ = (
        UniqueConstraint("request_id", "server_name", "reason", name="uq_anomaly_request_reason"),
        Index("ix_metering_anomalies_time", "event_timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 异常分类：missing_rows_returned | unparseable_event
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    server_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    caller: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credential_track: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 结构化细节（如 mcp_response.status / error_code），JSON 文本
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "request_id": self.request_id,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "caller": self.caller,
            "credential_track": self.credential_track,
            "detail": self.detail,
            "event_timestamp": self.event_timestamp.isoformat() if self.event_timestamp else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = ["MeteringAnomaly", "MeteringRecord", "utc_now"]
