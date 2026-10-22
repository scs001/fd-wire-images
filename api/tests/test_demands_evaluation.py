"""AI 预评估与人工报价流测试（wire-platform-v1 任务 6.4 → 三态化后收窄）。

wire-ux-consolidation 任务 3.1 起，评估/报价挂接的旧态（evaluating/quoted）
已废除：本文件覆盖——
- 纯函数层（不变）：LLM 结构解析（三种 verdict / 代码块围栏 / 失败形态）、
  客户概要中文映射、合规初筛（法律域关键词，LLM 漏标也补）、提示词组装
- 端点层（收窄后的如实回绝）：/evaluate、/evaluation、/quote 对现行状态
  一律 409（服务端为唯一裁决面）；内部端点鉴权与 404 语义不变
- 客户视图：evaluation_detail / quote_operator / quote_history /
  quote_amount_micro 绝不出现在客户响应（旧单字段保留可查的读路径纪律）
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

# 在导入 app 前把默认引擎指向内存库（与 test_demands_statemachine 同款纪律；
# wire-dedicated-postgres 起该 env 升为元数据库收敛开关——只在导入期间生效，
# 导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]
from fd_wire_api.demands import evaluation as evaluation_module
from fd_wire_api.demands import get_db
from fd_wire_api.demands.db import Base

INTERNAL_TOKEN = "wire-internal-dev"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-cust-001"
CUST = bearer(mint_id_token(SUB_CUST))

#: 客户响应绝不应携带的内部字段。
FORBIDDEN_CUSTOMER_KEYS = {
    "evaluation_detail",
    "quote_operator",
    "quote_history",
    "quote_amount_micro",
    "operator",
}

FEASIBLE_JSON = json.dumps(
    {
        "verdict": "feasible",
        "confidence": 0.85,
        "reasons": ["统计公报与行业协会月报可覆盖", "更新频率 monthly 可满足"],
        "data_source_hints": ["国家统计局", "中国汽车工业协会"],
        "compliance_flags": [],
    },
    ensure_ascii=False,
)

DOUBTFUL_JSON = json.dumps(
    {
        "verdict": "doubtful",
        "confidence": 0.4,
        "reasons": ["源可达但历史口径需确认"],
        "data_source_hints": ["统计局月度公报"],
        "compliance_flags": [],
    },
    ensure_ascii=False,
)

INFEASIBLE_JSON = json.dumps(
    {
        "verdict": "infeasible",
        "confidence": 0.9,
        "reasons": ["无公开源且涉个人敏感信息"],
        "data_source_hints": [],
        "compliance_flags": ["personal-privacy"],
    },
    ensure_ascii=False,
)


def fake_demand(**overrides):
    """纯函数层用的轻量需求单替身（不落库）。"""
    base = dict(
        code="DE-20261010-01",
        title="上海汽车销量月度数据",
        intent="需要上海市 2024 年以来乘用车月度销量与价格带分布",
        entities_hint={"region": "上海市"},
        frequency_expect="monthly",
        linked_indicators=["IND-AUTO-014"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立的内存库 + 覆盖 get_db 的 TestClient（外加平台库身份种子）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(tmp_path, monkeypatch, customers=[(SUB_CUST, "cust-001")])
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ helpers


def submit(client, **overrides) -> dict:
    payload = {
        "description": "需要上海市 2024 年以来乘用车月度销量与价格带分布，用于市场分析",
        "contact_email": "buyer@example.com",
        # 合规门两入参（wire-demand-generation-orchestration 1.1 起必填）
        "source_urls": ["https://www.stats.gov.cn/sj/zxfb/"],
        "public_data_asserted": True,
    }
    payload.update(overrides)
    resp = client.post("/api/v1/demands", json=payload, headers=CUST)
    assert resp.status_code == 201, resp.text
    return resp.json()


def customer_detail(client, demand_id: int) -> dict:
    resp = client.get(f"/api/v1/demands/{demand_id}", headers=CUST)
    assert resp.status_code == 200, resp.text
    return resp.json()


def transition(client, demand_id: int, target: str, **extra):
    return client.post(
        f"/internal/demands/{demand_id}/transition",
        json={"target_status": target, **extra},
        headers=INTERNAL,
    )


# ------------------------------------------------------- LLM 结构解析（纯函数）


@pytest.mark.parametrize(
    ("payload", "verdict"),
    [
        (FEASIBLE_JSON, "feasible"),
        (DOUBTFUL_JSON, "doubtful"),
        (INFEASIBLE_JSON, "infeasible"),
    ],
)
def test_parse_three_verdicts(payload, verdict):
    result = evaluation_module.parse_llm_evaluation(payload)
    assert result is not None
    assert result.verdict.value == verdict
    assert 0.0 <= result.confidence <= 1.0
    assert result.reasons and isinstance(result.reasons, list)


def test_parse_json_inside_code_fence():
    fenced = f"评估如下：\n```json\n{FEASIBLE_JSON}\n```\n以上。"
    result = evaluation_module.parse_llm_evaluation(fenced)
    assert result is not None
    assert result.verdict.value == "feasible"


@pytest.mark.parametrize(
    "content",
    [
        "这个需求看起来还行吧",                                   # 非 JSON
        json.dumps({"verdict": "maybe", "confidence": 0.5}),     # verdict 不合法
        None,                                                    # 空输出
    ],
    ids=["non-json", "invalid-verdict", "empty-content"],
)
def test_parse_failure_returns_none(content):
    assert evaluation_module.parse_llm_evaluation(content) is None


def test_unavailable_evaluation_is_honest_doubtful():
    result = evaluation_module.unavailable_evaluation("token 网关未配置")
    assert result.verdict.value == "doubtful"
    assert result.confidence == 0.0
    assert any("AI 评估不可用" in r for r in result.reasons)


# ------------------------------------------------------- 概要映射（纯函数）


def test_summarize_maps_verdict_label_and_first_reason():
    result = evaluation_module.parse_llm_evaluation(FEASIBLE_JSON)
    summary = evaluation_module.summarize(result)
    assert summary.startswith("可行：")
    assert "统计公报" in summary
    assert summary.endswith("。")


def test_summarize_falls_back_to_default_one_liner():
    result = evaluation_module.EvaluationResult(
        verdict=evaluation_module.EvaluationVerdict.infeasible,
        confidence=0.5,
        reasons=[],
    )
    summary = evaluation_module.summarize(result)
    assert summary.startswith("不可行：")


# ------------------------------------------------------- 合规初筛（纯函数）


def test_legal_domain_intent_gets_review_flag():
    demand = fake_demand(
        title="涉诉企业名单",
        intent="需要全国企业裁判文书与诉讼记录，按被告企业聚合",
    )
    assert evaluation_module.compliance_pre_screen(demand) == [
        evaluation_module.LEGAL_DOMAIN_REVIEW_FLAG
    ]


def test_non_legal_domain_no_flag():
    assert evaluation_module.compliance_pre_screen(fake_demand()) == []


def test_build_messages_carries_demand_payload():
    messages = evaluation_module.build_messages(fake_demand())
    assert messages[0]["role"] == "system"
    assert "verdict" in messages[0]["content"]
    user = json.loads(messages[1]["content"].split("请评估以下数据需求：\n", 1)[1])
    assert user["intent"].startswith("需要上海市")
    assert user["linked_indicators"] == ["IND-AUTO-014"]


# ------------------------------------------------------- 旧流端点如实回绝（409）


@pytest.mark.parametrize("target", ["submitted", "in_progress"])
def test_evaluation_endpoints_409_on_active_states(client, target):
    """旧八态评估流（submitted→evaluating 自动接单 / evaluating 重跑）已废除：
    现行两态下 /evaluate 与 /evaluation 一律 409，评估字段不被写入。"""
    did = submit(client)["id"]
    if target == "in_progress":
        assert transition(client, did, "in_progress", by="ops-001").status_code == 200

    resp = client.post(f"/internal/demands/{did}/evaluate", headers=INTERNAL)
    assert resp.status_code == 409, resp.text
    resp = client.post(
        f"/internal/demands/{did}/evaluation",
        json={"summary": "试图写概要", "by": "ai"},
        headers=INTERNAL,
    )
    assert resp.status_code == 409, resp.text
    detail = customer_detail(client, did)
    assert detail["evaluation_summary"] is None  # 未写入
    assert detail["status"] == target


@pytest.mark.parametrize("target", ["submitted", "in_progress"])
def test_quote_endpoint_409_on_active_states(client, target):
    """报价不进三态状态机（走定价页自助或线下沟通）：/quote 一律 409，
    不落任何报价字段。"""
    did = submit(client)["id"]
    if target == "in_progress":
        assert transition(client, did, "in_progress", by="ops-001").status_code == 200

    resp = client.post(
        f"/internal/demands/{did}/quote",
        json={
            "amount_micro": 1_200_000_000,
            "confirmed_by_operator": True,
            "operator": "op-007",
        },
        headers=INTERNAL,
    )
    assert resp.status_code == 409, resp.text
    internal = client.get(f"/internal/demands/{did}", headers=INTERNAL).json()
    assert internal["quote_amount"] is None
    assert internal["quote_history"] == []
    assert internal["status"] == target


# ------------------------------------------------------- 客户视图字段分工


def test_customer_view_hides_internal_fields(client):
    did = submit(client)["id"]
    assert transition(client, did, "in_progress", by="ops-001").status_code == 200

    body = customer_detail(client, did)
    assert not (set(body) & FORBIDDEN_CUSTOMER_KEYS)

    raw = client.get(f"/api/v1/demands/{did}", headers=CUST).text
    for key in ("evaluation_detail", "quote_operator", "quote_history", "amount_micro"):
        assert key not in raw


# ------------------------------------------------------- 内部视图鉴权


def test_internal_endpoints_require_token(client):
    did = submit(client)["id"]
    # 缺 token：内部视图 401、评估触发 401
    assert client.get(f"/internal/demands/{did}").status_code == 401
    assert client.post(f"/internal/demands/{did}/evaluate").status_code == 401
    # 不存在的需求单一律 404
    assert client.get("/internal/demands/99999", headers=INTERNAL).status_code == 404
    assert (
        client.post("/internal/demands/99999/evaluate", headers=INTERNAL).status_code == 404
    )
