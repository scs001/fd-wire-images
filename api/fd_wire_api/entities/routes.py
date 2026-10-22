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

from fastapi import APIRouter, Body, Depends, HTTPException, status
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
from .db import get_db as get_entities_db
from .models import Entity
from .service import dataset_summary

router = APIRouter(prefix="/api/v1/entities", tags=["entities"])


# ------------------------------------------------------------------ schemas


class EntityCreateIn(BaseModel):
    """声明/更新实体请求体（PUT 为整量替换，重走同一条校验链）。"""

    name: str
    dataset_id: str = Field(min_length=1)
    pk_columns: list[str]
    description: str | None = None


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
    dataset: DatasetSummaryOut
    created_at: datetime
    updated_at: datetime


class RelationshipCreateIn(BaseModel):
    """声明关系请求体：目标实体 + 源列 + 目标主键列 + 基数。"""

    to_entity_id: int
    from_column: str = Field(min_length=1)
    to_column: str = Field(min_length=1)
    kind: str
    label: str | None = None


class RelationshipOut(BaseModel):
    """关系卡片（图谱 edges 同形）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    from_entity_id: int
    from_column: str
    to_entity_id: int
    to_column: str
    kind: str
    label: str | None = None
    created_at: datetime


class GraphNodeOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    pk_columns: list[str]
    dataset: DatasetSummaryOut


class GraphEdgeOut(BaseModel):
    id: int
    from_entity_id: int
    from_column: str
    to_entity_id: int
    to_column: str
    kind: str
    label: str | None = None


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
) -> dict:
    """删实体：同事务级联删两端关系（响应带 deleted_relationships 计数留痕）。"""
    entity = _entity_or_404(db, entity_id, customer_id)
    removed = service.delete_entity(db, entity)
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
        )
    except service.EntityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.EntityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidEntity as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RelationshipOut.model_validate(relationship)


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
