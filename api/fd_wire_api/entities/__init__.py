"""entities 包：实体语义层（wire-entity-layer）。

对外暴露 ``router``（挂到 app 即得 /api/v1/entities 全部端点）与
``init_db``（entities / entity_relationships 幂等建表兜底，生产走
alembic 0026）。图谱紧凑文本块（``service.entity_graph_prompt_block``）
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
from .models import Entity, EntityRelationship
from .routes import router
from .service import (
    EntityConflict,
    EntityNotFound,
    InvalidEntity,
    entity_graph,
    entity_graph_prompt_block,
)

__all__ = [
    "EntitiesBase",
    "Entity",
    "EntityConflict",
    "EntityNotFound",
    "EntityRelationship",
    "InvalidEntity",
    "entity_graph",
    "entity_graph_prompt_block",
    "get_db",
    "get_engine",
    "get_session",
    "init_db",
    "reset_entities_runtime",
    "router",
]
