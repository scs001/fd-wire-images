"""产物生命周期 API 的请求/响应模型（pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Artifact, ArtifactVersion


class ArtifactOut(BaseModel):
    """产物概要（列表用；含当前 spec 供摘要呈现）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: str
    kind: str
    title: str
    status: str
    version: int
    origin_conversation_id: int | None
    created_at: datetime
    updated_at: datetime


class ArtifactDetailOut(ArtifactOut):
    """产物详情（当前 spec + 状态迁移操作记录）。"""

    spec: Any
    status_history: list[dict[str, Any]] = []


class ArtifactVersionOut(BaseModel):
    """产物版本条目（历史列表用；单查含 spec）。"""

    model_config = ConfigDict(from_attributes=True)

    version: int
    operator: str | None
    created_at: datetime


class ArtifactVersionDetailOut(ArtifactVersionOut):
    """单个历史版本（含该版本 spec 快照）。"""

    spec: Any


class ArtifactUpdateIn(BaseModel):
    """spec 更新（追加新版本）：spec 为工具入参原样 JSON（含 title）。"""

    spec: dict[str, Any] = Field(min_length=1)


class ArtifactFromDraftIn(BaseModel):
    """会话草稿导入：conversation_id + 草稿 id → 建 draft 产物。"""

    conversation_id: int
    draft_id: int


class ArtifactCreateIn(BaseModel):
    """空白/带内容建板入参（空白=wire-dashboard-blank-create）。

    kind/title 必填；白名单与非空校验在 service 层统一裁决（空 title、
    非白名单 kind 一律 422 带原因）——模型只保字段在位与类型。

    ``spec`` 可选（add-artifact-create-endpoint）：携带时一步创建「带内容
    draft 产物」，走与 spec 更新相同的 kind 分派校验；仅限平台自营发布方
    （路由层 403）。缺省 None = 既有空白创建，行为逐字节不变。
    """

    kind: str
    title: str
    spec: dict[str, Any] | None = None


class PanelRefreshResult(BaseModel):
    """契约刷新的单面板结果（面板级失败隔离的如实呈现）。"""

    panel_id: str
    #: refreshed=快照已更新；kept=无合法 query 保留原快照；failed=保留原快照带原因
    status: Literal["refreshed", "kept", "failed"]
    error: str | None = None


class ArtifactRefreshOut(BaseModel):
    """手动刷新结果（wire-dashboard-refresh / wire-dashboard-query-contract）。

    mode：契约路径（≥1 面板带合法 query，零 LLM 确定性执行）="contract"，
    旧 AI 无头轮 ="ai"；panels 仅契约路径携带（AI 路径 None，向后兼容）。
    """

    version: int
    refreshed_at: str
    mode: Literal["contract", "ai"] | None = None
    panels: list[PanelRefreshResult] | None = None


class QueryPreviewOut(BaseModel):
    """契约试取结果（wire-panel-query-binding）：试取快照 + 归一后 query。

    snapshot 与契约刷新写回面板的快照同构（``{columns, rows}``）——试取与
    刷新走同一执行路径，试取所见即刷新所得。
    """

    snapshot: dict[str, Any]
    query: dict[str, Any]


def artifact_detail(artifact: Artifact, spec: dict) -> ArtifactDetailOut:
    """ORM + 当前 spec → 详情视图（spec 来自版本表最新行）。"""
    return ArtifactDetailOut(
        id=artifact.id,
        customer_id=artifact.customer_id,
        kind=artifact.kind,
        title=artifact.title,
        status=artifact.status,
        version=artifact.version,
        origin_conversation_id=artifact.origin_conversation_id,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
        spec=spec,
        status_history=list(artifact.status_history or []),
    )


def version_detail(row: ArtifactVersion) -> ArtifactVersionDetailOut:
    """版本行 → 单版本视图（含 spec 快照）。"""
    return ArtifactVersionDetailOut(
        version=row.version,
        operator=row.operator,
        created_at=row.created_at,
        spec=row.spec,
    )
