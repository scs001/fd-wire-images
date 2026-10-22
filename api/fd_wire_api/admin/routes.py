"""内部后台路由（wire-platform-v1 任务 7.1；wire-self-serve-launch 2.1/2.2）。

一面六台：
1. **客户管理**：GET/POST ``/customers``、PATCH ``/customers/{id}``——
   开户经 ``billing.ensure_customer`` 自动代建 sub2api 支付账户并回写
   ``sub2api_user_id``（2.1；sub2api 不可达 fail-closed 502 中止，不留半
   开户）；显式传 ``sub2api_user_id`` 保留为兼容路径（跳过代建）。禁用档
   （is_active=false，如旧三档）不可指派 → 409（2.2）。
2. **档位管理**：GET ``/plans``（全部档位含禁用标记）、POST ``/plans``
   （自定义档建档，恒 is_public=false，2.2）。
3. **额度与账本**：GET ``/customers/{id}/usage``（metering_records 只读联查：
   credential_track=customer × caller=billing_customers.sub2api_user_id——v1
   身份映射由该列承载）、GET ``/ledger``（billing_ledger_entries 只读）。
4. **需求单派发台**：GET ``/demands``（全客户）、POST ``/demands/{id}/transition``
   ——转发既有内部 transition 端点语义（直接调 service 层，无 HTTP 自调）；
   迁入 delivered 时补调 ``maybe_auto_deliver`` 自动开客户私有 scope
   （wire-demand-visibility：与内部端点逐字同语义，scope 失败只落
   pending、绝不打断迁移）；POST ``/demands/{id}/scope-retry``——对
   delivered+pending 单重试开 scope（wire-scope-delivery-live：与内部
   端点同语义，前置闸 409 如实回显）；POST ``/demands/{id}/workorder``
   与 ``/demands/{id}/wanxing-dispatch``——出单入舰队拉取队列 / 萬星
   直连 A2A 快车道（wire-wanxing-direct-dispatch：与内部端点共用同一
   service，拒绝口径与响应形状完全一致）。
5. **目录治理**：GET ``/catalog/pending``（读镜像表）、POST
   ``/catalog/{indicator_id}/verify-note``——不改镜像数据（仅同步管道可写），
   审核意见只记 admin 自己的 catalog_review_notes（append-only）。
6. **用量视图（分轨）**：GET ``/usage/overview``——按 credential_track 与
   source 聚合 metering_records（复用计量聚合口径）。

鉴权（5.1 双闸）：全部端点经 ``require_admin_access``——X-Admin-Token
（env ``FD_WIRE_ADMIN_TOKEN``，fail-closed）+ Bearer ID token 角色闸
（env ``FD_WIRE_ADMIN_ROLE``，默认 wire-admin；FD_WIRE_ADMIN_BEARER_GATE
控制，默认 on，"off" 仅测试/开发用）。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..billing.customers import ensure_customer
from ..billing.models import Customer, CustomerStatus, LedgerEntry
from ..billing.models import Plan as BillingPlan
from ..billing.models import validate_period
from ..billing.plans import get_plan
from ..billing.sub2api import Sub2ApiError, Sub2ApiClient, sub2api_client_dep
from ..catalog.db import readonly_session
from ..catalog.models import IndicatorSnapshot
from ..dbcompat import period_expression
from ..delivery.scope_client import ScopeClient, scope_client_dep
from ..demands import delivery as delivery_service
from ..demands import service as demands_service
from ..demands.db import get_db as get_demands_db
from ..demands.models import Demand
from ..demands.orchestration import WanxingDispatchFailure, WorkorderExportError, WorkorderIn
from ..demands.orchestration import dispatch_wanxing_service, export_workorder_service
from ..demands.schemas import DemandInternalOut, ScopeRetryIn, TransitionIn
from ..demands.statemachine import DemandStatus, TransitionError
from ..metering.db import to_utc
from ..metering.models import MeteringRecord
from ..metering.settings import ALLOWED_TRACKS
from .auth import require_admin_access
from .db import get_db
from .models import CatalogReviewNote
from .schemas import (
    CatalogPendingOut,
    CustomerCreateIn,
    CustomerOut,
    CustomerPatchIn,
    CustomerUsageOut,
    IndicatorPendingItem,
    LedgerEntryOut,
    PlanCreateIn,
    PlanOut,
    ReviewNoteIn,
    ReviewNoteOut,
    TrackRow,
    UsageOverviewOut,
    UsagePeriodRow,
)

router = APIRouter(
    prefix="/internal/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_access)],
)

#: 镜像状态的合法取值（与 indicator_snapshots 的 CHECK 约束一致）
_MIRROR_STATUSES = ("verified", "registered")

#: 单页上限——与公开指标目录列表（catalog/service.py）的 page_size 上限同值，
#: 使后台目录治理页与公开目录共用一套分页红线（规模再大也只按页取）。
_CATALOG_PENDING_MAX_LIMIT = 100


# ---------------------------------------------------------------------------
# 客户管理（billing_customers / billing_plans：只读查询 + 开户记录）
# ---------------------------------------------------------------------------


def _customer_or_404(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail=f"customer {customer_id} 不存在")
    return customer


def _customer_out(db: Session, customer: Customer) -> CustomerOut:
    plan = db.get(BillingPlan, customer.plan_id)
    return CustomerOut(
        id=customer.id,
        name=customer.name,
        sub2api_user_id=customer.sub2api_user_id,
        logto_sub=customer.logto_sub,
        plan_id=customer.plan_id,
        plan_code=plan.code if plan else "",
        plan_name=plan.name if plan else "",
        status=customer.status,
        created_at=customer.created_at,
    )


def _assignable_plan_or_error(db: Session, plan_code: str) -> BillingPlan:
    """指派校验（2.2）：未知档 → 422；禁用档（is_active=false，含旧三档）
    → 409（旧档禁用退场，不可再指派给新客户）。"""
    try:
        plan = get_plan(db, plan_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not plan.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"档位 {plan_code!r} 已禁用（不可指派）",
        )
    return plan


@router.get("/plans", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_db)) -> list[PlanOut]:
    """套餐档位下拉数据源（billing_plans 只读；含禁用档与 is_public 标记）。"""
    plans = db.execute(select(BillingPlan).order_by(BillingPlan.id)).scalars().all()
    return [PlanOut.from_plan(p) for p in plans]


@router.post(
    "/plans",
    response_model=PlanOut,
    status_code=status.HTTP_201_CREATED,
)
def create_custom_plan(payload: PlanCreateIn, db: Session = Depends(get_db)) -> PlanOut:
    """自定义档建档（2.2）：询价定案的月费/含额，恒 ``is_public=false``
    （不进公开档位列表）；建后可指派给指定客户，指派后行数门与结算按
    新档口径生效。code 冲突 → 409。"""
    existing = db.execute(
        select(BillingPlan).where(BillingPlan.code == payload.code)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"档位 code 已存在: {payload.code!r}",
        )
    plan = BillingPlan(
        code=payload.code,
        name=payload.name,
        monthly_price_micro=payload.monthly_price_micro,
        included_lines=payload.included_lines,
        overage_unit_price_micro=payload.overage_unit_price_micro,
        is_active=True,
        is_public=False,
        created_at=datetime.now(dt_timezone.utc),
    )
    db.add(plan)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"档位创建冲突: {payload.code!r}",
        ) from exc
    return PlanOut.from_plan(plan)


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(db: Session = Depends(get_db)) -> list[CustomerOut]:
    """客户列表（新→旧，带套餐档位联查字段）。"""
    customers = db.execute(select(Customer).order_by(Customer.id.desc())).scalars().all()
    return [_customer_out(db, c) for c in customers]


@router.post(
    "/customers",
    response_model=CustomerOut,
    status_code=status.HTTP_201_CREATED,
)
def create_customer(
    payload: CustomerCreateIn,
    db: Session = Depends(get_db),
    sub2api: Sub2ApiClient = Depends(sub2api_client_dep),
) -> CustomerOut:
    """开户（2.1）：名称 + 套餐档位 → 自动代建 sub2api 支付账户并回写
    ``sub2api_user_id``（``billing.ensure_customer`` 幂等；Idempotency-Key
    兜底重试不重复建户）。

    - sub2api 不可达/5xx → 502 fail-closed 中止（不留半开户：本地行与
      代建账户要么都有、要么都没有）；
    - 显式传 ``sub2api_user_id`` 保留为兼容路径（跳过代建，直接落本地行）；
    - 同名（任意档位）→ 422 如实拒（wire-console-resilience D3：客户名
      全局唯一，提示改名）；禁用档 → 409；未知档位 → 422。
    """
    plan = _assignable_plan_or_error(db, payload.plan_code)

    duplicate = db.execute(
        select(Customer).where(Customer.name == payload.name)
    ).scalar_one_or_none()
    if duplicate is not None:
        # 建户动作之前拦截（零半写）：唯一索引的最后防线在这里前置成
        # 可读的 422，而不是等 flush IntegrityError。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"客户名已存在: {payload.name}",
        )

    if payload.sub2api_user_id:
        # 兼容路径：显式身份映射键（v1 手工口径；不触发 sub2api 代建）
        customer = Customer(
            name=payload.name,
            sub2api_user_id=payload.sub2api_user_id,
            plan_id=plan.id,
            status=CustomerStatus.ACTIVE.value,
        )
        db.add(customer)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="客户创建冲突（sub2api_user_id 已被其他客户占用？）",
            ) from exc
        return _customer_out(db, customer)

    # 自动代建路径（2.1）：sub2api 建户 → 落本地行；不可达即 502 中止
    try:
        customer, created = ensure_customer(
            db, sub2api, name=payload.name, plan_code=plan.code
        )
    except Sub2ApiError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"sub2api 代建账户失败（fail-closed，未落本地行）: {exc}",
        ) from exc
    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"同名客户已存在（id={customer.id}）",
        )
    db.flush()
    return _customer_out(db, customer)


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
def patch_customer(
    customer_id: int, payload: CustomerPatchIn, db: Session = Depends(get_db)
) -> CustomerOut:
    """改套餐档位 / 客户状态（active | suspended | closed）；禁用档不可指派（409）。

    状态转 suspended/closed 时 best-effort 吊销该客户经首登开户铸出的
    registry key（console_onboardings.key_id 在册者；self-serve-onboarding
    「停用即断」）。吊销失败不阻塞停用（治理动作不能因 registry 故障被
    卡死），行数门侧另有非 active 额度归零兜底。
    """
    if payload.plan_code is None and payload.status is None:
        raise HTTPException(status_code=422, detail="至少提供 plan_code 或 status 之一")
    customer = _customer_or_404(db, customer_id)
    if payload.plan_code is not None:
        plan = _assignable_plan_or_error(db, payload.plan_code)
        customer.plan_id = plan.id
    if payload.status is not None:
        customer.status = payload.status.value
    db.flush()
    if payload.status is not None and payload.status.value in (
        CustomerStatus.SUSPENDED.value,
        CustomerStatus.CLOSED.value,
    ):
        _revoke_customer_keys_best_effort(db, customer.id)
    return _customer_out(db, customer)


def _revoke_customer_keys_best_effort(db: Session, customer_id: int) -> None:
    """吊销该客户名下在册 key（幂等：revoked/already/not_found 均算达成）。"""
    from ..console.models import ConsoleOnboarding
    from ..console.onboarding import default_mint_channel

    try:
        channel = default_mint_channel()
    except Exception:  # CELL_REGISTRY_TOKEN 未配置：无 key 可吊的部署面，跳过
        return
    rows = db.execute(
        select(ConsoleOnboarding.key_id).where(
            ConsoleOnboarding.customer_id == customer_id,
            ConsoleOnboarding.key_id.is_not(None),
        )
    ).all()
    for (key_id,) in rows:
        try:
            channel.revoke(key_id)
        except Exception:
            continue  # best-effort：单把失败不挡其余与停用本身


# ---------------------------------------------------------------------------
# 额度与账本（metering_records 只读联查 / billing_ledger_entries 只读）
# ---------------------------------------------------------------------------


@router.get("/customers/{customer_id}/usage", response_model=CustomerUsageOut)
def customer_usage(customer_id: int, db: Session = Depends(get_db)) -> CustomerUsageOut:
    """单客户用量：metering_records 中 credential_track=customer 且
    caller=该客户身份（v1 由 billing_customers.sub2api_user_id 承载）的
    只读聚合，按周期（YYYY-MM）分解。

    身份映射未建立（sub2api_user_id 为空）时如实返回空聚合。
    """
    customer = _customer_or_404(db, customer_id)
    caller = customer.sub2api_user_id

    periods: list[UsagePeriodRow] = []
    if caller:
        period_expr = period_expression(db.get_bind(), MeteringRecord.event_timestamp)
        rows = db.execute(
            select(
                period_expr.label("period"),
                func.count(MeteringRecord.id).label("calls"),
                func.sum(MeteringRecord.rows_returned).label("rows"),
            )
            .where(
                MeteringRecord.credential_track == "customer",
                MeteringRecord.caller == caller,
            )
            .group_by(period_expr)
            .order_by(period_expr)
        ).all()
        periods = [
            UsagePeriodRow(period=p, calls=int(calls), rows=int(rows or 0))
            for p, calls, rows in rows
        ]
    totals = UsagePeriodRow(
        period="all",
        calls=sum(p.calls for p in periods),
        rows=sum(p.rows for p in periods),
    )
    return CustomerUsageOut(
        customer_id=customer.id,
        customer_name=customer.name,
        caller=caller,
        credential_track="customer",
        totals=totals,
        periods=periods,
    )


@router.get("/ledger", response_model=list[LedgerEntryOut])
def list_ledger(
    customer_id: int | None = Query(default=None),
    period: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[LedgerEntryOut]:
    """账本流水（billing_ledger_entries 只读；可按客户与周期过滤）。"""
    if period is not None:
        try:
            validate_period(period)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if customer_id is not None:
        _customer_or_404(db, customer_id)
    stmt = select(LedgerEntry).order_by(LedgerEntry.id)
    if customer_id is not None:
        stmt = stmt.where(LedgerEntry.customer_id == customer_id)
    if period is not None:
        stmt = stmt.where(LedgerEntry.period == period)
    entries = db.execute(stmt).scalars().all()
    return [LedgerEntryOut.from_entry(e) for e in entries]


# ---------------------------------------------------------------------------
# 需求单派发台（demands：全客户列表 + 状态机迁移转发）
# ---------------------------------------------------------------------------


@router.get("/demands", response_model=list[DemandInternalOut])
def list_demands(
    status_filter: DemandStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_demands_db),
) -> list[DemandInternalOut]:
    """全客户需求单列表（新→旧，可按状态过滤；内部完整视图）。"""
    stmt = select(Demand).order_by(Demand.id.desc())
    if status_filter is not None:
        stmt = stmt.where(Demand.status == status_filter.value)
    demands = db.execute(stmt).scalars().all()
    return [DemandInternalOut.model_validate(d) for d in demands]


@router.post("/demands/{demand_id}/transition", response_model=DemandInternalOut)
def transition_demand(
    demand_id: int,
    payload: TransitionIn,
    db: Session = Depends(get_demands_db),
    scope_client: ScopeClient | None = Depends(scope_client_dep),
) -> DemandInternalOut:
    """派发操作：转发既有内部 transition 端点语义（直调 service 层）。

    - 非法迁移/前置不满足 → 409（三态白名单裁决，与内部端点一致）；
    - 合法目标仅 in_progress / delivered / rejected（带理由）——废除态
      迁入一律 409 如实回显（wire-ux-consolidation 3.1）；
    - 迁入 ``delivered`` 成功后补调 ``delivery_service.maybe_auto_deliver``
      （wire-demand-visibility）：自动发起客户私有 scope 交付，与内部端点
      逐字同语义——scope 面失败只落 ``scope_status=pending`` 留档，绝不
      打断迁移响应。
    """
    try:
        demand = demands_service.get_demand(db, demand_id)
    except demands_service.DemandNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        demand = demands_service.apply_transition(
            db,
            demand,
            target=payload.target_status,
            by=payload.by or "admin-console",
            note=payload.note,
            reason=payload.reason,
        )
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if payload.target_status is DemandStatus.delivered:
        demand = delivery_service.maybe_auto_deliver(db, demand, scope_client=scope_client)
    return DemandInternalOut.model_validate(demand)


@router.post("/demands/{demand_id}/scope-retry", response_model=DemandInternalOut)
def retry_demand_scope(
    demand_id: int,
    payload: ScopeRetryIn | None = None,
    db: Session = Depends(get_demands_db),
    scope_client: ScopeClient | None = Depends(scope_client_dep),
) -> DemandInternalOut:
    """「重试开 scope」运营动作（wire-scope-delivery-live）。

    与内部端点 ``POST /internal/demands/{id}/scope-retry`` 逐字同语义：
    仅 delivered 且 scope_status=pending 受理（共用 ``scope_retry_conflict``
    前置闸，409 如实回显当前状态/scope 态、零副作用），复用
    ``deliver_demand`` 的重试分支只重试建私有 scope——成功 scope_status=ok
    且 scope_name 落档，失败照旧留档 pending（诚实降级），响应即最新单态。
    """
    try:
        demand = demands_service.get_demand(db, demand_id)
    except demands_service.DemandNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    conflict = delivery_service.scope_retry_conflict(demand)
    if conflict is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict)
    body = payload or ScopeRetryIn()
    demand = delivery_service.deliver_demand(
        db,
        demand,
        scope_client=scope_client,
        by=body.by,
        note=body.note,
        indicators=body.indicators,
        source_dbs=body.source_dbs,
    )
    return DemandInternalOut.model_validate(demand)


@router.post("/demands/{demand_id}/workorder", response_model=None)
def export_demand_workorder(
    demand_id: int,
    payload: WorkorderIn | None = None,
    db: Session = Depends(get_demands_db),
) -> dict:
    """「转生成工单」操作员控制台动作（wire-workorder-console-action）。

    与内部端点 ``POST /api/v1/demands/{id}/workorder`` 共用同一 service
    （``export_workorder_service``）——确定性映射、脱敏红线、三态 slug
    探测、append-only 留档、出单不迁移状态，语义与拒绝口径完全一致；
    响应恒带工单全文供人工推送降级（诚实降级红线：出单时刻未投递）。
    """
    try:
        demand = demands_service.get_demand(db, demand_id)
    except demands_service.DemandNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return export_workorder_service(
            db, demand, slug_override=payload.slug if payload is not None else None
        )
    except WorkorderExportError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail
        ) from exc


@router.post("/demands/{demand_id}/wanxing-dispatch", response_model=None)
def dispatch_demand_wanxing(
    demand_id: int,
    payload: WorkorderIn | None = None,
    db: Session = Depends(get_demands_db),
) -> dict:
    """「萬星直连」操作员控制台动作（wire-wanxing-direct-dispatch）。

    与内部端点 ``POST /api/v1/demands/{id}/wanxing-dispatch`` 共用同一
    service（``dispatch_wanxing_service``）——同映射同脱敏红线、同一出单
    闸、``wanxing_dispatched`` 留档、直连单不进拉取队列，语义与拒绝口径
    完全一致；502 时 detail 是 dict（message + 工单全文等 payload）——
    失败降级红线：人工推送通道始终可用。
    """
    try:
        demand = demands_service.get_demand(db, demand_id)
    except demands_service.DemandNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return dispatch_wanxing_service(
            db, demand, slug_override=payload.slug if payload is not None else None
        )
    except WanxingDispatchFailure as exc:
        # 子类先捕：502 带工单全文（FastAPI 的 detail 合法可为 dict）。
        raise HTTPException(
            status_code=exc.status_code, detail={"message": exc.detail, **exc.payload}
        ) from exc
    except WorkorderExportError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail
        ) from exc


# ---------------------------------------------------------------------------
# 目录治理（镜像只读 + 审核意见 append-only）
# ---------------------------------------------------------------------------


@router.get("/catalog/pending", response_model=CatalogPendingOut)
def catalog_pending(
    mirror_status: str = Query(default="registered", alias="status"),
    limit: int = Query(default=_CATALOG_PENDING_MAX_LIMIT),
    offset: int = Query(default=0),
) -> CatalogPendingOut:
    """镜像目录列表（默认 registered=待治理；verified 可复查）分页只读。

    只读：镜像仅同步管道可写（spec wire-indicator-catalog），
    状态修改在权威库侧完成，本台只看不改。

    分页：镜像规模可达万级（生产实测 registered 5.4 万条），全量返回会让
    呈现层一次渲染全部条目（下滑丢内容），故按 limit/offset 取页并回 total
    （匹配总数，不是本页条数）供前端算页数；limit 超限按上限收敛而非报错
    ——超限是常见手写参数，如实截断比 422 更贴合人工翻阅意图。
    """
    if mirror_status not in _MIRROR_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {list(_MIRROR_STATUSES)}",
        )
    page_limit = min(max(1, limit), _CATALOG_PENDING_MAX_LIMIT)
    page_offset = max(0, offset)
    with readonly_session() as session:
        stmt = select(IndicatorSnapshot).where(
            IndicatorSnapshot.status == mirror_status
        )
        total = session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        rows = (
            session.execute(
                stmt.order_by(IndicatorSnapshot.indicator_id)
                .offset(page_offset)
                .limit(page_limit)
            )
            .scalars()
            .all()
        )
        items = [
            IndicatorPendingItem(
                indicator_id=r.indicator_id,
                code=r.code,
                name_zh=r.name_zh,
                name_en=r.name_en,
                domain=r.domain,
                frequency=r.frequency,
                status=r.status,
                synced_at=r.synced_at,
            )
            for r in rows
        ]
    return CatalogPendingOut(status=mirror_status, items=items, total=total)


@router.post(
    "/catalog/{indicator_id}/verify-note",
    response_model=ReviewNoteOut,
    status_code=status.HTTP_201_CREATED,
)
def add_verify_note(
    indicator_id: str, payload: ReviewNoteIn, db: Session = Depends(get_db)
) -> ReviewNoteOut:
    """记录一条审核意见（append-only 到 admin 自己的 catalog_review_notes）。

    不改镜像数据：镜像行状态只能在权威库侧修改，本端点仅留档人工意见。
    指标不在镜像中 → 404（如实拒绝，不凭空留档）。
    """
    if not payload.note.strip():
        raise HTTPException(status_code=422, detail="审核意见不能为空白")
    with readonly_session() as session:
        exists = session.execute(
            select(IndicatorSnapshot.id).where(
                IndicatorSnapshot.indicator_id == indicator_id
            )
        ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=404, detail=f"indicator {indicator_id!r} 不在目录镜像中"
        )
    note = CatalogReviewNote(
        indicator_id=indicator_id,
        note=payload.note,
        author=payload.author or "admin-console",
    )
    db.add(note)
    db.flush()
    return ReviewNoteOut(
        id=note.id,
        indicator_id=note.indicator_id,
        note=note.note,
        author=note.author,
        created_at=note.created_at.isoformat() if note.created_at else "",
    )


@router.get("/catalog/{indicator_id}/notes", response_model=list[ReviewNoteOut])
def list_verify_notes(
    indicator_id: str, db: Session = Depends(get_db)
) -> list[ReviewNoteOut]:
    """某指标的审核意见留档（时间正序，只追加不可改）。"""
    notes = (
        db.execute(
            select(CatalogReviewNote)
            .where(CatalogReviewNote.indicator_id == indicator_id)
            .order_by(CatalogReviewNote.id)
        )
        .scalars()
        .all()
    )
    return [
        ReviewNoteOut(
            id=n.id,
            indicator_id=n.indicator_id,
            note=n.note,
            author=n.author,
            created_at=n.created_at.isoformat() if n.created_at else "",
        )
        for n in notes
    ]


# ---------------------------------------------------------------------------
# 用量视图（分轨概览：credential_track × source）
# ---------------------------------------------------------------------------


def _parse_window(name: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be ISO-8601 datetime") from exc


@router.get("/usage/overview", response_model=UsageOverviewOut)
def usage_overview(
    credential_track: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> UsageOverviewOut:
    """分轨用量概览：按 credential_track（customer/platform）与 source
    （yizuo/wanxing/platform-internal/customer）聚合 metering_records，
    可按轨道与时间窗过滤——客户轨/平台轨数字卡与来源分布的数据源。
    """
    if credential_track is not None and credential_track not in ALLOWED_TRACKS:
        raise HTTPException(
            status_code=422,
            detail=f"credential_track must be one of {list(ALLOWED_TRACKS)}",
        )
    start_dt = _parse_window("start", start)
    end_dt = _parse_window("end", end)

    conditions = []
    if credential_track:
        conditions.append(MeteringRecord.credential_track == credential_track)
    if start_dt:
        conditions.append(MeteringRecord.event_timestamp >= start_dt)
    if end_dt:
        conditions.append(MeteringRecord.event_timestamp <= end_dt)

    def _grouped(dimension):
        stmt = select(
            dimension.label("key"),
            func.count(MeteringRecord.id).label("calls"),
            func.sum(MeteringRecord.rows_returned).label("rows"),
        ).group_by(dimension)
        for condition in conditions:
            stmt = stmt.where(condition)
        stmt = stmt.order_by(dimension)
        return [
            TrackRow(key=key, calls=int(calls), rows=int(rows or 0))
            for key, calls, rows in db.execute(stmt).all()
        ]

    tracks = _grouped(MeteringRecord.credential_track)
    by_source = _grouped(MeteringRecord.source)
    totals = TrackRow(
        key="all",
        calls=sum(t.calls for t in tracks),
        rows=sum(t.rows for t in tracks),
    )
    return UsageOverviewOut(
        credential_track=credential_track,
        start=start,
        end=end,
        tracks=tracks,
        by_source=by_source,
        totals=totals,
    )
