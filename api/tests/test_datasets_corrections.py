"""修正 CRUD / 行预览 / 删除（任务 2.6）与引擎通道报文形态。

覆盖：
- 修正三件套（改值/删行/加行）→ 引擎 ``correction_add`` 参数逐项断言
  （行锚 = 预览行 ``__row_id``；actor 归属；云端不写回客户源）；
- 修正历史透传（含悬空/已回滚）与单条回滚；
- 行预览分页透传（limit/offset）；
- 删除调引擎 ``delete``；引擎不可达 → 502 且登记行不删（fail-closed）；
- 跨企业 404；
- 真实 ``EngineToolClient``：经 fd_mcp_client gateway 模式的简化报文
  （``{"tool","arguments"}`` + 平台服务凭据头，httpx.MockTransport），
  连接器 key 绝不进入该通道。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import httpx
import pytest

from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.ingest import (
    TOOL_CORRECTION_ADD,
    TOOL_CORRECTION_LIST,
    TOOL_CORRECTION_REVERT,
    TOOL_DELETE,
    TOOL_PREVIEW,
    EngineCallError,
    EngineToolClient,
)
from fd_wire_api.datasets.models import Dataset
from test_datasets_common import (
    CUSTOMER_A,
    CUSTOMER_B,
    EngineUnavailable,
    datasets_env,  # noqa: F401 - pytest fixture
    make_uploaded_dataset,
)


def _dataset(dataset_id: str) -> Dataset:
    session = get_session()
    try:
        return session.get(Dataset, dataset_id)
    finally:
        session.close()


# ------------------------------------------------------------------ 修正三件套


def test_correction_update_delete_insert_passthrough(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env)
    url = f"/api/v1/datasets/{dataset['id']}/corrections"
    update = datasets_env.client.post(
        url,
        headers=CUSTOMER_A,
        json={"op": "update", "row_id": "pk:1", "column": "name", "value": "fixed"},
    )
    assert update.status_code == 201, update.text
    assert update.json() == {"correction_id": 7}
    delete = datasets_env.client.post(
        url, headers=CUSTOMER_A, json={"op": "delete", "row_id": "pk:2"}
    )
    assert delete.status_code == 201
    insert = datasets_env.client.post(
        url,
        headers=CUSTOMER_A,
        json={"op": "insert", "values": {"id": 4, "name": "d"}},
    )
    assert insert.status_code == 201

    calls = datasets_env.fake.calls_for(TOOL_CORRECTION_ADD)
    assert calls[0] == {
        "dataset_key": dataset["key"],
        "op": "update",
        "anchor": "pk:1",
        "values": {"name": "fixed"},
        "actor": "customer:cust-001",
    }
    assert calls[1] == {
        "dataset_key": dataset["key"],
        "op": "delete",
        "anchor": "pk:2",
        "values": {},
        "actor": "customer:cust-001",
    }
    assert calls[2] == {
        "dataset_key": dataset["key"],
        "op": "insert",
        "anchor": None,
        "values": {"id": 4, "name": "d"},
        "actor": "customer:cust-001",
    }


def test_correction_update_allows_explicit_null_value(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env)
    resp = datasets_env.client.post(
        f"/api/v1/datasets/{dataset['id']}/corrections",
        headers=CUSTOMER_A,
        json={"op": "update", "row_id": "pk:1", "column": "name", "value": None},
    )
    assert resp.status_code == 201
    assert datasets_env.fake.calls_for(TOOL_CORRECTION_ADD)[0]["values"] == {"name": None}


def test_correction_invalid_payloads_400(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env)
    url = f"/api/v1/datasets/{dataset['id']}/corrections"
    cases = [
        {"op": "update", "row_id": "pk:1", "column": "name"},  # 缺 value
        {"op": "update", "row_id": "pk:1", "value": 1},  # 缺 column
        {"op": "update", "column": "name", "value": 1},  # 缺 row_id
        {"op": "delete"},  # 缺 row_id
        {"op": "insert"},  # 缺 values
        {"op": "insert", "values": {}},  # 空 values
    ]
    for payload in cases:
        resp = datasets_env.client.post(url, headers=CUSTOMER_A, json=payload)
        assert resp.status_code == 400, payload
    # 校验失败绝不调引擎的 correction_add
    assert TOOL_CORRECTION_ADD not in datasets_env.fake.tools


def test_corrections_list_and_revert_passthrough(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env)
    resp = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/corrections",
        headers=CUSTOMER_A,
        params={"include_reverted": "true", "include_dangling": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["corrections"][0]["correction_id"] == 7
    assert body["corrections"][0]["dangling_at"] is not None
    args = datasets_env.fake.calls_for(TOOL_CORRECTION_LIST)[0]
    assert args == {
        "dataset_key": dataset["key"],
        "include_reverted": True,
        "include_dangling": True,
    }
    reverted = datasets_env.client.post(
        f"/api/v1/datasets/{dataset['id']}/corrections/7/revert", headers=CUSTOMER_A
    )
    assert reverted.status_code == 200
    assert reverted.json() == {"reverted": True}
    assert datasets_env.fake.calls_for(TOOL_CORRECTION_REVERT)[0] == {
        "dataset_key": dataset["key"],
        "correction_id": 7,
        "actor": "customer:cust-001",
    }


def test_preview_rows_pagination_passthrough(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env)
    resp = datasets_env.client.get(
        f"/api/v1/datasets/{dataset['id']}/rows",
        headers=CUSTOMER_A,
        params={"page": 2, "page_size": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 2 and body["page_size"] == 10
    assert body["total"] == 2
    assert body["rows"][0]["__row_id"] == "pk:1"
    assert datasets_env.fake.calls_for(TOOL_PREVIEW)[0] == {
        "dataset_key": dataset["key"],
        "limit": 10,
        "offset": 10,
        "include_corrected": True,
    }


# ------------------------------------------------------------------ 删除 / 跨企业


def test_delete_dataset_calls_engine_then_removes_registration(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env)
    resp = datasets_env.client.delete(
        f"/api/v1/datasets/{dataset['id']}", headers=CUSTOMER_A
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert datasets_env.fake.calls_for(TOOL_DELETE)[0] == {
        "dataset_key": dataset["key"],
        "actor": "customer:cust-001",
    }
    assert datasets_env.client.get("/api/v1/datasets", headers=CUSTOMER_A).json() == []
    assert _dataset(dataset["id"]) is None


def test_delete_fail_closed_when_engine_unreachable(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env)
    datasets_env.fake.fail_on[TOOL_DELETE] = EngineUnavailable("cell 未开通")
    resp = datasets_env.client.delete(
        f"/api/v1/datasets/{dataset['id']}", headers=CUSTOMER_A
    )
    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("engine_unavailable:")
    assert _dataset(dataset["id"]) is not None


def test_cross_customer_access_all_404(datasets_env) -> None:
    dataset = make_uploaded_dataset(datasets_env, customer="cust-001")
    base = f"/api/v1/datasets/{dataset['id']}"
    assert datasets_env.client.get(base, headers=CUSTOMER_B).status_code == 404
    assert datasets_env.client.get(f"{base}/rows", headers=CUSTOMER_B).status_code == 404
    assert datasets_env.client.get(f"{base}/syncs", headers=CUSTOMER_B).status_code == 404
    assert (
        datasets_env.client.get(f"{base}/corrections", headers=CUSTOMER_B).status_code
        == 404
    )
    assert (
        datasets_env.client.post(
            f"{base}/corrections",
            headers=CUSTOMER_B,
            json={"op": "delete", "row_id": "pk:1"},
        ).status_code
        == 404
    )
    assert (
        datasets_env.client.post(
            f"{base}/resume",
            headers=CUSTOMER_B,
            json={"accept_columns": [{"name": "id", "type": "INTEGER"}]},
        ).status_code
        == 404
    )
    assert datasets_env.client.delete(base, headers=CUSTOMER_B).status_code == 404
    assert datasets_env.client.get(base, headers=CUSTOMER_A).status_code == 200


# ------------------------------------------------------------------ 引擎通道报文


def test_engine_client_native_body_and_platform_credential() -> None:
    """引擎通道 native 报文钉子：标准 MCP 握手 + tools/call（工具名进
    JSON-RPC params）+ Bearer 平台凭据；registry 入口端点形态。

    （历史：本用例两代更迭——JSON-RPC 直发 → registry mcp-proxy 验证
    过的 gateway 简化形态 → 2026-10-07 registry 定版后回归标准 MCP
    native 形态，简化报文已透传不翻译、正式退役。）
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "method": body.get("method"),
                "body": body,
            }
        )
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-eng"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"success": True, "rows": 3, "size_bytes": 42}
                            ),
                        }
                    ],
                    "isError": False,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = EngineToolClient(
        "cust-001",
        endpoint="https://registry.example/fd-wire-cell-cust-001/mcp",
        credential="platform-service-credential",
        http_client=httpx.Client(transport=transport),
    )
    result = client.call("customer_dataset_ingest_commit", {"ingest_id": "ing-1"})
    assert result == {"rows": 3, "size_bytes": 42}
    calls = [s for s in seen if s["method"] == "tools/call"]
    assert len(calls) == 1
    assert calls[0]["headers"]["authorization"] == "Bearer platform-service-credential"
    assert calls[0]["body"]["jsonrpc"] == "2.0"
    assert calls[0]["body"]["params"] == {
        "name": "customer_dataset_ingest_commit",
        "arguments": {"ingest_id": "ing-1"},
    }
    assert calls[0]["headers"]["mcp-session-id"] == "sess-eng"
    assert "fwc_" not in json.dumps(calls[0]["body"])
    assert "mcp-proxy" not in calls[0]["url"]
    client.close()


def test_engine_client_surfaces_tool_error_and_non_2xx() -> None:
    def _handshake(final_handler):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "s"},
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                )
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return final_handler(body)

        return handler

    def tool_error(body) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"success": False, "error": "行数超过上限"})}
                    ],
                    "isError": True,
                },
            },
        )

    client = EngineToolClient(
        "cust-001",
        endpoint="https://example/mcp",
        credential="cred",
        http_client=httpx.Client(transport=httpx.MockTransport(_handshake(tool_error))),
    )
    with pytest.raises(EngineCallError, match="行数超过上限"):
        client.call("customer_dataset_ingest_commit", {})
    client.close()

    def boom(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaboom")

    client = EngineToolClient(
        "cust-001",
        endpoint="https://example/mcp",
        credential="cred",
        http_client=httpx.Client(transport=httpx.MockTransport(boom)),
    )
    with pytest.raises(EngineCallError, match="500"):
        client.call("customer_dataset_overview", {})
    client.close()