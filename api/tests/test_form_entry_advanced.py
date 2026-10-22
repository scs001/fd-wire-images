"""wire-form-entry-advanced 后端契约与提交执法测试（任务组 1/3 + 2.1/2.2/3.4）。

覆盖：
- 1.1 三新键（optionsFromDataset/fillFrom/computed）形状与互斥校验矩阵
  （select 三源恰其一、fillFrom 源必须为动态选项 select、computed 引用
  类型约束与自引用拒——错误定位到字段）；
- 1.2 spec 级 constraints 校验矩阵（id 唯一、message ≤500、when 同形、
  probe 算子白名单、value 标量|数组（仅 in）|{field}、expect 枚举、
  ≤10 条/≤10 filters、引用存在性二趟）；
- 1.3 新键引用数据集属主校验（optionsFromDataset.datasetId 与
  probe.datasetId——他企/不存在 422 定位引用位，零半写）；
- 1.4 存量零漂移（不带新键的保存/加载行为逐字一致的显式锚点）；
- 2.1 服务端选项解析（免费档本地 distinct / cell 档引擎 TOOL_QUERY 双档）：
  去重非空值、升序、limit/截断、引用失效空集+错误标注；
- 2.2 console 查询通道验证：POST /api/v1/datasets/{id}/query 以 console
  身份带 filters+distinct_column 可解析选项（数据面既有轨道，免新端点）；
- 3.3 corrections 可选 artifact_id：归属裁决 → 当前版本全量字段校验+提交闸
  （命中 422 带 constraint id+message 零写入；值归一落 insert）；不携带
  零漂移（表单视角非法的值照旧 201）；
- 3.4 cell 档引擎调用上界：每次提交动态选项解析 + ≤10 probe 的引擎
  TOOL_QUERY 调用 ≤11 次（design D5 上界）。

环境：ai（artifacts 两表）与 datasets 两库同引擎合库（生产分库拓扑由路由
依赖签名保证——get_db 与 get_datasets_db/get_ai_db 各自注入，照
test_form_artifacts 先例）；引擎假件按工具脚本化返回。
"""

from __future__ import annotations

import copy
import os

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.artifacts.form_runtime import (
    resolve_dataset_options,
    validate_form_submission,
)
from fd_wire_api.artifacts.form_spec import validate_form_spec, validate_form_values
from fd_wire_api.datasets.db import (
    DatasetsBase,
    get_db as get_datasets_db,
)
from fd_wire_api.datasets.ingest import (
    TOOL_CORRECTION_ADD,
    TOOL_QUERY,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset, DatasetColumn, DatasetCorrection, DatasetRow

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-entry-adv-001"
SUB_OTHER = "sub-entry-adv-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))


class ScriptedEngine:
    """cell 档引擎假件：记录全部 tools/call，按工具脚本化返回
    （dict=每次深拷贝返回；Exception 即抛；未脚本化工具回 correction_id）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, copy.deepcopy(arguments)))
        result = self.responses.get(tool, {"correction_id": 7})
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def calls_for(self, tool: str) -> list[dict]:
        return [arguments for name, arguments in self.calls if name == tool]


@pytest.fixture()
def db_session():
    """独立内存库（ai + artifacts + datasets 契约面表同引擎）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AiBase.metadata.create_all(bind=engine)
    DatasetsBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def engine_fake() -> ScriptedEngine:
    return ScriptedEngine()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session, engine_fake):
    """覆写 ai/datasets 两库会话与引擎工厂假件的 TestClient。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_ai_db] = override_get_db
    app.dependency_overrides[get_datasets_db] = override_get_db
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: engine_fake
    )
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


# ------------------------------------------------------------------ 流程助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = "cust-001",
    key: str = "ref_src",
    tier: str = "api",
    columns: list[dict] | None = None,
) -> str:
    """直插一条数据集登记（含 api 层列注册表），返回 dataset id。"""
    columns = columns if columns is not None else [
        {"name": "name", "type": "TEXT"},
        {"name": "interval_days", "type": "INTEGER"},
    ]
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=f"{key} 数据源",
        source_kind="upload",
        storage_tier=tier,
        columns_json=columns,
    )
    db_session.add(dataset)
    db_session.flush()
    for ordinal, column in enumerate(columns):
        db_session.add(
            DatasetColumn(
                dataset_id=dataset.id,
                name=column["name"],
                dtype=column["type"],
                ordinal=ordinal,
            )
        )
    db_session.commit()
    return dataset.id


def seed_rows(db_session, dataset_id: str, rows: list[dict]) -> None:
    """直插基行（api 层行存储）。"""
    for index, row in enumerate(rows):
        db_session.add(DatasetRow(dataset_id=dataset_id, row_index=index, data=row))
    db_session.commit()


def blank_form(client, *, title: str = "农事登记") -> dict:
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "form", "title": title}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def put_spec(client, artifact_id: int, inner: dict, *, title: str = "农事登记"):
    return client.put(
        f"/api/v1/artifacts/{artifact_id}",
        json={"spec": {"title": title, "spec": inner}},
        headers=CUST,
    )


def corrections_rows(db_session, dataset_id: str) -> list[DatasetCorrection]:
    return list(
        db_session.execute(
            select(DatasetCorrection).where(DatasetCorrection.dataset_id == dataset_id)
        ).scalars().all()
    )


# --------------------------------------------- 1.1 optionsFromDataset 校验矩阵


def _dynamic_select_spec(**extra) -> dict:
    """单 select 字段 spec（extra 覆盖字段键）。"""
    field: dict = {
        "column": "pesticide",
        "label": "农药",
        "input": "select",
        "required": True,
        "optionsFromDataset": {
            "datasetId": "ds-arch",
            "column": "name",
        },
    }
    field.update(extra)
    return {"datasetId": None, "fields": [field]}


def test_validate_form_spec_options_from_dataset_matrix() -> None:
    """optionsFromDataset 形状矩阵：合法形态通过、三源互斥、非 select 拒、
    limit 1..500、filters 形状（数组仅 in）。"""
    # 合法：最小形态 / 带过滤器与上限
    assert validate_form_spec(_dynamic_select_spec()) is None
    full = _dynamic_select_spec(
        optionsFromDataset={
            "datasetId": "ds-arch",
            "column": "name",
            "filters": [
                {"column": "category", "op": "eq", "value": "杀虫剂"},
                {"column": "name", "op": "in", "value": ["药A", "药B"]},
                {"column": "interval_days", "op": "lte", "value": 30},
            ],
            "limit": 50,
        }
    )
    assert validate_form_spec(full) is None
    # 非对象 / datasetId / column
    assert validate_form_spec(_dynamic_select_spec(optionsFromDataset=["x"])) == (
        "fields[0].optionsFromDataset 必须是 JSON 对象（{datasetId, column, filters?, limit?}）"
    )
    assert validate_form_spec(
        _dynamic_select_spec(optionsFromDataset={"column": "name"})
    ) == "fields[0].optionsFromDataset.datasetId 必须是非空字符串（引用数据集 id）"
    assert validate_form_spec(
        _dynamic_select_spec(optionsFromDataset={"datasetId": "ds-arch"})
    ) == "fields[0].optionsFromDataset.column 必须是非空字符串（选项列名）"
    # limit：bool/越界/非整数
    for bad in (True, 0, 501, 1.5, "50"):
        spec = _dynamic_select_spec(
            optionsFromDataset={"datasetId": "ds-arch", "column": "name", "limit": bad}
        )
        assert validate_form_spec(spec) == (
            f"fields[0].optionsFromDataset.limit 必须是 1..500 的整数（缺省 200，收到 {bad!r}）"
        ), bad
    # 边界：1 与 500 合法
    for ok in (1, 500):
        spec = _dynamic_select_spec(
            optionsFromDataset={"datasetId": "ds-arch", "column": "name", "limit": ok}
        )
        assert validate_form_spec(spec) is None, ok
    # filters：算子白名单 / 数组仅 in / null 与容器拒 / 缺 value
    spec = _dynamic_select_spec(
        optionsFromDataset={
            "datasetId": "ds-arch",
            "column": "name",
            "filters": [{"column": "c", "op": "contains", "value": "x"}],
        }
    )
    assert validate_form_spec(spec) == (
        "fields[0].optionsFromDataset.filters[0].op 必须是 eq/in/gt/gte/lt/lte（收到 'contains'）"
    )
    spec = _dynamic_select_spec(
        optionsFromDataset={
            "datasetId": "ds-arch",
            "column": "name",
            "filters": [{"column": "c", "op": "eq", "value": ["a", "b"]}],
        }
    )
    assert validate_form_spec(spec) == (
        "fields[0].optionsFromDataset.filters[0].value 数组形态仅 op=in 可用（收到 op='eq'）"
    )
    for bad_value in (["a", ""], []):
        spec = _dynamic_select_spec(
            optionsFromDataset={
                "datasetId": "ds-arch",
                "column": "name",
                "filters": [{"column": "c", "op": "in", "value": bad_value}],
            }
        )
        assert validate_form_spec(spec) == (
            f"fields[0].optionsFromDataset.filters[0].value 必须是非空标量数组（收到 {bad_value!r}）"
        ), bad_value
    # null 与容器形态按标量口径拒
    for bad_value in (None, {"field": "x"}):
        spec = _dynamic_select_spec(
            optionsFromDataset={
                "datasetId": "ds-arch",
                "column": "name",
                "filters": [{"column": "c", "op": "in", "value": bad_value}],
            }
        )
        assert validate_form_spec(spec) == (
            f"fields[0].optionsFromDataset.filters[0].value 必须是非空标量（收到 {bad_value!r}）"
        ), bad_value
    spec = _dynamic_select_spec(
        optionsFromDataset={
            "datasetId": "ds-arch",
            "column": "name",
            "filters": [{"column": "c", "op": "eq"}],
        }
    )
    assert validate_form_spec(spec) == (
        "fields[0].optionsFromDataset.filters[0].value 必须是非空标量或标量数组（op=eq 必带）"
    )
    # 三源互斥：options+optionsFromDataset / optionsFrom+optionsFromDataset
    both_static = _dynamic_select_spec(options=["药A"])
    assert validate_form_spec(both_static) == (
        "fields[0] 同时携带 options 与 optionsFromDataset——两者互斥"
    )
    both_link = _dynamic_select_spec(
        optionsFrom={"field": "x", "cases": {"a": ["1"]}},
    )
    # 需要一个被引用字段存在——单字段 spec 下 optionsFrom.field 引用不存在，
    # 但互斥错误在首趟先于引用二趟报出
    assert validate_form_spec(both_link) == (
        "fields[0] 同时携带 optionsFrom 与 optionsFromDataset——两者互斥"
    )
    # 既有互斥文案零漂移（options × optionsFrom）
    legacy_both = {
        "datasetId": None,
        "fields": [
            {
                "column": "a",
                "label": "甲",
                "input": "select",
                "options": ["x"],
                "optionsFrom": {"field": "b", "cases": {"c": ["y"]}},
            }
        ],
    }
    assert validate_form_spec(legacy_both) == "fields[0] 同时携带 options 与 optionsFrom——两者互斥"
    # 非 select 携带拒
    alien = {
        "datasetId": None,
        "fields": [
            {
                "column": "a",
                "label": "甲",
                "input": "text",
                "optionsFromDataset": {"datasetId": "ds", "column": "name"},
            }
        ],
    }
    assert validate_form_spec(alien) == (
        "fields[0].optionsFromDataset 仅 input=select 字段可携带（收到 'text'）"
    )


# ---------------------------------------------------- 1.1 fillFrom / computed


def _farm_fields(arch_dataset_id: str = "ds-arch", **overrides) -> list[dict]:
    """农药 select（动态源）+ 间隔期 number + 记录日 date + 截止日 date。"""
    pesticide: dict = {
        "column": "pesticide",
        "label": "农药",
        "input": "select",
        "required": True,
        "optionsFromDataset": {"datasetId": arch_dataset_id, "column": "name"},
    }
    interval: dict = {"column": "interval_days", "label": "安全间隔期天数", "input": "number"}
    record_day: dict = {"column": "record_day", "label": "记录日期", "input": "date"}
    deadline: dict = {"column": "deadline", "label": "截止日期", "input": "date"}
    fields = [pesticide, interval, record_day, deadline]
    for column, extra in overrides.items():
        for field in fields:
            if field["column"] == column:
                field.update(extra)
    return fields


def test_validate_form_spec_fill_from_matrix() -> None:
    """fillFrom 形状与引用矩阵：field 须为带 optionsFromDataset 的 select、
    不得自引用；column 非空字符串。"""
    spec = {"datasetId": None, "fields": _farm_fields(interval_days={"fillFrom": {"field": "pesticide", "column": "interval_days"}})}
    assert validate_form_spec(spec) is None
    # 非对象 / field / column
    bad = {"datasetId": None, "fields": _farm_fields(interval_days={"fillFrom": ["x"]})}
    assert validate_form_spec(bad) == "fields[1].fillFrom 必须是 JSON 对象（{field, column}）"
    bad = {"datasetId": None, "fields": _farm_fields(interval_days={"fillFrom": {"column": "interval_days"}})}
    assert validate_form_spec(bad) == (
        "fields[1].fillFrom.field 必须是非空字符串（同表单带 optionsFromDataset 的 select 字段列）"
    )
    bad = {"datasetId": None, "fields": _farm_fields(interval_days={"fillFrom": {"field": "pesticide"}})}
    assert validate_form_spec(bad) == (
        "fields[1].fillFrom.column 必须是非空字符串（引用数据集回填列名）"
    )
    # 自引用拒
    bad = {
        "datasetId": None,
        "fields": [
            {
                "column": "pesticide",
                "label": "农药",
                "input": "select",
                "optionsFromDataset": {"datasetId": "ds-arch", "column": "name"},
                "fillFrom": {"field": "pesticide", "column": "interval_days"},
            }
        ],
    }
    assert validate_form_spec(bad) == "fields[0].fillFrom.field 不能自引用本字段: pesticide"
    # 引用不存在列 / 引用非动态源 select（静态 options）/ 引用非 select
    bad = {"datasetId": None, "fields": _farm_fields(interval_days={"fillFrom": {"field": "ghost", "column": "c"}})}
    assert validate_form_spec(bad) == "fields[1].fillFrom.field 引用了不存在的字段列: ghost"
    static_fields = [
        {
            "column": "region",
            "label": "区域",
            "input": "select",
            "required": True,
            "options": ["east"],
        },
        {"column": "note", "label": "备注", "input": "text", "fillFrom": {"field": "region", "column": "c"}},
    ]
    assert validate_form_spec({"datasetId": None, "fields": static_fields}) == (
        "fields[1].fillFrom.field 必须引用带 optionsFromDataset 的 select 字段（收到 'region'）"
    )
    non_select = {"datasetId": None, "fields": _farm_fields(interval_days={"fillFrom": {"field": "record_day", "column": "c"}})}
    assert validate_form_spec(non_select) == (
        "fields[1].fillFrom.field 必须引用带 optionsFromDataset 的 select 字段（收到 'record_day'）"
    )


def test_validate_form_spec_computed_matrix() -> None:
    """computed 形状与引用矩阵：op 枚举、date 须另一 date 字段、days 须
    number 字段或非负整数字面量、自引用拒。"""
    spec = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "record_day", "days": "interval_days"}})}
    assert validate_form_spec(spec) is None
    literal = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "record_day", "days": 7}})}
    assert validate_form_spec(literal) is None
    # op 枚举 / date / days 形状
    bad = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "sub_days", "date": "record_day", "days": 7}})}
    assert validate_form_spec(bad) == "fields[3].computed.op 必须是 add_days（收到 'sub_days'）"
    bad = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "days": 7}})}
    assert validate_form_spec(bad) == "fields[3].computed.date 必须是非空字符串（同表单 input=date 字段列）"
    for bad_days in (True, 1.5, "  ", None):
        spec = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "record_day", "days": bad_days}})}
        assert validate_form_spec(spec) == (
            f"fields[3].computed.days 必须是非空字符串（input=number 字段列）或非负整数字面量（收到 {bad_days!r}）"
        ), bad_days
    # 负整数字面量：更具体的文案
    spec = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "record_day", "days": -1}})}
    assert validate_form_spec(spec) == "fields[3].computed.days 字面量必须是非负整数（收到 -1）"
    # date 引用非 date 字段 / 不存在 / 自引用；days 引用非 number / 不存在 / 自引用
    bad = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "pesticide", "days": 7}})}
    assert validate_form_spec(bad) == "fields[3].computed.date 必须引用 input=date 字段（收到 'pesticide'）"
    bad = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "ghost", "days": 7}})}
    assert validate_form_spec(bad) == "fields[3].computed.date 引用了不存在的字段列: ghost"
    bad = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "deadline", "days": 7}})}
    assert validate_form_spec(bad) == "fields[3].computed.date 不能自引用本字段: deadline"
    bad = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "record_day", "days": "record_day"}})}
    assert validate_form_spec(bad) == (
        "fields[3].computed.days 必须引用 input=number 字段或给非负整数字面量（收到 'record_day'）"
    )
    bad = {"datasetId": None, "fields": _farm_fields(deadline={"computed": {"op": "add_days", "date": "record_day", "days": "ghost"}})}
    assert validate_form_spec(bad) == "fields[3].computed.days 引用了不存在的字段列: ghost"


# --------------------------------------------------------- 1.2 constraints


def _constraint_spec(constraints: object, **field_extra) -> dict:
    fields = _farm_fields(**field_extra)
    return {
        "datasetId": "ds-target",
        "fields": fields,
        "constraints": constraints,
    }


def test_validate_form_spec_constraints_matrix() -> None:
    """constraints 校验矩阵：id 唯一、message 长度、when 同形、probe
    算子白名单与 value 形状、expect 枚举、≤10 条/≤10 filters、引用二趟。"""
    good = [
        {
            "id": "no-harvest-in-interval",
            "message": "采收日期仍在农药安全间隔期内，禁止采收",
            "when": {"field": "pesticide", "op": "notEmpty"},
            "probe": {
                "datasetId": "ds-spray",
                "filters": [
                    {"column": "deadline", "op": "gte", "value": {"field": "record_day"}},
                ],
                "expect": "none",
            },
        }
    ]
    assert validate_form_spec(_constraint_spec(good)) is None
    # 字面量与数组与 {field} 三种 value 形态全合法
    good_values = [
        {
            "id": "c1",
            "message": "m",
            "probe": {
                "datasetId": "ds-spray",
                "filters": [
                    {"column": "a", "op": "eq", "value": "x"},
                    {"column": "b", "op": "in", "value": ["x", 1]},
                    {"column": "c", "op": "gte", "value": {"field": "interval_days"}},
                ],
                "expect": "any",
            },
        }
    ]
    assert validate_form_spec(_constraint_spec(good_values)) is None
    # 非数组 / 条数上限
    assert validate_form_spec(_constraint_spec({"id": "c"})) == (
        "spec.spec.constraints 必须是数组（[{id, message, when?, probe}]）"
    )
    eleven = [
        {"id": f"c{i}", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}}
        for i in range(11)
    ]
    assert validate_form_spec(_constraint_spec(eleven)) == (
        "spec.spec.constraints 不能超过 10 条（收到 11 条）"
    )
    # id：空/重复
    bad = [{"id": "  ", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].id 必须是非空字符串（表单内唯一）"
    )
    dup = [
        {"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}},
        {"id": "c", "message": "m2", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}},
    ]
    assert validate_form_spec(_constraint_spec(dup)) == "constraints[1].id 重复: c"
    # message：空/超长
    bad = [{"id": "c", "message": "  ", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].message 必须是非空字符串（命中拦截时呈现给填写者）"
    )
    bad = [{"id": "c", "message": "x" * 501, "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].message 长度超过 500 上限"
    )
    # when：非对象/越枚举（复用单条件校验器文案）
    bad = [{"id": "c", "message": "m", "when": ["x"], "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].when 必须是 JSON 对象（{field, op, value?}）"
    )
    # probe：datasetId/filters 形状/expect 枚举
    bad = [{"id": "c", "message": "m", "probe": {"filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].probe.datasetId 必须是非空字符串（探测数据集 id）"
    )
    bad = [{"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].probe.filters 必须是非空数组（1..10 条）"
    )
    eleven_filters = [{"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}] * 11, "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(eleven_filters)) == (
        "constraints[0].probe.filters 不能超过 10 条（收到 11 条）"
    )
    bad = [{"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "sometimes"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].probe.expect 必须是 none/any（收到 'sometimes'）"
    )
    # probe filter：算子白名单 / {field} 形状 / 数组仅 in
    bad = [{"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "contains", "value": "x"}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].probe.filters[0].op 必须是 eq/in/gt/gte/lt/lte（收到 'contains'）"
    )
    bad = [{"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": {"field": "x", "extra": 1}}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].probe.filters[0].value 引用形态必须是 {field: <本表单字段列>}"
        "（收到 {'field': 'x', 'extra': 1}）"
    )
    bad = [{"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": ["x"]}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].probe.filters[0].value 数组形态仅 op=in 可用（收到 op='eq'）"
    )
    # 引用二趟：when.field 与 probe {field} 引用不存在的列
    bad = [{"id": "c", "message": "m", "when": {"field": "ghost", "op": "notEmpty"}, "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": 1}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].when.field 引用了不存在的字段列: ghost"
    )
    bad = [{"id": "c", "message": "m", "probe": {"datasetId": "ds", "filters": [{"column": "a", "op": "eq", "value": {"field": "ghost"}}], "expect": "none"}}]
    assert validate_form_spec(_constraint_spec(bad)) == (
        "constraints[0].probe.filters[0].value.field 引用了不存在的字段列: ghost"
    )
    # 合法：{field} 引用前向字段（引用存在性在全列齐后裁决）
    forward = [
        {
            "id": "c",
            "message": "m",
            "probe": {
                "datasetId": "ds",
                "filters": [{"column": "a", "op": "eq", "value": {"field": "interval_days"}}],
                "expect": "any",
            },
        }
    ]
    assert validate_form_spec(_constraint_spec(forward)) is None


# ----------------------------------------------- 1.4 存量零漂移 + 1.3 属主


def test_legacy_spec_zero_drift_matrix() -> None:
    """存量零漂移锚点：不带新键的 spec 校验行为逐字不变（合法/非法抽样
    的既有文案原样）。"""
    legacy_ok = {
        "datasetId": "ds-0001",
        "fields": [
            {"column": "region", "label": "区域", "input": "select", "required": True, "options": ["east", "west"]},
            {"column": "amount", "label": "金额", "input": "number"},
            {"column": "note", "label": "备注", "input": "text"},
            {
                "column": "detail",
                "label": "明细",
                "input": "text",
                "visibleWhen": {"field": "region", "op": "eq", "value": "east"},
            },
        ],
        "submitLabel": "提交",
        "successMessage": "成功",
    }
    assert validate_form_spec(legacy_ok) is None
    # 既有非法文案抽样（新键校验不改变报错定位）
    assert validate_form_spec({"datasetId": None, "fields": [{"column": "a", "label": "甲", "input": "select"}]}) == (
        "fields[0].options 必须是非空字符串列表（input=select 必带）"
    )
    # validate_form_values 三参缺省（不带 dataset_options）行为不变
    fields = [
        {"column": "region", "label": "区域", "input": "select", "required": True, "options": ["east"]},
    ]
    assert validate_form_values(fields, {"region": "east"}) == ({"region": "east"}, None)
    values, error = validate_form_values(fields, {"region": "south"})
    assert values is None and "选项" in error


def test_new_key_dataset_owner_422_zero_half_write(client, db_session) -> None:
    """新键引用数据集属主校验：optionsFromDataset.datasetId 与
    probe.datasetId 他企/不存在 → 422 定位引用位、版本行不增；本企引用
    保存成功。主 datasetId 文案保持零漂移。"""
    own_id = seed_dataset(db_session, key="arch_own")
    other_id = seed_dataset(db_session, customer_id="cust-002", key="arch_other")
    artifact = blank_form(client)

    # 本企引用（optionsFromDataset + probe + fillFrom 链）保存成功
    inner = {
        "title": "农事登记",
        "datasetId": own_id,
        "fields": _farm_fields(
            own_id,
            interval_days={"fillFrom": {"field": "pesticide", "column": "interval_days"}},
        ),
        "constraints": [
            {
                "id": "gate-1",
                "message": "间隔期内禁采",
                "probe": {
                    "datasetId": own_id,
                    "filters": [{"column": "name", "op": "eq", "value": {"field": "pesticide"}}],
                    "expect": "none",
                },
            }
        ],
    }
    ok = put_spec(client, artifact["id"], inner)
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"] == 2

    # optionsFromDataset.datasetId 他企：422 定位引用位
    alien = {
        "title": "农事登记",
        "datasetId": own_id,
        "fields": _farm_fields(
            pesticide={"optionsFromDataset": {"datasetId": other_id, "column": "name"}},
        ),
    }
    resp = put_spec(client, artifact["id"], alien)
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"数据集 {other_id} 不存在（fields[0].optionsFromDataset.datasetId）"
    # 不存在同文案
    ghost = {
        "title": "农事登记",
        "datasetId": own_id,
        "fields": _farm_fields(
            pesticide={"optionsFromDataset": {"datasetId": "ds-nope", "column": "name"}},
        ),
    }
    resp = put_spec(client, artifact["id"], ghost)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "数据集 ds-nope 不存在（fields[0].optionsFromDataset.datasetId）"
    # probe.datasetId 他企：422 定位引用位
    bad_probe = {
        "title": "农事登记",
        "datasetId": own_id,
        "fields": _farm_fields(own_id),
        "constraints": [
            {
                "id": "gate-1",
                "message": "m",
                "probe": {
                    "datasetId": other_id,
                    "filters": [{"column": "name", "op": "eq", "value": 1}],
                    "expect": "none",
                },
            }
        ],
    }
    resp = put_spec(client, artifact["id"], bad_probe)
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"数据集 {other_id} 不存在（constraints[0].probe.datasetId）"
    # 主 datasetId 他企：既有文案零漂移（无引用位后缀）
    hijack = {"title": "农事登记", "datasetId": other_id, "fields": []}
    resp = put_spec(client, artifact["id"], hijack)
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"数据集 {other_id} 不存在"
    # 零半写：版本停在 2、版本链不增
    detail = client.get(f"/api/v1/artifacts/{artifact['id']}", headers=CUST).json()
    assert detail["version"] == 2
    versions = client.get(f"/api/v1/artifacts/{artifact['id']}/versions", headers=CUST).json()
    assert [v["version"] for v in versions] == [2, 1]


# --------------------------------------------------- 2.1 选项解析（双档直连）


def test_resolve_dataset_options_free_tier(db_session) -> None:
    """免费档解析：去重非空值、升序、limit/截断、filters 过滤、数值列
    stringify、引用失效（集删/列不在快照）= 空集+错误标注。"""
    dataset_id = seed_dataset(
        db_session,
        key="arch_free",
        columns=[
            {"name": "name", "type": "TEXT"},
            {"name": "category", "type": "TEXT"},
            {"name": "interval_days", "type": "INTEGER"},
        ],
    )
    seed_rows(
        db_session,
        dataset_id,
        [
            {"name": "药B", "category": "杀虫剂", "interval_days": 7},
            {"name": "药A", "category": "杀虫剂", "interval_days": 3},
            {"name": "药B", "category": "杀虫剂", "interval_days": 7},
            {"name": None, "category": "杀虫剂"},
            {"name": "", "category": "杀虫剂"},
            {"name": "除草剂X", "category": "除草剂", "interval_days": 10},
        ],
    )

    def resolve(source: dict) -> dict:
        return resolve_dataset_options(
            db_session, source=source, customer_id="cust-001", engine_factory=None
        )

    # 全量：去重非空、升序
    assert resolve({"datasetId": dataset_id, "column": "name"}) == {
        "options": ["药A", "药B", "除草剂X"],
        "truncated": False,
        "error": None,
    }
    # filters 过滤：只看杀虫剂
    filtered = resolve(
        {
            "datasetId": dataset_id,
            "column": "name",
            "filters": [{"column": "category", "op": "eq", "value": "杀虫剂"}],
        }
    )
    assert filtered == {"options": ["药A", "药B"], "truncated": False, "error": None}
    # limit 截断：limit=2 → 前两值 + truncated
    limited = resolve({"datasetId": dataset_id, "column": "name", "limit": 2})
    assert limited == {"options": ["药A", "药B"], "truncated": True, "error": None}
    # 数值列：去重 stringify 升序（3、7、10）
    numeric = resolve({"datasetId": dataset_id, "column": "interval_days"})
    assert numeric["options"] == ["3", "7", "10"]
    # 引用失效：列不在快照 → 空集 + 错误标注（不静默）
    drifted = resolve({"datasetId": dataset_id, "column": "ghost_col"})
    assert drifted["options"] == []
    assert drifted["truncated"] is False
    assert "ghost_col" in drifted["error"]
    # 引用失效：数据集不存在 → 空集 + 错误标注
    gone = resolve({"datasetId": "ds-nope", "column": "name"})
    assert gone["options"] == []
    assert "ds-nope" in gone["error"]
    # 他企数据集 → 空集 + 错误（不泄露属主差异）
    other_id = seed_dataset(db_session, customer_id="cust-002", key="arch_alien")
    alien = resolve({"datasetId": other_id, "column": "name"})
    assert alien["options"] == []
    assert other_id in alien["error"]


def test_resolve_dataset_options_cell_tier(db_session, engine_fake) -> None:
    """cell 档解析：TOOL_QUERY 报文带 dataset_key/filters/distinct_column；
    引擎 distinct 值去重 stringify；引擎失败 = 空集+错误标注。"""
    dataset_id = seed_dataset(db_session, key="arch_cell", tier="cell")
    engine_fake.responses[TOOL_QUERY] = {
        # 引擎 distinct 面原生升序（distinct + ORDER BY）；空值/重复由解析层剔除
        "columns": [{"name": "name", "type": "TEXT"}],
        "distinct_column": "name",
        "values": ["药A", "药B", "药B", "", None],
        "count": 3,
    }
    resolved = resolve_dataset_options(
        db_session,
        source={"datasetId": dataset_id, "column": "name"},
        customer_id="cust-001",
        engine_factory=lambda customer_id: engine_fake,
    )
    assert resolved == {"options": ["药A", "药B"], "truncated": False, "error": None}
    call = engine_fake.calls_for(TOOL_QUERY)[0]
    assert call["dataset_key"] == "arch_cell"
    assert call["distinct_column"] == "name"
    assert call["filters"] == []
    # 引擎失败（不可达）→ 空集 + 错误
    from fd_wire_api.datasets.ingest import EngineUnavailable

    engine_fake.responses[TOOL_QUERY] = EngineUnavailable("引擎不可达（模拟）")
    failed = resolve_dataset_options(
        db_session,
        source={"datasetId": dataset_id, "column": "name"},
        customer_id="cust-001",
        engine_factory=lambda customer_id: engine_fake,
    )
    assert failed["options"] == []
    assert failed["error"]


# ---------------------------------------- 2.2 console 查询通道（数据面既有轨道）


def test_console_query_channel_resolves_options(client, db_session, engine_fake) -> None:
    """console 身份经既有 POST /api/v1/datasets/{id}/query（filters+
    distinct_column）即可解析选项——数据面既有轨道免新端点（api 档本地、
    cell 档引擎同报文）。"""
    api_id = seed_dataset(db_session, key="channel_free")
    seed_rows(
        db_session,
        api_id,
        [{"name": "药B"}, {"name": "药A"}, {"name": "药B"}],
    )
    resp = client.post(
        f"/api/v1/datasets/{api_id}/query",
        headers=CUST,
        json={
            "filters": [],
            "distinct_column": "name",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["distinct_column"] == "name"
    assert body["values"] == ["药A", "药B"]
    assert engine_fake.calls == []  # api 档本地执行零引擎

    # cell 档同通道透传引擎（distinct_column 随 QueryIn 透传）
    cell_id = seed_dataset(db_session, key="channel_cell", tier="cell")
    engine_fake.responses[TOOL_QUERY] = {
        "columns": [{"name": "name", "type": "TEXT"}],
        "distinct_column": "name",
        "values": ["药A"],
        "count": 1,
    }
    resp2 = client.post(
        f"/api/v1/datasets/{cell_id}/query",
        headers=CUST,
        json={"filters": [{"column": "name", "op": "eq", "value": "药A"}], "distinct_column": "name"},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["values"] == ["药A"]
    assert engine_fake.calls_for(TOOL_QUERY)[-1]["distinct_column"] == "name"


# ------------------------------------------- 3.3 corrections 携 artifact_id 执法


def _spray_setup(client, db_session, *, tier: str = "api"):
    """打药记录集（探测源）+ 档案集（动态选项源）+ 采收表单（带闸）。

    返回 (form_inner_target_dataset_id, form_id, spray_dataset_id, arch_dataset_id)。"""
    spray_id = seed_dataset(
        db_session,
        key="spray_src",
        tier=tier,
        columns=[
            {"name": "plot", "type": "TEXT"},
            {"name": "deadline", "type": "TEXT"},
        ],
    )
    arch_id = seed_dataset(db_session, key="arch_src2", tier=tier)
    target_id = seed_dataset(
        db_session,
        key="harvest_target",
        tier=tier,
        columns=[
            {"name": "plot", "type": "TEXT"},
            {"name": "pesticide", "type": "TEXT"},
            {"name": "interval_days", "type": "INTEGER"},
            {"name": "record_day", "type": "TEXT"},
            {"name": "deadline", "type": "TEXT"},
        ],
    )
    fields = [
        {"column": "plot", "label": "地块", "input": "text", "required": True},
    ] + _farm_fields(arch_id)
    # 本用例需要「农药留空（when 假）」路径——动态 select 不设必填
    for field in fields:
        if field["column"] == "pesticide":
            field["required"] = False
    inner = {
        "title": "采收登记",
        "datasetId": target_id,
        "fields": fields,
        "constraints": [
            {
                "id": "no-harvest-in-interval",
                "message": "采收日期仍在农药安全间隔期内，禁止采收",
                "when": {"field": "pesticide", "op": "notEmpty"},
                "probe": {
                    "datasetId": spray_id,
                    "filters": [
                        {"column": "plot", "op": "eq", "value": {"field": "plot"}},
                        {"column": "deadline", "op": "gte", "value": {"field": "record_day"}},
                    ],
                    "expect": "none",
                },
            }
        ],
    }
    artifact = blank_form(client, title="采收登记")
    resp = put_spec(client, artifact["id"], inner, title="采收登记")
    assert resp.status_code == 200, resp.text
    return target_id, artifact["id"], spray_id, arch_id


def test_corrections_with_artifact_id_enforces_fields_and_gate(
    client, db_session
) -> None:
    """corrections 携 artifact_id：当前版本全量字段校验（必填/动态选项
    成员）+ 提交闸（{field} 代入、expect none 命中 422 带 id+message 零
    写入、when 假跳过）；通过后值归一落 insert。"""
    target_id, form_id, spray_id, arch_id = _spray_setup(client, db_session)
    seed_rows(db_session, arch_id, [{"name": "药A", "interval_days": 7}])
    url = f"/api/v1/datasets/{target_id}/corrections"

    # 字段校验：动态 select 值不在服务端实时解析选项内 → 422 零写入
    tampered = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": form_id,
            "values": {"plot": "P1", "pesticide": "伪造药"},
        },
    )
    assert tampered.status_code == 422, tampered.text
    assert "选项" in tampered.json()["detail"]
    assert corrections_rows(db_session, target_id) == []

    # 提交闸命中：打药记录覆盖采收日（deadline ≥ record_day）→ 422 带
    # constraint id 与 message，零写入
    seed_rows(
        db_session,
        spray_id,
        [{"plot": "P1", "deadline": "2026-10-20"}],
    )
    blocked = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": form_id,
            "values": {
                "plot": "P1",
                "pesticide": "药A",
                "interval_days": "7",  # number 字符串 → 归一 7
                "record_day": "2026-10-15",
                "deadline": "2026-10-22",
            },
        },
    )
    assert blocked.status_code == 422, blocked.text
    detail = blocked.json()["detail"]
    assert "no-harvest-in-interval" in detail
    assert "禁止采收" in detail
    assert corrections_rows(db_session, target_id) == []

    # when 假（pesticide 空 → 不探测）+ 非必填全空 → 通过；值归一落库
    ok = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": form_id,
            "values": {
                "plot": "P2",
                "interval_days": "7",
                "record_day": "2026-10-15",
            },
        },
    )
    assert ok.status_code == 201, ok.text
    rows = corrections_rows(db_session, target_id)
    assert len(rows) == 1
    assert rows[0].value_json == {
        "plot": "P2",
        "interval_days": 7,  # number 字符串已归一
        "record_day": "2026-10-15",
    }
    # 安全间隔期外（打药截止 2026-10-14 < 采收 2026-10-15）→ 探测零行放行
    db_session.execute(
        DatasetRow.__table__.delete().where(DatasetRow.dataset_id == spray_id)
    )
    seed_rows(db_session, spray_id, [{"plot": "P3", "deadline": "2026-10-14"}])
    passed = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": form_id,
            "values": {"plot": "P3", "pesticide": "药A", "record_day": "2026-10-15"},
        },
    )
    assert passed.status_code == 201, passed.text
    assert corrections_rows(db_session, target_id)[-1].value_json == {
        "plot": "P3",
        "pesticide": "药A",
        "record_day": "2026-10-15",
    }


def test_corrections_without_artifact_id_zero_drift(client, db_session) -> None:
    """不携带 artifact_id：行为与现状逐字一致——不做表单字段校验与提交闸
    （表单视角非法的 select 值照旧 201；值原样不归一）。"""
    target_id, _form_id, _spray_id, arch_id = _spray_setup(client, db_session)
    url = f"/api/v1/datasets/{target_id}/corrections"
    legacy = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "values": {"plot": "P1", "pesticide": "任何值都收"},
        },
    )
    assert legacy.status_code == 201, legacy.text
    row = corrections_rows(db_session, target_id)[0]
    # 表单视角非法的 select 值照原样落库（无字段校验、无提交闸——现状行为）
    assert row.value_json["pesticide"] == "任何值都收"


def test_corrections_artifact_id_mismatch_422_matrix(client, db_session) -> None:
    """artifact_id 校验不符 422 定位原因：他企/不存在、非 form kind、
    datasetId 与目标集不一致、op≠insert 携带。"""
    target_id, form_id, _spray, _arch = _spray_setup(client, db_session)
    url = f"/api/v1/datasets/{target_id}/corrections"

    # 不存在
    resp = client.post(
        url, headers=CUST, json={"op": "insert", "artifact_id": 99999, "values": {"plot": "P"}}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "表单产物 99999 不存在"

    # 他企（别家 form 产物——借 OTHER 身份提交）
    other_target = seed_dataset(db_session, customer_id="cust-002", key="other_target")
    resp = client.post(
        f"/api/v1/datasets/{other_target}/corrections",
        headers=OTHER,
        json={"op": "insert", "artifact_id": form_id, "values": {"plot": "P"}},
    )
    assert resp.status_code == 422
    assert "不存在" in resp.json()["detail"]

    # 非 form kind（dashboard 产物）
    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    resp = client.post(
        url, headers=CUST, json={"op": "insert", "artifact_id": board["id"], "values": {"plot": "P"}}
    )
    assert resp.status_code == 422
    assert "仅 form 产物" in resp.json()["detail"]

    # datasetId 与目标集不一致（form 绑 target_id，打向别的集）
    other_own = seed_dataset(db_session, key="another_target")
    resp = client.post(
        f"/api/v1/datasets/{other_own}/corrections",
        headers=CUST,
        json={"op": "insert", "artifact_id": form_id, "values": {"plot": "P"}},
    )
    assert resp.status_code == 422
    assert "不一致" in resp.json()["detail"]

    # op=update 携 artifact_id → 422（表单提交执法面仅 insert）
    resp = client.post(
        url,
        headers=CUST,
        json={"op": "update", "artifact_id": form_id, "row_id": "1", "column": "plot", "value": "x"},
    )
    assert resp.status_code == 422
    assert "仅 insert" in resp.json()["detail"]
    # op=delete 携带同拒
    resp = client.post(
        url, headers=CUST, json={"op": "delete", "artifact_id": form_id, "row_id": "1"}
    )
    assert resp.status_code == 422


def test_corrections_expect_any_and_when_matrix(client, db_session) -> None:
    """expect any（零行即拦）/ 字面量过滤 / when 真假两态直连共享函数。"""
    spray_id = seed_dataset(
        db_session,
        key="batch_src",
        columns=[{"name": "batch_no", "type": "TEXT"}, {"name": "plot", "type": "TEXT"}],
    )
    seed_rows(
        db_session,
        spray_id,
        [{"batch_no": "B-001", "plot": "P1"}],
    )
    inner = {
        "datasetId": "ds-any",
        "fields": [
            {"column": "batch_no", "label": "批次号", "input": "text", "required": True},
            {"column": "kind", "label": "类型", "input": "select", "required": True, "options": ["入库", "出库"]},
        ],
        "constraints": [
            {
                "id": "batch-must-exist",
                "message": "批次号必须已在台账中登记",
                "when": {"field": "kind", "op": "eq", "value": "出库"},
                "probe": {
                    "datasetId": spray_id,
                    "filters": [{"column": "batch_no", "op": "eq", "value": {"field": "batch_no"}}],
                    "expect": "any",
                },
            }
        ],
    }
    # 出库 + 不存在的批次 → 零行 → 拦
    normalized, error = validate_form_submission(
        inner,
        {"batch_no": "B-404", "kind": "出库"},
        datasets_db=db_session,
        customer_id="cust-001",
        engine_factory=None,
    )
    assert normalized is None
    assert error == "提交被约束 batch-must-exist 拦截：批次号必须已在台账中登记"
    # 出库 + 存在批次 → 过
    normalized, error = validate_form_submission(
        inner,
        {"batch_no": "B-001", "kind": "出库"},
        datasets_db=db_session,
        customer_id="cust-001",
        engine_factory=None,
    )
    assert error is None
    assert normalized == {"batch_no": "B-001", "kind": "出库"}
    # 入库（when 假）→ 不探测（批次不存在也过）
    normalized, error = validate_form_submission(
        inner,
        {"batch_no": "B-404", "kind": "入库"},
        datasets_db=db_session,
        customer_id="cust-001",
        engine_factory=None,
    )
    assert error is None


# ------------------------------------------------- 3.4 cell 档引擎调用上界


def test_submit_engine_call_upper_bound_cell_tier(client, db_session, engine_fake) -> None:
    """cell 档提交的引擎调用上界（design D5）：1 次动态选项解析 + ≤10
    probe 的 TOOL_QUERY 调用 ≤11 次，另加 1 次 TOOL_CORRECTION_ADD 落库；
    when 假的约束不产生探测调用。"""
    spray_id = seed_dataset(
        db_session,
        key="bound_spray",
        tier="cell",
        columns=[{"name": "plot", "type": "TEXT"}],
    )
    arch_id = seed_dataset(db_session, key="bound_arch", tier="cell")
    target_id = seed_dataset(db_session, key="bound_target", tier="cell")
    constraints = [
        {
            "id": f"gate-{index}",
            "message": f"闸 {index}",
            "when": {"field": "pesticide", "op": "notEmpty"},
            "probe": {
                "datasetId": spray_id,
                "filters": [{"column": "plot", "op": "eq", "value": {"field": "plot"}}],
                "expect": "none",
            },
        }
        for index in range(9)
    ] + [
        {
            "id": "gate-skip",
            "message": "when 假不探测",
            "when": {"field": "pesticide", "op": "eq", "value": "不存在的药"},
            "probe": {
                "datasetId": spray_id,
                "filters": [{"column": "plot", "op": "eq", "value": "P"}],
                "expect": "none",
            },
        }
    ]
    inner = {
        "title": "采收",
        "datasetId": target_id,
        "fields": [{"column": "plot", "label": "地块", "input": "text", "required": True}]
        + _farm_fields(arch_id),
        "constraints": constraints,
    }
    artifact = blank_form(client, title="采收")
    resp = put_spec(client, artifact["id"], inner, title="采收")
    assert resp.status_code == 200, resp.text
    engine_fake.calls.clear()
    # 引擎侧：distinct 返回药A（选项解析）；探测返回零行（none 放行）
    engine_fake.responses[TOOL_QUERY] = {
        "columns": [{"name": "name", "type": "TEXT"}],
        "distinct_column": "name",
        "values": ["药A"],
        "count": 1,
    }

    submitted = client.post(
        f"/api/v1/datasets/{target_id}/corrections",
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": artifact["id"],
            "values": {"plot": "P1", "pesticide": "药A", "record_day": "2026-10-15"},
        },
    )
    assert submitted.status_code == 201, submitted.text
    query_calls = engine_fake.calls_for(TOOL_QUERY)
    # 1 次选项解析 + 9 次 probe（when 假的 gate-skip 不探测）= 10 ≤ 11 上界
    assert len(query_calls) == 10
    assert len(query_calls) <= 11
    correction_calls = engine_fake.calls_for(TOOL_CORRECTION_ADD)
    assert len(correction_calls) == 1
    # 满配 10 条真探测也在上界内（补一条 when 真约束直连共享函数验证）
    probe_args = query_calls[-1]
    assert probe_args["filters"] == [{"column": "plot", "op": "eq", "value": "P1"}]
    assert probe_args["limit"] == 1


def test_submit_probe_ten_constraints_upper_bound_direct(db_session, engine_fake) -> None:
    """满配 10 条 when 真约束 + 1 个动态 select：TOOL_QUERY 恰 11 次
    （design D5 上界「≤10 probe × 1 + 选项解析 1」）。"""
    spray_id = seed_dataset(
        db_session,
        key="bound10_spray",
        tier="cell",
        columns=[{"name": "plot", "type": "TEXT"}],
    )
    arch_id = seed_dataset(db_session, key="bound10_arch", tier="cell")
    constraints = [
        {
            "id": f"g{i}",
            "message": "m",
            "probe": {
                "datasetId": spray_id,
                "filters": [{"column": "plot", "op": "eq", "value": "P1"}],
                "expect": "none",
            },
        }
        for i in range(10)
    ]
    inner = {
        "datasetId": "ds-x",
        "fields": [{"column": "plot", "label": "地块", "input": "text", "required": True}]
        + _farm_fields(arch_id),
        "constraints": constraints,
    }
    engine_fake.responses[TOOL_QUERY] = {
        "columns": [{"name": "name", "type": "TEXT"}],
        "distinct_column": "name",
        "values": ["药A"],
        "count": 1,
    }
    normalized, error = validate_form_submission(
        inner,
        {"plot": "P1", "pesticide": "药A"},
        datasets_db=db_session,
        customer_id="cust-001",
        engine_factory=lambda customer_id: engine_fake,
    )
    assert error is None
    assert normalized == {"plot": "P1", "pesticide": "药A"}
    assert len(engine_fake.calls_for(TOOL_QUERY)) == 11  # 1 选项 + 10 probe
    assert len(engine_fake.calls_for(TOOL_QUERY)) <= 11


def test_computed_and_fillfrom_submission_semantics(client, db_session) -> None:
    """computed 字段提交仅按 date 类型校验（不强制等于重算值——可手动
    修正语义）；fillFrom 回填值=普通提交值（按其 input 类型校验）。"""
    target_id = seed_dataset(
        db_session,
        key="compute_target",
        columns=[
            {"name": "pesticide", "type": "TEXT"},
            {"name": "interval_days", "type": "INTEGER"},
            {"name": "record_day", "type": "TEXT"},
            {"name": "deadline", "type": "TEXT"},
        ],
    )
    arch_id = seed_dataset(db_session, key="compute_arch")
    fields = _farm_fields(
        arch_id,
        interval_days={"fillFrom": {"field": "pesticide", "column": "interval_days"}},
        deadline={"computed": {"op": "add_days", "date": "record_day", "days": "interval_days"}, "required": True},
    )
    # 本用例聚焦 computed/fillFrom 字段的提交语义——动态 select 不设必填
    for field in fields:
        if field["column"] == "pesticide":
            field["required"] = False
    inner = {
        "title": "打药",
        "datasetId": target_id,
        "fields": fields,
    }
    artifact = blank_form(client, title="打药")
    resp = put_spec(client, artifact["id"], inner, title="打药")
    assert resp.status_code == 200, resp.text
    url = f"/api/v1/datasets/{target_id}/corrections"
    # computed 字段带合法日期（哪怕 ≠ 重算值 record+7=2026-10-22，手改 10-25）→ 过
    ok = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": artifact["id"],
            "values": {
                "record_day": "2026-10-15",
                "interval_days": 7,
                "deadline": "2026-10-25",
            },
        },
    )
    assert ok.status_code == 201, ok.text
    # computed 字段非法日期 → 422（YYYY-MM-DD）
    bad = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": artifact["id"],
            "values": {"record_day": "2026-10-15", "deadline": "2026/10/25"},
        },
    )
    assert bad.status_code == 422
    assert "YYYY-MM-DD" in bad.json()["detail"]
    # fillFrom 回填值按 number 校验：字符串数字归一、非数字拒
    ok2 = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": artifact["id"],
            "values": {"record_day": "2026-10-15", "interval_days": "7", "deadline": "2026-10-22"},
        },
    )
    assert ok2.status_code == 201, ok2.text
    bad2 = client.post(
        url,
        headers=CUST,
        json={
            "op": "insert",
            "artifact_id": artifact["id"],
            "values": {"record_day": "2026-10-15", "interval_days": "七天", "deadline": "2026-10-22"},
        },
    )
    assert bad2.status_code == 422
    assert "interval_days" in bad2.json()["detail"]
