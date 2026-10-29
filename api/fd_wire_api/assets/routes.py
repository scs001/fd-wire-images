"""资产目录 API 路由（wire-asset-catalog 任务组 1/2）。

客户侧（Bearer ID token → ``require_console_customer_id``，与 datasets/
entities 切片同款身份键；表挂 ``DatasetsBase``——datasets 库会话）：

- ``GET   /api/v1/datasets/{dataset_id}/asset-meta``        合并视图（缺行
  LEFT JOIN + COALESCE 全默认值；**懒行不落库**——GET 绝不创建）
- ``PATCH /api/v1/datasets/{dataset_id}/asset-meta``        懒创建/更新
  （owner/editor；viewer 403；敏感度三级校验 422 带合法清单；append 留痕）
- ``GET   /api/v1/datasets/{dataset_id}/asset-meta/events`` 留痕时间线
  （最近 N 条，新→旧）
- ``GET   /api/v1/assets``                                  资产目录列表
  （域/标签/敏感度/来源类型/新鲜度组合筛选 + 名称/描述搜索 + 分页；
  客户隔离照 datasets 列表口径——本客户命名空间，团队成员共享同客户面）

红线（design 风险面）：资产元数据（标签/描述/责任人/敏感度）只进本面，
公开目录（catalog/ 只读镜像）与未鉴权端点绝不返回——后者由统一鉴权依赖
的 401 兜底，测试显式覆盖（任务 6.1）。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..admin.db import get_db as get_platform_db
from ..billing.models import Customer
from ..console.auth import (
    bearer_token,
    require_console_customer_id,
    resolve_member_role,
    verify_id_token,
)
from ..datasets.db import get_db as get_datasets_db
from ..datasets.models import Dataset
from .models import SENSITIVITY_DEFAULT, SENSITIVITY_LEVELS, AssetMeta, AssetMetaEvent

router = APIRouter(prefix="/api/v1", tags=["assets"])

#: 域筛选的「未补全」哨兵（design D1：未补全条目归未分类，这是特性）
DOMAIN_UNCLASSIFIED = "未分类"


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dataset_or_404(db: Session, dataset_id: str, customer_id: str) -> Dataset:
    """数据集属主校验（他企/不存在统一 404 不泄露——照 datasets 先例）。"""
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不存在")
    return dataset


def require_asset_editor(
    request: Request, db: Session = Depends(get_platform_db)
) -> tuple[Customer, str]:
    """资产元数据编辑依赖（wire-team-spaces 三角角色）：owner/editor 可写。

    与 ``require_console_owner`` 同源解析（verify_id_token →
    resolve_member_role，owner 优先→成员回退），未映射 401 同文案；viewer
    403（资产元数据仅 owner/editor 可补全）。返回 (Customer, caller_sub)——
    sub 落 ``updated_by`` 与留痕 actor（服务端身份，不信请求头）。
    """
    identity = verify_id_token(bearer_token(request))
    resolved = resolve_member_role(db, identity.sub)
    if resolved is None:
        raise HTTPException(
            status_code=401,
            detail="登录身份尚未映射到客户（请先完成首次登录开户）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    customer, role = resolved
    if role == "viewer":
        raise HTTPException(
            status_code=403,
            detail="团队成员权限不足（资产元数据仅 owner/editor 可编辑）",
        )
    return customer, identity.sub


# ------------------------------------------------------------------ schemas


class AssetMetaPatchIn(BaseModel):
    """元数据补全请求体（全字段可选——只改提供的；cadence_hint 显式 null 清空）。"""

    domain: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = None
    description: str | None = None
    cadence_hint: str | None = Field(default=None, max_length=16)
    owner_note: str | None = Field(default=None, max_length=200)
    #: 常规|内部|敏感（路由层校验——422 需带合法清单，故不用 Literal）
    sensitivity: str | None = None


# ------------------------------------------------- 视图与快照（COALESCE 语义）


def _clean_tags(tags: list[str]) -> list[str]:
    """标签清洗：逐项去空白、去空项、保序去重。"""
    cleaned: list[str] = []
    for raw in tags:
        tag = (raw or "").strip()
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def _meta_snapshot(meta: AssetMeta | None) -> dict:
    """六字段快照（缺行 = 全默认值——留痕 before 与合并视图的同源）。"""
    if meta is None:
        return {
            "domain": "",
            "tags": [],
            "description": "",
            "cadence_hint": None,
            "owner_note": "",
            "sensitivity": SENSITIVITY_DEFAULT,
        }
    return {
        "domain": meta.domain or "",
        "tags": list(meta.tags_json or []),
        "description": meta.description or "",
        "cadence_hint": meta.cadence_hint,
        "owner_note": meta.owner_note or "",
        "sensitivity": meta.sensitivity or SENSITIVITY_DEFAULT,
    }


def _is_api_tier(dataset: Dataset) -> bool:
    """api 层数据集（storage_tier=api 免费档轻量存储；兼容 source_kind 口径）。"""
    return (
        getattr(dataset, "storage_tier", None) == "api"
        or dataset.source_kind == "api"
    )


# 权威源节奏查询的进程内 TTL 缓存（key=dataset_key → (到期时刻, code→cadence)）。
# HttpRegistryClient 只有全量枚举一档（无单条查询），不缓存则每次详情页都
# 拉全量登记表；失败也缓存（空映射，短 TTL）防权威源故障时被打爆。
_REGISTRY_CADENCE_TTL_SECONDS = 300.0
_REGISTRY_CADENCE_FAIL_TTL_SECONDS = 60.0
_registry_cadence_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _normalize_cadence(frequency: str | None) -> str | None:
    """权威源 frequency 文案 → cadence 口径（daily/weekly/monthly/…）。"""
    text = (frequency or "").strip().lower()
    if not text:
        return None
    if "日" in text or "daily" in text or "day" in text:
        return "daily"
    if "周" in text or "week" in text:
        return "weekly"
    if "月" in text or "month" in text:
        return "monthly"
    if "季" in text or "quarter" in text:
        return "quarterly"
    if "年" in text or "year" in text or "annual" in text:
        return "yearly"
    return None


def _lookup_registry_cadence(dataset_key: str) -> str | None:
    """api 层数据集 cadence_effective：从指标注册表取更新频率（design D4）。

    - 经既有目录客户端（``catalog/client.HttpRegistryClient``）查权威登记
      表，按 dataset key 匹配语义码/原生码；取不到/客户端不可用/超时 →
      None（界面如实显示「未知」），**绝不自动写回** cadence_hint；
    - 全量枚举无单条查询面，TTL 缓存兜住详情页频次（见上）。
    """
    now = time.monotonic()
    cached = _registry_cadence_cache.get(dataset_key)
    if cached is not None and cached[0] > now:
        return cached[1].get(dataset_key)
    freq_map: dict[str, str] = {}
    failed = False
    try:
        from ..catalog.client import HttpRegistryClient

        batch = HttpRegistryClient().fetch_concepts()
        for record in batch.items:
            cadence = _normalize_cadence(record.frequency)
            if cadence is None:
                continue
            for code in (record.code, record.native_code):
                if code:
                    freq_map[code] = cadence
    except Exception:
        failed = True  # 权威源不可达/未配置/超时——如实未知，不抛错不写回
    ttl = _REGISTRY_CADENCE_FAIL_TTL_SECONDS if failed else _REGISTRY_CADENCE_TTL_SECONDS
    _registry_cadence_cache[dataset_key] = (now + ttl, freq_map)
    return freq_map.get(dataset_key)


def _cadence_effective(dataset: Dataset, meta: AssetMeta | None) -> str | None:
    """有效节奏：显式 cadence_hint 优先；api 层数据集空节奏时查注册表。"""
    hint = (meta.cadence_hint or "").strip() if meta is not None else ""
    if hint:
        return hint
    if _is_api_tier(dataset):
        return _lookup_registry_cadence(dataset.key)
    return None


def _meta_view(
    db: Session, dataset: Dataset, meta: AssetMeta | None
) -> dict:
    """合并视图（缺行 COALESCE 默认值；GET 懒行不落库）。

    附有效敏感度三字段（wire-sensitivity-propagation）：level/inherited/
    incomplete——闭包按需计算（详情页单点，成本可忽略）。
    """
    from .sensitivity import effective_sensitivity  # noqa: PLC0415

    eff = effective_sensitivity(db, dataset.customer_id, dataset.id)
    return {
        "dataset_id": dataset.id,
        **_meta_snapshot(meta),
        "updated_by": meta.updated_by if meta is not None else None,
        "updated_at": meta.updated_at if meta is not None else None,
        "cadence_effective": _cadence_effective(dataset, meta),
        "sensitivity_effective": eff["level"],
        "sensitivity_inherited": eff["inherited"],
        "sensitivity_incomplete": eff["incomplete"],
    }


# ------------------------------------------------------------------ 端点


@router.get("/datasets/{dataset_id}/asset-meta")
def get_asset_meta(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """元数据合并视图（未补全 = 全默认值；懒行——GET 绝不创建行）。"""
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    meta = db.get(AssetMeta, dataset_id)
    return _meta_view(db, dataset, meta)


@router.patch("/datasets/{dataset_id}/asset-meta")
def patch_asset_meta(
    dataset_id: str,
    payload: AssetMetaPatchIn,
    principal: tuple[Customer, str] = Depends(require_asset_editor),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """懒创建/更新元数据行：只改提供的字段；append 留痕（前后六字段快照）。

    - 敏感度只接受 常规/内部/敏感（design D3），非法值 422 带合法清单；
    - 首次 PATCH 才落行（懒初始化，design D1），before 快照取全默认值；
    - 修改人取服务端身份解析的 sub（updated_by + 留痕 actor 同源）。
    """
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))

    if payload.sensitivity is not None and payload.sensitivity not in SENSITIVITY_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"敏感度只接受 {'、'.join(SENSITIVITY_LEVELS)}"
                f"（收到: {payload.sensitivity}）"
            ),
        )

    provided = payload.model_fields_set
    meta = db.get(AssetMeta, dataset_id)
    before = _meta_snapshot(meta)
    if meta is None:
        meta = AssetMeta(dataset_id=dataset_id)
        db.add(meta)
    if "domain" in provided and payload.domain is not None:
        meta.domain = payload.domain.strip()
    if "tags" in provided and payload.tags is not None:
        meta.tags_json = _clean_tags(payload.tags)
    if "description" in provided and payload.description is not None:
        meta.description = payload.description
    if "cadence_hint" in provided:
        # 显式提交（含 null）才动——null = 清空节奏（回到「未知」）
        meta.cadence_hint = (payload.cadence_hint or "").strip() or None
    if "owner_note" in provided and payload.owner_note is not None:
        meta.owner_note = payload.owner_note
    if "sensitivity" in provided and payload.sensitivity is not None:
        meta.sensitivity = payload.sensitivity
    meta.updated_by = sub[:64]
    meta.updated_at = _utcnow_naive()
    after = _meta_snapshot(meta)

    # 留痕（design D7：append-only 小表，只 INSERT 无改删代码路径）
    db.add(
        AssetMetaEvent(
            dataset_id=dataset_id,
            actor_sub=sub[:64],
            at=_utcnow_naive(),
            before_json=before,
            after_json=after,
        )
    )

    # 敏感度互锁（wire-artifact-marketplace design D12）：改为「敏感」且该
    # 数据集处于非 private 档 → 自动撤回 private（复用 market 单写入口：
    # 清锚点 + 清定向 grant 行 + 追加八字段留痕；与本次 PATCH 同事务原子）。
    # 惰性导入避免 import 环（market 包路由依赖本模块的 require_asset_editor）。
    from ..market.service import (  # noqa: PLC0415
        SENSITIVITY_BLOCKED,
        enforce_sensitivity_interlock,
        propagate_downward_reverts,
    )

    reverted = enforce_sensitivity_interlock(
        db, dataset=dataset, meta=meta, actor_sub=sub
    )
    # 向下传播撤回（wire-sensitivity-propagation 任务 3.2）：敏感化沿派生
    # 闭包连带撤回非 private 后代（同一写入口、同一事务；幂等——已 private
    # 的后代不动；祖先改回常规不自动恢复）。
    propagated: list[dict] = []
    if payload.sensitivity == SENSITIVITY_BLOCKED and (
        "sensitivity" in provided
    ):
        propagated = propagate_downward_reverts(db, dataset=dataset, actor_sub=sub)
    db.commit()
    db.refresh(meta)
    view = _meta_view(db, dataset=dataset, meta=meta)
    if reverted is not None:
        # 如实告知前端（绝不静默改档）：可见性已被互锁自动撤回
        view["auto_reverted_visibility"] = reverted
    if propagated:
        view["propagated_reverts"] = propagated
    return view


@router.get("/datasets/{dataset_id}/asset-meta/events")
def list_asset_meta_events(
    dataset_id: str,
    limit: int = Query(default=100, ge=1, le=100, description="最近 N 条（新→旧）"),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """留痕时间线（append-only 的读面；他企/不存在 404 不泄露）。"""
    _dataset_or_404(db, dataset_id, customer_id)
    rows = (
        db.execute(
            select(AssetMetaEvent)
            .where(AssetMetaEvent.dataset_id == dataset_id)
            .order_by(AssetMetaEvent.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return {
        "dataset_id": dataset_id,
        "events": [
            {
                "id": event.id,
                "actor_sub": event.actor_sub,
                "at": event.at,
                "before": event.before_json,
                "after": event.after_json,
            }
            for event in rows
        ],
    }


@router.get("/assets")
def list_assets(
    domain: str | None = Query(default=None, max_length=64, description="业务域；未分类=未补全条目"),
    tag: str | None = Query(default=None, max_length=64, description="单标签（命中 tags 列表即中）"),
    sensitivity: str | None = Query(default=None, max_length=16, description="常规|内部|敏感"),
    source_kind: str | None = Query(default=None, max_length=16, description="upload|connector|declared|derived"),
    freshness: str | None = Query(default=None, pattern="^(fresh|stale|unknown)$", description="新鲜度（wire-dataset-quality 判定）"),
    q: str | None = Query(default=None, max_length=200, description="名称/描述关键字"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_datasets_db),
) -> dict:
    """资产目录列表（数据集摘要 + 元数据合并视图；组合筛选 + 搜索 + 分页）。

    客户隔离照 datasets 列表口径（本客户命名空间；团队成员共享同客户面）。
    域/标签/敏感度作用于**合并视图**（缺行按默认值参与——敏感度缺省常规、
    域缺省未分类），保证「未补全条目归未分类可筛」。

    响应形状（任务 1.1 回嵌套契约）：条目 = 数据集摘要平铺字段 +
    ``updated_by``/``updated_at`` + 嵌套 ``asset`` 段（六字段快照 +
    ``cadence_effective``）——元数据不再平铺在条目顶层。
    """
    # 跨模块钩子（wire-dataset-quality，并行落片中）：freshness_statuses(
    # customer_id, dataset_ids) → {dataset_id: fresh|stale|unknown}。刻意不在
    # 模块顶部导入（切片可能未部署——引擎/版本滚动窗口），函数体内
    # try/except ImportError → 全员按 unknown 处理（诚实降级，不误判）。
    freshness_hook = None
    try:
        from ..quality.service import freshness_statuses as freshness_hook
    except ImportError:
        freshness_hook = None

    if sensitivity is not None and sensitivity not in SENSITIVITY_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"敏感度只接受 {'、'.join(SENSITIVITY_LEVELS)}"
                f"（收到: {sensitivity}）"
            ),
        )

    datasets = (
        db.execute(
            select(Dataset)
            .where(Dataset.customer_id == customer_id)
            .order_by(Dataset.created_at.desc(), Dataset.id)
        )
        .scalars()
        .all()
    )
    metas = {
        meta.dataset_id: meta
        for meta in db.execute(
            select(AssetMeta).where(
                AssetMeta.dataset_id.in_([d.id for d in datasets])
            )
        )
        .scalars()
        .all()
    }

    # 有效敏感度（wire-sensitivity-propagation 任务 4.1）：筛选与徽标按
    # 闭包取严口径（own_levels/names 注入本页已装载数据，祖先缺的自动补查）。
    from .sensitivity import effective_sensitivity_bulk  # noqa: PLC0415

    eff_map = effective_sensitivity_bulk(
        db,
        customer_id,
        [d.id for d in datasets],
        own_levels={
            mid: (m.sensitivity or "常规") for mid, m in metas.items()
        },
        names={d.id: d.name for d in datasets},
    )

    # 新鲜度状态只在筛选时取（钩子缺席/异常 → 全员 unknown，如不误判）
    freshness_map: dict[str, str] = {}
    if freshness is not None and freshness_hook is not None and datasets:
        try:
            freshness_map = dict(
                freshness_hook(customer_id, [d.id for d in datasets]) or {}
            )
        except Exception:
            freshness_map = {}

    items: list[dict] = []
    for dataset in datasets:
        meta = metas.get(dataset.id)
        snapshot = _meta_snapshot(meta)
        if domain is not None:
            wanted = domain.strip()
            unclassified = wanted in ("", DOMAIN_UNCLASSIFIED)
            if not unclassified and snapshot["domain"] != wanted:
                continue
            if unclassified and snapshot["domain"] != "":
                continue
        if tag is not None and tag.strip() not in snapshot["tags"]:
            continue
        if sensitivity is not None and eff_map[dataset.id]["level"] != sensitivity:
            continue
        if source_kind is not None and dataset.source_kind != source_kind:
            continue
        if q is not None:
            needle = q.strip().lower()
            haystack_name = (dataset.name or "").lower()
            haystack_desc = snapshot["description"].lower()
            if needle not in haystack_name and needle not in haystack_desc:
                continue
        if freshness is not None:
            if freshness_map.get(dataset.id, "unknown") != freshness:
                continue
        # 列表条目的 cadence_effective 刻意只取 cadence_hint，不做注册表
        # 回查（与详情视图 _meta_view 的差异）：注册表只有全量枚举一档，
        # 逐条目回查会让列表页变慢——列表显示「未知」即可，详情页才补全。
        hint = (meta.cadence_hint or "").strip() if meta is not None else ""
        eff = eff_map[dataset.id]
        items.append(
            {
                "id": dataset.id,
                "name": dataset.name,
                "source_kind": dataset.source_kind,
                "row_count": dataset.row_count,
                "storage_tier": getattr(dataset, "storage_tier", "cell"),
                "created_at": dataset.created_at,
                "updated_by": meta.updated_by if meta is not None else None,
                "updated_at": meta.updated_at if meta is not None else None,
                "asset": {
                    **snapshot,
                    "cadence_effective": hint or None,
                    "sensitivity_effective": eff["level"],
                    "sensitivity_inherited": eff["inherited"],
                    "sensitivity_incomplete": eff["incomplete"],
                },
            }
        )

    total = len(items)
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
