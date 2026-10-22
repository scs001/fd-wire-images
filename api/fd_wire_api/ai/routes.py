"""AI 对话构建 API 路由（wire-platform-v1 任务 5.4）。

客户侧（Bearer ID token → logto_sub → Customer，wire-self-serve-launch 4.5
身份切换，同 demands 切片；X-Customer-Id 占位头退位不再接受）：
- ``POST /api/v1/ai/conversations``               新开对话（可带首条消息即跑循环）
- ``GET  /api/v1/ai/conversations``               本企业对话列表
- ``GET  /api/v1/ai/conversations/{id}/messages`` 会话消息（他企一律 404；
                                                  可选 after_id 增量取）
- ``POST /api/v1/ai/conversations/{id}/messages`` 追加用户消息并驱动构建循环
- ``GET  /api/v1/ai/conversations/{id}/artifacts``会话暂存的产物草稿（draft）
- ``POST /api/v1/ai/answer``                      问答轨一轮（无 cell 依赖）

两轨（wire-assistant-panel）：``/answer`` 是问答轨——不取 tool_executor 依赖，
结构上不执行工具、不写产物，免费档可用；``/messages`` 是构建轨——依赖 cell，
引擎未就绪时如实说明。

预算护栏：新开对话受每客户每日限额（429）；单轮循环受 AI_MAX_TURNS 截断。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import service
from .actions import (
    ActionNotRegistered,
    ActorContext,
    ApprovalDenied,
    ComplianceMissing,
    GateSessions,
    PreflightError,
    approve as gate_approve,
    get_proposal as gate_get_proposal,
    list_proposals as gate_list_proposals,
    propose as gate_propose,
    reject as gate_reject,
    set_conversation_id,
)
from .db import get_db
from ..console.auth import require_console_actor, require_console_customer_id
from ..datasets.db import get_db as get_datasets_db
from ..demands.db import get_db as get_demands_db
from ..entities.db import get_db as get_entities_db
from ..entities.service import entity_graph_prompt_block
from ..materialization.db import get_db as get_materialization_db
from ..packs.db import get_db as get_packs_db
from .executor import CellMcpExecutor
from .llm import TokenGatewayLlmClient
from .protocols import LlmClient, ToolExecutor
from .reads import ReadToolExecutor
from .schemas import (
    ActionApproveIn,
    ActionApproveOut,
    ActionOut,
    ActionProposeIn,
    ActionProposeOut,
    AssistantAnswerOut,
    AssistantAskIn,
    ChatIn,
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    DraftArtifactOut,
    MessageOut,
    action_out,
    artifact_out,
    conversation_detail,
)
from .settings import get_settings

router = APIRouter(tags=["ai"])


def get_llm_client() -> LlmClient:
    """LLM 出口依赖：token 网关 OpenAI 兼容面（测试覆写注入 mock）。"""
    return TokenGatewayLlmClient()


def get_tool_executor(customer_id: str = Depends(require_console_customer_id)) -> ToolExecutor:
    """工具执行出口依赖：该企业 cell 的 MCP 端点（测试覆写注入 mock）。

    计量归属：平台服务凭据轨道——AI 探索不计费。
    """
    return CellMcpExecutor(customer_id)


def _release(client: object) -> None:
    if hasattr(client, "close"):
        client.close()


def _actor_context(
    actor: tuple[object, str, str] = Depends(require_console_actor),
) -> ActorContext:
    """require_console_actor → 动作闸 ActorContext（customer_id=活跃队命名空间）。"""
    customer, sub, role = actor
    return ActorContext(customer_id=str(customer.name), sub=sub, role=role)


def get_gate_sessions(
    ai_db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    demands_db: Session = Depends(get_demands_db),
    materialization_db: Session = Depends(get_materialization_db),
    packs_db: Session = Depends(get_packs_db),
) -> GateSessions:
    """闸执行器的跨切片会话束（各库依赖注入；会话生命周期归各依赖管）。"""
    return GateSessions(
        ai=ai_db,
        datasets=datasets_db,
        demands=demands_db,
        materialization=materialization_db,
        packs=packs_db,
    )


def get_read_executor(
    ai_db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    demands_db: Session = Depends(get_demands_db),
) -> ReadToolExecutor:
    """副驾读工具执行器（自有数据切片直调；测试可整体覆写本依赖）。"""
    return ReadToolExecutor(ai_db=ai_db, datasets_db=datasets_db, demands_db=demands_db)


def _compose_system_extra(
    db: Session,
    entities_db: Session,
    datasets_db: Session,
    *,
    customer_id: str,
    context: dict | None,
) -> str | None:
    """图谱块 + 页面上下文块的组合（wire-copilot-action-gate：上下文注入
    从 /answer 迁到统一循环）。

    零漂移纪律：上下文为空（或净化后为空）时返回值与纯图谱块**逐字节
    一致**；两块都有时以换行拼接（图谱块在前）。
    """
    # 惰性导入（answer → artifacts.service → artifacts.routes → 本模块的环）：
    # 与既有 answer 端点的函数内导入同款纪律。
    from .answer import assistant_context_block, sanitize_context

    graph = entity_graph_prompt_block(entities_db, datasets_db, customer_id)
    clean = sanitize_context(context) if context else {}
    context_block = (
        assistant_context_block(db, datasets_db, customer_id=customer_id, context=clean)
        if clean
        else None
    )
    if graph is None:
        return context_block
    if context_block is None:
        return graph
    return f"{graph}\n{context_block}"


def _conversation_or_404(db: Session, conversation_id: int, customer_id: str):
    try:
        return service.get_customer_conversation(
            db, conversation_id=conversation_id, customer_id=customer_id
        )
    except service.ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/v1/ai/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationDetailOut,
)
def create_conversation(
    payload: ConversationCreate,
    actor: ActorContext = Depends(_actor_context),
    db: Session = Depends(get_db),
    entities_db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
    demands_db: Session = Depends(get_demands_db),
    llm: LlmClient = Depends(get_llm_client),
    tool_executor: ToolExecutor = Depends(get_tool_executor),
    read_executor: ReadToolExecutor = Depends(get_read_executor),
    gate_sessions: GateSessions = Depends(get_gate_sessions),
) -> ConversationDetailOut:
    """新开对话（先过每日限额护栏）；带 message 即当场跑一轮构建循环。

    实体图谱注入（wire-entity-layer）：跑循环前经 entities（元数据库）+
    datasets 双库会话组装图谱紧凑块——空图谱得 None，system prompt 与
    固定文案逐字一致（零漂移）。"""
    customer_id = actor.customer_id
    settings = get_settings()
    try:
        service.ensure_daily_limit(db, customer_id, settings)
    except service.DailyConversationLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    conversation = service.create_conversation(db, customer_id=customer_id, payload=payload)
    if payload.message:
        set_conversation_id(conversation.id)
        service.chat(
            db,
            conversation,
            content=payload.message,
            llm=llm,
            tool_executor=tool_executor,
            settings=settings,
            system_extra=_compose_system_extra(
                db, entities_db, datasets_db,
                customer_id=customer_id, context=payload.context,
            ),
            read_executor=read_executor,
            actor=actor,
            gate_sessions=gate_sessions,
        )
    _release(llm)
    _release(tool_executor)
    messages = service.list_messages(db, conversation_id=conversation.id)
    return conversation_detail(conversation, messages)


@router.get("/api/v1/ai/conversations", response_model=list[ConversationOut])
def list_conversations(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[ConversationOut]:
    """本企业对话列表（按创建时间倒序）。"""
    conversations = service.list_conversations(db, customer_id=customer_id)
    return [ConversationOut.model_validate(c) for c in conversations]


@router.get(
    "/api/v1/ai/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
def list_messages(
    conversation_id: int,
    after_id: int | None = None,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[MessageOut]:
    """会话消息（仅本企业可见；他企或不存在一律 404）。

    ``after_id``（wire-assistant-panel）：只返回该 id 之后新增的消息——浮层在
    长耗时构建期间据此增量展示进展。缺省不给 = 全量（既有语义不变）。
    """
    _conversation_or_404(db, conversation_id, customer_id)
    messages = service.list_messages(
        db, conversation_id=conversation_id, after_id=after_id
    )
    return [MessageOut.model_validate(m) for m in messages]


@router.post(
    "/api/v1/ai/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
def append_and_run(
    conversation_id: int,
    payload: ChatIn,
    actor: ActorContext = Depends(_actor_context),
    db: Session = Depends(get_db),
    entities_db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
    demands_db: Session = Depends(get_demands_db),
    llm: LlmClient = Depends(get_llm_client),
    tool_executor: ToolExecutor = Depends(get_tool_executor),
    read_executor: ReadToolExecutor = Depends(get_read_executor),
    gate_sessions: GateSessions = Depends(get_gate_sessions),
) -> list[MessageOut]:
    """追加用户消息并驱动一轮构建循环（返回本轮落库的消息序列）。

    实体图谱注入同 create_conversation（空图谱 None = 零漂移）。
    """
    customer_id = actor.customer_id
    conversation = _conversation_or_404(db, conversation_id, customer_id)
    settings = get_settings()
    set_conversation_id(conversation.id)
    appended = service.chat(
        db,
        conversation,
        content=payload.content,
        llm=llm,
        tool_executor=tool_executor,
        settings=settings,
        system_extra=_compose_system_extra(
            db, entities_db, datasets_db,
            customer_id=customer_id, context=payload.context,
        ),
        read_executor=read_executor,
        actor=actor,
        gate_sessions=gate_sessions,
    )
    _release(llm)
    _release(tool_executor)
    return [MessageOut.model_validate(m) for m in appended]


@router.get(
    "/api/v1/ai/conversations/{conversation_id}/artifacts",
    response_model=list[DraftArtifactOut],
)
def list_artifacts(
    conversation_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[DraftArtifactOut]:
    """会话暂存的产物草稿（draft；物化到 cell 是后续任务的事）。"""
    _conversation_or_404(db, conversation_id, customer_id)
    artifacts = service.list_artifacts(db, conversation_id=conversation_id)
    return [artifact_out(a) for a in artifacts]


@router.post("/api/v1/ai/answer", response_model=AssistantAnswerOut)
def answer(
    payload: AssistantAskIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    llm: LlmClient = Depends(get_llm_client),
) -> AssistantAnswerOut:
    """问答轨一轮（wire-assistant-panel）：不依赖数据处理引擎。

    与 ``append_and_run`` 的根本差别是**不取 tool_executor 依赖**——本端点
    结构上就不可能执行工具或写产物草稿，因此免费档（无 cell）同样可用。
    这直接落实 spec ``对话后端 SHALL 保留且按能力分轨`` 的问答轨场景：
    无 cell 客户调用它 MUST 拿到自然语言回答，而不是「引擎未开通」。

    ``conversation_id`` 给了就追加到该会话（浮层的单活会话，D7），缺省新开
    ——新开受既有的每客户每日限额护栏（与构建轨共用同一计数）。
    """
    settings = get_settings()
    if payload.conversation_id is None:
        try:
            service.ensure_daily_limit(db, customer_id, settings)
        except service.DailyConversationLimitExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        conversation = service.create_conversation(
            db,
            customer_id=customer_id,
            payload=ConversationCreate(message=None),
        )
    else:
        conversation = _conversation_or_404(db, payload.conversation_id, customer_id)

    from .answer import assistant_context_block, sanitize_context

    context_block = assistant_context_block(
        db,
        datasets_db,
        customer_id=customer_id,
        context=sanitize_context(payload.context),
    )
    try:
        appended = service.ask(
            db,
            conversation,
            content=payload.content,
            llm=llm,
            system_extra=context_block,
        )
    except Exception as exc:  # noqa: BLE001 - 失败如实：不伪造回答
        raise HTTPException(
            status_code=502,
            detail=f"AI 服务调用失败，本轮问答未产生回复。原因：{exc}",
        ) from exc
    finally:
        _release(llm)
    return AssistantAnswerOut(
        conversation_id=conversation.id,
        messages=[MessageOut.model_validate(m) for m in appended],
    )


# ── 动作闸端点（wire-copilot-action-gate design D4）──────────────────────────
#
# 提议/确认/审批/查询——写操作唯一的对外入口。角色与状态裁决在闸内
# （actions.approve / reject），路由只做异常→HTTP 映射：
# 未注册 404、preflight/合规 422、角色 403、状态/过期 409、他企 404。


def _gate_error(exc: Exception) -> HTTPException:
    """闸异常 → HTTPException（如实文案透传，绝不吞成 500）。"""
    if isinstance(exc, ActionNotRegistered):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PreflightError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ComplianceMissing):
        return HTTPException(
            status_code=422, detail="合规声明未完成：" + "、".join(exc.missing)
        )
    if isinstance(exc, ApprovalDenied):
        return HTTPException(
            status_code=409 if exc.kind == "state" else 403, detail=str(exc)
        )
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=f"动作闸内部错误：{exc}")


@router.post(
    "/api/v1/ai/actions/propose",
    status_code=status.HTTP_201_CREATED,
    response_model=ActionProposeOut,
)
def propose_action_endpoint(
    payload: ActionProposeIn,
    actor: ActorContext = Depends(_actor_context),
    db: Session = Depends(get_db),
    gate_sessions: GateSessions = Depends(get_gate_sessions),
) -> ActionProposeOut:
    """提议一个写操作（面板直发入口；副驾循环内部同经 ``actions.propose``）。

    未注册/预检失败不落 pending（4xx 如实文案）——「板上没有的动作，
    副驾也不提供」的闸面收口。
    """
    _conversation_or_404(db, payload.conversation_id, actor.customer_id)
    set_conversation_id(payload.conversation_id)
    try:
        proposal, missing = gate_propose(
            db,
            gate_sessions,
            actor,
            action_id=payload.action_id,
            args=payload.args,
        )
    except (ActionNotRegistered, PreflightError, ComplianceMissing, ApprovalDenied, LookupError) as exc:
        raise _gate_error(exc) from exc
    return ActionProposeOut(
        proposal_id=proposal.id,
        action_id=proposal.action_id,
        state=proposal.state,
        tier=proposal.tier,
        impact=proposal.impact_text,
        missing_fields=missing,
    )


@router.post(
    "/api/v1/ai/actions/{proposal_id}/approve",
    response_model=ActionApproveOut,
)
def approve_action_endpoint(
    proposal_id: int,
    payload: ActionApproveIn | None = None,
    actor: ActorContext = Depends(_actor_context),
    db: Session = Depends(get_db),
    gate_sessions: GateSessions = Depends(get_gate_sessions),
) -> ActionApproveOut:
    """确认（inline/second 档）或审批（async 档）并执行——单一执行路径+审计。

    合规字段（需求单类）只认本载荷：``compliance.source_url`` 与
    ``compliance.public_claim``（模型提议参数里的取值已在 propose 丢弃）。
    """
    try:
        compliance = (
            payload.compliance.model_dump() if payload and payload.compliance else None
        )
        outcome = gate_approve(
            db, gate_sessions, actor, proposal_id, compliance=compliance
        )
    except (ActionNotRegistered, PreflightError, ComplianceMissing, ApprovalDenied, LookupError) as exc:
        raise _gate_error(exc) from exc
    proposal = gate_get_proposal(db, actor, proposal_id)
    return ActionApproveOut(
        proposal_id=proposal_id,
        action_id=proposal.action_id,
        state=outcome["state"],
        result_summary=outcome.get("result_summary"),
        error=outcome.get("error"),
    )


@router.post("/api/v1/ai/actions/{proposal_id}/reject", response_model=ActionOut)
def reject_action_endpoint(
    proposal_id: int,
    actor: ActorContext = Depends(_actor_context),
    db: Session = Depends(get_db),
) -> ActionOut:
    """拒绝提议（任意 pending 态；本队成员即可，不执行）。"""
    try:
        proposal = gate_reject(db, actor, proposal_id)
    except (ApprovalDenied, LookupError) as exc:
        raise _gate_error(exc) from exc
    return action_out(proposal)


@router.get("/api/v1/ai/actions/{proposal_id}", response_model=ActionOut)
def get_action_endpoint(
    proposal_id: int,
    actor: ActorContext = Depends(_actor_context),
    db: Session = Depends(get_db),
) -> ActionOut:
    """提议详情（含惰性过期：读时发现超时即置 expired，fail-closed）。"""
    try:
        proposal = gate_get_proposal(db, actor, proposal_id)
    except LookupError as exc:
        raise _gate_error(exc) from exc
    return action_out(proposal)


@router.get("/api/v1/ai/actions", response_model=list[ActionOut])
def list_actions_endpoint(
    conversation_id: int,
    actor: ActorContext = Depends(_actor_context),
    db: Session = Depends(get_db),
) -> list[ActionOut]:
    """会话内提议列表（面板历史回放与草稿区的数据源）。"""
    _conversation_or_404(db, conversation_id, actor.customer_id)
    return [
        action_out(row)
        for row in gate_list_proposals(
            db, conversation_id=conversation_id, customer_id=actor.customer_id
        )
    ]
