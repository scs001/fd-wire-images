"""工作台聚合端点（wire-personal-workbench 任务 1.1）。

``GET /api/v1/workbench``：控制台「工作台」首屏——单次往返返回登录客户的
信息资产全貌（datasets / free_quota / packs / artifacts / alerts / demands
六段）。约束（spec 冻结）：

- **纯读聚合、零新表**：四个切片库（packs / datasets / ai / demands）各自
  经本模块 ``get_db`` 依赖取会话——生产各指不同库，**绝无跨库事务**；
  端点只读不写，任何一段失败都直接 500（不静默吞段呈现假零）。
- **跨客户隔离（红线）**：每段查询都带 ``customer_id`` 过滤，其他客户的
任何标识（名称/id/code/计数）零出现。
- 免费配额段**镜像** ``GET /api/v1/datasets/quota`` 既有的服务函数
  （``is_free_tier`` / ``free_dataset_count`` / settings 限额），不发明
  新的档位查询；付费档整段为 ``null``（无此限额展示，与 quota 端点同口径）。
- 时间口径：全仓 naive UTC（各切片 ``utc_now_naive`` 同款），24h 事件窗
  以请求时刻回推、与 ``created_at`` 直接比较。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai.db import get_db as get_ai_db
from .alerts.models import AlertEvent, AlertState
from .artifacts.models import Artifact
from .console.auth import require_console_customer_id
from .datasets import service as datasets_service
from .datasets.db import get_db as get_datasets_db
from .datasets.models import Dataset
from .datasets.settings import get_settings as get_datasets_settings
from .demands import get_db as get_demands_db
from .demands.models import Demand
from .packs.db import get_db as get_packs_db
from .packs.definitions import get_pack
from .packs.models import PackSubscription

router = APIRouter(prefix="/api/v1/workbench", tags=["workbench"])

#: 最近列表条数上限（datasets / packs / artifacts 三段共用，spec 冻结 ≤5）
RECENT_LIMIT = 5

#: 产物「活跃」状态集：draft / published（archived 不计——spec 冻结）
ACTIVE_ARTIFACT_STATUSES: tuple[str, ...] = ("draft", "published")

#: 需求单「进行中」口径：submitted / in_progress（注意：与 demands 切片
#: statemachine 的 ACTIVE_STATUSES 不同——那边含 delivered，工作台口径以
#: spec 冻结的开放态为准，故本地定义不复用）
OPEN_DEMAND_STATUSES: tuple[str, ...] = ("submitted", "in_progress")

#: 告警事件回看窗口（naive UTC 上做差）
EVENT_WINDOW = timedelta(hours=24)


# ------------------------------------------------------------------ 响应模型


class DatasetRecentItem(BaseModel):
    """最近数据集条目（分层徽标 + 行数/体积快照；两列均非空有默认）。"""

    id: str
    name: str
    storage_tier: str
    row_count: int
    size_bytes: int
    updated_at: datetime


class WorkbenchDatasets(BaseModel):
    """数据集段：总数 + api/cell 分层计数 + 最近 ≤5 条（updated_at 倒序）。"""

    total: int
    api_tier: int
    cell_tier: int
    recent: list[DatasetRecentItem]


class WorkbenchFreeQuota(BaseModel):
    """免费档真实配额（个数/单集行数上限来自 datasets 切片配置）。

    付费档**整个对象为 null**——无此限额展示（与 quota 端点 max=null 同
    口径的更紧凑形态：工作台不重复 plan_code 字段）。
    """

    used_datasets: int
    max_datasets: int
    max_rows_per_dataset: int


class PackRecentItem(BaseModel):
    """最近订阅条目：名称取静态包定义，定义缺失双语名如实 null（不虚构）。"""

    pack_id: str
    name_zh: str | None
    name_en: str | None
    subscribed_at: datetime


class WorkbenchPacks(BaseModel):
    """源包订阅段：订阅总数 + 最近 ≤5 条（订阅时间倒序）。"""

    subscribed: int
    recent: list[PackRecentItem]


class ArtifactRecentItem(BaseModel):
    """最近产物条目（活跃产物面：archived 不进最近列表）。"""

    id: int
    kind: str
    title: str
    status: str
    updated_at: datetime


class WorkbenchArtifacts(BaseModel):
    """产物段：活跃总数（draft+published）+ 八类分计数（缺类零）+ 最近 ≤5。"""

    active_total: int
    by_kind: dict[str, int]
    recent: list[ArtifactRecentItem]


class WorkbenchAlerts(BaseModel):
    """告警段：alert_rule 活跃产物数 / firing 中规则数 / 近 24h 事件数。"""

    alert_rules: int
    firing: int
    events_24h: int


class WorkbenchDemandLatest(BaseModel):
    """最新需求单摘要（updated_at 倒序第一单；无单则整段 latest=null）。"""

    code: str
    status: str
    updated_at: datetime


class WorkbenchDemands(BaseModel):
    """需求单段：开放态计数（submitted/in_progress）+ 最新一单摘要。"""

    open: int
    latest: WorkbenchDemandLatest | None


class WorkbenchOut(BaseModel):
    """工作台聚合响应：六段一次返回（字段名与 web 预约定逐字冻结）。"""

    datasets: WorkbenchDatasets
    free_quota: WorkbenchFreeQuota | None
    packs: WorkbenchPacks
    artifacts: WorkbenchArtifacts
    alerts: WorkbenchAlerts
    demands: WorkbenchDemands


# ------------------------------------------------------------------ service


def _utcnow_naive() -> datetime:
    """naive UTC 当前时刻（与各切片模型的存时口径一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def datasets_section(db: Session, *, customer_id: str) -> WorkbenchDatasets:
    """数据集段：分层计数（一次 GROUP BY）+ 最近列表（updated_at 倒序 ≤5）。"""
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    tier_rows = dict(
        db.execute(
            select(Dataset.storage_tier, func.count(Dataset.id)).where(
                Dataset.customer_id == customer_id
            ).group_by(Dataset.storage_tier)
        ).all()
    )
    recent = (
        db.execute(
            select(Dataset)
            # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
            .where(Dataset.customer_id == customer_id)
            .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
            .limit(RECENT_LIMIT)
        )
        .scalars()
        .all()
    )
    return WorkbenchDatasets(
        total=sum(int(count) for count in tier_rows.values()),
        api_tier=int(tier_rows.get(datasets_service.TIER_API, 0)),
        cell_tier=int(tier_rows.get(datasets_service.TIER_CELL, 0)),
        recent=[
            DatasetRecentItem(
                id=row.id,
                name=row.name,
                storage_tier=row.storage_tier,
                row_count=int(row.row_count),
                size_bytes=int(row.size_bytes),
                updated_at=row.updated_at,
            )
            for row in recent
        ],
    )


def free_quota_section(db: Session, *, customer_id: str) -> WorkbenchFreeQuota | None:
    """免费配额段：镜像 ``GET /api/v1/datasets/quota`` 的既有服务函数。

    免费档返回真实数字（used = api 层数据集数——升档后旧 api 集仍计入，
    见 datasets service 口径）；非免费档返回 null（档位不可解析按付费口径
    呈现，不放大免费额度）。
    """
    used = datasets_service.free_dataset_count(db, customer_id=customer_id)
    if not datasets_service.is_free_tier(customer_id=customer_id):
        return None
    settings = get_datasets_settings()
    return WorkbenchFreeQuota(
        used_datasets=used,
        max_datasets=settings.free_tier_max_datasets,
        max_rows_per_dataset=settings.free_tier_max_rows,
    )


def packs_section(db: Session, *, customer_id: str) -> WorkbenchPacks:
    """源包订阅段：订阅总数 + 最近 ≤5（created_at 倒序映射 subscribed_at）。

    名称取 ``definitions.get_pack`` 静态定义；包定义缺失（订阅留档先于
    定义下线等）时 name_zh/name_en 如实 null——pack_id 本身照发不虚构。
    """
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    subscribed = int(
        db.execute(
            select(func.count(PackSubscription.id)).where(
                PackSubscription.customer_id == customer_id
            )
        ).scalar_one()
    )
    recent_rows = (
        db.execute(
            select(PackSubscription)
            # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
            .where(PackSubscription.customer_id == customer_id)
            .order_by(PackSubscription.created_at.desc(), PackSubscription.id.desc())
            .limit(RECENT_LIMIT)
        )
        .scalars()
        .all()
    )
    recent: list[PackRecentItem] = []
    for row in recent_rows:
        pack = get_pack(row.pack_id)
        recent.append(
            PackRecentItem(
                pack_id=row.pack_id,
                name_zh=pack.name_zh if pack else None,
                name_en=pack.name_en if pack else None,
                subscribed_at=row.created_at,
            )
        )
    return WorkbenchPacks(subscribed=subscribed, recent=recent)


#: 工作台展示面的产物类清单（八类，含表单构建器的 form、单记录公开页的
#: record-view——wire-record-view 任务 1.3——与富文本文档的 doc——
#: wire-rich-text-docs 任务 1.4）。注意与 artifacts.models.
#: ARTIFACT_KINDS（五类，校验面口径）区分：那边不加 form/record-view/doc
#: 是产物校验语义（它们走 service 层常量路线，AI 门亦不收录），展示面
#: 必须覆盖全部实际 kind，否则 active_total 与 by_kind 徽标和会对不上。
#: 键序与 web api.ts 的 ARTIFACT_KINDS 展示序一致。
WORKBENCH_KINDS: tuple[str, ...] = (
    "dashboard",
    "form",
    "composite_indicator",
    "research_report",
    "alert_rule",
    "refresh_schedule",
    "record-view",
    "doc",
)


def artifacts_section(db: Session, *, customer_id: str) -> WorkbenchArtifacts:
    """产物段：活跃（draft+published）总数与八类分计数 + 最近 ≤5。

    by_kind 八键全给（缺类零）；archived 不计（活跃口径覆盖计数与最近
    列表两处）；库中出现清单外 kind 时如实附加键（不吞不虚构）。
    """
    active = Artifact.status.in_(ACTIVE_ARTIFACT_STATUSES)
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    kind_rows = db.execute(
        select(Artifact.kind, func.count(Artifact.id))
        .where(Artifact.customer_id == customer_id, active)
        .group_by(Artifact.kind)
    ).all()
    by_kind: dict[str, int] = {kind: 0 for kind in WORKBENCH_KINDS}
    for kind, count in kind_rows:
        by_kind[str(kind)] = int(count)
    recent_rows = (
        db.execute(
            select(Artifact)
            # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
            .where(Artifact.customer_id == customer_id, active)
            .order_by(Artifact.updated_at.desc(), Artifact.id.desc())
            .limit(RECENT_LIMIT)
        )
        .scalars()
        .all()
    )
    return WorkbenchArtifacts(
        active_total=sum(by_kind.values()),
        by_kind=by_kind,
        recent=[
            ArtifactRecentItem(
                id=row.id,
                kind=row.kind,
                title=row.title,
                status=row.status,
                updated_at=row.updated_at,
            )
            for row in recent_rows
        ],
    )


def alerts_section(db: Session, *, customer_id: str) -> WorkbenchAlerts:
    """告警段：alert_rule 活跃产物数 + firing 状态计数 + 近 24h 事件数。

    firing 数的是 AlertState（每条 alert_rule 产物一行的持久状态位）里
    ``state='firing'`` 的行；事件数是 AlertEvent 近 24h（naive UTC 回推，
    与建表存时同口径）的行数——均带 customer_id 冗余列直查，零 join。
    """
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    alert_rules = int(
        db.execute(
            select(func.count(Artifact.id)).where(
                Artifact.customer_id == customer_id,
                Artifact.kind == "alert_rule",
                Artifact.status.in_(ACTIVE_ARTIFACT_STATUSES),
            )
        ).scalar_one()
    )
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    firing = int(
        db.execute(
            select(func.count(AlertState.artifact_id)).where(
                AlertState.customer_id == customer_id,
                AlertState.state == "firing",
            )
        ).scalar_one()
    )
    window_start = _utcnow_naive() - EVENT_WINDOW
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    events_24h = int(
        db.execute(
            select(func.count(AlertEvent.id)).where(
                AlertEvent.customer_id == customer_id,
                AlertEvent.created_at >= window_start,
            )
        ).scalar_one()
    )
    return WorkbenchAlerts(
        alert_rules=alert_rules, firing=firing, events_24h=events_24h
    )


def demands_section(db: Session, *, customer_id: str) -> WorkbenchDemands:
    """需求单段：开放态计数 + 最新一单摘要（无单 latest=null）。"""
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    open_count = int(
        db.execute(
            select(func.count(Demand.id)).where(
                Demand.customer_id == customer_id,
                Demand.status.in_(OPEN_DEMAND_STATUSES),
            )
        ).scalar_one()
    )
    # customer 过滤是隔离红线——去掉它跨客户数据即泄漏（变异自证依赖此行）
    latest = db.execute(
        select(Demand)
        .where(Demand.customer_id == customer_id)
        .order_by(Demand.updated_at.desc(), Demand.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return WorkbenchDemands(
        open=open_count,
        latest=(
            WorkbenchDemandLatest(
                code=latest.code,
                status=latest.status,
                updated_at=latest.updated_at,
            )
            if latest is not None
            else None
        ),
    )


def build_workbench(
    *,
    customer_id: str,
    packs_db: Session,
    datasets_db: Session,
    ai_db: Session,
    demands_db: Session,
) -> WorkbenchOut:
    """聚合六段（各切片纯读、独立会话；供路由直调与测试复用）。"""
    return WorkbenchOut(
        datasets=datasets_section(datasets_db, customer_id=customer_id),
        free_quota=free_quota_section(datasets_db, customer_id=customer_id),
        packs=packs_section(packs_db, customer_id=customer_id),
        artifacts=artifacts_section(ai_db, customer_id=customer_id),
        alerts=alerts_section(ai_db, customer_id=customer_id),
        demands=demands_section(demands_db, customer_id=customer_id),
    )


# ------------------------------------------------------------------ 路由


@router.get("", response_model=WorkbenchOut)
def get_workbench(
    customer_id: str = Depends(require_console_customer_id),
    packs_db: Session = Depends(get_packs_db),
    datasets_db: Session = Depends(get_datasets_db),
    ai_db: Session = Depends(get_ai_db),
    demands_db: Session = Depends(get_demands_db),
) -> WorkbenchOut:
    """工作台聚合视图（登录客户；纯读、免费档可用、不产生计量）。"""
    return build_workbench(
        customer_id=customer_id,
        packs_db=packs_db,
        datasets_db=datasets_db,
        ai_db=ai_db,
        demands_db=demands_db,
    )
