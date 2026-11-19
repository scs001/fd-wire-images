"""entities 包：实体语义层（wire-entity-layer）+ 本体版本（wire-ontology-versioning）。

对外暴露 ``router``（/api/v1/entities 全部端点）与 ``ontology_router``
（/api/v1/ontology 版本面：版本列表/打发布版/diff/导出/回滚/完整性自检）
与 ``init_db``（本体两表 + 版本两表幂等建表兜底，生产走 alembic
0026/0049）。图谱紧凑文本块（``service.entity_graph_prompt_block``）
是 AI 构建上下文注入的同源（ai/routes 组装 system prompt 时消费）。
"""

from .db import (
    EntitiesBase,
    get_db,
    get_engine,
    get_session,
    init_db,
    reset_entities_runtime,
)
from .internal import router as entities_internal_router
from .models import Entity, EntityRelationship, OntologyEvent, OntologySnapshot
from .routes import ontology_router, router
from .service import (
    EntityConflict,
    EntityNotFound,
    InvalidEntity,
    entity_graph,
    entity_graph_prompt_block,
)
from .snapshots import start_snapshot_scheduler, stop_snapshot_scheduler

__all__ = [
    "EntitiesBase",
    "Entity",
    "EntityConflict",
    "EntityNotFound",
    "EntityRelationship",
    "InvalidEntity",
    "OntologyEvent",
    "OntologySnapshot",
    "entities_internal_router",
    "entity_graph",
    "entity_graph_prompt_block",
    "get_db",
    "get_engine",
    "get_session",
    "init_db",
    "ontology_router",
    "reset_entities_runtime",
    "router",
    "start_snapshot_scheduler",
    "stop_snapshot_scheduler",
]
