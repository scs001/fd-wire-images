"""动作闸（wire-copilot-action-gate）：「AI 提议、人确认、系统执行、事后审计」。

与壹座侧动作闸（paas ``add-line-boards-and-action-gate``）同构，语义以它为
准：注册表声明风险 → 风险决定确认档位（write→inline、destructive/cost→
second、publish→async 24h fail-closed）→ 单一执行路径 → 审计台账 → 回执
不冒充结果。柏讯侧更强的两点：

- **不可绕过是结构性的**：副驾循环（:mod:`.loop`）的工具面里只有读工具与
  ``propose_action``——写工具在代码路径上不可达，不依赖运行时判断。
- **执行体直调 service 层**：柏讯自己就是后端，执行不经 HTTP、不经 MCP、
  不经对话循环的 ToolExecutor（cell 探索除外，那是读）。

多队语义：提议落库时 ``customer_id`` 即活跃队命名空间（身份解析在
console/auth）；审批角色 = 该队 owner/editor，viewer 对写操作一律拒绝
（spec「viewer 写操作被拒」——propose 的 preflight 与 approve 双重校验）。

合规红线（spec「需求单提议不代填合规声明」）：``wire.demand.submit`` 的
公开源 URL 与公开数据声明**只能**来自客户在确认卡上的输入——模型给的
任何合规取值在 propose 时即被丢弃，approve 缺字段即 422。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    PROPOSAL_APPROVAL_TTL_HOURS,
    PROPOSAL_STATE_EXECUTED,
    PROPOSAL_STATE_EXPIRED,
    PROPOSAL_STATE_FAILED,
    PROPOSAL_STATE_PENDING_APPROVAL,
    PROPOSAL_STATE_PENDING_CONFIRM,
    PROPOSAL_STATE_REJECTED,
    AIActionAudit,
    AIActionProposal,
    utc_now_naive,
)
from .tools import ARTIFACT_TOOL_KINDS, stage_artifact, validate_artifact_args

# ── 风险与档位 ──────────────────────────────────────────────────────────────

RISK_WRITE = "write"
RISK_DESTRUCTIVE = "destructive"
RISK_COST = "cost"
RISK_PUBLISH = "publish"

#: 风险 → 确认档位（与壹座侧动作闸逐字一致的映射；read 不入闸——读是直通工具）
RISK_TO_TIER: dict[str, str] = {
    RISK_WRITE: "inline",
    RISK_DESTRUCTIVE: "second",
    RISK_COST: "second",
    RISK_PUBLISH: "async",
}

#: 审批角色门：async 档仅 owner/editor（团队模型无 admin，editor 即管理员档）
APPROVAL_ROLES = ("owner", "editor")
#: 写操作禁发角色（全局 preflight + approve 双重校验）
WRITE_DENIED_ROLE = "viewer"


class ActionNotRegistered(LookupError):
    """动作不在注册表——闸拒绝，不调用任何底层能力（spec 场景「未注册动作被拒绝」）。"""


class PreflightError(Exception):
    """preflight 失败：阻止提议进入确认档位，消息须含可操作的修复指引。"""


class ComplianceMissing(Exception):
    """合规字段缺失/不合法（需求单类动作）。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("合规声明未完成：" + "、".join(missing))


class ExecutionError(Exception):
    """执行失败：如实保留原因（HTTPException 取 detail，不取空 repr）。"""


def _detail_of(exc: Exception) -> str:
    """底层异常 → 客户可读文案（HTTPException 只取 detail）。"""
    if isinstance(exc, HTTPException):
        detail = exc.detail
        return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    return str(exc)


# ── 上下文与会话束 ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActorContext:
    """提议/审批者身份（require_console_actor 解析所得）。"""

    customer_id: str
    sub: str
    role: str


@dataclass
class GateSessions:
    """执行器的跨切片会话束：各库各取各的（answer.py 双库纪律同款）。

    - ``ai``：AI 切片库（提议/审计/草稿/产物——artifacts 表挂 AiBase 同库）。
    - ``datasets``：datasets 库（数据集/上传/市场元数据）。
    - ``demands``：需求单库。
    - ``materialization``：物化库（materialized_artifacts）。
    - ``packs``：源包订阅库（pack_subscriptions，元数据库；add-pack-subscribe-actions）。
    """

    ai: Session
    datasets: Session
    demands: Session
    materialization: Session
    packs: Session

    def close(self) -> None:
        for session in (self.ai, self.datasets, self.demands, self.materialization, self.packs):
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 尽力关，不掩盖主流程
                pass


@dataclass(frozen=True)
class ActionDef:
    """注册表条目：风险决定档位；preflight 先于确认；execute 是唯一执行体。"""

    id: str
    risk: str
    impact: Callable[[dict[str, Any]], str]
    preflight: Callable[[ActorContext, GateSessions, dict[str, Any]], None]
    execute: Callable[[ActorContext, GateSessions, dict[str, Any]], str]
    #: 提议参数脱敏摘要（默认取标量字段；全文 spec 不落提议表）
    summarize: Callable[[dict[str, Any]], str] | None = None
    #: 需求单类：合规字段只能来自 approve 载荷，模型给的取值一律丢弃
    needs_compliance: bool = False

    @property
    def tier(self) -> str:
        """确认档位（风险决定；与 RISK_TO_TIER 单一映射）。"""
        return RISK_TO_TIER[self.risk]


#: 动作 id ↔ 操作面工具名（wire-ops-mcp / wire-build-mcp 的 tool-surface.json
#: ``tool_risk`` 对拍锚点，design D10）：两闸共用同一份风险声明的映射点。
#: 无对应工具的动作（share.create 等）不在此表——它们的风险只由本注册表声明。
ACTION_TO_TOOL_NAME: dict[str, str] = {
    "wire.demand.submit": "wire_demand_submit",
    "wire.market.subscribe": "wire_market_subscribe",
    "wire.market.unsubscribe": "wire_market_unsubscribe",
    "wire.pack.subscribe": "wire_pack_subscribe",
    "wire.pack.unsubscribe": "wire_pack_unsubscribe",
    "wire.artifact.publish": "wire_artifact_publish",
    "wire.artifact.archive": "wire_artifact_archive",
    "wire.dataset.correct": "wire_dataset_correct",
    "wire.dataset.correction.revert": "wire_dataset_correction_revert",
}


# ── 通用 preflight 原语 ─────────────────────────────────────────────────────

def _require_engine(ctx: ActorContext) -> None:
    """构建类动作的引擎门：免费档（api 层存储）不开构建（spec 场景
    「免费档构建动作被拦截并说明」）。"""
    from ..datasets import service as datasets_service

    tier = datasets_service.storage_tier_for(customer_id=ctx.customer_id)
    if tier == datasets_service.TIER_API:
        raise PreflightError(
            "当前档位为免费档（api 层存储），构建类动作需要开通数据处理引擎"
            "（cell）——请在套餐升级后重试，或联系平台开通。"
        )


def _deny_viewer(ctx: ActorContext) -> None:
    if ctx.role == WRITE_DENIED_ROLE:
        raise PreflightError(
            "团队成员权限不足（viewer 只读）——写操作需由 owner 或 editor 发起。"
        )


def _own_dataset(sess: GateSessions, ctx: ActorContext, dataset_id: str):
    """数据集归属校验：不存在/他队一律「不存在」（不暴露他人对象存在性）。"""
    from ..datasets.models import Dataset

    dataset = sess.datasets.get(Dataset, dataset_id)
    if dataset is None or dataset.customer_id != ctx.customer_id:
        raise PreflightError(f"数据集 {dataset_id} 不存在或不属于当前团队。")
    return dataset


def _own_artifact(sess: GateSessions, ctx: ActorContext, artifact_id: int):
    from ..artifacts.service import ArtifactNotFound, get_customer_artifact

    try:
        return get_customer_artifact(
            sess.ai, artifact_id=artifact_id, customer_id=ctx.customer_id
        )
    except ArtifactNotFound as exc:
        raise PreflightError(
            f"产物 {artifact_id} 不存在或不属于当前团队。"
        ) from exc


def _own_draft(sess: GateSessions, ctx: ActorContext, draft_id: int):
    from .models import AIDraftArtifact

    draft = sess.ai.get(AIDraftArtifact, draft_id)
    if draft is None or draft.customer_id != ctx.customer_id:
        raise PreflightError(f"草稿 {draft_id} 不存在或不属于当前团队。")
    return draft


# ── v1 动作执行体（design D3 冻结表，十一条）────────────────────────────────

#: kind 短名 → 产物工具名（stage_artifact 的入参键；五类共用一套校验）
_KIND_TO_TOOL: dict[str, str] = {kind: tool for tool, kind in ARTIFACT_TOOL_KINDS.items()}

_KIND_CN: dict[str, str] = {
    "dashboard": "仪表盘",
    "composite_indicator": "组合指标",
    "research_report": "研究报告",
    "alert_rule": "告警规则",
    "refresh_schedule": "定时刷新",
}


def _pf_draft_create(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    _deny_viewer(ctx)
    _require_engine(ctx)
    kind = args.get("kind")
    tool = _KIND_TO_TOOL.get(kind) if isinstance(kind, str) else None
    if tool is None:
        raise PreflightError(
            f"kind 必须是五类产物之一：{'、'.join(sorted(_KIND_TO_TOOL))}——收到 {kind!r}"
        )
    error = validate_artifact_args(tool, {"title": args.get("title"), "spec": args.get("spec")})
    if error:
        raise PreflightError(error)


def _exec_draft_create(
    ctx: ActorContext, sess: GateSessions, args: dict[str, Any]
) -> str:
    from .models import AIDraftArtifact

    # 会话 id 优先取提议行的注入（approve 与提议不在同一请求，ContextVar
    # 传不过去）；循环内同请求路径经 ContextVar 兜底。
    conversation_id = args.get("_conversation_id") or _current_conversation_id()
    result = stage_artifact(
        sess.ai,
        conversation_id=conversation_id,
        customer_id=ctx.customer_id,
        tool_name=_KIND_TO_TOOL[args["kind"]],
        args={"title": args.get("title"), "spec": args.get("spec")},
    )
    if not result.get("ok"):
        raise ExecutionError(str(result.get("error") or "草稿暂存失败"))
    draft = sess.ai.get(AIDraftArtifact, result.get("artifact_id"))
    title = args.get("title") if isinstance(args.get("title"), str) else ""
    return (
        f"已创建{_KIND_CN.get(args['kind'], args['kind'])}草稿「{title}」"
        f"（草稿 #{result.get('artifact_id')}，可在会话草稿区预览并转正）"
    )


def _current_conversation_id() -> int:
    value = _CONVERSATION.get()
    if value is None:
        raise PreflightError("提议缺少会话上下文（conversation_id）——请从副驾会话内发起。")
    return value


from contextvars import ContextVar  # noqa: E402 - 置于使用处之后，避免打断执行体段落

_CONVERSATION: ContextVar[int | None] = ContextVar("ai_action_conversation", default=None)


def set_conversation_id(conversation_id: int | None) -> None:
    """路由/循环层在提议前设定会话上下文（每请求设置，用完即清）。"""
    _CONVERSATION.set(conversation_id)


def _summarize_scalars(args: dict[str, Any]) -> str:
    """参数脱敏摘要：只取顶层标量（截断），嵌套结构只记键名——全文 spec
    不落提议表（正本在草稿表），凭据永不入表。"""
    parts: list[str] = []
    for key, value in args.items():
        if isinstance(value, (str, int, float, bool)):
            text = str(value)
            parts.append(f"{key}={text[:120]}")
        elif isinstance(value, dict):
            parts.append(f"{key}={{{','.join(sorted(map(str, value.keys())))}}}")
        elif isinstance(value, list):
            parts.append(f"{key}[{len(value)}]")
    return "; ".join(parts) or "(无参数)"


#: 执行结果持久化前的凭据脱敏（spec「审计不含密钥」：响应不含 token/
#: 密钥或其片段）。分享令牌只在 approve 的即时响应里出现一次；提议表与
#: 历史回放只留 ``t=***``——与首登密钥 ``key_once`` 同款一次性展示纪律。
_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"([?&]t=)[^&\s）)]+"), r"\1***"),
    (re.compile(r"\bwgk-[A-Za-z0-9_-]+"), "wgk-***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]+=*", re.IGNORECASE), r"\1***"),
)


def redact_credentials(text: str) -> str:
    """把执行结果里的分享令牌/数据面 Key/Bearer 头替换为 ``***``。"""
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _pf_dataset_create(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    _deny_viewer(ctx)
    name = args.get("name")
    source = args.get("source")
    if not isinstance(name, str) or not name.strip():
        raise PreflightError("建集动作必须带非空 name。")
    if not isinstance(source, dict) or source.get("kind") not in ("upload", "declared"):
        raise PreflightError(
            "source.kind 必须是 upload（引用既有上传件 upload_id）或 declared"
            "（列声明空集，需数据引擎）——本动作不接收文件内容。"
        )
    if source.get("kind") == "declared":
        _require_engine(ctx)
    if source.get("kind") == "upload" and not source.get("upload_id"):
        raise PreflightError("upload 来源必须带 upload_id（客户先在「我的数据」完成上传）。")


def _exec_dataset_create(
    ctx: ActorContext, sess: GateSessions, args: dict[str, Any]
) -> str:
    from ..datasets import service as datasets_service
    from ..datasets.routes import (
        _create_from_declared,
        _create_from_upload,
        _create_from_upload_local,
    )
    from ..datasets.ingest import get_engine_client_factory
    from ..datasets.schemas import DatasetCreateIn

    payload = DatasetCreateIn(name=args["name"], source=args["source"])
    tier = datasets_service.storage_tier_for(customer_id=ctx.customer_id)
    try:
        if tier == datasets_service.TIER_API:
            datasets_service.ensure_free_dataset_quota(sess.datasets, customer_id=ctx.customer_id)
        else:
            datasets_service.ensure_new_dataset_room(sess.datasets, customer_id=ctx.customer_id)
        if payload.source.kind == "upload":
            if tier == datasets_service.TIER_API:
                created = _create_from_upload_local(
                    sess.datasets,
                    customer_id=ctx.customer_id,
                    name=payload.name,
                    source=payload.source,
                )
            else:
                created = _create_from_upload(
                    sess.datasets,
                    customer_id=ctx.customer_id,
                    name=payload.name,
                    source=payload.source,
                    engine_factory=get_engine_client_factory(),
                )
        else:
            created = _create_from_declared(
                sess.datasets,
                customer_id=ctx.customer_id,
                name=payload.name,
                source=payload.source,
                engine_factory=get_engine_client_factory(),
            )
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    except datasets_service.QuotaExceeded as exc:
        raise ExecutionError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 执行失败如实留原因
        raise ExecutionError(_detail_of(exc)) from exc
    return f"已创建数据集「{created.name}」（{created.id}，{created.storage_tier} 层存储）"


def _pf_promote(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    from ..materialization.specs import MATERIALIZABLE_KINDS

    _deny_viewer(ctx)
    draft_id = args.get("draft_id")
    if not isinstance(draft_id, int):
        raise PreflightError("转正动作必须带草稿 id（draft_id）。")
    draft = _own_draft(sess, ctx, draft_id)
    if draft.kind not in MATERIALIZABLE_KINDS:
        raise PreflightError(
            f"产物类型 {draft.kind!r} 暂不支持物化（支持：{sorted(MATERIALIZABLE_KINDS)}）"
        )


def _exec_promote(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    """草稿转正 = 正式产物建档 + 物化登记（spec「转正后出现在产物页」）。

    两条既有服务都是转正的组成部分而非替代关系：``import_from_draft``
    产生 artifacts 行（产物页/发布/分享的对象），``promote_draft`` 登记
    物化与调度（计费读取的来源）。只登记物化会让「已转正」的草稿在产物页
    无处可寻——真机验收 6.2 抓到的缺口。
    """
    from sqlalchemy import select

    from ..artifacts.service import (
        ArtifactNotFound,
        InvalidArtifactSpec,
        import_from_draft,
    )
    from ..materialization.models import MaterializedArtifact
    from ..materialization.service import promote_draft

    draft = _own_draft(sess, ctx, args["draft_id"])
    conversation_id = args.get("_conversation_id") or _current_conversation_id()
    # 重复转正先拦在建档之前：登记有唯一约束，建档没有——先查一行免得
    # 二次执行留下两份 artifacts 行后再失败。
    already = sess.materialization.execute(
        select(MaterializedArtifact).where(
            MaterializedArtifact.artifact_id == draft.id
        )
    ).scalar_one_or_none()
    if already is not None:
        raise ExecutionError(
            f"草稿 #{draft.id} 已转正过（物化 #{already.id}），不可重复转正。"
        )
    try:
        artifact = import_from_draft(
            sess.ai,
            customer_id=ctx.customer_id,
            conversation_id=conversation_id,
            draft_id=draft.id,
        )
        row = promote_draft(
            sess.materialization, draft=draft, schedule=args.get("schedule")
        )
    except (ArtifactNotFound, InvalidArtifactSpec) as exc:
        raise ExecutionError(str(exc)) from exc
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    return (
        f"草稿 #{draft.id}（{_KIND_CN.get(draft.kind, draft.kind)}）已转正为正式产物 "
        f"#{artifact.id}「{artifact.title}」（物化 #{row.id}），产物页可见"
    )


def _pf_artifact_id(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    _deny_viewer(ctx)
    if not isinstance(args.get("artifact_id"), int):
        raise PreflightError("动作必须带产物 id（artifact_id）。")
    _own_artifact(sess, ctx, args["artifact_id"])


def _exec_publish(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..artifacts.service import publish_artifact

    artifact = _own_artifact(sess, ctx, args["artifact_id"])
    try:
        published = publish_artifact(sess.ai, artifact, customer_id=ctx.customer_id)
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    return f"产物「{published.title}」已发布（版本 v{published.version}，队内可见）"


def _pf_share_create(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    from ..artifacts.service import RECORD_VIEW_KIND, current_spec
    from ..share.routes import SHARE_KIND_PATHS

    _deny_viewer(ctx)
    if not isinstance(args.get("artifact_id"), int):
        raise PreflightError("分享动作必须带产物 id（artifact_id）。")
    artifact = _own_artifact(sess, ctx, args["artifact_id"])
    if artifact.kind not in SHARE_KIND_PATHS:
        raise PreflightError(
            f"产物 {artifact.id} 是 {artifact.kind}，仅仪表盘/表单/单记录页/文档支持分享链接。"
        )
    if artifact.status != "published":
        raise PreflightError(
            f"产物 {artifact.id} 当前状态 {artifact.status}，仅已发布（published）的产物可分享——请先发布。"
        )
    if artifact.kind == RECORD_VIEW_KIND:
        spec = current_spec(sess.ai, artifact)
        inner = spec.get("spec") if isinstance(spec, dict) else None
        bound = (
            isinstance(inner, dict)
            and isinstance(inner.get("datasetId"), str)
            and inner.get("datasetId").strip()
            and isinstance(inner.get("keyColumn"), str)
            and inner.get("keyColumn").strip()
        )
        if not bound:
            raise PreflightError(
                f"产物 {artifact.id} 尚未完成主数据集/键列配置（未绑数据集的中间态不可分享）。"
            )


def _exec_share_create(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..share import service as share_service
    from ..share.routes import ALLOWED_TTL_DAYS, SHARE_KIND_PATHS

    artifact = _own_artifact(sess, ctx, args["artifact_id"])
    ttl_days = args.get("ttl_days")
    if ttl_days is not None and ttl_days not in ALLOWED_TTL_DAYS:
        raise ExecutionError(
            f"ttl_days 仅支持 {'、'.join(str(d) for d in ALLOWED_TTL_DAYS)} 天——收到 {ttl_days}"
        )
    try:
        secret = share_service.require_share_secret()
        ttl = (
            share_service.ttl_seconds()
            if ttl_days is None
            else ttl_days * 24 * 3600
        )
        token, exp = share_service.build_token(
            secret, artifact_id=artifact.id, version=artifact.version, ttl=ttl
        )
    except share_service.ShareDisabled as exc:
        raise ExecutionError(f"分享服务未配置：{exc}") from exc
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    url = (
        f"/api/v1/share/{SHARE_KIND_PATHS[artifact.kind]}/{artifact.id}"
        f"?t={token}&v={artifact.version}"
    )
    expires = (
        share_service.expires_at_iso(exp) if exp > 0 else "永久有效（0 档）"
    )
    return f"分享链接已生成（v{artifact.version}，{expires}）：{url}"


def _exec_archive(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..artifacts.service import archive_artifact

    artifact = _own_artifact(sess, ctx, args["artifact_id"])
    try:
        archived = archive_artifact(sess.ai, artifact, customer_id=ctx.customer_id)
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    return (
        f"产物「{archived.title}」已归档（可经 restore 恢复——影响面：可逆）。"
    )


#: 需求单类动作：模型可以给业务字段，但合规两字段只认客户输入——
#: 模型给的取值在 propose 时即被丢弃（spec「不代勾」场景）。
_COMPLIANCE_KEYS_MODEL_SIDE = frozenset(
    {"source_urls", "public_data_asserted", "source_url", "public_claim", "compliance"}
)


def _pf_demand_submit(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    _deny_viewer(ctx)
    if not isinstance(args.get("description"), str) or not args["description"].strip():
        raise PreflightError("需求单提议必须带需求描述（description）。")
    if not isinstance(args.get("contact_email"), str) or not args["contact_email"].strip():
        raise PreflightError("需求单提议必须带联系邮箱（contact_email）。")


def _exec_demand_submit(
    ctx: ActorContext, sess: GateSessions, args: dict[str, Any]
) -> str:
    from ..demands.schemas import DemandCreate
    from ..demands.service import create_demand

    compliance = args.get("_compliance")
    if not isinstance(compliance, dict):
        raise ExecutionError("合规声明缺失（服务端未收到客户输入）")
    try:
        payload = DemandCreate(
            description=args["description"],
            contact_email=args["contact_email"],
            company=args.get("company"),
            indicator_code=args.get("indicator_code"),
            # SourceUrl = Annotated[str, 校验器]——直接传字符串，校验器管 http(s)/无账密
            source_urls=[compliance["source_url"]],
            public_data_asserted=bool(compliance["public_claim"]),
        )
        demand = create_demand(sess.demands, customer_id=ctx.customer_id, payload=payload)
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    except Exception as exc:  # pydantic 校验失败等：如实回显
        raise ExecutionError(_detail_of(exc)) from exc
    return f"需求单已提交（编号 {demand.code}），进展可在「我的需求」查看"


def _pf_pack_id(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    _deny_viewer(ctx)
    if not isinstance(args.get("pack_id"), str) or not args["pack_id"].strip():
        raise PreflightError("源包动作必须带非空 pack_id（源包页可见各包 id）。")


def _exec_pack_subscribe(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..packs.service import PackSubscriptionService

    svc = PackSubscriptionService()
    svc.subscribe(sess.packs, customer_id=ctx.customer_id, pack_id=args["pack_id"])
    return (
        f"已订阅源包 {args['pack_id']}（订阅免费；源包页可随时退订，"
        "目录更新即跟踪）"
    )


def _exec_pack_unsubscribe(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..packs.service import PackSubscriptionService

    svc = PackSubscriptionService()
    removed = svc.unsubscribe(sess.packs, customer_id=ctx.customer_id, pack_id=args["pack_id"])
    return (
        f"已退订源包 {args['pack_id']}"
        if removed
        else f"源包 {args['pack_id']} 此前未订阅（幂等，无需操作）"
    )


def _market_dataset(sess: GateSessions, dataset_id: str):
    from ..assets.models import AssetMeta
    from ..datasets.models import Dataset
    from ..market import browse

    dataset = sess.datasets.get(Dataset, dataset_id)
    if dataset is None:
        raise PreflightError(f"数据集 {dataset_id} 不存在。")
    meta = sess.datasets.get(AssetMeta, dataset_id)
    if meta is None or browse._visibility_of(meta) != browse.VISIBILITY_MARKET:
        raise PreflightError(f"数据集 {dataset_id} 不在市场（可能已下架或未开放）。")
    return dataset, meta


def _pf_market_ready(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    from ..market import browse

    _deny_viewer(ctx)
    if not isinstance(args.get("dataset_id"), str) or not args["dataset_id"].strip():
        raise PreflightError("市场动作必须带 dataset_id。")
    if not browse.data_layer_ready():
        raise PreflightError("市场数据层暂不可用，请稍后重试或联系平台。")
    _market_dataset(sess, args["dataset_id"])


def _exec_subscribe(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..market import browse

    _, meta = _market_dataset(sess, args["dataset_id"])
    try:
        if browse._access_mode_of(meta) == browse.ACCESS_MODE_APPROVAL:
            view, created = browse.create_access_request(
                sess.datasets,
                dataset_id=args["dataset_id"],
                requester_customer_id=ctx.customer_id,
            )
            state = "已发起" if created else "已存在"
            return f"该数据集为审批档——访问申请{state}（平台审核通过后自动开通，不产生订阅）。"
        browse.subscribe(
            sess.datasets, dataset_id=args["dataset_id"], customer_id=ctx.customer_id
        )
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    return f"已订阅市场数据集 {args['dataset_id']}（「我的数据」可见）"


def _exec_unsubscribe(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..market import browse

    _market_dataset(sess, args["dataset_id"])
    try:
        removed = browse.unsubscribe(
            sess.datasets, dataset_id=args["dataset_id"], customer_id=ctx.customer_id
        )
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    return (
        f"已退订市场数据集 {args['dataset_id']}"
        if removed
        else f"数据集 {args['dataset_id']} 此前未订阅（幂等，无需操作）"
    )


def _pf_correct(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    _deny_viewer(ctx)
    if not isinstance(args.get("dataset_id"), str):
        raise PreflightError("修正动作必须带 dataset_id。")
    _own_dataset(sess, ctx, args["dataset_id"])


def _exec_correct(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..datasets import service as datasets_service
    from ..datasets.ingest import (
        EngineCallError,
        EngineUnavailable,
        TOOL_CORRECTION_ADD,
        get_engine_client_factory,
    )
    from ..datasets.routes import _add_local_correction
    from ..datasets.schemas import CorrectionIn

    dataset = _own_dataset(sess, ctx, args["dataset_id"])
    try:
        payload = CorrectionIn(
            **{k: v for k, v in args.items() if k in CorrectionIn.model_fields}
        )
    except Exception as exc:  # noqa: BLE001 - 参数不合契约：如实回显
        raise ExecutionError(f"修正参数不合契约：{exc}") from exc
    try:
        if getattr(dataset, "storage_tier", datasets_service.TIER_CELL) == datasets_service.TIER_API:
            outcome = _add_local_correction(
                sess.datasets, dataset=dataset, payload=payload, customer_id=ctx.customer_id
            )
            return f"已写入本地修正 #{outcome.correction_id}（原行不改写，可撤销）"
        client = get_engine_client_factory()(ctx.customer_id)
        result = client.call(
            TOOL_CORRECTION_ADD,
            {
                "dataset_key": dataset.key,
                **payload.model_dump(exclude_none=True, exclude={"artifact_id"}),
                "actor": f"customer:{ctx.customer_id}",
            },
        )
        correction_id = result.get("correction_id")
        if correction_id is None:
            raise ExecutionError("引擎未返回 correction_id")
        return f"已写入引擎修正 #{correction_id}（原行不改写，可撤销）"
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    except (EngineUnavailable, EngineCallError) as exc:
        raise ExecutionError(_detail_of(exc)) from exc


def _pf_revert(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> None:
    _deny_viewer(ctx)
    if not isinstance(args.get("dataset_id"), str) or not isinstance(
        args.get("correction_id"), int
    ):
        raise PreflightError("撤销动作必须带 dataset_id 与 correction_id。")
    _own_dataset(sess, ctx, args["dataset_id"])


def _exec_revert(ctx: ActorContext, sess: GateSessions, args: dict[str, Any]) -> str:
    from ..datasets import api_store, service as datasets_service
    from ..datasets.ingest import (
        EngineCallError,
        EngineUnavailable,
        TOOL_CORRECTION_REVERT,
        get_engine_client_factory,
    )

    dataset = _own_dataset(sess, ctx, args["dataset_id"])
    correction_id = args["correction_id"]
    try:
        if getattr(dataset, "storage_tier", datasets_service.TIER_CELL) == datasets_service.TIER_API:
            api_store.revert_local_correction(
                sess.datasets, dataset=dataset, correction_id=correction_id
            )
            return f"修正 #{correction_id} 已撤销（基行恢复，留痕保留）"
        client = get_engine_client_factory()(ctx.customer_id)
        client.call(
            TOOL_CORRECTION_REVERT,
            {
                "dataset_key": dataset.key,
                "correction_id": correction_id,
                "actor": f"customer:{ctx.customer_id}",
            },
        )
        return f"修正 #{correction_id} 已撤销（引擎重建有效表，留痕保留）"
    except HTTPException as exc:
        raise ExecutionError(_detail_of(exc)) from exc
    except (api_store.CorrectionNotFound, api_store.CorrectionAlreadyReverted) as exc:
        raise ExecutionError(str(exc)) from exc
    except (EngineUnavailable, EngineCallError) as exc:
        raise ExecutionError(_detail_of(exc)) from exc


#: v1 注册表（design D3 冻结表，十一条）。名单外的操作副驾不提供——
#: 即使底层服务存在该能力（spec「名单外操作不提供」）。
REGISTRY: dict[str, ActionDef] = {
    def_.id: def_
    for def_ in (
        ActionDef(
            id="wire.artifact.draft.create",
            risk=RISK_WRITE,
            impact=lambda a: (
                f"创建{_KIND_CN.get(a.get('kind'), a.get('kind'))}草稿"
                f"「{a.get('title', '')}」（落暂存区，确认前不产生正式产物）"
            ),
            preflight=_pf_draft_create,
            execute=_exec_draft_create,
            summarize=lambda a: (
                f"kind={a.get('kind')}; title={str(a.get('title', ''))[:120]}; "
                f"spec 字段：{','.join(sorted((a.get('spec') or {}).keys()))}"
            ),
        ),
        ActionDef(
            id="wire.dataset.create",
            risk=RISK_WRITE,
            impact=lambda a: f"创建数据集「{a.get('name', '')}」"
            f"（{str((a.get('source') or {}).get('kind', ''))} 来源）",
            preflight=_pf_dataset_create,
            execute=_exec_dataset_create,
        ),
        ActionDef(
            id="wire.artifact.promote",
            risk=RISK_WRITE,
            impact=lambda a: f"草稿 #{a.get('draft_id')} 转正为正式产物（产物页可见）",
            preflight=_pf_promote,
            execute=_exec_promote,
        ),
        ActionDef(
            id="wire.artifact.publish",
            risk=RISK_WRITE,
            impact=lambda a: f"发布产物 #{a.get('artifact_id')}（队内可见；对外分享需另经审批）",
            preflight=_pf_artifact_id,
            execute=_exec_publish,
        ),
        ActionDef(
            id="wire.artifact.share.create",
            risk=RISK_PUBLISH,
            impact=lambda a: (
                f"为产物 #{a.get('artifact_id')} 生成对外分享链接"
                f"（有效期 {a.get('ttl_days', '默认 30')} 天——任何人持链接可访问）"
            ),
            preflight=_pf_share_create,
            execute=_exec_share_create,
        ),
        ActionDef(
            id="wire.artifact.archive",
            risk=RISK_WRITE,
            impact=lambda a: f"归档产物 #{a.get('artifact_id')}（可逆：restore 可恢复）",
            preflight=_pf_artifact_id,
            execute=_exec_archive,
        ),
        ActionDef(
            id="wire.demand.submit",
            risk=RISK_WRITE,
            impact=lambda a: (
                f"提交数据需求单（{str(a.get('description', ''))[:80]}…）"
                "——公开源 URL 与公开数据声明须由你在确认卡上填写"
            ),
            preflight=_pf_demand_submit,
            execute=_exec_demand_submit,
            needs_compliance=True,
        ),
        ActionDef(
            id="wire.market.subscribe",
            risk=RISK_COST,
            impact=lambda a: (
                f"订阅市场数据集 {a.get('dataset_id')}"
                "（审批档将转为访问申请；订阅可能产生费用/配额占用）"
            ),
            preflight=_pf_market_ready,
            execute=_exec_subscribe,
        ),
        ActionDef(
            id="wire.market.unsubscribe",
            risk=RISK_WRITE,
            impact=lambda a: f"退订市场数据集 {a.get('dataset_id')}（「我的数据」不再可见）",
            preflight=_pf_market_ready,
            execute=_exec_unsubscribe,
        ),
        ActionDef(
            id="wire.pack.subscribe",
            risk=RISK_WRITE,
            impact=lambda a: f"订阅源包 {a.get('pack_id')}（订阅免费，随时可退订；目录更新即跟踪）",
            preflight=_pf_pack_id,
            execute=_exec_pack_subscribe,
        ),
        ActionDef(
            id="wire.pack.unsubscribe",
            risk=RISK_WRITE,
            impact=lambda a: f"退订源包 {a.get('pack_id')}（幂等；「源包」页可重新订阅）",
            preflight=_pf_pack_id,
            execute=_exec_pack_unsubscribe,
        ),
        ActionDef(
            id="wire.dataset.correct",
            risk=RISK_WRITE,
            impact=lambda a: (
                f"修正数据集 {a.get('dataset_id')} 一处数据"
                f"（{a.get('op', '')}——追加修正不改原行，可撤销）"
            ),
            preflight=_pf_correct,
            execute=_exec_correct,
        ),
        ActionDef(
            id="wire.dataset.correction.revert",
            risk=RISK_WRITE,
            impact=lambda a: f"撤销数据集 {a.get('dataset_id')} 的修正 #{a.get('correction_id')}",
            preflight=_pf_revert,
            execute=_exec_revert,
        ),
    )
}


# ── 闸流程：propose / approve / reject / 查询 ───────────────────────────────

def _digest(args: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(args, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def propose(
    db: Session,
    sess: GateSessions,
    ctx: ActorContext,
    *,
    action_id: str,
    args: dict[str, Any],
) -> tuple[AIActionProposal, list[str]]:
    """提议：注册表校验 + preflight → 落 pending 行。

    未注册（404 语义）/ preflight 失败（422 语义）**不落 pending**——调用方
    （路由或循环）把异常转成如实文案。合规字段（需求单类）在模型参数里的
    任何取值此刻即被丢弃。
    """
    def_ = REGISTRY.get(action_id)
    if def_ is None:
        raise ActionNotRegistered(
            f"动作 {action_id!r} 未注册（v1 名单见 REGISTRY）——副驾不提供该操作。"
        )
    if def_.needs_compliance:
        args = {k: v for k, v in args.items() if k not in _COMPLIANCE_KEYS_MODEL_SIDE}
    def_.preflight(ctx, sess, args)

    state = (
        PROPOSAL_STATE_PENDING_APPROVAL
        if RISK_TO_TIER[def_.risk] == "async"
        else PROPOSAL_STATE_PENDING_CONFIRM
    )
    expires = (
        utc_now_naive() + timedelta(hours=PROPOSAL_APPROVAL_TTL_HOURS)
        if state == PROPOSAL_STATE_PENDING_APPROVAL
        else None
    )
    summarize = def_.summarize or _summarize_scalars
    proposal = AIActionProposal(
        conversation_id=_current_conversation_id(),
        customer_id=ctx.customer_id,
        action_id=action_id,
        args_digest=_digest(args),
        args_summary=summarize(args)[:2000],
        risk=def_.risk,
        tier=RISK_TO_TIER[def_.risk],
        state=state,
        impact_text=def_.impact(args)[:2000],
        proposed_by=ctx.sub,
        expires_at=expires,
    )
    if proposal.conversation_id is None:
        raise PreflightError("提议缺少会话上下文（conversation_id）——请从副驾会话内发起。")
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    # 原始参数面进程内暂存（执行体要全文 spec；audit/提议表只落摘要）——
    # 见 _PROPOSED_ARGS 的取舍说明。
    remember_proposed_args(proposal.id, args)
    missing = ["source_url", "public_claim"] if def_.needs_compliance else []
    return proposal, missing


class ApprovalDenied(PermissionError):
    """审批角色门或状态门拒绝（路由层按 kind 转 403/409，提议状态不变）。

    ``kind="role"`` = 角色权限不足（403）；``kind="state"`` = 状态/过期
    不允许该操作（409）。二者都绝不执行底层能力。
    """

    def __init__(self, message: str, *, kind: str = "role") -> None:
        self.kind = kind
        super().__init__(message)


def _expire_if_due(db: Session, proposal: AIActionProposal, *, now: datetime | None) -> bool:
    """到期惰性过期：async 档超时置 expired（fail-closed，绝不执行）。"""
    if proposal.state != PROPOSAL_STATE_PENDING_APPROVAL or proposal.expires_at is None:
        return False
    current = now or utc_now_naive()
    if proposal.expires_at > current:
        return False
    proposal.state = PROPOSAL_STATE_EXPIRED
    db.commit()
    return True


def approve(
    db: Session,
    sess: GateSessions,
    ctx: ActorContext,
    proposal_id: int,
    *,
    compliance: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """确认/审批并执行：单一执行路径 + 审计，成败都如实返回。

    返回 ``{state, result_summary?, error?}``——pending 不冒充 executed
    （spec「回执不冒充结果」）。
    """
    proposal = db.get(AIActionProposal, proposal_id)
    if proposal is None or proposal.customer_id != ctx.customer_id:
        raise LookupError(f"提议 {proposal_id} 不存在")
    if proposal.state not in (PROPOSAL_STATE_PENDING_CONFIRM, PROPOSAL_STATE_PENDING_APPROVAL):
        raise ApprovalDenied(
            f"提议 {proposal_id} 当前状态 {proposal.state}，不可确认。", kind="state"
        )
    if _expire_if_due(db, proposal, now=now):
        raise ApprovalDenied(
            f"提议 {proposal_id} 已超过 {PROPOSAL_APPROVAL_TTL_HOURS} 小时审批期，"
            "自动拒绝（fail-closed）。",
            kind="state",
        )
    if ctx.role == WRITE_DENIED_ROLE:
        raise ApprovalDenied("团队成员权限不足（viewer 只读），不能确认写操作。")
    if proposal.tier == "async" and ctx.role not in APPROVAL_ROLES:
        roles = "/".join(APPROVAL_ROLES)
        raise ApprovalDenied(
            f"对外暴露类动作仅 {roles} 可审批（当前角色 {ctx.role}）。"
        )

    def_ = REGISTRY[proposal.action_id]
    execute_args = dict(_PROPOSED_ARGS.get(proposal.id, {}))
    # 提议行携带会话上下文注入执行体（跨请求执行的唯一可靠通道）。
    execute_args["_conversation_id"] = proposal.conversation_id
    if def_.needs_compliance:
        missing: list[str] = []
        source_url = (compliance or {}).get("source_url")
        claim = (compliance or {}).get("public_claim")
        if not isinstance(source_url, str) or not source_url.strip():
            missing.append("source_url")
        elif not source_url.startswith(("http://", "https://")):
            raise ComplianceMissing(["source_url（须为 http(s) 公开地址）"])
        if claim is not True:
            missing.append("public_claim")
        if missing:
            raise ComplianceMissing(missing)
        execute_args["_compliance"] = {
            "source_url": source_url.strip(),
            "public_claim": True,
        }

    proposal.approved_by = ctx.sub
    started = datetime.now(timezone.utc)
    try:
        summary = def_.execute(ctx, sess, execute_args)
        proposal.state = PROPOSAL_STATE_EXECUTED
        # 即时响应保留全文（分享链接要能当场复制一次）；持久记录脱敏。
        proposal.result_summary = redact_credentials(summary)
        proposal.approved_at = utc_now_naive()
        db.commit()
        _write_audit(db, proposal, ok=True, started=started)
        return {"state": PROPOSAL_STATE_EXECUTED, "result_summary": summary}
    except (ExecutionError, PreflightError, HTTPException) as exc:
        # 执行期的归属/档位失败（PreflightError）与底层 4xx/5xx（HTTPException）
        # 一律落 failed + 审计——批准后的失败不裸抛（客户已点击，须看到结果）。
        message = _detail_of(exc) if isinstance(exc, HTTPException) else str(exc)
        proposal.state = PROPOSAL_STATE_FAILED
        proposal.approved_at = utc_now_naive()
        db.commit()
        _write_audit(db, proposal, ok=False, started=started, error=message)
        return {"state": PROPOSAL_STATE_FAILED, "error": message}


#: 提议原始参数的进程内暂存（proposal_id → args）——audit/提议表只存脱敏
#: 摘要，执行体需要原始参数面（spec 全文等）时经此取。进程重启丢失 = 该批
#: pending 提议无法执行（fail-closed，重新提议即可）——v1 接受的取舍，
#: 后续如需跨重启可迁移为 args 密文列。
_PROPOSED_ARGS: dict[int, dict[str, Any]] = {}


def remember_proposed_args(proposal_id: int, args: dict[str, Any]) -> None:
    _PROPOSED_ARGS[proposal_id] = args


def reject(db: Session, ctx: ActorContext, proposal_id: int) -> AIActionProposal:
    """拒绝：任意 pending 态 → rejected（本队成员即可，不执行）。"""
    proposal = db.get(AIActionProposal, proposal_id)
    if proposal is None or proposal.customer_id != ctx.customer_id:
        raise LookupError(f"提议 {proposal_id} 不存在")
    if proposal.state not in (PROPOSAL_STATE_PENDING_CONFIRM, PROPOSAL_STATE_PENDING_APPROVAL):
        raise ApprovalDenied(f"提议 {proposal_id} 当前状态 {proposal.state}，不可拒绝。", kind="state")
    proposal.state = PROPOSAL_STATE_REJECTED
    db.commit()
    return proposal


def get_proposal(db: Session, ctx: ActorContext, proposal_id: int) -> AIActionProposal:
    proposal = db.get(AIActionProposal, proposal_id)
    if proposal is None or proposal.customer_id != ctx.customer_id:
        raise LookupError(f"提议 {proposal_id} 不存在")
    _expire_if_due(db, proposal, now=None)
    return proposal


def list_proposals(
    db: Session, *, conversation_id: int, customer_id: str
) -> list[AIActionProposal]:
    rows = list(
        db.scalars(
            select(AIActionProposal)
            .where(
                AIActionProposal.conversation_id == conversation_id,
                AIActionProposal.customer_id == customer_id,
            )
            .order_by(AIActionProposal.id)
        ).all()
    )
    for row in rows:
        _expire_if_due(db, row, now=None)
    return rows


def _write_audit(
    db: Session,
    proposal: AIActionProposal,
    *,
    ok: bool,
    started: datetime,
    error: str | None = None,
) -> None:
    duration_ms = str(int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
    db.add(
        AIActionAudit(
            proposal_id=proposal.id,
            customer_id=proposal.customer_id,
            action_id=proposal.action_id,
            result="ok" if ok else "error",
            error_text=error,
            duration_ms=duration_ms,
        )
    )
    db.commit()


def list_audit(
    db: Session, *, customer_id: str, limit: int = 200
) -> list[AIActionAudit]:
    """审计台账检索（按团队；倒序限量——spec「可按团队、时间与动作检索」）。"""
    stmt = (
        select(AIActionAudit)
        .where(AIActionAudit.customer_id == customer_id)
        .order_by(AIActionAudit.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def expire_stale_proposals(db: Session | None = None, *, now: datetime | None = None) -> int:
    """过期扫描（可注入时钟）：pending_approval 超 24h → expired，fail-closed。

    供定时器与测试调用；读路径另有惰性过期（get/list/approve 内嵌）。
    """
    session = db if db is not None else _borrow_session()
    if session is None:  # pragma: no cover - 引擎未初始化（极端启动序）
        return 0
    owns = db is None
    current = now or utc_now_naive()
    rows = list(
        session.scalars(
            select(AIActionProposal).where(
                AIActionProposal.state == PROPOSAL_STATE_PENDING_APPROVAL,
                AIActionProposal.expires_at.is_not(None),
                AIActionProposal.expires_at <= current,
            )
        ).all()
    )
    for row in rows:
        row.state = PROPOSAL_STATE_EXPIRED
    if rows:
        session.commit()
    if owns:
        session.close()
    return len(rows)


def _borrow_session() -> Session | None:
    from .db import get_session

    try:
        return get_session()
    except Exception:  # noqa: BLE001 - 引擎未配置：扫描静默跳过（读路径仍兜底）
        return None


#: 过期扫描定时器（15min 心跳；TestClient 会启动 lifespan，测试可 stop）
_SWEEP_INTERVAL_SECONDS = 15 * 60
_sweeper_stop = threading.Event()
_sweeper_thread: threading.Thread | None = None


def start_expiry_sweeper() -> None:
    """启动后台过期扫描（幂等；app lifespan 调用）。"""
    global _sweeper_thread
    if _sweeper_thread is not None and _sweeper_thread.is_alive():
        return

    def _loop() -> None:
        while not _sweeper_stop.wait(_SWEEP_INTERVAL_SECONDS):
            try:
                expire_stale_proposals()
            except Exception:  # noqa: BLE001 - 单轮失败不影响下一轮
                continue

    _sweeper_stop.clear()
    _sweeper_thread = threading.Thread(
        target=_loop, name="ai-action-expiry-sweeper", daemon=True
    )
    _sweeper_thread.start()


def stop_expiry_sweeper() -> None:
    global _sweeper_thread
    _sweeper_stop.set()
    _sweeper_thread = None


# ── 副驾循环的提议工具（loop.execute_tool_call 消费）─────────────────────────

PROPOSE_ACTION_TOOL_NAME = "propose_action"

PROPOSE_ACTION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROPOSE_ACTION_TOOL_NAME,
        "description": (
            "提议一个写操作。这是你唯一的写通道——你不能直接执行任何写操作。"
            "立即返回提议 id 与状态（pending），由客户在确认卡上确认后才执行；"
            "转述回执时如实区分 pending/executed/failed，绝不把 pending 说成成功。"
            f"可用动作：{'、'.join(sorted(REGISTRY))}。"
            "需求单类动作的公开源 URL 与公开数据声明不要给——那必须由客户自己填写。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "enum": sorted(REGISTRY),
                    "description": "要提议的动作 id",
                },
                "args": {
                    "type": "object",
                    "description": "动作参数（如 draft.create 的 kind/title/spec；"
                    "publish 的 artifact_id）",
                },
            },
            "required": ["action_id", "args"],
        },
    },
}
