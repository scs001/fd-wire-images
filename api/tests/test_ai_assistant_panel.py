"""问答轨与增量取消息测试（wire-assistant-panel）。

覆盖：免费档（无 cell）可问答、问答轨结构上不带工具、上下文净化与归属丢弃、
上下文不含数据行、LLM 失败如实、单活会话不重复计限额、after_id 增量语义。
"""

from __future__ import annotations

import json

import os

os.environ.setdefault("AI_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.answer import SYSTEM_PROMPT, sanitize_context
from fd_wire_api.ai.db import AiBase, get_db, reset_ai_runtime
from fd_wire_api.ai.protocols import LlmAssistantMessage
from fd_wire_api.ai.routes import get_llm_client, get_tool_executor
from fd_wire_api.app import app

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_FREE = "sub-free-001"
SUB_PAID = "sub-paid-001"
FREE = bearer(mint_id_token(SUB_FREE))
PAID = bearer(mint_id_token(SUB_PAID))


class FakeLlm:
    def __init__(self) -> None:
        self.script: list = []
        self.calls: list = []

    def complete(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeExecutor:
    """构建轨替身：问答轨绝不该碰它，这里记录调用以便断言「零调用」。"""

    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, tool_name, args):
        self.calls.append((tool_name, args))
        return {"ok": True}


def text_reply(text: str) -> LlmAssistantMessage:
    return LlmAssistantMessage(content=text)


@pytest.fixture()
def llm():
    return FakeLlm()


@pytest.fixture()
def executor():
    return FakeExecutor()


@pytest.fixture()
def client(monkeypatch, tmp_path, llm, executor):
    """两客户：免费档（无 cell）与付费档（有 cell）。

    ``AI_CELL_ENDPOINT_MAP`` 只配付费档——免费档因 map 已配且未命中而
    ``resolve_cell_endpoint`` 返回 None，正是「无引擎」的真实形态。
    """
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-paid=http://cell-paid/mcp")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AiBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    reset_ai_runtime()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_tool_executor] = lambda: executor
    from fd_wire_api.packs.db import get_db as get_packs_db
    app.dependency_overrides[get_packs_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_FREE, "cust-free"), (SUB_PAID, "cust-paid")],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


# ---------------------------------------------------------------- 上下文净化


def test_sanitize_context_drops_unknown_keys() -> None:
    """白名单之外的一切键丢弃——不透传客户端任意载荷。"""
    clean = sanitize_context(
        {
            "tab": "datasets",
            "artifact_id": 7,
            "dataset_id": "12",
            "customer_id": "cust-other",
            "__proto__": {"x": 1},
        }
    )
    assert clean == {"tab": "datasets", "artifact_id": 7, "dataset_id": 12}


def test_sanitize_context_rejects_bad_shapes() -> None:
    """非 dict / 非法值一律空上下文（不抛错——上下文是锦上添花）。"""
    assert sanitize_context(None) == {}
    assert sanitize_context("tab=datasets") == {}
    assert sanitize_context({"artifact_id": 0}) == {}
    assert sanitize_context({"artifact_id": True}) == {}
    assert sanitize_context({"artifact_id": -3}) == {}
    assert sanitize_context({"tab": "   "}) == {}


# ------------------------------------------------------------------ 免费档问答


def test_free_tier_can_ask(client, llm, executor) -> None:
    """免费档（无 cell）问答成功：拿到自然语言回答，不是引擎未开通。"""
    llm.script = [text_reply("「我的数据」页可以上传表格或接连接器，看字段与质量规则。")]
    resp = client.post(
        "/api/v1/ai/answer",
        json={"content": "我的数据页能做什么？"},
        headers=FREE,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conversation_id"] > 0
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    reply = body["messages"][1]["content"]
    assert "我的数据" in reply
    # 核心断言：绝不能被「引擎未开通」类文案替代
    assert "尚未开通数据 cell" not in reply
    assert "引擎未开通" not in reply


def test_answer_track_passes_no_tools(client, llm, executor) -> None:
    """问答轨结构上无工具可调：tools 恒为空，且从不碰 ToolExecutor。"""
    llm.script = [text_reply("去「产物」页可以查看已生成的看板。")]
    resp = client.post(
        "/api/v1/ai/answer", json={"content": "产物在哪里看？"}, headers=FREE
    )
    assert resp.status_code == 200, resp.text
    assert llm.calls[0]["tools"] == []
    assert executor.calls == []


def test_answer_uses_assistant_system_prompt(client, llm) -> None:
    """system prompt 是问答轨文案，且无上下文时逐字节一致（零漂移）。"""
    llm.script = [text_reply("在「用量」页查看调用量。")]
    client.post("/api/v1/ai/answer", json={"content": "用量在哪"}, headers=FREE)
    messages = llm.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}


# ------------------------------------------------------------------ 上下文注入


def test_context_block_injected_for_current_tab(client, llm) -> None:
    """给了 tab 上下文：system 行追加当前页说明，客户的问题落在此页上。"""
    llm.script = [text_reply("这个页可以上传数据。")]
    client.post(
        "/api/v1/ai/answer",
        json={"content": "这页能干嘛", "context": {"tab": "datasets"}},
        headers=FREE,
    )
    system = llm.calls[0]["messages"][0]["content"]
    assert system.startswith(SYSTEM_PROMPT)
    assert "我的数据" in system
    assert "【当前页面上下文】" in system


def test_context_never_contains_data_rows(client, llm) -> None:
    """上下文只含标识与元数据：提示里不出现任何数据行内容。"""
    llm.script = [text_reply("好的。")]
    client.post(
        "/api/v1/ai/answer",
        json={
            "content": "这页能干嘛",
            "context": {"tab": "artifacts", "artifact_id": 999, "dataset_id": 888},
        },
        headers=FREE,
    )
    system = llm.calls[0]["messages"][0]["content"]
    # 不存在的对象被静默丢弃——提示里除了页签说明不应出现产物/数据集描述
    assert "客户正在查看产物" not in system
    assert "客户正在查看数据集" not in system


# ---------------------------------------------------------------- 单活会话与限额


def test_multi_turn_reuses_conversation(client, llm) -> None:
    """多轮问答追加到同一会话：只创建一次，日限额计数只加一。"""
    llm.script = [text_reply("第一次回答。"), text_reply("第二次回答。")]
    first = client.post(
        "/api/v1/ai/answer", json={"content": "问一"}, headers=FREE
    ).json()
    second = client.post(
        "/api/v1/ai/answer",
        json={"content": "问二", "conversation_id": first["conversation_id"]},
        headers=FREE,
    ).json()
    assert second["conversation_id"] == first["conversation_id"]

    conversations = client.get("/api/v1/ai/conversations", headers=FREE).json()
    assert len(conversations) == 1

    # 第二轮的 wire 历史含第一轮的两条消息（上下文连续性）
    second_call_messages = llm.calls[1]["messages"]
    contents = [m["content"] for m in second_call_messages if m["role"] != "system"]
    assert contents[:3] == ["问一", "第一次回答。", "问二"]


def test_ask_rejects_other_tenant_conversation(client, llm) -> None:
    """他企会话一律 404（不泄露存在性）。"""
    llm.script = [text_reply("回答。")]
    mine = client.post(
        "/api/v1/ai/answer", json={"content": "问"}, headers=FREE
    ).json()
    resp = client.post(
        "/api/v1/ai/answer",
        json={"content": "问", "conversation_id": mine["conversation_id"]},
        headers=PAID,
    )
    assert resp.status_code == 404


def test_ask_requires_auth(client, llm) -> None:
    """未鉴权 401。"""
    resp = client.post("/api/v1/ai/answer", json={"content": "问"})
    assert resp.status_code == 401


# ------------------------------------------------------------------ 失败如实


def test_llm_failure_reported_not_faked(client, llm) -> None:
    """LLM 不可达：502 如实报错，不伪造回答。"""
    llm.script = [RuntimeError("网关超时")]
    resp = client.post(
        "/api/v1/ai/answer", json={"content": "问"}, headers=FREE
    )
    assert resp.status_code == 502
    assert "AI 服务调用失败" in resp.json()["detail"]
    assert "网关超时" in resp.json()["detail"]


# ------------------------------------------------------------------ 增量取消息


def test_after_id_returns_only_new_messages(client, llm) -> None:
    """after_id 只返回其后新增的消息（构建进展可见性的基础）。"""
    llm.script = [text_reply("回答一。"), text_reply("回答二。")]
    first = client.post(
        "/api/v1/ai/answer", json={"content": "问一"}, headers=FREE
    ).json()
    conversation_id = first["conversation_id"]
    first_ids = [m["id"] for m in first["messages"]]
    assert len(first_ids) == 2

    client.post(
        "/api/v1/ai/answer",
        json={"content": "问二", "conversation_id": conversation_id},
        headers=FREE,
    )

    incremental = client.get(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        params={"after_id": first_ids[-1]},
        headers=FREE,
    ).json()
    assert [m["role"] for m in incremental] == ["user", "assistant"]
    assert all(m["id"] > first_ids[-1] for m in incremental)

    full = client.get(
        f"/api/v1/ai/conversations/{conversation_id}/messages", headers=FREE
    ).json()
    assert len(full) == 4


def test_after_id_absent_keeps_full_semantics(client, llm) -> None:
    """不给 after_id = 全量（既有语义零变化）。"""
    llm.script = [text_reply("回答。")]
    body = client.post(
        "/api/v1/ai/answer", json={"content": "问"}, headers=FREE
    ).json()
    full = client.get(
        f"/api/v1/ai/conversations/{body['conversation_id']}/messages", headers=FREE
    ).json()
    assert len(full) == len(body["messages"]) == 2


# ------------------------------------------------------------------ 构建轨不变


def test_build_class_proposal_refuses_free_tier_honestly(client, llm, executor) -> None:
    """免费档（wire-copilot-action-gate 合并后）：对话照常（不再整轮短路），
    构建类提议被 preflight 如实拦截（spec「免费档不见构建入口」的闸侧语义）。"""
    from fd_wire_api.ai.protocols import LlmAssistantMessage, ToolCall

    llm.script = [
        LlmAssistantMessage(content=None, tool_calls=[ToolCall(
            id="c1", name="propose_action",
            arguments={"action_id": "wire.artifact.draft.create",
                       "args": {"kind": "dashboard", "title": "t", "spec": {"panels": []}}},
        )]),
        LlmAssistantMessage(content="构建需要数据引擎，我已说明原因。"),
    ]
    conversation = client.post(
        "/api/v1/ai/conversations", json={"title": "t"}, headers=FREE
    ).json()
    resp = client.post(
        f"/api/v1/ai/conversations/{conversation['id']}/messages",
        json={"content": "帮我建个看板"},
        headers=FREE,
    )
    assert resp.status_code == 200, resp.text
    roles = [m["role"] for m in resp.json()]
    assert roles == ["user", "assistant", "tool_trace", "assistant"]
    trace = json.loads(resp.json()[2]["content"])
    assert trace["result"]["ok"] is False
    assert "数据处理引擎" in trace["result"]["error"]
    # 提议未落 pending（不进确认档位）；cell 零调用
    actions = client.get(
        f"/api/v1/ai/actions?conversation_id={conversation['id']}", headers=FREE
    )
    assert actions.json() == []
    assert executor.calls == []
