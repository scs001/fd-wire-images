"""动作闸测试（wire-copilot-action-gate）：注册表/提议/确认/审批/审计/过期/对拍。

覆盖 spec wire-action-gate 的核心场景：风险→档位映射、未注册拒绝、写不进
工具面（循环侧见 test_ai_loop）、三级确认、异步审批角色与 24h fail-closed、
需求单合规不代填、单一执行路径与审计脱敏、preflight 如实拦截、
tool-surface.json 风险单一真源对拍（design D10）。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.actions import (
    ACTION_TO_TOOL_NAME,
    ActionNotRegistered,
    ActorContext,
    ApprovalDenied,
    ComplianceMissing,
    ExecutionError,
    GateSessions,
    PreflightError,
    REGISTRY,
    RISK_TO_TIER,
    approve,
    expire_stale_proposals,
    propose,
    reject,
)
from fd_wire_api.ai.db import AiBase
from fd_wire_api.ai.models import (
    PROPOSAL_STATE_EXPIRED,
    PROPOSAL_STATE_FAILED,
    PROPOSAL_STATE_PENDING_APPROVAL,
    PROPOSAL_STATE_PENDING_CONFIRM,
    AIActionAudit,
    AIActionProposal,
    utc_now_naive,
)
from fd_wire_api.ai.tools import stage_artifact

FIXTURES = Path(__file__).parent / "fixtures"

OWNER = ActorContext(customer_id="cust-001", sub="sub-owner", role="owner")
EDITOR = ActorContext(customer_id="cust-001", sub="sub-editor", role="editor")
VIEWER = ActorContext(customer_id="cust-001", sub="sub-viewer", role="viewer")
OTHERS = ActorContext(customer_id="cust-002", sub="sub-x", role="owner")


@pytest.fixture()
def db():
    """单引擎内存库：AI 表 + 物化表 + 需求单表（闸执行体的跨切片会话束全挂它）。"""
    from fd_wire_api.demands.db import Base as DemandsBase
    from fd_wire_api.materialization.db import MatBase
    from fd_wire_api.packs.db import PacksBase
    import fd_wire_api.artifacts.models  # noqa: F401 - 注册 AiBase 映射（share/publish 执行体要查 artifacts）
    import fd_wire_api.packs.models  # noqa: F401 - 注册 PacksBase 映射（源包订阅执行体）

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AiBase.metadata.create_all(bind=engine)
    MatBase.metadata.create_all(bind=engine)
    DemandsBase.metadata.create_all(bind=engine)
    PacksBase.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def sess(db):
    return GateSessions(ai=db, datasets=db, demands=db, materialization=db, packs=db)


@pytest.fixture(autouse=True)
def engine_tier(monkeypatch):
    """默认按付费档（cell 层）判引擎；免费档用例自行覆盖。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for", lambda **_: "cell"
    )


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


# ── 注册表 ──────────────────────────────────────────────────────────────────


def test_v1_roster_and_tier_mapping() -> None:
    """v1 十三条（+源包订阅/退订，add-pack-subscribe-actions）；映射与壹座侧一致；名单外不存在。"""
    assert len(REGISTRY) == 13
    assert RISK_TO_TIER == {
        "write": "inline",
        "destructive": "second",
        "cost": "second",
        "publish": "async",
    }
    assert REGISTRY["wire.market.subscribe"].risk == "cost"
    assert REGISTRY["wire.pack.subscribe"].risk == "write"  # 源包订阅免费
    assert REGISTRY["wire.pack.unsubscribe"].risk == "write"
    assert REGISTRY["wire.artifact.share.create"].tier == "async"
    for banned in ("wire.key.mint", "wire.key.revoke", "wire.team.invite"):
        assert banned not in REGISTRY


def test_unregistered_action_rejected_no_row(db, sess) -> None:
    """未注册动作：异常抛出、不落提议行、不进确认（spec「未注册动作被拒绝」）。"""
    _conversation(db)
    with pytest.raises(ActionNotRegistered):
        propose(db, sess, OWNER, action_id="wire.dataset.delete", args={})
    assert db.query(AIActionProposal).count() == 0


# ── preflight ───────────────────────────────────────────────────────────────


def test_viewer_write_denied_at_propose(db, sess) -> None:
    """viewer 发起写提议：preflight 拒绝并给角色指引（spec「viewer 写操作被拒」）。"""
    _conversation(db)
    with pytest.raises(PreflightError) as exc:
        propose(
            db, sess, VIEWER,
            action_id="wire.artifact.publish", args={"artifact_id": 1},
        )
    assert "viewer" in str(exc.value)
    assert db.query(AIActionProposal).count() == 0


def test_free_tier_build_preflight_honest(db, sess, monkeypatch) -> None:
    """免费档构建类提议：被拦、文案含修复指引（spec「免费档构建动作被拦截并说明」）。"""
    monkeypatch.setattr(
        "fd_wire_api.datasets.service.storage_tier_for", lambda **_: "api"
    )
    _conversation(db)
    with pytest.raises(PreflightError) as exc:
        propose(
            db, sess, OWNER,
            action_id="wire.artifact.draft.create",
            args={"kind": "dashboard", "title": "t", "spec": {"panels": []}},
        )
    assert "数据处理引擎" in str(exc.value)


def test_ownership_preflight_no_leak(db, sess) -> None:
    """他队对象：统一「不存在或不属于当前团队」，不泄露存在性。"""
    _conversation(db)
    # 他队草稿（cust-002 的草稿行）
    other_conv = _conversation(db, customer_id="cust-002", cid=202)
    draft = stage_artifact(
        db, conversation_id=other_conv, customer_id="cust-002",
        tool_name="dashboard_spec_create",
        args={"title": "别家的", "spec": {"panels": []}},
    )
    with pytest.raises(PreflightError) as exc:
        propose(
            db, sess, OWNER,
            action_id="wire.artifact.promote", args={"draft_id": draft["artifact_id"]},
        )
    assert "不属于当前团队" in str(exc.value)


# ── 提议/确认/审计 ──────────────────────────────────────────────────────────


def test_draft_propose_then_approve_stages_and_audits(db, sess) -> None:
    """草稿提议→确认执行：草稿落库、审计 ok、result_summary 客户可读。"""
    _conversation(db)
    proposal, missing = propose(
        db, sess, OWNER,
        action_id="wire.artifact.draft.create",
        args={"kind": "dashboard", "title": "看板一",
              "spec": {"panels": [{"type": "kpi", "title": "p", "snapshot": {}}]}},
    )
    assert proposal.state == PROPOSAL_STATE_PENDING_CONFIRM
    assert missing == []

    outcome = approve(db, sess, OWNER, proposal.id)
    assert outcome["state"] == "executed"
    assert "草稿" in outcome["result_summary"]

    from fd_wire_api.ai.models import AIDraftArtifact

    rows = db.query(AIDraftArtifact).all()
    assert len(rows) == 1 and rows[0].kind == "dashboard"
    audit = db.query(AIActionAudit).one()
    assert audit.result == "ok" and audit.error_text is None
    # 拒绝重复确认（已终态）
    with pytest.raises(ApprovalDenied):
        approve(db, sess, OWNER, proposal.id)


def test_args_summary_sanitized(db, sess) -> None:
    """提议摘要脱敏：不含 spec 全文与凭据形态的值（spec「审计不含密钥」）。"""
    _conversation(db)
    proposal, _ = propose(
        db, sess, OWNER,
        action_id="wire.artifact.draft.create",
        args={"kind": "dashboard", "title": "看板一",
              "spec": {"panels": [{"type": "line", "data": list(range(50))}],
                       "secret": "sk-super-secret-value-123456"}},
    )
    # spec 只记键名：面板数据与藏进去的凭据形态值都不进摘要
    assert "panels" in proposal.args_summary and "secret" in proposal.args_summary
    assert "sk-super-secret-value-123456" not in proposal.args_summary
    assert "49" not in proposal.args_summary


def test_execution_failure_honest_audit_error(db, sess, published_dashboard, monkeypatch) -> None:
    """执行失败：state=failed、审计 error、原因保留可重试（spec「回执不冒充结果」）。

    形态：提议时分享密钥已配（preflight 过），执行前撤掉 → 执行期
    ShareDisabled 如实落 failed。
    """
    _conversation(db)
    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", "test-share-secret-0123456789")
    proposal, _ = propose(
        db, sess, OWNER,
        action_id="wire.artifact.share.create",
        args={"artifact_id": published_dashboard.id},
    )
    assert proposal.state == PROPOSAL_STATE_PENDING_APPROVAL
    assert proposal.expires_at is not None
    monkeypatch.delenv("FD_WIRE_SHARE_SECRET")

    outcome = approve(db, sess, OWNER, proposal.id)
    assert outcome["state"] == PROPOSAL_STATE_FAILED
    assert "分享" in outcome["error"]
    audit = db.query(AIActionAudit).one()
    assert audit.result == "error"


# ── 异步审批：角色门 + 24h fail-closed ─────────────────────────────────────


@pytest.fixture()
def published_dashboard(db):
    """真实已发布看板产物（空白创建→发布），share 异步族用。"""
    from fd_wire_api.artifacts.service import create_blank_artifact, publish_artifact

    artifact = create_blank_artifact(
        db, customer_id="cust-001", kind="dashboard", title="区域看板"
    )
    return publish_artifact(db, artifact, customer_id="cust-001")


@pytest.fixture()
def share_secret(monkeypatch):
    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", "test-share-secret-0123456789")


def _async_proposal(db, sess, published_dashboard) -> AIActionProposal:
    _conversation(db)
    proposal, _ = propose(
        db, sess, OWNER,
        action_id="wire.artifact.share.create",
        args={"artifact_id": published_dashboard.id},
    )
    return proposal


def test_async_viewer_cannot_approve(db, sess, published_dashboard) -> None:
    """viewer 批 async：403 语义（ApprovalDenied kind=role），状态不变。"""
    proposal = _async_proposal(db, sess, published_dashboard)
    with pytest.raises(ApprovalDenied) as exc:
        approve(db, sess, VIEWER, proposal.id)
    assert exc.value.kind == "role"
    assert db.get(AIActionProposal, proposal.id).state == PROPOSAL_STATE_PENDING_APPROVAL


def test_async_expiry_fail_closed(db, sess, published_dashboard) -> None:
    """24h 超时：批准被拒（fail-closed）、状态 expired；扫描器可注入时钟。"""
    proposal = _async_proposal(db, sess, published_dashboard)
    future = utc_now_naive() + timedelta(hours=25)

    # 批准时发现已过期 → 拒绝且不执行
    with pytest.raises(ApprovalDenied) as exc:
        approve(db, sess, OWNER, proposal.id, now=future)
    assert exc.value.kind == "state"
    assert db.get(AIActionProposal, proposal.id).state == PROPOSAL_STATE_EXPIRED

    # 扫描器同口径（injectable now）：另一条提议被扫为 expired
    proposal2 = _async_proposal(db, sess, published_dashboard)
    assert expire_stale_proposals(db, now=future) == 1
    assert db.get(AIActionProposal, proposal2.id).state == PROPOSAL_STATE_EXPIRED


def test_share_result_redacts_token_in_persisted_summary(
    db, sess, published_dashboard, share_secret
) -> None:
    """分享链接只在批准确认响应里出现一次；提议表/历史回放不含令牌（spec
    「审计不含密钥」）。"""
    _conversation(db)
    proposal, _ = propose(
        db, sess, OWNER,
        action_id="wire.artifact.share.create",
        args={"artifact_id": published_dashboard.id},
    )
    outcome = approve(db, sess, OWNER, proposal.id)
    assert outcome["state"] == "executed"
    # 即时响应带完整链接（客户当场复制）。
    assert "?t=" in outcome["result_summary"]
    assert "t=***" not in outcome["result_summary"]

    row = db.get(AIActionProposal, proposal.id)
    assert "t=***" in row.result_summary
    assert "?t=1" not in row.result_summary


def test_reject_any_pending(db, sess, published_dashboard) -> None:
    """拒绝：pending → rejected，不执行。"""
    proposal = _async_proposal(db, sess, published_dashboard)
    rejected = reject(db, OWNER, proposal.id)
    assert rejected.state == "rejected"
    with pytest.raises(ApprovalDenied):
        reject(db, OWNER, proposal.id)


# ── 需求单合规门 ────────────────────────────────────────────────────────────


def test_demand_compliance_never_from_model(db, sess) -> None:
    """模型给的合规取值在提议时即丢弃；缺字段/未勾选/非 http 均 422 语义。"""
    _conversation(db)
    proposal, missing = propose(
        db, sess, OWNER,
        action_id="wire.demand.submit",
        args={
            "description": "要某市的规上工业月度数据",
            "contact_email": "a@b.c",
            "source_urls": ["https://evil.example"],  # 模型侧取值——必须被丢弃
            "public_data_asserted": True,
        },
    )
    assert missing == ["source_url", "public_claim"]
    assert "evil.example" not in proposal.args_summary  # 连摘要都不留

    # 缺合规 → ComplianceMissing
    with pytest.raises(ComplianceMissing):
        approve(db, sess, OWNER, proposal.id, compliance={})
    # 未勾选声明 → 同样拒绝
    with pytest.raises(ComplianceMissing):
        approve(db, sess, OWNER, proposal.id,
                compliance={"source_url": "https://data.example", "public_claim": False})
    # 非 http(s) → 拒绝
    with pytest.raises(ComplianceMissing):
        approve(db, sess, OWNER, proposal.id,
                compliance={"source_url": "ftp://x", "public_claim": True})


def test_demand_submit_executes_with_customer_compliance(db, sess) -> None:
    """客户手填合规 → 执行建单成功（demands 库真行）。"""
    from fd_wire_api.demands.models import Demand

    _conversation(db)
    proposal, _ = propose(
        db, sess, OWNER,
        action_id="wire.demand.submit",
        args={"description": "要某市的规上工业月度数据", "contact_email": "a@b.c"},
    )
    outcome = approve(
        db, sess, OWNER, proposal.id,
        compliance={"source_url": "https://data.example/open", "public_claim": True},
    )
    assert outcome["state"] == "executed"
    demand = db.query(Demand).one()
    assert demand.customer_id == "cust-001"
    assert demand.public_data_asserted is True


# ── 转正执行路径（promote e2e）──────────────────────────────────────────────


def test_promote_single_execution_path(db, sess) -> None:
    """草稿转正：经闸执行体建档（产物页可见）+ 登记物化；二次转正如实失败。"""
    cid = _conversation(db)
    draft = stage_artifact(
        db, conversation_id=cid, customer_id="cust-001",
        tool_name="dashboard_spec_create",
        args={"title": "看板一", "spec": {"panels": []}},
    )
    proposal, _ = propose(
        db, sess, OWNER,
        action_id="wire.artifact.promote", args={"draft_id": draft["artifact_id"]},
    )
    outcome = approve(db, sess, OWNER, proposal.id)
    assert outcome["state"] == "executed"
    assert "产物页可见" in outcome["result_summary"]

    from fd_wire_api.artifacts.models import Artifact
    from fd_wire_api.artifacts.service import list_artifacts
    from fd_wire_api.materialization.models import MaterializedArtifact

    assert db.query(MaterializedArtifact).count() == 1
    rows = list_artifacts(db, customer_id="cust-001")
    assert len(rows) == 1
    assert rows[0].title == "看板一"
    assert rows[0].status == "draft"
    assert f"#{rows[0].id}" in outcome["result_summary"]
    assert db.query(Artifact).count() == 1

    proposal2, _ = propose(
        db, sess, OWNER,
        action_id="wire.artifact.promote", args={"draft_id": draft["artifact_id"]},
    )
    outcome = approve(db, sess, OWNER, proposal2.id)
    assert outcome["state"] == PROPOSAL_STATE_FAILED
    assert "已转正过" in outcome["error"]
    # 二次转正不得再建一份 artifacts 行（先查物化再建档）。
    assert db.query(Artifact).count() == 1


def test_pack_subscribe_and_unsubscribe_executes(db, sess) -> None:
    """源包订阅/退订（add-pack-subscribe-actions）：订阅免费 write/inline；行随动作生灭。"""
    from fd_wire_api.packs.models import PackSubscription

    _conversation(db)
    p1, _ = propose(
        db, sess, OWNER, action_id="wire.pack.subscribe", args={"pack_id": "macro-price"}
    )
    assert p1.tier == "inline" and p1.state == "pending_confirm"
    out = approve(db, sess, OWNER, p1.id)
    assert out["state"] == "executed"
    assert "订阅免费" in out["result_summary"]
    assert db.query(PackSubscription).count() == 1

    # 重复订阅幂等：仍 executed，不落第二行
    p2, _ = propose(db, sess, OWNER, action_id="wire.pack.subscribe", args={"pack_id": "macro-price"})
    approve(db, sess, OWNER, p2.id)
    assert db.query(PackSubscription).count() == 1

    p3, _ = propose(db, sess, OWNER, action_id="wire.pack.unsubscribe", args={"pack_id": "macro-price"})
    out3 = approve(db, sess, OWNER, p3.id)
    assert out3["state"] == "executed"
    assert db.query(PackSubscription).count() == 0


# ── 发布执行体 + 读工具执行器 ────────────────────────────────────────────────


def test_publish_inline_tier_executes(db, sess) -> None:
    """发布（write/inline）：非 viewer 确认即执行，结果摘要含版本与队内口径。"""
    from fd_wire_api.artifacts.service import create_blank_artifact

    _conversation(db)
    artifact = create_blank_artifact(
        db, customer_id="cust-001", kind="dashboard", title="待发布看板"
    )
    proposal, _ = propose(
        db, sess, EDITOR,
        action_id="wire.artifact.publish",
        args={"artifact_id": artifact.id},
    )
    assert proposal.tier == "inline"
    outcome = approve(db, sess, EDITOR, proposal.id)
    assert outcome["state"] == "executed"
    assert "队内可见" in outcome["result_summary"]


def test_read_tool_executor_own_data_only(db, sess) -> None:
    """读工具执行器：自有数据可读、他企不可见（spec「免费档问答与读取可用」）。"""
    from fd_wire_api.ai.reads import ReadToolExecutor
    from fd_wire_api.datasets.db import DatasetsBase
    from fd_wire_api.datasets.models import Dataset

    DatasetsBase.metadata.create_all(bind=db.get_bind())
    db.add(Dataset(id="ds-1", customer_id="cust-001", key="k1", name="我的表",
                   source_kind="upload", columns_json=[{"name": "日期", "type": "TEXT"}],
                   row_count=7))
    db.add(Dataset(id="ds-2", customer_id="cust-002", key="k2", name="别家的表",
                   source_kind="upload", columns_json=[], row_count=0))
    db.commit()

    reader = ReadToolExecutor(ai_db=db, datasets_db=db, demands_db=db)
    listing = reader.execute("dataset_list", {}, customer_id="cust-001")
    assert listing["ok"] is True and listing["count"] == 1
    assert listing["datasets"][0]["name"] == "我的表"

    schema = reader.execute("dataset_schema", {"dataset_id": "ds-1"}, customer_id="cust-001")
    assert schema["columns"] == [{"name": "日期", "type": "TEXT"}]

    # 他企数据集：不存在口径，不泄露
    foreign = reader.execute("dataset_schema", {"dataset_id": "ds-2"}, customer_id="cust-001")
    assert foreign["ok"] is False


# ── 风险单一真源对拍（design D10 / 任务 5.2）────────────────────────────────


def test_tool_risk_fixture_consistency() -> None:
    """注册表风险 ↔ tool-surface.json ``tool_risk`` 逐条对拍；不一致即失败。"""
    mismatches: list[str] = []
    for action_id, tool_name in ACTION_TO_TOOL_NAME.items():
        fixture = FIXTURES / f"{tool_name.rsplit('_', 1)[0]}-mcp.tool-surface.json"
        # 工具名 → 夹具文件名的映射：wire_market_subscribe → wire 前缀族（ops/build）
        fixture_path = _fixture_for(tool_name)
        assert fixture_path.exists(), f"缺少夹具：{fixture_path}"
        tool_risk = json.loads(fixture_path.read_text()).get("tool_risk", {})
        if tool_name not in tool_risk:
            mismatches.append(f"{tool_name} 未在 {fixture_path.name} 声明风险")
        elif tool_risk[tool_name] != REGISTRY[action_id].risk:
            mismatches.append(
                f"{action_id}({REGISTRY[action_id].risk}) ≠ "
                f"{fixture_path.name}:{tool_name}({tool_risk[tool_name]})"
            )
    assert not mismatches, "风险声明与操作面清单不一致：\n" + "\n".join(mismatches)


def _fixture_for(tool_name: str) -> Path:
    ops_tools = {
        "wire_demand_submit", "wire_market_subscribe", "wire_market_unsubscribe",
        "wire_pack_subscribe", "wire_pack_unsubscribe",
    }
    return (
        FIXTURES / "wire-ops-mcp.tool-surface.json"
        if tool_name in ops_tools
        else FIXTURES / "wire-build-mcp.tool-surface.json"
    )
