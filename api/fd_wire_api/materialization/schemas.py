"""物化内部端点的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PromoteIn(BaseModel):
    """draft 转正入参：schedule 可空（NULL = 不调度，仅手动触发）。"""

    schedule: str | None = Field(
        default=None,
        description="五字段 cron（naive UTC 评估）；缺省/null = 不登记调度",
    )


class MaterializedArtifactOut(BaseModel):
    """转正产物 + 最近一次运行摘要（控制台/后台如实展示用）。"""

    id: int
    artifact_id: int
    customer_id: str
    kind: str
    title: str | None = None
    schedule: str | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_rows: int = 0
    last_run_detail: dict | None = None
    last_error: str | None = None
    enabled: bool = True
    created_at: datetime | None = None


def artifact_out(artifact) -> MaterializedArtifactOut:
    """ORM → 出参（title 从 spec 快照宽容提取，缺失如实为 None）。"""
    spec = artifact.spec if isinstance(artifact.spec, dict) else {}
    title = spec.get("title")
    detail: Any = artifact.last_run_detail
    if not isinstance(detail, dict):
        detail = None
    return MaterializedArtifactOut(
        id=artifact.id,
        artifact_id=artifact.artifact_id,
        customer_id=artifact.customer_id,
        kind=artifact.kind,
        title=title if isinstance(title, str) else None,
        schedule=artifact.schedule,
        last_run_at=artifact.last_run_at,
        last_status=artifact.last_status,
        last_rows=artifact.last_rows or 0,
        last_run_detail=detail,
        last_error=artifact.last_error,
        enabled=bool(artifact.enabled),
        created_at=artifact.created_at,
    )
