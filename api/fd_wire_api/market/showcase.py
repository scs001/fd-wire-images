"""匿名橱窗浏览层（wire-showcase-market）。

与 ``artifact_browse.py``（登录市场）**物理分路**——本模块只服务匿名读，且查询
条件在 SQL 层就钉死平台自营发布方：

    customer_id = <平台租户>  AND  visibility = 'market'  AND  status = 'published'

三条同时成立才进结果集。客户发布的任何产物在这条路径上**根本不在查询范围里**，
红线由查询边界保证，而不是靠「先查出来再过滤」（后者一个漏写就是泄露）。

两处与登录市场的**有意差异**（design D3）：

1. 详情返回**完整 spec（含面板快照）**——匿名橱窗要画得出图。这不违反红线：
   橱窗里只有平台自有内容，数据是平台自己的营销资产；红线保护的是客户数据。
2. 不要求身份、不做订阅判定（匿名访客没有身份可判）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..artifacts.models import Artifact, ArtifactVersion
from .artifact_browse import _panel_summaries
from .artifact_service import VISIBILITY_MARKET
from .platform_publisher import PLATFORM_PUBLISHER_NAME, resolve_platform_publisher

#: 橱窗只展示看板（研究报告等后续按需扩，白名单式推进）。
SHOWCASE_KINDS: frozenset[str] = frozenset({"dashboard"})


def _base_query(kind: str | None = None):
    """橱窗查询骨架：三条硬条件 + 可选 kind。

    平台租户名直接内联在 WHERE 里（而不是先解析出租户 id 再拼）——少一次往返，
    且「租户缺失」自然退化为空结果集而非报错。
    """
    stmt = select(Artifact).where(
        Artifact.customer_id == PLATFORM_PUBLISHER_NAME,
        Artifact.visibility == VISIBILITY_MARKET,
        Artifact.status == "published",
    )
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    return stmt


def _card(artifact: Artifact, panel_count: int | None) -> dict[str, Any]:
    """橱窗列表卡片：元数据 + 面板数摘要；**无租户标识、无面板数据**。

    与登录市场的 card 不同，这里刻意不返回 publisher_customer_id——匿名面
    不需要知道归属租户（它就是平台自己），少一个字段少一分泄露面。
    """
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "title": artifact.title,
        "version": artifact.version,
        "published_at": _iso(artifact.published_at),
        "updated_at": _iso(artifact.updated_at),
        "panel_count": panel_count,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat() + "Z"
    return str(value)


def _count_panels(spec: Any) -> int | None:
    """spec 内层面板数；形状不认识返回 None（列表照常返回，不毁条目）。"""
    body = spec.get("spec") if isinstance(spec, dict) and isinstance(spec.get("spec"), dict) else spec
    panels = body.get("panels") if isinstance(body, dict) else None
    return len(panels) if isinstance(panels, list) else None


def list_showcase_dashboards(
    db: Session,
    platform_db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """匿名橱窗列表：平台自有、已上架、market 档看板。

    ``db`` = artifacts 所在库（ai 库）；``platform_db`` = billing_customers 所在
    库（平台库）。两者**不是同一个会话**——租户存在性必须在平台库上确认。

    平台租户行缺失时返回空列表（安全默认，不 500、不放宽任何过滤）。
    """
    if resolve_platform_publisher(platform_db) is None:
        return {"items": [], "total": 0}

    stmt = _base_query().where(Artifact.kind.in_(tuple(SHOWCASE_KINDS)))
    stmt = stmt.order_by(Artifact.published_at.desc().nullslast(), Artifact.id.desc())

    rows = db.execute(
        stmt.limit(max(1, min(limit, 200))).offset(max(0, offset))
    ).scalars().all()
    total = len(db.execute(stmt).scalars().all())

    items = []
    for artifact in rows:
        latest = _latest_version(db, artifact.id)
        items.append(_card(artifact, _count_panels(latest.spec) if latest else None))
    return {"items": items, "total": total}


def get_showcase_dashboard(
    db: Session, platform_db: Session, artifact_id: int
) -> dict[str, Any] | None:
    """匿名橱窗详情：钉当前版本，返回完整 spec（含快照）。

    **安全断言先于一切**：产物必须属于平台自营发布方，否则返回 None——调用方
    一律转 404（不是 403：403 会泄露「此处存在一个客户产物」）。
    """
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        return None
    # 红线断言：不满足三条硬条件的一律当作不存在。
    if artifact.customer_id != PLATFORM_PUBLISHER_NAME:
        return None
    if artifact.visibility != VISIBILITY_MARKET:
        return None
    if artifact.status != "published":
        return None
    if artifact.kind not in SHOWCASE_KINDS:
        return None
    # 租户行必须在场（防「产物在但租户被删」的悬空态被当成平台资产）。
    # 注意查的是平台库（billing_customers 不在 ai 库）。
    if resolve_platform_publisher(platform_db) is None:
        return None

    latest = _latest_version(db, artifact.id)
    if latest is None:
        return None

    spec = latest.spec or {}
    refreshed_at = spec.get("refreshed_at") if isinstance(spec, dict) else None
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "title": artifact.title,
        "version": latest.version,
        "published_at": _iso(artifact.published_at),
        "updated_at": _iso(artifact.updated_at),
        "panels": _panel_summaries(spec),
        "spec": spec,
        "refreshed_at": refreshed_at if isinstance(refreshed_at, str) else None,
    }


def _latest_version(db: Session, artifact_id: int) -> ArtifactVersion | None:
    return db.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


__all__ = [
    "SHOWCASE_KINDS",
    "get_showcase_dashboard",
    "list_showcase_dashboards",
]
