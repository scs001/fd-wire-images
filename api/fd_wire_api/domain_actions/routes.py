"""客户域动作路由（wire-domain-actions 任务组 3/4）。

三组面：

- **类型声明**（挂实体，console 身份 + owner/editor 角色门禁）：
  ``GET/POST /api/v1/entities/{id}/actions``、
  ``PUT/DELETE /api/v1/entities/{id}/actions/{type_id}``（DELETE=停用，
  留痕不删行）；
- **提议**（console 身份或会话；**wdk- 数据面 key 不受理**——业务写提议走
  客户身份而非数据凭据，安全边界）：``POST /api/v1/domain-actions/propose``
  → 闸 propose（tier 由类型 risk 决定）→ 实例台账行；
- **台账**：``GET /api/v1/domain-actions``（本企业启用类型——MCP list）、
  ``GET /api/v1/domain-actions/instances``（台账，客户域隔离）、
  ``GET /api/v1/domain-actions/instances/{id}``（MCP status）。

确认/执行**不设新端点**——复用闸既有 ``/api/v1/ai/actions/{proposal_id}/
approve|reject``（单一执行路径；控制台确认卡复用副驾 ActionCard）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai.actions import (
    ActionNotRegistered,
    ApprovalDenied,
    ComplianceMissing,
    PreflightError,
    propose as gate_propose,
)
from ..ai.db import get_db as get_ai_db
from ..ai.routes import _gate_error, get_gate_sessions
from ..admin.db import get_db as get_platform_db
from ..console.auth import (
    bearer_token,
    require_console_customer_id,
    resolve_member_role,
    verify_id_token,
)
from ..datasets.db import get_db as get_datasets_db
from ..entities.db import get_db as get_entities_db
from . import service
from .builtins import BUILTIN_BY_ID, builtin_payload, builtin_types
from .models import DomainActionInstance, DomainActionType

router = APIRouter(prefix="/api/v1", tags=["domain-actions"])


# ------------------------------------------------------------------ schemas


class DomainActionTypeIn(BaseModel):
    """声明/更新动作类型请求体（PUT 整量替换，重走同一条校验链）。"""

    name: str = Field(min_length=1, max_length=100)
    risk: str
    form_schema: dict[str, Any]
    effect: dict[str, Any]


class DomainActionTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_id: int
    name: str
    risk: str
    form_schema: dict[str, Any]
    effect: dict[str, Any]
    enabled: bool
    dataset_id: str
    pk_columns_json: list[str]
    created_at: datetime
    updated_at: datetime


class DomainActionProposeIn(BaseModel):
    """提议入参：类型 id + 表单值 + 可选来源标注（原样透传 ≤256 字符）。"""

    action_type_id: int
    values: dict[str, Any]
    source_ref: str | None = Field(default=None, max_length=256)


class DomainActionProposeOut(BaseModel):
    """提议回执：立即返回，不冒充结果（对偶义务——阻塞将被壹座归因 stalled）。"""

    proposal_id: int
    instance_id: int
    action_id: str
    state: str
    tier: str
    impact: str


class DomainActionInstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type_id: int
    proposal_id: int | None
    proposer_sub: str
    channel: str
    source_ref: str | None
    args_json: dict[str, Any]
    status: str
    confirmed_by: str | None
    correction_id: int | None
    #: metadata 效果（内置声明动作）成功回填：实体/关系 id 字符串（0045）
    result_ref: str | None = None
    error: str | None
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ helpers


def require_action_editor(
    request: Request, db: Session = Depends(get_platform_db)
) -> tuple[Any, str]:
    """动作类型编辑/提议依赖（与 assets.require_asset_editor 同语义：owner/
    editor 可写、viewer 403、未映射 401；返回 (Customer, caller_sub)——服务端
    身份解析，不信请求头）。刻意不跨包导入路由模块（import 环），语义以
    assets 侧为准、两处同改。"""
    identity = verify_id_token(bearer_token(request))
    resolved = resolve_member_role(db, identity.sub)
    if resolved is None:
        raise HTTPException(
            status_code=401, detail="登录态无法映射到企业身份，请重新登录"
        )
    customer, role = resolved
    if role == "viewer":
        raise HTTPException(
            status_code=403, detail="团队成员权限不足（viewer 只读）"
        )
    return customer, identity.sub


def _type_or_404(db: Session, type_id: int, customer_id: str) -> DomainActionType:
    try:
        return service.get_owned_type(db, type_id=type_id, customer_id=customer_id)
    except service.ActionTypeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ------------------------------------------------------------- 类型声明面


@router.get("/entities/{entity_id}/actions")
def list_entity_actions(
    entity_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_ai_db),
) -> dict:
    """实体动作类型列表（含停用行——台账完整性；启用位如实）。"""
    rows = service.list_types_for_entity(
        db, customer_id=customer_id, entity_id=entity_id
    )
    return {
        "entity_id": entity_id,
        "actions": [DomainActionTypeOut.model_validate(r).model_dump(mode="json") for r in rows],
    }


@router.post(
    "/entities/{entity_id}/actions",
    response_model=DomainActionTypeOut,
    status_code=status.HTTP_201_CREATED,
)
def create_entity_action(
    entity_id: int,
    payload: DomainActionTypeIn,
    principal: tuple[Any, str] = Depends(require_action_editor),
    db: Session = Depends(get_ai_db),
    entities_db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> DomainActionTypeOut:
    """声明动作类型（owner/editor；viewer 403）。"""
    customer, sub = principal
    try:
        type_row = service.create_type(
            db,
            entities_db,
            datasets_db,
            customer_id=str(customer.name),
            entity_id=entity_id,
            name=payload.name,
            risk=payload.risk,
            form_schema=payload.form_schema,
            effect=payload.effect,
            actor_sub=sub,
        )
    except service.ActionTypeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.ActionTypeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidActionType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return DomainActionTypeOut.model_validate(type_row)


@router.put(
    "/entities/{entity_id}/actions/{type_id}",
    response_model=DomainActionTypeOut,
)
def update_entity_action(
    entity_id: int,
    type_id: int,
    payload: DomainActionTypeIn,
    principal: tuple[Any, str] = Depends(require_action_editor),
    db: Session = Depends(get_ai_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> DomainActionTypeOut:
    """整量更新（重校验；停用类型更新不自动启用——启用走显式启停）。"""
    customer, sub = principal
    type_row = _type_or_404(db, type_id, str(customer.name))
    if type_row.entity_id != entity_id:
        raise HTTPException(status_code=404, detail=f"动作类型 {type_id} 不存在")
    try:
        type_row = service.update_type(
            db,
            datasets_db,
            type_row,
            name=payload.name,
            risk=payload.risk,
            form_schema=payload.form_schema,
            effect=payload.effect,
            actor_sub=sub,
        )
    except service.ActionTypeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidActionType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return DomainActionTypeOut.model_validate(type_row)


@router.delete("/entities/{entity_id}/actions/{type_id}")
def disable_entity_action(
    entity_id: int,
    type_id: int,
    principal: tuple[Any, str] = Depends(require_action_editor),
    db: Session = Depends(get_ai_db),
) -> dict:
    """停用（删除语义=停用：留痕不删行；在途实例冻结 disabled）。"""
    customer, sub = principal
    type_row = _type_or_404(db, type_id, str(customer.name))
    if type_row.entity_id != entity_id:
        raise HTTPException(status_code=404, detail=f"动作类型 {type_id} 不存在")
    service.set_enabled(db, type_row, enabled=False, actor_sub=sub)
    db.commit()
    return {"id": type_id, "enabled": False}


# ------------------------------------------------------------- 提议与台账


@router.post("/domain-actions/propose", response_model=DomainActionProposeOut)
def propose_domain_action(
    payload: DomainActionProposeIn,
    principal: tuple[Any, str] = Depends(require_action_editor),
    db: Session = Depends(get_ai_db),
    gate_sessions: Session = Depends(get_gate_sessions),
) -> DomainActionProposeOut:
    """提议域动作（闸管线的域动作入口；tier 由类型 risk 决定）。

    - **即时回执**：返回 pending 提议标识，不阻塞等待确认（对偶义务——
      阻塞实现将被壹座回合归因 stalled）；
    - viewer 403（require_asset_editor 角色门）；wdk- 数据面 key 不受理；
    - 未注册/停用/他企类型 → 闸「动作未注册」语义（404 透传，不泄露存在性）。
    """
    customer, sub = principal
    customer_id = str(customer.name)
    # 内置声明动作类型（负数哨兵 id，无 DB 行）：跳过行查找，闸解析走
    # build_domain_action_def 的内置分流；其余 id 维持既有 404 语义。
    if payload.action_type_id not in BUILTIN_BY_ID:
        _type_or_404(db, payload.action_type_id, customer_id)
    try:
        proposal, _missing = gate_propose(
            db,
            gate_sessions,
            _actor_ctx(customer_id, sub),
            action_id=f"domain.{payload.action_type_id}",
            args={
                "values": payload.values,
                "source_ref": payload.source_ref,
                "_channel": "console",
            },
        )
    except (ActionNotRegistered, PreflightError, ComplianceMissing, ApprovalDenied) as exc:
        raise _gate_error(exc) from exc
    except LookupError as exc:
        raise _gate_error(exc) from exc
    instance = service.instance_by_proposal(
        db, customer_id=customer_id, proposal_id=proposal.id
    )
    if instance is None:  # pragma: no cover —— on_proposed 保证
        raise HTTPException(status_code=500, detail="实例台账缺失（内部错误）")
    return DomainActionProposeOut(
        proposal_id=proposal.id,
        instance_id=instance.id,
        action_id=proposal.action_id,
        state=proposal.state,
        tier=proposal.tier,
        impact=proposal.impact_text,
    )


@router.get("/domain-actions")
def list_customer_domain_actions(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_ai_db),
) -> dict:
    """本企业启用动作类型（MCP wire_action_list 同源；含表单 schema）。

    内置声明动作类型（wire-entity-write-surface）经代码合成注入载荷尾部
    （builtin=true、负数 id）——闸的「未注册拒绝」语义与列表通道零分叉。"""
    rows = service.list_enabled_for_customer(db, customer_id=customer_id)
    actions = [
        DomainActionTypeOut.model_validate(r).model_dump(mode="json") for r in rows
    ]
    for row in actions:
        row["builtin"] = False
    actions.extend(
        builtin_payload(bt) for bt in builtin_types(customer_id)
    )
    return {"actions": actions}


@router.get("/domain-actions/instances")
def list_domain_action_instances(
    type_id: int | None = None,
    instance_status: str | None = None,
    limit: int = 50,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_ai_db),
) -> dict:
    """实例台账（客户域隔离；新→旧；type/status 可选过滤）。"""
    rows = service.list_instances(
        db, customer_id=customer_id, type_id=type_id, status=instance_status, limit=limit
    )
    return {
        "instances": [
            DomainActionInstanceOut.model_validate(r).model_dump(mode="json")
            for r in rows
        ]
    }


@router.get("/domain-actions/instances/{instance_id}")
def get_domain_action_instance(
    instance_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_ai_db),
) -> dict:
    """实例详情（MCP wire_action_status 同源；他企/不存在 404 不泄露）。"""
    instance = db.get(DomainActionInstance, instance_id)
    if instance is None or instance.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"实例 {instance_id} 不存在")
    return DomainActionInstanceOut.model_validate(instance).model_dump(mode="json")


# ------------------------------------------------------------------ 内部


def _actor_ctx(customer_id: str, sub: str) -> Any:
    """域动作提议的闸身份（require_asset_editor 已过角色门——owner/editor；
    role=owner 满足闸的写许可语义）。"""
    from ..ai.actions import ActorContext  # noqa: PLC0415

    return ActorContext(customer_id=customer_id, sub=sub, role="owner")
