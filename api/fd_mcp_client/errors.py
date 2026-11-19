"""fd_mcp_client 的两级错误体系（契约钉住，见包 docstring）。

- :class:`McpTransportError`：**传输层**失败——端点不可达、非 2xx、响应非
  JSON（含 SSE 里解不出 JSON-RPC 消息）、握手失败。属「端点/网络」问题。
- :class:`McpCallError`：**调用层**失败——JSON-RPC error 对象、工具自身
  报错（isError / success=False）、响应不合 MCP 结果契约。属「工具/协议」
  问题。

调用方把这两级包装成自己的领域错误（如 datasets 的 ``EngineCallError``、
ai 的 ``CellToolError``），fail-closed 路由行为留在调用方，包不掺路由语义。
"""

from __future__ import annotations


class McpTransportError(RuntimeError):
    """MCP 端点不可达 / 非 2xx / 非 JSON / 握手失败（传输层）。"""


class McpCallError(RuntimeError):
    """工具报错或响应不合契约（JSON-RPC error / isError / 契约形态不符）。"""


__all__ = ["McpCallError", "McpTransportError"]
