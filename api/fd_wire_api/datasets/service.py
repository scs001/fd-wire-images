"""datasets 切片业务逻辑：配额、漂移判定、状态机与凭据散列。

契约锚点：
- 配额（§3.1/§3.4）：存储上限与行上限均为套餐杠杆，超限 fail-closed
  （拒收新同步/新数据集），如实带恢复途径提示。
- 漂移（§3.4）：判定在 **api 侧**——新 columns 相对 ``datasets.columns_json``
  增列=接受并追加；缺列/类型变=拒绝 + ``paused_drift``，**不调引擎**。
- 连接器状态（§3.4）：由心跳推导（``now - last_heartbeat_at > 3×poll``
  → offline），不落库。
- 凭据（§6.4）：配对令牌与连接器 key 只存 sha256；明文只在签发当次响应。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ingest import TOOL_CORRECTION_ADD, EngineCallError, EngineClientFactory
from .models import Connector, Dataset
from .settings import get_settings

#: 配对令牌有效期（契约 §3.4：15 分钟、单次）
PAIRING_TOKEN_TTL_MINUTES = 15
PAIRING_TOKEN_PREFIX = "fwp_"
CONNECTOR_KEY_PREFIX = "fwc_"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class QuotaExceeded(RuntimeError):
    """配额超限（路由层转 429；带恢复途径提示）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DriftDetected(RuntimeError):
    """破坏性 schema 漂移（路由层转 409；``differences`` 机器可读）。"""

    def __init__(self, message: str, differences: list[dict]):
        super().__init__(message)
        self.differences = differences


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_pairing_token() -> tuple[str, str, datetime]:
    """返回 (明文, sha256, 过期时间)——明文只进响应，绝不落库。"""
    token = PAIRING_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, sha256_hex(token), utcnow() + timedelta(minutes=PAIRING_TOKEN_TTL_MINUTES)


def new_connector_key() -> tuple[str, str]:
    token = CONNECTOR_KEY_PREFIX + secrets.token_urlsafe(32)
    return token, sha256_hex(token)


def find_by_hash(rows, raw: str, *, attr: str = "token_hash"):
    """恒定时间比对（``secrets.compare_digest``）挑出散列匹配行。"""
    digest = sha256_hex(raw)
    for row in rows:
        if secrets.compare_digest(getattr(row, attr), digest):
            return row
    return None


# ------------------------------------------------------------------ dataset key


def slugify(name: str) -> str:
    """名称 → dataset_key 基座：``^[a-z][a-z0-9_]{0,39}$``（冲突由调用方加）。

    首字符非字母时统一加 ``ds_`` 前缀（数字开头非法）；超长截断到 40。
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    if not slug:
        slug = "dataset"
    if not slug[0].isalpha():
        slug = f"ds_{slug}"
    return slug[:40].rstrip("_") or "dataset"


def allocate_key(db: Session, *, customer_id: str, name: str) -> str:
    """企业内唯一 key：基座冲突时追加 ``_2``/``_3``…（契约 §1）。"""
    base = slugify(name)
    for seq in range(1, 1000):
        candidate = base if seq == 1 else _with_suffix(base, seq)
        if not _KEY_RE.match(candidate):  # pragma: no cover - slugify 已保证
            raise RuntimeError(f"slug 产物不合 dataset_key 约定: {candidate!r}")
        exists = db.execute(
            select(Dataset.id).where(
                Dataset.customer_id == customer_id, Dataset.key == candidate
            )
        ).first()
        if not exists:
            return candidate
    raise RuntimeError(f"无法为 {name!r} 分配 dataset_key（冲突过多）")  # pragma: no cover


def _with_suffix(base: str, seq: int) -> str:
    suffix = f"_{seq}"
    return base[: 40 - len(suffix)].rstrip("_") + suffix


# ------------------------------------------------------------------ 配额


def dataset_storage_total(
    db: Session, *, customer_id: str, exclude_dataset_id: str | None = None
) -> int:
    conditions = [Dataset.customer_id == customer_id]
    if exclude_dataset_id is not None:
        conditions.append(Dataset.id != exclude_dataset_id)
    total = db.execute(
        select(func.coalesce(func.sum(Dataset.size_bytes), 0)).where(*conditions)
    ).scalar_one()
    return int(total or 0)


def storage_limit_for(*, customer_id: str) -> int:
    """套餐分层的存储上限（wire-customer-data-engine 6.4）：
    billing_plans.limits_json.storage_bytes（经 Customer.name 命名空间键解析）
    → 无档位配置时回落全局缺省（datasets settings）。查询失败同样回落缺省
    （fail-closed 方向仍是「有上限」，只是不放大）；同名多行按
    :func:`_resolve_customer_row` 确定性收敛（不抛多行异常）。"""
    limit = None
    try:
        from ..admin.db import get_session as _platform_session

        sess = _platform_session()
        try:
            row = _resolve_customer_row(sess, customer_id)
            if row is not None and row.plan is not None:
                limits = row.plan.limits_json or {}
                limit = limits.get("storage_bytes")
        finally:
            sess.close()
    except Exception:  # noqa: BLE001 - 平台库不可达：回落缺省上限
        limit = None
    return int(limit) if limit else get_settings().dataset_storage_limit_bytes


def ensure_storage_quota(
    db: Session,
    *,
    customer_id: str,
    incoming_bytes: int,
    exclude_dataset_id: str | None = None,
) -> None:
    """存储配额 fail-closed：``其他数据集已用 + 本次预期 > 上限`` 即 429。
    上限套餐分层（storage_limit_for），无分层配置用全局缺省。"""
    limit = storage_limit_for(customer_id=customer_id)
    used = dataset_storage_total(
        db, customer_id=customer_id, exclude_dataset_id=exclude_dataset_id
    )
    if used + max(int(incoming_bytes), 0) > limit:
        raise QuotaExceeded(
            f"存储用量将超过配额（已用 {used} + 本次 {incoming_bytes} > {limit} 字节）——"
            "请删除不再需要的数据集，或联系平台提高配额"
        )


def ensure_new_dataset_room(db: Session, *, customer_id: str) -> None:
    """新数据集 fail-closed 前置：已用存储已达上限即 429（无空余可分配）。"""
    limit = storage_limit_for(customer_id=customer_id)
    used = dataset_storage_total(db, customer_id=customer_id)
    if used >= limit:
        raise QuotaExceeded(
            f"存储用量已达配额（{used} ≥ {limit} 字节）——"
            "请删除不再需要的数据集，或联系平台提高配额"
        )


def ensure_row_quota(row_count: int | None) -> None:
    """行上限 fail-closed（与引擎 ``CUSTOMER_DATASET_MAX_ROWS`` 同默认）。"""
    if row_count is None:
        return
    limit = get_settings().dataset_max_rows
    if int(row_count) > limit:
        raise QuotaExceeded(
            f"行数 {row_count} 超过单数据集上限 {limit}——"
            "请拆分数据或联系平台提高配额"
        )


def estimate_bytes(dataset: Dataset, row_count: int | None) -> int:
    """按上次快照的均值估算本次快照字节数（无历史则 0=未知，交由提交后核对）。"""
    if not row_count:
        return 0
    if dataset.row_count and dataset.size_bytes:
        return int(row_count * (dataset.size_bytes / dataset.row_count))
    return 0


# ------------------------------------------------------------------ 漂移


def diff_columns(
    baseline: list[dict], incoming: list[dict]
) -> tuple[list[dict], list[dict]]:
    """比对 columns 基线；返回 (接受后的 columns, differences)。

    增列（基线 ⊆ 新列，同型）接受并追加（引擎副本新增可空列）；缺列或
    类型变即破坏性漂移——differences 机器可读、逐列列出原因。基线为空
    （首次同步/首次建档）任何形态都接受。
    """
    normalized = [
        {"name": str(column.get("name")), "type": str(column.get("type") or "TEXT")}
        for column in incoming
    ]
    if not baseline:
        return normalized, []
    base_types = {str(c.get("name")): str(c.get("type")) for c in baseline}
    incoming_types = {c["name"]: c["type"] for c in normalized}
    differences: list[dict] = []
    for name, old_type in base_types.items():
        if name not in incoming_types:
            differences.append(
                {"column": name, "kind": "missing", "expected": old_type, "actual": None}
            )
        elif incoming_types[name] != old_type:
            differences.append(
                {
                    "column": name,
                    "kind": "type_changed",
                    "expected": old_type,
                    "actual": incoming_types[name],
                }
            )
    if differences:
        return [], differences
    merged = [
        {"name": str(c.get("name")), "type": str(c.get("type"))} for c in baseline
    ]
    merged += [c for c in normalized if c["name"] not in base_types]
    return merged, []


def drift_message(differences: list[dict]) -> str:
    parts = []
    for diff in differences:
        if diff["kind"] == "missing":
            parts.append(f"列 {diff['column']} 缺失")
        elif diff["kind"] == "type_changed":
            parts.append(
                f"列 {diff['column']} 类型变更（{diff['expected']}→{diff['actual']}）"
            )
        else:  # pragma: no cover - 未来种类兜底
            parts.append(f"列 {diff.get('column')} 漂移（{diff['kind']}）")
    return "drift: " + "；".join(parts)


# ------------------------------------------------------------------ 存储分层（wire-free-tier-data-gate）


#: 免费档档位码（billing_plans.code；free 档按 self-serve-onboarding 裁决无 cell）
FREE_PLAN_CODE = "free"
#: 存储分层取值：api 层轻量行存储 / cell 引擎物化（design D1/D2）
TIER_API = "api"
TIER_CELL = "cell"


def _resolve_customer_row(sess, name: str):
    """billing_customers 按 name 的确定性单行解析（wire-console-resilience D1）。

    同名多行（历史缺陷：admin 手工建档孤儿行 + 自开户真户同 email）时
    ``scalar_one_or_none`` 会抛 MultipleResultsFound——本助手全取后按
    「``logto_sub`` 非空行优先 → ``id`` 最小」收敛到一行，与生产手工止血
    规则一致（真户恒有 sub）；纯读解析，不写库。空 → None。同名全部无
    sub（孤儿群）时也收敛 id 最小一行——上游 ``row.plan`` 判空语义不变，
    fail-closed 方向不受影响。
    """
    from ..billing.models import Customer as BillingCustomer

    rows = (
        sess.execute(select(BillingCustomer).where(BillingCustomer.name == name))
        .scalars()
        .all()
    )
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    return sorted(rows, key=lambda r: (r.logto_sub is None, r.id))[0]


def plan_code_for(*, customer_id: str) -> str | None:
    """客户当前档位码（平台库 ``billing_customers.name`` → ``plan.code``）。

    解析纪律与 :func:`storage_limit_for` 同款：平台库不可达/客户未映射
    返回 None（不放大权限——tier 判定按 None=非免费处理）；同名多行按
    :func:`_resolve_customer_row` 确定性收敛（不抛多行异常）。
    """
    try:
        from ..admin.db import get_session as _platform_session

        sess = _platform_session()
        try:
            row = _resolve_customer_row(sess, customer_id)
            if row is not None and row.plan is not None:
                return str(row.plan.code)
        finally:
            sess.close()
    except Exception:  # noqa: BLE001 - 平台库不可达：档位未知，交由 tier 缺省
        return None
    return None


def storage_tier_for(*, customer_id: str) -> str:
    """建集时刻的存储分层判定（design D1）：免费档 → api 层；其余 → cell 层。

    档位未知（平台库不可达/未映射）按 cell 处理——与现状行为一致（付费
    缺省零变化）；免费档判定必须显式命中 free 档。
    """
    return (
        TIER_API
        if plan_code_for(customer_id=customer_id) == FREE_PLAN_CODE
        else TIER_CELL
    )


def is_free_tier(*, customer_id: str) -> bool:
    """客户当前是否免费档（无 cell；quota 端点与档位闸共用）。"""
    return plan_code_for(customer_id=customer_id) == FREE_PLAN_CODE


def free_dataset_count(db: Session, *, customer_id: str) -> int:
    """免费档口径的数据集个数（= api 层数据集数；升档后旧 api 集仍计入）。"""
    return int(
        db.execute(
            select(func.count(Dataset.id)).where(
                Dataset.customer_id == customer_id,
                Dataset.storage_tier == TIER_API,
            )
        ).scalar_one()
    )


def ensure_free_dataset_quota(db: Session, *, customer_id: str) -> None:
    """建集闸（design D3）：免费档数据集个数达上限即 429（错误含余量）。"""
    limit = get_settings().free_tier_max_datasets
    used = free_dataset_count(db, customer_id=customer_id)
    if used >= limit:
        raise QuotaExceeded(
            f"免费档数据集个数已达上限（已用 {used}/{limit}）——"
            "请删除不再需要的数据集，或升级到基础档及以上套餐"
        )


def ensure_free_row_quota(row_count: int | None) -> None:
    """写入闸（design D3）：行数超免费档单集上限即 429（含上限与实际行数）。

    在任何行写入之前判定（上传件行数建档时已知），拒收不留半写状态。
    """
    if row_count is None:
        return
    count = int(row_count)
    limit = get_settings().free_tier_max_rows
    if count > limit:
        raise QuotaExceeded(
            f"行数 {count} 超过免费档单数据集上限 {limit}——"
            "请拆分数据，或升级到基础档及以上套餐"
        )


# ------------------------------------------------------------------ 连接器状态


def connector_online_at(connector: Connector, *, now: datetime | None = None) -> bool:
    if connector.revoked_at is not None or connector.last_heartbeat_at is None:
        return False
    now = now or utcnow()
    return (now - connector.last_heartbeat_at) <= timedelta(
        seconds=get_settings().heartbeat_offline_seconds
    )


def connector_status(connector: Connector, *, now: datetime | None = None) -> str:
    """推导状态（不落库）：revoked > offline > online。"""
    if connector.revoked_at is not None:
        return "revoked"
    return "online" if connector_online_at(connector, now=now) else "offline"


def new_uuid() -> str:
    return str(uuid.uuid4())


# ----------------------------------------------------- 修正提交（共享 insert 落层）


def submit_insert_correction(
    db: Session,
    *,
    dataset: Dataset,
    values: dict,
    actor: str,
    engine_factory: EngineClientFactory | None = None,
) -> int:
    """表单提交的 insert 修正共享落层（wire-form-builder 任务 2.1，design D2）。

    从 corrections 路由的 insert 分支下沉（行为零漂移）：console 表单运行时
    与公开提交与 corrections 端点走**同一实现**（同闸同审计，杜绝两口径）。

    - 载荷校验：values 空对象 → :class:`~.api_store.LocalQueryError`
      （路由层转 400，文案与下沉前逐字一致）；
    - api 档（storage_tier=api）：``api_store.apply_local_correction`` 本地
      落层——免费档行数闸 :class:`QuotaExceeded`（路由层转 429）、未知列
      LocalQueryError（路由层转 400）原样上抛，全链路零引擎调用；
    - cell 档：经 ``engine_factory`` 透传引擎 ``TOOL_CORRECTION_ADD``（报文
      与 corrections 端点同形：dataset_key/op/anchor/values/actor）；引擎
      不可达/报错抛 EngineUnavailable/EngineCallError、未返回 correction_id
      抛 EngineCallError（路由层一律转 502）。

    异常转译（400/429/502）留在路由层——本函数只抛域异常。返回 correction_id。
    """
    # 局部导入防环：api_store 模块头导入本模块（service ← api_store 互引）
    from . import api_store

    if not values:
        raise api_store.LocalQueryError("insert 必须带 values（整行值）")
    if getattr(dataset, "storage_tier", TIER_CELL) == TIER_API:
        return api_store.apply_local_correction(
            db,
            dataset=dataset,
            op="insert",
            row_anchor=None,
            column_name=None,
            value=values,
            actor=actor,
        )
    if engine_factory is None:
        # 路由/调用方接线遗漏（cell 档必须注入引擎工厂）——fail-loud
        raise RuntimeError(
            "cell 档数据集的 insert 修正需要 engine_factory（调用方注入引擎工厂）"
        )
    client = engine_factory(dataset.customer_id)
    result = client.call(
        TOOL_CORRECTION_ADD,
        {
            "dataset_key": dataset.key,
            "op": "insert",
            "anchor": None,
            "values": values,
            "actor": actor,
        },
    )
    correction_id = result.get("correction_id")
    if correction_id is None:
        raise EngineCallError("引擎未返回 correction_id")
    return int(correction_id)
