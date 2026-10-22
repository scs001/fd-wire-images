"""私有 scope 交付与授权转公开（wire-platform-v1 任务 6.6）。

交付纪律：
- ``in_progress→delivered`` 迁移成功后，交付默认进**客户私有
  scope**（命名 ``demand-<demand_code>``，summary 含需求摘要），指标逐个
  grant 进 ``semantic_codes`` allow-list；
- 建 scope 失败（scope API 未配置/不可达/拒绝）→ 如实落
  ``delivery_events``（scope_create_failed），需求单状态照常 ``delivered``，
  但 ``scope_status`` 标 ``pending``——重试端点（同一 ``/deliver``）可再试；
- 授权转公开是客户显式动作：``publish-authorization`` append-only 留档，
  granted=true 调 scope/目录 API 把指标转公共、撤回（granted=false）调
  un-publish 回归私有——授权与撤回都留档，客户视图可见授权状态。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..delivery.scope_client import ScopeClient, ScopeClientError
from . import service
from .models import Demand
from .statemachine import DemandStatus, TransitionError


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

SCOPE_STATUS_OK = "ok"
SCOPE_STATUS_PENDING = "pending"

EVENT_SCOPE_CREATED = "scope_created"
EVENT_SCOPE_CREATE_FAILED = "scope_create_failed"
EVENT_PUBLISH_AUTHORIZATION = "publish_authorization"


def scope_name_for(demand: Demand) -> str:
    """客户私有 scope 命名：``demand-<demand_code>``。"""
    return f"demand-{demand.code}"


def scope_retry_conflict(demand: Demand) -> str | None:
    """重试端点前置闸：非受理态 → 409 文案（``None``=放行）。

    比 service 层 TransitionError 更准的口径——分别如实回显当前状态与
    scope 状态；内部端点与管理台端点共用本函数（同语义 409 文案唯一来源）。
    放行后的状态闸仍由 ``deliver_demand`` 的 delivered+pending 分支兜底。
    """
    if DemandStatus(demand.status) is not DemandStatus.delivered:
        return f"仅已交付单可重试开 scope（当前 {demand.status}）"
    if demand.scope_status != SCOPE_STATUS_PENDING:
        return f"scope 已交付或无 pending 留档（当前 {demand.scope_status or 'none'}）"
    return None


def scope_summary_for(demand: Demand) -> str:
    """scope 摘要（description）：需求编码 + 标题，交付可追溯。"""
    return f"需求单 {demand.code}《{demand.title}》客户私有交付 scope"


def _indicator_codes(demand: Demand, indicators: Sequence[str] | None) -> list[str]:
    """授权/交付的指标集合：显式传入优先，缺省用目录预填 linked_indicators。"""
    if indicators is not None:
        return [c for c in (str(i).strip() for i in indicators) if c]
    return [c for c in (str(i).strip() for i in (demand.linked_indicators or [])) if c]


def _create_customer_scope(
    db: Session,
    demand: Demand,
    *,
    scope_client: ScopeClient | None,
    indicators: Sequence[str] | None,
    source_dbs: Sequence[str] | None,
) -> Demand:
    """建客户私有 scope 并逐指标授权；失败如实留档并标 ``pending``。"""
    codes = _indicator_codes(demand, indicators)
    dbs = [s for s in (str(x).strip() for x in (source_dbs or [])) if s]
    name = scope_name_for(demand)
    summary = scope_summary_for(demand)
    try:
        if scope_client is None:
            raise ScopeClientError(
                "scope API 未配置（SCOPE_API_BASE 为空），私有 scope 交付挂起待重试"
            )
        scope = scope_client.create_scope(name, summary, dbs, semantic_codes=codes)
        granted: list[str] = []
        grant_errors: dict[str, str] = {}
        for code in codes:
            try:
                scope_client.grant_indicator(scope_name=name, indicator_code=code)
                granted.append(code)
            except ScopeClientError as exc:
                grant_errors[code] = str(exc)
        demand.scope_status = SCOPE_STATUS_OK
        demand.scope_name = name
        demand.append_delivery_event(
            {
                "type": EVENT_SCOPE_CREATED,
                "scope_name": name,
                "summary": summary,
                "indicators": codes,
                "granted": granted,
                "grant_errors": grant_errors,
                "scope": {
                    k: scope.get(k)
                    for k in ("name", "matched", "per_dimension_matches")
                    if isinstance(scope, dict) and k in scope
                },
            }
        )
    except ScopeClientError as exc:
        demand.scope_status = SCOPE_STATUS_PENDING
        demand.scope_name = None
        demand.append_delivery_event(
            {
                "type": EVENT_SCOPE_CREATE_FAILED,
                "scope_name": name,
                "summary": summary,
                "indicators": codes,
                "error": str(exc),
            }
        )
    demand.updated_at = _utcnow_naive()
    db.commit()
    db.refresh(demand)
    return demand


def deliver_demand(
    db: Session,
    demand: Demand,
    *,
    scope_client: ScopeClient | None = None,
    by: str = "customer",
    note: str | None = None,
    indicators: Sequence[str] | None = None,
    source_dbs: Sequence[str] | None = None,
) -> Demand:
    """交付动作：``in_progress→delivered`` 迁移 + 自动建私有 scope。

    wire-ux-consolidation 3.1 起，交付前置态自 delivery_acceptance（废除）
    改为 in_progress——与三态白名单一致。重试语义不变：``delivered`` 且
    ``scope_status=pending`` 时再次调用只重试建 scope（不再迁移）；
    其余状态 409。
    """
    current = DemandStatus(demand.status)
    if current is DemandStatus.in_progress:
        demand = service.apply_transition(
            db,
            demand,
            target=DemandStatus.delivered,
            by=by,
            note=note or "完成交付",
        )
    elif not (
        current is DemandStatus.delivered
        and demand.scope_status == SCOPE_STATUS_PENDING
    ):
        raise TransitionError(
            current,
            DemandStatus.delivered,
            "交付动作仅允许从 in_progress 发起"
            "（delivered 且 scope 交付 pending 时可重试）",
        )
    return _create_customer_scope(
        db, demand, scope_client=scope_client, indicators=indicators, source_dbs=source_dbs
    )


def maybe_auto_deliver(
    db: Session, demand: Demand, *, scope_client: ScopeClient | None
) -> Demand:
    """transition 到 delivered 后的自动私有 scope 交付钩子——绝不打断迁移。

    迁移已成功是事实；scope 交付失败只落 ``scope_pending`` 与留档，
    由重试端点补课。钩子自身的意外异常也兜住（如实标 pending）。
    """
    try:
        if DemandStatus(demand.status) is not DemandStatus.delivered:
            return demand
        if demand.scope_status == SCOPE_STATUS_OK:
            return demand
        return _create_customer_scope(
            db, demand, scope_client=scope_client, indicators=None, source_dbs=None
        )
    except Exception:
        db.rollback()
        try:
            demand.scope_status = SCOPE_STATUS_PENDING
            demand.scope_name = None
            demand.append_delivery_event(
                {
                    "type": EVENT_SCOPE_CREATE_FAILED,
                    "scope_name": scope_name_for(demand),
                    "indicators": _indicator_codes(demand, None),
                    "error": "自动交付钩子意外失败（scope 交付挂起，可经重试端点补课）",
                }
            )
            db.commit()
            db.refresh(demand)
        except Exception:
            db.rollback()
        return demand


def authorize_publish(
    db: Session,
    demand: Demand,
    *,
    granted: bool,
    conditions: str | None = None,
    by: str | None = None,
    indicators: Sequence[str] | None = None,
    scope_client: ScopeClient | None = None,
) -> Demand:
    """客户授权转公开（或撤回）：append-only 留档 + 调 scope/目录 API。

    - 授权动作本身必留档（granted/conditions/by/indicators）；
    - granted=true → ``publish_indicators``（指标转公共）；撤回 →
      ``unpublish_indicators``（回归私有）——调用形状经 mock 客户端验证；
    - 客户端不可用/失败：授权照常留档，publish_outcome 如实记失败
      （授权是客户事实，转公开的执行状态另案跟进）。
    """
    current = DemandStatus(demand.status)
    if current is not DemandStatus.delivered:
        raise TransitionError(
            current,
            DemandStatus.delivered,
            "授权转公开只在 delivered 后可用（客户显式动作，留档可撤回）",
        )
    codes = _indicator_codes(demand, indicators)
    actor = by or f"customer:{demand.customer_id}"
    outcome: dict[str, Any]
    try:
        if scope_client is None:
            raise ScopeClientError(
                "scope/目录 API 未配置（SCOPE_API_BASE 为空），转公开执行挂起（授权已留档）"
            )
        if granted:
            result = scope_client.publish_indicators(codes)
            outcome = {
                "ok": True,
                "action": "publish",
                "result": result if isinstance(result, dict) else {"result": result},
            }
        else:
            result = scope_client.unpublish_indicators(codes)
            outcome = {
                "ok": True,
                "action": "unpublish",
                "result": result if isinstance(result, dict) else {"result": result},
            }
    except ScopeClientError as exc:
        outcome = {"ok": False, "error": str(exc)}
    demand.append_delivery_event(
        {
            "type": EVENT_PUBLISH_AUTHORIZATION,
            "granted": bool(granted),
            "conditions": conditions,
            "by": actor,
            "indicators": codes,
            "publish_outcome": outcome,
        }
    )
    demand.updated_at = _utcnow_naive()
    db.commit()
    db.refresh(demand)
    return demand
