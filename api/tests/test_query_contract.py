"""查询契约单测（wire-dashboard-query-contract 任务 1.4）。

覆盖：
- ``parse_panel_query`` 宽容解析边界：非法算子/空 dataset_id/limit 夹取/
  列去重保序/结构上限/未知键容忍；
- ``contract_refresh_plan``：信封/本体两种形状、panels 非数组空计划、
  非法 query 面板不进计划；
- ``refresh_spec_with_contracts``：快照值归一（None/bool/嵌套/数值）、
  投影列序与列缺失失败、api 档本地执行零引擎、cell 档引擎调用形态与
  三类引擎失败、面板级失败隔离、全失败抛 RefreshFailed、kept 面板
  原样保留、信封形状原样返回且入参 spec 不被就地修改。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.artifacts import query_contract
from fd_wire_api.artifacts.refresh import RefreshFailed
from fd_wire_api.datasets.ingest import EngineCallError, EngineUnavailable, TOOL_QUERY
from fd_wire_api.datasets.models import Dataset, DatasetColumn, DatasetRow

CUST = "cust-001"
OTHER = "cust-002"


# ------------------------------------------------------------------ 解析边界


class TestParsePanelQuery:
    def test_full_query_normalized(self) -> None:
        """全字段合法：归一为固定键序，value 任意原样保留。"""
        parsed = query_contract.parse_panel_query(
            {
                "dataset_id": "ds-1",
                "filters": [{"column": "region", "op": "eq", "value": "east"}],
                "sort": [{"column": "amount", "dir": "desc"}],
                "limit": 20,
                "columns": ["amount", "region"],
            }
        )
        assert parsed == {
            "dataset_id": "ds-1",
            "filters": [{"column": "region", "op": "eq", "value": "east"}],
            "sort": [{"column": "amount", "dir": "desc"}],
            "limit": 20,
            "columns": ["amount", "region"],
        }

    def test_minimal_query_gets_defaults(self) -> None:
        """只带 dataset_id：filters/sort 空、limit 100、columns 缺省 None。"""
        parsed = query_contract.parse_panel_query({"dataset_id": "ds-1"})
        assert parsed == {
            "dataset_id": "ds-1",
            "filters": [],
            "sort": [],
            "limit": 100,
            "columns": None,
        }

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "ds-1",
            [],  # 非 dict 一律丢弃
            {},  # 缺 dataset_id
            {"dataset_id": ""},  # 空 dataset_id
            {"dataset_id": "   "},  # 空白 dataset_id
            {"dataset_id": 42},  # 非字符串
            {"dataset_id": "ds", "filters": "x=1"},  # filters 非数组
            {"dataset_id": "ds", "filters": [{"column": "a", "op": "ne", "value": 1}]},  # 算子不在白名单
            {"dataset_id": "ds", "filters": [{"op": "eq", "value": 1}]},  # 过滤缺列
            {"dataset_id": "ds", "filters": [{"column": "a", "value": 1}]},  # 过滤缺算子
            {"dataset_id": "ds", "sort": [{"column": "a", "dir": "DESC2"}]},  # 方向非法
            {"dataset_id": "ds", "limit": "100"},  # limit 非数值
            {"dataset_id": "ds", "limit": 10.5},  # limit 非整数值
            {"dataset_id": "ds", "limit": True},  # bool 不是合法 limit
            {"dataset_id": "ds", "columns": "amount"},  # columns 非数组
            {"dataset_id": "ds", "columns": ["a", 2]},  # 列名非字符串
            {"dataset_id": "ds", "columns": ["a", ""]},  # 空列名
        ],
    )
    def test_invalid_query_dropped(self, raw) -> None:
        """任一字段非法 → 整个 query 丢弃（None，不抛）。"""
        assert query_contract.parse_panel_query(raw) is None

    def test_limit_clamped_into_1_1000(self) -> None:
        """limit 夹取：0→1、10000→1000、边界值 1/1000 原样。"""
        for raw, expected in [(0, 1), (-5, 1), (10000, 1000), (1, 1), (1000, 1000), (100.0, 100)]:
            parsed = query_contract.parse_panel_query({"dataset_id": "ds", "limit": raw})
            assert parsed is not None and parsed["limit"] == expected

    def test_columns_dedup_preserving_order(self) -> None:
        """列去重保序（投影列序=快照列序，重复无意义）。"""
        parsed = query_contract.parse_panel_query(
            {"dataset_id": "ds", "columns": ["b", "a", "b", "c", "a"]}
        )
        assert parsed["columns"] == ["b", "a", "c"]

    def test_structure_limits_reject_not_truncate(self) -> None:
        """filters>50 / sort>10 → 整体非法（与数据面 QueryIn max_length 同界）。"""
        filters = [{"column": "a", "op": "eq", "value": i} for i in range(51)]
        assert query_contract.parse_panel_query({"dataset_id": "ds", "filters": filters}) is None
        sort = [{"column": "a", "dir": "asc"} for _ in range(11)]
        assert query_contract.parse_panel_query({"dataset_id": "ds", "sort": sort}) is None
        # 恰在上限内合法
        assert query_contract.parse_panel_query(
            {"dataset_id": "ds", "filters": filters[:50], "sort": sort[:10]}
        ) is not None

    def test_tolerates_unknown_keys_and_defaults(self) -> None:
        """未知键容忍（只保留契约面）；缺 dir 补 asc；value 缺键视同 null。"""
        parsed = query_contract.parse_panel_query(
            {"dataset_id": "ds", "offset": 5, "filters": [{"column": "a", "op": "eq"}], "sort": [{"column": "b"}]}
        )
        assert parsed == {
            "dataset_id": "ds",
            "filters": [{"column": "a", "op": "eq", "value": None}],
            "sort": [{"column": "b", "dir": "asc"}],
            "limit": 100,
            "columns": None,
        }


# ------------------------------------------------------------------ 计划提取


class TestContractRefreshPlan:
    def test_envelope_and_body_shapes(self) -> None:
        """信封 {title, spec:{panels}} 与本体 {title, panels} 都能提取。"""
        query = {"dataset_id": "ds-1"}
        envelope = {"title": "t", "spec": {"panels": [{"id": "p1", "query": query}]}}
        body = {"title": "t", "panels": [{"id": "p1", "query": query}]}
        for spec in (envelope, body):
            plan = query_contract.contract_refresh_plan(spec)
            assert len(plan) == 1
            assert plan[0][0] == 0 and plan[0][1]["id"] == "p1"
            assert plan[0][2]["dataset_id"] == "ds-1"

    def test_inner_object_without_panels_falls_back_to_top_level(self) -> None:
        """spec.spec 是对象但无 panels 数组 → 顶层（web 宽容口径同界）。"""
        spec = {
            "title": "t",
            "spec": {"indicator": "X"},
            "panels": [{"id": "p1", "query": {"dataset_id": "ds-1"}}],
        }
        assert len(query_contract.contract_refresh_plan(spec)) == 1

    def test_no_panels_array_means_empty_plan(self) -> None:
        """panels 非数组（旧自由 JSON）→ 空计划（刷新走 AI 路径）。"""
        assert query_contract.contract_refresh_plan({"title": "t", "spec": {"indicator": "X"}}) == []
        assert query_contract.contract_refresh_plan("not-a-dict") == []

    def test_invalid_query_panel_excluded(self) -> None:
        """带非法 query 的面板不进计划（宽容丢弃，与无 query 同待遇）。"""
        spec = {
            "title": "t",
            "spec": {
                "panels": [
                    {"id": "p1", "query": {"dataset_id": "ds-1"}},
                    {"id": "p2", "query": {"dataset_id": ""}},  # 非法
                    {"id": "p3"},  # 无 query
                    "garbage-entry",  # 非对象条目跳过
                ]
            },
        }
        plan = query_contract.contract_refresh_plan(spec)
        assert [item[1]["id"] for item in plan] == ["p1"]


# ------------------------------------------------------------------ 执行


class FakeEngine:
    """引擎客户端假件：记录调用，可按需返回结果/信封错误/抛异常。"""

    def __init__(self, result: dict | None = None, error: str | None = None, exc: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._result = result
        self._error = error
        self._exc = exc

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if self._exc is not None:
            raise self._exc
        if self._error is not None:
            return {"error": self._error}
        return dict(self._result or {})


@pytest.fixture()
def db():
    """datasets 切片表的内存库会话（契约执行只触达 datasets 面）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from fd_wire_api.datasets.db import DatasetsBase

    DatasetsBase.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def make_dataset(
    db,
    *,
    dataset_id: str = "ds-1",
    customer_id: str = CUST,
    key: str = "sales",
    tier: str = "api",
    columns: list[tuple[str, str]] | None = None,
    rows: list[dict] | None = None,
) -> str:
    """直插数据集登记 + api 层列/行（不走上传通道，聚焦契约执行）。"""
    columns = columns if columns is not None else [("region", "TEXT"), ("amount", "REAL")]
    rows = rows if rows is not None else [{"region": "east", "amount": 10.5}]
    dataset = Dataset(
        id=dataset_id,
        customer_id=customer_id,
        key=key,
        name=key,
        source_kind="upload",
        storage_tier=tier,
        columns_json=[{"name": name, "type": dtype} for name, dtype in columns],
    )
    db.add(dataset)
    db.flush()
    for ordinal, (name, dtype) in enumerate(columns):
        db.add(DatasetColumn(dataset_id=dataset.id, name=name, dtype=dtype, ordinal=ordinal))
    for index, row in enumerate(rows):
        db.add(DatasetRow(dataset_id=dataset.id, row_index=index, data=row))
    db.commit()
    return dataset.id


def make_panel(pid: str, *, query: dict | None = None, snapshot: dict | None = None) -> dict:
    panel = {
        "id": pid,
        "type": "table",
        "title": pid,
        "span": 1,
        "source": "销售明细",
        "snapshot": snapshot or {"columns": ["旧列"], "rows": [["旧值"]]},
    }
    if query is not None:
        panel["query"] = query
    return panel


def run_contracts(db, spec: dict, engine=None):
    engine = engine if engine is not None else FakeEngine()
    return query_contract.refresh_spec_with_contracts(
        db, spec=spec, customer_id=CUST, engine_factory=lambda customer_id: engine
    )


class TestApiTierExecution:
    def test_snapshot_values_normalized_and_projected(self, db) -> None:
        """快照归一：None→null、bool/嵌套→str 不丢数据、数值/字符串原样；
        投影列序=columns 声明序（与响应列序不同也照契约）。"""
        make_dataset(
            db,
            columns=[("region", "TEXT"), ("flag", "TEXT"), ("meta", "TEXT"), ("amount", "REAL")],
            rows=[
                {"region": "east", "flag": True, "meta": {"k": 1}, "amount": 10.5},
                {"region": None, "flag": False, "meta": [1, 2], "amount": 3},
            ],
        )
        spec = {
            "title": "看板",
            "spec": {
                "panels": [
                    make_panel(
                        "p1",
                        query={
                            "dataset_id": "ds-1",
                            "columns": ["amount", "region", "flag", "meta"],
                        },
                    )
                ]
            },
        }
        new_spec, results = run_contracts(db, spec)

        assert results == [{"panel_id": "p1", "status": "refreshed"}]
        snapshot = new_spec["spec"]["panels"][0]["snapshot"]
        assert snapshot["columns"] == ["amount", "region", "flag", "meta"]
        assert snapshot["rows"] == [
            [10.5, "east", "True", "{'k': 1}"],
            [3, None, "False", "[1, 2]"],
        ]
        # 面板其余字段不动（query/source/type 逐字保留）
        panel = new_spec["spec"]["panels"][0]
        assert panel["query"] == spec["spec"]["panels"][0]["query"]
        assert panel["source"] == "销售明细" and panel["type"] == "table"

    def test_default_projection_follows_response_column_order(self, db) -> None:
        """缺省全列：按响应列序（dataset_columns 的 ordinal 序）投影。"""
        make_dataset(db, columns=[("b", "TEXT"), ("a", "TEXT")], rows=[{"b": "1", "a": "2"}])
        _, results = run_contracts(
            db, {"title": "t", "spec": {"panels": [make_panel("p1", query={"dataset_id": "ds-1"})]}}
        )
        assert results[0]["status"] == "refreshed"

    def test_missing_projection_column_fails_panel(self, db) -> None:
        """投影列在响应列中不存在 → 面板级 failed「列不存在」，原快照保留
        （旁边健康面板照常刷新，失败不外溢）。"""
        make_dataset(db)
        spec = {
            "title": "t",
            "spec": {
                "panels": [
                    make_panel("p1", query={"dataset_id": "ds-1", "columns": ["amount", "nope"]}),
                    make_panel("p2", query={"dataset_id": "ds-1"}),
                ]
            },
        }
        new_spec, results = run_contracts(db, spec)

        assert results == [
            {"panel_id": "p1", "status": "failed", "error": "列不存在: nope"},
            {"panel_id": "p2", "status": "refreshed"},
        ]
        assert new_spec["spec"]["panels"][0]["snapshot"] == {"columns": ["旧列"], "rows": [["旧值"]]}

    def test_local_query_error_fails_panel(self, db) -> None:
        """api 窄面拒绝（过滤引用不存在的列）→ 面板级 failed 带原因。"""
        make_dataset(db)
        spec = {
            "title": "t",
            "spec": {
                "panels": [
                    make_panel(
                        "p1",
                        query={
                            "dataset_id": "ds-1",
                            "filters": [{"column": "ghost", "op": "eq", "value": 1}],
                        },
                    ),
                    make_panel("p2", query={"dataset_id": "ds-1"}),
                ]
            },
        }
        _, results = run_contracts(db, spec)
        assert results[0]["status"] == "failed"
        assert "ghost" in results[0]["error"]
        assert results[1]["status"] == "refreshed"

    def test_zero_engine_calls_on_api_tier(self, db) -> None:
        """免费档（api tier）全程零引擎调用（引擎假件零调用记录）。"""
        make_dataset(db, tier="api")
        engine = FakeEngine()
        run_contracts(
            db,
            {"title": "t", "spec": {"panels": [make_panel("p1", query={"dataset_id": "ds-1"})]}},
            engine=engine,
        )
        assert engine.calls == []


class TestCellTierExecution:
    def test_engine_call_shape_and_projection(self, db) -> None:
        """cell 档：TOOL_QUERY 带 dataset_key/filters/sort/limit（契约形参）；
        响应行 dict 形态按投影列取值。"""
        make_dataset(db, dataset_id="ds-cell", key="cell_sales", tier="cell")
        engine = FakeEngine(
            result={
                "columns": [{"name": "year", "type": "TEXT"}, {"name": "gdp", "type": "REAL"}],
                "rows": [{"year": "2023", "gdp": 129.4}, {"year": "2024", "gdp": 134.9}],
                "total": 2,
            }
        )
        spec = {
            "title": "t",
            "spec": {
                "panels": [
                    make_panel(
                        "p1",
                        query={
                            "dataset_id": "ds-cell",
                            "filters": [{"column": "year", "op": "gte", "value": "2023"}],
                            "sort": [{"column": "gdp", "dir": "desc"}],
                            "limit": 5,
                            "columns": ["gdp"],
                        },
                    )
                ]
            },
        }
        new_spec, results = run_contracts(db, spec, engine=engine)

        assert engine.calls == [
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
        assert results == [{"panel_id": "p1", "status": "refreshed"}]
        assert new_spec["spec"]["panels"][0]["snapshot"] == {
            "columns": ["gdp"],
            "rows": [[129.4], [134.9]],
        }

    def test_engine_array_rows_positional_projection(self, db) -> None:
        """行数组形态（引擎契约演化防御）：按响应列序取位投影。"""
        make_dataset(db, dataset_id="ds-cell", key="k", tier="cell")
        engine = FakeEngine(
            result={
                "columns": [{"name": "a", "type": "TEXT"}, {"name": "b", "type": "TEXT"}],
                "rows": [["x", "y"]],
            }
        )
        new_spec, _ = run_contracts(
            db,
            {
                "title": "t",
                "spec": {
                    "panels": [
                        make_panel("p1", query={"dataset_id": "ds-cell", "columns": ["b", "a"]})
                    ]
                },
            },
            engine=engine,
        )
        assert new_spec["spec"]["panels"][0]["snapshot"] == {"columns": ["b", "a"], "rows": [["y", "x"]]}

    @pytest.mark.parametrize(
        "engine",
        [
            FakeEngine(exc=EngineUnavailable("customer cust-001 的 cell 未开通")),
            FakeEngine(exc=EngineCallError("引擎端点 502")),
            FakeEngine(error="列类型不匹配"),
        ],
        ids=["unavailable", "call-error", "envelope-error"],
    )
    def test_engine_failures_isolate_panel(self, db, engine) -> None:
        """引擎不可达/调用失败/信封 error → 面板级 failed 带原因（旁边
        api 档面板本地照常刷新，不受引擎故障波及）。"""
        make_dataset(db)  # api 档健康面板（本地执行，零引擎）
        make_dataset(db, dataset_id="ds-cell", key="k", tier="cell")
        spec = {
            "title": "t",
            "spec": {
                "panels": [
                    make_panel("p1", query={"dataset_id": "ds-cell"}),
                    make_panel("p2", query={"dataset_id": "ds-1"}),
                ]
            },
        }
        _, results = run_contracts(db, spec, engine=engine)
        assert results[0]["status"] == "failed"
        assert results[0]["error"]
        assert results[1]["status"] == "refreshed"


class TestFailureIsolationAndKept:
    def test_mixed_panels_partial_success(self, db) -> None:
        """混合面板：好面板 refreshed、坏面板 failed 保留原快照、无 query kept。"""
        make_dataset(db)
        spec = {
            "title": "看板",
            "spec": {
                "panels": [
                    make_panel("p1", query={"dataset_id": "ds-1", "columns": ["region"]}),
                    make_panel("p2", query={"dataset_id": "ds-missing"}),
                    make_panel("p3"),
                ]
            },
        }
        new_spec, results = run_contracts(db, spec)

        assert results == [
            {"panel_id": "p1", "status": "refreshed"},
            {"panel_id": "p2", "status": "failed", "error": "数据集不存在: ds-missing"},
            {"panel_id": "p3", "status": "kept"},  # kept 不带 error 字段
        ]
        panels = new_spec["spec"]["panels"]
        assert panels[0]["snapshot"] == {"columns": ["region"], "rows": [["east"]]}
        assert panels[1]["snapshot"] == {"columns": ["旧列"], "rows": [["旧值"]]}
        assert panels[2]["snapshot"] == {"columns": ["旧列"], "rows": [["旧值"]]}

    def test_all_contract_panels_failed_raises(self, db) -> None:
        """全部契约面板失败 → RefreshFailed（消息含各面板原因摘要）。"""
        spec = {
            "title": "t",
            "spec": {
                "panels": [
                    make_panel("p1", query={"dataset_id": "ds-a"}),
                    make_panel("p2", query={"dataset_id": "ds-b"}),
                    make_panel("p3"),  # kept 不算契约面板，不影响全失败判定
                ]
            },
        }
        with pytest.raises(RefreshFailed) as excinfo:
            run_contracts(db, spec)
        message = str(excinfo.value)
        assert "p1" in message and "ds-a" in message
        assert "p2" in message and "ds-b" in message

    def test_cross_tenant_dataset_is_not_found(self, db) -> None:
        """他企数据集 → 面板级 failed「数据集不存在」（不泄露存在性）。"""
        make_dataset(db)
        make_dataset(db, dataset_id="ds-other", customer_id=OTHER, key="other")
        _, results = run_contracts(
            db,
            {
                "title": "t",
                "spec": {
                    "panels": [
                        make_panel("p1", query={"dataset_id": "ds-other"}),
                        make_panel("p2", query={"dataset_id": "ds-1"}),
                    ]
                },
            },
        )
        assert results == [
            {"panel_id": "p1", "status": "failed", "error": "数据集不存在: ds-other"},
            {"panel_id": "p2", "status": "refreshed"},
        ]

    def test_spec_not_mutated_in_place_and_shape_preserved(self, db) -> None:
        """入参 spec 不被就地修改（版本行对象安全）；信封/本体形状原样返回。"""
        make_dataset(db)
        envelope = {
            "title": "看板",
            "spec": {"panels": [make_panel("p1", query={"dataset_id": "ds-1", "columns": ["amount"]})]},
        }
        body = {"title": "看板", "panels": [dict(envelope["spec"]["panels"][0])]}
        original_rows = [["旧值"]]

        new_envelope, _ = run_contracts(db, envelope)
        assert envelope["spec"]["panels"][0]["snapshot"]["rows"] == original_rows  # 原件未动
        assert set(new_envelope) == {"title", "spec"}  # 信封形状保持

        new_body, _ = run_contracts(db, body)
        assert set(new_body) == {"title", "panels"}  # 本体形状保持
        assert new_body["panels"][0]["snapshot"]["columns"] == ["amount"]
