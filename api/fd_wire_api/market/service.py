"""市场面服务：可见性阶梯的写入闸门（wire-dataset-marketplace）。

职责边界（本模块只做**发布方**的写入侧，消费侧的浏览/订阅/审批归并行
代理 B 的路由层，两者共用这里的闸门函数与模型）：

- :func:`set_visibility` —— 可见性阶梯的唯一写入口（四档 visibility +
  access_mode + 定向 grant 替换）。所有闸门都在这里，路由层只做鉴权与
  404（不重复实现闸门——避免"换个端点就绕过"）。
- :func:`publish_dataset` / :func:`unpublish_dataset` —— 薄封装（publish =
  切到 market、unpublish = 切回 private），保留给前端并行开发与既有
  契约；语义与 :func:`set_visibility` 完全一致（含留痕与幂等）。

闸门（硬闸，任一不满足 422/403，**绝不静默改档**）：

1. **敏感度**：``敏感`` 档禁止设为 ``directed``/``market``——敏感数据进
   共享面是不可逆风险（design D2 第 3 层）。``link`` 档同样禁止
   （**保守一律禁**：签名链接虽不进市场，但可被转发、可被检索到，
   "敏感"数据一旦出客户边界就不可逆，故敏感档只允许 private）；
2. **空数据集禁止非 private**：未提交过数据的数据集没有可发内容——
   ``row_count=0 且无引擎表``（cell 层判定 = 从未成功同步/建档；api 层
   判定 = 本地行存储零行）。发空集只会让市场多一条零行噪声；
3. **角色**：仅 owner/editor（viewer 403）——由路由层的既有依赖
   （``require_asset_editor`` 同源，或统一依赖的 viewer 方法级门禁）挡，
   本服务只接收已鉴权的 ``actor_sub``（**服务端身份，从不信请求头**）；
4. **定向档需至少一个 grant**（本服务**不硬拒**——无 grant 的 directed
   视为不可见，诚实处理：绝不退化成"全可见"；路由层可对空 grant 的
   directed 请求 422，此处按调用方传入的 grants 原样落行）。

留痕与幂等：

- 每次**实际变更**写一条 ``AssetMetaEvent``（append-only；before/after
  含 visibility 与 access_mode——与既有六字段快照同信封，扩展两键不改
  既有键义）；
- **幂等**：设为当前值（visibility 与 access_mode 均无变化且 grants
  无变化）**不新增事件**、不刷新 published_at（重复点击不留噪声）；
- **撤回即时生效**：切回 private 清 ``published_at``/``published_version``
  （不是软删标记——消费方 allowlist 同步失效的落点）。

版本锚点（design D3）：进入非 private 态时钉
``published_version = f"{dataset.updated_at.isoformat()}@{dataset.row_count}"``
——**如实的可回放锚点**，不是虚构的语义版本号（引擎侧无快照 API 时前端
按"当前内容读"降级并在详情标注，绝不伪造锚点）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..assets.models import (
    ACCESS_MODE_DEFAULT,
    ACCESS_MODES,
    VISIBILITY_DEFAULT,
    VISIBILITY_LEVELS,
    AssetMeta,
    AssetMetaEvent,
)
from ..datasets.models import Dataset, DatasetSync
from ..datasets.service import TIER_API, TIER_CELL
from .models import DatasetShareGrant

#: 敏感档禁发的可见性（private 是唯一允许档——保守：link 也禁）
SENSITIVITY_BLOCKED = "敏感"

#: 敏感档禁止的可见性档位（值域外的档位一律由值域校验拦下）
SENSITIVITY_FORBIDDEN_VISIBILITIES = ("link", "directed", "market")


class VisibilityError(HTTPException):
    """可见性闸门失败（422）——路由层直接透传 detail。"""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=422, detail=detail)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate(visibility: str, access_mode: str) -> None:
    """值域校验（422 带合法清单——照 assets 敏感度的既有文案风格）。"""
    if visibility not in VISIBILITY_LEVELS:
        raise VisibilityError(
            f"可见性只接受 {'、'.join(VISIBILITY_LEVELS)}（收到: {visibility}）"
        )
    if access_mode not in ACCESS_MODES:
        raise VisibilityError(
            f"准入模式只接受 {'、'.join(ACCESS_MODES)}（收到: {access_mode}）"
        )


def _has_content(db: Session, dataset: Dataset) -> bool:
    """数据集是否已有内容（闸门 2：无内容不得进入非 private 档）。

    判定照 datasets 既有口径（诚实递补，绝不猜）：

    - api 层（``storage_tier=api``）：本地行存储实时计数 > 0（``dataset_rows``
      经 ``api_store.schema_of`` 的有效行数口径——含未回滚修正）；
    - cell 层：``row_count > 0``（引擎同步/建档成功即落值）**或**存在一条
      ``status=ok`` 的同步行（引擎侧已成功提交过数据的留痕——极端情况下
      row_count 未回写时仍如实认定"提交过数据"）。

    两者都无 = 从未提交过数据 = 无可发内容。
    """
    if int(dataset.row_count or 0) > 0:
        return True
    if getattr(dataset, "storage_tier", TIER_CELL) == TIER_API:
        try:
            from ..datasets import api_store

            return int(api_store.schema_of(db, dataset_id=dataset.id)["row_count"]) > 0
        except Exception:  # noqa: BLE001 - 存储面不可读：如实按无内容
            return False
    synced = (
        db.execute(
            select(DatasetSync.id)
            .where(DatasetSync.dataset_id == dataset.id, DatasetSync.status == "ok")
            .limit(1)
        )
        .first()
    )
    return synced is not None


def _version_anchor(dataset: Dataset) -> str:
    """发布时刻的版本锚点（如实可回放，绝不虚构语义版本号）。

    形态 ``{updated_at_iso}@{row_count}``：内容变更（同步/刷新/修正）会
    同时动 ``updated_at`` 与 ``row_count``，故该串能标识"发布那一刻的
    内容态"；消费方按锚点读（引擎侧无快照语义时诚实降级为按当前内容
    读并在详情标注——design D3）。
    """
    stamp = dataset.updated_at.isoformat() if dataset.updated_at is not None else "unknown"
    return f"{stamp}@{int(dataset.row_count or 0)}"[:64]


def _current(meta: AssetMeta | None) -> tuple[str, str]:
    """当前可见性二元组（缺行 = 默认私有 + 默认 open）。"""
    if meta is None:
        return VISIBILITY_DEFAULT, ACCESS_MODE_DEFAULT
    return (
        meta.visibility or VISIBILITY_DEFAULT,
        meta.access_mode or ACCESS_MODE_DEFAULT,
    )


def _snapshot(meta: AssetMeta | None) -> dict:
    """八字段快照（既有六字段 + visibility/access_mode）——留痕信封。

    与 ``assets/routes._meta_snapshot`` 同形（缺行 = 全默认值），扩展两键
    不改既有键义：留痕面与合并视图的唯一真源在此，路由层的读视图后续
    可直接复用。
    """
    if meta is None:
        return {
            "domain": "",
            "tags": [],
            "description": "",
            "cadence_hint": None,
            "owner_note": "",
            "sensitivity": "常规",
            "visibility": VISIBILITY_DEFAULT,
            "access_mode": ACCESS_MODE_DEFAULT,
        }
    return {
        "domain": meta.domain or "",
        "tags": list(meta.tags_json or []),
        "description": meta.description or "",
        "cadence_hint": meta.cadence_hint,
        "owner_note": meta.owner_note or "",
        "sensitivity": meta.sensitivity or "常规",
        "visibility": meta.visibility or VISIBILITY_DEFAULT,
        "access_mode": meta.access_mode or ACCESS_MODE_DEFAULT,
    }


def set_visibility(
    db: Session,
    *,
    dataset: Dataset,
    visibility: str,
    access_mode: str | None = None,
    grants: list[str] | None = None,
    actor_sub: str,
) -> AssetMeta:
    """设置数据集可见性（唯一写入口；闸门齐备 + append-only 留痕 + 幂等）。

    参数：
    - ``visibility``：private|link|directed|market（值域外 422 带清单）；
    - ``access_mode``：open|approval（缺省沿用当前值；private/link 档忽略
      但**保留**原值——切回 directed/market 时立即生效）；
    - ``grants``：定向被授权客户清单（**仅 ``visibility=directed`` 时落
      行**；整表替换语义——传什么就是什么，传空即清空。其他档位一律清空
      grant 行，避免"退回私有仍留着授权行"）；
    - ``actor_sub``：服务端身份解析的 Logto sub（留痕 actor，不信请求头）。

    副作用（同一会话，调用方 commit）：
    - 懒创建/更新 ``AssetMeta`` 行（与 assets PATCH 同款懒初始化纪律）；
    - 实际变更时 append 一条 ``AssetMetaEvent``（before/after 八字段）；
    - 定向 grant 行按传入清单整表替换。

    幂等：visibility 与 access_mode 均无变化且 grant 清单无变化 → 不写
    事件、不刷新 published_at（重复点击不留噪声）。
    """
    _validate(visibility, access_mode or ACCESS_MODE_DEFAULT)
    mode = access_mode or ACCESS_MODE_DEFAULT

    meta = db.get(AssetMeta, dataset.id)
    before = _snapshot(meta)
    cur_visibility, cur_mode = _current(meta)

    # 闸门 1：敏感档禁发（保守——link 也禁：链接可转发可检索，不可逆）
    sensitivity = (meta.sensitivity if meta is not None else None) or "常规"
    if sensitivity == SENSITIVITY_BLOCKED and visibility in SENSITIVITY_FORBIDDEN_VISIBILITIES:
        raise VisibilityError(
            f"敏感度「{SENSITIVITY_BLOCKED}」的数据集不允许发布（只允许 private）"
        )

    # 闸门 2：空数据集不得进入非 private 档（无内容可发）
    if visibility != VISIBILITY_DEFAULT and not _has_content(db, dataset):
        raise VisibilityError("数据集尚无内容（未提交过数据），不能开放可见性")

    wanted = [g.strip() for g in (grants or []) if isinstance(g, str) and g.strip()]
    # 去重保序（与 assets 标签清洗同款——授权清单不允许重复行）
    wanted = list(dict.fromkeys(wanted))
    if visibility == "directed":
        existing = [
            row.customer_id
            for row in db.execute(
                select(DatasetShareGrant)
                .where(DatasetShareGrant.dataset_id == dataset.id)
                .order_by(DatasetShareGrant.id)
            )
            .scalars()
            .all()
        ]
    else:
        existing = []
    grants_changed = sorted(wanted) != sorted(existing)
    changed = (visibility != cur_visibility) or (mode != cur_mode) or grants_changed

    if meta is None:
        meta = AssetMeta(dataset_id=dataset.id)
        db.add(meta)
    meta.visibility = visibility
    meta.access_mode = mode
    meta.updated_by = actor_sub[:64]

    if not changed:
        # 幂等：无任何变化 → 不写事件、不动时间戳（重复点击不留噪声）
        db.flush()
        return meta

    now = _utcnow_naive()
    if visibility == VISIBILITY_DEFAULT:
        # 撤回即时生效：清锚点与时间戳（不是软删标记）
        meta.published_at = None
        meta.published_version = None
    else:
        if meta.published_at is None:
            meta.published_at = now
        meta.published_version = _version_anchor(dataset)
    meta.updated_at = now

    # 定向 grant 整表替换（非 directed 档一律清空——退回私有不留授权行）
    db.execute(
        delete(DatasetShareGrant).where(DatasetShareGrant.dataset_id == dataset.id)
    )
    for customer_id in wanted if visibility == "directed" else []:
        db.add(
            DatasetShareGrant(
                dataset_id=dataset.id, customer_id=customer_id[:64], created_at=now
            )
        )

    after = _snapshot(meta)
    db.add(
        AssetMetaEvent(
            dataset_id=dataset.id,
            actor_sub=actor_sub[:64],
            at=now,
            before_json=before,
            after_json=after,
        )
    )
    db.flush()
    return meta


def publish_dataset(
    db: Session, *, dataset: Dataset, actor_sub: str, access_mode: str = "open"
) -> AssetMeta:
    """发布到市场（薄封装：visibility=market；闸门/留痕/幂等全同）。"""
    return set_visibility(
        db, dataset=dataset, visibility="market", access_mode=access_mode,
        actor_sub=actor_sub,
    )


def unpublish_dataset(db: Session, *, dataset: Dataset, actor_sub: str) -> AssetMeta:
    """撤回（薄封装：visibility=private；清锚点，即时生效）。"""
    return set_visibility(db, dataset=dataset, visibility="private", actor_sub=actor_sub)


def enforce_sensitivity_interlock(
    db: Session, *, dataset: Dataset, meta: AssetMeta, actor_sub: str
) -> dict | None:
    """敏感度互锁（wire-artifact-marketplace design D12，顺带修既有缺口）。

    数据集处于**非 private 档**时敏感度被改为「敏感」→ 自动撤回 private
    （复用 :func:`set_visibility` 单写入口：清锚点 + 清定向 grant 行 +
    追加八字段留痕事件——撤回与敏感度变更同事务原子生效）。两通道一起严：
    数据集面在此互锁，看板/报告面由市场渲染点实时复检（Q5 双点闸）。

    返回 ``{"from": 原档位, "to": "private"}``（未触发 None）。调用方
    （assets PATCH）据此在响应体如实告知前端；**绝不静默改档**。
    """
    if (meta.sensitivity or "常规") != SENSITIVITY_BLOCKED:
        return None
    visibility, _mode = _current(meta)
    if visibility == VISIBILITY_DEFAULT:
        return None
    # 写入口返回的行与 meta 是同一会话同一对象（就地更新）；此处不取返回值
    set_visibility(
        db, dataset=dataset, visibility=VISIBILITY_DEFAULT, actor_sub=actor_sub
    )
    return {"from": visibility, "to": VISIBILITY_DEFAULT}


def visibility_view(meta: AssetMeta | None) -> dict:
    """可见性读视图（响应段；缺行 = 默认私有 + open）。"""
    visibility, access_mode = _current(meta)
    return {
        "visibility": visibility,
        "access_mode": access_mode if visibility in ("directed", "market") else None,
        "published_at": meta.published_at if meta is not None else None,
        "published_version": meta.published_version if meta is not None else None,
    }


def list_grants(db: Session, dataset_id: str) -> list[str]:
    """定向授权清单（按落行序；无 grant 的 directed = 不可见，诚实返回空）。"""
    return [
        row.customer_id
        for row in db.execute(
            select(DatasetShareGrant)
            .where(DatasetShareGrant.dataset_id == dataset_id)
            .order_by(DatasetShareGrant.id)
        )
        .scalars()
        .all()
    ]


__all__ = [
    "SENSITIVITY_FORBIDDEN_VISIBILITIES",
    "VisibilityError",
    "list_grants",
    "publish_dataset",
    "set_visibility",
    "unpublish_dataset",
    "visibility_view",
]
