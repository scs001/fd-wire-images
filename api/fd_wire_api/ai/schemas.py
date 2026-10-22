"""AI 对话 API 的请求/响应模型（pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import AIDraftArtifact, AIMessage, AIActionProposal, AIConversation


class ConversationCreate(BaseModel):
    """新开对话（可携带首条用户消息——给即当场驱动一轮副驾循环）。"""

    title: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, min_length=1)
    #: 页面上下文（ChatIn.context 同款；见 ChatIn 注释）
    context: dict[str, Any] | None = None


class ChatIn(BaseModel):
    """追加一条用户消息并驱动副驾循环。

    ``context``（wire-assistant-panel 既有语义在统一循环上的延续）：当前
    页面上下文（``tab`` / ``artifact_id`` / ``dataset_id``），服务端按
    ``answer.sanitize_context`` 白名单净化后注入 system 提示——只含标识与
    元数据，绝不带数据行。
    """

    content: str = Field(min_length=1)
    context: dict[str, Any] | None = None


class AssistantAskIn(BaseModel):
    """问答轨一轮提问（wire-assistant-panel）。

    - ``content``：客户问题。
    - ``conversation_id``：可选——给则追加到该会话（浮层的单活会话），
      缺省则新开一个会话；新开会计入每日限额。
    - ``context``：当前页面上下文（``tab`` / ``artifact_id`` / ``dataset_id``）；
      服务端按白名单净化，未知键丢弃。
    """

    content: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None
    context: dict[str, Any] | None = None


class AssistantAnswerOut(BaseModel):
    """问答轨一轮结果：``conversation_id`` 供浮层记住单活会话。"""

    conversation_id: int
    messages: list["MessageOut"]


class MessageOut(BaseModel):
    """对话消息（tool_trace 的 content 为 JSON 留痕串）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    created_at: datetime


class ConversationOut(BaseModel):
    """对话概要（列表用）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: str
    title: str
    created_at: datetime


class ConversationDetailOut(ConversationOut):
    """对话详情（创建后带全部消息返回）。"""

    messages: list[MessageOut] = []


class DraftArtifactOut(BaseModel):
    """产物暂存草稿（spec 为工具入参原样 JSON）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    customer_id: str
    kind: str
    spec: Any
    status: str
    created_at: datetime


def conversation_detail(
    conversation: AIConversation, messages: list[AIMessage]
) -> ConversationDetailOut:
    """ORM → 详情视图。"""
    return ConversationDetailOut(
        id=conversation.id,
        customer_id=conversation.customer_id,
        title=conversation.title,
        created_at=conversation.created_at,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


def artifact_out(artifact: AIDraftArtifact) -> DraftArtifactOut:
    return DraftArtifactOut.model_validate(artifact)


# ── 动作闸（wire-copilot-action-gate design D4）──────────────────────────────


class ActionProposeIn(BaseModel):
    """提议入参（副驾循环内部与面板直发共用一份契约）。"""

    action_id: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    conversation_id: int


class ActionComplianceIn(BaseModel):
    """需求单类动作的客户合规输入（确认卡上手填；模型给的取值服务端不采纳）。"""

    source_url: str = Field(default="", max_length=2000)
    public_claim: bool = False


class ActionApproveIn(BaseModel):
    """确认/审批入参。"""

    compliance: ActionComplianceIn | None = None


class ActionProposeOut(BaseModel):
    """提议回执：pending 不冒充 executed（spec「回执不冒充结果」）。"""

    proposal_id: int
    action_id: str
    state: str
    tier: str
    impact: str
    missing_fields: list[str] = []


class ActionApproveOut(BaseModel):
    """确认/审批回执：executed 与 failed 如实分态。"""

    proposal_id: int
    action_id: str
    state: str
    result_summary: str | None = None
    error: str | None = None


class ActionOut(BaseModel):
    """提议详情/列表行（审计留痕指向经 result_summary 与 error 呈现）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    action_id: str
    state: str
    risk: str
    tier: str
    impact: str
    args_summary: str
    proposed_by: str
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    result_summary: str | None = None
    created_at: datetime


def action_out(proposal: AIActionProposal) -> ActionOut:
    """ORM → 视图（impact_text 列 → impact 字段的显式映射）。"""
    return ActionOut(
        id=proposal.id,
        conversation_id=proposal.conversation_id,
        action_id=proposal.action_id,
        state=proposal.state,
        risk=proposal.risk,
        tier=proposal.tier,
        impact=proposal.impact_text,
        args_summary=proposal.args_summary,
        proposed_by=proposal.proposed_by,
        approved_by=proposal.approved_by,
        approved_at=proposal.approved_at,
        expires_at=proposal.expires_at,
        result_summary=proposal.result_summary or None,
        created_at=proposal.created_at,
    )
