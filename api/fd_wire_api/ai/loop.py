"""副驾对话循环（wire-copilot-action-gate D1/D2；前身是 v1 构建循环）。

``用户消息 → LLM（读工具 + propose_action + cell 工具[有引擎时]）→ 工具
调用执行 → 结果回灌 → …… → LLM 终答或轮次上限``。

- 工具三分路：**读工具**（reads.py，直调自有数据切片，任意档位）；
  **propose_action**（actions.py 动作闸——写操作唯一的通道，立即返回
  pending，不在回合内等待确认）；**cell 工具**（executor.py，指标探索，
  仅引擎可用时动态注入工具面）。
- 不可绕过是结构性的：工具面组装处**只有**这三类 schema——五类产物
  spec 写入从模型直调改为经闸提议（v1 假语义退役），代码路径上不存在
  写工具可达的分支。
- 失败如实：LLM 异常 → assistant 消息如实报错并安全中止；工具异常 →
  错误结果回灌 LLM、tool_trace 留痕，绝不留下半成品。
- 预算护栏：轮次达上限 → 如实告知截断。免费档（无引擎）不再整轮短路
  ——读工具与提议面照常可用（wire-assistant-panel 问/做合并的落点）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .actions import (
    ActorContext,
    GateSessions,
    ActionNotRegistered,
    PreflightError,
    PROPOSE_ACTION_TOOL_NAME,
    PROPOSE_ACTION_TOOL_SCHEMA,
    propose,
    set_conversation_id,
)
from .executor import (
    CELL_NOT_PROVISIONED_MESSAGE,
    CellNotProvisionedError,
    resolve_cell_endpoint,
)
from .models import AIMessage, AIConversation, MessageRole
from .protocols import LlmAssistantMessage, LlmClient, ToolCall, ToolExecutor
from .reads import READ_TOOL_SCHEMAS, ReadToolExecutor, is_read_tool

SYSTEM_PROMPT = (
    "你是柏讯控制台的副驾。客户就当前页面提问或下达操作意图，你经读工具与"
    "提议通道帮他完成。纪律："
    "① 读工具直通——dataset_list / dataset_schema / artifact_list / "
    "artifact_get / demands_list 只读本团队资产；有引擎时另有指标探索工具。"
    "② 写操作只能经 propose_action 提议——你不能直接执行任何写操作；提议"
    "立即返回 pending，由客户在确认卡上确认后才执行。"
    "③ 回执如实转述——pending 不是成功，执行失败如实说明并保留重试。"
    "④ 需求单提议的公开源 URL 与公开数据声明绝不代填——那必须由客户在"
    "确认卡上自己填写。"
    "⑤ 意图不清先追问；数据不可得时如实说明，绝不编造数值、绝不产出"
    "占位符。"
    "⑥ 产物草稿提议要给完整 spec（把取到的真实数据写进快照），让客户在"
    "确认卡上直接看到成品预览。"
    "⑦ 完成后用中文给简短总结，指明结果落在哪里（会话草稿区/产物页/"
    "我的需求）。"
)

#: 轮次上限截断的如实文案
def _truncation_message(max_turns: int) -> str:
    return (
        f"本轮对话已达到最大轮次上限（AI_MAX_TURNS={max_turns}），"
        "对话被安全截断。已产生的提议与草稿保留；"
        "请开新对话或简化意图后继续。"
    )


def _llm_failure_message(exc: Exception) -> str:
    return f"AI 服务调用失败，本轮对话已安全中止，未产生半成品。原因：{exc}"


def append_message(
    db: Session, *, conversation_id: int, role: MessageRole, content: str
) -> AIMessage:
    """追加一条消息（append-only，绝不改写既有消息）。"""
    message = AIMessage(
        conversation_id=conversation_id, role=role.value, content=content
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def _assistant_wire(assistant: LlmAssistantMessage) -> dict:
    """归一化 assistant 消息 → OpenAI wire 形态（含 tool_calls）。"""
    entry: dict[str, Any] = {"role": "assistant", "content": assistant.text() or None}
    if assistant.has_tool_calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in assistant.tool_calls
        ]
    return entry


def _wire_history(
    db: Session, conversation_id: int, system_extra: str | None = None
) -> list[dict]:
    """历史消息 → OpenAI wire 形态：tool_trace 还原为 assistant.tool_calls +
    tool 结果对（满足兼容面对「tool 消息须紧跟带 tool_calls 的 assistant」
    的次序要求）。

    ``system_extra``（wire-entity-layer）：实体图谱紧凑块，非空时在固定
    文案后追加（换行分隔）；None/空 → system 文本与固定文案**逐字节一致**
    （零漂移，有测试断言）。组装方为 ai/routes（经
    ``entities.service.entity_graph_prompt_block`` 取块）。
    """
    rows = list(
        db.scalars(
            select(AIMessage)
            .where(AIMessage.conversation_id == conversation_id)
            .order_by(AIMessage.id)
        ).all()
    )
    system_content = SYSTEM_PROMPT
    if system_extra:
        system_content = f"{SYSTEM_PROMPT}\n{system_extra}"
    wire: list[dict] = [{"role": "system", "content": system_content}]
    i = 0
    while i < len(rows):
        row = rows[i]
        if row.role == MessageRole.user.value:
            wire.append({"role": "user", "content": row.content})
            i += 1
            continue
        if row.role == MessageRole.assistant.value:
            # 紧随其后的 tool_trace 归属该 assistant 轮（还原 tool_calls）。
            traces: list[AIMessage] = []
            j = i + 1
            while j < len(rows) and rows[j].role == MessageRole.tool_trace.value:
                traces.append(rows[j])
                j += 1
            entry: dict[str, Any] = {"role": "assistant", "content": row.content or None}
            if traces:
                calls = []
                for trace in traces:
                    payload = json.loads(trace.content)
                    calls.append(
                        {
                            "id": payload.get("tool_call_id") or f"call-{trace.id}",
                            "type": "function",
                            "function": {
                                "name": payload["tool_name"],
                                "arguments": json.dumps(
                                    payload.get("arguments") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
                entry["tool_calls"] = calls
            wire.append(entry)
            for trace in traces:
                payload = json.loads(trace.content)
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": payload.get("tool_call_id") or f"call-{trace.id}",
                        "content": json.dumps(
                            payload.get("result"), ensure_ascii=False
                        ),
                    }
                )
            i = j
            continue
        # 游离 tool_trace（不该出现）：按 tool 消息原样回放，不丢弃。
        payload = json.loads(row.content)
        wire.append(
            {
                "role": "tool",
                "tool_call_id": payload.get("tool_call_id") or f"call-{row.id}",
                "content": json.dumps(payload.get("result"), ensure_ascii=False),
            }
        )
        i += 1
    return wire


def execute_tool_call(
    db: Session,
    conversation: AIConversation,
    call: ToolCall,
    tool_executor: ToolExecutor,
    *,
    read_executor: ReadToolExecutor | None = None,
    actor: ActorContext | None = None,
    gate_sessions: GateSessions | None = None,
) -> dict:
    """执行一次工具调用并返回回灌结果（异常即错误结果；未开通例外穿透）。

    三分路：读工具 → ``ReadToolExecutor``（自有数据切片直调）；
    ``propose_action`` → 动作闸（立即返回 pending，不等待确认）；
    其余 → ``ToolExecutor``（cell MCP，平台服务凭据轨道：AI 探索不计费，
    计量在 registry audit 侧按平台身份归集）。
    ``CellNotProvisionedError`` 不在此吞——上抛给循环以常量回复收场
    （约定派生口径下「未开通」在首个探索调用的握手期才被发现）。
    """
    if call.name == PROPOSE_ACTION_TOOL_NAME:
        if actor is None or gate_sessions is None:
            return {
                "ok": False,
                "error": "提议通道未就绪（缺少身份或会话束），本轮不可提议。",
            }
        set_conversation_id(conversation.id)
        action_id = call.arguments.get("action_id")
        args = call.arguments.get("args")
        if not isinstance(action_id, str) or not isinstance(args, dict):
            return {
                "ok": False,
                "error": "propose_action 入参须为 {action_id: string, args: object}。",
            }
        try:
            proposal, missing = propose(
                db, gate_sessions, actor, action_id=action_id, args=args
            )
        except ActionNotRegistered as exc:
            return {"ok": False, "error": str(exc)}
        except PreflightError as exc:
            return {"ok": False, "error": f"提议未通过前置检查：{exc}"}
        except Exception as exc:  # noqa: BLE001 - 落库异常：回滚不留半行
            db.rollback()
            return {"ok": False, "error": f"提议落库失败: {exc}"}
        result: dict = {
            "ok": True,
            "proposal_id": proposal.id,
            "state": proposal.state,
            "tier": proposal.tier,
            "impact": proposal.impact_text,
            "note": "提议已提交，等待客户在确认卡上确认——如实转述状态，"
            "pending 不是成功。",
        }
        if missing:
            result["missing_fields"] = missing
        return result
    if is_read_tool(call.name) and read_executor is not None:
        return read_executor.execute(
            call.name, call.arguments, customer_id=conversation.customer_id
        )
    try:
        return tool_executor.execute(call.name, call.arguments)
    except CellNotProvisionedError:
        raise  # 未开通：穿透到循环层走常量回复
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def _engine_available(customer_id: str) -> bool:
    """cell 引擎可用性（工具面注入判定）：端点可解析且档位非免费（api 层）。

    与构建类动作 preflight 同一口径（datasets.storage_tier_for）——免费档
    的指标探索工具不进工具面，但读工具与提议面照常可用。
    """
    from ..datasets import service as datasets_service

    if resolve_cell_endpoint(customer_id) is None:
        return False
    return datasets_service.storage_tier_for(customer_id=customer_id) != datasets_service.TIER_API


def copilot_tool_schemas(tool_executor: ToolExecutor, *, customer_id: str) -> list[dict]:
    """组装本轮工具面：读工具 + propose_action + cell 工具（引擎可用时）。

    结构性不可绕过的实现点——这里**只有**这三类 schema，写工具没有可达
    分支（spec「写操作不进入副驾的可见工具面」）。
    """
    schemas = [*READ_TOOL_SCHEMAS, PROPOSE_ACTION_TOOL_SCHEMA]
    if _engine_available(customer_id):
        discover = getattr(tool_executor, "list_tool_schemas", None)
        if callable(discover):
            schemas.extend(discover())
    return schemas


def run_copilot_loop(
    db: Session,
    conversation: AIConversation,
    *,
    llm: LlmClient,
    tool_executor: ToolExecutor,
    max_turns: int,
    system_extra: str | None = None,
    read_executor: ReadToolExecutor | None = None,
    actor: ActorContext | None = None,
    gate_sessions: GateSessions | None = None,
) -> list[AIMessage]:
    """跑一轮副驾循环，返回本次追加的全部消息（含首条 user）。

    调用方须已把用户消息落库（``chat`` 一并处理）。``system_extra`` 见
    ``_wire_history``（图谱块注入；缺省 None = 固定文案零漂移）。
    ``actor`` / ``gate_sessions`` / ``read_executor`` 缺席时提议与读工具
    如实降级（不可用回灌），对话本身继续——兼容无这些依赖的旧调用方。
    """
    appended: list[AIMessage] = []

    def emit(role: MessageRole, content: str) -> AIMessage:
        message = append_message(
            db, conversation_id=conversation.id, role=role, content=content
        )
        appended.append(message)
        return message

    wire = _wire_history(db, conversation.id, system_extra)
    schemas = copilot_tool_schemas(tool_executor, customer_id=conversation.customer_id)
    for _ in range(max_turns):
        try:
            assistant = llm.complete(wire, schemas)
        except Exception as exc:
            # 失败如实：assistant 消息报错并安全中止（无终答、无半成品）。
            emit(MessageRole.assistant, _llm_failure_message(exc))
            return appended

        emit(MessageRole.assistant, assistant.text())
        if not assistant.has_tool_calls:
            return appended  # 终答

        wire.append(_assistant_wire(assistant))
        try:
            for call in assistant.tool_calls:
                result = execute_tool_call(
                    db,
                    conversation,
                    call,
                    tool_executor,
                    read_executor=read_executor,
                    actor=actor,
                    gate_sessions=gate_sessions,
                )
                emit(
                    MessageRole.tool_trace,
                    json.dumps(
                        {
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "arguments": call.arguments,
                            "result": result,
                        },
                        ensure_ascii=False,
                    ),
                )
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        except CellNotProvisionedError:
            # 约定派生口径的「未开通」：首个探索调用握手期 404/405——
            # 常量回复、安全中止（不把未开通当作可继续探索的普通错误回灌）。
            emit(MessageRole.assistant, CELL_NOT_PROVISIONED_MESSAGE)
            return appended

    # 护栏：轮次上限——如实告知截断（提议与草稿留存，不静默丢弃）。
    emit(MessageRole.assistant, _truncation_message(max_turns))
    return appended


#: 旧名兼容（v1 构建循环时代的外部引用；语义已并入副驾循环）
run_build_loop = run_copilot_loop
