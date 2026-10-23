"""产物证据块测试（wire-info-stream-program 4.1/4.2/4.3）。

覆盖：

- 4.1 结构层：可选 evidence 块逐条校验，错误定位到证据项（索引/字段）；
  无 evidence 的既有 spec 照常通过（向后兼容零影响）。
- 4.2 发布层：经可注入 InfoStore 假实现核对——存在性（伪造 stable_id）、
  逐字片段（span 区间服务端复核，错位/越界均拒）、时间（未来 as_of 拒）。
- 4.3 发布门：EVIDENCE_GATE_MODE=off|warn|enforce，默认 warn 只记结构化
  告警日志（含产物 id、失败项、原因）不阻断；enforce 才 422；off 跳过。
"""

from __future__ import annotations

import json
import logging
import os

# 在导入 app 前把默认引擎指向内存库（同 test_artifacts_lifecycle 口径）
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db, reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.artifacts import service
from fd_wire_api.artifacts.evidence import (
    EvidenceGateSettings,
    EvidenceItem,
    GATE_ENFORCE,
    GATE_OFF,
    GATE_WARN,
    get_evidence_gate_settings,
    parse_evidence,
    validate_evidence,
    verify_evidence,
)
from fd_wire_api.artifacts.models import Artifact, ArtifactVersion

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-evd-001"
CUST = bearer(mint_id_token(SUB_CUST))

#: 证据指向的"原文"（正文全文——假 info-store 返回它，span 按它定位）
TEXT = "公司公告：拟以十二亿元收购标的公司100%股权，交易尚需股东大会审议。"
QUOTE = "拟以十二亿元收购"
START = TEXT.index(QUOTE)
END = START + len(QUOTE)


def evidence_item(**override):
    item = {
        "claim": "公司拟收购",
        "source": "cninfo",
        "stable_id": "cninfo:123",
        "as_of": "2026-10-20",
        "span": {"start": START, "end": END, "quote": QUOTE},
    }
    item.update(override)
    return item


def spec_with(evidence):
    return {"title": "报告", "spec": {"body": "x"}, "evidence": evidence}


class FakeInfoStore:
    """info-store 窄口假实现：命中表 + 调用留痕 + 可注入故障。"""

    def __init__(self, docs=None, fail=False):
        self.docs = {(d["source"], d["stable_id"]): d for d in (docs or [])}
        self.fail = fail
        self.calls = []

    def resolve(self, *, source, stable_id):
        from fd_wire_api.artifacts.evidence import ResolvedEvidence

        self.calls.append({"source": source, "stable_id": stable_id})
        if self.fail:
            raise RuntimeError("connection refused")
        doc = self.docs.get((source, stable_id))
        if doc is None:
            return None
        return ResolvedEvidence(
            text=doc["text"], published_at=doc.get("published_at")
        )


def good_store():
    return FakeInfoStore(
        docs=[
            {
                "source": "cninfo",
                "stable_id": "cninfo:123",
                "text": TEXT,
                "published_at": "2026-10-20T09:00:00",
            }
        ]
    )


# ------------------------------------------------------------- 4.1 结构层

def test_old_spec_without_evidence_passes():
    """向后兼容硬约束：无 evidence 的既有 spec 结构层/发布层都通过。"""
    old = {"title": "t", "spec": {"panels": []}}
    assert validate_evidence(old) is None
    assert verify_evidence(old, good_store(), version_time="2026-10-20T00:00:00") == []


def test_good_evidence_passes_structure():
    assert validate_evidence(spec_with([evidence_item()])) is None


def test_structural_errors_locate_item_and_field():
    # 缺 span → 定位到 evidence[0].span
    error = validate_evidence(
        spec_with([{"source": "s", "stable_id": "i", "as_of": "2026-10-20"}])
    )
    assert "evidence[0]" in error and "span" in error
    # 区间非法 → 定位到第 2 条的 span
    bad = spec_with([evidence_item(), evidence_item(span={"start": 5, "end": 5, "quote": "x"})])
    error = validate_evidence(bad)
    assert "evidence[1]" in error and "区间非法" in error
    # as_of 非法
    error = validate_evidence(spec_with([evidence_item(as_of="2026/10/20")]))
    assert "evidence[0].as_of" in error and "ISO" in error
    # claim 类型错
    error = validate_evidence(spec_with([evidence_item(claim=42)]))
    assert "evidence[0].claim" in error
    # 非数组
    error = validate_evidence(spec_with({"a": 1}))
    assert "必须是数组" in error


def test_parse_evidence_fields():
    items = parse_evidence([evidence_item()])
    assert items == [
        EvidenceItem(
            source="cninfo",
            stable_id="cninfo:123",
            as_of="2026-10-20",
            start=START,
            end=END,
            quote=QUOTE,
            claim="公司拟收购",
        )
    ]


def test_kind_spec_validation_includes_evidence():
    """信封级接线：dashboard 等无 kind 专属校验的产物也过证据结构层。"""
    assert service.validate_kind_spec("dashboard", spec_with([evidence_item()])) is None
    broken = spec_with([{"source": "s", "stable_id": "i", "as_of": "2026-10-20"}])
    error = service.validate_kind_spec("dashboard", broken)
    assert "evidence[0]" in error and "span" in error
    # 既有 spec（无证据）语义不变
    assert (
        service.validate_kind_spec("dashboard", {"title": "t", "spec": {"panels": []}})
        is None
    )


# ------------------------------------------------------------- 4.2 发布层

def test_verify_passes_and_uses_narrow_adapter():
    store = good_store()
    failures = verify_evidence(
        spec_with([evidence_item()]), store, version_time="2026-10-21T00:00:00"
    )
    assert failures == []
    # 窄口只按 (source, stable_id) 取正文与发布时间
    assert store.calls == [{"source": "cninfo", "stable_id": "cninfo:123"}]


def test_verify_rejects_forged_stable_id():
    """拒收用例①：伪造 stable_id（source+stable_id 未在 info_items 命中）。"""
    failures = verify_evidence(
        spec_with([evidence_item(stable_id="forged:id")]),
        good_store(),
        version_time="2026-10-21T00:00:00",
    )
    assert len(failures) == 1
    assert failures[0].index == 0
    assert failures[0].field == "stable_id"
    assert "未在 info_items 中命中" in failures[0].reason
    assert "evidence[0].stable_id" in failures[0].message()


def test_verify_rejects_misaligned_quote():
    """拒收用例②：错位 quote——span 区间取出的正文与 quote 不符。"""
    shifted = evidence_item(span={"start": START + 1, "end": END + 1, "quote": QUOTE})
    failures = verify_evidence(
        spec_with([shifted]), good_store(), version_time="2026-10-21T00:00:00"
    )
    assert len(failures) == 1
    assert failures[0].field == "span"
    assert "逐字不一致" in failures[0].reason


def test_verify_rejects_out_of_range_span():
    """拒收用例②变体：span 区间越界（end 超出正文长度）。"""
    overflow = evidence_item(span={"start": 0, "end": len(TEXT) + 10, "quote": TEXT})
    failures = verify_evidence(
        spec_with([overflow]), good_store(), version_time="2026-10-21T00:00:00"
    )
    assert len(failures) == 1
    assert failures[0].field == "span"
    assert "区间越界" in failures[0].reason


def test_verify_rejects_future_as_of():
    """拒收用例③：未来 as_of（晚于产物版本时间）。"""
    failures = verify_evidence(
        spec_with([evidence_item(as_of="2026-10-21")]),
        good_store(),
        version_time="2026-10-20T10:00:00",
    )
    assert len(failures) == 1
    assert failures[0].field == "as_of"
    assert "晚于产物版本时间" in failures[0].reason


def test_verify_collects_multiple_failures_with_location():
    """逐条核对、错误定位到具体证据项：两条各有各的失败。"""
    spec = spec_with(
        [
            evidence_item(stable_id="forged:id"),
            evidence_item(as_of="2026-10-21"),
        ]
    )
    failures = verify_evidence(spec, good_store(), version_time="2026-10-20T10:00:00")
    assert [f.index for f in failures] == [0, 1]
    assert [f.field for f in failures] == ["stable_id", "as_of"]


def test_verify_without_store_reports_not_configured():
    failures = verify_evidence(
        spec_with([evidence_item()]), None, version_time="2026-10-21T00:00:00"
    )
    assert len(failures) == 1
    assert failures[0].field == "info_store"
    assert "FD_INFO_URL" in failures[0].reason


def test_verify_store_failure_is_visible_not_silent():
    """适配器故障转为定位到证据项的可见失败（不放行也不崩溃）。"""
    failures = verify_evidence(
        spec_with([evidence_item()]),
        FakeInfoStore(fail=True),
        version_time="2026-10-21T00:00:00",
    )
    assert len(failures) == 1
    assert failures[0].field == "source"
    assert "info-store 查询失败" in failures[0].reason


def test_verify_body_unavailable():
    """条目存在但正文未落库：存在性算过，片段无法复核——可见失败。"""
    from fd_wire_api.artifacts.evidence import ResolvedEvidence

    class NoBodyStore:
        def resolve(self, *, source, stable_id):
            return ResolvedEvidence(text=None, published_at="2026-10-20T09:00:00")

    failures = verify_evidence(
        spec_with([evidence_item()]), NoBodyStore(), version_time="2026-10-21T00:00:00"
    )
    assert len(failures) == 1
    assert failures[0].field == "span"
    assert "正文不可用" in failures[0].reason


def test_verify_structural_error_becomes_block_failure():
    failures = verify_evidence(
        spec_with([{"source": "s"}]), good_store(), version_time="2026-10-21T00:00:00"
    )
    assert len(failures) == 1
    assert failures[0].index == -1
    assert "evidence[0]" in failures[0].message()


# --------------------------------------------------------- 4.3 发布门开关

def test_gate_settings_default_warn(monkeypatch):
    monkeypatch.delenv("EVIDENCE_GATE_MODE", raising=False)
    assert EvidenceGateSettings().mode == GATE_WARN


def test_gate_settings_reads_env_and_falls_back(monkeypatch):
    monkeypatch.setenv("EVIDENCE_GATE_MODE", "ENFORCE")
    assert EvidenceGateSettings().mode == GATE_ENFORCE
    monkeypatch.setenv("EVIDENCE_GATE_MODE", "bogus")
    assert EvidenceGateSettings().mode == GATE_WARN  # 非法值回落 warn


@pytest.fixture()
def db_session():
    """独立内存库（artifacts 两表）会话——发布门服务层用例。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AiBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def gate(monkeypatch):
    """EVIDENCE_GATE_MODE 开关（含 lru_cache 复位），保证进程内不串档。"""
    monkeypatch.delenv("FD_INFO_URL", raising=False)

    def _set(mode):
        monkeypatch.setenv("EVIDENCE_GATE_MODE", mode)
        get_evidence_gate_settings.cache_clear()

    _set(GATE_WARN)  # 默认档
    yield _set
    get_evidence_gate_settings.cache_clear()


def stage_artifact(db_session, spec, *, status="draft"):
    """直插一条带当前版本的产物（版本建立时间即 created_at）。"""
    artifact = Artifact(
        customer_id="cust-001",
        kind="dashboard",
        title=str(spec.get("title", "t")),
        status=status,
        version=1,
        status_history=[],
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactVersion(
            artifact_id=artifact.id, version=1, spec=dict(spec), operator="test"
        )
    )
    db_session.commit()
    db_session.refresh(artifact)
    return artifact


def test_publish_warn_mode_logs_but_does_not_block(db_session, gate, caplog, monkeypatch):
    """4.3 默认 warn：伪造 stable_id 也不阻断发布，只记结构化告警（可查）。"""
    artifact = stage_artifact(db_session, spec_with([evidence_item(stable_id="forged:id")]))
    # 迁移类测试经 alembic/env.py 的 fileConfig 会把既有 logger 置
    # disabled=True（disable_existing_loggers 默认开）——全量套件顺序下
    # 先复位被测 logger（纯测试环境伪影，与 test_demands_notifications 同款）。
    target = logging.getLogger("fd_wire_api.artifacts.service")
    monkeypatch.setattr(target, "disabled", False)
    with caplog.at_level("WARNING", logger="fd_wire_api.artifacts.service"):
        published = service.publish_artifact(
            db_session, artifact, customer_id="cust-001",
            info_store=FakeInfoStore(),  # 空 info_items：必失败
        )
    assert published.status == "published"
    warnings = [r for r in caplog.records if r.message.startswith("evidence_gate warn")]
    assert len(warnings) == 1
    payload = json.loads(warnings[0].message.split(" ", 2)[2])
    assert payload["artifact_id"] == artifact.id
    assert payload["customer_id"] == "cust-001"
    assert payload["index"] == 0
    assert payload["field"] == "stable_id"
    assert "未在 info_items 中命中" in payload["reason"]


def test_publish_enforce_rejects_with_item_location(db_session, gate):
    """enforce 档：校验失败 InvalidArtifactSpec（路由层 422）并指名证据项。"""
    artifact = stage_artifact(db_session, spec_with([evidence_item(stable_id="forged:id")]))
    gate(GATE_ENFORCE)
    with pytest.raises(service.InvalidArtifactSpec) as excinfo:
        service.publish_artifact(
            db_session, artifact, customer_id="cust-001", info_store=FakeInfoStore()
        )
    assert "证据校验未通过" in str(excinfo.value)
    assert "evidence[0].stable_id" in str(excinfo.value)
    db_session.refresh(artifact)
    assert artifact.status == "draft"  # 零半写：状态未迁移


def test_publish_enforce_passes_good_evidence(db_session, gate):
    artifact = stage_artifact(db_session, spec_with([evidence_item()]))
    gate(GATE_ENFORCE)
    published = service.publish_artifact(
        db_session, artifact, customer_id="cust-001", info_store=good_store()
    )
    assert published.status == "published"


def test_publish_off_mode_skips_gate(db_session, gate, caplog):
    """off 档：完全跳过核对（回滚位），坏证据也发布且无告警。"""
    artifact = stage_artifact(db_session, spec_with([evidence_item(stable_id="forged:id")]))
    gate(GATE_OFF)
    with caplog.at_level("WARNING", logger="fd_wire_api.artifacts.service"):
        published = service.publish_artifact(
            db_session, artifact, customer_id="cust-001", info_store=FakeInfoStore()
        )
    assert published.status == "published"
    assert not [r for r in caplog.records if "evidence_gate" in r.message]


def test_publish_without_evidence_unchanged_in_all_modes(db_session, gate):
    """向后兼容：无证据产物在 enforce（服务未配置）下照常发布。"""
    artifact = stage_artifact(db_session, {"title": "t", "spec": {"panels": []}})
    gate(GATE_ENFORCE)
    published = service.publish_artifact(
        db_session, artifact, customer_id="cust-001", info_store=None
    )
    assert published.status == "published"


# ------------------------------------------------- 路由层（422 语义 + 回归）

@pytest.fixture()
def client(monkeypatch, tmp_path, db_session):
    """覆写 get_db 的 TestClient（同 test_artifacts_lifecycle 口径）。"""
    monkeypatch.delenv("FD_INFO_URL", raising=False)
    monkeypatch.delenv("EVIDENCE_GATE_MODE", raising=False)
    get_evidence_gate_settings.cache_clear()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(tmp_path, monkeypatch, customers=[(SUB_CUST, "cust-001")])
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_evidence_gate_settings.cache_clear()
    reset_ai_runtime()


def _stage_draft_with_evidence(db_session, evidence):
    from fd_wire_api.ai.models import AIDraftArtifact
    from fd_wire_api.ai.schemas import ConversationCreate
    from fd_wire_api.ai.service import create_conversation

    conversation = create_conversation(
        db_session, customer_id="cust-001", payload=ConversationCreate(title="证据报告")
    )
    draft = AIDraftArtifact(
        conversation_id=conversation.id,
        customer_id="cust-001",
        kind="dashboard",
        spec=spec_with(evidence),
        status="draft",
    )
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)
    return conversation, draft


def test_route_publish_enforce_returns_422(client, db_session, monkeypatch):
    """enforce + 服务未配置 → 发布 422，detail 定位证据项（HTTP 语义）。"""
    conversation, draft = _stage_draft_with_evidence(
        db_session, [evidence_item(stable_id="forged:id")]
    )
    created = client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conversation.id, "draft_id": draft.id},
        headers=CUST,
    )
    assert created.status_code == 201, created.text
    artifact = created.json()

    monkeypatch.setenv("EVIDENCE_GATE_MODE", "enforce")
    get_evidence_gate_settings.cache_clear()
    resp = client.post(f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST)
    assert resp.status_code == 422
    assert "evidence" in resp.json()["detail"]
    get_evidence_gate_settings.cache_clear()


def test_route_publish_warn_mode_does_not_block(client, db_session):
    """默认 warn 档：同一坏证据 HTTP 发布照常 200（仅告警日志）。"""
    conversation, draft = _stage_draft_with_evidence(
        db_session, [evidence_item(stable_id="forged:id")]
    )
    created = client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conversation.id, "draft_id": draft.id},
        headers=CUST,
    )
    artifact = created.json()
    resp = client.post(f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"


def test_route_edit_saves_structurally_valid_evidence(client, db_session):
    """保存层只做结构校验（离线可存草稿）：合法证据 PUT 200。"""
    conversation, draft = _stage_draft_with_evidence(db_session, [evidence_item()])
    created = client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conversation.id, "draft_id": draft.id},
        headers=CUST,
    )
    artifact = created.json()
    spec_v2 = spec_with([evidence_item(), evidence_item(stable_id="cninfo:456")])
    resp = client.put(
        f"/api/v1/artifacts/{artifact['id']}", json={"spec": spec_v2}, headers=CUST
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["spec"] == spec_v2

    # 结构非法的证据：PUT 422 且定位到证据项（不落版本）
    bad = spec_with([evidence_item(), {"source": "s", "stable_id": "i"}])
    resp = client.put(
        f"/api/v1/artifacts/{artifact['id']}", json={"spec": bad}, headers=CUST
    )
    assert resp.status_code == 422
    assert "evidence[1]" in resp.json()["detail"]
