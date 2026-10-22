"""副驾循环核心测试（LLM/工具执行全 mock，直接跑 loop/service 层）。

wire-copilot-action-gate 语义：
- 工具面 = 读工具 + propose_action + cell 工具（引擎可用且执行器支持发现时）
- 写操作经闸提议：立即返回 pending，草稿**不在提议时**产生（确认后才执行）
- 五类产物 spec 工具不再由模型直调（结构性不可达）
- 失败如实：LLM/工具异常如实反馈、不留半成品
- 历史回灌：tool_trace 还原为 OpenAI wire 的 tool_calls+tool 消息对
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, reset_ai_runtime
from fd_wire_api.ai.executor import (
    CELL_NOT_PROVISIONED_MESSAGE,
    CellToolError,
)
from fd_wire_api.ai.llm import LlmGatewayError
from fd_wire_api.ai.loop import run_copilot_loop
from fd_wire_api.ai.models import AIActionProposal, AIDraftArtifact, AIMessage, MessageRole
from fd_wire_api.ai.protocols import LlmAssistantMessage, ToolCall
from fd_wire_api.ai.service import append_message, chat
from fd_wire_api.ai.settings import AiSettings


# ------------------------------------------------------------------ fakes


class FakeLlm:
    """脚本化 LlmClient：按序弹出结果，Exception 即抛。记录每次输入。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def complete(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeExecutor:
    """脚本化 ToolExecutor：按工具名给结果或抛异常。记录每次调用。"""

    def __init__(self, behavior: dict | None = None) -> None:
        self.behavior = behavior or {}
        self.calls: list[tuple[str, dict]] = []

    def execute(self, tool_name: str, args: dict) -> dict:
        self.calls.append((tool_name, args))
        action = self.behavior.get(tool_name, {"ok": True, "rows": 0})
        if isinstance(action, Exception):
            raise action
        return action


class FakeReadExecutor:
    """脚本化读工具执行器（记录调用，默认空结果）。"""

    def __init__(self, behavior: dict | None = None) -> None:
        self.behavior = behavior or {}
        self.calls: list[tuple[str, dict]] = []

    def execute(self, tool_name: str, args: dict, *, customer_id: str) -> dict:
        self.calls.append((tool_name, args))
        return self.behavior.get(tool_name, {"ok": True, "count": 0})


def tool_call(name: str, args: dict, call_id: str = "call-1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


def assistant_reply(text: str) -> LlmAssistantMessage:
    return LlmAssistantMessage(content=text)


def assistant_tool(*calls: ToolCall) -> LlmAssistantMessage:
    return LlmAssistantMessage(content=None, tool_calls=calls)


def propose_call(action_id: str, args: dict, call_id: str = "call-1") -> ToolCall:
    return tool_call("propose_action", {"action_id": action_id, "args": args}, call_id)


DRAFT_ARGS = {
    "kind": "dashboard",
    "title": "中美日人均 GDP 对比",
    "spec": {"indicators": ["IND-GDP-US", "IND-GDP-CN", "IND-GDP-JP"], "chart": "line"},
}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AiBase.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def cell_provisioned(monkeypatch):
    """默认给 cust-001 开通 cell 端点映射（引擎档位判定见各用例按需 monkeypatch）；
    循环层不再因未开通而整轮短路——那是 v1 构建循环的语义。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
    reset_ai_runtime()
    yield
    reset_ai_runtime()


def _conversation(db, customer_id="cust-001"):
    from fd_wire_api.ai.models import AIConversation

    conversation = AIConversation(customer_id=customer_id, title="GDP 分析")
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _messages(db, conversation_id: int) -> list[AIMessage]:
    return list(
        db.query(AIMessage)
        .filter(AIMessage.conversation_id == conversation_id)
        .order_by(AIMessage.id)
        .all()
    )


def _proposals(db, conversation_id: int) -> list[AIActionProposal]:
    return list(
        db.query(AIActionProposal)
        .filter(AIActionProposal.conversation_id == conversation_id)
        .order_by(AIActionProposal.id)
        .all()
    )


def _artifacts(db, conversation_id: int) -> list[AIDraftArtifact]:
    return list(
        db.query(AIDraftArtifact)
        .filter(AIDraftArtifact.conversation_id == conversation_id)
        .all()
    )


# ------------------------------------------------------------------ 正循环


def test_full_round_propose_pending_no_draft(db, monkeypatch) -> None:
    """完整一轮：用户 → propose_action → pending 回执（不冒充成功）→ 终答。

    草稿**不**在提议时产生（确认在回合之外）；产物 spec 工具不在工具面。
    """
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for",
        lambda **_: "cell",
    )
    from fd_wire_api.ai.actions import ActorContext, GateSessions

    conversation = _conversation(db)
    llm = FakeLlm([
        assistant_tool(propose_call("wire.artifact.draft.create", DRAFT_ARGS)),
        assistant_reply("已提议创建仪表盘草稿，请在确认卡上预览并确认。"),
    ])
    executor = FakeExecutor()
    reads = FakeReadExecutor()
    actor = ActorContext(customer_id="cust-001", sub="sub-001", role="owner")
    sessions = GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db)

    appended = chat(
        db,
        conversation,
        content="帮我建一个中美日人均 GDP 对比仪表盘",
        llm=llm,
        tool_executor=executor,
        settings=AiSettings(ai_max_turns=8),
        read_executor=reads,
        actor=actor,
        gate_sessions=sessions,
    )

    # 消息序列：user → assistant(空文本工具轮) → tool_trace → assistant(终答)
    assert [(m.role, type(m)) for m in _messages(db, conversation.id)] == [
        (MessageRole.user.value, AIMessage),
        (MessageRole.assistant.value, AIMessage),
        (MessageRole.tool_trace.value, AIMessage),
        (MessageRole.assistant.value, AIMessage),
    ]

    # tool_trace 留痕：propose_action，回执 pending_confirm（不是成功）
    trace = json.loads(appended[2].content)
    assert trace["tool_name"] == "propose_action"
    assert trace["result"]["ok"] is True
    assert trace["result"]["state"] == "pending_confirm"
    assert trace["result"]["tier"] == "inline"
    assert "中美日" in trace["result"]["impact"]

    # 提议落一行 pending；草稿表零行（确认在回合外）
    rows = _proposals(db, conversation.id)
    assert len(rows) == 1
    assert rows[0].action_id == "wire.artifact.draft.create"
    assert rows[0].state == "pending_confirm"
    assert _artifacts(db, conversation.id) == []

    # 工具面：读工具 + propose_action（FakeExecutor 无 list_tool_schemas
    # → 无 cell 工具；五类产物工具结构性缺席）
    from fd_wire_api.ai.actions import PROPOSE_ACTION_TOOL_SCHEMA
    from fd_wire_api.ai.reads import READ_TOOL_SCHEMAS

    assert llm.calls[0]["tools"] == [*READ_TOOL_SCHEMAS, PROPOSE_ACTION_TOOL_SCHEMA]
    names = {item["function"]["name"] for item in llm.calls[0]["tools"]}
    assert "dashboard_spec_create" not in names
    assert executor.calls == []

    # 回灌 wire：tool 消息带 pending 回执
    wire = llm.calls[1]["messages"]
    tool_msg = wire[3]
    assert tool_msg["role"] == "tool"
    assert json.loads(tool_msg["content"])["state"] == "pending_confirm"


def test_multi_tool_turn_cell_and_read_dispatch(db, monkeypatch) -> None:
    """一轮多工具：探索工具经 ToolExecutor 打到 cell、读工具走读执行器。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for",
        lambda **_: "cell",
    )
    conversation = _conversation(db)
    llm = FakeLlm([
        assistant_tool(
            tool_call("search_indicators", {"q": "人均GDP"}, call_id="c-1"),
            tool_call("dataset_list", {}, call_id="c-2"),
        ),
        assistant_reply("探索到 3 条指标；你名下暂无数据集。"),
    ])
    executor = FakeExecutor(behavior={"search_indicators": {"ok": True, "rows": 3}})
    reads = FakeReadExecutor(behavior={"dataset_list": {"ok": True, "count": 0}})

    chat(
        db,
        conversation,
        content="先搜人均GDP指标，再看看我的数据集",
        llm=llm,
        tool_executor=executor,
        settings=AiSettings(ai_max_turns=8),
        read_executor=reads,
    )

    trace1 = json.loads(_messages(db, conversation.id)[2].content)
    trace2 = json.loads(_messages(db, conversation.id)[3].content)
    assert trace1["result"] == {"ok": True, "rows": 3}
    assert trace2["result"] == {"ok": True, "count": 0}
    assert executor.calls == [("search_indicators", {"q": "人均GDP"})]
    assert reads.calls == [("dataset_list", {})]


def test_history_replay_rebuilds_wire_pairs(db, monkeypatch) -> None:
    """第二轮对话回灌历史：tool_trace 还原为 tool_calls+tool 消息对。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for",
        lambda **_: "cell",
    )
    from fd_wire_api.ai.actions import ActorContext, GateSessions

    conversation = _conversation(db)
    llm = FakeLlm([
        assistant_tool(propose_call("wire.artifact.draft.create", DRAFT_ARGS)),
        assistant_reply("第一轮完成。"),
        assistant_reply("第二轮也完成。"),
    ])
    settings = AiSettings(ai_max_turns=8)
    kwargs = dict(
        llm=llm,
        tool_executor=FakeExecutor(),
        settings=settings,
        read_executor=FakeReadExecutor(),
        actor=ActorContext(customer_id="cust-001", sub="sub-001", role="owner"),
        gate_sessions=GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db),
    )
    chat(db, conversation, content="第一轮", **kwargs)
    chat(db, conversation, content="第二轮", **kwargs)

    wire = llm.calls[-1]["messages"]
    # system → user1 → assistant(tool_calls) → tool → assistant(终答) → user2
    assert [m["role"] for m in wire] == [
        "system", "user", "assistant", "tool", "assistant", "user",
    ]
    replayed_call = wire[2]["tool_calls"][0]
    assert replayed_call["function"]["name"] == "propose_action"
    assert wire[3]["tool_call_id"] == "call-1"
    assert json.loads(wire[3]["content"])["state"] == "pending_confirm"


# ------------------------------------------------------------------ 护栏


def test_max_turns_truncation(db, monkeypatch) -> None:
    """轮次上限：LLM 一直提议 → 达上限如实截断，提议留存。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for",
        lambda **_: "cell",
    )
    from fd_wire_api.ai.actions import ActorContext, GateSessions

    conversation = _conversation(db)
    endless = assistant_tool(propose_call("wire.artifact.draft.create", DRAFT_ARGS))
    llm = FakeLlm([endless, endless, endless, endless])  # 远超上限

    appended = chat(
        db,
        conversation,
        content="一直建仪表盘",
        llm=llm,
        tool_executor=FakeExecutor(),
        settings=AiSettings(ai_max_turns=3),
        read_executor=FakeReadExecutor(),
        actor=ActorContext(customer_id="cust-001", sub="sub-001", role="owner"),
        gate_sessions=GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db),
    )

    assert len(llm.calls) == 3
    assert "AI_MAX_TURNS=3" in appended[-1].content
    # 每轮提议都留存（不静默丢弃）
    assert len(_proposals(db, conversation.id)) == 3
    assert _artifacts(db, conversation.id) == []


def test_cell_tool_unprovisioned_midcall_constant_reply(db) -> None:
    """约定派生口径（map 未配）：cell 工具调用抛未开通 → 常量回复、安全中止。

    v1 的「未开通即整轮短路」已退役：免费档/未开通客户照常对话（读+提议），
    只有真的去调 cell 工具时才发现未开通。
    """
    from fd_wire_api.ai.executor import CellNotProvisionedError

    conversation = _conversation(db, customer_id="cust-405")
    llm = FakeLlm([assistant_tool(tool_call("search_indicators", {"q": "GDP"}))])
    executor = FakeExecutor(
        behavior={
            "search_indicators": CellNotProvisionedError(
                "customer cust-405 的 cell 未开通（握手期端点拒绝: initialize 返回 404）"
            )
        }
    )

    appended = chat(
        db,
        conversation,
        content="查 GDP 指标",
        llm=llm,
        tool_executor=executor,
        settings=AiSettings(ai_max_turns=8),
    )

    assert appended[0].role == MessageRole.user.value
    assert appended[-1].content == CELL_NOT_PROVISIONED_MESSAGE
    assert len(llm.calls) == 1  # 首轮 LLM 真的被调（派生口径下无法预知）
    assert len(executor.calls) == 1


def test_map_miss_customer_still_converses(db, monkeypatch) -> None:
    """显式部署口径（map 已配未命中）：不整轮短路——cell 工具不进工具面，
    读+提议照常（wire-assistant-panel 问/做合并场景的循环侧落点）。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-1/mcp")
    reset_ai_runtime()
    conversation = _conversation(db, customer_id="cust-404")
    llm = FakeLlm([assistant_reply("你的问题我答：去「我的数据」页。")])
    executor = FakeExecutor()

    appended = chat(
        db,
        conversation,
        content="这个数据集能画什么图",
        llm=llm,
        tool_executor=executor,
        settings=AiSettings(ai_max_turns=8),
    )

    assert appended[-1].content == "你的问题我答：去「我的数据」页。"
    assert llm.calls[0]["tools"]  # 照常拿到读+提议工具面
    assert executor.calls == []
    reset_ai_runtime()


# ------------------------------------------------------------------ 失败如实


def test_llm_failure_honest_no_artifacts(db) -> None:
    """LLM 异常：assistant 如实报错并安全中止，不留半成品。"""
    conversation = _conversation(db)
    llm = FakeLlm([LlmGatewayError("token 网关不可达: 连接超时")])
    executor = FakeExecutor()

    appended = chat(
        db,
        conversation,
        content="建个仪表盘",
        llm=llm,
        tool_executor=executor,
        settings=AiSettings(ai_max_turns=8),
    )

    assert [m.role for m in appended] == [
        MessageRole.user.value,
        MessageRole.assistant.value,
    ]
    assert "AI 服务调用失败" in appended[1].content
    assert "token 网关不可达" in appended[1].content
    assert len(llm.calls) == 1
    assert _artifacts(db, conversation.id) == []


def test_tool_failure_fed_back_and_reported(db) -> None:
    """工具异常：错误结果回灌 LLM，终答如实说明，不落产物。"""
    conversation = _conversation(db)
    llm = FakeLlm([
        assistant_tool(tool_call("search_indicators", {"q": "销量"}, call_id="c-1")),
        assistant_reply("抱歉，探索工具暂时不可用（cell MCP 端点不可达），本轮没能建出仪表盘。"),
    ])
    executor = FakeExecutor(
        behavior={"search_indicators": CellToolError("cell MCP 端点不可达: connection refused")}
    )

    appended = chat(
        db,
        conversation,
        content="查销量指标并建仪表盘",
        llm=llm,
        tool_executor=executor,
        settings=AiSettings(ai_max_turns=8),
    )

    messages = _messages(db, conversation.id)
    trace_search = json.loads(messages[2].content)
    assert trace_search["result"]["ok"] is False
    assert trace_search["result"]["error_type"] == "CellToolError"
    assert "不可达" in trace_search["result"]["error"]
    assert "不可用" in appended[-1].content
    assert _artifacts(db, conversation.id) == []


def test_invalid_draft_args_rejected_at_propose(db, monkeypatch) -> None:
    """草稿提议入参不合法：preflight 拒绝并回灌错误（提议表/草稿表都不多一行）。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for",
        lambda **_: "cell",
    )
    from fd_wire_api.ai.actions import ActorContext, GateSessions

    conversation = _conversation(db)
    bad_args = {"kind": "dashboard", "title": "缺 spec 的半成品", "spec": {}}
    llm = FakeLlm([
        assistant_tool(propose_call("wire.artifact.draft.create", bad_args)),
        assistant_reply("上一次提议缺 spec 对象，已如实放弃，未产生提议。"),
    ])

    chat(
        db,
        conversation,
        content="建个仪表盘",
        llm=llm,
        tool_executor=FakeExecutor(),
        settings=AiSettings(ai_max_turns=8),
        read_executor=FakeReadExecutor(),
        actor=ActorContext(customer_id="cust-001", sub="sub-001", role="owner"),
        gate_sessions=GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db),
    )

    trace = json.loads(_messages(db, conversation.id)[2].content)
    assert trace["result"]["ok"] is False
    assert "spec" in trace["result"]["error"]
    assert _proposals(db, conversation.id) == []
    assert _artifacts(db, conversation.id) == []


def test_unregistered_action_rejected(db, monkeypatch) -> None:
    """未注册动作：闸拒绝、不落提议、不调用底层（spec「未注册动作被拒绝」）。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for",
        lambda **_: "cell",
    )
    from fd_wire_api.ai.actions import ActorContext, GateSessions

    conversation = _conversation(db)
    llm = FakeLlm([
        assistant_tool(propose_call("wire.dataset.delete", {"dataset_id": "ds-1"})),
        assistant_reply("删除数据集不在动作面内——请到「我的数据」页操作。"),
    ])

    chat(
        db,
        conversation,
        content="删掉这个数据集",
        llm=llm,
        tool_executor=FakeExecutor(),
        settings=AiSettings(ai_max_turns=8),
        read_executor=FakeReadExecutor(),
        actor=ActorContext(customer_id="cust-001", sub="sub-001", role="owner"),
        gate_sessions=GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db),
    )

    trace = json.loads(_messages(db, conversation.id)[2].content)
    assert trace["result"]["ok"] is False
    assert "未注册" in trace["result"]["error"]
    assert _proposals(db, conversation.id) == []


def test_propose_without_deps_degrades_honestly(db) -> None:
    """旧调用方（无 actor/gate_sessions）：提议通道如实降级，对话不断。"""
    conversation = _conversation(db)
    llm = FakeLlm([
        assistant_tool(propose_call("wire.artifact.publish", {"artifact_id": 1})),
        assistant_reply("提议通道当前不可用，请在产物页手动操作。"),
    ])

    appended = chat(
        db,
        conversation,
        content="发布它",
        llm=llm,
        tool_executor=FakeExecutor(),
        settings=AiSettings(ai_max_turns=8),
    )

    trace = json.loads(_messages(db, conversation.id)[2].content)
    assert trace["result"]["ok"] is False
    assert "提议通道未就绪" in trace["result"]["error"]
    assert appended[-1].content == "提议通道当前不可用，请在产物页手动操作。"


# ------------------------------------------------------------------ run_copilot_loop 直测


def test_run_copilot_loop_plain_round(db) -> None:
    conversation = _conversation(db)
    append_message(
        db, conversation_id=conversation.id, role=MessageRole.user, content="hi"
    )
    llm = FakeLlm([assistant_reply("done")])
    appended = run_copilot_loop(
        db, conversation, llm=llm, tool_executor=FakeExecutor(), max_turns=8
    )
    assert [m.role for m in appended] == [MessageRole.assistant.value]
    assert appended[0].content == "done"
