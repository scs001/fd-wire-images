"""form 产物契约/出生/属主校验（wire-form-builder 任务 1.1–1.4）与
insert 修正共享落层（任务 2.1/2.2）的 API 测试。

覆盖：
- 空白创建 form：201 draft v1 = 零字段、datasetId=None 的最小 spec，审计/
  留痕与 dashboard 同款、无来源会话指针；kind 白名单外（其余四类 AI 产物
  kind）仍 422 零半写；title 校验对 form 同样生效；
- form spec 结构校验矩阵：validate_form_spec 纯函数逐条文案断言 + 路由
  422 零半写（版本行不增）；1.1b visibleWhen 保存校验矩阵（wire-form-
  field-linkage：九算子合法/形状错/带值错/自引用与不存在列拒）；1.1c
  visibleWhenGroup / optionsFrom 保存校验矩阵（wire-form-linkage-v2：
  组形状与互斥/映射形状与互斥/组内与 optionsFrom.field 引用二趟）；
- datasetId 属主校验（design D1 第②层）：绑定本企数据集保存成功且版本
  递增；他企/不存在 422 且零半写（版本行不增）；datasetId=None 构建中间
  态可保存；
- dashboard 语义零漂移：空白建板 + PUT 追加版本照旧（kind 分派不影响
  dashboard——form 形状内层不触发 form 校验）；
- submit_insert_correction（2.1 下沉直连断言）：api 档本地落层零引擎、
  值按 dtype 归一、空 values 抛 LocalQueryError；cell 档引擎透传报文
  逐项一致、缺 correction_id 抛 EngineCallError；corrections 路由两档
  insert 端到端仍通（下沉零漂移的主证 = 既有 corrections 用例零改动全绿，
  本文件补同环境直连）。

环境：ai（artifacts 两表）与 datasets 两库同引擎合库（生产分库拓扑由路由
依赖签名保证——get_db 与 get_datasets_db 各自注入，照 test_artifacts_refresh
先例）；引擎工厂假件记录 TOOL_CORRECTION_ADD 调用。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.app import app
from fd_wire_api.artifacts.form_spec import validate_form_spec
from fd_wire_api.datasets import service as datasets_service
from fd_wire_api.datasets.api_store import LocalQueryError
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.ingest import (
    TOOL_CORRECTION_ADD,
    EngineCallError,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset, DatasetColumn, DatasetCorrection

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-form-001"
SUB_OTHER = "sub-form-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))


class RecordingEngine:
    """cell 档引擎假件：记录 tools/call 调用，脚本化返回（Exception 即抛）。"""

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result: dict | Exception = result if result is not None else {
            "correction_id": 7
        }

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)


@pytest.fixture()
def db_session():
    """独立内存库（ai 三表 + artifacts 两表 + datasets 契约面表同引擎）。"""
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
def engine_fake() -> RecordingEngine:
    return RecordingEngine()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session, engine_fake):
    """覆写 ai/datasets 两库会话（同一 override——路由依赖签名与生产分库
    拓扑一致）与引擎工厂假件的 TestClient。"""
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


def blank_form(client, *, title: str = "访客登记", headers=CUST) -> dict:
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "form", "title": title}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def put_spec(client, artifact_id: int, spec: dict, *, headers=CUST):
    return client.put(
        f"/api/v1/artifacts/{artifact_id}", json={"spec": spec}, headers=headers
    )


def seed_dataset(
    db_session, *, customer_id: str = "cust-001", key: str = "form_src", tier: str = "api"
) -> str:
    """直插一条数据集登记（含 api 层列注册表），返回 dataset id。"""
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=f"{key} 数据源",
        source_kind="upload",
        storage_tier=tier,
        columns_json=[
            {"name": "region", "type": "TEXT"},
            {"name": "amount", "type": "REAL"},
            {"name": "qty", "type": "INTEGER"},
        ],
    )
    db_session.add(dataset)
    db_session.flush()
    for ordinal, column in enumerate(dataset.columns_json):
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


# ------------------------------------------------------- 1.1 结构校验纯函数


def test_validate_form_spec_matrix() -> None:
    """validate_form_spec 逐条规则与文案（None=合法）。"""
    # 合法：最小构建中间态（datasetId=None + 零字段）
    assert validate_form_spec({"datasetId": None, "fields": []}) is None
    # 合法：全字段形态（select 带 options、required 可省略）
    full = {
        "datasetId": "ds-0001",
        "fields": [
            {
                "column": "region",
                "label": "区域",
                "input": "select",
                "required": True,
                "options": ["east", "west"],
            },
            {"column": "amount", "label": "金额", "input": "number"},  # required 缺省
            {"column": "note", "label": "备注", "input": "text", "required": False},
            {"column": "day", "label": "日期", "input": "date"},
        ],
        "submitLabel": "提交登记",
        "successMessage": "登记成功",
    }
    assert validate_form_spec(full) is None

    # 非 dict
    assert validate_form_spec(["fields"]) == "spec.spec 必须是 JSON 对象"
    # datasetId：非 null 非字符串 / 空白串
    bad_dataset = "spec.spec.datasetId 必须是 null 或非空字符串（数据集 id）"
    assert validate_form_spec({"datasetId": 42, "fields": []}) == bad_dataset
    assert validate_form_spec({"datasetId": "  ", "fields": []}) == bad_dataset
    # fields 非数组
    assert validate_form_spec({"datasetId": None, "fields": {}}) == (
        "spec.spec.fields 必须是数组"
    )
    # 字段非对象
    assert validate_form_spec({"datasetId": None, "fields": ["x"]}) == (
        "fields[0] 必须是 JSON 对象"
    )
    # column 非空字符串 / 重复（报第二处出现位）
    assert validate_form_spec(
        {"datasetId": None, "fields": [{"label": "甲", "input": "text"}]}
    ) == "fields[0].column 必须是非空字符串（绑定数据集列名）"
    dup = {
        "datasetId": None,
        "fields": [
            {"column": "region", "label": "区域", "input": "text"},
            {"column": "amount", "label": "金额", "input": "number"},
            {"column": "region", "label": "区域", "input": "text"},
        ],
    }
    assert validate_form_spec(dup) == "fields[2].column 重复: region"
    # label 非空 / 超长
    assert validate_form_spec(
        {"datasetId": None, "fields": [{"column": "a", "input": "text"}]}
    ) == "fields[0].label 必须是非空字符串"
    assert validate_form_spec(
        {
            "datasetId": None,
            "fields": [{"column": "a", "label": "x" * 201, "input": "text"}],
        }
    ) == "fields[0].label 长度超过 200 上限"
    # input 越枚举
    assert validate_form_spec(
        {
            "datasetId": None,
            "fields": [{"column": "a", "label": "甲", "input": "checkbox"}],
        }
    ) == "fields[0].input 必须是 text/number/date/select（收到 'checkbox'）"
    # required 非布尔（可省略，缺省 false 语义）
    assert validate_form_spec(
        {
            "datasetId": None,
            "fields": [{"column": "a", "label": "甲", "input": "text", "required": 1}],
        }
    ) == "fields[0].required 必须是布尔值（可省略，缺省 false）"
    # select 的 options：缺失 / 空列表 / 含非字符串项
    bad_options = "fields[0].options 必须是非空字符串列表（input=select 必带）"
    assert validate_form_spec(
        {"datasetId": None, "fields": [{"column": "a", "label": "甲", "input": "select"}]}
    ) == bad_options
    assert validate_form_spec(
        {
            "datasetId": None,
            "fields": [{"column": "a", "label": "甲", "input": "select", "options": []}],
        }
    ) == bad_options
    assert validate_form_spec(
        {
            "datasetId": None,
            "fields": [{"column": "a", "label": "甲", "input": "select", "options": ["x", ""]}],
        }
    ) == bad_options
    # 顶层文案：空白串 / 超长（可选字段）
    assert validate_form_spec({"datasetId": None, "fields": [], "submitLabel": " "}) == (
        "spec.spec.submitLabel 必须是非空字符串"
    )
    assert validate_form_spec(
        {"datasetId": None, "fields": [], "successMessage": "x" * 201}
    ) == "spec.spec.successMessage 长度超过 200 上限"


# ------------------------- 1.1b visibleWhen 保存校验（wire-form-field-linkage）


def _linked_spec(rule: object) -> dict:
    """两字段 spec：channel 控制列（select 必填）+ detail 带 visibleWhen 规则。"""
    return {
        "datasetId": None,
        "fields": [
            {
                "column": "channel",
                "label": "渠道",
                "input": "select",
                "required": True,
                "options": ["其他", "电话"],
            },
            {
                "column": "detail",
                "label": "明细",
                "input": "text",
                "required": True,
                "visibleWhen": rule,
            },
        ],
    }


def test_validate_form_spec_visible_when_matrix() -> None:
    """visibleWhen 形状校验矩阵：九算子合法通过 + 形状/带值/引用错逐条拒。"""
    # 合法：带值算子逐一通过（value 字符串）
    for op in ("eq", "neq", "gt", "gte", "lt", "lte", "contains"):
        rule = {"field": "channel", "op": op, "value": "其他"}
        assert validate_form_spec(_linked_spec(rule)) is None, op
    # 合法：无值算子（不得带 value——不带即合法）
    for op in ("empty", "notEmpty"):
        assert validate_form_spec(_linked_spec({"field": "channel", "op": op})) is None, op
    # 合法：value 标量形态（int/float/bool 均为合法标量）
    for value in (1, 1.5, True, False):
        rule = {"field": "channel", "op": "eq", "value": value}
        assert validate_form_spec(_linked_spec(rule)) is None, value
    # 合法：前向引用（field[0] 引用后置字段列——引用存在性在全列齐后裁决）
    forward = {
        "datasetId": None,
        "fields": [
            {
                "column": "a",
                "label": "甲",
                "input": "text",
                "visibleWhen": {"field": "b", "op": "notEmpty"},
            },
            {"column": "b", "label": "乙", "input": "text"},
        ],
    }
    assert validate_form_spec(forward) is None

    # visibleWhen 非对象
    assert validate_form_spec(_linked_spec(["x"])) == (
        "fields[1].visibleWhen 必须是 JSON 对象（{field, op, value?}）"
    )
    # op 越枚举
    assert validate_form_spec(
        _linked_spec({"field": "channel", "op": "xor", "value": "1"})
    ) == (
        "fields[1].visibleWhen.op 必须是"
        " eq/neq/gt/gte/lt/lte/contains/empty/notEmpty（收到 'xor'）"
    )
    # empty/notEmpty 带 value 键拒
    for op in ("empty", "notEmpty"):
        rule = {"field": "channel", "op": op, "value": "x"}
        error = validate_form_spec(_linked_spec(rule))
        assert error == f"fields[1].visibleWhen.{op} 不得带 value 键", op
    # 带值算子缺 value 拒
    assert validate_form_spec(_linked_spec({"field": "channel", "op": "eq"})) == (
        "fields[1].visibleWhen.value 必须是非空标量（op=eq 必带）"
    )
    # value 空串/纯空白拒
    for bad in ("", "   "):
        rule = {"field": "channel", "op": "eq", "value": bad}
        error = validate_form_spec(_linked_spec(rule))
        assert error == (
            f"fields[1].visibleWhen.value 必须是非空标量（收到 {bad!r}）"
        ), bad
    # value 为 list/dict/null 拒
    for bad in (["x"], {"a": 1}, None):
        rule = {"field": "channel", "op": "eq", "value": bad}
        error = validate_form_spec(_linked_spec(rule))
        assert error == (
            f"fields[1].visibleWhen.value 必须是非空标量（收到 {bad!r}）"
        ), bad
    # field：缺失/空白串
    bad_field = "fields[1].visibleWhen.field 必须是非空字符串（同表单另一字段的 column）"
    assert validate_form_spec(_linked_spec({"op": "empty"})) == bad_field
    assert validate_form_spec(_linked_spec({"field": "  ", "op": "empty"})) == bad_field
    # field 自引用拒
    assert validate_form_spec(_linked_spec({"field": "detail", "op": "empty"})) == (
        "fields[1].visibleWhen.field 不能自引用本字段: detail"
    )
    # field 引用不存在的列拒
    assert validate_form_spec(_linked_spec({"field": "ghost", "op": "empty"})) == (
        "fields[1].visibleWhen.field 引用了不存在的字段列: ghost"
    )
    # 存量零回归：无 visibleWhen 的全字段形态 spec 全通过（显式锚点）
    legacy = {
        "datasetId": "ds-0001",
        "fields": [
            {
                "column": "region",
                "label": "区域",
                "input": "select",
                "required": True,
                "options": ["east", "west"],
            },
            {"column": "amount", "label": "金额", "input": "number"},
            {"column": "note", "label": "备注", "input": "text", "required": False},
            {"column": "day", "label": "日期", "input": "date"},
        ],
        "submitLabel": "提交登记",
        "successMessage": "登记成功",
    }
    assert validate_form_spec(legacy) is None


# ------------- 1.1c visibleWhenGroup / optionsFrom 保存校验（wire-form-linkage-v2）


def _grouped_spec(group: object) -> dict:
    """三字段 spec：kind(select) + amount(number) + detail 带 visibleWhenGroup。"""
    return {
        "datasetId": None,
        "fields": [
            {
                "column": "kind",
                "label": "类型",
                "input": "select",
                "required": True,
                "options": ["A", "B"],
            },
            {"column": "amount", "label": "金额", "input": "number"},
            {
                "column": "detail",
                "label": "明细",
                "input": "text",
                "required": True,
                "visibleWhenGroup": group,
            },
        ],
    }


def _condition(field: str, op: str, value: object = None, *, with_value: bool = True):
    """单条件捷径：无值算子不带 value 键。"""
    rule: dict = {"field": field, "op": op}
    if with_value:
        rule["value"] = value
    return rule


def _city_spec(city_field: dict) -> dict:
    """两字段 spec：province 控制列（select）+ 给定的 city 字段。"""
    return {
        "datasetId": None,
        "fields": [
            {
                "column": "province",
                "label": "省",
                "input": "select",
                "required": True,
                "options": ["浙江", "江苏", "新疆"],
            },
            city_field,
        ],
    }


def _city_field(**extra) -> dict:
    """city 字段捷径（select 必填；extra 覆盖/追加键）。"""
    field: dict = {
        "column": "city",
        "label": "市",
        "input": "select",
        "required": True,
    }
    field.update(extra)
    return field


def test_validate_form_spec_group_matrix() -> None:
    """visibleWhenGroup 形状校验矩阵：mode/条数/组内单条件逐条拒 +
    与 visibleWhen 互斥 + 组内引用二趟（文案带组内索引）。"""
    # 合法：all/any × 单条件 / 10 条（上限边界）
    for mode in ("all", "any"):
        group = {"mode": mode, "conditions": [_condition("kind", "eq", "A")]}
        assert validate_form_spec(_grouped_spec(group)) is None, mode
    ten = {
        "mode": "all",
        "conditions": [
            _condition("kind", "eq", "A"),
            _condition("amount", "gt", "0"),
        ]
        + [_condition("kind", "neq", "B") for _ in range(8)],
    }
    assert validate_form_spec(_grouped_spec(ten)) is None
    # 合法：组内无值算子/标量 value 形态（复用单条件口径）
    mixed = {
        "mode": "any",
        "conditions": [
            {"field": "kind", "op": "empty"},
            _condition("amount", "gte", 100),
        ],
    }
    assert validate_form_spec(_grouped_spec(mixed)) is None
    # 合法：组内前向引用（detail 引用后不存在的列——这里引用自身前的列即可；
    # 前向 = conditions 引用声明在其后的字段列，引用存在性在全列齐后裁决）
    forward = {
        "datasetId": None,
        "fields": [
            {
                "column": "a",
                "label": "甲",
                "input": "text",
                "visibleWhenGroup": {
                    "mode": "all",
                    "conditions": [{"field": "b", "op": "notEmpty"}],
                },
            },
            {"column": "b", "label": "乙", "input": "text"},
        ],
    }
    assert validate_form_spec(forward) is None

    # 非对象
    assert validate_form_spec(_grouped_spec(["x"])) == (
        "fields[2].visibleWhenGroup 必须是 JSON 对象（{mode, conditions}）"
    )
    # mode：缺失 / 越枚举
    assert validate_form_spec(
        _grouped_spec({"conditions": [_condition("kind", "eq", "A")]})
    ) == "fields[2].visibleWhenGroup.mode 必须是 all/any（收到 None）"
    assert validate_form_spec(
        _grouped_spec(
            {"mode": "xor", "conditions": [_condition("kind", "eq", "A")]}
        )
    ) == "fields[2].visibleWhenGroup.mode 必须是 all/any（收到 'xor'）"
    # conditions：非数组 / 空数组 / 11 条
    bad_conditions = (
        "fields[2].visibleWhenGroup.conditions 必须是非空数组（1..10 条）"
    )
    assert validate_form_spec(
        _grouped_spec({"mode": "all", "conditions": {"field": "kind"}})
    ) == bad_conditions
    assert validate_form_spec(
        _grouped_spec({"mode": "all", "conditions": []})
    ) == bad_conditions
    eleven = {
        "mode": "all",
        "conditions": [_condition("kind", "eq", "A") for _ in range(11)],
    }
    assert validate_form_spec(_grouped_spec(eleven)) == (
        "fields[2].visibleWhenGroup.conditions 不能超过 10 条（收到 11 条）"
    )
    # 组内单条件非法：定位带组内索引（复用单条件校验器与文案）
    assert validate_form_spec(
        _grouped_spec(
            {
                "mode": "all",
                "conditions": [
                    _condition("kind", "eq", "A"),
                    ["x"],
                ],
            }
        )
    ) == (
        "fields[2].visibleWhenGroup.conditions[1] 必须是 JSON 对象"
        "（{field, op, value?}）"
    )
    assert validate_form_spec(
        _grouped_spec(
            {
                "mode": "all",
                "conditions": [_condition("kind", "xor", "A")],
            }
        )
    ) == (
        "fields[2].visibleWhenGroup.conditions[0].op 必须是"
        " eq/neq/gt/gte/lt/lte/contains/empty/notEmpty（收到 'xor'）"
    )
    # 组内无值算子带 value 键拒（定位带组内索引）
    assert validate_form_spec(
        _grouped_spec(
            {
                "mode": "any",
                "conditions": [
                    {"field": "kind", "op": "empty", "value": "x"},
                ],
            }
        )
    ) == "fields[2].visibleWhenGroup.conditions[0].empty 不得带 value 键"
    # 组内带值算子缺 value 拒
    assert validate_form_spec(
        _grouped_spec(
            {"mode": "all", "conditions": [{"field": "kind", "op": "eq"}]}
        )
    ) == (
        "fields[2].visibleWhenGroup.conditions[0].value"
        " 必须是非空标量（op=eq 必带）"
    )
    # 与 visibleWhen 并存拒（互斥）
    both = {
        "datasetId": None,
        "fields": [
            {"column": "a", "label": "甲", "input": "text"},
            {
                "column": "b",
                "label": "乙",
                "input": "text",
                "visibleWhen": {"field": "a", "op": "notEmpty"},
                "visibleWhenGroup": {
                    "mode": "all",
                    "conditions": [{"field": "a", "op": "notEmpty"}],
                },
            },
        ],
    }
    assert validate_form_spec(both) == (
        "fields[1] 同时携带 visibleWhen 与 visibleWhenGroup——两者互斥"
    )
    # 组内引用二趟：自引用 / 不存在列（文案带组定位与组内索引）
    assert validate_form_spec(
        _grouped_spec(
            {
                "mode": "all",
                "conditions": [
                    _condition("kind", "eq", "A"),
                    _condition("detail", "eq", "x"),
                ],
            }
        )
    ) == (
        "fields[2].visibleWhenGroup.conditions[1].field 不能自引用本字段: detail"
    )
    assert validate_form_spec(
        _grouped_spec(
            {
                "mode": "all",
                "conditions": [
                    _condition("kind", "eq", "A"),
                    _condition("ghost", "eq", "x"),
                ],
            }
        )
    ) == (
        "fields[2].visibleWhenGroup.conditions[1].field"
        " 引用了不存在的字段列: ghost"
    )


def test_validate_form_spec_options_from_matrix() -> None:
    """optionsFrom 形状校验矩阵：field/cases/fallback 逐条拒 + 与静态
    options 互斥（恰有其一）+ 非 select 拒 + 引用二趟。"""
    mapping = {
        "field": "province",
        "cases": {"浙江": ["杭州", "宁波"], "江苏": ["南京"]},
        "fallback": ["其他"],
    }
    # 合法：带 fallback / 不带 fallback / 前向引用
    assert validate_form_spec(_city_spec(_city_field(optionsFrom=mapping))) is None
    no_fallback = {
        "field": "province",
        "cases": {"浙江": ["杭州"]},
    }
    assert (
        validate_form_spec(_city_spec(_city_field(optionsFrom=no_fallback))) is None
    )
    forward = {
        "datasetId": None,
        "fields": [
            {
                "column": "a",
                "label": "甲",
                "input": "select",
                "optionsFrom": {"field": "b", "cases": {"x": ["1"]}},
            },
            {"column": "b", "label": "乙", "input": "text"},
        ],
    }
    assert validate_form_spec(forward) is None

    # 非对象
    assert validate_form_spec(_city_spec(_city_field(optionsFrom=["x"]))) == (
        "fields[1].optionsFrom 必须是 JSON 对象（{field, cases, fallback?}）"
    )
    # field：缺失 / 空白串
    bad_field = "fields[1].optionsFrom.field 必须是非空字符串（同表单另一字段的 column）"
    assert validate_form_spec(
        _city_spec(_city_field(optionsFrom={"cases": {"浙江": ["杭州"]}}))
    ) == bad_field
    assert validate_form_spec(
        _city_spec(
            _city_field(
                optionsFrom={"field": "  ", "cases": {"浙江": ["杭州"]}}
            )
        )
    ) == bad_field
    # cases：缺失 / 非对象 / 空对象
    bad_cases = "fields[1].optionsFrom.cases 必须是非空对象（控制值 → 非空选项列表）"
    assert validate_form_spec(
        _city_spec(_city_field(optionsFrom={"field": "province"}))
    ) == bad_cases
    assert validate_form_spec(
        _city_spec(
            _city_field(
                optionsFrom={"field": "province", "cases": ["浙江"]}
            )
        )
    ) == bad_cases
    assert validate_form_spec(
        _city_spec(
            _city_field(optionsFrom={"field": "province", "cases": {}})
        )
    ) == bad_cases
    # cases 键 trim 后空白拒
    assert validate_form_spec(
        _city_spec(
            _city_field(
                optionsFrom={"field": "province", "cases": {"  ": ["杭州"]}}
            )
        )
    ) == (
        "fields[1].optionsFrom.cases 的键必须 trim 后非空（收到 '  '）"
    )
    # cases 值：非列表 / 空列表 / 列表含空串（同 options 列表口径）
    for bad_value in ("杭州", [], ["杭州", ""]):
        spec = _city_spec(
            _city_field(
                optionsFrom={"field": "province", "cases": {"浙江": bad_value}}
            )
        )
        assert validate_form_spec(spec) == (
            "fields[1].optionsFrom.cases['浙江'] 必须是非空字符串列表"
        ), bad_value
    # fallback：非列表 / 空列表 / 含空串
    for bad_fallback in ("其他", [], ["其他", ""]):
        spec = _city_spec(
            _city_field(
                optionsFrom={
                    "field": "province",
                    "cases": {"浙江": ["杭州"]},
                    "fallback": bad_fallback,
                }
            )
        )
        assert validate_form_spec(spec) == (
            "fields[1].optionsFrom.fallback 必须是非空字符串列表"
        ), bad_fallback

    # 与静态 options 并存拒（互斥）
    both = _city_spec(_city_field(options=["x"], optionsFrom=no_fallback))
    assert validate_form_spec(both) == "fields[1] 同时携带 options 与 optionsFrom——两者互斥"
    # 都无：现行文案不变（select 必带 options 或 optionsFrom 恰其一）
    assert validate_form_spec(_city_spec(_city_field())) == (
        "fields[1].options 必须是非空字符串列表（input=select 必带）"
    )
    # 非 select 字段携带 optionsFrom 拒
    alien = _city_spec(
        {
            "column": "city",
            "label": "市",
            "input": "text",
            "optionsFrom": no_fallback,
        }
    )
    assert validate_form_spec(alien) == (
        "fields[1].optionsFrom 仅 input=select 字段可携带（收到 'text'）"
    )
    # 引用二趟：自引用 / 不存在列
    self_ref = _city_spec(
        _city_field(optionsFrom={"field": "city", "cases": {"x": ["1"]}})
    )
    assert validate_form_spec(self_ref) == (
        "fields[1].optionsFrom.field 不能自引用本字段: city"
    )
    ghost_ref = _city_spec(
        _city_field(optionsFrom={"field": "ghost", "cases": {"x": ["1"]}})
    )
    assert validate_form_spec(ghost_ref) == (
        "fields[1].optionsFrom.field 引用了不存在的字段列: ghost"
    )


# ------------------------------------------------------------- 1.2 空白创建


def test_blank_create_form_success(client) -> None:
    """空白创建 form：201 draft v1 = 零字段、datasetId=None 最小 spec。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "form", "title": "访客登记"}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    assert artifact["kind"] == "form"
    assert artifact["title"] == "访客登记"
    assert artifact["status"] == "draft"
    assert artifact["version"] == 1
    assert artifact["spec"] == {
        "title": "访客登记",
        "spec": {"title": "访客登记", "datasetId": None, "fields": []},
    }
    # 直接创建：无来源会话指针；审计/留痕与 dashboard 空白创建同款
    assert artifact["origin_conversation_id"] is None
    assert artifact["status_history"][0]["status"] == "draft"
    assert artifact["status_history"][0]["by"] == "console:cust-001"
    assert "空白" in artifact["status_history"][0]["note"]
    versions = client.get(
        f"/api/v1/artifacts/{artifact['id']}/versions", headers=CUST
    ).json()
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["operator"] == "console:cust-001"
    # 他企不可见（列表隔离覆盖新 kind）
    assert client.get("/api/v1/artifacts", headers=OTHER).json() == []


def test_blank_create_whitelist_outside_kinds_422(client) -> None:
    """kind 白名单 = (dashboard, form, alert_rule)：其余三类 AI 产物 kind 422
    零半写（alert_rule 已放行空白出生——wire-alert-evaluation，模板壳断言
    在 test_artifacts_lifecycle 侧）。"""
    for kind in (
        "composite_indicator",
        "research_report",
        "refresh_schedule",
    ):
        resp = client.post(
            "/api/v1/artifacts", json={"kind": kind, "title": "x"}, headers=CUST
        )
        assert resp.status_code == 422, kind
    # title 校验对 form 同样生效（空白串 422）
    assert (
        client.post(
            "/api/v1/artifacts", json={"kind": "form", "title": "   "}, headers=CUST
        ).status_code
        == 422
    )
    # 拒收零半写
    assert client.get("/api/v1/artifacts", headers=CUST).json() == []


# ----------------------------------------------- 1.2/1.4 路由结构校验 + 零半写


def test_form_put_structural_422_zero_half_write(client) -> None:
    """form spec 结构校验矩阵（路由面）：422 带原因，版本行不增。"""
    artifact = blank_form(client)
    aid = artifact["id"]
    cases = [
        # 通用层：信封缺非空 title
        {"spec": {"datasetId": None, "fields": []}},
        {"title": "  ", "spec": {"datasetId": None, "fields": []}},
        # 通用层：spec.spec 空
        {"title": "访客登记"},
        # form 层：datasetId 类型 / fields 非数组 / 字段非对象
        {"title": "访客登记", "spec": {"datasetId": 42, "fields": []}},
        {"title": "访客登记", "spec": {"datasetId": None, "fields": {}}},
        {"title": "访客登记", "spec": {"datasetId": None, "fields": ["x"]}},
        # form 层：input 越枚举 / column 重复 / select 无 options / label 非空
        {
            "title": "访客登记",
            "spec": {
                "datasetId": None,
                "fields": [{"column": "a", "label": "甲", "input": "richtext"}],
            },
        },
        {
            "title": "访客登记",
            "spec": {
                "datasetId": None,
                "fields": [
                    {"column": "a", "label": "甲", "input": "text"},
                    {"column": "a", "label": "乙", "input": "text"},
                ],
            },
        },
        {
            "title": "访客登记",
            "spec": {
                "datasetId": None,
                "fields": [{"column": "a", "label": "甲", "input": "select"}],
            },
        },
        {
            "title": "访客登记",
            "spec": {
                "datasetId": None,
                "fields": [{"column": "a", "input": "text"}],
            },
        },
        # form 层：顶层文案空白串
        {"title": "访客登记", "spec": {"datasetId": None, "fields": [], "submitLabel": ""}},
    ]
    for spec in cases:
        resp = put_spec(client, aid, spec)
        assert resp.status_code == 422, spec
        assert resp.json()["detail"]
    # 零半写：版本不动、版本链仍只有 v1、当前 spec 保持出生态
    detail = client.get(f"/api/v1/artifacts/{aid}", headers=CUST).json()
    assert detail["version"] == 1
    assert detail["spec"] == artifact["spec"]
    versions = client.get(f"/api/v1/artifacts/{aid}/versions", headers=CUST).json()
    assert [v["version"] for v in versions] == [1]


# ------------------------------------------------------- 1.3 datasetId 属主校验


def test_form_dataset_binding_own_dataset_saves(client, db_session) -> None:
    """绑定本企数据集：保存成功、版本递增、spec 原样落版本。"""
    dataset_id = seed_dataset(db_session, key="own_src")
    artifact = blank_form(client)
    spec_v2 = {
        "title": "访客登记",
        "spec": {
            "title": "访客登记",
            "datasetId": dataset_id,
            "fields": [
                {
                    "column": "region",
                    "label": "区域",
                    "input": "select",
                    "required": True,
                    "options": ["east", "west"],
                },
                {"column": "amount", "label": "金额", "input": "number"},
            ],
            "submitLabel": "提交",
            "successMessage": "已登记",
        },
    }
    resp = put_spec(client, artifact["id"], spec_v2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["spec"] == spec_v2


def test_form_dataset_owner_check_422_zero_half_write(client, db_session) -> None:
    """datasetId 指向他企/不存在数据集：422 如实拒，版本行不增（零半写）。"""
    own_id = seed_dataset(db_session, key="own_src")
    other_id = seed_dataset(db_session, customer_id="cust-002", key="other_src")
    artifact = blank_form(client)
    aid = artifact["id"]

    # 先成功绑定本企数据集（v2）
    bound = {
        "title": "访客登记",
        "spec": {
            "datasetId": own_id,
            "fields": [{"column": "region", "label": "区域", "input": "text"}],
        },
    }
    ok = put_spec(client, aid, bound)
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"] == 2

    # 他企数据集：422（不泄露属主差异——与不存在同文案）
    hijack = {
        "title": "访客登记",
        "spec": {"datasetId": other_id, "fields": []},
    }
    resp = put_spec(client, aid, hijack)
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"数据集 {other_id} 不存在"

    # 不存在的数据集：同样 422
    ghost = {"title": "访客登记", "spec": {"datasetId": "ds-nope", "fields": []}}
    resp2 = put_spec(client, aid, ghost)
    assert resp2.status_code == 422
    assert resp2.json()["detail"] == "数据集 ds-nope 不存在"

    # 零半写：版本停在 2、版本链不增
    detail = client.get(f"/api/v1/artifacts/{aid}", headers=CUST).json()
    assert detail["version"] == 2
    versions = client.get(f"/api/v1/artifacts/{aid}/versions", headers=CUST).json()
    assert [v["version"] for v in versions] == [2, 1]


def test_form_dataset_none_intermediate_saves(client) -> None:
    """datasetId=None 构建中间态：可保存（可保存不可提交）。"""
    artifact = blank_form(client)
    spec = {
        "title": "访客登记",
        "spec": {
            "datasetId": None,
            "fields": [
                {"column": "name", "label": "姓名", "input": "text", "required": True}
            ],
        },
    }
    resp = put_spec(client, artifact["id"], spec)
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["spec"]["spec"]["datasetId"] is None


def test_form_archived_rejects_edit(client) -> None:
    """archived form 拒改 409（状态机语义对 form 同样生效）。"""
    artifact = blank_form(client)
    client.post(f"/api/v1/artifacts/{artifact['id']}/archive", headers=CUST)
    spec = {"title": "访客登记", "spec": {"datasetId": None, "fields": []}}
    resp = put_spec(client, artifact["id"], spec)
    assert resp.status_code == 409
    assert "恢复" in resp.json()["detail"]


# ------------------------------------------------- dashboard 语义零漂移（1.2）


def test_dashboard_blank_and_put_zero_drift(client) -> None:
    """kind 分派不影响 dashboard：空白建板与 PUT 语义照旧（form 形状的
    内层对 dashboard 不触发 form 校验）。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "运营看板"}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["spec"] == {
        "title": "运营看板",
        "spec": {"title": "运营看板", "panels": []},
    }
    aid = resp.json()["id"]
    # 常规编辑：追加面板 → v2
    put = put_spec(
        client,
        aid,
        {"title": "运营看板", "spec": {"title": "运营看板", "panels": [{"id": "p1"}]}},
    )
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2
    # dashboard 的 spec.spec 即便长得像 form 内层（fields 非数组）也不走
    # form 校验——既有最小校验口径零漂移
    weird = {"title": "运营看板", "spec": {"fields": "not-a-list"}}
    put2 = put_spec(client, aid, weird)
    assert put2.status_code == 200, put2.text
    assert put2.json()["version"] == 3


# --------------------------------------------- 2.1/2.2 insert 共享落层直连断言


def test_submit_insert_correction_api_tier_local(db_session) -> None:
    """api 档：本地落层（零引擎）、值按 dtype 归一、空 values 域异常。"""
    dataset_id = seed_dataset(db_session, key="free_src")
    dataset = db_session.get(Dataset, dataset_id)
    correction_id = datasets_service.submit_insert_correction(
        db_session,
        dataset=dataset,
        values={"region": "east", "amount": "1.5", "qty": "9"},
        actor="console:cust-001",
    )
    row = db_session.get(DatasetCorrection, correction_id)
    assert row is not None and row.op == "insert"
    assert row.actor == "console:cust-001"
    assert row.value_json == {"region": "east", "amount": 1.5, "qty": 9}  # dtype 归一
    # 空 values → LocalQueryError（路由层转 400，与下沉前文案一致）
    with pytest.raises(LocalQueryError, match="insert 必须带 values"):
        datasets_service.submit_insert_correction(
            db_session, dataset=dataset, values={}, actor="console:cust-001"
        )


def test_submit_insert_correction_cell_tier_engine_passthrough(
    db_session, engine_fake
) -> None:
    """cell 档：引擎 TOOL_CORRECTION_ADD 报文逐项一致；缺 correction_id
    抛 EngineCallError（路由层转 502）。"""
    dataset_id = seed_dataset(db_session, key="cell_src", tier="cell")
    dataset = db_session.get(Dataset, dataset_id)
    correction_id = datasets_service.submit_insert_correction(
        db_session,
        dataset=dataset,
        values={"region": "east"},
        actor="share-form:9",
        engine_factory=lambda customer_id: engine_fake,
    )
    assert correction_id == 7
    assert engine_fake.calls == [
        (
            TOOL_CORRECTION_ADD,
            {
                "dataset_key": "cell_src",
                "op": "insert",
                "anchor": None,
                "values": {"region": "east"},
                "actor": "share-form:9",
            },
        )
    ]
    # 引擎未返回 correction_id → EngineCallError
    engine_fake.result = {"rows": 0}
    with pytest.raises(EngineCallError, match="correction_id"):
        datasets_service.submit_insert_correction(
            db_session,
            dataset=dataset,
            values={"region": "west"},
            actor="share-form:9",
            engine_factory=lambda customer_id: engine_fake,
        )


def test_corrections_route_insert_both_tiers_end_to_end(
    client, db_session, engine_fake
) -> None:
    """corrections 端点 insert 分支改调共享函数后两档仍通（api 档零引擎、
    cell 档报文同形；下沉零漂移主证 = 既有 corrections 用例零改动通过）。"""
    api_id = seed_dataset(db_session, key="route_api")
    resp = client.post(
        f"/api/v1/datasets/{api_id}/corrections",
        headers=CUST,
        json={"op": "insert", "values": {"region": "east", "qty": "3"}},
    )
    assert resp.status_code == 201, resp.text
    stored = db_session.get(DatasetCorrection, resp.json()["correction_id"])
    assert stored is not None
    assert stored.value_json == {"region": "east", "qty": 3}
    assert stored.actor == "customer:cust-001"
    assert engine_fake.calls == []  # api 档全链路零引擎

    cell_id = seed_dataset(db_session, key="route_cell", tier="cell")
    resp2 = client.post(
        f"/api/v1/datasets/{cell_id}/corrections",
        headers=CUST,
        json={"op": "insert", "values": {"region": "west"}},
    )
    assert resp2.status_code == 201, resp2.text
    assert resp2.json() == {"correction_id": 7}
    assert engine_fake.calls[-1] == (
        TOOL_CORRECTION_ADD,
        {
            "dataset_key": "route_cell",
            "op": "insert",
            "anchor": None,
            "values": {"region": "west"},
            "actor": "customer:cust-001",
        },
    )
