"""native 模式客户端：标准 MCP streamable-http（完整握手 + 会话管理）。

线协议（从 ``fd_wire_api/catalog/client.py`` 的会话逻辑提炼，D8）：

1. POST ``initialize``（JSON-RPC 2.0，协议版本按序试探）→ 200，JSON 或
   SSE 体携带 result，响应头 ``Mcp-Session-Id`` 是后续请求的会话凭证；
2. POST ``notifications/initialized``（服务端 202 无体，带会话头）；
3. 之后每个请求都带 ``Mcp-Session-Id`` 头；``tools/call`` 与 ``tools/list``
   均可用；请求头 ``Accept: application/json, text/event-stream``（与既有
   catalog client 一致，streamable-http 服务端可二选一应答）。

- **惰性握手**：首次 ``call`` / ``list_tools`` 才握手；会话在整个客户端
  生命周期内复用（一次握手，多次调用）；``close()`` 尽力 DELETE 会话并
  释放自建的 HTTP 客户端。
- 凭据：``credential`` 非空即随每个请求上行 Bearer，空 = 匿名（测试/匿名
  端点友好）；凭据全部构造注入，包**不读任何 env**。
- 端点归一：自动补齐 ``/mcp`` 后缀（已带则不重复），与 catalog client
  同一纪律。

计量轨道不变量（spec「计量轨道不变量（端点决定）」）：计量归属由端点
决定——endpoint 配成 **registry 入口**（``https://<registry>/<server>/mcp``
形态）即计量轨，registry 审计侧照常落计量记录、归集平台凭据轨道；
endpoint 配成**容器直连地址**（如 ``http://fd-wire-cell-x:8000/mcp``）
则不经计量门、不产生计量记录——仅限开发与测试，SHALL NOT 用于对
cell 的客户数据读取。包不强制（无 auth 边界可依），由代码评审与
调用方部署纪律保证。

错误：传输层失败（不可达 / 非 2xx / 非 JSON / 握手失败）→
:class:`McpTransportError`；JSON-RPC error / 工具报错 / 结果不合契约 →
:class:`McpCallError`。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ._result import unwrap_tool_result
from .errors import McpCallError, McpTransportError

#: 协议版本按新到旧试探（400 + protocol 字样 → 试下一档）
MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: streamable-http 会话凭证响应/请求头
SESSION_HEADER = "Mcp-Session-Id"


def normalize_mcp_endpoint(base_url: str) -> str:
    """归一基地址为 streamable-http 端点：确保以 ``/mcp`` 结尾。"""
    trimmed = (base_url or "").rstrip("/")
    if trimmed.endswith("/mcp"):
        return trimmed
    return f"{trimmed}/mcp"


def _decode_rpc_payload(resp: httpx.Response, label: str) -> dict[str, Any]:
    """解码 JSON-RPC 响应体：streamable-http 以 SSE（``data:`` 行）或纯 JSON 应答。"""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and ("result" in payload or "error" in payload):
                    return payload
        raise McpTransportError(f"{label}返回非 JSON（SSE 无 JSON-RPC 消息）")
    try:
        payload: Any = resp.json()
    except ValueError as exc:
        raise McpTransportError(f"{label}返回非 JSON") from exc
    if not isinstance(payload, dict):
        raise McpTransportError(f"{label}返回非 JSON（需 JSON-RPC 对象）")
    return payload


def _raise_on_rpc_error(payload: dict[str, Any], context: str) -> None:
    error = payload.get("error")
    if isinstance(error, dict):
        raise McpCallError(
            f"返回 JSON-RPC 错误: {error.get('code')} {error.get('message')}"
        )
    if error:
        raise McpCallError(f"返回 JSON-RPC 错误: {error}")


class NativeMcpClient:
    """MCP streamable-http 客户端（initialize 握手 + tools/call + list_tools）。

    ``http_client`` 缺省时自建 :class:`httpx.Client` 并持有（``close()``
    负责释放）；注入时不持有、不关闭（所有权归注入方）。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        credential: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
        protocol_versions: tuple[str, ...] = MCP_PROTOCOL_VERSIONS,
        client_name: str = "fd-wire-api",
        client_version: str = "1.0",
        label: str = "MCP 端点",
    ) -> None:
        self._endpoint = normalize_mcp_endpoint(endpoint)
        self._credential = (credential or "").strip()
        self._label = label
        self._protocol_versions = protocol_versions
        self._client_info = {"name": client_name, "version": client_version}
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._session_id: str | None = None
        self._rpc_id = 0

    # ── 线协议 plumbing ─────────────────────────────────────────────────────

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def session_id(self) -> str | None:
        """当前 MCP 会话 id（握手前为 None；测试断言会话复用用）。"""
        return self._session_id

    def _post(self, payload: dict[str, Any], *, with_session: bool = False) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._credential:
            headers["Authorization"] = f"Bearer {self._credential}"
        if with_session and self._session_id:
            headers[SESSION_HEADER] = self._session_id
        try:
            return self._http.post(self._endpoint, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise McpTransportError(f"{self._label}不可达: {exc}") from exc

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _ensure_session(self) -> str:
        """惰性 initialize 握手：返回会话 id（已握过即复用）。"""
        if self._session_id:
            return self._session_id
        for version in self._protocol_versions:
            resp = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": version,
                        "capabilities": {},
                        "clientInfo": self._client_info,
                    },
                }
            )
            if resp.status_code == 400 and "protocol" in resp.text.lower():
                continue  # 服务端不支持该协议版本 → 试下一档
            if resp.status_code >= 400:
                raise McpTransportError(
                    f"{self._label} initialize 返回 {resp.status_code}: {resp.text[:200]}"
                )
            payload = _decode_rpc_payload(resp, self._label)
            _raise_on_rpc_error(payload, "initialize")
            session_id = resp.headers.get(SESSION_HEADER)
            if not session_id:
                raise McpTransportError(
                    f"{self._label} initialize 未返回 {SESSION_HEADER} 响应头"
                )
            self._session_id = session_id
            # initialized 通知（无 id；服务端 202 无体，带会话头）
            note = self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                with_session=True,
            )
            if note.status_code >= 400:
                self._session_id = None  # 会话未建立成功，不留半状态
                raise McpTransportError(
                    f"{self._label} initialized 通知被拒: {note.status_code}"
                )
            return session_id
        raise McpTransportError(f"{self._label}拒绝了所有已知协议版本")

    # ── 公开操作 ─────────────────────────────────────────────────────────────

    def call(self, tool: str, arguments: dict) -> dict:
        """``tools/call`` 一次，返回工具结果 dict（解包契约见包 docstring）。"""
        session_id = self._ensure_session()
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            with_session=True,
        )
        if resp.status_code >= 400:
            raise McpTransportError(
                f"{self._label} tools/call 返回 {resp.status_code}: {resp.text[:200]}"
            )
        payload = _decode_rpc_payload(resp, self._label)
        _raise_on_rpc_error(payload, f"tools/call {tool}")
        return unwrap_tool_result(payload)

    def list_tools(self) -> list[dict]:
        """``tools/list``，返回工具描述 dict 列表（gateway 模式不做发现类操作）。"""
        session_id = self._ensure_session()
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {},
            },
            with_session=True,
        )
        if resp.status_code >= 400:
            raise McpTransportError(
                f"{self._label} tools/list 返回 {resp.status_code}: {resp.text[:200]}"
            )
        payload = _decode_rpc_payload(resp, self._label)
        _raise_on_rpc_error(payload, "tools/list")
        result = payload.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
            raise McpCallError("tools/list 结果不合契约（需 tools 数组）")
        return tools

    def close(self) -> None:
        """尽力 DELETE MCP 会话 + 释放自建 HTTP 客户端（幂等，清理失败不抛）。"""
        session_id = self._session_id
        if session_id:
            self._session_id = None
            try:
                headers = {SESSION_HEADER: session_id}
                if self._credential:
                    headers["Authorization"] = f"Bearer {self._credential}"
                self._http.delete(self._endpoint, headers=headers)
            except Exception:  # pragma: no cover - 清理失败可忽略
                pass
        if self._owns_http:
            self._http.close()


__all__ = ["MCP_PROTOCOL_VERSIONS", "SESSION_HEADER", "NativeMcpClient", "normalize_mcp_endpoint"]
