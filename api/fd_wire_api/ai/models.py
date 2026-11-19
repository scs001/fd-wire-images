"""AI 对话构建 ORM 模型（三表，Alembic 0005）。

- ``ai_conversations``：对话会话（每客户每日新建数受限额护栏约束）。
- ``ai_messages``：对话消息（user / assistant / tool_trace 三角色，
  append-only——tool_trace 记录每次工具调用的名字、入参与结果）。
- ``ai_draft_artifacts``：产物暂存（五类产物 spec 的 draft 落点；
  真正物化到企业 cell 是任务 5.3/5.5 的活）。
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import AiBase


def utc_now_naive() -> datetime:
    """统一存 naive UTC（与 demands/metering 切片同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MessageRole(str, enum.Enum):
    """消息角色：用户输入 / AI 回复 / 工具调用留痕。"""

    user = "user"
    assistant = "assistant"
    tool_trace = "tool_trace"


#: 产物暂存的合法状态（v1 只有 draft；物化流转在 5.3/5.5）
ARTIFACT_STATUS_DRAFT = "draft"


class AIConversation(AiBase):
    """一次 AI 对话会话（归属企业，按 customer_id 隔离）。"""

    __tablename__ = "ai_conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 归属企业（客户侧按此隔离列表与消息）
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<AIConversation {self.id} customer={self.customer_id}>"


class AIMessage(AiBase):
    """对话消息：user / assistant / tool_trace 三角色，只追加不改写。"""

    __tablename__ = "ai_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 副驾会话上下文（域动作提议无会话——0043 起可空；副驾通道恒有值）
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_conversations.id"), index=True, nullable=True
    )
    #: MessageRole 值
    role: Mapped[str] = mapped_column(String(16))
    #: user/assistant 为自然语言文本；tool_trace 为 JSON 留痕
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<AIMessage {self.id} role={self.role}>"


class AIDraftArtifact(AiBase):
    """产物暂存：构建循环里五类产物工具的产出落这里（draft）。

    spec 为工具入参原样 JSON（含 title）；kind 取五类产物短名
    （dashboard / composite_indicator / research_report / alert_rule /
    refresh_schedule）。真正落 cell 由任务 5.3/5.5 物化。
    """

    __tablename__ = "ai_draft_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("ai_conversations.id"), index=True
    )
    #: 归属企业（冗余存储：会话删除后仍可按企业审计暂存产物）
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 五类产物短名（见 tools.ARTIFACT_TOOL_KINDS）
    kind: Mapped[str] = mapped_column(String(64), index=True)
    #: 产物规格 JSON（v1 假语义：工具入参原样暂存）
    spec: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(16), default=ARTIFACT_STATUS_DRAFT, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<AIDraftArtifact {self.id} kind={self.kind} status={self.status}>"


#: 动作提议的合法状态（wire-copilot-action-gate design D4）：
#: pending_confirm = write/destructive/cost 档待客户行内确认；
#: pending_approval = publish 档待本队 owner/editor 异步审批（24h 过期）；
#: 终态 executed/rejected/expired/failed 只落一次（append-only 纪律同 corrections）。
PROPOSAL_STATE_PENDING_CONFIRM = "pending_confirm"
PROPOSAL_STATE_PENDING_APPROVAL = "pending_approval"
PROPOSAL_STATE_EXECUTED = "executed"
PROPOSAL_STATE_REJECTED = "rejected"
PROPOSAL_STATE_EXPIRED = "expired"
PROPOSAL_STATE_FAILED = "failed"

#: 异步审批的有效期（小时）——超时置 expired，fail-closed 绝不自动执行
PROPOSAL_APPROVAL_TTL_HOURS = 24


class AIActionProposal(AiBase):
    """动作闸提议（wire-copilot-action-gate）：副驾的每一次写操作提议。

    副驾循环的工具面里没有写工具——写只能以本表一行的形态「提议」，由
    客户确认（inline/second 档）或本队 owner/editor 审批（async 档）后经
    :mod:`.actions` 的单一执行路径执行。参数只存脱敏摘要与指纹（全文
    spec/长文本不落本表——草稿正本在 ai_draft_artifacts）。
    """

    __tablename__ = "ai_action_proposals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 副驾会话上下文（域动作提议无会话——0043 起可空；副驾通道恒有值）
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_conversations.id"), index=True, nullable=True
    )
    #: 归属企业 = 活跃队命名空间（require_console_actor 解析所得）
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 注册表动作 id（如 wire.artifact.publish）
    action_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 提议参数的 sha256 指纹（重试幂等判定与审计比对用；非凭据）
    args_digest: Mapped[str] = mapped_column(String(64))
    #: 脱敏参数摘要（标量字段级；不含 spec 全文/凭据/长文本）
    args_summary: Mapped[str] = mapped_column(Text)
    #: 风险等级（write/destructive/cost/publish）与确认档位（inline/second/async）
    risk: Mapped[str] = mapped_column(String(16))
    tier: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24), index=True)
    #: 按提议参数生成的影响面文案（确认卡展示用）
    impact_text: Mapped[str] = mapped_column(Text)
    #: 提议者（logto_sub）
    proposed_by: Mapped[str] = mapped_column(String(128))
    #: 批准者（确认/审批者 logto_sub；未批准为空）
    approved_by: Mapped[str] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    #: async 档的审批到期时刻（其余档为空）
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, index=True)
    #: 执行结果摘要（executed 后由执行体回填——分享链接等客户要回看的事实；
    #: 失败原因走审计表，不双写）
    result_summary: Mapped[str] = mapped_column(Text, nullable=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<AIActionProposal {self.id} {self.action_id} {self.state}>"


class AIActionAudit(AiBase):
    """动作闸审计台账（append-only）：每次执行一行，成败都记。

    与 corrections 体系的分工：闸审计记**对话侧**（谁提议、谁批、结果）；
    数据修正自身的留痕（改了哪行、可撤销）仍在 corrections——两层并存
    不重复。绝不写凭据明文。
    """

    __tablename__ = "ai_action_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("ai_action_proposals.id"), index=True
    )
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    action_id: Mapped[str] = mapped_column(String(64), index=True)
    #: ok / error
    result: Mapped[str] = mapped_column(String(8))
    #: 失败原因（如实；不含凭据）
    error_text: Mapped[str] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
