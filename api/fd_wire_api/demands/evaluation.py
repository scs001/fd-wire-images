"""AI 预评估服务（wire-platform-v1 任务 6.4：需求单的 AI 预评估与人工报价流）。

wire-ux-consolidation 任务 3.1 起三态化：评估挂接的 evaluating 态已废除，
端点流程（transition 自动评估 / 独立触发 / 评估写入）对现行状态一律 409。
本模块保留纯函数（LLM 结构解析 / verdict 中文映射 / 合规初筛 / 提示词
组装）与旧单 evaluation 字段的读路径——解析与初筛逻辑将来可复用于
in_progress 观测挂点（design D1：为萬星闭环预留种子）。

评估产物（结构化 JSON，由 LLM 依需求意图产出）：
    {verdict: feasible|doubtful|infeasible, confidence, reasons[],
     data_source_hints[], compliance_flags[]}

诚实纪律（绝不编造结论）：
- LLM 不可用（网关错误/未配置）或输出无法解析为合法结构时，
  verdict 一律落 ``doubtful``，reasons 如实注明「AI 评估不可用，转人工评估」；
- 合规初筛在本地关键词层独立执行：涉法律域数据 → compliance_flags
  追加 ``legal-domain-review``（LLM 漏标也会补上）。

落库：
- ``evaluation_summary``：客户可见概要 = verdict 中文映射（可行/存疑/不可行）
  + 一句话（取首条 reason）；
- ``evaluation_detail``：内部全文 JSON（verdict/confidence/reasons/hints/
  flags + evaluated_by/llm_available/evaluated_at），客户侧响应绝不携带。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import service
from .models import Demand
from .statemachine import DemandStatus


class EvaluationVerdict(str, Enum):
    """预评估结论三态（对应状态机里的人工后续动作）。"""

    feasible = "feasible"      # 可行 → 可推进人工报价
    doubtful = "doubtful"      # 存疑 → 人工复核 / AI 不可用兜底也落此态
    infeasible = "infeasible"  # 不可行 → 建议 rejected（仍需人工决断）


#: verdict → 客户可见中文映射。
VERDICT_LABELS: dict[EvaluationVerdict, str] = {
    EvaluationVerdict.feasible: "可行",
    EvaluationVerdict.doubtful: "存疑",
    EvaluationVerdict.infeasible: "不可行",
}

#: 无 reason 时的兜底一句话（概要仍是「verdict 中文映射+一句话」形态）。
_DEFAULT_ONE_LINERS: dict[EvaluationVerdict, str] = {
    EvaluationVerdict.feasible: "初步判断存在可覆盖需求的公开数据源",
    EvaluationVerdict.doubtful: "数据源可达性或口径待人工复核",
    EvaluationVerdict.infeasible: "未找到可行数据源，建议人工复核后拒绝",
}

#: 合规初筛：法律域数据标记（本地关键词层，独立于 LLM）。
LEGAL_DOMAIN_REVIEW_FLAG = "legal-domain-review"

#: 法律域关键词（初筛宽一点没关系——flag 只触发人工合规复核，不直接拒绝）。
_LEGAL_DOMAIN_KEYWORDS: tuple[str, ...] = (
    "裁判文书",
    "判决书",
    "裁定书",
    "诉讼",
    "开庭公告",
    "庭审",
    "案号",
    "卷宗",
    "被执行人",
    "失信名单",
    "司法拍卖",
    "立案信息",
    "行政处罚决定",
    "律师执业",
    "法律诉讼",
)

#: LLM verdict 字段的宽容别名（模型偶尔回中文/大小写漂移）。
_VERDICT_ALIASES: dict[str, EvaluationVerdict] = {
    "feasible": EvaluationVerdict.feasible,
    "可行": EvaluationVerdict.feasible,
    "doubtful": EvaluationVerdict.doubtful,
    "存疑": EvaluationVerdict.doubtful,
    "infeasible": EvaluationVerdict.infeasible,
    "不可行": EvaluationVerdict.infeasible,
}

_LLM_CLIENT_FACTORY: Callable[[], Any] | None = None

_SYSTEM_PROMPT = (
    "你是数据需求可行性评估助手。请严格只输出一个 JSON 对象"
    "（不要 markdown 代码块、不要任何多余文字），字段如下：\n"
    'verdict: "feasible" | "doubtful" | "infeasible"（可行/存疑/不可行）\n'
    "confidence: 0 到 1 之间的小数，表示对该结论的把握\n"
    "reasons: 字符串数组，评估依据（中文，逐条）\n"
    "data_source_hints: 字符串数组，建议的数据源线索（如机构/站点/报告名）\n"
    "compliance_flags: 字符串数组，合规风险标记；"
    '涉法律域数据（裁判文书、诉讼、司法执行等）必须包含 "legal-domain-review"\n'
    "纪律：只依据需求描述判断；依据不足时给 doubtful 并在 reasons 说明缺什么；"
    "绝不编造数据源、绝不编造结论。"
)


class EvaluationResult(BaseModel):
    """LLM 结构化预评估的归一化形态。"""

    verdict: EvaluationVerdict
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    data_source_hints: list[str] = Field(default_factory=list)
    compliance_flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- LLM 出口


def set_llm_client_factory(factory: Callable[[], Any] | None) -> None:
    """注入 LLM 客户端工厂（测试用；传 None 恢复默认）。"""
    global _LLM_CLIENT_FACTORY
    _LLM_CLIENT_FACTORY = factory


def reset_llm_client_factory() -> None:
    """清除注入，恢复默认出口（token 网关；未配置则返回 None）。"""
    global _LLM_CLIENT_FACTORY
    _LLM_CLIENT_FACTORY = None


def get_llm_client() -> Any:
    """解析当前 LLM 出口（满足 ai.protocols.LlmClient 协议或 None）。

    优先级：测试注入的工厂 > token 网关（要求 ``TOKEN_GATEWAY_KEY`` 已配置）
    > None（未配置即不可用，评估走 doubtful 兜底，绝不打真网碰运气）。
    """
    if _LLM_CLIENT_FACTORY is not None:
        return _LLM_CLIENT_FACTORY()
    if not os.getenv("TOKEN_GATEWAY_KEY", "").strip():
        return None
    from ..ai.llm import TokenGatewayLlmClient

    return TokenGatewayLlmClient()


# ---------------------------------------------------------------- 合规初筛


def compliance_pre_screen(demand: Demand) -> list[str]:
    """本地关键词合规初筛：法律域数据 → ``legal-domain-review``。

    独立于 LLM 执行——即便模型漏标/不可用，法律域复核标记也必须留下。
    """
    corpus = " ".join(
        (
            demand.title or "",
            demand.intent or "",
            json.dumps(demand.entities_hint or {}, ensure_ascii=False),
        )
    )
    if any(kw in corpus for kw in _LEGAL_DOMAIN_KEYWORDS):
        return [LEGAL_DOMAIN_REVIEW_FLAG]
    return []


# ---------------------------------------------------------------- 提示词与解析


def build_messages(demand: Demand) -> list[dict]:
    """组装评估对话：system（结构契约+诚实纪律）+ user（需求原文）。"""
    user_payload = {
        "title": demand.title,
        "intent": demand.intent,
        "entities_hint": demand.entities_hint,
        "frequency_expect": demand.frequency_expect,
        "linked_indicators": demand.linked_indicators or [],
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "请评估以下数据需求：\n"
            + json.dumps(user_payload, ensure_ascii=False, indent=2),
        },
    ]


def _extract_json_object(text: str) -> dict | None:
    """从 LLM 文本中宽容地抽出首个 JSON 对象（容忍代码块围栏与前后废话）。"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            pass  # 围栏内不合法则退回全文扫描
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _coerce_str_list(value: Any) -> list[str]:
    """reasons/hints/flags 的宽容归一：单字符串→单元素列表，逐项 str 化。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_llm_evaluation(content: str | None) -> EvaluationResult | None:
    """把 LLM 输出解析为 :class:`EvaluationResult`；不合法返回 None。

    只认 verdict 可映射、结构可归一的输出——解析失败交由上层走
    「AI 评估不可用」兜底，绝不猜测或补造结论。
    """
    obj = _extract_json_object(content or "")
    if obj is None:
        return None
    verdict_raw = obj.get("verdict")
    verdict = _VERDICT_ALIASES.get(str(verdict_raw).strip().lower())
    if verdict is None:
        # 别名表大小写不敏感后再试原文（中文无大小写，直接命中表）
        verdict = _VERDICT_ALIASES.get(str(verdict_raw).strip())
    if verdict is None:
        return None
    try:
        confidence = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(1.0, max(0.0, confidence))
    return EvaluationResult(
        verdict=verdict,
        confidence=confidence,
        reasons=_coerce_str_list(obj.get("reasons")),
        data_source_hints=_coerce_str_list(obj.get("data_source_hints")),
        compliance_flags=_coerce_str_list(obj.get("compliance_flags")),
    )


def unavailable_evaluation(why: str) -> EvaluationResult:
    """AI 不可用时的诚实兜底：doubtful + 如实注明转人工，绝不编造。"""
    return EvaluationResult(
        verdict=EvaluationVerdict.doubtful,
        confidence=0.0,
        reasons=[f"AI 评估不可用，转人工评估（{why}）"],
        data_source_hints=[],
        compliance_flags=[],
    )


def summarize(result: EvaluationResult) -> str:
    """客户可见概要：verdict 中文映射 + 一句话（首条 reason）。"""
    one_liner = ""
    for reason in result.reasons:
        if reason.strip():
            one_liner = reason.strip().rstrip("。；;.")
            break
    if not one_liner:
        one_liner = _DEFAULT_ONE_LINERS[result.verdict]
    return f"{VERDICT_LABELS[result.verdict]}：{one_liner}。"


# ---------------------------------------------------------------- 主流程


def _run_llm_evaluation(demand: Demand, client: Any) -> tuple[EvaluationResult, dict]:
    """调一次 LLM 并解析；任何失败都回落到不可用兜底（元数据如实记录）。"""
    if client is None:
        return unavailable_evaluation("token 网关未配置"), {
            "llm_available": False,
            "evaluated_by": "ai-fallback",
            "unavailable_reason": "token 网关未配置（TOKEN_GATEWAY_KEY 为空）",
        }
    from ..ai.llm import LlmGatewayError
    from ..ai.protocols import LlmAssistantMessage

    try:
        message: LlmAssistantMessage = client.complete(build_messages(demand), [])
        result = parse_llm_evaluation(message.text())
    except LlmGatewayError as exc:
        return unavailable_evaluation(str(exc)), {
            "llm_available": False,
            "evaluated_by": "ai-fallback",
            "unavailable_reason": f"LLM 出口异常：{exc}",
        }
    except Exception as exc:  # 未知出口异常同样如实兜底
        return unavailable_evaluation(str(exc)), {
            "llm_available": False,
            "evaluated_by": "ai-fallback",
            "unavailable_reason": f"LLM 出口异常：{exc}",
        }
    if result is None:
        # 出口可达但输出不可用：结论仍走兜底——llm_available 如实记 True，
        # evaluated_by 记 ai-fallback（结论不是模型给的）。
        return unavailable_evaluation("LLM 输出无法解析为结构化评估"), {
            "llm_available": True,
            "evaluated_by": "ai-fallback",
            "unavailable_reason": "LLM 输出无法解析为结构化评估（verdict/JSON 不合法）",
        }
    return result, {
        "llm_available": True,
        "evaluated_by": "ai",
        "unavailable_reason": None,
    }


def evaluate_demand(
    db: Session, demand: Demand, *, llm: Any = None, by: str = "ai"
) -> Demand:
    """对一条需求单跑 AI 预评估并落库（summary 客户可见 / detail 内部）。

    状态规则沿用 :func:`fd_wire_api.demands.service.write_evaluation`：
    submitted 自动接单进 evaluating；evaluating 可更新；其余状态 409。
    """
    flags = compliance_pre_screen(demand)
    client = llm if llm is not None else get_llm_client()
    result, meta = _run_llm_evaluation(demand, client)
    for flag in flags:  # 本地初筛结果必留（LLM 漏标也补上）
        if flag not in result.compliance_flags:
            result.compliance_flags.append(flag)
    detail = {
        "verdict": result.verdict.value,
        "verdict_label": VERDICT_LABELS[result.verdict],
        "confidence": result.confidence,
        "reasons": result.reasons,
        "data_source_hints": result.data_source_hints,
        "compliance_flags": result.compliance_flags,
        "evaluated_by": meta["evaluated_by"],
        "llm_available": meta["llm_available"],
        "unavailable_reason": meta["unavailable_reason"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    return service.write_evaluation(
        db, demand, summary=summarize(result), by=by, detail=detail
    )


def maybe_auto_evaluate(db: Session, demand: Demand) -> Demand:
    """迁移到 evaluating 后的自动预评估钩子（旧八态工作流遗留，已无调用方）。

    wire-ux-consolidation 3.1 起 evaluating 不可达，本钩子恒为直通返回；
    保留是为旧调用方渐进退场与将来 in_progress 观测挂点复用。
    """
    try:
        if DemandStatus(demand.status) is not DemandStatus.evaluating:
            return demand
        return evaluate_demand(db, demand, by="ai")
    except Exception:
        return demand
