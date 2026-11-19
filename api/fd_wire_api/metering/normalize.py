"""规范化层：registry 审计事件（MCPServerAccessRecord 形状）→ 平台计量字段。

registry/audit/models.py 的 MCPServerAccessRecord 关键字段：

- ``timestamp`` / ``request_id`` / ``correlation_id``
- ``identity``（username、principal_name 等）
- ``mcp_server``（name、path、proxy_target）
- ``mcp_request``（method、tool_name、resource_uri）
- ``mcp_response``（status、duration_ms、error_code…）
- ``request``（client_ip 等，可缺）

rows_returned 戳由 business-mcp 在工具响应里盖，registry 透传时可能落在
多个位置，本层按候选路径逐个提取；提取不到即分类为缺戳异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .db import to_utc
from .settings import ALLOWED_SOURCES, get_settings

#: 缺戳异常分类
REASON_MISSING_ROWS = "missing_rows_returned"
#: 结构不可解析（缺 request_id/timestamp/server 等必备件）
REASON_UNPARSABLE = "unparseable_event"

#: rows_returned 候选提取路径（按优先级；registry 版本演进时在此追加）
_ROWS_PATHS: tuple[tuple[str, ...], ...] = (
    ("rows_returned",),
    ("mcp_response", "rows_returned"),
    ("mcp_response", "meta", "rows_returned"),
    ("mcp_response", "result", "rows_returned"),
    ("mcp_response", "result", "meta", "rows_returned"),
)


def extract_rows_returned(event: dict[str, Any]) -> int | None:
    """从原始事件提取 rows_returned；不存在或非非负整数返回 None。"""
    for path in _ROWS_PATHS:
        node: Any = event
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if node is None:
            continue
        try:
            value = int(node)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


# ---------------------------------------------------------------------------
# 宽松输入模型：未知字段放行（audit schema 演进不炸摄取）
# ---------------------------------------------------------------------------


class _IdentityIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    username: str = "anonymous"
    principal_name: str | None = None
    auth_method: str | None = None


class _McpServerIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    path: str | None = None


class _McpRequestIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    method: str | None = None
    tool_name: str | None = None


class AuditEventIn(BaseModel):
    """MCPServerAccessRecord 的宽松投影；规范化只依赖这些字段。"""

    model_config = ConfigDict(extra="allow")

    timestamp: datetime | None = None
    request_id: str | None = None
    identity: _IdentityIn | None = None
    mcp_server: _McpServerIn | None = None
    mcp_request: _McpRequestIn | None = None
    mcp_response: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    log_type: str | None = Field(default=None)


@dataclass(slots=True)
class NormalizedRecord:
    """可入 ``metering_records`` 的一条规范化计量记录。"""

    event_timestamp: datetime
    request_id: str
    caller: str
    caller_principal: str | None
    credential_track: str  # platform | customer
    server_name: str
    server_path: str | None
    tool_name: str | None
    rows_returned: int
    source: str


@dataclass(slots=True)
class SkippedEvent:
    """与计量无关的审计事件（如 registry_api_access/token_mint）：静默跳过，只计数。"""

    log_type: str | None


@dataclass(slots=True)
class NormalizedAnomaly:
    """不可计量的事件：留痕到 ``metering_anomalies``。"""

    reason: str
    request_id: str | None
    server_name: str | None
    tool_name: str | None
    caller: str | None
    credential_track: str | None
    detail: dict[str, Any] = field(default_factory=dict)
    event_timestamp: datetime | None = None


def resolve_credential_track(caller: str, platform_identities: frozenset[str]) -> str:
    """命中平台服务身份名单 → platform，否则 customer。"""
    return "platform" if caller.lower() in platform_identities else "customer"


def _match_identity_source(caller: str, identity_source_map: dict[str, str]) -> str | None:
    """调用身份 → 来源：精确优先（大小写不敏感），其次 ``前缀*`` 通配。

    ``identity_source_map`` 的键经 ``settings.identity_source_map()`` 已统一
    小写，此处把 caller 一并小写后匹配。
    """
    lowered = caller.lower()
    if lowered in identity_source_map:
        return identity_source_map[lowered]
    for pattern, source in identity_source_map.items():
        if pattern.endswith("*") and lowered.startswith(pattern[:-1]):
            return source
    return None


def resolve_source(
    credential_track: str,
    caller: str,
    server_name: str,
    source_map: dict[str, str],
    identity_source_map: dict[str, str],
) -> str:
    """来源标记：platform 轨先按调用身份查 ``PLATFORM_IDENTITY_SOURCE_MAP``
    （精确/``前缀*`` 通配，区分壹座/萬星/平台内部发起），未命中回退
    ``platform-internal``；customer 轨按 server 名查 ``METERING_SOURCE_MAP``
    （精确优先，其次 ``前缀*`` 通配）；其余 customer。"""
    if credential_track == "platform":
        hit = _match_identity_source(caller, identity_source_map)
        return hit if hit is not None else "platform-internal"
    if server_name in source_map:
        return source_map[server_name]
    for pattern, source in source_map.items():
        if pattern.endswith("*") and server_name.startswith(pattern[:-1]):
            return source
    return "customer"


def normalize_event(
    raw: dict[str, Any],
) -> NormalizedRecord | NormalizedAnomaly | SkippedEvent:
    """单条审计事件 → 计量记录 / 异常留痕 / 跳过（两条摄取入口共用的唯一入口）。"""
    settings = get_settings()
    # 仅消费 MCP 工具调用审计流；其余审计类型（registry_api_access、
    # token_mint…）与逐调用计量无关，跳过不落任何表。
    log_type = raw.get("log_type") if isinstance(raw, dict) else None
    if log_type is not None and log_type != "mcp_server_access":
        return SkippedEvent(log_type=str(log_type))
    try:
        event = AuditEventIn.model_validate(raw)
    except Exception as exc:  # 结构彻底不合法 → 异常留痕而非丢弃
        return NormalizedAnomaly(
            reason=REASON_UNPARSABLE,
            request_id=str(raw.get("request_id")) if isinstance(raw, dict) else None,
            server_name=None,
            tool_name=None,
            caller=None,
            credential_track=None,
            detail={"error": f"schema validation failed: {exc}"},
        )

    request_id = (event.request_id or "").strip()
    server_name = (event.mcp_server.name if event.mcp_server else "") or ""
    timestamp = to_utc(event.timestamp) if event.timestamp else None

    # 必备件缺失：无法定位幂等键/时间，无法计量
    if not request_id or not server_name or timestamp is None:
        return NormalizedAnomaly(
            reason=REASON_UNPARSABLE,
            request_id=request_id or None,
            server_name=server_name or None,
            tool_name=event.mcp_request.tool_name if event.mcp_request else None,
            caller=event.identity.username if event.identity else None,
            credential_track=None,
            detail={
                "missing": [
                    name
                    for name, value in (
                        ("request_id", request_id),
                        ("mcp_server.name", server_name),
                        ("timestamp", timestamp),
                    )
                    if not value
                ]
            },
            event_timestamp=timestamp,
        )

    caller = event.identity.username if event.identity else "anonymous"
    track = resolve_credential_track(caller, settings.platform_identity_set())
    tool_name = event.mcp_request.tool_name if event.mcp_request else None
    rows = extract_rows_returned(raw)

    if rows is None:
        # 缺戳：不进计量，异常留痕（wire-facade-metering「缺戳告警」）
        response = event.mcp_response or {}
        return NormalizedAnomaly(
            reason=REASON_MISSING_ROWS,
            request_id=request_id,
            server_name=server_name,
            tool_name=tool_name,
            caller=caller,
            credential_track=track,
            detail={
                "mcp_response_status": response.get("status"),
                "mcp_response_error_code": response.get("error_code"),
                "method": event.mcp_request.method if event.mcp_request else None,
                "searched_paths": [".".join(p) for p in _ROWS_PATHS],
            },
            event_timestamp=timestamp,
        )

    source = resolve_source(
        track, caller, server_name, settings.source_map(), settings.identity_source_map()
    )
    assert source in ALLOWED_SOURCES  # settings 校验保证映射值合法
    return NormalizedRecord(
        event_timestamp=timestamp,
        request_id=request_id,
        caller=caller,
        caller_principal=event.identity.principal_name if event.identity else None,
        credential_track=track,
        server_name=server_name,
        server_path=event.mcp_server.path if event.mcp_server else None,
        tool_name=tool_name,
        rows_returned=rows,
        source=source,
    )
