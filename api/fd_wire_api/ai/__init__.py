"""ai 包：控制台 AI 对话构建循环的服务端（wire-platform-v1 任务 5.4）。

对外暴露 ``router``（挂到 app 即得全部端点）、幂等建表 ``init_db`` 与
构建循环的两个核心协议（``LlmClient`` / ``ToolExecutor``）。

**导出是惰性的（PEP 562）**：这个包曾被别的模块当"顺手一引"的目标——
``datasets/ingest.py`` 只用 ``..ai.executor`` 里的一个函数，却因为包 ``__init__``
急切 ``from .routes import router``，把 ``ai.routes → entities → data_plane →
datasets.ingest`` 整条链拖进来，于是任何"先导入 datasets.ingest 或 entities"的
冷启动顺序都撞成 ``ImportError: cannot import name … from partially initialized
module``（2026-11-13 在线上 pod、`7b2dfc3` 与 main 三处复现）。

改成按需导入后，`from fd_wire_api.ai import router` 这类写法照旧可用（首次访问时
才真正导入），但包本身在 import 期不再牵动任何子模块，环就断了。
"""

from __future__ import annotations

import importlib
from typing import Any

#: 名字 → 拥有它的子模块。`__all__` 由它派生，新增导出只改这一张表。
_EXPORTS: dict[str, str] = {
    "AIConversation": ".models",
    "AIDraftArtifact": ".models",
    "AIMessage": ".models",
    "ARTIFACT_TOOL_KINDS": ".tools",
    "AiBase": ".db",
    "CELL_NOT_PROVISIONED_MESSAGE": ".executor",
    "CellMcpExecutor": ".executor",
    "CellNotProvisionedError": ".executor",
    "CellToolError": ".executor",
    "LlmAssistantMessage": ".protocols",
    "LlmClient": ".protocols",
    "LlmGatewayError": ".llm",
    "MessageRole": ".models",
    "TOOL_SCHEMAS": ".tools",
    "TokenGatewayLlmClient": ".llm",
    "ToolCall": ".protocols",
    "ToolExecutor": ".protocols",
    "append_message": ".loop",
    "execute_tool_call": ".loop",
    "get_db": ".db",
    "get_engine": ".db",
    "get_session": ".db",
    "init_db": ".db",
    "reset_ai_runtime": ".db",
    "resolve_cell_endpoint": ".executor",
    "router": ".routes",
    "run_build_loop": ".loop",
    "stage_artifact": ".tools",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name, __name__), name)
    globals()[name] = value  # 缓存：第二次访问走普通属性查找
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
