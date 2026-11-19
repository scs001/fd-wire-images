"""契约试取端点测试（wire-panel-query-binding 任务 1.2）。

``POST /api/v1/artifacts/query-preview``（console 身份）：入参 = 面板
query 同形，经 parse_panel_query → 与契约刷新**同一执行路径**
（_query_dataset + _build_snapshot）出试取快照。覆盖：

- 合法试取（api 档零引擎调用）：快照 = 刷新同款归一（投影列序、值归一），
  query 归一后随响应返回（前端回填表单）；
- 非法契约 422 如实拒（白名单外算子/空 dataset_id/结构越界），零引擎调用；
- 跨客户/不存在数据集 404（不泄露存在性）；
- 执行失败 400 带原因：投影列不存在、免费窄面 LocalQueryError、
  引擎不可达/引擎信封错误（cell 档）；
- cell 档引擎路径：TOOL_QUERY 契约形参（dataset_key/filters/sort/limit），
  快照按引擎响应投影；
- 未登录 401。

fixture 手法照 test_artifacts_refresh（dependency_overrides 注入 datasets
会话与假引擎工厂；生产分库拓扑下路由依赖签名一致）。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库：TestClient 启动钩子里的 init_ai_db()
# / init_datasets_db() 不至于在测试机上落下残留 db 文件。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    EngineUnavailable,
    TOOL_QUERY,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset, DatasetColumn, DatasetRow

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-preview-001"
SUB_OTHER = "sub-preview-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))


# ------------------------------------------------------------------ fakes


class FakeEngine:
    """契约路径引擎假件（cell 档试取用）：记录调用，可注入结果/信封错误/异常。"""

    def __init__(
        self,
        result: dict | None = None,
        error: str | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result
        self._error = error
        self._exc = exc

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if self._exc is not None:
            raise self._exc
        if self._error is not None:
            return {"error": self._error}
        return dict(self.result or {})


@pytest.fixture()
def db_session():
    """datasets 切片表的内存库会话（试取只触达 datasets 面 + console 平台库）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    DatasetsBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session):
    """覆写 datasets 会话 + 引擎工厂依赖的 TestClient（console 身份真验签）。"""
    def override_get_db():
        yield db_session

    reset_ai_runtime()
    app.dependency_overrides[get_datasets_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUST, "cust-001"), (SUB_OTHER, "cust-002")],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


@pytest.fixture()
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture()
def wired_engine(engine):
    """把假引擎接进工厂覆盖（cell 档用例用；api 档用例换被调即抛的工厂）。"""
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: engine
    )
    return engine


def make_dataset(
    db_session,
    *,
    dataset_id: str = "ds-1",
    customer_id: str = "cust-001",
    key: str = "sales",
    tier: str = "api",
    columns=(("region", "TEXT"), ("amount", "REAL")),
    rows=(("east", 10.5), ("west", 20.75), ("north", None)),
) -> str:
    """直插数据集登记 + api 层列/行（不走上传通道，聚焦试取执行）。"""
    dataset = Dataset(
        id=dataset_id,
        customer_id=customer_id,
        key=key,
        name=key,
        source_kind="upload",
        storage_tier=tier,
        columns_json=[{"name": name, "type": dtype} for name, dtype in columns],
    )
    db_session.add(dataset)
    db_session.flush()
    for ordinal, (name, dtype) in enumerate(columns):
        db_session.add(DatasetColumn(dataset_id=dataset.id, name=name, dtype=dtype, ordinal=ordinal))
    for index, values in enumerate(rows):
        db_session.add(
            DatasetRow(
                dataset_id=dataset.id,
                row_index=index,
                data={name: value for (name, _dtype), value in zip(columns, values)},
            )
        )
    db_session.commit()
    return dataset.id


def preview(client, payload: dict, headers: dict = CUST):
    return client.post("/api/v1/artifacts/query-preview", json=payload, headers=headers)


# ------------------------------------------------------------------ 合法试取（api 档）


def test_preview_api_tier_local_execution_zero_engine(
    client, db_session, wired_engine
) -> None:
    """api 档合法试取：本地执行零引擎调用；快照与刷新同款归一（投影列序、
    None/null、数值原样）；响应带归一后 query（缺省补全）。"""
    make_dataset(db_session)

    resp = preview(
        client,
        {
            "dataset_id": "ds-1",
            "filters": [{"column": "amount", "op": "gte", "value": 10}],
            "sort": [{"column": "amount", "dir": "desc"}],
            "limit": 2,
            "columns": ["amount", "region"],
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 投影列序 = columns 声明序；排序/过滤后取前 2 行（amount 降序）
    assert body["snapshot"] == {
        "columns": ["amount", "region"],
        "rows": [[20.75, "west"], [10.5, "east"]],
    }
    assert body["query"] == {
        "dataset_id": "ds-1",
        "filters": [{"column": "amount", "op": "gte", "value": 10}],
        "sort": [{"column": "amount", "dir": "desc"}],
        "limit": 2,
        "columns": ["amount", "region"],
    }
    assert wired_engine.calls == []  # 免费档全程零引擎


def test_preview_minimal_query_defaults(client, db_session, wired_engine) -> None:
    """只带 dataset_id：filters/sort 补空、limit 缺省 100、columns 缺省全列
    （按列注册表 ordinal 序投影）。"""
    make_dataset(db_session)

    resp = preview(client, {"dataset_id": "ds-1"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == {
        "dataset_id": "ds-1",
        "filters": [],
        "sort": [],
        "limit": 100,
        "columns": None,
    }
    assert body["snapshot"]["columns"] == ["region", "amount"]
    assert body["snapshot"]["rows"][2] == ["north", None]  # 空值如实 null
    assert wired_engine.calls == []


# ------------------------------------------------------------------ 非法 422


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 缺 dataset_id
        {"dataset_id": ""},  # 空 dataset_id
        {"dataset_id": "ds-1", "filters": [{"column": "a", "op": "ne", "value": 1}]},  # 白名单外算子
        {"dataset_id": "ds-1", "sort": [{"column": "a", "dir": "DESC2"}]},  # 方向非法
        {"dataset_id": "ds-1", "limit": "100"},  # limit 非数值
        {"dataset_id": "ds-1", "columns": ["a", 2]},  # 列名非字符串
        {"dataset_id": 42},  # dataset_id 非字符串
    ],
    ids=["missing-dataset", "empty-dataset", "bad-op", "bad-dir", "bad-limit", "bad-columns", "non-str-dataset"],
)
def test_preview_invalid_query_422_no_engine(client, wired_engine, payload) -> None:
    """非法契约如实拒：422 带原因（讲清契约面），不发起任何引擎调用。"""
    resp = preview(client, payload)

    assert resp.status_code == 422, resp.text
    assert "查询契约非法" in resp.json()["detail"]
    assert wired_engine.calls == []


def test_preview_non_object_body_422(client) -> None:
    """非对象 body（数组）→ 422（FastAPI 形态校验，同样零引擎调用）。"""
    resp = client.post("/api/v1/artifacts/query-preview", json=["ds-1"], headers=CUST)
    assert resp.status_code == 422


# ------------------------------------------------------------------ 404 隔离


def test_preview_cross_tenant_dataset_404(client, db_session, wired_engine) -> None:
    """跨客户数据集 → 404（不泄露存在性）；不存在的数据集同样 404。"""
    make_dataset(db_session, dataset_id="ds-foreign", customer_id="cust-002", key="foreign")

    resp = preview(client, {"dataset_id": "ds-foreign"})

    assert resp.status_code == 404
    assert "ds-foreign" in resp.json()["detail"]
    missing = preview(client, {"dataset_id": "ds-nope"})
    assert missing.status_code == 404
    assert wired_engine.calls == []


# ------------------------------------------------------------------ 失败 400 带原因


def test_preview_missing_projection_column_400(client, db_session, wired_engine) -> None:
    """投影列在响应列中不存在 → 400「列不存在」（与刷新面板级失败同文案）。"""
    make_dataset(db_session)

    resp = preview(client, {"dataset_id": "ds-1", "columns": ["amount", "nope"]})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "列不存在: nope"
    assert wired_engine.calls == []  # api 档：失败发生在本地投影，零引擎


def test_preview_local_query_error_400(client, db_session, wired_engine) -> None:
    """免费窄面拒绝（过滤引用不存在的列）→ 400 带原因（LocalQueryError）。"""
    make_dataset(db_session)

    resp = preview(
        client,
        {"dataset_id": "ds-1", "filters": [{"column": "ghost", "op": "eq", "value": 1}]},
    )

    assert resp.status_code == 400, resp.text
    assert "ghost" in resp.json()["detail"]
    assert wired_engine.calls == []


@pytest.mark.parametrize(
    "failing",
    [
        FakeEngine(exc=EngineUnavailable("customer cust-001 的 cell 未开通")),
        FakeEngine(exc=EngineCallError("引擎端点 502")),
        FakeEngine(error="列类型不匹配"),
    ],
    ids=["unavailable", "call-error", "envelope-error"],
)
def test_preview_engine_failure_400_with_reason(client, db_session, failing) -> None:
    """cell 档引擎失败（不可达/调用错/信封 error）→ 400 带原因（试取是调试面，
    失败原因如实递回，不静默）。"""
    make_dataset(db_session, dataset_id="ds-cell", key="cell_src", tier="cell")
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: failing
    )
    try:
        resp = preview(client, {"dataset_id": "ds-cell"})
    finally:
        app.dependency_overrides.pop(get_engine_client_factory, None)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]


# ------------------------------------------------------------------ cell 档引擎路径


def test_preview_cell_tier_engine_call_shape(client, db_session, wired_engine) -> None:
    """cell 档试取经引擎 query：TOOL_QUERY 契约形参（dataset_key + 归一后
    filters/sort/limit，不带渲染字段），快照按引擎响应列投影。"""
    make_dataset(db_session, dataset_id="ds-cell", key="cell_sales", tier="cell")
    wired_engine.result = {
        "columns": [{"name": "year", "type": "TEXT"}, {"name": "gdp", "type": "REAL"}],
        "rows": [{"year": "2023", "gdp": 129.4}, {"year": "2024", "gdp": 134.9}],
        "total": 2,
    }

    resp = preview(
        client,
        {
            "dataset_id": "ds-cell",
            "filters": [{"column": "year", "op": "gte", "value": "2023"}],
            "sort": [{"column": "gdp", "dir": "desc"}],
            "limit": 5,
            "columns": ["gdp"],
        },
    )

    assert resp.status_code == 200, resp.text
    assert wired_engine.calls == [
        (
            TOOL_QUERY,
            {
                "dataset_key": "cell_sales",
                "filters": [{"column": "year", "op": "gte", "value": "2023"}],
                "sort": [{"column": "gdp", "dir": "desc"}],
                "limit": 5,
            },
        )
    ]
    body = resp.json()
    assert body["snapshot"] == {"columns": ["gdp"], "rows": [[129.4], [134.9]]}
    assert body["query"]["columns"] == ["gdp"]


# ------------------------------------------------------------------ 鉴权


def test_preview_requires_auth(client) -> None:
    """缺 Bearer ID token 一律 401。"""
    assert client.post(
        "/api/v1/artifacts/query-preview", json={"dataset_id": "ds-1"}
    ).status_code == 401
