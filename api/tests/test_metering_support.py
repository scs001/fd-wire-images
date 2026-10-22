"""计量管道测试的共享夹具与事件工厂（仅测试辅助，不含断言）。

事件形状对齐 registry/audit/models.py 的 MCPServerAccessRecord。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from fd_wire_api.app import app
from fd_wire_api.metering.db import ensure_schema, get_engine, reset_metering_runtime

#: 测试用 ingest 令牌
TOKEN = "test-ingest-token"

#: 平台服务身份名单（大小写不敏感）
PLATFORM_IDENTITIES = "svc-analysis,svc-vaas"

#: 来源映射：精确 + 前缀通配
SOURCE_MAP = "business-mcp=yizuo,wanxing-*=wanxing"


def make_event(
    request_id: str = "req-1",
    username: str = "alice@customer.com",
    server: str = "business-mcp",
    tool: str = "read_range",
    rows: int | None = 120,
    ts: str = "2026-10-01T10:00:00+00:00",
    rows_location: str = "response",
    log_type: str = "mcp_server_access",
    response_status: str = "success",
) -> dict[str, Any]:
    """构造一条 MCPServerAccessRecord 形状的审计事件。

    ``rows_location`` 控制 rows_returned 戳的位置：
    ``response``（mcp_response.rows_returned）/ ``top``（事件顶层）/
    ``meta``（mcp_response.meta）/ ``result``（mcp_response.result）/
    ``missing``（不带戳 → 异常路径）。
    """
    event: dict[str, Any] = {
        "timestamp": ts,
        "log_type": log_type,
        "version": "1.0",
        "request_id": request_id,
        "correlation_id": None,
        "identity": {
            "username": username,
            "auth_method": "oauth2",
            "provider": "cognito",
            "groups": [],
            "scopes": ["mcp:call"],
            "is_admin": False,
            "credential_type": "bearer_token",
            "credential_hint": "***",
            "principal_name": username,
        },
        "mcp_server": {
            "name": server,
            "path": f"/servers/{server}",
            "version": "1.0",
            "proxy_target": f"http://{server}.internal/mcp",
        },
        "mcp_request": {
            "method": "tools/call",
            "tool_name": tool,
            "resource_uri": None,
            "mcp_session_id": None,
            "transport": "streamable-http",
            "jsonrpc_id": "42",
        },
        "mcp_response": {
            "status": response_status,
            "duration_ms": 42.0,
            "error_code": None,
            "error_message": None,
        },
        "request": {
            "method": "POST",
            "path": "/mcp",
            "query_params": {},
            "client_ip": "203.0.113.9",
            "forwarded_for": None,
            "user_agent": "pytest",
            "content_length": 128,
        },
    }
    if rows_location == "missing" or rows is None:
        return event
    if rows_location == "top":
        event["rows_returned"] = rows
    elif rows_location == "meta":
        event["mcp_response"]["meta"] = {"rows_returned": rows}
    elif rows_location == "result":
        event["mcp_response"]["result"] = {"rows_returned": rows}
    else:  # response（默认）
        event["mcp_response"]["rows_returned"] = rows
    return event


def auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def metering_env(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, **overrides: str | None
) -> Iterator[TestClient]:
    """按 env 指向临时 sqlite 库并建表，产出 TestClient；退出时清理缓存。"""
    env: dict[str, str | None] = {
        "DATABASE_URL": f"sqlite:///{tmp_path}/metering.db",
        "METERING_INGEST_TOKEN": TOKEN,
        "PLATFORM_IDENTITIES": PLATFORM_IDENTITIES,
        "PLATFORM_IDENTITY_SOURCE_MAP": None,  # 默认未配置身份→来源映射
        "METERING_SOURCE_MAP": SOURCE_MAP,
        "REGISTRY_MONGO_URI": None,  # 默认未配置直读路径
        "REGISTRY_MONGO_DB": None,
    }
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    reset_metering_runtime()
    ensure_schema()
    yield TestClient(app)
    get_engine().dispose()
    reset_metering_runtime()


@pytest.fixture
def metering_client(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """默认配置的计量客户端：临时库 + ingest 令牌 + 平台名单 + 来源映射。"""
    with metering_env(tmp_path, monkeypatch) as client:
        client.headers.update(auth_headers())
        yield client


def db_row_count(table: str) -> int:
    """当前计量库里某表的行数（测试断言用）。"""
    from sqlalchemy import text

    from fd_wire_api.metering.db import get_session

    with get_session() as session:
        return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
