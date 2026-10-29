"""实体语义层 API 路由（wire-entity-layer）。

客户侧（Bearer ID token → ``require_console_customer_id``，与
artifacts/schedules 切片同款身份键）：

- ``GET    /api/v1/entities``                          实体列表（含数据集摘要）
- ``POST   /api/v1/entities``                          声明实体 → 201
- ``PUT    /api/v1/entities/{id}`                      整量更新（重校验）
- ``DELETE /api/v1/entities/{id}`                      删（级联两端关系，带计数）
- ``POST   /api/v1/entities/{id}/relationships`        声明关系 → 201
- ``DELETE /api/v1/entities/{id}/relationships/{rid}`  删关系（须以该实体为端点）
- ``GET    /api/v1/entities/graph`                     同源图谱（空图谱 200 空集）
- ``POST   /api/v1/entities/{id}/relationships/{rid}/materialize``
  按关系声明物化连接数据集（wire-entity-link，design D2）→ 201 DatasetOut

错误矩阵：404（他企/不存在，不泄露）；409（唯一冲突，detail 带冲突方）；
422（结构/校验，逐条文案，零半写）；403（物化档位闸——两端数据集均须
cell 档，PLAN_TIER_REQUIRED）；429（配额满）；502（引擎不可达/旧 cell
镜像未含 link 工具，detail 带引擎原文）。

数据集摘要与属主/列校验查 datasets 表 → 必须用 datasets 库会话（生产
wire_platform/wire_datasets 分库，实体表所在的元数据库会话落不到表——
与 artifacts 的 form 属主校验同款双库注入，测试夹具合库不得掩盖此拓扑）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..console.auth import require_console_customer_id
from ..data_plane.auth import DataPlanePrincipal
from ..data_plane.data_routes import _register_derived_child, _sync_columns_and_size
from ..datasets import service as datasets_service
from ..datasets.db import get_db as get_datasets_db
from ..datasets.ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
    TOOL_LINK,
    get_engine_client_factory,
)
from ..datasets.models import Dataset
from ..datasets.routes import _actor, _raise_quota, engine_failure, require_cell_tier
from ..datasets.schemas import DatasetOut
from . import service
from ..ai.db import get_db as get_ai_db
from .db import get_db as get_entities_db
from .models import Entity
from .service import dataset_summary

router = APIRouter(prefix="/api/v1/entities", tags=["entities"])


# ------------------------------------------------------------------ schemas


class EntityCreateIn(BaseModel):
    """声明/更新实体请求体（PUT 为整量替换，重走同一条校验链）。

    v2 可选属性元数据：display_column（对象展示列）/ column_notes（列注记）——
    不带新键的 v0 请求行为逐字不变。
    """

    name: str
    dataset_id: str = Field(min_length=1)
    pk_columns: list[str]
    description: str | None = None
    display_column: str | None = None
    column_notes: dict[str, str] | None = None


class DatasetSummaryOut(BaseModel):
    """数据集摘要（数据集行缺失=drift 时除 id 外如实置 None）。"""

    id: str
    key: str | None = None
    name: str | None = None
    row_count: int | None = None
    storage_tier: str | None = None


class EntityOut(BaseModel):
    """实体卡片（含数据集摘要——datasets 库会话取）。"""

    id: int
    name: str
    dataset_id: str
    pk_columns: list[str]
    description: str | None = None
    display_column: str | None = None
    column_notes: dict[str, str] | None = None
    dataset: DatasetSummaryOut
    created_at: datetime
    updated_at: datetime


class RelationshipCreateIn(BaseModel):
    """声明关系请求体：目标实体 + 源列 + 目标主键列 + 基数。

    v2：kind 补 ``1:n``（反向声明，服务端归一化存储）与 ``m:n``
    （须带中间数据集三键）。
    """

    to_entity_id: int
    from_column: str = Field(min_length=1)
    to_column: str = Field(min_length=1)
    kind: str
    label: str | None = None
    junction_dataset_id: str | None = None
    junction_source_column: str | None = None
    junction_target_column: str | None = None


class RelationshipOut(BaseModel):
    """关系卡片（**声明方向**呈现——1:n 存储为反向 n:1，此处按声明翻转）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    from_entity_id: int
    from_column: str
    to_entity_id: int
    to_column: str
    kind: str
    label: str | None = None
    junction_dataset_id: str | None = None
    junction_source_column: str | None = None
    junction_target_column: str | None = None
    created_at: datetime


class GraphNodeOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    pk_columns: list[str]
    display_column: str | None = None
    column_notes: dict[str, str] | None = None
    dataset: DatasetSummaryOut


class GraphEdgeOut(BaseModel):
    id: int
    from_entity_id: int
    from_column: str
    to_entity_id: int
    to_column: str
    kind: str
    label: str | None = None
    junction_dataset_id: str | None = None
    junction_source_column: str | None = None
    junction_target_column: str | None = None


class GraphOut(BaseModel):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]


# ------------------------------------------------------------------ helpers


def _entity_or_404(db: Session, entity_id: int, customer_id: str) -> Entity:
    try:
        return service.get_owned_entity(db, entity_id=entity_id, customer_id=customer_id)
    except service.EntityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _entity_out(entity: Entity, datasets_db: Session) -> EntityOut:
    dataset = datasets_db.get(Dataset, entity.dataset_id)
    return EntityOut(
        id=entity.id,
        name=entity.name,
        dataset_id=entity.dataset_id,
        pk_columns=list(entity.pk_columns or []),
        description=entity.description,
        display_column=entity.display_column,
        column_notes=dict(entity.column_notes or {}) or None,
        dataset=DatasetSummaryOut(
            **dataset_summary(dataset, entity.dataset_id)
        ),
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


# ------------------------------------------------------------------ 端点


@router.get("")
def list_entities(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> dict:
    """本企业实体列表（创建序；数据集摘要经 datasets 库会话）。

    信封 {entities:[…]}——照 corrections/datasets 列表既定惯例（web 客户端
    与假后端同款形状）。"""
    return {
        "entities": [
            _entity_out(entity, datasets_db).model_dump(mode="json")
            for entity in service.list_entities(db, customer_id=customer_id)
        ]
    }


@router.get("/graph", response_model=GraphOut)
def get_graph(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> GraphOut:
    """同源图谱（控制台实体页与 AI 上下文注入的唯一同源；空图谱 200 空集）。"""
    graph: dict[str, list[dict[str, Any]]] = service.entity_graph(
        db, datasets_db, customer_id=customer_id
    )
    return GraphOut(
        nodes=[GraphNodeOut(**node) for node in graph["nodes"]],
        edges=[GraphEdgeOut(**edge) for edge in graph["edges"]],
    )


@router.post("", response_model=EntityOut, status_code=status.HTTP_201_CREATED)
def create_entity(
    payload: EntityCreateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> EntityOut:
    """声明实体：name/dataset 属主/pk ⊆ 列快照/双唯一 全过才落行（否则零半写）。"""
    try:
        entity = service.create_entity(
            db,
            datasets_db,
            customer_id=customer_id,
            name=payload.name,
            dataset_id=payload.dataset_id,
            pk_columns=payload.pk_columns,
            description=payload.description,
            display_column=payload.display_column,
            column_notes=payload.column_notes,
        )
    except service.EntityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidEntity as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _entity_out(entity, datasets_db)


@router.put("/{entity_id}", response_model=EntityOut)
def update_entity(
    entity_id: int,
    payload: EntityCreateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> EntityOut:
    """整量更新（重校验——与创建同一条链；他企/不存在一律 404）。"""
    entity = _entity_or_404(db, entity_id, customer_id)
    try:
        entity = service.update_entity(
            db,
            datasets_db,
            entity,
            name=payload.name,
            dataset_id=payload.dataset_id,
            pk_columns=payload.pk_columns,
            description=payload.description,
            display_column=payload.display_column,
            column_notes=payload.column_notes,
        )
    except service.EntityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidEntity as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _entity_out(entity, datasets_db)


@router.delete("/{entity_id}")
def delete_entity(
    entity_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    ai_db: Session = Depends(get_ai_db),
) -> dict:
    """删实体：同事务级联删两端关系（响应带 deleted_relationships 计数留痕）；
    域动作类型级联停用（ai 元数据会话注入——wire-domain-actions）。"""
    entity = _entity_or_404(db, entity_id, customer_id)
    removed = service.delete_entity(db, entity, domain_db=ai_db)
    return {
        "id": entity_id,
        "deleted": True,
        "deleted_relationships": removed,
    }


@router.post(
    "/{entity_id}/relationships",
    response_model=RelationshipOut,
    status_code=status.HTTP_201_CREATED,
)
def create_relationship(
    entity_id: int,
    payload: RelationshipCreateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> RelationshipOut:
    """声明关系：源列 ∈ 源集列、目标列 ∈ 目标实体主键（纪律）、四元组唯一。"""
    entity = _entity_or_404(db, entity_id, customer_id)
    try:
        relationship = service.create_relationship(
            db,
            datasets_db,
            customer_id=customer_id,
            source_entity=entity,
            to_entity_id=payload.to_entity_id,
            from_column=payload.from_column,
            to_column=payload.to_column,
            kind=payload.kind,
            label=payload.label,
            junction_dataset_id=payload.junction_dataset_id,
            junction_source_column=payload.junction_source_column,
            junction_target_column=payload.junction_target_column,
        )
    except service.EntityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.EntityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidEntity as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RelationshipOut.model_validate(
        {**service.declared_view(relationship), "created_at": relationship.created_at}
    )


@router.delete("/{entity_id}/relationships/{rel_id}")
def delete_relationship(
    entity_id: int,
    rel_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
) -> dict:
    """删关系：须属本企业且以该实体为端点，否则一律 404（不泄露）。"""
    _entity_or_404(db, entity_id, customer_id)
    try:
        service.delete_relationship(
            db, customer_id=customer_id, entity_id=entity_id, rel_id=rel_id
        )
    except service.EntityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": rel_id, "deleted": True}


# ------------------------------------------------- 关系物化（wire-entity-link）


class MaterializeIn(BaseModel):
    """物化请求体（name 缺省「{左实体名}×{右实体名} 连接」）。"""

    name: str | None = Field(default=None, max_length=200)


#: 旧 cell 镜像（未滚动到含 link 工具的版本）的工具缺失错误特征——MCP
#: 服务端对未注册工具的 JSON-RPC 错误文案，大小写不敏感子串匹配。
_UNKNOWN_TOOL_MARKERS: tuple[str, ...] = (
    "unknown tool",
    "tool not found",
    "no such tool",
    "not a valid tool",
    "invalid tool",
    "工具不存在",
    "未知工具",
)


def _is_unknown_tool_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _UNKNOWN_TOOL_MARKERS)


def _call_link_engine(
    engine_factory: EngineClientFactory, customer_id: str, arguments: dict
) -> dict:
    """引擎 link 调用 + link 专属错误矩阵（design D2）：

    - ``EngineUnavailable`` → 502 fail-closed（照 datasets 既有 engine_failure）；
    - 工具缺失（旧 cell 镜像未滚动）→ 502，detail 带引擎原文（如实不伪造）；
    - 引擎域错误（列/数据集/上限）→ 400 如实（照「引擎拒绝」先例）；
    - 结果信封带 error / 形态异常 → 400 / 502 如实。
    """
    client = engine_factory(customer_id)
    try:
        result = client.call(TOOL_LINK, arguments)
    except EngineUnavailable as exc:
        raise engine_failure(exc) from exc
    except EngineCallError as exc:
        text = str(exc)
        if _is_unknown_tool_error(text):
            raise HTTPException(
                status_code=502,
                detail=(
                    "当前 cell 引擎镜像未包含连接工具 customer_dataset_link"
                    f"（旧镜像未滚动）——引擎原文: {text}；"
                    "请等待 cell 镜像滚动到含 link 工具的版本后重试，或联系平台支持"
                ),
            ) from exc
        raise HTTPException(status_code=400, detail=f"引擎拒绝: {text}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=f"引擎拒绝: {result['error']}")
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail=f"引擎返回形态异常: {result!r}")
    return result


@router.post(
    "/{entity_id}/relationships/{rel_id}/materialize",
    response_model=DatasetOut,
    status_code=status.HTTP_201_CREATED,
)
def materialize_relationship(
    entity_id: int,
    rel_id: int,
    payload: MaterializeIn = Body(default=MaterializeIn()),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """按关系声明一键物化连接数据集（design D2）。

    左=关系 from 实体的数据集、右=to 实体的数据集（端点参数只是定位入口，
    任一端实体路径均可触发）；两端均须 cell 档（否则 403 档位闸——免费档
    数据存 api 层轻量存储，不在引擎侧）。引擎 LEFT JOIN 语义（左行全保留、
    未命中右列置 NULL、撞名 right_ 前缀）；子集登记 source_kind="derived"、
    source_ref=左集 key（管线图自然挂上左父边），列与体量以引擎 overview
    回填。零半写：引擎成功后才登记；登记失败则响应如实带清理提示文案。
    """
    _entity_or_404(db, entity_id, customer_id)
    try:
        rel, left_entity, right_entity, left_dataset, right_dataset = (
            service.resolve_relationship_materialization(
                db,
                datasets_db,
                customer_id=customer_id,
                entity_id=entity_id,
                rel_id=rel_id,
            )
        )
    except service.EntityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.InvalidEntity as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    require_cell_tier(left_dataset, "物化连接（实体关系 link）")
    require_cell_tier(right_dataset, "物化连接（实体关系 link）")

    try:
        datasets_service.ensure_new_dataset_room(
            datasets_db, customer_id=customer_id
        )
    except datasets_service.QuotaExceeded as exc:
        _raise_quota(exc)

    name = (payload.name or "").strip() or (
        f"{left_entity.name}×{right_entity.name} 连接"
    )
    # Dataset.name 列宽 200（用户名 pydantic 已拦；缺省名在两侧实体名各近
    # 100 的极端组合下可能越界——截断兜底，不落库报错）
    name = name[:200]

    child_key = datasets_service.allocate_key(
        datasets_db, customer_id=customer_id, name=name
    )
    result = _call_link_engine(
        engine_factory,
        customer_id,
        {
            "child_key": child_key,
            "left_key": left_dataset.key,
            "left_column": rel.from_column,
            "right_key": right_dataset.key,
            "right_column": rel.to_column,
            "actor": _actor(customer_id),
            "channel": "console",
        },
    )

    # console 轨道 principal（与数据面 console 凭据同款口径）——子集登记与
    # 列/体量回填复用 derive 先例的共享实现（data_routes 私有函数照原样复用）
    principal = DataPlanePrincipal(
        customer_id=customer_id, channel="console", actor_id=customer_id
    )
    try:
        child = _register_derived_child(
            datasets_db,
            principal=principal,
            source=left_dataset,
            name=name,
            engine_result={**result, "dataset_key": child_key},
            extra_parents=[right_dataset],
            origin="link",
        )
    except HTTPException as exc:
        # 零半写 = 引擎成功才登记；此处登记失败则引擎侧子集已成孤儿——
        # 响应如实带清理提示（不静默吞、不伪造成功）
        raise HTTPException(
            status_code=502,
            detail=(
                f"连接数据集已在引擎侧生成（key={child_key}），但本地登记失败: "
                f"{exc.detail}——请重试物化，或联系平台支持清理引擎侧孤儿"
                f"（key={child_key}）"
            ),
        ) from exc
    _sync_columns_and_size(datasets_db, child, engine_factory, principal)
    return DatasetOut.from_dataset(child)


# ------------------------------------------------------------------ 对象浏览
# （wire-entity-v2：可浏览、可遍历的对象世界——owner 输出面 + MCP 工具同源）


def _browse_dataset_or_403(
    datasets_db: Session, entity: Entity, customer_id: str, *, action: str
) -> Dataset:
    """实体绑定数据集（datasets 库属主校验）+ cell 档闸（对象浏览 cell 档
    专属——免费档 403 如实，沿关系物化先例）。"""
    dataset = datasets_db.get(Dataset, entity.dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"实体 {entity.id} 不存在")
    require_cell_tier(dataset, action)
    return dataset


def _console_principal(customer_id: str) -> DataPlanePrincipal:
    return DataPlanePrincipal(
        customer_id=customer_id, channel="console", actor_id=customer_id
    )


def _run_dataset_query(
    datasets_db: Session,
    engine_factory: EngineClientFactory,
    customer_id: str,
    dataset: Dataset,
    *,
    filters: list[dict],
    sort: list[dict] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """复用数据面查询端点函数（console 凭据轨；cell 档走引擎——与面板刷新
    同一执行层，零第二执行路径）。"""
    from ..data_plane.data_routes import QueryIn, dataset_query  # noqa: PLC0415

    body = QueryIn(filters=filters, sort=sort or [], limit=limit, offset=offset)
    return dataset_query(
        dataset.id, body, _console_principal(customer_id), datasets_db, engine_factory
    )


def _rows_to_dicts(result: dict) -> tuple[list[str], list[dict]]:
    """引擎/本地查询结果 → 行字典（columns 为 {name,type} 卡片或字符串的容错）。"""
    columns_raw = result.get("columns") or []
    names = [
        (col.get("name") if isinstance(col, dict) else str(col))
        for col in columns_raw
    ]
    rows = [dict(zip(names, row)) for row in (result.get("rows") or [])]
    return names, rows


@router.get("/{entity_id}/objects")
def list_entity_objects(
    entity_id: int,
    q: str | None = Query(default=None, max_length=200),
    filters: str | None = Query(default=None, max_length=2000),
    sort_column: str | None = Query(default=None, max_length=200),
    sort_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """对象实例列表（cell 档专属；只查实体绑定数据集——读边界纪律）。"""
    entity = _entity_or_404(db, entity_id, customer_id)
    dataset = _browse_dataset_or_403(
        datasets_db, entity, customer_id, action="对象浏览"
    )
    parsed_filters: list[dict] = []
    if filters:
        import json as _json  # noqa: PLC0415

        try:
            parsed = _json.loads(filters)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"filters 必须是 JSON 数组: {exc}"
            ) from exc
        if not isinstance(parsed, list) or len(parsed) > 50:
            raise HTTPException(status_code=422, detail="filters 必须是 ≤50 条的数组")
        parsed_filters = parsed
    if q:
        needle_column = entity.display_column or (
            (entity.pk_columns or [""])[0] if entity.pk_columns else ""
        )
        if not needle_column:
            raise HTTPException(
                status_code=422, detail="实体未声明主键，无法按关键字检索"
            )
        parsed_filters = parsed_filters + [
            {"column": needle_column, "op": "contains", "value": q}
        ]
    sort = (
        [{"column": sort_column, "direction": sort_dir}] if sort_column else []
    )
    offset = (page - 1) * page_size
    result = _run_dataset_query(
        datasets_db,
        engine_factory,
        customer_id,
        dataset,
        filters=parsed_filters,
        sort=sort,
        limit=page_size,
        offset=offset,
    )
    names, rows = _rows_to_dicts(result)
    return {
        "entity_id": entity.id,
        "entity_name": entity.name,
        "dataset_id": dataset.id,
        "display_column": entity.display_column,
        "pk_columns": list(entity.pk_columns or []),
        "columns": names,
        "objects": rows,
        "total": result.get("total"),
        "page": page,
        "page_size": page_size,
    }


@router.get("/{entity_id}/objects/{key}")
def get_entity_object(
    entity_id: int,
    key: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """单对象（主键等值取行；零行=结构化「无此对象」态不泄露历史存在性）。

    v0 读边界：对象浏览仅支持单列主键实体（多列主键 422 如实——组合键取数
    留 v1）。"""
    entity = _entity_or_404(db, entity_id, customer_id)
    dataset = _browse_dataset_or_403(
        datasets_db, entity, customer_id, action="对象浏览"
    )
    pk_columns = list(entity.pk_columns or [])
    if len(pk_columns) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "对象浏览 v0 仅支持单列主键实体"
                f"（当前主键列: {', '.join(pk_columns) or '未声明'}）"
            ),
        )
    result = _run_dataset_query(
        datasets_db,
        engine_factory,
        customer_id,
        dataset,
        filters=[{"column": pk_columns[0], "op": "eq", "value": key}],
        limit=2,
    )
    names, rows = _rows_to_dicts(result)
    if not rows:
        return {
            "entity_id": entity.id,
            "dataset_id": dataset.id,
            "found": False,
        }
    return {
        "entity_id": entity.id,
        "entity_name": entity.name,
        "dataset_id": dataset.id,
        "display_column": entity.display_column,
        "pk_columns": pk_columns,
        "found": True,
        "object": rows[0],
        "column_notes": dict(entity.column_notes or {}),
    }


@router.get("/{entity_id}/objects/{key}/relations/{rel_id}")
def list_entity_object_relations(
    entity_id: int,
    key: str,
    rel_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """沿关系遍历关联对象（声明方向；n:1 反查/1:n 正查/m:n 经中间集两段）。

    读边界：只执行关系声明所含查询（目标/中间数据集与声明列），不开放
    任意查询；limit 钳制沿对象列表。"""
    entity = _entity_or_404(db, entity_id, customer_id)
    from .models import EntityRelationship  # noqa: PLC0415

    rel = db.get(EntityRelationship, rel_id)
    if (
        rel is None
        or rel.customer_id != customer_id
        or (rel.from_entity_id != entity_id and rel.to_entity_id != entity_id)
    ):
        raise HTTPException(status_code=404, detail=f"关系 {rel_id} 不存在")

    # 遍历按**存储方向**分支（n:1: from=「多」侧携带外键、to=「一」侧主键）；
    # 响应 kind 按声明方向呈现（declared_view 同源）。
    on_from_side = rel.from_entity_id == entity.id
    other_entity_id = rel.to_entity_id if on_from_side else rel.from_entity_id
    view = service.declared_view(rel)
    other_entity = db.get(Entity, other_entity_id)
    if other_entity is None or other_entity.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"关系 {rel_id} 不存在")
    other_dataset = _browse_dataset_or_403(
        datasets_db, other_entity, customer_id, action="对象遍历"
    )

    if rel.kind == "m:n":
        if not on_from_side:
            # 声明 to 侧对象：对称遍历（junction_target == key → 源集 in）
            junction = datasets_db.get(Dataset, rel.junction_dataset_id)
            if junction is None:
                raise HTTPException(
                    status_code=404, detail="中间数据集不存在（drift）"
                )
            require_cell_tier(junction, "对象遍历")
            step1 = _run_dataset_query(
                datasets_db,
                engine_factory,
                customer_id,
                junction,
                filters=[
                    {
                        "column": rel.junction_target_column,
                        "op": "eq",
                        "value": key,
                    }
                ],
                limit=200,
            )
            _, jrows = _rows_to_dicts(step1)
            keys = [
                row.get(rel.junction_source_column)
                for row in jrows
                if row.get(rel.junction_source_column) is not None
            ][:200]
            source_entity = db.get(Entity, view["from_entity_id"])
            if source_entity is None or source_entity.customer_id != customer_id:
                raise HTTPException(status_code=404, detail=f"关系 {rel_id} 不存在")
            source_dataset = _browse_dataset_or_403(
                datasets_db, source_entity, customer_id, action="对象遍历"
            )
            if not keys:
                return _relations_payload(entity, rel, source_dataset, [], 0, page, page_size)
            result = _run_dataset_query(
                datasets_db,
                engine_factory,
                customer_id,
                source_dataset,
                filters=[
                    {
                        "column": view["from_column"],
                        "op": "in",
                        "value": keys,
                    }
                ],
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            names, rows = _rows_to_dicts(result)
            return _relations_payload(
                entity, rel, source_dataset, rows, result.get("total"), page, page_size
            )
        # 声明 from 侧对象：junction_source == key → 目标集 in
        junction = datasets_db.get(Dataset, rel.junction_dataset_id)
        if junction is None:
            raise HTTPException(status_code=404, detail="中间数据集不存在（drift）")
        require_cell_tier(junction, "对象遍历")
        step1 = _run_dataset_query(
            datasets_db,
            engine_factory,
            customer_id,
            junction,
            filters=[
                {"column": rel.junction_source_column, "op": "eq", "value": key}
            ],
            limit=200,
        )
        _, jrows = _rows_to_dicts(step1)
        keys = [
            row.get(rel.junction_target_column)
            for row in jrows
            if row.get(rel.junction_target_column) is not None
        ][:200]
        if not keys:
            return _relations_payload(entity, rel, other_dataset, [], 0, page, page_size)
        result = _run_dataset_query(
            datasets_db,
            engine_factory,
            customer_id,
            other_dataset,
            filters=[{"column": view["to_column"], "op": "in", "value": keys}],
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        names, rows = _rows_to_dicts(result)
        return _relations_payload(
            entity, rel, other_dataset, rows, result.get("total"), page, page_size
        )

    if on_from_side:
        # 对象在存储 from 侧（「多」侧）：n:1 正向——先取本对象行拿外键值，
        # 再查目标集 主键 == 外键值（1:n 反向声明落此分支=声明的 to 侧）。
        # 需先取本对象行拿源列值（源列可为任意列，不一定是主键）。
        own_dataset = _browse_dataset_or_403(
            datasets_db, entity, customer_id, action="对象遍历"
        )
        pk_columns = list(entity.pk_columns or [])
        if len(pk_columns) != 1:
            raise HTTPException(
                status_code=422,
                detail="对象遍历 v0 仅支持单列主键实体（组合键留 v1）",
            )
        own = _run_dataset_query(
            datasets_db,
            engine_factory,
            customer_id,
            own_dataset,
            filters=[{"column": pk_columns[0], "op": "eq", "value": key}],
            limit=2,
        )
        _, orows = _rows_to_dicts(own)
        if not orows:
            return _relations_payload(entity, rel, other_dataset, [], 0, page, page_size)
        link_value = orows[0].get(view["from_column"])
        if link_value is None:
            return _relations_payload(entity, rel, other_dataset, [], 0, page, page_size)
        filters = [{"column": view["to_column"], "op": "eq", "value": link_value}]
    else:
        # 对象在存储 to 侧（「一」侧）：反向遍历——源集 外键列 == 对象主键值
        #（1:n 声明落此分支=声明的 from 侧对象，direct 正查）
        filters = [{"column": rel.from_column, "op": "eq", "value": key}]
    result = _run_dataset_query(
        datasets_db,
        engine_factory,
        customer_id,
        other_dataset,
        filters=filters,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    names, rows = _rows_to_dicts(result)
    return _relations_payload(
        entity, rel, other_dataset, rows, result.get("total"), page, page_size
    )


def _relations_payload(
    entity: Entity,
    rel: Any,
    target_dataset: Dataset,
    rows: list[dict],
    total: int | None,
    page: int,
    page_size: int,
) -> dict:
    return {
        "entity_id": entity.id,
        "relationship_id": rel.id,
        "kind": rel.declared_kind or rel.kind,
        "dataset_id": target_dataset.id,
        "dataset_name": target_dataset.name,
        "objects": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
