"""REST 源云端登记与下发（wire-rest-connector 任务 2.1 / 3.1 的 api 半边）。

覆盖：
- 登记（``source.kind=connector`` + ``source_config``）：落库形状原样、投影
  列名重复 422、URL scheme 非法 422、分页声明互斥校验；
- **零凭据**（ADR-0004）：凭据类请求头名（authorization/cookie/x-api-key…）
  在登记载荷层直接 422；带 ``auth`` 字段因 ``extra="forbid"`` 被拒；
- ``GET /connector/config`` 下发 ``source_config``，且响应全文不出现任何凭据
  字段名与取值（对拍登记载荷里的 secret 值）；
- 非 rest 源零漂移：不带 ``source_config`` 的登记行为不变（响应
  ``source_config`` 为 null，配置下发无该键）。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest

from test_datasets_common import (
    CUST_HEADERS,
    connector_headers,
    create_connector_dataset,
    datasets_env,  # noqa: F401 - pytest fixture
    pair_connector,
)

#: 登记载荷里的模拟 secret（断言它绝不进云端任何响应）
SECRET = "s3cr3t-should-never-leave-the-customer-network"

REST_CONFIG = {
    "url": "https://api.example.com/v1/orders",
    "method": "GET",
    "headers": {"X-Tenant": "acme"},
    "root_path": "data.items",
    "projection": [
        {"path": "id", "column": "order_id", "type": "INTEGER"},
        {"path": "buyer.name", "column": "buyer_name", "type": "TEXT"},
        {"path": "amount", "column": "amount", "type": "REAL"},
    ],
    "pagination": {
        "mode": "page",
        "page_param": "page",
        "size_param": "size",
        "start_page": 1,
        "page_size": 500,
        "max_pages": 50,
    },
    "watermark_path": "data.updated_at",
}


def _pair(env, name: str = "rest-conn") -> tuple[str, str]:
    paired = pair_connector(env, name=name)
    return paired["connector_id"], paired["connector_key"]


def _register(env, connector_id: str, payload: dict, *, name: str = "Orders API"):
    return env.client.post(
        "/api/v1/datasets",
        headers=CUST_HEADERS["cust-001"],
        json={
            "name": name,
            "source": {
                "kind": "connector",
                "connector_id": connector_id,
                "source_name": "orders-api",
                "source_ref": "orders",
                "schedule_cron": "0 * * * *",
                **payload,
            },
        },
    )


# ------------------------------------------------------------------ 登记成功


def test_register_rest_source_roundtrips_config(datasets_env) -> None:
    connector_id, key = _pair(datasets_env)
    resp = _register(datasets_env, connector_id, {"source_config": REST_CONFIG})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    cfg = body["source_config"]
    assert cfg["url"] == REST_CONFIG["url"]
    assert cfg["method"] == "GET"
    assert cfg["headers"] == {"X-Tenant": "acme"}
    assert cfg["root_path"] == "data.items"
    assert [p["column"] for p in cfg["projection"]] == [
        "order_id",
        "buyer_name",
        "amount",
    ]
    assert cfg["pagination"]["mode"] == "page"
    assert cfg["pagination"]["page_size"] == 500
    assert cfg["watermark_path"] == "data.updated_at"


def test_config_pull_carries_source_config_without_credentials(datasets_env) -> None:
    connector_id, key = _pair(datasets_env, name="rest-pull")
    resp = _register(datasets_env, connector_id, {"source_config": REST_CONFIG})
    assert resp.status_code == 201, resp.text

    config = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(key)
    )
    assert config.status_code == 200, config.text
    raw = config.text
    # 零凭据：响应全文不含 secret 值，也不出现认证字段名
    assert SECRET not in raw
    assert "authorization" not in raw.lower()
    for banned in ("password", "secret", "api_key", "apikey", "token"):
        assert banned not in raw.lower(), banned

    entry = config.json()["datasets"][0]
    assert entry["source_config"]["url"] == REST_CONFIG["url"]
    assert entry["source_config"]["projection"][0]["column"] == "order_id"


def test_non_rest_dataset_has_no_source_config_key(datasets_env) -> None:
    """零漂移：不带 source_config 的登记，响应为 null、下发无该键。"""
    connector_id, key = _pair(datasets_env, name="plain-conn")
    plain = create_connector_dataset(datasets_env, connector_id=connector_id)
    assert plain["source_config"] is None
    config = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(key)
    ).json()
    assert "source_config" not in config["datasets"][0]


# ------------------------------------------------------------------ 零凭据闸


@pytest.mark.parametrize(
    "header_name",
    ["Authorization", "authorization", "Cookie", "X-Api-Key", "PRIVATE-TOKEN"],
)
def test_credential_headers_rejected_at_registration(
    datasets_env, header_name: str
) -> None:
    connector_id, _ = _pair(datasets_env, name=f"cred-{header_name.lower()}")
    cfg = dict(REST_CONFIG)
    cfg["headers"] = {header_name: SECRET}
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text
    detail = json.dumps(resp.json(), ensure_ascii=False)
    assert "凭据" in detail


def test_auth_field_forbidden_in_cloud_payload(datasets_env) -> None:
    """auth（bearer/basic 凭据）在云端 schema 里根本不存在——extra=forbid。"""
    connector_id, _ = _pair(datasets_env, name="auth-forbid")
    cfg = dict(REST_CONFIG)
    cfg["auth"] = {"type": "bearer", "token": SECRET}
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text
    assert SECRET not in resp.text


@pytest.mark.parametrize(
    "url",
    ["ftp://api.example.com/x", "file:///etc/passwd", "api.example.com/x", ""],
)
def test_url_scheme_validated(datasets_env, url: str) -> None:
    connector_id, _ = _pair(datasets_env, name=f"url-{abs(hash(url)) % 10000}")
    cfg = dict(REST_CONFIG)
    cfg["url"] = url
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------ 声明校验


def test_duplicate_projection_column_rejected(datasets_env) -> None:
    connector_id, _ = _pair(datasets_env, name="dup-col")
    cfg = dict(REST_CONFIG)
    cfg["projection"] = [
        {"path": "a", "column": "same"},
        {"path": "b", "column": "same"},
    ]
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text
    assert "投影列名重复" in resp.text


def test_page_pagination_requires_page_param(datasets_env) -> None:
    connector_id, _ = _pair(datasets_env, name="pag-miss")
    cfg = dict(REST_CONFIG)
    cfg["pagination"] = {"mode": "page", "size_param": "size"}
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text


def test_cursor_pagination_requires_cursor_param(datasets_env) -> None:
    connector_id, _ = _pair(datasets_env, name="cursor-miss")
    cfg = dict(REST_CONFIG)
    cfg["pagination"] = {"mode": "cursor", "cursor_path": "meta.next"}
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text


def test_empty_projection_rejected(datasets_env) -> None:
    connector_id, _ = _pair(datasets_env, name="empty-proj")
    cfg = dict(REST_CONFIG)
    cfg["projection"] = []
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text


def test_page_size_ceiling(datasets_env) -> None:
    connector_id, _ = _pair(datasets_env, name="pagesize")
    cfg = dict(REST_CONFIG)
    cfg["pagination"] = {
        "mode": "page",
        "page_param": "page",
        "page_size": 999_999,
    }
    resp = _register(datasets_env, connector_id, {"source_config": cfg})
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------ 增量沿用


def test_rest_source_supports_incremental_settings(datasets_env) -> None:
    """rest 源沿用既有 sync-settings（零新调度路径）。"""
    connector_id, key = _pair(datasets_env, name="rest-incr")
    created = _register(datasets_env, connector_id, {"source_config": REST_CONFIG})
    assert created.status_code == 201, created.text
    dataset = created.json()

    # 增量设置要求水位列 ∈ columns_json；先经一次同步建立列基线
    from test_datasets_common import push_sync

    push = push_sync(
        datasets_env,
        connector_key=key,
        dataset_key=dataset["key"],
        columns=[
            {"name": "order_id", "type": "INTEGER"},
            {"name": "updated_at", "type": "TEXT"},
        ],
        rows=[{"order_id": 1, "updated_at": "2026-10-29T00:00:00Z"}],
        pk_columns=["order_id"],
    )
    assert push["commit"].status_code == 200, push["commit"].text

    patch = datasets_env.client.patch(
        f"/api/v1/datasets/{dataset['id']}/sync-settings",
        headers=CUST_HEADERS["cust-001"],
        json={"sync_mode": "incremental", "watermark_column": "updated_at"},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["sync_mode"] == "incremental"

    config = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(key)
    ).json()
    entry = config["datasets"][0]
    assert entry["sync_mode"] == "incremental"
    assert entry["watermark_column"] == "updated_at"
    # REST 声明随增量配置一起下发（连接器据 watermark_path 提取水位）
    assert entry["source_config"]["watermark_path"] == "data.updated_at"
