"""产物市场服务（wire-artifact-marketplace）：可见性写入 + 浏览 + 订阅 + 复制。

与 dataset 市场（``market/service.py`` + ``market/browse.py``）同构，但住
``AiBase``（ai 库）——两者的表分库，代码层共享模式不共用表（design D1）。

红线与纪律（与数据集市场逐条对齐）：

- **默认私有**：``visibility`` 默认 ``private``；市场列表**在 SQL 层**过滤
  ``visibility='market'``——未开放的产物在任何检索结果中不存在；
- **撤回即时失效**：改回 private → 订阅者渲染请求立即被拒（实时查表）；
- **敏感度双点闸（Q5）**：绑定的数据集含「敏感」档 → 禁设 market（422），
  且**渲染时实时复检**（事后数据集成敏感 → 拒绝渲染）；
- **快照剥离（Q4）**：复制的 spec 递归移除全部 ``snapshot`` 键——复制者拿到
  分析结构（布局/图表/查询绑定），不是数据；
- **详情结构摘要（Q8）**：市场详情只给 ``{id, type, title}`` 面板清单，
  不含 config/snapshot/query；
- **快照时间（Q3/Q9）**：渲染面返回 spec 与 ``refreshed_at``（刷新路径写入
  spec；缺失诚实为 None → 前端显「未知」），**不自动刷新**。
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..artifacts.models import (
    Artifact,
    ArtifactAccessRequest,
    ArtifactMarketSubscription,
    ArtifactShareGrant,
    ArtifactVersion,
)

logger = logging.getLogger(__name__)

#: 可见性档位（与数据集市场逐字一致）
VISIBILITY_LEVELS: tuple[str, ...] = ("private", "link", "directed", "market")
VISIBILITY_DEFAULT = "private"
VISIBILITY_MARKET = "market"
VISIBILITY_LINK = "link"
VISIBILITY_DIRECTED = "directed"

#: 开放模式
ACCESS_MODES: tuple[str, ...] = ("open", "approval")
ACCESS_MODE_DEFAULT = "open"

#: 可进入市场的产物 kind（用户裁决：看板 + 研究报告）
MARKETABLE_KINDS: tuple[str, ...] = ("dashboard", "research_report")

#: 绑定数据集敏感档值（与 assets 敏感度三级一致）
SENSITIVITY_BLOCKED = "敏感"


class ArtifactMarketError(Exception):
    """产物市场业务错误（路由层翻译成响应码）。"""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate_levels(visibility: str, access_mode: str) -> None:
    if visibility not in VISIBILITY_LEVELS:
        raise ArtifactMarketError(
            f"未知可见性档位 {visibility!r}（允许: {', '.join(VISIBILITY_LEVELS)}）"
        )
    if access_mode not in ACCESS_MODES:
        raise ArtifactMarketError(
            f"未知开放模式 {access_mode!r}（允许: {', '.join(ACCESS_MODES)}）"
        )


# ------------------------------------------------------------------ 绑定解析


def bound_dataset_ids(spec: dict | None) -> list[str]:
    """从产物 spec 提取绑定的数据集 id（看板 panel.query.dataset_id）。"""
    ids: list[str] = []
    if not isinstance(spec, dict):
        return ids
    body = spec.get("spec") if isinstance(spec.get("spec"), dict) else spec
    panels = body.get("panels") if isinstance(body, dict) else None
    if not isinstance(panels, list):
        return ids
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        query = panel.get("query")
        if isinstance(query, dict):
            did = query.get("dataset_id")
            if isinstance(did, str) and did and did not in ids:
                ids.append(did)
    return ids


def _sensitivity_of(dataset_id: str) -> str:
    """读绑定数据集的**有效敏感度**（实时，不缓存——Q10 裁决）。

    口径 = 自身声明与祖先闭包取严（wire-sensitivity-market-closure：与数据集面
    同源同一 helper ``assets.sensitivity``，堵「敏感祖先链上的派生子集绑进
    看板」旁路）。**父集不全（dangling/环/超深）→ 按「敏感」fail-closed**
    （产物面闸直接拒，与数据集面闸同语义）；数据集已不存在/查询失败保守
    回落「常规」（不因基建问题卡死发布；真正的敏感数据在 assets 侧有值）。
    """
    try:
        from ..assets.sensitivity import effective_sensitivity  # noqa: PLC0415
        from ..datasets.db import get_session  # noqa: PLC0415

        session = get_session()
        try:
            from sqlalchemy import text  # noqa: PLC0415

            owner = session.execute(
                text("SELECT customer_id FROM datasets WHERE id = :did"),
                {"did": dataset_id},
            ).scalar_one_or_none()
            if not isinstance(owner, str) or not owner:
                return "常规"  # 数据集已不存在——闭包无可算，沿既有放行口径
            eff = effective_sensitivity(session, owner, dataset_id)
            if eff["incomplete"]:
                return SENSITIVITY_BLOCKED  # fail-closed（闸文案点名该数据集）
            return eff["level"]
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - 敏感度读取失败保守放行（发布方自担）
        logger.warning("绑定数据集有效敏感度读取失败 dataset_id=%s", dataset_id)
        return "常规"


def sensitive_bound_datasets(spec: dict | None) -> list[str]:
    """绑定数据集里敏感档的清单（Q5 双点闸的判定核心）。"""
    return [
        did for did in bound_dataset_ids(spec)
        if _sensitivity_of(did) == SENSITIVITY_BLOCKED
    ]


# ------------------------------------------------------------------ 写入口


def _latest_version(db: Session, artifact_id: int) -> ArtifactVersion | None:
    return db.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def set_visibility(
    db: Session,
    *,
    artifact: Artifact,
    visibility: str,
    access_mode: str | None = None,
    allow_copy: bool | None = None,
    grants: list[str] | None = None,
    actor_sub: str,
) -> Artifact:
    """可见性/开放模式/复制开关/定向名单的单写入口（同数据集市场纪律）。

    闸门：值域；仅 published 可设非 private；绑定敏感数据集禁 market；
    幂等（同值零变更不留痕——含 grant 清单）；append-only 留痕
    （status_history 追加 visibility 事件，8 字段 before/after 快照）。
    ``grants`` 整表替换（仅 directed 档落行；其他档一律清空，避免「退回
    私有仍留着授权行」）。
    """
    _validate_levels(visibility, access_mode or ACCESS_MODE_DEFAULT)

    if artifact.kind not in MARKETABLE_KINDS:
        raise ArtifactMarketError(
            f"产物类型 {artifact.kind!r} 不支持市场可见性"
            f"（允许: {', '.join(MARKETABLE_KINDS)}）"
        )
    if visibility != VISIBILITY_DEFAULT and artifact.status != "published":
        raise ArtifactMarketError(
            "仅已发布（published）的产物可设置非私有档位——请先发布",
            status_code=409,
        )

    mode = access_mode or artifact.access_mode or ACCESS_MODE_DEFAULT
    copy_flag = artifact.allow_copy if allow_copy is None else bool(allow_copy)

    if visibility == VISIBILITY_MARKET:
        latest = _latest_version(db, artifact.id)
        blocked = sensitive_bound_datasets(latest.spec if latest else None)
        if blocked:
            raise ArtifactMarketError(
                f"绑定数据集含敏感档（{', '.join(blocked[:3])}）——"
                "禁止设为 market（可选 link/directed）"
            )

    wanted = [
        g.strip() for g in (grants or []) if isinstance(g, str) and g.strip()
    ]
    wanted = list(dict.fromkeys(wanted))  # 去重保序（不允许重复行）
    # 有效清单按档位归一：非 directed 档一律清空——幂等比较用归一后的值
    # （否则「market + 传了 grants」每次调用都被判有变更，重复留痕噪声）
    effective = wanted if visibility == VISIBILITY_DIRECTED else []
    current_grants = list_grants(db, artifact.id)
    grants_changed = sorted(effective) != sorted(current_grants)

    before = {
        "visibility": artifact.visibility,
        "access_mode": artifact.access_mode,
        "allow_copy": bool(artifact.allow_copy),
        "published_at": artifact.published_at.isoformat() if artifact.published_at else None,
        "published_version": artifact.published_version,
        "grants": current_grants,
        "status": artifact.status,
        "version": artifact.version,
    }
    same = (
        artifact.visibility == visibility
        and artifact.access_mode == mode
        and bool(artifact.allow_copy) == copy_flag
        and not grants_changed
    )
    if same:
        return artifact  # 幂等：同值零变更

    artifact.visibility = visibility
    artifact.access_mode = mode
    artifact.allow_copy = copy_flag
    if visibility == VISIBILITY_DEFAULT:
        artifact.published_at = None
        artifact.published_version = None
    else:
        artifact.published_at = _utcnow_naive()
        artifact.published_version = artifact.version

    # 定向 grant 整表替换（非 directed 档一律清空）
    db.execute(
        delete(ArtifactShareGrant).where(ArtifactShareGrant.artifact_id == artifact.id)
    )
    now = _utcnow_naive()
    for customer_id in effective:
        db.add(
            ArtifactShareGrant(
                artifact_id=artifact.id, customer_id=customer_id[:64], created_at=now
            )
        )

    after = {
        "visibility": artifact.visibility,
        "access_mode": artifact.access_mode,
        "allow_copy": bool(artifact.allow_copy),
        "published_at": artifact.published_at.isoformat() if artifact.published_at else None,
        "published_version": artifact.published_version,
        "grants": effective,
        "status": artifact.status,
        "version": artifact.version,
    }
    history = list(artifact.status_history or [])
    history.append(
        {
            "status": artifact.status,
            "at": _utcnow_naive().isoformat() + "Z",
            "by": actor_sub[:64],
            "note": "visibility",
            "before": before,
            "after": after,
        }
    )
    artifact.status_history = history
    db.commit()
    db.refresh(artifact)
    return artifact


def list_grants(db: Session, artifact_id: int) -> list[str]:
    """定向授权清单（按落行序；无 grant 的 directed = 不可见，诚实返回空）。"""
    return [
        row.customer_id
        for row in db.execute(
            select(ArtifactShareGrant)
            .where(ArtifactShareGrant.artifact_id == artifact_id)
            .order_by(ArtifactShareGrant.id)
        )
        .scalars()
        .all()
    ]


def visibility_view(artifact: Artifact) -> dict:
    return {
        "visibility": artifact.visibility or VISIBILITY_DEFAULT,
        "access_mode": artifact.access_mode or ACCESS_MODE_DEFAULT,
        "allow_copy": bool(artifact.allow_copy),
        "published_at": artifact.published_at.isoformat() + "Z"
        if artifact.published_at else None,
        "published_version": artifact.published_version,
    }


# ------------------------------------------------------------------ 关系查询


def is_subscribed(db: Session, artifact_id: int, customer_id: str) -> bool:
    return (
        db.execute(
            select(ArtifactMarketSubscription.id).where(
                ArtifactMarketSubscription.artifact_id == artifact_id,
                ArtifactMarketSubscription.customer_id == customer_id,
            )
        ).scalar_one_or_none()
        is not None
    )


def has_grant(db: Session, artifact_id: int, customer_id: str) -> bool:
    return (
        db.execute(
            select(ArtifactShareGrant.id).where(
                ArtifactShareGrant.artifact_id == artifact_id,
                ArtifactShareGrant.customer_id == customer_id,
            )
        ).scalar_one_or_none()
        is not None
    )


def has_approved_request(db: Session, artifact_id: int, customer_id: str) -> bool:
    return (
        db.execute(
            select(ArtifactAccessRequest.id).where(
                ArtifactAccessRequest.artifact_id == artifact_id,
                ArtifactAccessRequest.requester_customer_id == customer_id,
                ArtifactAccessRequest.status == "approved",
            )
        ).scalar_one_or_none()
        is not None
    )


def subscribe(db: Session, *, artifact_id: int, customer_id: str) -> dict:
    """订阅（open 档）——幂等。approval 档应走申请流。"""
    if is_subscribed(db, artifact_id, customer_id):
        return {"subscribed": True, "created": False}
    db.add(
        ArtifactMarketSubscription(
            artifact_id=artifact_id, customer_id=customer_id
        )
    )
    db.commit()
    return {"subscribed": True, "created": True}


def unsubscribe(db: Session, *, artifact_id: int, customer_id: str) -> dict:
    row = db.execute(
        select(ArtifactMarketSubscription).where(
            ArtifactMarketSubscription.artifact_id == artifact_id,
            ArtifactMarketSubscription.customer_id == customer_id,
        )
    ).scalar_one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()
        return {"subscribed": False, "removed": True}
    return {"subscribed": False, "removed": False}


# ------------------------------------------------------------------ 复制


def _strip_snapshots(spec: Any) -> Any:
    """递归剥离全部 snapshot 键（Q4：复制的是分析结构不是数据）。"""
    if isinstance(spec, dict):
        return {
            k: _strip_snapshots(v)
            for k, v in spec.items()
            if k != "snapshot"
        }
    if isinstance(spec, list):
        return [_strip_snapshots(item) for item in spec]
    return spec


def copy_artifact(
    db: Session,
    *,
    artifact: Artifact,
    caller_customer_id: str,
) -> dict:
    """复制到 caller 账号（draft v1）——快照剥离 + 绑定重映射 + 溯源指针。

    caller 对面板绑定数据集有访问权（自有/已订阅/定向）→ 保留绑定；
    无访问权 → 该面板标 ``binding_status: unbound``（不赋权不造假数据）。

    准入：自有产物可复制（自己的内容换一份独立 spec）；他人产物须仍处
    市场/定向档（撤回即时失效——旧订阅者也被拦）且发布方开启 allow_copy
    且 caller 有订阅/授权/已批申请。
    """
    if artifact.customer_id != caller_customer_id:
        if (
            artifact.visibility not in (VISIBILITY_MARKET, VISIBILITY_DIRECTED)
            or artifact.status != "published"
        ):
            raise ArtifactMarketError("产物已撤回或已下架，不可复制", status_code=404)
        if not artifact.allow_copy:
            raise ArtifactMarketError(
                "发布方未允许复制此产物", status_code=403
            )
        if not (
            (
                artifact.visibility == VISIBILITY_MARKET
                and is_subscribed(db, artifact.id, caller_customer_id)
            )
            or (
                artifact.visibility == VISIBILITY_DIRECTED
                and has_grant(db, artifact.id, caller_customer_id)
            )
            or has_approved_request(db, artifact.id, caller_customer_id)
        ):
            raise ArtifactMarketError(
                "需先订阅该产物才能复制", status_code=403
            )

    latest = _latest_version(db, artifact.id)
    if latest is None:
        raise ArtifactMarketError("产物无版本可复制", status_code=404)

    stripped = _strip_snapshots(copy.deepcopy(latest.spec or {}))

    # 绑定重映射（面板级）
    bound: list[str] = []
    unbound: list[str] = []
    body = stripped.get("spec") if isinstance(stripped.get("spec"), dict) else stripped
    panels = body.get("panels") if isinstance(body, dict) else None
    if isinstance(panels, list):
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            query = panel.get("query")
            did = query.get("dataset_id") if isinstance(query, dict) else None
            if not isinstance(did, str) or not did:
                continue
            if _caller_can_read_dataset(did, caller_customer_id):
                bound.append(did)
            else:
                unbound.append(did)
                panel["binding_status"] = "unbound"

    title = artifact.title or f"{artifact.kind}"
    new_artifact = Artifact(
        customer_id=caller_customer_id,
        kind=artifact.kind,
        title=f"{title}（复制）"[:200],
        status="draft",
        version=1,
        status_history=[
            {
                "status": "draft",
                "at": _utcnow_naive().isoformat() + "Z",
                "by": caller_customer_id,
                "note": f"copied_from:{artifact.id}",
            }
        ],
        copied_from_artifact_id=artifact.id,
    )
    db.add(new_artifact)
    db.flush()
    db.add(
        ArtifactVersion(
            artifact_id=new_artifact.id,
            version=1,
            spec=stripped,
            operator=f"copy:{caller_customer_id}",
        )
    )
    db.commit()
    db.refresh(new_artifact)
    return {
        "artifact_id": new_artifact.id,
        "title": new_artifact.title,
        "kind": new_artifact.kind,
        "copied_from_artifact_id": artifact.id,
        "binding_report": {
            "bound": len(bound),
            "unbound": len(unbound),
            "unbound_datasets": unbound,
        },
    }


def _caller_can_read_dataset(dataset_id: str, customer_id: str) -> bool:
    """caller 对某数据集是否有读权（自有 / 市场订阅 / 定向授权）。

    经 dataset 市场 browse 的实时判定（跨切片只读调用；模块缺席保守
    视为无权限——unbound 诚实标注，不越权）。
    """
    try:
        from ..datasets.db import get_session  # noqa: PLC0415
        from . import browse as dataset_browse  # noqa: PLC0415

        session = get_session()
        try:
            dataset, source = dataset_browse.resolve_accessible_dataset(
                session, dataset_id, customer_id
            )
            return dataset is not None
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - 判定失败保守 unbound
        logger.warning("数据集读权判定失败 dataset_id=%s", dataset_id)
        return False
