"""artifacts 包：产物生命周期服务端（wire-artifacts-persistence）。

对外暴露 ``router``（挂到 app 即得 ``/api/v1/artifacts`` 全部端点）。
持久层与 ai 切片同引擎（``AiBase`` / ``AI_DATABASE_URL``——产物是
``ai_draft_artifacts`` 的转正去向，``origin_conversation_id`` 逻辑引用
``ai_conversations``，建表走 ai 切片的 create_all 兜底 + alembic 0021）。
"""

from .models import ARTIFACT_KINDS, ArtifactStatus, Artifact, ArtifactVersion
from .routes import router

__all__ = [
    "ARTIFACT_KINDS",
    "Artifact",
    "ArtifactStatus",
    "ArtifactVersion",
    "router",
]
