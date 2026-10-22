"""连接器身份与配置面（任务 2.2 / 2.4）。

覆盖：
- 配对令牌 15 分钟单次（二次使用 401、过期 401）；
- 吊销即时生效（心跳/配置/推送全部 401）；
- 连接器 key 在客户面与内部读端点一律无效（scope 仅推送 + 拉配置）；
- 心跳在线态推导（3×轮询周期阈值）；
- ``GET /connector/config`` 响应**零凭据**断言（无任何 dsn/password/
  凭据字段与 key 明文/散列）且只含本连接器白名单数据集；
- 内部概览：admin 双闸鉴权（X-Admin-Token + Bearer wire-admin 角色，
  wire-console-ux-fixes 4.3；旧 X-Internal-Token 不再被接受）+ 只读聚合形状。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from datetime import timedelta

from fd_wire_api.datasets import service
from fd_wire_api.datasets.db import get_session
from fd_wire_api.datasets.models import Connector, ConnectorPairingToken
from test_datasets_common import (
    ADMIN_AUTH,
    CUSTOMER_A,
    CUSTOMER_B,
    INTERNAL,
    connector_headers,
    create_connector_dataset,
    datasets_env,  # noqa: F401 - pytest fixture
    pair_connector,
    pairing_token,
)
from console_fixture import bearer, mint_id_token


def _session():
    return get_session()


# ------------------------------------------------------------------ 配对 / 吊销


def test_pairing_token_single_use_then_401(datasets_env) -> None:
    token = pairing_token(datasets_env)
    first = datasets_env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": "conn-1", "version": "1.0.0"},
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["connector_key"].startswith("fwc_")
    # 明文只落响应：库内只有 sha256，且与明文不同
    session = _session()
    try:
        key_row = session.query(Connector).filter_by(id=body["connector_id"]).one()
        assert key_row.name == "conn-1"
        from fd_wire_api.datasets.models import ConnectorKey

        stored = session.query(ConnectorKey).filter_by(connector_id=body["connector_id"]).all()
        assert len(stored) == 1
        assert stored[0].key_hash == service.sha256_hex(body["connector_key"])
        assert body["connector_key"] not in stored[0].key_hash
    finally:
        session.close()
    # 单次：同一令牌再用即 401
    second = datasets_env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": "conn-2", "version": "1.0.0"},
    )
    assert second.status_code == 401


def test_expired_pairing_token_401(datasets_env) -> None:
    token = pairing_token(datasets_env)
    session = _session()
    try:
        row = session.query(ConnectorPairingToken).one()
        row.expires_at = service.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()
    resp = datasets_env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": "conn-late", "version": "1.0.0"},
    )
    assert resp.status_code == 401


def test_bogus_pairing_token_401(datasets_env) -> None:
    resp = datasets_env.client.post(
        "/api/v1/connectors/pair",
        json={"token": "fwp_not-a-real-token", "name": "x", "version": "1"},
    )
    assert resp.status_code == 401


def test_revoke_immediate_401_on_all_connector_faces(datasets_env) -> None:
    paired = pair_connector(datasets_env)
    key = paired["connector_key"]
    headers = connector_headers(key)
    assert (
        datasets_env.client.post(
            "/api/v1/connector/heartbeat", headers=headers, json={"version": "1.0.0"}
        ).status_code
        == 200
    )
    revoked = datasets_env.client.delete(
        f"/api/v1/connectors/{paired['connector_id']}", headers=CUSTOMER_A
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert (
        datasets_env.client.post(
            "/api/v1/connector/heartbeat", headers=headers, json={"version": "1.0.0"}
        ).status_code
        == 401
    )
    assert datasets_env.client.get("/api/v1/connector/config", headers=headers).status_code == 401
    assert (
        datasets_env.client.post(
            "/api/v1/connector/syncs",
            headers=headers,
            json={
                "dataset_key": "anything",
                "columns": [{"name": "id", "type": "INTEGER"}],
                "pk_columns": [],
                "row_count": 1,
            },
        ).status_code
        == 401
    )
    listed = datasets_env.client.get("/api/v1/connectors", headers=CUSTOMER_A).json()
    assert listed[0]["status"] == "revoked"


def test_connector_key_not_valid_for_customer_or_internal_reads(datasets_env) -> None:
    """连接器 key 不能读数据：客户面/内部读端点 401（无客户身份可言）。"""
    paired = pair_connector(datasets_env)
    headers = connector_headers(paired["connector_key"])
    assert datasets_env.client.get("/api/v1/datasets", headers=headers).status_code == 401
    assert (
        datasets_env.client.get("/api/v1/datasets", headers={**headers, **CUSTOMER_A}).status_code
        == 200
    )
    assert (
        datasets_env.client.get("/internal/datasets/overview", headers=headers).status_code
        == 401
    )
    # 内部概览已切 admin 双闸（4.3）：连接器 key 的 Bearer 不是 ID token，
    # 即便叠加 X-Admin-Token 也过不了角色闸（X-Admin-Token 单独给 → 401 缺 Bearer）
    assert (
        datasets_env.client.get(
            "/internal/datasets/overview",
            headers={**headers, **ADMIN_AUTH},
        ).status_code
        == 401
    )
    # 载体错误（非 Bearer / 非 fwc_ 前缀）同样 401
    assert (
        datasets_env.client.post(
            "/api/v1/connector/heartbeat",
            headers={"Authorization": "Basic Zm9v"},
            json={"version": "1"},
        ).status_code
        == 401
    )


def test_re_pair_same_name_reuses_connector_and_keeps_old_key(datasets_env) -> None:
    """同名重配对=同一台机器重装：复用连接器行、旧 key 不自动吊销。"""
    paired = pair_connector(datasets_env, name="host-1")
    create_connector_dataset(
        datasets_env,
        connector_id=paired["connector_id"],
        name="Keep Bound",
        source_ref="erp_db",
    )
    token = pairing_token(datasets_env)
    again = datasets_env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": "host-1", "version": "2.0.0"},
    )
    assert again.status_code == 201, again.text
    re_paired = again.json()
    assert re_paired["connector_id"] == paired["connector_id"]
    assert re_paired["connector_key"] != paired["connector_key"]
    # 旧 key 仍然可用（不自动吊销），新 key 也可用
    for key in (paired["connector_key"], re_paired["connector_key"]):
        assert (
            datasets_env.client.post(
                "/api/v1/connector/heartbeat",
                headers=connector_headers(key),
                json={"version": "2.0.0"},
            ).status_code
            == 200
        )
    # 白名单/数据集绑定保持不变
    config = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(re_paired["connector_key"])
    ).json()
    assert [d["dataset_key"] for d in config["datasets"]] == ["keep_bound"]
    # 吊销后不复生：同名再配对成长为新连接器
    datasets_env.client.delete(
        f"/api/v1/connectors/{paired['connector_id']}", headers=CUSTOMER_A
    )
    token2 = pairing_token(datasets_env)
    fresh = datasets_env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token2, "name": "host-1", "version": "3.0.0"},
    ).json()
    assert fresh["connector_id"] != paired["connector_id"]


# ------------------------------------------------------------------ 心跳推导


def test_heartbeat_status_derived_online_then_offline(datasets_env) -> None:
    paired = pair_connector(datasets_env)
    headers = connector_headers(paired["connector_key"])
    resp = datasets_env.client.post(
        "/api/v1/connector/heartbeat", headers=headers, json={"version": "2.3.4"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"poll_interval_seconds": 60}
    listed = datasets_env.client.get("/api/v1/connectors", headers=CUSTOMER_A).json()
    assert listed[0]["status"] == "online"
    assert listed[0]["version"] == "2.3.4"
    assert listed[0]["last_heartbeat_at"] is not None
    # 心跳过期（> 3×60s）→ offline（推导，不落库）
    session = _session()
    try:
        row = session.get(Connector, paired["connector_id"])
        row.last_heartbeat_at = service.utcnow() - timedelta(seconds=181)
        session.commit()
    finally:
        session.close()
    assert (
        datasets_env.client.get("/api/v1/connectors", headers=CUSTOMER_A).json()[0]["status"]
        == "offline"
    )


# ------------------------------------------------------------------ 配置零凭据


def test_config_response_has_zero_credentials(datasets_env) -> None:
    paired = pair_connector(datasets_env)
    create_connector_dataset(
        datasets_env,
        connector_id=paired["connector_id"],
        name="ERP Orders",
        source_ref="erp_db",
    )
    resp = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(paired["connector_key"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["poll_interval_seconds"] == 60
    assert body["min_sync_interval_seconds"] == 15 * 60
    assert body["limits"]["max_rows"] == 500000
    assert body["datasets"][0]["dataset_key"] == "erp_orders"
    assert body["datasets"][0]["source"] == "erp"
    assert body["datasets"][0]["source_ref"] == "erp_db"
    assert body["datasets"][0]["schedule_cron"] == "0 * * * *"
    # 零凭据断言：序列化文本不含任何凭据/密钥字样与明文
    text = json.dumps(body, ensure_ascii=False).lower()
    for forbidden in ("dsn", "password", "passwd", "passwords", "secret", "credential", "token"):
        assert forbidden not in text, forbidden
    assert paired["connector_key"] not in text
    assert service.sha256_hex(paired["connector_key"]) not in text


def test_config_lists_only_own_connector_datasets(datasets_env) -> None:
    paired_a = pair_connector(datasets_env, name="conn-a")
    paired_b = pair_connector(datasets_env, name="conn-b")
    create_connector_dataset(
        datasets_env,
        connector_id=paired_a["connector_id"],
        name="A Data",
        source_name="src_a",
        source_ref="public.orders_a",
    )
    create_connector_dataset(
        datasets_env,
        connector_id=paired_b["connector_id"],
        name="B Data",
        source_name="src_b",
        source_ref="public.orders_b",
    )
    config_b = datasets_env.client.get(
        "/api/v1/connector/config", headers=connector_headers(paired_b["connector_key"])
    ).json()
    assert [d["source"] for d in config_b["datasets"]] == ["src_b"]
    assert [d["source_ref"] for d in config_b["datasets"]] == ["public.orders_b"]
    # 另一 key 推送 A 的数据集 → 404（白名单按连接器隔离）
    resp = datasets_env.client.post(
        "/api/v1/connector/syncs",
        headers=connector_headers(paired_b["connector_key"]),
        json={
            "dataset_key": "a_data",
            "columns": [{"name": "id", "type": "INTEGER"}],
            "pk_columns": [],
            "row_count": 1,
        },
    )
    assert resp.status_code == 404


def test_connector_management_is_customer_scoped(datasets_env) -> None:
    paired = pair_connector(datasets_env, customer="cust-001")
    # 他企不可见、不可改名、不可吊销
    listed = datasets_env.client.get("/api/v1/connectors", headers=CUSTOMER_B).json()
    assert listed == []
    assert (
        datasets_env.client.patch(
            f"/api/v1/connectors/{paired['connector_id']}",
            headers=CUSTOMER_B,
            json={"name": "hijack"},
        ).status_code
        == 404
    )
    assert (
        datasets_env.client.delete(
            f"/api/v1/connectors/{paired['connector_id']}", headers=CUSTOMER_B
        ).status_code
        == 404
    )
    renamed = datasets_env.client.patch(
        f"/api/v1/connectors/{paired['connector_id']}",
        headers=CUSTOMER_A,
        json={"name": "renamed"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "renamed"
    assert renamed.json()["dataset_count"] == 0


def test_pairing_token_is_customer_scoped(datasets_env) -> None:
    token = pairing_token(datasets_env, "cust-002")
    paired = datasets_env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": "b-conn", "version": "1"},
    ).json()
    assert datasets_env.client.get("/api/v1/connectors", headers=CUSTOMER_A).json() == []
    assert len(datasets_env.client.get("/api/v1/connectors", headers=CUSTOMER_B).json()) == 1
    session = _session()
    try:
        connector = session.get(Connector, paired["connector_id"])
        assert connector.customer_id == "cust-002"
    finally:
        session.close()


# ------------------------------------------------------------------ 内部概览


def _admin_bearer(*, roles: list[str] | None = None, expired: bool = False) -> dict:
    """admin 双闸第二道凭据（照 test_admin_api 5.1 手法：本地铸 ID token）。"""
    return bearer(mint_id_token("sub-overview-admin", roles=roles, expired=expired))


def test_internal_overview_requires_admin_dual_gate_and_aggregates(datasets_env) -> None:
    """admin 双闸矩阵（gate=on 缺省）+ 只读聚合形状（wire-console-ux-fixes 4.3）。"""
    paired = pair_connector(datasets_env)
    create_connector_dataset(
        datasets_env, connector_id=paired["connector_id"], name="Overview Data"
    )
    # 1. 双全（X-Admin-Token 正确 + Bearer 含 wire-admin 角色）→ 200 返回概览
    resp = datasets_env.client.get(
        "/internal/datasets/overview", headers=ADMIN_AUTH | _admin_bearer(roles=["wire-admin"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["customer_id"] == "cust-001"
    assert body[0]["datasets"] == 1
    assert body[0]["storage_bytes"] == 0
    assert body[0]["connectors"][0]["id"] == paired["connector_id"]
    assert body[0]["connectors"][0]["status"] == "offline"
    # 只读视图：概览负载不含任何管理操作字段
    keys = set(body[0]["connectors"][0].keys())
    assert keys == {"id", "status", "last_heartbeat_at"}

    # 2. 有令牌缺 Bearer → 401（缺第二道凭据）
    assert (
        datasets_env.client.get("/internal/datasets/overview", headers=ADMIN_AUTH).status_code
        == 401
    )
    # 3. Bearer 无 wire-admin 角色（普通客户登录态）→ 403
    assert (
        datasets_env.client.get(
            "/internal/datasets/overview",
            headers=ADMIN_AUTH | _admin_bearer(roles=["ordinary-customer"]),
        ).status_code
        == 403
    )
    # roles claim 缺失同样 403
    assert (
        datasets_env.client.get(
            "/internal/datasets/overview", headers=ADMIN_AUTH | _admin_bearer()
        ).status_code
        == 403
    )
    # 4. 缺 X-Admin-Token（仅带合法 Bearer）→ 401
    assert (
        datasets_env.client.get(
            "/internal/datasets/overview", headers=_admin_bearer(roles=["wire-admin"])
        ).status_code
        == 401
    )
    # X-Admin-Token 错 → 同样 401（第一道闸照常校验）
    assert (
        datasets_env.client.get(
            "/internal/datasets/overview",
            headers={"X-Admin-Token": "wrong"} | _admin_bearer(roles=["wire-admin"]),
        ).status_code
        == 401
    )
    # 6. 旧 X-Internal-Token 单闸不再被接受（只带旧头 → 401）
    assert (
        datasets_env.client.get("/internal/datasets/overview", headers=INTERNAL).status_code
        == 401
    )


def test_internal_overview_admin_token_unconfigured_fail_closed(
    datasets_env, monkeypatch
) -> None:
    """FD_WIRE_ADMIN_TOKEN 未配置 → 403（fail-closed 优先于一切）。"""
    monkeypatch.delenv("FD_WIRE_ADMIN_TOKEN", raising=False)
    resp = datasets_env.client.get(
        "/internal/datasets/overview", headers=_admin_bearer(roles=["wire-admin"])
    )
    assert resp.status_code == 403, resp.text