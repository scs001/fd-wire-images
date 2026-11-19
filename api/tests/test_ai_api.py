"""AI 对话 API 测试（LLM/工具执行全 mock，经依赖覆写注入）。

覆盖：创建即跑一轮完整循环并落库、消息追加续聊、企业隔离（他企 404）、
每客户每日对话数限额 429 带说明、cell 未开通常量错误回复、轮次上限
经 env 生效、LLM 异常如实反馈不产半成品、鉴权 401。
"""

from __future__ import annotations

import json
import os

# 在导入 app 前把默认引擎指向内存库：TestClient 启动钩子里的
# init_ai_db() 不至于在测试机上落下 fd_wire_ai.db 文件。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db, reset_ai_runtime
from fd_wire_api.ai.llm import LlmGatewayError
from fd_wire_api.ai.protocols import LlmAssistantMessage, ToolCall
from fd_wire_api.ai.routes import get_llm_client, get_tool_executor
from fd_wire_api.app import app

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-cust-001"
SUB_OTHER = "sub-cust-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))


class FakeLlm:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list = []

    def complete(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeExecutor:
    def __init__(self, behavior: dict | None = None) -> None:
        self.behavior = behavior or {}
        self.calls: list = []

    def execute(self, tool_name, args):
        self.calls.append((tool_name, args))
        action = self.behavior.get(tool_name, {"ok": True, "rows": 0})
        if isinstance(action, Exception):
            raise action
        return action


def tool_reply(*calls: ToolCall) -> LlmAssistantMessage:
    return LlmAssistantMessage(content=None, tool_calls=calls)


def text_reply(text: str) -> LlmAssistantMessage:
    return LlmAssistantMessage(content=text)


DASHBOARD_ARGS = {
    "title": "中美日人均 GDP 对比",
    "spec": {"indicators": ["IND-GDP-US", "IND-GDP-CN", "IND-GDP-JP"], "chart": "line"},
}


@pytest.fixture()
def fake_llm():
    return FakeLlm([])


@pytest.fixture()
def fake_executor():
    return FakeExecutor()


@pytest.fixture()
def client(monkeypatch, tmp_path, fake_llm, fake_executor):
    """独立内存库 + 覆写 get_db/LLM/工具执行依赖的 TestClient。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
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
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    app.dependency_overrides[get_tool_executor] = lambda: fake_executor
    # 动作闸跨切片会话束（wire-copilot-action-gate）：本文件用例不触
    # demands/materialization 表，挂同一测试引擎即可（惰性连接，不建表）。
    from fd_wire_api.ai.routes import (
        get_gate_sessions,
        get_read_executor,
        _actor_context,
    )
    from fd_wire_api.demands.db import get_db as get_demands_db
    from fd_wire_api.materialization.db import get_db as get_materialization_db
    from fd_wire_api.packs.db import get_db as get_packs_db

    for dep in (get_demands_db, get_materialization_db, get_packs_db):
        app.dependency_overrides[dep] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path, monkeypatch,
        customers=[(SUB_CUST, "cust-001"), (SUB_OTHER, "cust-002")],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


def create(client, *, headers=CUST, **payload) -> dict:
    resp = client.post("/api/v1/ai/conversations", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------ 循环落库


def _propose(action_id: str, args: dict, call_id: str = "call-1") -> ToolCall:
    return ToolCall(id=call_id, name="propose_action",
                    arguments={"action_id": action_id, "args": args})


def test_create_with_message_runs_full_round(client, fake_llm, fake_executor, monkeypatch) -> None:
    """创建带首条消息：完整「用户→propose_action→pending→终答」序列落库并返回；
    确认后草稿才产生（提议-确认-执行 e2e，wire-copilot-action-gate）。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for", lambda **_: "cell"
    )
    fake_llm.script = [
        tool_reply(_propose("wire.artifact.draft.create", {
            "kind": "dashboard",
            **DASHBOARD_ARGS,
        })),
        text_reply("已提议创建仪表盘草稿，请在确认卡上预览并确认。"),
    ]
    body = create(client, title="GDP 分析", message="帮我建一个中美日人均 GDP 对比仪表盘")

    assert body["title"] == "GDP 分析"
    assert body["customer_id"] == "cust-001"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "tool_trace", "assistant"]
    assert body["messages"][0]["content"] == "帮我建一个中美日人均 GDP 对比仪表盘"

    trace = json.loads(body["messages"][2]["content"])
    assert trace["tool_name"] == "propose_action"
    assert trace["result"]["ok"] is True
    assert trace["result"]["state"] == "pending_confirm"
    assert "仪表盘草稿已生成" not in body["messages"][3]["content"]  # 回执不冒充成功
    proposal_id = trace["result"]["proposal_id"]

    # 提议列表端点：本会话一条 pending
    actions = client.get(
        f"/api/v1/ai/actions?conversation_id={body['id']}", headers=CUST
    )
    assert actions.status_code == 200
    assert [a["state"] for a in actions.json()] == ["pending_confirm"]
    assert actions.json()[0]["action_id"] == "wire.artifact.draft.create"

    # 确认前：草稿端点空（确认在回合之外）
    cid = body["id"]
    assert client.get(f"/api/v1/ai/conversations/{cid}/artifacts", headers=CUST).json() == []

    # 确认 → 执行（stage_artifact）→ 草稿可见（收口 G1）
    approved = client.post(
        f"/api/v1/ai/actions/{proposal_id}/approve", json={}, headers=CUST
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "executed"
    assert "草稿" in approved.json()["result_summary"]

    rows = client.get(f"/api/v1/ai/conversations/{cid}/artifacts", headers=CUST).json()
    assert len(rows) == 1
    assert rows[0]["kind"] == "dashboard"
    assert rows[0]["status"] == "draft"
    assert rows[0]["spec"] == DASHBOARD_ARGS

    # 消息端点按 id 可查（append-only 顺序）
    messages = client.get(f"/api/v1/ai/conversations/{cid}/messages", headers=CUST)
    assert messages.status_code == 200
    assert [m["role"] for m in messages.json()] == roles


def test_append_message_continues_conversation(client, fake_llm) -> None:
    """POST messages 续聊：追加用户消息并跑新一轮循环。"""
    fake_llm.script = [
        text_reply("好的，请把对比范围告诉我。"),
        tool_reply(ToolCall(id="c2", name="propose_action", arguments={
            "action_id": "wire.artifact.draft.create",
            "args": {"kind": "alert_rule", "title": "GDP 偏离告警",
                     "spec": {"indicator": "IND-GDP-CN", "op": ">", "threshold": 13000}},
        })),
        text_reply("告警规则草稿提议已提交，等待确认。"),
    ]
    created = create(client, message="我想监控 GDP")
    cid = created["id"]

    resp = client.post(
        f"/api/v1/ai/conversations/{cid}/messages",
        json={"content": "人均GDP超 13000 美元时提醒我"},
        headers=CUST,
    )
    assert resp.status_code == 200, resp.text
    appended = resp.json()
    assert [m["role"] for m in appended] == ["user", "assistant", "tool_trace", "assistant"]

    # 会话累计消息序列：第一轮终答 + 第二轮完整序列
    all_messages = client.get(f"/api/v1/ai/conversations/{cid}/messages", headers=CUST).json()
    assert [m["role"] for m in all_messages] == [
        "user", "assistant",
        "user", "assistant", "tool_trace", "assistant",
    ]


# ------------------------------------------------------------------ 护栏


def test_daily_conversation_limit_429(client, fake_llm, monkeypatch) -> None:
    """每客户每日对话数限额：超限 429 且说明文案带限额值。"""
    monkeypatch.setenv("AI_DAILY_CONVERSATION_LIMIT", "2")
    reset_ai_runtime()
    fake_llm.script = [text_reply("ok")] * 5

    assert client.post("/api/v1/ai/conversations", json={}, headers=CUST).status_code == 201
    assert client.post("/api/v1/ai/conversations", json={}, headers=CUST).status_code == 201
    third = client.post(
        "/api/v1/ai/conversations", json={"message": "再来一个"}, headers=CUST
    )
    assert third.status_code == 429
    assert "AI_DAILY_CONVERSATION_LIMIT=2" in third.json()["detail"]
    assert "每日上限" in third.json()["detail"]

    # 限额按客户隔离：另一企业不受影响
    assert client.post("/api/v1/ai/conversations", json={}, headers=OTHER).status_code == 201
    # 超限后对话未被创建（列表仍为 2 条）
    listing = client.get("/api/v1/ai/conversations", headers=CUST).json()
    assert len(listing) == 2


def test_max_turns_via_env(client, fake_llm, monkeypatch) -> None:
    """AI_MAX_TURNS 经 env 生效：达上限如实截断。"""
    monkeypatch.setenv("AI_MAX_TURNS", "1")
    reset_ai_runtime()
    call = ToolCall(id="c1", name="propose_action", arguments={
        "action_id": "wire.artifact.publish", "args": {"artifact_id": 1}
    })
    fake_llm.script = [tool_reply(call), text_reply("不该被调到")]

    body = create(client, message="建仪表盘")
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "tool_trace", "assistant"]
    assert "AI_MAX_TURNS=1" in body["messages"][3]["content"]
    assert len(fake_llm.calls) == 1


def test_cell_not_provisioned_constant_error(client, fake_llm, fake_executor, monkeypatch) -> None:
    """cell 未开通的两种形态都回常量错误，零 LLM 调用：
    ①显式部署口径：map 已配且未命中；②约定派生口径：握手期 404/405
    （CellNotProvisionedError 由 executor 包装）。"""
    from fd_wire_api.ai.executor import CellNotProvisionedError

    # ① map 已配、OTHER 企业未命中：不整轮短路——cell 工具不进工具面，
    # 对话照常（wire-copilot-action-gate：免费/未开通客户读+提议可用）
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
    reset_ai_runtime()
    fake_llm.script = [text_reply("你的问题我答：先到「我的数据」上传数据。")]
    body = create(client, headers=OTHER, message="帮我建个仪表盘")

    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert "先到" in body["messages"][1]["content"]
    assert len(fake_llm.calls) == 1
    tool_names = {
        item["function"]["name"] for item in fake_llm.calls[0]["tools"]
    }
    assert "propose_action" in tool_names  # 提议面照常
    assert "search_indicators" not in tool_names  # cell 工具不在面
    assert fake_executor.calls == []

    # ② map 未配（约定派生恒有端点）→ 派生端点握手期 404 → 未开通
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "")
    reset_ai_runtime()
    fake_llm.script = [tool_reply(ToolCall(id="c1", name="search_indicators", arguments={"q": "GDP"}))]
    fake_executor.behavior = {
        "search_indicators": CellNotProvisionedError(
            "customer cust-002 的 cell 未开通（握手期端点拒绝: initialize 返回 404）"
        )
    }
    body2 = create(client, headers=OTHER, message="帮我建个仪表盘")

    # 派生口径：首轮 LLM 真实发生（无法预知未开通），探索调用抛
    # CellNotProvisionedError → 常量回复收场（tool_trace 不落库）
    assert [m["role"] for m in body2["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert body2["messages"][-1]["content"] == "该企业尚未开通数据 cell，AI 构建暂不可用。请联系平台开通 cell 后再试。"
    artifacts = client.get(f"/api/v1/ai/conversations/{body2['id']}/artifacts", headers=OTHER)
    assert artifacts.json() == []


# ------------------------------------------------------------------ 失败如实


def test_llm_failure_honest_feedback_via_api(client, fake_llm) -> None:
    """LLM 异常：对话仍创建成功，assistant 如实报错，无产物草稿。"""
    fake_llm.script = [LlmGatewayError("token 网关不可达: 503")]
    body = create(client, message="建个仪表盘")

    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert "AI 服务调用失败" in body["messages"][1]["content"]
    assert "token 网关不可达" in body["messages"][1]["content"]
    artifacts = client.get(f"/api/v1/ai/conversations/{body['id']}/artifacts", headers=CUST)
    assert artifacts.json() == []


def test_tool_failure_reported_no_half_product(client, fake_llm, fake_executor) -> None:
    """工具异常经 API：错误回灌、终答如实说明、产物表为空。"""
    from fd_wire_api.ai.executor import CellToolError

    fake_llm.script = [
        tool_reply(ToolCall(id="c1", name="search_indicators", arguments={"q": "GDP"})),
        text_reply("探索工具执行失败（cell 端点 500），本轮没有产出，请稍后再试。"),
    ]
    fake_executor.behavior = {"search_indicators": CellToolError("cell MCP 端点返回 500: boom")}

    body = create(client, message="搜一下 GDP 指标")
    trace = json.loads(body["messages"][2]["content"])
    assert trace["result"]["ok"] is False
    assert trace["result"]["error_type"] == "CellToolError"
    assert "执行失败" in body["messages"][3]["content"]
    assert client.get(
        f"/api/v1/ai/conversations/{body['id']}/artifacts", headers=CUST
    ).json() == []


# ------------------------------------------------------------------ 隔离与鉴权


def test_tenant_isolation(client, fake_llm) -> None:
    """企业隔离：他企不可见列表、消息与产物（一律 404/不可见）。"""
    fake_llm.script = [text_reply("ok")]
    mine = create(client, title="我的对话")
    fake_llm.script = [text_reply("ok2")]
    theirs = create(client, headers=OTHER, title="别家对话")

    # 列表各见各的
    assert [c["title"] for c in client.get("/api/v1/ai/conversations", headers=CUST).json()] == ["我的对话"]
    assert [c["title"] for c in client.get("/api/v1/ai/conversations", headers=OTHER).json()] == ["别家对话"]

    # 他企访问我的会话：消息/产物/续聊一律 404（不泄露存在性）
    cid = mine["id"]
    assert client.get(f"/api/v1/ai/conversations/{cid}/messages", headers=OTHER).status_code == 404
    assert client.get(f"/api/v1/ai/conversations/{cid}/artifacts", headers=OTHER).status_code == 404
    assert client.post(
        f"/api/v1/ai/conversations/{cid}/messages", json={"content": "偷看"}, headers=OTHER
    ).status_code == 404
    # 不存在的会话同样 404
    assert client.get("/api/v1/ai/conversations/99999/messages", headers=CUST).status_code == 404


def test_customer_header_required(client) -> None:
    """缺 Bearer ID token 一律 401（X-Customer-Id 占位头退位）。"""
    assert client.post("/api/v1/ai/conversations", json={}).status_code == 401
    assert client.get("/api/v1/ai/conversations").status_code == 401
    assert client.get("/api/v1/ai/conversations/1/messages").status_code == 401


def test_create_without_message_is_empty_conversation(client, fake_llm) -> None:
    """不带 message 创建：只建档不跑循环（首条消息走 messages 端点）。"""
    body = create(client, title="空对话")
    assert body["messages"] == []
    assert fake_llm.calls == []
    assert client.get("/api/v1/ai/conversations", headers=CUST).json()[0]["title"] == "空对话"


# ── 动作端点补面（wire-copilot-action-gate）──────────────────────────────────


def _draft_propose(client, monkeypatch) -> dict:
    """便利：建会话并提议一份合法仪表盘草稿（付费档口径）。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for", lambda **_: "cell"
    )
    fake_llm = client.app.dependency_overrides[get_llm_client]()
    fake_llm.script = [
        tool_reply(ToolCall(id="c1", name="propose_action", arguments={
            "action_id": "wire.artifact.draft.create",
            "args": {"kind": "dashboard", "title": "看板A", "spec": {"panels": []}},
        })),
        text_reply("已提议。"),
    ]
    body = create(client, message="建看板")
    trace = json.loads(body["messages"][2]["content"])
    assert trace["result"]["ok"] is True
    return {"conversation_id": body["id"], "proposal_id": trace["result"]["proposal_id"]}


def test_action_reject_and_get_endpoints(client, monkeypatch) -> None:
    """拒绝端点：pending → rejected；详情端点回读同态；重复拒绝 409。"""
    ids = _draft_propose(client, monkeypatch)
    pid = ids["proposal_id"]

    rejected = client.post(f"/api/v1/ai/actions/{pid}/reject", headers=CUST)
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"

    detail = client.get(f"/api/v1/ai/actions/{pid}", headers=CUST)
    assert detail.status_code == 200
    assert detail.json()["state"] == "rejected"
    assert detail.json()["action_id"] == "wire.artifact.draft.create"

    again = client.post(f"/api/v1/ai/actions/{pid}/reject", headers=CUST)
    assert again.status_code == 409

    # 他企读/拒一律 404（不泄露存在性）
    assert client.get(f"/api/v1/ai/actions/{pid}", headers=OTHER).status_code == 404
    assert client.post(f"/api/v1/ai/actions/{pid}/reject", headers=OTHER).status_code == 404


def test_propose_endpoint_unregistered_404_and_cross_tenant_404(client) -> None:
    """直发提议端点：未注册 404；他企会话 404。"""
    body = create(client, title="t")
    resp = client.post("/api/v1/ai/actions/propose", json={
        "action_id": "wire.dataset.delete", "args": {}, "conversation_id": body["id"],
    }, headers=CUST)
    assert resp.status_code == 404
    assert "未注册" in resp.json()["detail"]

    cross = client.post("/api/v1/ai/actions/propose", json={
        "action_id": "wire.artifact.publish", "args": {"artifact_id": 1},
        "conversation_id": body["id"],
    }, headers=OTHER)
    assert cross.status_code == 404


def test_context_injected_into_unified_loop(client, fake_llm) -> None:
    """页面上下文进统一循环（wire-assistant-panel「注入受限上下文」在合并后
    的延续）：带 tab 时 system 提示含当前页标签；不带时与固定文案零漂移。"""
    fake_llm.script = [text_reply("ok"), text_reply("ok2")]
    create(client, message="这页能干什么", context={"tab": "datasets"})
    system_with = fake_llm.calls[0]["messages"][0]["content"]
    assert "我的数据" in system_with

    create(client, message="再问一句")
    system_without = fake_llm.calls[-1]["messages"][0]["content"]
    assert "【当前页面上下文】" not in system_without
