"""数据面读写端点（wire-customer-data-engine 5.3，路径契约=design 附录 A）。

薄壳转发引擎（ADR-0007：单点执行层，REST 与 MCP 工具零漂移）：
- 双凭据经 require_data_plane_principal（wdk- key 或 console 会话）；
- allowlist 越界 403、额度满 fail-closed 429、引擎不可达如实 502/503
  （绝不静默降级）；
- 写入按凭据通道归属（data-key/console）落引擎编辑记录。

api 层分支（wire-free-tier-data-gate）：``storage_tier=api`` 的数据集
（免费档）query/aggregate/lineage 走本地执行（不触碰引擎、不计
metering_records——D7 口径），derive/join/batchWrite/加列等 cell 耦合
路径返回 403 档位闸（PLAN_TIER_REQUIRED）。

市场访问（wire-dataset-marketplace）：**读端点**接受"他人开放给我的"
数据集——本客户 ∪ 市场订阅 ∪ 定向 grant ∪ 已批准申请（判定与来源标注
复用 ``market.browse.resolve_accessible_dataset``，实时查表无缓存，撤回/
退订/拒绝下一次请求即失效）。**写端点仍严格本客户**（`_dataset_or_404`）：
他人的数据永不写入（红线 1「永不写回客户源」在共享维度的延伸——别人的
数据更不能写）。
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..datasets import acl, api_store, service
from ..datasets.db import get_db
from ..datasets.ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
    TOOL_ADD_COLUMN,
    TOOL_AGGREGATE,
    TOOL_AGGREGATE_DERIVE,
    TOOL_DERIVE,
    TOOL_DERIVE_REFRESH,
    TOOL_FILTER,
    TOOL_JOIN_INDICATORS,
    TOOL_LINEAGE,
    TOOL_OVERVIEW,
    TOOL_PROJECT,
    TOOL_QUERY,
    TOOL_SQL,
    TOOL_SQL_PREVIEW,
    TOOL_UNION,
    TOOL_WINDOW,
    TOOL_WRITE_BATCH,
    get_engine_client_factory,
)
from ..datasets.models import Dataset
from ..datasets.routes import (
    _actor,
    _dataset_or_404,
    _ensure_engine_ok,
    _raise_quota,
    require_cell_tier,
)
from ..datasets.schemas import ColumnIn, DatasetOut
from .auth import DataPlanePrincipal, ensure_dataset_allowed, require_data_plane_principal

router = APIRouter(tags=["data-plane"])


# ── 请求体（契约 = 引擎工具入参同形，附录 A）─────────────────────────


class QueryIn(BaseModel):
    filters: list[dict] = Field(default_factory=list, max_length=50)
    sort: list[dict] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    count_only: bool = False
    distinct_column: Optional[str] = None
    raw: bool = False


class AggregateIn(BaseModel):
    group_by: list[str] = Field(min_length=1, max_length=10)
    measures: list[dict] = Field(min_length=1, max_length=10)
    filters: list[dict] = Field(default_factory=list, max_length=50)
    raw: bool = False


class BatchOpIn(BaseModel):
    op: str
    anchor: Optional[str] = None
    values: Optional[dict] = None


class BatchWriteIn(BaseModel):
    ops: list[BatchOpIn] = Field(min_length=1, max_length=1000)
    actor_label: Optional[str] = Field(default=None, max_length=200)


class AddColumnIn(BaseModel):
    column: ColumnIn


class DeriveIn(BaseModel):
    filters: list[dict] = Field(default_factory=list, max_length=50)
    sort: list[dict] = Field(default_factory=list, max_length=10)
    limit: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    name: str = Field(min_length=1, max_length=200)


class JoinIndicatorIn(BaseModel):
    code: Optional[str] = None
    concept_id: Optional[int] = None
    as_: str = Field(alias="as")

    model_config = {"populate_by_name": True}


class JoinIn(BaseModel):
    indicators: list[JoinIndicatorIn] = Field(min_length=1, max_length=10)
    date_column: str
    region_column: Optional[str] = None
    default_entity: str = "中国"
    filters: list[dict] = Field(default_factory=list, max_length=50)
    name: str = Field(min_length=1, max_length=200)


# ── 确定性算子 + SQL 步骤（wire-transform-operators）───────────────
# 请求体与引擎工具入参同形（spec「参数形状一致」）；深校验在引擎侧
# （同一套校验口径，api 不复写第二套语义）。


class FilterIn(BaseModel):
    """行过滤算子：与 derive 同一套 filters/sort/limit 语义，血缘 kind=filter。"""

    filters: list[dict] = Field(default_factory=list, max_length=50)
    sort: list[dict] = Field(default_factory=list, max_length=10)
    limit: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    name: str = Field(min_length=1, max_length=200)


class ProjectColumnIn(BaseModel):
    """一条投影：源列 → 输出列名（as 缺省=源列名）。"""

    source: str = Field(min_length=1, max_length=200)
    as_: Optional[str] = Field(default=None, alias="as", max_length=200)

    model_config = {"populate_by_name": True}


class ProjectIn(BaseModel):
    columns: list[ProjectColumnIn] = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class AggregateDeriveIn(BaseModel):
    group_by: list[str] = Field(min_length=1, max_length=10)
    measures: list[dict] = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)


class UnionIn(BaseModel):
    right_dataset_id: str = Field(min_length=1, max_length=36)
    on: str = "by_name"
    name: str = Field(min_length=1, max_length=200)


class WindowIn(BaseModel):
    """窗口算子：分区/排序/函数行三块，形状即引擎工具入参。

    深校验（函数闭集、排序类必填 order_by、offset 1..10000、``as`` 唯一且
    不与源列冲突、frame 闭集）全在引擎侧——api 不复写第二套语义（同四算子
    口径）。这里只保留与工具入参同形的结构性上限。
    """

    partition_by: list[str] = Field(default_factory=list, max_length=10)
    order_by: list[dict] = Field(default_factory=list, max_length=10)
    window: list[dict] = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)


class SqlTableIn(BaseModel):
    """SQL 占位声明：SQL 里写 ``{name}``，映射到本客户的某数据集。"""

    name: str = Field(min_length=1, max_length=64)
    dataset_key: str = Field(min_length=1, max_length=40)


class SqlIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    tables: list[SqlTableIn] = Field(min_length=1, max_length=10)
    params: dict = Field(default_factory=dict)
    name: str = Field(min_length=1, max_length=200)


class SqlPreviewIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    tables: list[SqlTableIn] = Field(min_length=1, max_length=10)
    params: dict = Field(default_factory=dict)
    limit: Optional[int] = Field(default=None, ge=1, le=100)


# ── 薄壳工具 ─────────────────────────────────────────────────────────


def _engine_call(
    engine_factory: EngineClientFactory, principal: DataPlanePrincipal, tool: str, args: dict
) -> dict:
    client = engine_factory(principal.customer_id)
    try:
        result = client.call(tool, args)
    except (EngineUnavailable, EngineCallError) as exc:
        raise _ensure_engine_ok(exc) from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=f"引擎拒绝: {result['error']}")
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail=f"引擎返回形态异常: {result!r}")
    return result


def _sync_row_count(
    db: Session,
    dataset: Dataset,
    engine_factory: EngineClientFactory,
    principal: DataPlanePrincipal,
) -> None:
    """写入后以引擎 overview 为准刷新本地元数据（row_count；绝不用估算冒充）。"""
    result = _engine_call(engine_factory, principal, TOOL_OVERVIEW, {"dataset_key": dataset.key})
    dataset.row_count = int(result.get("rows") or 0)
    dataset.size_bytes = int(result.get("size_bytes") or 0)
    db.commit()


def _estimate_batch_bytes(ops: list[BatchOpIn]) -> int:
    """批量写的存储预检估算（fail-closed 方向宁严勿松：值 JSON 长 + 每条开销）。"""
    total = 0
    for op in ops:
        total += len(json.dumps(op.values or {}, ensure_ascii=False, default=str)) + 128
    return total


# ── 查询/聚合 ────────────────────────────────────────────────────────


def _is_api_tier(dataset: Dataset) -> bool:
    return getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API


def _require_acl(
    db: Session,
    principal: DataPlanePrincipal,
    dataset: Dataset,
    need: str = acl.NEED_READ,
) -> None:
    """数据集级授权拦截（唯一实现 acl.can_access_principal；wire-dataset-acl D3）。

    只在 console 轨道 + 本队数据集上生效；``data-key`` 轨道与系统合成身份
    不承载成员身份（D6 边界），市场授予的他企数据集属独立可见性域。
    调用顺序由调用方保证：租户 404 → allowlist 403 → 档位闸 403 → 本闸。
    """
    acl.can_access_principal(db, principal=principal, dataset=dataset, need=need)


def _readable_dataset_or_404(db: Session, dataset_id: str, customer_id: str) -> Dataset:
    """读端点数据集解析：本客户 ∪ 市场授予（订阅/定向/已批准申请）。

    与 `_dataset_or_404` 的唯一差异：归属判定扩展为"该客户有权读"。
    市场切片缺席（未部署/表未建）时退化为纯本客户判定——安全方向
    （私有数据永不多给）。来源判定在 market.browse 内实时查表，撤回/
    退订/拒绝后立即失效。
    """
    try:
        from ..market import browse as market_browse

        dataset, _source = market_browse.resolve_accessible_dataset(db, dataset_id, customer_id)
        if dataset is not None:
            return dataset
    except Exception:  # noqa: BLE001 - 市场切片未就绪：退化纯本客户判定
        pass
    return _dataset_or_404(db, dataset_id, customer_id)


@router.post("/api/v1/datasets/{dataset_id}/query")
def dataset_query(
    dataset_id: str,
    body: QueryIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    dataset = _readable_dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    _require_acl(db, principal, dataset)
    if _is_api_tier(dataset):
        # 免费档本地执行：零引擎调用、零 metering_records（D5/D7）；
        # 限流沿用本依赖链既有 rpm（wdk- 轨道在鉴权步已生效）。
        try:
            return api_store.execute_query(
                db,
                dataset_id=dataset.id,
                filters=body.filters,
                sort=body.sort,
                limit=body.limit,
                offset=body.offset,
                count_only=body.count_only,
                distinct_column=body.distinct_column,
            )
        except api_store.LocalQueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _engine_call(
        engine_factory,
        principal,
        TOOL_QUERY,
        {"dataset_key": dataset.key, **body.model_dump()},
    )


@router.post("/api/v1/datasets/{dataset_id}/aggregate")
def dataset_aggregate(
    dataset_id: str,
    body: AggregateIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    dataset = _readable_dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    _require_acl(db, principal, dataset)
    if _is_api_tier(dataset):
        try:
            return api_store.execute_aggregate(
                db,
                dataset_id=dataset.id,
                group_by=body.group_by,
                measures=body.measures,
                filters=body.filters,
            )
        except api_store.LocalQueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _engine_call(
        engine_factory,
        principal,
        TOOL_AGGREGATE,
        {"dataset_key": dataset.key, **body.model_dump()},
    )


# ── 批量写 ───────────────────────────────────────────────────────────


@router.post("/api/v1/datasets/{dataset_id}/rows:batchWrite")
def dataset_batch_write(
    dataset_id: str,
    body: BatchWriteIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """业务写通道（Q1/ADR-0006）：ops 单事务原子；通道归属由凭据决定
    （wdk- → data-key + 指纹；console 会话 → console）。api 层数据集
    无引擎侧表 → 403 档位闸（批量写是 cell 耦合的付费面）。"""
    dataset = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    require_cell_tier(dataset, "批量写")
    _require_acl(db, principal, dataset, acl.NEED_WRITE)
    try:
        service.ensure_storage_quota(
            db,
            customer_id=principal.customer_id,
            incoming_bytes=_estimate_batch_bytes(body.ops),
        )
    except service.QuotaExceeded as exc:
        _raise_quota(exc)
    result = _engine_call(
        engine_factory,
        principal,
        TOOL_WRITE_BATCH,
        {
            "dataset_key": dataset.key,
            "ops": [op.model_dump() for op in body.ops],
            "actor": principal.actor_label or principal.actor_id or principal.customer_id,
            "channel": principal.channel,
            "actor_id": principal.actor_id,
            "actor_label": body.actor_label or principal.actor_label,
        },
    )
    _sync_row_count(db, dataset, engine_factory, principal)
    return result


# ── schema 演化 ──────────────────────────────────────────────────────


@router.post("/api/v1/datasets/{dataset_id}/columns", response_model=DatasetOut)
def dataset_add_column(
    dataset_id: str,
    body: AddColumnIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    dataset = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    require_cell_tier(dataset, "加列（schema 演化）")
    _require_acl(db, principal, dataset, acl.NEED_WRITE)
    result = _engine_call(
        engine_factory,
        principal,
        TOOL_ADD_COLUMN,
        {"dataset_key": dataset.key, "column": body.column.model_dump()},
    )
    if isinstance(result.get("columns"), list):
        dataset.columns_json = result["columns"]
        db.commit()
        db.refresh(dataset)
    return DatasetOut.from_dataset(dataset)


# ── 派生 ─────────────────────────────────────────────────────────────


def _register_parent_refs(
    db: Session,
    *,
    customer_id: str,
    child: Dataset,
    parents: list[Dataset],
    origin: str,
) -> None:
    """父集边登记（唯一收口；``_register_derived_child`` 与 DAG 多父共用）。

    ``(child, parent)`` 唯一约束在库层，这里先查后插保证幂等（DAG 节点的
    多输入里，union/link 的右父可能已被端点自身登记——去重后不重复写）。
    """
    from ..assets.models import DatasetParentRef  # noqa: PLC0415

    existing = {
        row[0]
        for row in db.execute(
            select(DatasetParentRef.parent_dataset_id).where(
                DatasetParentRef.child_dataset_id == child.id
            )
        ).all()
    }
    for parent in parents:
        if parent is None or parent.id in existing:
            continue
        existing.add(parent.id)
        db.add(
            DatasetParentRef(
                customer_id=customer_id,
                child_dataset_id=child.id,
                parent_dataset_id=parent.id,
                origin=origin,
            )
        )
    db.commit()


def _register_derived_child(
    db: Session,
    *,
    principal: DataPlanePrincipal,
    source: Dataset,
    name: str,
    engine_result: dict,
    extra_parents: list[Dataset] | None = None,
    parents: list[Dataset] | None = None,
    origin: str = "derive",
) -> Dataset:
    """登记派生子集 + 父集边。

    ``parents``（wire-dag-executor D6）：DAG 多输入节点的完整父集；单父调用
    点继续用 ``source``/``extra_parents``，行为逐字节不变。``source`` 仍是
    主父（``Dataset.source_ref`` 单列口径保留）。
    """
    child_key = str(engine_result.get("dataset_key") or "")
    if not child_key:
        raise HTTPException(status_code=502, detail="引擎派生未返回 dataset_key")
    dataset = Dataset(
        customer_id=principal.customer_id,
        key=child_key,
        name=name,
        source_kind="derived",
        connector_id=None,
        source_ref=source.key,
        columns_json=[],
        row_count=int(engine_result.get("rows") or 0),
        size_bytes=0,
        sync_status="ok",
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    # 父集边登记（wire-sensitivity-propagation 任务 1.2）：有效敏感度闭包的
    # 唯一数据源，本函数是全部派生路径的统一收口（derive/join/link/管线步骤）。
    # link 的右父经 extra_parents 传入（调用点现成）；DAG 多输入经 parents 传
    # 完整父集；去重（自环/重复输入合并为一条）。
    full_parents: list[Dataset] = [source]
    full_parents.extend(parents or [])
    full_parents.extend(extra_parents or [])
    _register_parent_refs(
        db,
        customer_id=principal.customer_id,
        child=dataset,
        parents=full_parents,
        origin=origin,
    )
    return dataset


@router.post("/api/v1/datasets/{dataset_id}/derive", response_model=DatasetOut, status_code=201)
def dataset_derive(
    dataset_id: str,
    body: DeriveIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """以 {dataset_id} 为源派生子集（Q13）：引擎血缘留档，子 key 由引擎侧
    （本 api 经 create_declared 同款 allocate 语义）落定。api 层数据集
    无引擎侧表 → 403 档位闸（派生是 cell 耦合的付费面）。"""
    source = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    require_cell_tier(source, "派生（derive）")
    _require_acl(db, principal, source, acl.NEED_WRITE)
    try:
        service.ensure_new_dataset_room(db, customer_id=principal.customer_id)
    except service.QuotaExceeded as exc:
        _raise_quota(exc)
    child_key = service.allocate_key(db, customer_id=principal.customer_id, name=body.name)
    result = _engine_call(
        engine_factory,
        principal,
        TOOL_DERIVE,
        {
            "child_key": child_key,
            "parent_key": source.key,
            "filters": body.filters,
            "sort": body.sort,
            "limit": body.limit,
            "actor": _actor(principal.customer_id),
            "channel": principal.channel,
        },
    )
    child = _register_derived_child(
        db, principal=principal, source=source, name=body.name, engine_result={**result, "dataset_key": child_key}
    )
    # 列与体量以引擎为准回填（派生集列=源列，join 额外值列）
    _sync_columns_and_size(db, child, engine_factory, principal)
    return DatasetOut.from_dataset(child)


def _sync_columns_and_size(
    db: Session,
    dataset: Dataset,
    engine_factory: EngineClientFactory,
    principal: DataPlanePrincipal,
) -> None:
    result = _engine_call(engine_factory, principal, TOOL_OVERVIEW, {"dataset_key": dataset.key})
    if isinstance(result.get("columns"), list):
        dataset.columns_json = result["columns"]
    dataset.row_count = int(result.get("rows") or 0)
    dataset.size_bytes = int(result.get("size_bytes") or 0)
    db.commit()


@router.post("/api/v1/datasets/{dataset_id}/join", response_model=DatasetOut, status_code=201)
def dataset_join(
    dataset_id: str,
    body: JoinIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """指标连接派生（Q17）：指标序列经 cell 引擎走 wire 入口拉取并计量；
    未命中值列填 null。未解析地区在响应明细（dataset.detail 查 lineage）。
    api 层数据集无引擎侧表 → 403 档位闸。"""
    source = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    require_cell_tier(source, "指标连接（join）")
    _require_acl(db, principal, source, acl.NEED_WRITE)
    try:
        service.ensure_new_dataset_room(db, customer_id=principal.customer_id)
    except service.QuotaExceeded as exc:
        _raise_quota(exc)
    child_key = service.allocate_key(db, customer_id=principal.customer_id, name=body.name)
    result = _engine_call(
        engine_factory,
        principal,
        TOOL_JOIN_INDICATORS,
        {
            "child_key": child_key,
            "parent_key": source.key,
            "indicators": [i.model_dump(by_alias=True) for i in body.indicators],
            "date_column": body.date_column,
            "region_column": body.region_column,
            "default_entity": body.default_entity,
            "filters": body.filters,
            "actor": _actor(principal.customer_id),
            "channel": principal.channel,
        },
    )
    child = _register_derived_child(
        db, principal=principal, source=source, name=body.name, engine_result={**result, "dataset_key": child_key},
        origin="join",
    )
    _sync_columns_and_size(db, child, engine_factory, principal)
    return DatasetOut.from_dataset(child)


# ── 确定性算子 + SQL 步骤（wire-transform-operators D1/D3/D4）─────────

#: 旧 cell 镜像（未滚动到含新算子的版本）的工具缺失错误特征——MCP 服务端
#: 对未注册工具的 JSON-RPC 错误文案，大小写不敏感子串匹配（同 entities 先例）。
_UNKNOWN_TOOL_MARKERS: tuple[str, ...] = (
    "unknown tool",
    "tool not found",
    "no such tool",
    "not a valid tool",
    "invalid tool",
    "工具不存在",
    "未知工具",
)

#: 工具名 → 面向客户的能力名（「引擎暂不支持」文案里要说人话）
_OPERATOR_LABELS: dict[str, str] = {
    TOOL_FILTER: "行过滤（filter）",
    TOOL_PROJECT: "投影（project）",
    TOOL_AGGREGATE_DERIVE: "分组聚合（aggregate）",
    TOOL_UNION: "纵向合并（union）",
    TOOL_WINDOW: "窗口计算（window）",
    TOOL_SQL: "SQL 步骤",
    TOOL_SQL_PREVIEW: "SQL 试运行",
}


def _call_operator_engine(
    engine_factory: EngineClientFactory,
    principal: DataPlanePrincipal,
    tool: str,
    arguments: dict,
    *,
    rejection_status: int = 400,
) -> dict:
    """算子/SQL 引擎调用 + 旧引擎降级矩阵（spec「旧引擎如实失败」）：

    - ``EngineUnavailable`` → 502 fail-closed（沿既有 engine_failure）；
    - 工具缺失（旧 cell 镜像未滚动）→ 502，detail 明示「引擎暂不支持」+原文；
    - 引擎域错误（列类型/占位/超上限）→ 400 如实；
    - 结果信封带 error / 形态异常 → 400 / 502 如实。

    ``rejection_status``：引擎域拒绝的 HTTP 码。四算子/SQL 沿用 400（既有
    行为零漂移）；window 用 422——design D1 的校验矩阵语义即「参数不合法」，
    spec 场景逐条写明 422。工具缺失/形态异常不受此参数影响（恒 502）。

    不 preflight 探测：探测省不了一次往返（真调用本身就是探测），且多一次
    调用就多一次失败面——沿 link 先例。
    """
    label = _OPERATOR_LABELS.get(tool, tool)
    client = engine_factory(principal.customer_id)
    try:
        result = client.call(tool, arguments)
    except EngineUnavailable as exc:
        raise engine_failure(exc) from exc
    except EngineCallError as exc:
        text = str(exc)
        lowered = text.lower()
        if any(marker in lowered for marker in _UNKNOWN_TOOL_MARKERS):
            raise HTTPException(
                status_code=502,
                detail=(
                    f"当前 cell 引擎不支持{label}（镜像未滚动到含该工具的版本）"
                    f"——引擎原文: {text}；请等待 cell 镜像滚动后重试，或联系平台支持"
                ),
            ) from exc
        raise HTTPException(
            status_code=rejection_status, detail=f"引擎拒绝: {text}"
        ) from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(
            status_code=rejection_status, detail=f"引擎拒绝: {result['error']}"
        )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail=f"引擎返回形态异常: {result!r}")
    return result


def _materialize_operator(
    db: Session,
    *,
    principal: DataPlanePrincipal,
    engine_factory: EngineClientFactory,
    source: Dataset,
    name: str,
    tool: str,
    arguments: dict,
    origin: str,
    extra_parents: list[Dataset] | None = None,
    rejection_status: int = 400,
) -> DatasetOut:
    """算子物化统一收口（D5「无第二条物化路径」）：

    配额闸 → allocate_key → 引擎工具 → ``_register_derived_child``（父边登记）
    → ``_sync_columns_and_size``（列与体量以引擎为准）。所有新算子与管线步骤
    都走这里，任何一环都不许绕开。
    """
    try:
        service.ensure_new_dataset_room(db, customer_id=principal.customer_id)
    except service.QuotaExceeded as exc:
        _raise_quota(exc)
    child_key = service.allocate_key(db, customer_id=principal.customer_id, name=name)
    result = _call_operator_engine(
        engine_factory,
        principal,
        tool,
        {
            **arguments,
            "child_key": child_key,
            "actor": _actor(principal.customer_id),
            "channel": principal.channel,
        },
        rejection_status=rejection_status,
    )
    child = _register_derived_child(
        db,
        principal=principal,
        source=source,
        name=name,
        engine_result={**result, "dataset_key": child_key},
        extra_parents=extra_parents,
        origin=origin,
    )
    _sync_columns_and_size(db, child, engine_factory, principal)
    return DatasetOut.from_dataset(child)


def _require_cell_source(db: Session, dataset_id: str, principal: DataPlanePrincipal) -> Dataset:
    """算子输入端：本客户 + allowlist + cell 档（三闸顺序与 derive/join 一致）。"""
    source = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    return source


@router.post(
    "/api/v1/datasets/{dataset_id}/filter",
    response_model=DatasetOut,
    status_code=201,
)
def dataset_filter(
    dataset_id: str,
    body: FilterIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """行过滤算子（wire-transform-operators）：与 derive 同一套 filters/sort/
    limit 语义，但血缘 kind=``filter``——管线与数据集图因此能如实标出「这一步
    是过滤」而不是笼统的「派生」。"""
    source = _require_cell_source(db, dataset_id, principal)
    require_cell_tier(source, "行过滤（filter）")
    _require_acl(db, principal, source, acl.NEED_WRITE)
    return _materialize_operator(
        db,
        principal=principal,
        engine_factory=engine_factory,
        source=source,
        name=body.name,
        tool=TOOL_FILTER,
        arguments={
            "parent_key": source.key,
            "filters": body.filters,
            "sort": body.sort,
            "limit": body.limit,
        },
        origin="filter",
    )


@router.post(
    "/api/v1/datasets/{dataset_id}/project",
    response_model=DatasetOut,
    status_code=201,
)
def dataset_project(
    dataset_id: str,
    body: ProjectIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """投影 + 重命名算子：输出列序按声明序；重名 as / 未知源列由引擎 422。"""
    source = _require_cell_source(db, dataset_id, principal)
    require_cell_tier(source, "投影（project）")
    _require_acl(db, principal, source, acl.NEED_WRITE)
    return _materialize_operator(
        db,
        principal=principal,
        engine_factory=engine_factory,
        source=source,
        name=body.name,
        tool=TOOL_PROJECT,
        arguments={
            "parent_key": source.key,
            "columns": [c.model_dump(by_alias=True, exclude_none=True) for c in body.columns],
        },
        origin="project",
    )


@router.post(
    "/api/v1/datasets/{dataset_id}/aggregate/derive",
    response_model=DatasetOut,
    status_code=201,
)
def dataset_aggregate_derive(
    dataset_id: str,
    body: AggregateDeriveIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """分组聚合**物化**算子（区别于只读面板聚合 ``POST /aggregate``——那条返回
    行不落表；本条把聚合结果落成派生数据集，供下游步骤继续加工）。"""
    source = _require_cell_source(db, dataset_id, principal)
    require_cell_tier(source, "分组聚合（aggregate）")
    _require_acl(db, principal, source, acl.NEED_WRITE)
    return _materialize_operator(
        db,
        principal=principal,
        engine_factory=engine_factory,
        source=source,
        name=body.name,
        tool=TOOL_AGGREGATE_DERIVE,
        arguments={
            "parent_key": source.key,
            "group_by": body.group_by,
            "measures": body.measures,
        },
        origin="aggregate",
    )


@router.post(
    "/api/v1/datasets/{dataset_id}/union",
    response_model=DatasetOut,
    status_code=201,
)
def dataset_union(
    dataset_id: str,
    body: UnionIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """按名对齐纵向合并（左集 = 路径数据集，右集 = 请求体指定）。

    两个父集都登记父边（敏感度闭包因此对两侧都生效——否则右集的敏感档会
    在合并结果上凭空消失）。
    """
    left = _require_cell_source(db, dataset_id, principal)
    require_cell_tier(left, "纵向合并（union）")
    _require_acl(db, principal, left, acl.NEED_WRITE)
    right = db.get(Dataset, body.right_dataset_id)
    if right is None or right.customer_id != principal.customer_id:
        raise HTTPException(
            status_code=404, detail=f"数据集 {body.right_dataset_id} 不存在"
        )
    _require_acl(db, principal, right)
    return _materialize_operator(
        db,
        principal=principal,
        engine_factory=engine_factory,
        source=left,
        name=body.name,
        tool=TOOL_UNION,
        arguments={
            "left_key": left.key,
            "right_key": right.key,
            "on": body.on,
        },
        origin="union",
        extra_parents=[right],
    )


@router.post(
    "/api/v1/datasets/{dataset_id}/window",
    response_model=DatasetOut,
    status_code=201,
)
@router.post(
    "/api/v1/data/datasets/{dataset_id}/window",
    response_model=DatasetOut,
    status_code=201,
    include_in_schema=False,
)
def dataset_window(
    dataset_id: str,
    body: WindowIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """分区/排序窗口算子（wire-window-operator）：输出=源列原样 + 各 ``as`` 列，
    行数与源一致；血缘 kind=``window``（spec 存 partition_by/order_by/window，
    ``derive_refresh`` 按 spec 重放）。

    三条闸与四算子逐字同序：租户 404 → allowlist 403 → 档位 403 → ACL
    （写面 NEED_WRITE——窗口产出派生数据集，与 filter/project 同属写面）。
    引擎域校验错误（排序类缺 order_by、``as`` 撞源列、offset 越界等）按
    design D1/spec 场景映射 422 并带引擎原文；旧镜像缺工具 → 502「引擎暂
    不支持」（``_call_operator_engine`` 的诚实降级矩阵）。

    ``/api/v1/data/datasets/{id}/window`` 是 design D5 写的路径别名（同一
    函数、同一语义；不进 OpenAPI schema——数据面既有面是
    ``/api/v1/datasets/...``）。
    """
    source = _require_cell_source(db, dataset_id, principal)
    require_cell_tier(source, "窗口计算（window）")
    _require_acl(db, principal, source, acl.NEED_WRITE)
    return _materialize_operator(
        db,
        principal=principal,
        engine_factory=engine_factory,
        source=source,
        name=body.name,
        tool=TOOL_WINDOW,
        arguments={
            "parent_key": source.key,
            "partition_by": body.partition_by,
            "order_by": body.order_by,
            "window": body.window,
        },
        origin="window",
        rejection_status=422,
    )


@router.post("/api/v1/datasets/{dataset_id}/sql/preview")
def dataset_sql_preview(
    dataset_id: str,
    body: SqlPreviewIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """SQL 试运行（**零副作用**）：同一执行器 + 强制 LIMIT 预览，绝不登记数据集。

    路径带 ``{dataset_id}`` 只为对齐既有数据面形状（闸与 allowlist 判定）；
    SQL 真正能看见哪些数据集由 ``tables`` 占位声明决定，且引擎只在**本 cell**
    内解析——跨租户面在物理上不存在（cell 隔离）。
    """
    source = _require_cell_source(db, dataset_id, principal)
    require_cell_tier(source, "SQL 试运行")
    _require_acl(db, principal, source)
    return _call_operator_engine(
        engine_factory,
        principal,
        TOOL_SQL_PREVIEW,
        {
            "sql": body.sql,
            "tables": [t.model_dump() for t in body.tables],
            "params": body.params,
            "limit": body.limit,
        },
    )


@router.post(
    "/api/v1/datasets/{dataset_id}/sql",
    response_model=DatasetOut,
    status_code=201,
)
def dataset_sql(
    dataset_id: str,
    body: SqlIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """SQL 步骤物化：只读 SELECT 子集 → 派生数据集（SQL 文本/占位/参数入血缘）。

    多占位时，**除路径数据集外**的占位数据集也登记父边：SQL 血缘的主父是
    引擎侧第一条占位，本地父边则覆盖全部被读到的数据集，敏感度闭包与「派生
    子集随父变化」两条语义因此都不漏。
    """
    source = _require_cell_source(db, dataset_id, principal)
    require_cell_tier(source, "SQL 步骤")
    _require_acl(db, principal, source, acl.NEED_WRITE)
    declared_keys = {t.dataset_key for t in body.tables}
    declared_keys.add(source.key)
    others: list[Dataset] = []
    for key in sorted(declared_keys - {source.key}):
        parent = db.execute(
            select(Dataset).where(
                Dataset.customer_id == principal.customer_id, Dataset.key == key
            )
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail=f"数据集 {key} 不存在")
        _require_acl(db, principal, parent)
        others.append(parent)
    return _materialize_operator(
        db,
        principal=principal,
        engine_factory=engine_factory,
        source=source,
        name=body.name,
        tool=TOOL_SQL,
        arguments={
            "sql": body.sql,
            "tables": [t.model_dump() for t in body.tables],
            "params": body.params,
        },
        origin="sql",
        extra_parents=others,
    )


# ── 重派生与级联（wire-refresh-scheduler 2.1/2.2；调度执行器复用同段）──

#: 级联深度上限（transitive 展开的护栏；visited 防环之外的第二道闸）
MAX_CASCADE_DEPTH = 10


def refresh_derived_dataset(
    db: Session,
    dataset: Dataset,
    engine_factory: EngineClientFactory,
    principal: DataPlanePrincipal,
) -> None:
    """单派生数据集重派生（可内部调用段）：引擎 derive_refresh + 元数据回填。

    手动端点与刷新调度执行器（schedules/executor）共用同一实现，行为零漂移。
    """
    _engine_call(
        engine_factory, principal, TOOL_DERIVE_REFRESH, {"dataset_key": dataset.key}
    )
    _sync_columns_and_size(db, dataset, engine_factory, principal)
    # 质量快照钩子（wire-transform-pipelines D5 统一路收拢）：重派生成功后
    # 触发一次质量快照（triggered_by=materialize）。此前钩子只挂在物化调度
    # 路（materialization/runner._run_derived_refresh 尾部），手动刷新与
    # 统一刷新调度器经本函数不带快照——语义分叉；管线步骤刷新复用本函数
    # 时把钩子收拢到这一处（手动刷新/调度刷新/管线刷新/级联刷新同源）。
    # 失败隔离：notify_dataset_changed 自身绝不抛异常（双保险 try），
    # 快照失败绝不影响重派生主流程（结果已在头顶落库）。
    try:
        from ..quality import service as quality_service  # noqa: PLC0415

        quality_service.notify_dataset_changed(
            dataset, "materialize", client=engine_factory(dataset.customer_id)
        )
    except Exception:  # noqa: BLE001 - 双保险：主流程绝不因快照中断
        pass


def direct_derived_children(db: Session, dataset: Dataset) -> list[Dataset]:
    """直接派生子反查：derive/join 登记时 ``source_ref=父 key``、
    ``source_kind='derived'``（见 _register_derived_child）——与 GET lineage
    端点回显的引擎 children 同源的血缘元数据，本地反查零引擎调用。"""
    return list(
        db.execute(
            select(Dataset).where(
                Dataset.customer_id == dataset.customer_id,
                Dataset.source_kind == "derived",
                Dataset.source_ref == dataset.key,
            )
        ).scalars()
    )


def cascade_refresh_children(
    db: Session,
    dataset: Dataset,
    engine_factory: EngineClientFactory,
    principal: DataPlanePrincipal,
    *,
    max_depth: int = MAX_CASCADE_DEPTH,
) -> list[dict]:
    """级联刷新：刷新本体后 transitive 依次刷新派生子链。

    visited 集合防环（异常数据态不无限递归）+ 深度上限 10；任一环失败记录
    该环 error 但继续其余环（失败隔离）——返回各级结果摘要（端点透出）。
    """
    results: list[dict] = []
    visited = {dataset.id}

    def _walk(parent: Dataset, depth: int) -> None:
        if depth >= max_depth:
            return
        for child in direct_derived_children(db, parent):
            if child.id in visited:  # 环防护：路径上已访问集合阻断
                continue
            visited.add(child.id)
            entry: dict = {
                "dataset_id": child.id,
                "name": child.name,
                "depth": depth + 1,
            }
            try:
                refresh_derived_dataset(db, child, engine_factory, principal)
                entry["status"] = "ok"
                entry["error"] = None
            except Exception as exc:  # noqa: BLE001 - 失败隔离：记录后继续其余环
                entry["status"] = "failed"
                entry["error"] = str(getattr(exc, "detail", None) or exc)
            results.append(entry)
            _walk(child, depth + 1)

    _walk(dataset, 0)
    return results


@router.post("/api/v1/datasets/{dataset_id}/refresh")
def dataset_refresh(
    dataset_id: str,
    cascade: bool = True,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """重派生：按存档 spec 对源当前有效态重算（join 会重拉指标=再次计量）。

    ``cascade`` 查询参数（默认 true）：刷新本体后依次级联刷新派生子链
    （transitively、visited 防环、深度上限 10、单环失败隔离）——响应在
    数据集字段外附 ``cascade`` 各级结果摘要。api 层数据集不可派生 →
    403 档位闸（source_kind 校验之外的兜底）。"""
    dataset = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    require_cell_tier(dataset, "重派生（refresh）")
    _require_acl(db, principal, dataset, acl.NEED_WRITE)
    if dataset.source_kind != "derived":
        raise HTTPException(
            status_code=400,
            detail=f"仅派生数据集可刷新（source_kind={dataset.source_kind}）",
        )
    refresh_derived_dataset(db, dataset, engine_factory, principal)
    cascade_results = (
        cascade_refresh_children(db, dataset, engine_factory, principal)
        if cascade
        else []
    )
    db.refresh(dataset)
    payload = DatasetOut.from_dataset(dataset).model_dump(mode="json")
    payload["cascade"] = cascade_results
    return payload


@router.get("/api/v1/datasets/{dataset_id}/lineage")
def dataset_lineage(
    dataset_id: str,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """血缘（引擎 key → dataset_id 映射后返回，附未解析地区等派生明细）。
    api 层数据集无引擎血缘 → 本地空视图（最小呈现，零引擎调用）。"""
    dataset = _readable_dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    _require_acl(db, principal, dataset)
    if _is_api_tier(dataset):
        return {"derivation": [], "children": []}
    result = _engine_call(
        engine_factory, principal, TOOL_LINEAGE, {"dataset_key": dataset.key}
    )
    key_to_id = {
        row.key: row.id
        for row in db.execute(
            select(Dataset).where(Dataset.customer_id == principal.customer_id)
        ).scalars()
    }
    for entry in result.get("derivation", []):
        if entry.get("parent_key") in key_to_id:
            entry["parent_dataset_id"] = key_to_id[entry["parent_key"]]
    for child in result.get("children", []):
        if child.get("dataset_key") in key_to_id:
            child["dataset_id"] = key_to_id[child["dataset_key"]]
    return result


# ── 定时重派生（6.1，搭 materialization 调度器）─────────────────────


class ScheduleIn(BaseModel):
    """五字段 cron（naive UTC；客户时区由登记侧换算——cron.py 既有 v1 口径）。"""

    cron: str = Field(min_length=5, max_length=64)


@router.put("/api/v1/datasets/{dataset_id}/schedule")
def dataset_put_schedule(
    dataset_id: str,
    body: ScheduleIn,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
) -> dict:
    """为派生数据集登记/更新定时重派生（唯一调度行 per 数据集）。"""
    from ..materialization.cron import parse_cron
    from ..materialization.db import get_session as get_mat_session
    from ..materialization.models import MaterializedArtifact

    dataset = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    _require_acl(db, principal, dataset, acl.NEED_WRITE)
    if dataset.source_kind != "derived":
        raise HTTPException(
            status_code=400, detail=f"仅派生数据集可登记定时重派生（source_kind={dataset.source_kind}）"
        )
    try:
        parse_cron(body.cron)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mat = get_mat_session()
    try:
        row = mat.execute(
            select(MaterializedArtifact).where(
                MaterializedArtifact.kind == "derived_dataset",
                MaterializedArtifact.ref_key == dataset_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = MaterializedArtifact(
                artifact_id=None,
                customer_id=principal.customer_id,
                kind="derived_dataset",
                ref_key=dataset_id,
                spec={"dataset_id": dataset_id, "name": dataset.name},
                schedule=body.cron,
                enabled=True,
            )
            mat.add(row)
        else:
            row.schedule = body.cron
            row.enabled = True
        mat.commit()
        mat.refresh(row)
        return {
            "dataset_id": dataset_id,
            "cron": row.schedule,
            "enabled": row.enabled,
            "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            "last_status": row.last_status,
        }
    finally:
        mat.close()


@router.get("/api/v1/datasets/{dataset_id}/schedule")
def dataset_get_schedule(
    dataset_id: str,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
) -> dict:
    dataset = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    _require_acl(db, principal, dataset)
    from ..materialization.db import get_session as get_mat_session
    from ..materialization.models import MaterializedArtifact

    mat = get_mat_session()
    try:
        row = mat.execute(
            select(MaterializedArtifact).where(
                MaterializedArtifact.kind == "derived_dataset",
                MaterializedArtifact.ref_key == dataset_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"dataset_id": dataset_id, "cron": None, "enabled": False}
        return {
            "dataset_id": dataset_id,
            "cron": row.schedule,
            "enabled": row.enabled,
            "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            "last_status": row.last_status,
            "last_error": row.last_error,
            "last_rows": row.last_rows,
        }
    finally:
        mat.close()


@router.delete("/api/v1/datasets/{dataset_id}/schedule")
def dataset_delete_schedule(
    dataset_id: str,
    principal: DataPlanePrincipal = Depends(require_data_plane_principal),
    db: Session = Depends(get_db),
) -> dict:
    dataset = _dataset_or_404(db, dataset_id, principal.customer_id)
    ensure_dataset_allowed(principal, dataset_id)
    _require_acl(db, principal, dataset, acl.NEED_WRITE)
    from ..materialization.db import get_session as get_mat_session
    from ..materialization.models import MaterializedArtifact

    mat = get_mat_session()
    try:
        row = mat.execute(
            select(MaterializedArtifact).where(
                MaterializedArtifact.kind == "derived_dataset",
                MaterializedArtifact.ref_key == dataset_id,
            )
        ).scalar_one_or_none()
        if row is not None:
            mat.delete(row)
            mat.commit()
        return {"dataset_id": dataset_id, "removed": True}
    finally:
        mat.close()
