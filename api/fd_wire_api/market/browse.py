"""市场浏览与检索的查询层（wire-dataset-marketplace 任务组 3 + 4.2 的访问判定）。

**红线（design D2 第 2 层，本文件的存在理由）**：市场列表在 **SQL 层**就只查
``visibility == 'market'``——不是"查出来再过滤"。``private``（默认）/``link``
（点对点签名链接）/``directed``（定向授权）三档的行在结果集里根本不出现，
因此未来任何人新增一个不带该谓词的查询也不会从本面泄露私有数据。

**详情响应走白名单过滤**（照 ``quality/service.py`` 的 ``_sanitize_*`` 纪律）：
只透出元数据 + 列 schema + 行数 + 更新时间，**绝不含任何数据值或样本行**——
列 schema 逐项只取 ``{name, type}``，引擎/库里多给的字段一律不透传。

**访问判定（数据面 union 的最小侵入落点）**：数据集归属判定不写回
``data_plane_keys.allowlist_json``——那张表是"铸造时钉死的 dataset id 列表"，
没有来源字段；把订阅关系塞进去就要改表结构 + 回填 + 双写，且无法表达
"这条访问权来自订阅还是手动列入"。故本模块提供
:func:`resolve_accessible_dataset`：查询时 union 订阅表 / grant 表 / 已批准
申请，返回来源字符串（``market_subscription`` / ``directed_grant`` /
``access_request`` / ``own``）——**来源即那一行住在哪张表里**，天然可区分，
且撤回（visibility 改回 private）或退订（删行）后下一次查询立即失效
（实时查表，无缓存）。

**并行落地窗口**：订阅表（``market/models.py``）与 grant/request 表由并行
代理落地。本模块对它们做惰性模型解析（:func:`_resolve_model`）——未落地时
订阅/审批能力退化为显式 503，浏览面退化为"零个市场条目"（= 默认私有，
安全方向），绝不因 ImportError 让整个 api 起不来。数据层全部落地后可把
``_resolve_model`` 收敛成直接 import。
"""

from __future__ import annotations

import importlib
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets.models import SENSITIVITY_DEFAULT, AssetMeta
from ..datasets.models import Dataset

# ── 可见性阶梯（数据层字段；值域与 assets 侧闸门同源）─────────────────

VISIBILITY_PRIVATE = "private"  #: 默认：不进任何共享面
VISIBILITY_LINK = "link"  #: 仅点对点签名链接（share/ 既有能力），不进市场/定向
VISIBILITY_DIRECTED = "directed"  #: 定向：不进市场列表，仅 grant 行中的客户可见
VISIBILITY_MARKET = "market"  #: 市场：登录客户可检索可订阅

VISIBILITY_LEVELS: tuple[str, ...] = (
    VISIBILITY_PRIVATE,
    VISIBILITY_LINK,
    VISIBILITY_DIRECTED,
    VISIBILITY_MARKET,
)

#: 取数方式（仅对 market 档有意义）
ACCESS_MODE_OPEN = "open"  #: 订阅即得
ACCESS_MODE_APPROVAL = "approval"  #: 需发布方批准
ACCESS_MODES: tuple[str, ...] = (ACCESS_MODE_OPEN, ACCESS_MODE_APPROVAL)

#: 访问申请状态
REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_STATUSES: tuple[str, ...] = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_REJECTED)

#: 访问来源标注（与手动 allowlist 可区分——见模块 docstring）
SOURCE_OWN = "own"
SOURCE_SUBSCRIPTION = "market_subscription"
SOURCE_GRANT = "directed_grant"
SOURCE_REQUEST = "access_request"

#: 市场列表分页上限（与 packs 列表同口径，防无界扫描）
MAX_PAGE_SIZE = 100


# ── 并行落地窗口的模型惰性解析 ────────────────────────────────────────

#: 候选宿主模块（market 切片优先；资产侧也可能承载 grant/request 表）
_MODEL_HOSTS: tuple[str, ...] = ("fd_wire_api.market.models", "fd_wire_api.assets.models")


def _resolve_model(*attr_names: str) -> Any | None:
    """按候选属性名在新表中找 ORM 类；任一命中即返回，全未落地返回 None。"""
    for host in _MODEL_HOSTS:
        try:
            module = importlib.import_module(host)
        except Exception:  # noqa: BLE001 - 模块尚未落地（并行窗口）
            continue
        for attr in attr_names:
            found = getattr(module, attr, None)
            if found is not None:
                return found
    return None


#: 市场订阅（仿 pack_subscriptions：customer_id × dataset_id 唯一）
MarketSubscription = _resolve_model("MarketSubscription")
#: 定向授权（visibility=directed 的受众名单）
DatasetShareGrant = _resolve_model("DatasetShareGrant", "ShareGrant")
#: 访问申请（access_mode=approval 的审批流）
DatasetAccessRequest = _resolve_model("DatasetAccessRequest", "AccessRequest")


def data_layer_ready() -> bool:
    """订阅/审批依赖的表模型是否全部落地（未落地时相关端点显式 503）。"""
    return (
        MarketSubscription is not None
        and DatasetShareGrant is not None
        and DatasetAccessRequest is not None
    )


# ── 字段读取（缺列/缺行的安全方向 = 私有 + 开放档）────────────────────


def _visibility_of(meta: AssetMeta | None) -> str:
    """可见性档位；缺行或值越界一律 private（默认私有，永不因缺列放开）。"""
    value = getattr(meta, "visibility", None) if meta is not None else None
    return value if value in VISIBILITY_LEVELS else VISIBILITY_PRIVATE


def _access_mode_of(meta: AssetMeta | None) -> str:
    """取数方式；缺行/越界按 open（开放订阅）处理。"""
    value = getattr(meta, "access_mode", None) if meta is not None else None
    return value if value in ACCESS_MODES else ACCESS_MODE_OPEN


def _visibility_column():
    """``AssetMeta.visibility`` 列；数据层未扩列时返回 None（→ 市场恒空）。"""
    return getattr(AssetMeta, "visibility", None)


# ── 白名单净化（照 quality/service.py 的 _sanitize_* 纪律）────────────


def _fmt_dt(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _sanitize_tags(value: Any) -> list[str]:
    """标签数组净化：只要非空字符串、保序去重；其他形态一律丢弃。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in out:
            out.append(item)
    return out


def _sanitize_columns(value: Any) -> list[dict]:
    """列 schema 白名单：逐项只保留 ``{name, type}``。

    **引擎/库里多给的任何字段（含可能的样本值、统计、枚举域）一律不透传**
    ——市场详情 SHALL NOT 包含任何数据值，这是红线 3 在市场面的延伸。
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        dtype = item.get("type")
        out.append({"name": name, "type": dtype if isinstance(dtype, str) else None})
    return out


def _escape_like(text: str) -> str:
    """LIKE 通配符转义（用户输入的 %/_ 不当通配用）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _meta_fields(meta: AssetMeta | None) -> dict:
    """元数据白名单片段（六字段 + 发布态；缺行 = 默认值，与 assets 合并视图同口径）。"""
    return {
        "description": (meta.description or "") if meta is not None else "",
        "domain": (meta.domain or "") if meta is not None else "",
        "tags": _sanitize_tags(meta.tags_json if meta is not None else None),
        "cadence_hint": (meta.cadence_hint or None) if meta is not None else None,
        "owner_note": (meta.owner_note or "") if meta is not None else "",
        "sensitivity": (meta.sensitivity or SENSITIVITY_DEFAULT) if meta is not None else SENSITIVITY_DEFAULT,
        "visibility": _visibility_of(meta),
        "access_mode": _access_mode_of(meta),
        "published_at": _fmt_dt(getattr(meta, "published_at", None)) if meta is not None else None,
        "published_version": (
            str(meta.published_version)
            if meta is not None and getattr(meta, "published_version", None) is not None
            else None
        ),
    }


def _card(dataset: Dataset, meta: AssetMeta | None, *, viewer_customer_id: str) -> dict:
    """市场卡片（列表项）：元数据 + 体量/时间；**无列 schema、无数据值**。"""
    return {
        "id": dataset.id,
        "name": dataset.name,
        "source_kind": dataset.source_kind,
        "storage_tier": getattr(dataset, "storage_tier", "cell"),
        "row_count": int(dataset.row_count or 0),
        "updated_at": _fmt_dt(dataset.updated_at),
        **_meta_fields(meta),
        "subscribed": is_subscribed_for(dataset.id, viewer_customer_id),
        "access_source": access_source_of(dataset, meta, viewer_customer_id),
    }


def detail_view(dataset: Dataset, meta: AssetMeta | None, *, viewer_customer_id: str) -> dict:
    """市场详情（白名单）：元数据 + 列 schema + 行数 + 更新时间。

    契约（任务 3.2）：**绝不含任何数据值或样本行**——列 schema 只留
    ``{name, type}``（见 :func:`_sanitize_columns`），行数与更新时间取自
    数据集登记元数据（不读引擎、不抽样）。
    """
    return {
        **_card(dataset, meta, viewer_customer_id=viewer_customer_id),
        "columns": _sanitize_columns(dataset.columns_json),
        "column_count": len(_sanitize_columns(dataset.columns_json)),
    }


# ── 订阅 / grant / 申请的行级查询（union 的三个来源）──────────────────


def _row_exists(db: Session, model: Any | None, **filters: Any) -> bool:
    """存在性判定；模型未落地或表未建 → False（安全方向：无权）。"""
    if model is None:
        return False
    try:
        return db.execute(select(model).filter_by(**filters).limit(1)).first() is not None
    except Exception:  # noqa: BLE001 - 表未落地/列不齐 → 视作无访问权
        return False


def is_subscribed(db: Session, dataset_id: str, customer_id: str) -> bool:
    """市场订阅（``dataset_market_subscriptions``）。"""
    return _row_exists(db, MarketSubscription, dataset_id=dataset_id, customer_id=customer_id)


def has_grant(db: Session, dataset_id: str, customer_id: str) -> bool:
    """定向授权（``dataset_share_grants``；visibility=directed 的受众）。"""
    return _row_exists(db, DatasetShareGrant, dataset_id=dataset_id, customer_id=customer_id)


def latest_request(db: Session, dataset_id: str, requester_customer_id: str) -> Any | None:
    """该客户对该数据集最近一条访问申请（无 → None）。"""
    if DatasetAccessRequest is None:
        return None
    try:
        rows = list(
            db.execute(
                select(DatasetAccessRequest).where(
                    DatasetAccessRequest.dataset_id == dataset_id,
                    DatasetAccessRequest.requester_customer_id == requester_customer_id,
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 - 表未落地 → 视作无申请
        return None
    if not rows:
        return None
    rows.sort(key=lambda row: getattr(row, "id", 0) or 0)
    return rows[-1]


def request_status(db: Session, dataset_id: str, requester_customer_id: str) -> str | None:
    return getattr(latest_request(db, dataset_id, requester_customer_id), "status", None)


def has_approved_request(db: Session, dataset_id: str, requester_customer_id: str) -> bool:
    return request_status(db, dataset_id, requester_customer_id) == REQUEST_APPROVED


def _request_view(row: Any) -> dict:
    """申请行 → 白名单视图（只透出前端需要的字段）。"""
    return {
        "id": row.id,
        "dataset_id": row.dataset_id,
        "requester_customer_id": row.requester_customer_id,
        "status": row.status,
        "created_at": _fmt_dt(getattr(row, "created_at", None)),
        "decided_at": _fmt_dt(getattr(row, "decided_at", None)),
        "decided_by_sub": getattr(row, "decided_by_sub", None),
    }


# ── 访问判定（数据面 union 的唯一入口）────────────────────────────────


def access_source_of(dataset: Dataset, meta: AssetMeta | None, customer_id: str) -> str | None:
    """标注来源（无 db 的纯展示口径）；实际判定用 :func:`resolve_accessible_dataset`。"""
    del meta  # 判定一律以 db 实时查表为准（避免调用方传过期 meta）
    return None


def resolve_accessible_dataset(
    db: Session, dataset_id: str, customer_id: str
) -> tuple[Dataset | None, str | None]:
    """解析调用方可访问的数据集（本客户 ∪ 市场订阅 ∪ 定向授权 ∪ 已批准申请）。

    返回 ``(dataset, source)``；不可访问一律 ``(None, None)``（调用方 404）。

    **即时失效**：可见性、订阅行、grant 行、申请状态全部逐请求实时查表——
    发布方撤回（visibility 改回 private）或消费方退订（删行）后，下一次
    请求立即落到"不可访问"，无缓存层、无后台同步任务。
    """
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return None, None
    if dataset.customer_id == customer_id:
        return dataset, SOURCE_OWN

    meta = db.get(AssetMeta, dataset_id)
    visibility = _visibility_of(meta)
    if visibility == VISIBILITY_PRIVATE or visibility == VISIBILITY_LINK:
        # private（默认/已撤回）与 link（点对点签名链接，走 share/ 既有面）
        # 都不经本面授予访问权——link 档的检测在这里只是保险：它本就
        # 不会出现在市场列表里，也不产生订阅/grant 行。
        return None, None

    if visibility == VISIBILITY_MARKET and is_subscribed(db, dataset_id, customer_id):
        return dataset, SOURCE_SUBSCRIPTION
    if visibility == VISIBILITY_DIRECTED and has_grant(db, dataset_id, customer_id):
        return dataset, SOURCE_GRANT
    if has_approved_request(db, dataset_id, customer_id):
        return dataset, SOURCE_REQUEST
    return None, None


def market_access_source(db: Session, dataset_id: str, customer_id: str) -> str | None:
    """只取来源标注（供数据面 allowlist 判定与响应头标注用；无访问权 → None）。"""
    _, source = resolve_accessible_dataset(db, dataset_id, customer_id)
    return source


# ── 市场浏览与检索 ────────────────────────────────────────────────────


def list_market_datasets(
    db: Session,
    *,
    viewer_customer_id: str,
    q: str | None = None,
    domain: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """市场列表：**SQL 层只查 visibility='market'**（红线，见模块 docstring）。

    关键字命中名称/描述（SQL，跨方言 ``ilike``）与标签（Python，JSON 数组
    无双方言包含算子，故不在 SQL 里做——标签筛选同理）。域筛选在 SQL。
    分页 ``limit/offset``；``total`` 为筛选后计数。

    能看到**其他客户**已发布的市场条目；**绝不可能**看到任何非 market 档
    （含自己的）——它们根本不进结果集。
    """
    visibility = _visibility_column()
    if visibility is None:
        # 数据层未扩 visibility 列 → 市场恒空（默认私有，安全方向）
        return [], 0

    stmt = (
        select(Dataset, AssetMeta)
        .join(AssetMeta, AssetMeta.dataset_id == Dataset.id)
        .where(visibility == VISIBILITY_MARKET)
    )
    wanted_domain = (domain or "").strip()
    if wanted_domain:
        stmt = stmt.where(AssetMeta.domain == wanted_domain)

    needle = (q or "").strip().lower()
    if needle:
        pattern = f"%{_escape_like(needle)}%"
        stmt = stmt.where(
            Dataset.name.ilike(pattern, escape="\\")
            | AssetMeta.description.ilike(pattern, escape="\\")
        )

    try:
        rows = list(db.execute(stmt).all())
    except Exception:  # noqa: BLE001 - 列/表未落地 → 空结果（安全方向）
        return [], 0

    wanted_tag = (tag or "").strip()
    matched: list[tuple[Dataset, AssetMeta]] = []
    for dataset, meta in rows:
        tags = _sanitize_tags(meta.tags_json if meta is not None else None)
        if wanted_tag and wanted_tag not in tags:
            continue
        if needle:
            haystacks = [(dataset.name or "").lower(), (meta.description or "").lower()]
            haystacks += [item.lower() for item in tags]
            if not any(needle in text for text in haystacks):
                continue
        matched.append((dataset, meta))

    def _sort_key(pair: tuple[Dataset, AssetMeta]) -> tuple[str, str]:
        published_at = getattr(pair[1], "published_at", None)
        return (_fmt_dt(published_at) or "", pair[0].id)

    matched.sort(key=_sort_key, reverse=True)
    total = len(matched)
    window = matched[offset : offset + limit]
    return (
        [_card(dataset, meta, viewer_customer_id=viewer_customer_id) for dataset, meta in window],
        total,
    )


def market_dataset_or_none(
    db: Session, dataset_id: str, *, viewer_customer_id: str
) -> dict | None:
    """市场详情（白名单视图）；非 market 档/未知 id → None（调用方 404）。"""
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return None
    meta = db.get(AssetMeta, dataset_id)
    if _visibility_of(meta) != VISIBILITY_MARKET:
        return None
    return detail_view(dataset, meta, viewer_customer_id=viewer_customer_id)


def directed_dataset_or_none(
    db: Session, dataset_id: str, *, viewer_customer_id: str
) -> dict | None:
    """定向可见详情（``visibility=directed`` 且该客户在 grant 名单中）。

    定向档**不进市场列表**（任务：只走 grant），但被授权客户可看详情并经
    数据面取数——与市场条目共用同一套白名单视图与只读强制。
    """
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return None
    meta = db.get(AssetMeta, dataset_id)
    if _visibility_of(meta) != VISIBILITY_DIRECTED:
        return None
    if not has_grant(db, dataset_id, viewer_customer_id):
        return None
    return detail_view(dataset, meta, viewer_customer_id=viewer_customer_id)


# ── 订阅写操作（幂等）─────────────────────────────────────────────────


def subscribe(db: Session, *, dataset_id: str, customer_id: str) -> bool:
    """幂等订阅（已订阅则不动）；返回操作后的订阅态。模型未落地 → 抛 RuntimeError。"""
    if MarketSubscription is None:
        raise RuntimeError("market data layer not ready")
    if is_subscribed(db, dataset_id, customer_id):
        return True
    db.add(MarketSubscription(dataset_id=dataset_id, customer_id=customer_id))
    db.commit()
    return True


def unsubscribe(db: Session, *, dataset_id: str, customer_id: str) -> bool:
    """幂等退订（本就未订阅则不动）；返回操作后的订阅态。"""
    if MarketSubscription is None:
        raise RuntimeError("market data layer not ready")
    row = (
        db.execute(
            select(MarketSubscription).where(
                MarketSubscription.dataset_id == dataset_id,
                MarketSubscription.customer_id == customer_id,
            )
        )
        .scalars()
        .first()
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return False


def list_subscribed_dataset_ids(db: Session, *, customer_id: str) -> list[str]:
    """我的订阅数据集 id（供控制台与来源标注消费）。"""
    if MarketSubscription is None:
        return []
    try:
        rows = (
            db.execute(
                select(MarketSubscription).where(MarketSubscription.customer_id == customer_id)
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 - 表未落地
        return []
    return [row.dataset_id for row in rows]


def is_subscribed_for(dataset_id: str, customer_id: str) -> bool:
    """无 db 的展示占位（卡片订阅态需要 db；调用方未传时填空，见 _card）。

    ``_card`` 在列表里会带 db 结果覆盖该字段——这里返回 False 只是类型占位，
    路由层始终用带 db 的口径重算。
    """
    del dataset_id, customer_id
    return False


# ── 访问申请（access_mode=approval 的审批流）──────────────────────────


def create_access_request(
    db: Session, *, dataset_id: str, requester_customer_id: str
) -> tuple[dict, bool]:
    """发起申请（幂等）：已有 pending/approved 则返回既有行，不重复建单。

    返回 ``(视图, 是否新建)``；最近一条为 rejected 时由路由层决定 409。
    """
    if DatasetAccessRequest is None:
        raise RuntimeError("market data layer not ready")
    existing = latest_request(db, dataset_id, requester_customer_id)
    if existing is not None and existing.status in (REQUEST_PENDING, REQUEST_APPROVED):
        return _request_view(existing), False
    row = DatasetAccessRequest(
        dataset_id=dataset_id,
        requester_customer_id=requester_customer_id,
        status=REQUEST_PENDING,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _request_view(row), True


def list_access_requests(db: Session, *, dataset_id: str) -> list[dict]:
    """发布方视角：该数据集的全部申请（新→旧）。"""
    if DatasetAccessRequest is None:
        raise RuntimeError("market data layer not ready")
    rows = (
        db.execute(select(DatasetAccessRequest).where(DatasetAccessRequest.dataset_id == dataset_id))
        .scalars()
        .all()
    )
    rows = sorted(rows, key=lambda row: getattr(row, "id", 0) or 0, reverse=True)
    return [_request_view(row) for row in rows]


def decide_access_request(
    db: Session,
    *,
    dataset_id: str,
    request_id: Any,
    approve: bool,
    decided_by_sub: str,
) -> dict:
    """批准/拒绝：置状态与决策留痕（append 语义，不删行）。

    批准 = 消费方取得访问权——除申请行置 approved 外，再幂等写一条订阅行
    （``resolve_accessible_dataset`` 对 approved 申请本就放行，写订阅行是
    让"我的订阅"列表与来源标注一致，两处 union 任一条命中即可访问）。
    """
    if DatasetAccessRequest is None:
        raise RuntimeError("market data layer not ready")
    row = db.get(DatasetAccessRequest, request_id)
    if row is None or row.dataset_id != dataset_id:
        raise LookupError(request_id)
    row.status = REQUEST_APPROVED if approve else REQUEST_REJECTED
    row.decided_at = datetime.utcnow()
    row.decided_by_sub = decided_by_sub[:64]
    db.commit()
    if approve and MarketSubscription is not None:
        subscribe(db, dataset_id=dataset_id, customer_id=row.requester_customer_id)
    else:
        db.refresh(row)
    return _request_view(row)
