"""delivery 包：客户私有 scope 交付的对外客户端面（wire-platform-v1 任务 6.6）。

对外只经显式 HTTP 契约访问 fd-open-data-mcp 的指标 scope 能力：
- :class:`ScopeClient`——可注入的客户端协议（测试用 mock 实现）；
- :class:`HttpScopeClient`——真实 HTTP 门面（env ``SCOPE_API_BASE`` /
  ``SCOPE_API_TOKEN``，httpx 客户端可注入）；
- :func:`scope_client_dep`——FastAPI 依赖（经 ``app.dependency_overrides``
  可整体替换，便于测试）。
"""

from .scope_client import (
    HttpScopeClient,
    ScopeClient,
    ScopeClientError,
    get_scope_client,
    reset_scope_client_factory,
    scope_client_dep,
    set_scope_client_factory,
)

__all__ = [
    "HttpScopeClient",
    "ScopeClient",
    "ScopeClientError",
    "get_scope_client",
    "reset_scope_client_factory",
    "scope_client_dep",
    "set_scope_client_factory",
]
