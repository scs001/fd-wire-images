"""产物市场浏览层（wire-artifact-marketplace）：列表/检索/详情 + 准入判定。

与 ``market/browse.py``（数据集版）同构；红线纪律逐条对齐：

- **SQL 层过滤**：列表只取 ``visibility='market'``——默认私有，未开放谁都不
  可见（不存在"先查出来再过滤"的路径）；
- **详情结构摘要（Q8）**：面板清单只回 ``{id, type, title}``——不含
  config/snapshot/query；完整 spec 只有订阅后经渲染面可取；
- **零数据值**：列表与详情的任何字段都不承载数据快照。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..artifacts.models import Artifact, ArtifactVersion
from .artifact_service import (
    ACCESS_MODE_DEFAULT,
    VISIBILITY_MARKET,
)
from .platform_publisher import PLATFORM_PUBLISHER_NAME

#: 详情/渲染准入的三种关系来源
SOURCE_OWN = "own"
SOURCE_SUBSCRIPTION = "market_subscription"
SOURCE_GRANT = "directed_grant"
SOURCE_APPROVED = "access_request"


def _fmt(value: Any) -> str | None:
    """datetime → ISO（带 Z）；其他原样透传（不在本层做类型转换）。"""
    if hasattr(value, "isoformat"):
        return value.isoformat() + "Z"
    return value


def _latest_version(db: Session, artifact_id: int) -> ArtifactVersion | None:
    return db.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _panel_summaries(spec: Any) -> list[dict]:
    """面板结构摘要（Q8）：只保留 id/type/title 三字段。"""
    body = spec.get("spec") if isinstance(spec, dict) and isinstance(spec.get("spec"), dict) else spec
    panels = body.get("panels") if isinstance(body, dict) else None
    out: list[dict] = []
    if not isinstance(panels, list):
        return out
    for idx, panel in enumerate(panels):
        if not isinstance(panel, dict):
            continue
        out.append(
            {
                "id": str(panel.get("id") or f"p{idx + 1}"),
                "type": str(panel.get("type") or "unknown"),
                "title": str(panel.get("title") or ""),
            }
        )
    return out


def _report_scope(spec: Any) -> tuple[Any, Any]:
    """报告的数据范围元信息（Q11：data_scope/data_range 是既有契约字段）。"""
    body = spec.get("spec") if isinstance(spec, dict) and isinstance(spec.get("spec"), dict) else spec
    if not isinstance(body, dict):
        return None, None
    return body.get("data_scope"), body.get("data_range")


def card(artifact: Artifact, *, viewer_customer_id: str) -> dict:
    """列表卡片：元数据 + 轻摘要（零数据值、零 config）。

    ``is_platform``（wire-showcase-market）：标识平台自营发布方——登录客户
    据此区分平台自有内容与本平台其他客户发布的内容。仅一个布尔标识，
    不额外暴露任何平台内部字段。
    """
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "title": artifact.title,
        "version": artifact.version,
        "publisher_customer_id": artifact.customer_id,
        "is_mine": artifact.customer_id == viewer_customer_id,
        "is_platform": artifact.customer_id == PLATFORM_PUBLISHER_NAME,
        "access_mode": artifact.access_mode or ACCESS_MODE_DEFAULT,
        "allow_copy": bool(artifact.allow_copy),
        "published_at": _fmt(artifact.published_at),
    }


def detail_view(db: Session, artifact: Artifact, *, viewer_customer_id: str) -> dict:
    """详情：元数据 + 面板结构摘要（Q8）+ 报告范围元信息（Q11）。"""
    latest = _latest_version(db, artifact.id)
    spec = latest.spec if latest else None
    out = {
        **card(artifact, viewer_customer_id=viewer_customer_id),
        "description": str((spec or {}).get("description") or "")[:500],
        "version_status": artifact.status,
    }
    if artifact.kind == "dashboard":
        out["panels"] = _panel_summaries(spec)
        out["panel_count"] = len(out["panels"])
    else:
        scope, rng = _report_scope(spec)
        out["data_scope"] = scope
        out["data_range"] = rng
    return out


def list_market_artifacts(
    db: Session,
    *,
    viewer_customer_id: str,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """市场列表：SQL 层 `visibility='market'` + `status='published'` 过滤。

    默认私有铁律之外追加在架态：归档（archived）或恢复为草稿（draft）的
    产物立即从市场消失（发布方自行下架语义——可见性档位不动，在架态
    说了算；重新发布即回归）。
    """
    stmt = select(Artifact).where(
        Artifact.visibility == VISIBILITY_MARKET,
        Artifact.status == "published",
    )
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(Artifact.title.ilike(needle))
    stmt = stmt.order_by(Artifact.published_at.desc().nullslast(), Artifact.id.desc())
    total = len(db.execute(stmt).scalars().all())
    rows = db.execute(stmt.limit(max(1, min(limit, 200))).offset(max(0, offset))).scalars().all()
    return {
        "items": [card(a, viewer_customer_id=viewer_customer_id) for a in rows],
        "total": total,
    }


def resolve_renderable(
    db: Session, artifact_id: int, customer_id: str
) -> tuple[Artifact | None, str | None]:
    """渲染准入：返回 (产物, 来源标签)——实时查表无缓存（撤回即时失效）。

    准入规则与数据集市场逐条对齐（``market/browse.resolve_accessible_dataset``）：
    自有 → own；market 档且已订阅 → 订阅；directed 档且名单命中 → 定向
    授权；任一档位 + 已批准申请 → 申请。**其他一律 None**：private/link
    不经本面（link 是签名链接语义）；directed 档的订阅行不授予访问权
    （定向访问只认 grant 名单——订阅是市场档机制）。

    在架态：非自有产物必须 status=published（归档/草稿即不应答——发布方
    改档案立即失效；自有不受限，编辑器需读回自己的任意态）。
    """
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        return None, None
    if artifact.customer_id == customer_id:
        return artifact, SOURCE_OWN
    if artifact.status != "published":
        return None, None
    # 非自有：必须处于开放档位（private 一律不可见——含 link 档：
    # link 是签名链接语义，不经市场面渲染）
    if artifact.visibility not in ("market", "directed"):
        return None, None
    from . import artifact_service  # noqa: PLC0415 - 循环导入防御

    if artifact.visibility == VISIBILITY_MARKET and artifact_service.is_subscribed(
        db, artifact_id, customer_id
    ):
        return artifact, SOURCE_SUBSCRIPTION
    if artifact.visibility == "directed" and artifact_service.has_grant(
        db, artifact_id, customer_id
    ):
        return artifact, SOURCE_GRANT
    if artifact_service.has_approved_request(db, artifact_id, customer_id):
        return artifact, SOURCE_APPROVED
    return None, None
