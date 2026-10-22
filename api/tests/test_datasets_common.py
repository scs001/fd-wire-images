"""datasets 切片测试的共享环境（本文件自身不含测试）。

- 内存 SQLite 库（``FD_WIRE_DATASETS_DATABASE_URL=sqlite://``，StaticPool）；
- 平台库临时文件（console 身份映射：Bearer ID token → logto_sub →
  Customer，wire-self-serve-launch 4.5；客户面命名空间键=Customer.name，
  故种子客户名沿用 cust-001/cust-002 字面量）；
- 挂了 datasets 客户面 + 连接器面的 FastAPI 应用（全量 app，路由真身）；
- ``FakeEngine``：引擎 MCP 客户端假实现——记录 ``tools/call`` 调用序列
  （工具全名 ``customer_dataset_*``）、可注入指定工具的失败。

每个用例独立重置 datasets 运行时缓存（引擎/配置/建表标记）。
"""

from __future__ import annotations

import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.datasets import connector_routes
from fd_wire_api.datasets.db import reset_datasets_runtime
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    EngineUnavailable,
    TOOL_ADD_COLUMN,
    TOOL_AGGREGATE,
    TOOL_CORRECTION_ADD,
    TOOL_CORRECTION_LIST,
    TOOL_CORRECTION_REVERT,
    TOOL_CREATE_DECLARED,
    TOOL_DELETE,
    TOOL_DERIVE,
    TOOL_DERIVE_REFRESH,
    TOOL_INGEST_ABORT,
    TOOL_INGEST_BEGIN,
    TOOL_INGEST_CHUNK,
    TOOL_INGEST_COMMIT,
    TOOL_JOIN_INDICATORS,
    TOOL_LINEAGE,
    TOOL_LINK,
    TOOL_OVERVIEW,
    TOOL_PREVIEW,
    TOOL_QUERY,
    TOOL_WRITE_BATCH,
    get_engine_client_factory,
)
from fd_wire_api.quality.service import TOOL_QUALITY

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

INTERNAL_TOKEN = "wire-internal-dev"
SUB_A = "sub-cust-001"
SUB_B = "sub-cust-002"
# token 在模块导入期铸好（RSA 进程缓存；iss/aud 在 fixture 里以 env 对齐），
# 客户面命名空间键 = Customer.name（种子客户名 cust-001/cust-002）
CUSTOMER_A = bearer(mint_id_token(SUB_A))
CUSTOMER_B = bearer(mint_id_token(SUB_B))
CUST_HEADERS = {"cust-001": CUSTOMER_A, "cust-002": CUSTOMER_B}
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}
# admin 双闸第一道凭据（4.3 内部概览切换 admin 门；FD_WIRE_ADMIN_TOKEN
# 由 make_console_env 固定种成 "test-admin-token"）
ADMIN_AUTH = {"X-Admin-Token": "test-admin-token"}


class FakeEngine:
    """引擎通道假实现：记录调用序列，按工具返回引擎契约形态的结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.chunk_rows: int = 0
        self.commit_size_bytes: int = 4096
        self.fail_on: dict[str, Exception] = {}
        #: 工具级引擎侧拒绝（返回 success=False 信封而非抛异常）
        self.error_on: dict[str, str] = {}
        self.overview_result: dict = {
            "key": "any",
            "columns": [
                {"name": "id", "type": "INTEGER"},
                {"name": "amount", "type": "REAL"},
            ],
            "rows": 2,
            "size_bytes": 2048,
            "capabilities": {"query": True},
        }
        self.query_result: dict = {
            "columns": [{"name": "id", "type": "INTEGER"}],
            "rows": [{"id": 1, "__row_id": "pk:[1]"}],
            "total": 1,
            "limit": 100,
            "offset": 0,
            "truncated": False,
        }
        self.aggregate_result: dict = {"group_by": ["region"], "groups": [{"region": "华东", "count": 2}], "group_count": 1, "truncated": False}
        self.lineage_result: dict = {"derivation": [], "children": []}
        self.preview_result: dict = {
            "columns": [
                {"name": "id", "type": "INTEGER"},
                {"name": "name", "type": "TEXT"},
            ],
            "rows": [
                {"id": 1, "name": "a", "__row_id": "pk:1"},
                {"id": 2, "name": "b", "__row_id": "pk:2"},
            ],
            "total": 2,
        }
        self.corrections: list[dict] = [
            {
                "correction_id": 7,
                "op": "update",
                "anchor": "pk:1",
                "values": {"name": "fixed"},
                "actor": "customer:cust-001",
                "dangling_at": "2026-10-06T00:00:00Z",
                "reverted_at": None,
            }
        ]
        #: 质量工具返回（wire-asset-catalog 4：建档/同步/派生钩子自动触发
        # customer_dataset_quality——聚合计数与比率，无任何数据值）
        self.quality_result: dict = {
            "row_count": 2,
            "computed_at": "2026-10-06T00:00:00Z",
            "pk_uniqueness": {"columns": ["id"], "unique_ratio": 1.0},
            "pk_declared": True,
            "null_rates": [{"column": "id", "null_rate": 0.0, "sampled_rows": 2}],
            "sample_limit_hit": False,
        }
        #: link 工具返回（wire-transform-pipelines link 步骤；行数可编程）
        self.link_result: dict = {"rows": 3}

    @property
    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def calls_for(self, tool: str) -> list[dict]:
        return [args for name, args in self.calls if name == tool]

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        failure = self.fail_on.get(tool)
        if failure is not None:
            raise failure
        error = self.error_on.get(tool)
        if error is not None:
            return {"success": False, "error": error}
        # 与真实客户端一致：返回前剥掉引擎的 success 信封
        result = self._result(tool, arguments)
        result.pop("success", None)
        return result

    def _result(self, tool: str, arguments: dict) -> dict:
        if tool == TOOL_INGEST_BEGIN:
            return {
                "success": True,
                "ingest_id": f"ing-{arguments['dataset_key']}",
                "staging_table": f"cust_{arguments['dataset_key']}__staging",
            }
        if tool == TOOL_INGEST_CHUNK:
            accepted = len(arguments["rows"])
            self.chunk_rows += accepted
            return {"success": True, "accepted": accepted}
        if tool == TOOL_INGEST_COMMIT:
            return {
                "success": True,
                "rows": self.chunk_rows,
                "size_bytes": self.commit_size_bytes,
                "corrections_replayed": 0,
                "corrections_dangling": 0,
            }
        if tool == TOOL_INGEST_ABORT:
            return {"success": True, "aborted": True}
        if tool == TOOL_PREVIEW:
            return dict(self.preview_result)
        if tool == TOOL_CORRECTION_ADD:
            return {"success": True, "correction_id": 7}
        if tool == TOOL_CORRECTION_LIST:
            return {"success": True, "corrections": [dict(c) for c in self.corrections]}
        if tool == TOOL_CORRECTION_REVERT:
            return {"success": True, "reverted": True}
        if tool == TOOL_DELETE:
            return {"success": True, "deleted": True}
        # ── 数据处理引擎（wire-customer-data-engine §1–§4）──
        if tool == TOOL_CREATE_DECLARED:
            return {
                "success": True,
                "dataset_key": arguments["dataset_key"],
                "rows": 0,
                "columns": arguments["columns"],
                "pk_columns": arguments.get("pk_columns") or [],
            }
        if tool == TOOL_QUERY:
            return dict(self.query_result)
        if tool == TOOL_AGGREGATE:
            return dict(self.aggregate_result)
        if tool == TOOL_WRITE_BATCH:
            return {
                "success": True,
                "applied": len(arguments["ops"]),
                "correction_ids": list(range(1, len(arguments["ops"]) + 1)),
            }
        if tool == TOOL_ADD_COLUMN:
            return {
                "success": True,
                "column": arguments["column"],
                "columns": [{"name": "id", "type": "INTEGER"}, arguments["column"]],
            }
        if tool in (TOOL_DERIVE, TOOL_JOIN_INDICATORS):
            return {
                "success": True,
                "dataset_key": arguments["child_key"],
                "rows": 3,
                "kind": "join" if tool == TOOL_JOIN_INDICATORS else "query",
                "unresolved_regions": [],
            }
        if tool == TOOL_DERIVE_REFRESH:
            return {"success": True, "dataset_key": arguments["dataset_key"], "rows": 4, "refreshed": True}
        if tool == TOOL_LINK:
            # LEFT JOIN 物化子集（wire-entity-link 引擎契约：rows + 子 key 回显）
            return {"success": True, **dict(self.link_result)}
        if tool == TOOL_LINEAGE:
            return dict(self.lineage_result)
        if tool == TOOL_OVERVIEW:
            return {**self.overview_result, "key": arguments["dataset_key"]}
        if tool == TOOL_QUALITY:
            return {"success": True, **dict(self.quality_result)}
        raise AssertionError(f"FakeEngine 未实现的工具: {tool}")


@pytest.fixture()
def datasets_env(tmp_path, monkeypatch):
    """隔离运行时（内存库/临时上传目录）+ 假引擎 + TestClient。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    # materialization 侧（wire-customer-data-engine 6.1）：独立引擎必须同样
    # 钉进临时库，否则落到 cwd 残留文件（schema 漂移）或跨测试串库
    monkeypatch.setenv("MATERIALIZATION_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db")
    monkeypatch.setenv("MATERIALIZATION_AUTO_CREATE_TABLES", "1")
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_A, "cust-001"), (SUB_B, "cust-002")],
        internal_token=INTERNAL_TOKEN,
        # 付费档种子：本 fixture 的既有用例全是 cell 链路行为（引擎调用断言），
        # tier 按建集时刻档位分流后（wire-free-tier-data-gate D1）须落在付费档；
        # 免费档（api 层）路径见 test_datasets_free_tier.py（用例内切档）。
        plan_code="basic",
    )
    reset_datasets_runtime()
    connector_routes._SYNC_SESSIONS.clear()
    from fd_wire_api.materialization.db import reset_materialization_runtime

    reset_materialization_runtime()

    fake = FakeEngine()
    seen_customers: list[str] = []

    def fake_factory(customer_id: str) -> FakeEngine:
        seen_customers.append(customer_id)
        return fake

    fastapi_app.dependency_overrides[get_engine_client_factory] = lambda: fake_factory
    with TestClient(fastapi_app) as client:
        yield SimpleNamespace(
            client=client,
            fake=fake,
            app=fastapi_app,
            customers=seen_customers,
            upload_dir=tmp_path / "uploads",
            tmp_path=tmp_path,
        )
    fastapi_app.dependency_overrides.clear()
    connector_routes._SYNC_SESSIONS.clear()
    reset_datasets_runtime()


# ------------------------------------------------------------------ 流程助手


def pairing_token(env, customer: str = "cust-001") -> str:
    resp = env.client.post(
        "/api/v1/connectors/pairing-tokens", headers=CUST_HEADERS[customer]
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def pair_connector(env, *, customer: str = "cust-001", name: str = "conn-1") -> dict:
    token = pairing_token(env, customer)
    resp = env.client.post(
        "/api/v1/connectors/pair",
        json={"token": token, "name": name, "version": "1.0.0"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def connector_headers(connector_key: str) -> dict:
    return {"Authorization": f"Bearer {connector_key}"}


def patch_dataset_settings(monkeypatch, **values) -> None:
    """就地改 datasets 设置单例（避免 reset 缓存丢内存库；用例结束自动还原）。"""
    from fd_wire_api.datasets.settings import get_settings

    for key, value in values.items():
        monkeypatch.setattr(get_settings(), key, value)


def create_connector_dataset(
    env,
    *,
    connector_id: str,
    customer: str = "cust-001",
    name: str = "Sales Table",
    source_name: str = "erp",
    source_ref: str = "erp_db",
    schedule_cron: str | None = "0 * * * *",
) -> dict:
    resp = env.client.post(
        "/api/v1/datasets",
        headers=CUST_HEADERS[customer],
        json={
            "name": name,
            "source": {
                "kind": "connector",
                "connector_id": connector_id,
                "source_name": source_name,
                "source_ref": source_ref,
                "schedule_cron": schedule_cron,
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def push_sync(
    env,
    *,
    connector_key: str,
    dataset_key: str,
    columns: list[dict],
    rows: list[dict],
    pk_columns: list[str] | None = None,
    chunk_size: int | None = None,
    watermark_value: str | None = None,
) -> dict:
    """完整走一遍三阶段（begin → chunks → commit），返回各步响应。

    ``watermark_value`` 提供时随 commit 上报（增量水位捕获；
    wire-connector-incremental-sync）。
    """
    headers = connector_headers(connector_key)
    begun = env.client.post(
        "/api/v1/connector/syncs",
        headers=headers,
        json={
            "dataset_key": dataset_key,
            "columns": columns,
            "pk_columns": pk_columns or [],
            "row_count": len(rows),
        },
    )
    if begun.status_code != 201:
        return {"begin": begun}
    sync_id = begun.json()["sync_id"]
    size = chunk_size or 2000
    for index, start in enumerate(range(0, len(rows), size)):
        chunk = rows[start : start + size]
        resp = env.client.post(
            f"/api/v1/connector/syncs/{sync_id}/chunks",
            headers=headers,
            json={"index": index, "rows": chunk},
        )
        if resp.status_code != 200:
            return {"begin": begun, "chunk": resp}
    committed = env.client.post(
        f"/api/v1/connector/syncs/{sync_id}/commit",
        headers=headers,
        json=None if watermark_value is None else {"watermark_value": watermark_value},
    )
    return {"begin": begun, "commit": committed}


def upload_csv(env, content: bytes | str, *, filename: str = "data.csv", customer: str = "cust-001"):
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return env.client.post(
        "/api/v1/datasets/uploads",
        headers=CUST_HEADERS[customer],
        files={"file": (filename, payload, "text/csv")},
    )


def create_upload_dataset(
    env, upload_id: str, *, name: str = "Uploaded Data", customer: str = "cust-001"
):
    return env.client.post(
        "/api/v1/datasets",
        headers=CUST_HEADERS[customer],
        json={"name": name, "source": {"kind": "upload", "upload_id": upload_id}},
    )


def make_uploaded_dataset(env, *, customer: str = "cust-001", name: str = "Uploaded Data") -> dict:
    """经上传通道建一个数据集（默认 3 行；用于修正/预览类用例）。"""
    resp = upload_csv(env, "id,name\n1,a\n2,b\n3,c\n", customer=customer)
    assert resp.status_code == 201, resp.text
    created = create_upload_dataset(
        env, resp.json()["upload_id"], name=name, customer=customer
    )
    assert created.status_code == 201, created.text
    return created.json()


__all__ = [
    "ADMIN_AUTH",
    "CUSTOMER_A",
    "CUSTOMER_B",
    "CUST_HEADERS",
    "INTERNAL",
    "EngineCallError",
    "EngineUnavailable",
    "FakeEngine",
    "connector_headers",
    "create_connector_dataset",
    "create_upload_dataset",
    "make_uploaded_dataset",
    "pair_connector",
    "pairing_token",
    "patch_dataset_settings",
    "push_sync",
    "upload_csv",
]