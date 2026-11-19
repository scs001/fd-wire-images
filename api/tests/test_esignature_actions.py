"""电子签动作闸测试（add-esignature-wire-actions 任务 2.2/2.3 + 红线 3.1）。

对账 spec `wire-esignature-actions`：
- 「四动作注册与风险分级」：三写动作 tier=inline、读工具直执行；
- 「不代签机器红线」：lex_esign 客户端路径全集断言 + 模块源码无签署人面路径；
- 「租户限定与执行如实性」：他租户 404 不泄露存在性、preflight 状态机、
  确认后漂移 execute 复校 failed、通道未配置如实失败；
- propose→pending→approve→execute 全链经注入的 httpx.MockTransport 假件，
  出网请求形状（路径/头/body）逐项断言冻结契约。

fd-lex 数据面行为在 fd-lex 仓 tests/test_admin_esignature.py 对账。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from fd_wire_api.ai.actions import (
    ACTION_TO_TOOL_NAME,
    ActorContext,
    GateSessions,
    PROPOSAL_STATE_PENDING_CONFIRM,
    ApprovalDenied,
    approve,
    propose,
    reject,
)
from fd_wire_api.ai.lex_esign import (
    ARCHIVABLE_STATES,
    MACHINE_PATHS,
    NUDGEABLE_STATES,
    HttpLexEsignClient,
    LexEsignChannelNotConfigured,
)
from fd_wire_api.ai.reads import READ_TOOL_SCHEMAS, ReadToolExecutor, is_read_tool

OWNER = ActorContext(customer_id="cust-001", sub="sub-owner", role="owner")
EDITOR = ActorContext(customer_id="cust-001", sub="sub-editor", role="editor")
VIEWER = ActorContext(customer_id="cust-001", sub="sub-viewer", role="viewer")

TOKEN = "test-internal"
FILE_B64 = base64.b64encode("买卖合同全文".encode()).decode()

_INIT_PAYLOAD = {
    "customer_id": "cust-001",
    "title": "设备买卖合同",
    "category": "sale",
    "file_name": "contract.txt",
    "file_b64": FILE_B64,
    "signers": [{"name": "A公司代表", "role_label": "甲方"},
                {"name": "B公司代表", "role_label": "乙方"}],
    "acknowledged": False,
}

_TASK_VIEW = {
    "id": "es_01",
    "status": "pending",
    "title": "设备买卖合同",
    "anchor_sha256": "a" * 64,
    "adapter_name": "stub",
    "signers": [
        {"seq": 1, "name": "A公司代表", "status": "pending"},
        {"seq": 2, "name": "B公司代表", "status": "pending"},
    ],
}


@pytest.fixture()
def db():
    from fd_wire_api.ai.db import AiBase

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AiBase.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def sess(db):
    return GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db)


@pytest.fixture(autouse=True)
def _clear_proposed_args():
    from fd_wire_api.ai import actions

    actions._PROPOSED_ARGS.clear()
    yield
    actions._PROPOSED_ARGS.clear()


def _conversation(db, customer_id="cust-001", cid=101):
    from fd_wire_api.ai.actions import set_conversation_id
    from fd_wire_api.ai.models import AIConversation

    if db.get(AIConversation, cid) is None:
        db.add(AIConversation(id=cid, customer_id=customer_id, title="t"))
        db.commit()
    set_conversation_id(cid)
    return cid


class _Recorder:
    """捕获出网请求并按脚本回shape的 MockTransport 构造器。"""

    def __init__(self, responses: list[tuple[int, dict]]):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self._responses.pop(0) if self._responses else (200, {})
        return httpx.Response(status, json=body)

    def calls(self) -> list[dict[str, Any]]:
        out = []
        for r in self.requests:
            body = json.loads(r.content) if r.content else None
            out.append({"method": r.method, "path": r.url.path,
                        "params": dict(r.url.params), "json": body,
                        "admin_token": r.headers.get("X-Admin-Token")})
        return out


@pytest.fixture()
def env_channel(monkeypatch):
    monkeypatch.setenv("LEX_ESIGN_BASE_URL", "https://lex.test")
    monkeypatch.setenv("LEX_ESIGN_INTERNAL_TOKEN", TOKEN)


# ── 注册表与风险分级 ─────────────────────────────────────────────────────────


def test_roster_tier_and_tool_mapping() -> None:
    from fd_wire_api.ai import actions

    for action_id in ("wire.esignature.initiate", "wire.esignature.nudge",
                      "wire.esignature.archive"):
        def_ = actions.resolve_action.__wrapped__ if hasattr(actions.resolve_action, "__wrapped__") else None
    registry = actions.REGISTRY
    for action_id in ("wire.esignature.initiate", "wire.esignature.nudge",
                      "wire.esignature.archive"):
        assert action_id in registry
        assert registry[action_id].risk == "write"
        assert registry[action_id].tier == "inline"
    # 无 MCP 工具名对拍条目（share.create 同款先例）
    assert not any(k.startswith("wire.esignature") for k in ACTION_TO_TOOL_NAME)


def test_read_tool_registered_direct() -> None:
    assert is_read_tool("esignature_status")
    schema = next(i for i in READ_TOOL_SCHEMAS if i["function"]["name"] == "esignature_status")
    assert schema["function"]["parameters"]["required"] == ["task_id"]


# ── 客户端契约（出网形状） ───────────────────────────────────────────────────


def test_client_request_shapes(env_channel) -> None:
    rec = _Recorder([
        (200, _TASK_VIEW),          # initiate
        (200, {**_TASK_VIEW, "status": "in_progress"}),  # status
        (200, {"task_id": "es_01", "status": "in_progress", "signers": [], "reminded": True}),
        (200, {"task_id": "es_01", "status": "completed", "evidence_ready": True,
               "anchor_sha256": "a" * 64, "cards": 2}),
    ])
    with HttpLexEsignClient(http_client=httpx.Client(transport=httpx.MockTransport(rec.handler))) as c:
        c.initiate_task(_INIT_PAYLOAD)
        c.get_status(customer_id="cust-001", task_id="es_01")
        c.nudge(customer_id="cust-001", task_id="es_01")
        c.archive_task(customer_id="cust-001", task_id="es_01")
    calls = rec.calls()
    assert [x["path"] for x in calls] == [
        "/api/admin/esignature/tasks",
        "/api/admin/esignature/tasks/es_01",
        "/api/admin/esignature/tasks/es_01/nudge",
        "/api/admin/esignature/tasks/es_01/archive",
    ]
    assert all(x["admin_token"] == TOKEN for x in calls)
    assert calls[0]["json"] == _INIT_PAYLOAD
    assert calls[1]["params"] == {"customer_id": "cust-001"}
    assert calls[2]["json"] == {"customer_id": "cust-001"}


def test_machine_paths_contain_no_signer_surface() -> None:
    """红线：客户端路径全集不含签署人面（/p/、/s/、realname、intent、sign）。"""
    forbidden = {"realname", "intent", "sign"}
    for p in MACHINE_PATHS:
        segments = set(p.strip("/").split("/"))
        assert not (segments & forbidden), p
        assert "/s/" not in p and "/p/" not in p, p
    # 源码级双锁：客户端模块不得出现签署面路径字面量（docstring 里的红线
    # 描述用词除外——只锁路径形态，不锁说明文字）
    import inspect

    from fd_wire_api.ai import lex_esign

    src = inspect.getsource(lex_esign)
    for frag in ('"/api/esignature/s/', '"/api/esignature/p/', "/realname", "intent_method"):
        assert frag not in src, frag


# ── 闸链：propose → approve → execute ───────────────────────────────────────


def test_initiate_propose_summary_masks_b64(db, sess) -> None:
    _conversation(db)
    proposal, missing = propose(
        db, sess, OWNER, action_id="wire.esignature.initiate", args=dict(_INIT_PAYLOAD)
    )
    assert proposal.state == PROPOSAL_STATE_PENDING_CONFIRM
    assert missing == []
    assert FILE_B64 not in (proposal.args_summary or "")
    assert "file_b64=<b64:" in (proposal.args_summary or "")


def test_initiate_full_chain_owner_executes(db, sess, env_channel) -> None:
    _conversation(db)
    rec = _Recorder([(200, _TASK_VIEW)])
    import fd_wire_api.ai.lex_esign as le

    real_client = le.HttpLexEsignClient

    def _fake(**kw):
        kw.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(rec.handler)))
        return real_client(**kw)

    le.HttpLexEsignClient = _fake  # type: ignore[assignment]
    try:
        proposal, _ = propose(
            db, sess, OWNER, action_id="wire.esignature.initiate", args=dict(_INIT_PAYLOAD)
        )
        outcome = approve(db, sess, OWNER, proposal.id)
        assert outcome["state"] == "executed"
        assert "task_id=es_01" in outcome["result_summary"]
        assert "签署链接在识律控制台" in outcome["result_summary"]
    finally:
        le.HttpLexEsignClient = real_client
    (call,) = rec.calls()
    assert call["path"] == "/api/admin/esignature/tasks"
    assert call["json"]["customer_id"] == "cust-001"
    assert call["admin_token"] == TOKEN


def test_initiate_viewer_denied_at_propose(db, sess) -> None:
    _conversation(db)
    with pytest.raises(Exception) as exc:
        propose(db, sess, VIEWER, action_id="wire.esignature.initiate", args=dict(_INIT_PAYLOAD))
    assert "viewer" in str(exc.value)


def test_unconfirmed_proposal_never_executes(db, sess, env_channel) -> None:
    _conversation(db)
    rec = _Recorder([(200, {**_TASK_VIEW, "status": "completed"})])
    import fd_wire_api.ai.lex_esign as le

    real_client = le.HttpLexEsignClient

    def _fake(**kw):
        kw.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(rec.handler)))
        return real_client(**kw)

    le.HttpLexEsignClient = _fake  # type: ignore[assignment]
    try:
        proposal, _ = propose(
            db, sess, OWNER, action_id="wire.esignature.archive", args={"task_id": "es_01"}
        )
        assert proposal.state == PROPOSAL_STATE_PENDING_CONFIRM
        calls = rec.calls()
        # preflight 只查状态（GET，设计 D4）；未确认零写操作出网
        assert calls, "preflight 应有一次状态查询"
        assert all(c["method"] == "GET" for c in calls)
    finally:
        le.HttpLexEsignClient = real_client


def test_nudge_preflight_state_machine(db, sess, env_channel) -> None:
    _conversation(db)
    rec = _Recorder([(200, {**_TASK_VIEW, "status": "completed"})])
    import fd_wire_api.ai.lex_esign as le

    real_client = le.HttpLexEsignClient

    def _fake(**kw):
        kw.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(rec.handler)))
        return real_client(**kw)

    le.HttpLexEsignClient = _fake  # type: ignore[assignment]
    try:
        with pytest.raises(Exception) as exc:
            propose(db, sess, OWNER, action_id="wire.esignature.nudge", args={"task_id": "es_01"})
        assert "不可催签" in str(exc.value)
    finally:
        le.HttpLexEsignClient = real_client
    assert len(rec.calls()) == 1  # 只查了状态，未落提议


def test_cross_tenant_404_no_leak(db, sess, env_channel) -> None:
    _conversation(db)
    rec = _Recorder([(404, {"detail": "任务不存在"})])
    import fd_wire_api.ai.lex_esign as le

    real_client = le.HttpLexEsignClient

    def _fake(**kw):
        kw.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(rec.handler)))
        return real_client(**kw)

    le.HttpLexEsignClient = _fake  # type: ignore[assignment]
    try:
        with pytest.raises(Exception) as exc:
            propose(db, sess, OWNER, action_id="wire.esignature.archive", args={"task_id": "es_x"})
        assert "不存在" in str(exc.value)
        assert "cust-002" not in str(exc.value)
    finally:
        le.HttpLexEsignClient = real_client


def test_archive_exec_drift_fails_honest(db, sess, env_channel) -> None:
    """确认后状态漂移：execute 复校如实 failed，零半写（无出网归档调用）。"""
    _conversation(db)
    rec = _Recorder([
        (200, {**_TASK_VIEW, "status": "completed"}),   # preflight status
        (200, {**_TASK_VIEW, "status": "revoked"}),     # execute 复校（漂移）
    ])
    import fd_wire_api.ai.lex_esign as le

    real_client = le.HttpLexEsignClient

    def _fake(**kw):
        kw.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(rec.handler)))
        return real_client(**kw)

    le.HttpLexEsignClient = _fake  # type: ignore[assignment]
    try:
        proposal, _ = propose(
            db, sess, OWNER, action_id="wire.esignature.archive", args={"task_id": "es_01"}
        )
        outcome = approve(db, sess, OWNER, proposal.id)
        assert outcome["state"] == "failed"
        assert "已漂移" in outcome.get("error", "") or "漂移" in str(outcome)
        assert len(rec.calls()) == 2  # 复校失败后不再调 archive
    finally:
        le.HttpLexEsignClient = real_client


def test_channel_unconfigured_honest_everywhere(db, sess, monkeypatch) -> None:
    """「机器通道未配置如实失败」：nudge/archive 提议即拒（preflight）；
    initiate 形状预检过提议，执行期如实 failed；读工具如实回错。零假成功。"""
    monkeypatch.delenv("LEX_ESIGN_BASE_URL", raising=False)
    monkeypatch.delenv("LEX_ESIGN_INTERNAL_TOKEN", raising=False)
    _conversation(db)
    # initiate：preflight 只做形状校验（无任务可查，D4），通道缺失在执行期暴露
    proposal, _ = propose(
        db, sess, OWNER, action_id="wire.esignature.initiate", args=dict(_INIT_PAYLOAD)
    )
    outcome = approve(db, sess, OWNER, proposal.id)
    assert outcome["state"] == "failed"
    assert "未配置" in outcome["error"]
    # nudge/archive：preflight 查状态即撞未配置 → 不落提议
    for action_id, args in (
        ("wire.esignature.nudge", {"task_id": "es_01"}),
        ("wire.esignature.archive", {"task_id": "es_01"}),
    ):
        with pytest.raises(Exception) as exc:
            propose(db, sess, OWNER, action_id=action_id, args=args)
        assert "未配置" in str(exc.value)
    ex = ReadToolExecutor.__new__(ReadToolExecutor)
    r = ex.execute("esignature_status", {"task_id": "es_01"}, customer_id="cust-001")
    assert r["ok"] is False and "未配置" in r["error"]


def test_read_tool_no_proposal_row(db, sess, env_channel) -> None:
    """查状态免确认：直执行且不产生提议记录。"""
    _conversation(db)
    rec = _Recorder([(200, _TASK_VIEW)])
    import fd_wire_api.ai.lex_esign as le

    real_client = le.HttpLexEsignClient

    def _fake(**kw):
        kw.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(rec.handler)))
        return real_client(**kw)

    le.HttpLexEsignClient = _fake  # type: ignore[assignment]
    try:
        ex = ReadToolExecutor.__new__(ReadToolExecutor)
        r = ex.execute("esignature_status", {"task_id": "es_01"}, customer_id="cust-001")
        assert r["ok"] is True and r["task"]["id"] == "es_01"
    finally:
        le.HttpLexEsignClient = real_client
    from fd_wire_api.ai.models import AIActionProposal

    assert db.query(AIActionProposal).count() == 0


# ── 壹座岗位车道：会话上下文可缺省（add-esignature-selfaction-gate 设计偏差修正）


def test_propose_without_conversation_lands_null(db, sess, env_channel) -> None:
    """MCP 车道（wire-ops-mcp 自理动作面）不带会话：提议落
    conversation_id=None，与 domain.* 同口径；面板语义（带会话）不变。"""
    from fd_wire_api.ai import actions
    from fd_wire_api.ai.models import AIActionProposal

    actions.set_conversation_id(None)  # 路由层对无会话车道的注入
    rec = _Recorder([(201, {"task_id": "es_90", "status": "pending"})])
    import fd_wire_api.ai.lex_esign as le

    real_client = le.HttpLexEsignClient

    def _fake(**kw):
        kw.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(rec.handler)))
        return real_client(**kw)

    le.HttpLexEsignClient = _fake  # type: ignore[assignment]
    try:
        proposal, _missing = propose(
            db, sess, OWNER,
            action_id="wire.esignature.initiate",
            args={"title": "无会话提议", "category": "service",
                  "file_name": "a.txt", "file_b64": "QUJD",
                  "signers": [{"name": "甲"}]},
        )
        assert proposal.conversation_id is None
        assert proposal.state == "pending_confirm"
        # 同请求后面板的带会话提议仍落会话值（ContextVar 每请求设置，互不泄漏）
        _conversation(db, cid=202)
        rec._responses.extend([(200, _TASK_VIEW), (200, {"task_id": "es_90", "status": "pending"})])
        proposal2, _ = propose(
            db, sess, OWNER,
            action_id="wire.esignature.nudge", args={"task_id": "es_90"},
        )
        assert proposal2.conversation_id == 202
    finally:
        le.HttpLexEsignClient = real_client
    rows = db.query(AIActionProposal).order_by(AIActionProposal.id).all()
    assert [r.conversation_id for r in rows] == [None, 202]


def test_list_proposals_without_conversation_sees_all_customer_rows(db, sess) -> None:
    """缺省会话的 list=本企业全量台账（岗位车道看 NULL 行的唯一窗口）。"""
    from fd_wire_api.ai.models import AIActionProposal

    db.add(AIActionProposal(
        conversation_id=None, customer_id="cust-001",
        action_id="wire.esignature.initiate", args_digest="d1",
        args_summary="s", risk="write", tier="inline",
        state="pending_confirm", impact_text="i", proposed_by="sub-owner",
    ))
    db.add(AIActionProposal(
        conversation_id=7, customer_id="cust-001",
        action_id="wire.artifact.publish", args_digest="d2",
        args_summary="s", risk="write", tier="inline",
        state="pending_confirm", impact_text="i", proposed_by="sub-owner",
    ))
    db.commit()
    from fd_wire_api.ai.actions import list_proposals

    all_rows = list_proposals(db, conversation_id=None, customer_id="cust-001")
    assert len(all_rows) == 2
    conv_rows = list_proposals(db, conversation_id=7, customer_id="cust-001")
    assert [r.action_id for r in conv_rows] == ["wire.artifact.publish"]
