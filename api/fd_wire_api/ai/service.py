"""对话会话业务逻辑：建档、限额护栏、消息查询与对话编排。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .loop import append_message, run_copilot_loop
from .models import AIMessage, AIConversation, MessageRole
from .protocols import LlmClient, ToolExecutor
from .schemas import ConversationCreate
from .settings import AiSettings


class ConversationNotFound(LookupError):
    """对话不存在（或不属于该客户）——路由层转 404（不泄露他企存在性）。"""


class DailyConversationLimitExceeded(Exception):
    """每客户每日新开对话数超限——路由层转 429 带说明。"""

    def __init__(self, customer_id: str, used: int, limit: int) -> None:
        self.customer_id = customer_id
        self.used = used
        self.limit = limit
        super().__init__(
            f"企业 {customer_id} 今日已新开 {used} 个对话，"
            f"达到每日上限（AI_DAILY_CONVERSATION_LIMIT={limit}）。"
            "请明日再试或联系平台调整限额。"
        )


def _utc_day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def count_today_conversations(db: Session, customer_id: str) -> int:
    """该企业今日（UTC 日）已新开对话数——复用 conversation 表按日 count。"""
    return int(
        db.scalar(
            select(func.count(AIConversation.id)).where(
                AIConversation.customer_id == customer_id,
                AIConversation.created_at >= _utc_day_start(),
            )
        )
        or 0
    )


def ensure_daily_limit(db: Session, customer_id: str, settings: AiSettings) -> None:
    """护栏三：每客户每日对话数限额（超限抛异常 → 429）。"""
    used = count_today_conversations(db, customer_id)
    if used >= settings.ai_daily_conversation_limit:
        raise DailyConversationLimitExceeded(
            customer_id, used, settings.ai_daily_conversation_limit
        )


def create_conversation(
    db: Session, *, customer_id: str, payload: ConversationCreate
) -> AIConversation:
    """新开对话（调用方须先过日限额护栏）。"""
    conversation = AIConversation(
        customer_id=customer_id,
        title=(payload.title or "新对话").strip() or "新对话",
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def list_conversations(db: Session, *, customer_id: str) -> list[AIConversation]:
    """本企业对话列表（按创建时间倒序）。"""
    stmt = (
        select(AIConversation)
        .where(AIConversation.customer_id == customer_id)
        .order_by(AIConversation.id.desc())
    )
    return list(db.scalars(stmt).all())


def get_customer_conversation(
    db: Session, *, conversation_id: int, customer_id: str
) -> AIConversation:
    """客户侧会话：不存在或非本企业一律 404。"""
    conversation = db.get(AIConversation, conversation_id)
    if conversation is None or conversation.customer_id != customer_id:
        raise ConversationNotFound(f"conversation {conversation_id} 不存在")
    return conversation


def list_messages(
    db: Session, *, conversation_id: int, after_id: int | None = None
) -> list[AIMessage]:
    """会话消息（append-only 顺序）。

    ``after_id``（wire-assistant-panel）：只取该消息 id 之后的新增行——供浮层
    在长耗时构建期间增量展示进展（循环每追加一条就 commit，天然可增量读）。
    缺省 None = 全量，语义与既有调用逐字节一致。
    """
    stmt = select(AIMessage).where(AIMessage.conversation_id == conversation_id)
    if after_id is not None:
        stmt = stmt.where(AIMessage.id > after_id)
    stmt = stmt.order_by(AIMessage.id)
    return list(db.scalars(stmt).all())


def list_artifacts(db: Session, *, conversation_id: int) -> list:
    """会话暂存的产物草稿（draft）。"""
    from .models import AIDraftArtifact

    stmt = (
        select(AIDraftArtifact)
        .where(AIDraftArtifact.conversation_id == conversation_id)
        .order_by(AIDraftArtifact.id)
    )
    return list(db.scalars(stmt).all())


def chat(
    db: Session,
    conversation: AIConversation,
    *,
    content: str,
    llm: LlmClient,
    tool_executor: ToolExecutor,
    settings: AiSettings,
    system_extra: str | None = None,
    read_executor=None,
    actor=None,
    gate_sessions=None,
) -> list[AIMessage]:
    """一条用户消息驱动一轮副驾循环（用户消息 + 循环产出全部落库）。

    ``system_extra``：实体图谱块（wire-entity-layer）——空图谱传 None，
    system prompt 与固定文案逐字一致（零漂移）。
    ``read_executor`` / ``actor`` / ``gate_sessions``：读工具与提议通道的
    依赖束（wire-copilot-action-gate）；缺席时对应能力如实降级，对话继续。
    """
    user_message = append_message(
        db, conversation_id=conversation.id, role=MessageRole.user, content=content
    )
    loop_messages = run_copilot_loop(
        db,
        conversation,
        llm=llm,
        tool_executor=tool_executor,
        max_turns=settings.ai_max_turns,
        system_extra=system_extra,
        read_executor=read_executor,
        actor=actor,
        gate_sessions=gate_sessions,
    )
    return [user_message, *loop_messages]


def ask(
    db: Session,
    conversation: AIConversation,
    *,
    content: str,
    llm: LlmClient,
    system_extra: str | None = None,
) -> list[AIMessage]:
    """一条用户消息走问答轨（wire-assistant-panel）：无工具、无 cell 依赖。

    与 :func:`chat` 的关键差别：**不传 tool_executor**——问答轨结构上就没有
    执行工具的能力（见 ``answer.answer_question`` 的 ``tools=[]``）。因此免费档
    客户（无 cell）也能用它。

    LLM 失败不吞：异常上抛给路由层转成如实失败响应，且**不留下 assistant 半
    成品**——用户消息已落库（客户可重试同一句），失败事实由响应告知。
    """
    from .answer import answer_history, answer_question

    # 先取历史再落库本次提问：历史天然不含本轮，无需去重。
    history = answer_history(db, conversation.id)
    user_message = append_message(
        db, conversation_id=conversation.id, role=MessageRole.user, content=content
    )
    history.append({"role": "user", "content": content})
    assistant = answer_question(
        history=history, llm=llm, system_extra=system_extra
    )
    reply = append_message(
        db,
        conversation_id=conversation.id,
        role=MessageRole.assistant,
        content=assistant.text(),
    )
    return [user_message, reply]
