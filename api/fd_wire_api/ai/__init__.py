"""ai 包：控制台 AI 对话构建循环的服务端（wire-platform-v1 任务 5.4）。

对外暴露 ``router``（挂到 app 即得全部端点）、幂等建表 ``init_db`` 与
构建循环的两个核心协议（``LlmClient`` / ``ToolExecutor``）。
"""

from .db import AiBase, get_db, get_engine, get_session, init_db, reset_ai_runtime
from .executor import (
    CELL_NOT_PROVISIONED_MESSAGE,
    CellMcpExecutor,
    CellNotProvisionedError,
    CellToolError,
    resolve_cell_endpoint,
)
from .llm import LlmGatewayError, TokenGatewayLlmClient
from .loop import append_message, execute_tool_call, run_build_loop
from .models import AIDraftArtifact, AIMessage, AIConversation, MessageRole
from .protocols import LlmAssistantMessage, LlmClient, ToolCall, ToolExecutor
from .routes import router
from .tools import ARTIFACT_TOOL_KINDS, TOOL_SCHEMAS, stage_artifact

__all__ = [
    "ARTIFACT_TOOL_KINDS",
    "AIDraftArtifact",
    "AIConversation",
    "AIMessage",
    "AiBase",
    "CELL_NOT_PROVISIONED_MESSAGE",
    "CellMcpExecutor",
    "CellNotProvisionedError",
    "CellToolError",
    "LlmAssistantMessage",
    "LlmClient",
    "LlmGatewayError",
    "MessageRole",
    "TOOL_SCHEMAS",
    "TokenGatewayLlmClient",
    "ToolCall",
    "ToolExecutor",
    "append_message",
    "execute_tool_call",
    "get_db",
    "get_engine",
    "get_session",
    "init_db",
    "reset_ai_runtime",
    "resolve_cell_endpoint",
    "router",
    "run_build_loop",
    "stage_artifact",
]
