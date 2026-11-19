"""fd_mcp_client：平台服务调用 MCP 端点的**唯一**客户端包。

native 单模式契约（spec「统一客户端 native 单模式」，单测钉住）：标准
MCP streamable-http，initialize 握手 + 会话复用 + ``tools/call`` +
``list_tools``。gateway 简化模式（``{"tool","arguments"}`` call-only
POST）已随 registry 定版镜像退役：新 registry 对请求体透传不翻译
（fastmcp 原生拒收简化报文）、外部 ``/mcp-proxy/`` 路径前缀已删除——
保留死模式即保留误用面，故整类移除。

计量轨道不变量（spec「计量轨道不变量（端点决定）」）：计量归属由
**端点**决定，不再由报文形态决定——

- **registry 入口端点**（``https://<registry>/<server>/mcp`` 形态）+
  平台服务凭据 = 计量轨：对 cell 的客户数据读写（datasets ingest、
  AI 探索）一律走此轨，registry 审计侧照常产生计量记录，归集平台
  轨道、不落客户账单；
- **容器直连地址**（如 ``http://fd-wire-cell-x:8000/mcp``）= 绕计量
  门：仅限开发与测试，SHALL NOT 用于对 cell 的客户数据读取；
- 两种用途 SHALL NOT 混用身份凭据。包不强制（无 auth 边界可依），
  由代码评审与调用方部署纪律保证。

纯包约束：endpoint / credential / HTTP 客户端全部**构造注入**，本包
不读任何 env——env 解析（``AI_CELL_ENDPOINT_MAP`` /
``AI_CELL_ENDPOINT_BASE`` / ``AI_PLATFORM_CREDENTIAL``，语义归本包
所有）留在调用方。

错误体系（:mod:`fd_mcp_client.errors`）：

- :class:`McpTransportError`：端点不可达 / 非 2xx / 非 JSON / 握手失败；
- :class:`McpCallError`：JSON-RPC error / 工具报错 / 响应不合契约。

调用方包装成自己的领域错误（``EngineUnavailable``/``EngineCallError``、
``CellNotProvisionedError``/``CellToolError``），fail-closed 路由语义
留在调用方。
"""

from __future__ import annotations

from ._result import unwrap_tool_result
from .errors import McpCallError, McpTransportError
from .native import (
    MCP_PROTOCOL_VERSIONS,
    SESSION_HEADER,
    NativeMcpClient,
    normalize_mcp_endpoint,
)

__all__ = [
    "MCP_PROTOCOL_VERSIONS",
    "SESSION_HEADER",
    "McpCallError",
    "McpTransportError",
    "NativeMcpClient",
    "normalize_mcp_endpoint",
    "unwrap_tool_result",
]
