"""wire-form-entry-advanced 公开面测试（任务 2.3 公开选项端点 + 3.2 公开
submit 接线的全量语义回归）。

覆盖：
- 2.3 公开选项解析端点 ``GET /api/v1/share/form/{id}/options``：载荷形状
  （仅动态选项字段的去重选项/截断标注/fills 回填映射）、未声明动态选项
  返回空 fields 非 404、引用失效如实 error 标注、统一 404 矩阵（缺 t/
  篡改/v 不一致/dashboard 产物/归档断链）、令牌级限流（独立 env
  FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR 缺省 120/h、与提交限流分桶）、
  60 秒服务端短缓存（同令牌零底层查询、新令牌重建）；
- 3.2 公开 submit 走共享执法点：动态选项提交时刻实时裁决（值不在有效
  视图 422 防篡改、档案行删除后 422、选项源列漂移 422）、提交闸公开路径
  同规执法（expect none 命中 422 带 constraint id+message 零写入、when 假
  跳过、{field} 代入）、字段校验先于闸呈现、既有 422/429/404 冻结序保持
  （字段错 422 先于限流 429）。

环境：照 test_share_form 先例（ai 与 datasets 同引擎合库、分享 secret 已
配置、限流器与选项缓存逐用例复位）。
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
from fd_wire_api.datasets.db import (
    DatasetsBase,
    get_db as get_datasets_db,
)
from fd_wire_api.datasets.ingest import (
    TOOL_QUERY,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset, DatasetColumn, DatasetCorrection, DatasetRow
from fd_wire_api.share import rate_limit
from fd_wire_api.share import routes as share_routes

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-share-adv-001"
SUB_OTHER = "sub-share-adv-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

SHARE_SECRET = "test-share-adv-secret-32bytes"


class ScriptedEngine:
    """cell 档引擎假件：记录 tools/call，按工具脚本化返回。"""

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
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", SHARE_SECRET)
    monkeypatch.delenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", raising=False)
    monkeypatch.delenv("FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR", raising=False)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    rate_limit.reset()
    share_routes._reset_options_cache()
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
    rate_limit.reset()
    share_routes._reset_options_cache()
    reset_ai_runtime()


# ------------------------------------------------------------------ 流程助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = "cust-001",
    key: str = "adv_src",
    tier: str = "api",
    columns: list[dict] | None = None,
) -> str:
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
    for index, row in enumerate(rows):
        db_session.add(DatasetRow(dataset_id=dataset_id, row_index=index, data=row))
    db_session.commit()


def make_form(
    client,
    *,
    dataset_id: str | None,
    inner_extra: dict | None = None,
    fields: list[dict] | None = None,
    publish: bool = True,
) -> dict:
    """空白建 form → PUT 配置 →（可选）发布。fields/inner_extra 二选一。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "form", "title": "打药登记"}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    if fields is not None or inner_extra is not None:
        inner: dict = {
            "title": "打药登记",
            "datasetId": dataset_id,
            "fields": fields or [],
        }
        inner.update(inner_extra or {})
        put = client.put(
            f"/api/v1/artifacts/{artifact['id']}",
            json={"spec": {"title": "打药登记", "spec": inner}},
            headers=CUST,
        )
        assert put.status_code == 200, put.text
        artifact = put.json()
    if publish:
        pub = client.post(f"/api/v1/artifacts/{artifact['id']}/publish", headers=CUST)
        assert pub.status_code == 200, pub.text
        artifact = pub.json()
    return artifact


def _share(client, artifact_id: int, *, ttl_days: int | None = None) -> dict:
    json = None if ttl_days is None else {"ttl_days": ttl_days}
    resp = client.post(
        f"/api/v1/artifacts/{artifact_id}/share", headers=CUST, json=json
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _options_url(link_url: str) -> str:
    base, _, query = link_url["url"].partition("?")
    return f"{base}/options?{query}"


def _submit(client, link: dict, values: dict):
    base, _, query = link["url"].partition("?")
    return client.post(f"{base}/submit?{query}", json={"values": values})


def _corrections(db_session, dataset_id: str) -> list[DatasetCorrection]:
    return list(
        db_session.execute(
            select(DatasetCorrection).where(DatasetCorrection.dataset_id == dataset_id)
        ).scalars().all()
    )


def _advanced_fields(arch_id: str) -> list[dict]:
    """农药（动态选项）+ 间隔期（fillFrom）+ 记录日（date）+ 截止日（computed）。"""
    return [
        {
            "column": "pesticide",
            "label": "农药",
            "input": "select",
            "required": True,
            "optionsFromDataset": {
                "datasetId": arch_id,
                "column": "name",
                "filters": [{"column": "category", "op": "eq", "value": "杀虫剂"}],
            },
        },
        {
            "column": "interval_days",
            "label": "安全间隔期天数",
            "input": "number",
            "fillFrom": {"field": "pesticide", "column": "interval_days"},
        },
        {"column": "record_day", "label": "记录日期", "input": "date"},
        {
            "column": "deadline",
            "label": "截止日期",
            "input": "date",
            "computed": {"op": "add_days", "date": "record_day", "days": "interval_days"},
        },
    ]


def _advanced_setup(client, db_session):
    """档案集（动态选项+回填源）+ 目标集 + 发布表单 + 分享链接。"""
    arch_id = seed_dataset(
        db_session,
        key="adv_arch",
        columns=[
            {"name": "name", "type": "TEXT"},
            {"name": "category", "type": "TEXT"},
            {"name": "interval_days", "type": "INTEGER"},
        ],
    )
    seed_rows(
        db_session,
        arch_id,
        [
            {"name": "药B", "category": "杀虫剂", "interval_days": 10},
            {"name": "药A", "category": "杀虫剂", "interval_days": 7},
            {"name": "药C", "category": "除草剂", "interval_days": 1},
        ],
    )
    target_id = seed_dataset(
        db_session,
        key="adv_target",
        columns=[
            {"name": "pesticide", "type": "TEXT"},
            {"name": "interval_days", "type": "INTEGER"},
            {"name": "record_day", "type": "TEXT"},
            {"name": "deadline", "type": "TEXT"},
        ],
    )
    form = make_form(client, dataset_id=target_id, fields=_advanced_fields(arch_id))
    link = _share(client, form["id"])
    return arch_id, target_id, form, link


# ------------------------------------------------------ 2.3 选项解析端点


def test_options_endpoint_payload_options_and_fills(client, db_session) -> None:
    """选项端点载荷：仅动态选项字段——去重选项（filters 过滤、升序）+
    truncated + fills 回填映射（每选项值一行声明列取值）；静态字段与
    目标数据集行不出现；未声明动态选项的表单返回空 fields 非 404。"""
    _arch_id, _target_id, _form, link = _advanced_setup(client, db_session)

    resp = client.get(_options_url(link))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"fields"}
    assert set(body["fields"]) == {"pesticide"}  # 只含声明动态选项的字段
    entry = body["fields"]["pesticide"]
    # 本组夹具（药A/药B/间隔 7/10）与 web formSpec.test.ts
    # 「双端一致性配对用例」同源——6.3 双端一致性。
    # filters 生效（除草剂被滤掉）、升序、无截断
    assert entry["options"] == ["药A", "药B"]
    assert entry["truncated"] is False
    # fills：fillFrom 声明列 interval_days → {选项值: 该行回填列值}
    # （载荷口径=字符串化，与选项值一致——公开页 fills 查表按字符串取值）
    assert entry["fills"] == {"interval_days": {"药A": "7", "药B": "10"}}

    # 未声明动态选项的表单：空 fields（非 404）
    plain = make_form(client, dataset_id=_target_id, fields=[
        {"column": "region", "label": "区域", "input": "select", "required": True, "options": ["east"]}
    ])
    plain_link = _share(client, plain["id"])
    plain_resp = client.get(_options_url(plain_link))
    assert plain_resp.status_code == 200
    assert plain_resp.json() == {"fields": {}}


def test_options_endpoint_truncation_and_drift_error(client, db_session) -> None:
    """截断如实标注（超 limit 返回前 limit 项 + truncated）；选项源列漂移
    （列不在快照）= 空集 + error 标注（不静默空白），fills 随空集为空映射。"""
    arch_id = seed_dataset(db_session, key="trunc_arch")
    seed_rows(
        db_session,
        arch_id,
        [{"name": f"药{i:02d}"} for i in range(5)],
    )
    target_id = seed_dataset(db_session, key="trunc_target")
    fields = [
        {
            "column": "pesticide",
            "label": "农药",
            "input": "select",
            "required": True,
            "optionsFromDataset": {"datasetId": arch_id, "column": "name", "limit": 3},
        }
    ]
    form = make_form(client, dataset_id=target_id, fields=fields)
    link = _share(client, form["id"])
    body = client.get(_options_url(link)).json()
    entry = body["fields"]["pesticide"]
    assert entry["options"] == ["药00", "药01", "药02"]
    assert entry["truncated"] is True
    assert entry["fills"] == {}

    # 引用失效：选项列从列注册表移除 → 空集 + error
    db_session.execute(
        DatasetColumn.__table__.delete().where(
            DatasetColumn.dataset_id == arch_id, DatasetColumn.name == "name"
        )
    )
    db_session.commit()
    share_routes._reset_options_cache()  # 绕过短缓存看实时解析
    drifted = client.get(_options_url(link)).json()["fields"]["pesticide"]
    assert drifted["options"] == []
    assert drifted["truncated"] is False
    assert "name" in drifted["error"]
    assert drifted["fills"] == {}


def test_options_endpoint_uniform_404_matrix(client, db_session) -> None:
    """统一 404 矩阵：缺 t/篡改/v 不一致/dashboard 产物/归档断链——
    detail 恒 "not found"；缺鉴权面（无令牌）同形。"""
    _a, _t, _form, link = _advanced_setup(client, db_session)
    base, _, _query = link["url"].partition("?")
    token = link["url"].split("t=")[1].split("&")[0]
    version = link["version"]

    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    client.post(f"/api/v1/artifacts/{board['id']}/publish", headers=CUST)
    board_link = _share(client, board["id"])
    board_base, _, board_query = board_link["url"].partition("?")

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    cases = [
        base + "/options",  # 缺 t 缺 v
        f"{base}/options?t={token}",  # 缺 v
        f"{base}/options?t={tampered}&v={version}",  # 篡改 mac
        f"{base}/options?t={token}&v=99",  # v 不一致
        f"{base}/options?t=not-a-token&v=1",  # 结构畸形
        "/api/v1/share/form/99999/options?t=1.2.abc&v=1",  # 产物不存在
        f"{board_base.replace('/share/dashboard/', '/share/form/')}/options?{board_query}",  # 非 form
    ]
    for url in cases:
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert resp.json()["detail"] == "not found", url

    # 归档断链（选项面同样断）
    archived = make_form(client, dataset_id=_t, fields=_advanced_fields(_a))
    archived_link = _share(client, archived["id"])
    assert client.get(_options_url(archived_link)).status_code == 200
    client.post(f"/api/v1/artifacts/{archived['id']}/archive", headers=CUST)
    resp = client.get(_options_url(archived_link))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


def test_options_endpoint_rate_limit_and_buckets(client, db_session, monkeypatch) -> None:
    """选项端点限流：独立 env（缺省 120、env=2 → 第 3 次 429 带
    Retry-After）；与提交限流分桶（选项超限后提交仍可 201）。"""
    _a, target_id, _form, link = _advanced_setup(client, db_session)
    monkeypatch.setenv("FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR", "2")
    url = _options_url(link)
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    third = client.get(url)
    assert third.status_code == 429
    assert "频繁" in third.json()["detail"]
    assert int(third.headers["Retry-After"]) >= 1
    # 提交桶不受连坐（另一把限流器）
    submit = _submit(client, link, {"pesticide": "药A"})
    assert submit.status_code == 201, submit.text
    # 另一把令牌（不同 mac）选项额度独立
    fresh = _share(client, _form["id"], ttl_days=7)
    assert client.get(_options_url(fresh)).status_code == 200


def test_options_endpoint_rate_limit_env_default_and_cache(
    client, db_session, monkeypatch
) -> None:
    """限流 env 惰性读（缺省 120、非法回退）；60 秒短缓存：同令牌第二击
    零底层查询（cell 档引擎调用数不增）、改版新版本键重建、数据变化在
    TTL 内呈现缓存值。"""
    assert rate_limit.options_rate_per_hour() == 120
    monkeypatch.setenv("FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR", "abc")
    assert rate_limit.options_rate_per_hour() == 120
    monkeypatch.setenv("FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR", "3")
    assert rate_limit.options_rate_per_hour() == 3

    # cell 档档案集：引擎计数验证短缓存
    arch_cell = seed_dataset(db_session, key="cache_arch", tier="cell")
    target_id = seed_dataset(db_session, key="cache_target")
    fields = [
        {
            "column": "pesticide",
            "label": "农药",
            "input": "select",
            "required": True,
            "optionsFromDataset": {"datasetId": arch_cell, "column": "name"},
        },
        {
            "column": "interval_days",
            "label": "间隔期",
            "input": "number",
            "fillFrom": {"field": "pesticide", "column": "interval_days"},
        },
    ]
    monkeypatch.delenv("FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR", raising=False)

    def query_response(arguments: dict) -> dict:
        if arguments.get("distinct_column"):
            return {
                "columns": [{"name": "name", "type": "TEXT"}],
                "distinct_column": "name",
                "values": ["药A", "药B"],
                "count": 2,
            }
        return {
            "columns": [
                {"name": "name", "type": "TEXT"},
                {"name": "interval_days", "type": "INTEGER"},
            ],
            "rows": [{"name": arguments["filters"][0]["value"], "interval_days": 7}],
        }

    class CountingEngine(ScriptedEngine):
        def call(self, tool: str, arguments: dict) -> dict:
            if tool == TOOL_QUERY:
                self.responses[TOOL_QUERY] = query_response(arguments)
            return super().call(tool, arguments)

    # 换装计数引擎（覆写依赖键——本用例断言引擎调用数）
    counting = CountingEngine()
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: counting
    )
    form = make_form(client, dataset_id=target_id, fields=fields)
    link = _share(client, form["id"])
    url = _options_url(link)

    first = client.get(url)
    assert first.status_code == 200, first.text
    calls_after_first = len(counting.calls_for(TOOL_QUERY))
    assert calls_after_first == 3  # 1 distinct + 2 fill（每选项值一行）
    body_first = first.json()

    # TTL 内第二击：零新增引擎调用、载荷逐字回放
    second = client.get(url)
    assert second.status_code == 200
    assert second.json() == body_first
    assert len(counting.calls_for(TOOL_QUERY)) == calls_after_first

    # 新令牌（不同 mac）→ 键不同 → 重建（引擎调用再 +3）
    fresh = _share(client, form["id"], ttl_days=7)
    third = client.get(_options_url(fresh))
    assert third.status_code == 200
    assert third.json() == body_first
    assert len(counting.calls_for(TOOL_QUERY)) == calls_after_first + 3


# --------------------------------------- 3.2 公开 submit：动态选项实时裁决


def test_public_submit_dynamic_options_realtime(client, db_session) -> None:
    """公开提交动态选项：有效值 201（回填值随记录落库=快照语义）；伪造值
    422 带解析选项；提交前档案行被删 → 值不在有效视图 422（提交时刻裁决）。"""
    _a, target_id, _form, link = _advanced_setup(client, db_session)

    # 正例：药A + 回填 7 + computed 截止日（手改 2026-10-30 亦过——仅类型校验）
    ok = _submit(
        client,
        link,
        {
            "pesticide": "药A",
            "interval_days": 7,
            "record_day": "2026-10-15",
            "deadline": "2026-10-30",
        },
    )
    assert ok.status_code == 201, ok.text
    rows = _corrections(db_session, target_id)
    assert rows[-1].value_json == {
        "pesticide": "药A",
        "interval_days": 7,
        "record_day": "2026-10-15",
        "deadline": "2026-10-30",
    }

    # 防篡改：伪造不属于解析选项的值（药C 被过滤器排除）
    tampered = _submit(client, link, {"pesticide": "药C"})
    assert tampered.status_code == 422, tampered.text
    detail = tampered.json()["detail"]
    assert "选项" in detail and "药C" in detail
    assert len(_corrections(db_session, target_id)) == 1

    # 提交时刻裁决：档案基行删除后药A 不在有效视图 → 422
    db_session.execute(
        DatasetRow.__table__.delete().where(
            DatasetRow.dataset_id == _a, DatasetRow.data["name"].as_string() == "药A"
        )
    )
    db_session.commit()
    stale = _submit(client, link, {"pesticide": "药A"})
    assert stale.status_code == 422, stale.text
    assert "选项" in stale.json()["detail"]
    assert len(_corrections(db_session, target_id)) == 1  # 零写入

    # 选项源列漂移：列从快照移除 → 非空值 422（源不可用如实标注）
    db_session.execute(
        DatasetColumn.__table__.delete().where(
            DatasetColumn.dataset_id == _a, DatasetColumn.name == "name"
        )
    )
    db_session.commit()
    drifted = _submit(client, link, {"pesticide": "药B"})
    assert drifted.status_code == 422
    assert "选项源当前不可用" in drifted.json()["detail"]


def test_public_submit_constraint_gate_and_frozen_order(client, db_session, monkeypatch) -> None:
    """公开提交闸同规执法：expect none 命中 422 带 constraint id+message
    零写入；when 假跳过；{field} 代入；字段校验失败先于限流 429（冻结序）。"""
    spray_id = seed_dataset(
        db_session,
        key="pub_spray",
        columns=[{"name": "plot", "type": "TEXT"}, {"name": "deadline", "type": "TEXT"}],
    )
    seed_rows(db_session, spray_id, [{"plot": "P1", "deadline": "2026-10-20"}])
    target_id = seed_dataset(
        db_session,
        key="pub_target",
        columns=[
            {"name": "plot", "type": "TEXT"},
            {"name": "record_day", "type": "TEXT"},
        ],
    )
    fields = [
        {"column": "plot", "label": "地块", "input": "text", "required": True},
        {"column": "record_day", "label": "采收日期", "input": "date", "required": True},
    ]
    inner_extra = {
        "constraints": [
            {
                "id": "no-harvest-in-interval",
                "message": "采收日期仍在安全间隔期内，禁止采收",
                "when": {"field": "plot", "op": "notEmpty"},
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
        "successMessage": "已登记",
    }
    form = make_form(
        client, dataset_id=target_id, fields=fields, inner_extra=inner_extra
    )
    link = _share(client, form["id"])

    # 命中：P1 打药截止 2026-10-20 ≥ 采收 2026-10-15 → 422 带 id+message，零写入
    blocked = _submit(
        client, link, {"plot": "P1", "record_day": "2026-10-15"}
    )
    assert blocked.status_code == 422, blocked.text
    detail = blocked.json()["detail"]
    assert "no-harvest-in-interval" in detail
    assert "禁止采收" in detail
    assert _corrections(db_session, target_id) == []

    # 间隔期外（P1 截止 10-14 < 采收 10-15）→ 201；无打药记录地块 → 201
    ok = _submit(client, link, {"plot": "P1", "record_day": "2026-10-21"})
    assert ok.status_code == 201, ok.text
    assert ok.json() == {"ok": True, "message": "已登记"}
    ok2 = _submit(client, link, {"plot": "P9", "record_day": "2026-10-15"})
    assert ok2.status_code == 201, ok2.text
    assert len(_corrections(db_session, target_id)) == 2

    # 字段校验先于限流（冻结序）：阈值 2 已被两次成功占满——非法载荷仍得
    # 422 字段错而非 429（validate 在 stage_gate 之前）
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "2")
    rate_limit.reset()
    assert _submit(client, link, {"plot": "P1", "record_day": "2026-10-21"}).status_code == 201
    assert _submit(client, link, {"plot": "P1", "record_day": "2026-10-21"}).status_code == 201
    invalid = _submit(client, link, {"plot": "P1", "record_day": "2026/10/21"})
    assert invalid.status_code == 422
    assert "YYYY-MM-DD" in invalid.json()["detail"]
    # 合法载荷此时过限流 → 429（闸与限流都在字段校验之后）
    limited = _submit(client, link, {"plot": "P1", "record_day": "2026-10-21"})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert len(_corrections(db_session, target_id)) == 4  # 超限零落库

    # when 假跳过探测：plot 空（缺省）→ when notEmpty 假 → 必填拦在前，
    # 换 when=eq 不命中值验证跳过（P9 无打药记录本来就过——此处验证 when
    # 假时不产生探测：直接构造 when 恒假约束）
    skip_fields = fields
    skip_extra = {
        "constraints": [
            {
                "id": "never",
                "message": "永不生效",
                "when": {"field": "plot", "op": "eq", "value": "不存在的地块"},
                "probe": {
                    "datasetId": spray_id,
                    "filters": [{"column": "plot", "op": "eq", "value": {"field": "plot"}}],
                    "expect": "none",
                },
            }
        ]
    }
    skip_form = make_form(
        client, dataset_id=target_id, fields=skip_fields, inner_extra=skip_extra
    )
    skip_link = _share(client, skip_form["id"])
    passed = _submit(client, skip_link, {"plot": "P1", "record_day": "2026-10-15"})
    assert passed.status_code == 201, passed.text  # when 假 → 探测跳过 → 不拦


def test_public_submit_pinned_version_dynamic_and_gate(client, db_session) -> None:
    """钉死版本语义：改版去掉闸与动态字段后，旧令牌仍按旧版本执行闸与
    动态选项裁决；新令牌按新版本（无闸）。"""
    arch_id = seed_dataset(db_session, key="pin_arch")
    seed_rows(db_session, arch_id, [{"name": "药A", "interval_days": 7}])
    target_id = seed_dataset(
        db_session,
        key="pin_target",
        columns=[{"name": "pesticide", "type": "TEXT"}, {"name": "plot", "type": "TEXT"}],
    )
    fields = [
        {
            "column": "pesticide",
            "label": "农药",
            "input": "select",
            "required": True,
            "optionsFromDataset": {"datasetId": arch_id, "column": "name"},
        },
    ]
    inner_extra = {
        "constraints": [
            {
                "id": "gate",
                "message": "被闸拦截",
                "probe": {
                    "datasetId": arch_id,
                    "filters": [{"column": "name", "op": "eq", "value": "药A"}],
                    "expect": "none",
                },
            }
        ]
    }
    form = make_form(client, dataset_id=target_id, fields=fields, inner_extra=inner_extra)
    old_link = _share(client, form["id"])

    # 改版：去掉闸、换静态 options、去掉动态字段
    new_inner = {
        "title": "打药登记",
        "datasetId": target_id,
        "fields": [
            {
                "column": "pesticide",
                "label": "农药",
                "input": "select",
                "required": True,
                "options": ["药A"],
            }
        ],
    }
    put = client.put(
        f"/api/v1/artifacts/{form['id']}",
        json={"spec": {"title": "打药登记", "spec": new_inner}},
        headers=CUST,
    )
    assert put.status_code == 200, put.text

    # 旧令牌：仍按旧版本——动态选项解析有效（药A 在档案）+ 闸命中（档案有
    # 药A 行、expect none）→ 422
    old = _submit(client, old_link, {"pesticide": "药A"})
    assert old.status_code == 422, old.text
    assert "提交被约束 gate 拦截" in old.json()["detail"]

    # 新令牌：按新版本——静态选项过、无闸 → 201
    new_link = _share(client, form["id"])
    fresh = _submit(client, new_link, {"pesticide": "药A"})
    assert fresh.status_code == 201, fresh.text
