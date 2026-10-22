"""wire-record-view 后端全量测试（任务 1.1-1.3 / 2.1-2.3 / 3.1-3.4 / 4.2 后端侧）。

覆盖：
- 产物契约：空白创建最小 spec、保存校验矩阵（结构层逐条文案断言 + 路由
  422 零半写——版本行不增）、中间态半写拦截（datasetId 空 ⇒ keyColumn/
  headerFields/sections 全空）、属主与保存时刻列快照层（他企/不存在/
  keyColumn/headerFields.column/sections.dateColumn/filters 的 {fromField}
  不在列快照 422 定位）；
- 分享与长效令牌：record-view 进 SHARE_KIND_PATHS、TTL 0 档（生成
  expires_at=null、令牌 exp=0、verify_token 跳过过期校验——0 档/正档/
  非法档/篡改双向用例）、dashboard/form 0 档同享、缺省 30 天零漂移、
  非法 ttl_days 422 列合法值；
- 公开取数端点：happy path（header 值/section 升序/fromField 代入/
  __row_id）、无此记录 found:false（不泄露存在性）、k 缺失/空白 422、
  多行取首行、limit 截断与 truncated、空/非法日期排末尾、单段失败
  error（列漂移/集删/引擎失败）其余照常、统一 404 矩阵（含 v 缺席的
  规范 URL 形态收）、钉死版本、补录后扫码可见（有效视图含修正）、
  免费+cell 双档（cell 档 mock 引擎断言声明查询形状）、限流（独立 env
  缺省 240/h、429+Retry-After、超限不落查询、令牌分桶）、60 秒短缓存
  （查询计数证明：同键零查询、新 k/新令牌重建、TTL 内回放缓存值）；
- 展示面与 AI 门：WORKBENCH_KINDS 收录 record-view、AI 门
  （ARTIFACT_TOOL_KINDS）与校验面（ARTIFACT_KINDS）不收录；
- preview 端点（4.2 后端侧）：与公开端点同一组装函数（同 k 同载荷）、
  当前版本语义（改版后预览随之变）、draft 可预览、kind 门 422、
  中间态 422、k 空白 422、跨客户 404、keyColumn 漂移 400 如实
  （与公开端点统一 404 分置）。

环境：照 test_share_form_options 先例（ai 与 datasets 同引擎合库、分享
secret 已配置、限流器与两处短缓存逐用例复位）。
"""

from __future__ import annotations

import copy
import os
from urllib.parse import quote

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.ai.tools import ARTIFACT_TOOL_KINDS
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import ARTIFACT_KINDS
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
from fd_wire_api.share import service as share_service
from fd_wire_api.workbench import WORKBENCH_KINDS

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-record-view-001"
SUB_OTHER = "sub-record-view-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

SHARE_SECRET = "test-record-view-secret-32bytes"

TITLE = "批次溯源页"


class ScriptedEngine:
    """cell 档引擎假件：记录 tools/call，按工具脚本化返回。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}

    def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, copy.deepcopy(arguments)))
        result = self.responses.get(tool, {"error": "no script"})
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
    monkeypatch.delenv("FD_WIRE_SHARE_RECORDVIEW_RATE_PER_HOUR", raising=False)
    monkeypatch.delenv("FD_WIRE_SHARE_TTL_SECONDS", raising=False)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    rate_limit.reset()
    share_routes._reset_options_cache()
    share_routes._reset_recordview_cache()
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
    share_routes._reset_recordview_cache()
    reset_ai_runtime()


# ------------------------------------------------------------------ 流程助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = "cust-001",
    key: str,
    tier: str = "api",
    columns: list[dict] | None = None,
) -> str:
    columns = columns if columns is not None else [{"name": "name", "type": "TEXT"}]
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


def seed_rows(db_session, dataset_id: str, rows: list[dict], *, start: int = 0) -> None:
    for index, row in enumerate(rows):
        db_session.add(
            DatasetRow(dataset_id=dataset_id, row_index=start + index, data=row)
        )
    db_session.commit()


BATCH_COLUMNS = [
    {"name": "batch_no", "type": "TEXT"},
    {"name": "product", "type": "TEXT"},
    {"name": "origin", "type": "TEXT"},
    {"name": "plot", "type": "TEXT"},
]
SPRAY_COLUMNS = [
    {"name": "plot", "type": "TEXT"},
    {"name": "date", "type": "TEXT"},
    {"name": "chemical", "type": "TEXT"},
]


def seed_traceability_fixtures(db_session):
    """溯源页三件套：批次主集 + 打药/采收时间轴源（spec.md 声明场景同源）。"""
    batches_id = seed_dataset(db_session, key="rv_batches", columns=BATCH_COLUMNS)
    seed_rows(
        db_session,
        batches_id,
        [
            {"batch_no": "B2026-001", "product": "阳光玫瑰葡萄", "origin": "云南", "plot": "P1"},
            {"batch_no": "B2026-002", "product": "蓝莓", "origin": "山东", "plot": "P2"},
        ],
    )
    spray_id = seed_dataset(db_session, key="rv_spray", columns=SPRAY_COLUMNS)
    seed_rows(
        db_session,
        spray_id,
        [
            {"plot": "P1", "date": "2026-09-10", "chemical": "波尔多液"},
            {"plot": "P1", "date": "2026-09-01", "chemical": "吡唑醚菌酯"},
            {"plot": "P2", "date": "2026-09-05", "chemical": "草铵膦"},
        ],
    )
    harvest_id = seed_dataset(db_session, key="rv_harvest", columns=SPRAY_COLUMNS)
    seed_rows(
        db_session,
        harvest_id,
        [
            {"plot": "P1", "date": "2026-09-25", "chemical": None},
            {"plot": "P2", "date": "2026-09-22", "chemical": None},
        ],
    )
    return batches_id, spray_id, harvest_id


def _inner(batches_id: str, spray_id: str, harvest_id: str | None = None) -> dict:
    sections = [
        {
            "title": "打药记录",
            "datasetId": spray_id,
            "dateColumn": "date",
            "filters": [
                {"column": "plot", "op": "eq", "value": {"fromField": "plot"}}
            ],
        },
    ]
    if harvest_id is not None:
        sections.append(
            {"title": "采收记录", "datasetId": harvest_id, "dateColumn": "date"}
        )
    return {
        "datasetId": batches_id,
        "keyColumn": "batch_no",
        "headerFields": [
            {"column": "product", "label": "产品名称"},
            {"column": "origin"},
        ],
        "sections": sections,
    }


def make_record_view(client, inner: dict | None, *, publish: bool = True) -> dict:
    """空白建 record-view →（可选）PUT 配置 →（可选）发布。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "record-view", "title": TITLE}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    if inner is not None:
        put = client.put(
            f"/api/v1/artifacts/{artifact['id']}",
            json={"spec": {"title": TITLE, "spec": inner}},
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


def _fetch_url(link: dict, k: str, *, with_v: bool = True) -> str:
    """公开取数 URL：``/api/v1/share/record-view/{id}?t=…(&v=…)&k=…``。"""
    base, _, query = link["url"].partition("?")
    token = query.split("t=")[1].split("&")[0]
    v_part = f"&v={link['version']}" if with_v else ""
    return f"{base}?t={token}{v_part}&k={quote(k)}"


def _fetch(client, link: dict, k: str, *, with_v: bool = True):
    return client.get(_fetch_url(link, k, with_v=with_v))


def _version_count(db_session, artifact_id: int) -> int:
    from fd_wire_api.artifacts.models import ArtifactVersion

    return len(
        list(
            db_session.execute(
                ArtifactVersion.__table__.select().where(
                    ArtifactVersion.artifact_id == artifact_id
                )
            )
        )
    )


def _drop_column(db_session, dataset_id: str, name: str) -> None:
    db_session.execute(
        DatasetColumn.__table__.delete().where(
            DatasetColumn.dataset_id == dataset_id, DatasetColumn.name == name
        )
    )
    dataset = db_session.get(Dataset, dataset_id)
    if dataset is not None:
        dataset.columns_json = [
            c for c in (dataset.columns_json or []) if c.get("name") != name
        ]
    db_session.commit()


# ------------------------------------------------- 1.1-1.2 产物契约与保存校验


def test_blank_create_minimal_spec(client, db_session) -> None:
    """空白创建 record-view：201 draft v1 = 未绑数据集最小 spec；白名单外
    kind 422 零半写；title 空白 422。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "record-view", "title": TITLE}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    assert artifact["kind"] == "record-view"
    assert artifact["status"] == "draft"
    assert artifact["version"] == 1
    assert artifact["spec"]["spec"] == {
        "datasetId": None,
        "keyColumn": None,
        "headerFields": [],
        "sections": [],
    }

    # 白名单外（AI 产物）仍 422 零半写
    rejected = client.post(
        "/api/v1/artifacts",
        json={"kind": "research_report", "title": "报告"},
        headers=CUST,
    )
    assert rejected.status_code == 422
    assert "record-view" in rejected.json()["detail"]
    listing = client.get("/api/v1/artifacts", headers=CUST).json()
    assert [item["id"] for item in listing] == [artifact["id"]]  # 拒收零半写

    # title 空白 422（同款口径）
    blank = client.post(
        "/api/v1/artifacts", json={"kind": "record-view", "title": "  "}, headers=CUST
    )
    assert blank.status_code == 422


def test_save_structural_validation_matrix(client, db_session) -> None:
    """结构层校验矩阵：逐条 422 定位 + 零半写（版本行不增）。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    good = _inner(batches_id, spray_id)
    artifact = make_record_view(client, None, publish=False)
    aid = artifact["id"]

    def mutate(**overrides):
        inner = copy.deepcopy(good)
        inner.update(overrides)
        return inner

    def bad_section(**overrides):
        inner = copy.deepcopy(good)
        section = copy.deepcopy(inner["sections"][0])
        section.update(overrides)
        inner["sections"] = [section]
        return inner

    cases: list[tuple[dict, str]] = [
        # 中间态半写拦截：datasetId 空 ⇒ 其余键全空
        (
            {**copy.deepcopy(good), "datasetId": None},
            "spec.spec.keyColumn 必须为空",
        ),
        (
            {
                "datasetId": None,
                "keyColumn": None,
                "headerFields": [{"column": "product"}],
                "sections": [],
            },
            "spec.spec.headerFields 必须为空数组",
        ),
        (
            {
                "datasetId": None,
                "keyColumn": None,
                "headerFields": [],
                "sections": [{"title": "t", "datasetId": spray_id, "dateColumn": "date"}],
            },
            "spec.spec.sections 必须为空数组",
        ),
        # datasetId 绑定后 keyColumn 必须就位
        (mutate(keyColumn=None), "spec.spec.keyColumn 必须是非空字符串"),
        (mutate(datasetId=""), "datasetId 必须是 null 或非空字符串"),
        # headerFields
        (mutate(headerFields=[{"column": "product"}, {"column": "product"}]), "重复"),
        (mutate(headerFields=[{"column": "product", "label": "x" * 201}]), "长度超过 200"),
        (mutate(headerFields=[{"column": " "}]), "headerFields[0].column"),
        (
            mutate(headerFields=[{"column": f"c{i}"} for i in range(51)]),
            "不能超过 50 条",
        ),
        (mutate(headerFields="no"), "headerFields 必须是数组"),
        # sections
        (bad_section(title=" "), "sections[0].title"),
        (bad_section(title="x" * 201), "长度超过 200"),
        (bad_section(datasetId=" "), "sections[0].datasetId"),
        (bad_section(dateColumn=" "), "sections[0].dateColumn"),
        (bad_section(limit=0), "sections[0].limit"),
        (bad_section(limit=501), "sections[0].limit"),
        (bad_section(limit="10"), "sections[0].limit"),
        (bad_section(filters=[{"column": "plot", "op": "contains", "value": "P1"}]), "sections[0].filters[0].op"),
        (bad_section(filters=[{"column": "plot", "op": "eq"}]), "sections[0].filters[0].value"),
        (bad_section(filters=[{"column": "plot", "op": "eq", "value": None}]), "sections[0].filters[0].value"),
        (
            bad_section(filters=[{"column": "plot", "op": "eq", "value": ["P1"]}]),
            "数组形态仅 op=in",
        ),
        (
            bad_section(filters=[{"column": "plot", "op": "eq", "value": {"field": "plot"}}]),
            "{fromField",
        ),
        (
            bad_section(
                filters=[
                    {"column": "plot", "op": "eq", "value": f"v{i}"} for i in range(11)
                ]
            ),
            "≤10 条",
        ),
        (
            mutate(sections=[dict(good["sections"][0]) for _ in range(11)]),
            "sections 不能超过 10 条",
        ),
    ]
    for inner, needle in cases:
        put = client.put(
            f"/api/v1/artifacts/{aid}",
            json={"spec": {"title": TITLE, "spec": inner}},
            headers=CUST,
        )
        assert put.status_code == 422, (needle, put.text)
        assert needle in put.json()["detail"], (needle, put.json()["detail"])
        assert _version_count(db_session, aid) == 1  # 零半写：出生版本不动

    # 合法完整声明保存通过且版本递增（spec.md「溯源页声明」场景）
    put = client.put(
        f"/api/v1/artifacts/{aid}",
        json={"spec": {"title": TITLE, "spec": good}},
        headers=CUST,
    )
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2
    assert _version_count(db_session, aid) == 2


def test_save_ownership_and_column_snapshot(client, db_session) -> None:
    """属主与保存时刻列快照层：主集/section 集他企或不存在 422；keyColumn/
    headerFields.column/sections.dateColumn/filters 的 {fromField} 不在列
    快照 422 定位（同 entity-layer 主键列口径）。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    other_dataset = seed_dataset(db_session, customer_id="cust-999", key="rv_other")
    artifact = make_record_view(client, None, publish=False)
    aid = artifact["id"]

    def put_with(inner):
        return client.put(
            f"/api/v1/artifacts/{aid}",
            json={"spec": {"title": TITLE, "spec": inner}},
            headers=CUST,
        )

    good = _inner(batches_id, spray_id)

    # 主 datasetId 不存在/他企（同一文案，不泄露差异）
    resp = put_with({**copy.deepcopy(good), "datasetId": "no-such-id"})
    assert resp.status_code == 422 and "数据集 no-such-id 不存在" in resp.json()["detail"]
    resp = put_with({**copy.deepcopy(good), "datasetId": other_dataset})
    assert resp.status_code == 422 and "不存在" in resp.json()["detail"]

    # keyColumn 不在主集列快照
    resp = put_with({**copy.deepcopy(good), "keyColumn": "nope"})
    assert resp.status_code == 422
    assert "spec.spec.keyColumn 不在数据集" in resp.json()["detail"]

    # headerFields.column 不在主集列快照
    inner = copy.deepcopy(good)
    inner["headerFields"] = [{"column": "product"}, {"column": "ghost"}]
    resp = put_with(inner)
    assert resp.status_code == 422
    assert "headerFields[1].column 不在数据集" in resp.json()["detail"]

    # sections[].datasetId 他企（定位引用位）
    inner = copy.deepcopy(good)
    inner["sections"][0]["datasetId"] = other_dataset
    resp = put_with(inner)
    assert resp.status_code == 422
    assert "数据集" in resp.json()["detail"] and "sections[0].datasetId" in resp.json()["detail"]

    # sections[].dateColumn 不在该集列快照
    inner = copy.deepcopy(good)
    inner["sections"][0]["dateColumn"] = "ghost_date"
    resp = put_with(inner)
    assert resp.status_code == 422
    assert f"sections[0].dateColumn 不在数据集 {spray_id} 列快照内" in resp.json()["detail"]

    # filters 的 {fromField} 不在主集列快照
    inner = copy.deepcopy(good)
    inner["sections"][0]["filters"] = [
        {"column": "plot", "op": "eq", "value": {"fromField": "ghost"}}
    ]
    resp = put_with(inner)
    assert resp.status_code == 422
    assert (
        f"sections[0].filters[0].value.fromField 不在数据集 {batches_id} 列快照内"
        in resp.json()["detail"]
    )
    assert _version_count(db_session, aid) == 1  # 零半写

    # 合法：section 集可与主集相同
    inner = copy.deepcopy(good)
    inner["sections"][0]["datasetId"] = batches_id
    inner["sections"][0]["dateColumn"] = "batch_no"
    assert put_with(inner).status_code == 200


def test_workbench_and_ai_gate_collections() -> None:
    """展示面收录与 AI 门排除（任务 1.3）：WORKBENCH_KINDS 收录 record-view；
    AI 门（ARTIFACT_TOOL_KINDS 值域）与校验面（ARTIFACT_KINDS）不收录。"""
    assert "record-view" in WORKBENCH_KINDS
    assert "record-view" not in set(ARTIFACT_TOOL_KINDS.values())
    for tool_name, kind in ARTIFACT_TOOL_KINDS.items():
        assert kind != "record-view", tool_name
    assert "record-view" not in set(ARTIFACT_KINDS)
    assert "form" not in set(ARTIFACT_KINDS)  # 校验面口径零漂移（五类）


# ------------------------------------------------------- 2. 分享与长效令牌


def test_verify_token_zero_tier_and_matrix() -> None:
    """verify_token 0 档/正档/非法档双向（任务 2.2，纯函数）：

    0 档（exp=0）跳过过期校验；正档未过期过、过期拒；伪造 exp=0 无密钥
    即签名失配；v 不一致/结构畸形照旧拒（其余校验零变化）。"""
    # 0 档：生成即 exp=0，验签跳过过期（无论墙钟如何）
    token, exp = share_service.build_token(
        SHARE_SECRET, artifact_id=7, version=3, ttl=0
    )
    assert exp == 0
    assert token.split(".")[1] == "0"
    assert share_service.verify_token(
        SHARE_SECRET, artifact_id=7, token=token, v_param=3
    ) == (3, 0)
    # v 不一致照旧拒（0 档不是免检通道）
    with pytest.raises(share_service.ShareLinkInvalid):
        share_service.verify_token(SHARE_SECRET, artifact_id=7, token=token, v_param=4)
    # 换产物重放照旧拒
    with pytest.raises(share_service.ShareLinkInvalid):
        share_service.verify_token(SHARE_SECRET, artifact_id=8, token=token, v_param=3)

    # 伪造 exp=0（无密钥重算 mac）→ 签名失配
    forged = f"3.0.{'ab' * 32}"
    with pytest.raises(share_service.ShareLinkInvalid):
        share_service.verify_token(SHARE_SECRET, artifact_id=7, token=forged, v_param=3)

    # 正档：未过期过、过期拒
    ok_token, _ = share_service.build_token(
        SHARE_SECRET, artifact_id=7, version=3, ttl=3600
    )
    assert share_service.verify_token(
        SHARE_SECRET, artifact_id=7, token=ok_token, v_param=3
    ) == (3, int(ok_token.split(".")[1]))
    stale_token, _ = share_service.build_token(
        SHARE_SECRET, artifact_id=7, version=3, ttl=-10
    )
    assert stale_token.split(".")[1] != "0"
    with pytest.raises(share_service.ShareLinkInvalid):
        share_service.verify_token(SHARE_SECRET, artifact_id=7, token=stale_token, v_param=3)

    # 结构畸形
    with pytest.raises(share_service.ShareLinkInvalid):
        share_service.verify_token(SHARE_SECRET, artifact_id=7, token="a.b", v_param=1)


def test_share_generation_ttl0_and_kinds(client, db_session) -> None:
    """生成面：record-view 进 kind 门与路径分派；0 档 expires_at=null、令牌
    exp=0；draft 403；非法 ttl_days 422 列合法值（含 0）；缺省 30 天零漂移。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    link = _share(client, artifact["id"])
    assert link["url"].startswith(f"/api/v1/share/record-view/{artifact['id']}?t=")
    assert f"&v={artifact['version']}" in link["url"]
    assert link["version"] == artifact["version"]
    assert link["expires_at"] and link["expires_at"].startswith("20")  # 缺省 30 天

    # 0 档：expires_at=null + 令牌中段 "0"
    long_lived = _share(client, artifact["id"], ttl_days=0)
    assert long_lived["expires_at"] is None
    token = long_lived["url"].split("t=")[1].split("&")[0]
    assert token.split(".")[1] == "0"

    # 非法 ttl_days：422 detail 列出全部合法值（0/7/30/90）
    for bad in (5, -1, 365):
        resp = client.post(
            f"/api/v1/artifacts/{artifact['id']}/share",
            headers=CUST,
            json={"ttl_days": bad},
        )
        assert resp.status_code == 422, bad
        for legal in ("0", "7", "30", "90"):
            assert legal in resp.json()["detail"]

    # draft 不可分享（未发布 403）
    draft = make_record_view(client, _inner(batches_id, spray_id), publish=False)
    resp = client.post(f"/api/v1/artifacts/{draft['id']}/share", headers=CUST)
    assert resp.status_code == 403

    # 未绑数据集的中间态：发布后仍不可生成分享（产物契约——空键页无取数
    # 语义，如实在生成面拦下而非给扫码即 404 的链接）
    blank = make_record_view(client, None)
    blocked = client.post(f"/api/v1/artifacts/{blank['id']}/share", headers=CUST)
    assert blocked.status_code == 403
    assert "中间态" in blocked.json()["detail"]


def test_ttl0_shared_across_dashboard_and_form(client, db_session) -> None:
    """0 档三类 kind 同享：dashboard/form 生成 expires_at=null、公开打开
    200 且 expires_at=null（不把 epoch 当到期时间）；既有缺省档零漂移。"""
    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    client.post(f"/api/v1/artifacts/{board['id']}/publish", headers=CUST)
    board0 = _share(client, board["id"], ttl_days=0)
    assert board0["expires_at"] is None
    opened = client.get(board0["url"])
    assert opened.status_code == 200, opened.text
    assert opened.json()["expires_at"] is None

    # form 同规
    target = seed_dataset(db_session, key="rv_form_target")
    form = client.post(
        "/api/v1/artifacts", json={"kind": "form", "title": "登记"}, headers=CUST
    ).json()
    put = client.put(
        f"/api/v1/artifacts/{form['id']}",
        json={
            "spec": {
                "title": "登记",
                "spec": {"title": "登记", "datasetId": target, "fields": []},
            }
        },
        headers=CUST,
    )
    assert put.status_code == 200, put.text
    client.post(f"/api/v1/artifacts/{form['id']}/publish", headers=CUST)
    form0 = _share(client, form["id"], ttl_days=0)
    assert form0["expires_at"] is None
    assert client.get(form0["url"]).json()["expires_at"] is None

    # 缺省档（无 body）零漂移：dashboard 30 天、打开带 ISO 到期
    board_default = _share(client, board["id"])
    assert board_default["expires_at"].startswith("20")
    assert client.get(board_default["url"]).json()["expires_at"] == board_default["expires_at"]


# ------------------------------------------------------- 3. 公开取数端点


def test_public_fetch_happy_path(client, db_session) -> None:
    """扫码取数 happy path：found + header（label 缺省=列名、原样值）+
    section（fromField 代入、dateColumn 升序、__row_id 在场、截断不虚标）。"""
    batches_id, spray_id, harvest_id = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id, harvest_id))
    link = _share(client, artifact["id"])

    resp = _fetch(client, link, "B2026-001")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"title", "version", "expires_at", "found", "header", "sections"}
    assert body["title"] == TITLE
    assert body["version"] == artifact["version"]
    assert body["expires_at"].startswith("20")
    assert body["found"] is True
    # header：label 改写与缺省（=列名）、值原样
    assert body["header"] == [
        {"label": "产品名称", "column": "product", "value": "阳光玫瑰葡萄"},
        {"label": "origin", "column": "origin", "value": "云南"},
    ]
    # sections：P1 的打药记录（fromField 代入 plot=P1，P2 行被滤掉）升序
    spray = body["sections"][0]
    assert spray["title"] == "打药记录"
    assert spray["dateColumn"] == "date"
    assert spray["truncated"] is False
    assert "error" not in spray
    assert [row["date"] for row in spray["rows"]] == ["2026-09-01", "2026-09-10"]
    assert all("__row_id" in row for row in spray["rows"])
    assert {row["plot"] for row in spray["rows"]} == {"P1"}
    # 第二段：采收（字面零过滤 → P1/P2 都在，按日期升序）
    harvest = body["sections"][1]
    assert [row["date"] for row in harvest["rows"]] == ["2026-09-22", "2026-09-25"]

    # v 缺席的规范 URL 形态（D5：#/share/record-view/{id}?t=…&k=…）同样可取
    assert _fetch(client, link, "B2026-001", with_v=False).status_code == 200


def test_public_fetch_key_not_found_and_k_validation(client, db_session) -> None:
    """无此记录：200 found:false、无 header/sections 键（不泄露存在性）；
    k 缺失/空白 422 如实。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    link = _share(client, artifact["id"])

    miss = _fetch(client, link, "NOPE")
    assert miss.status_code == 200
    assert miss.json() == {
        "title": TITLE,
        "version": artifact["version"],
        "expires_at": link["expires_at"],
        "found": False,
    }

    base, _, query = link["url"].partition("?")
    token = query.split("t=")[1].split("&")[0]
    no_k = client.get(f"{base}?t={token}&v={artifact['version']}")
    assert no_k.status_code == 422
    assert "k" in no_k.json()["detail"] and "非空" in no_k.json()["detail"]
    blank_k = client.get(f"{base}?t={token}&v={artifact['version']}&k=%20%20")
    assert blank_k.status_code == 422


def test_public_fetch_multi_row_takes_first(client, db_session) -> None:
    """主记录键不唯一：等值多行取首行（api 档 row_index 稳定序）。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    seed_rows(
        db_session,
        batches_id,
        [
            {"batch_no": "DUP", "product": "第一条", "origin": "甲", "plot": "P1"},
            {"batch_no": "DUP", "product": "第二条", "origin": "乙", "plot": "P2"},
        ],
        start=2,  # 接在溯源夹具两行之后（dataset_rows 行锚唯一）
    )
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    link = _share(client, artifact["id"])
    body = _fetch(client, link, "DUP").json()
    assert body["found"] is True
    values = {h["column"]: h["value"] for h in body["header"]}
    assert values["product"] == "第一条"


def test_public_fetch_limit_truncation_and_nulls_last(client, db_session) -> None:
    """limit 截断（limit+1 判定，truncated 如实）与空/非法日期排末尾稳定序。"""
    batches_id, _spray, _harvest = seed_traceability_fixtures(db_session)
    events_id = seed_dataset(db_session, key="rv_events", columns=SPRAY_COLUMNS)
    seed_rows(
        db_session,
        events_id,
        [
            {"plot": "P1", "date": "2026-09-10", "chemical": "三"},
            {"plot": "P1", "date": "2026-09-01", "chemical": "一"},
            {"plot": "P1", "date": "2026-09-05", "chemical": "二"},
        ],
    )
    inner = {
        "datasetId": batches_id,
        "keyColumn": "batch_no",
        "headerFields": [],
        "sections": [
            {
                "title": "事件",
                "datasetId": events_id,
                "dateColumn": "date",
                "filters": [{"column": "plot", "op": "eq", "value": "P1"}],
                "limit": 2,
            }
        ],
    }
    artifact = make_record_view(client, inner)
    link = _share(client, artifact["id"])
    section = _fetch(client, link, "B2026-001").json()["sections"][0]
    assert [row["date"] for row in section["rows"]] == ["2026-09-01", "2026-09-05"]
    assert section["truncated"] is True

    # 空/非法日期排末尾（稳定序：日期形行在前保持升序，其余沉底）
    messy_id = seed_dataset(db_session, key="rv_messy", columns=SPRAY_COLUMNS)
    seed_rows(
        db_session,
        messy_id,
        [
            {"plot": "P1", "date": "2026-09-10", "chemical": "日期晚"},
            {"plot": "P1", "date": None, "chemical": "空日期"},
            {"plot": "P1", "date": "2026-09-01", "chemical": "日期早"},
            {"plot": "P1", "date": "not-a-date", "chemical": "非法日期"},
        ],
    )
    inner["sections"][0]["datasetId"] = messy_id
    inner["sections"][0].pop("limit")
    artifact2 = make_record_view(client, inner)
    link2 = _share(client, artifact2["id"])
    rows = _fetch(client, link2, "B2026-001").json()["sections"][0]["rows"]
    assert [row["chemical"] for row in rows] == ["日期早", "日期晚", "空日期", "非法日期"]


def test_public_fetch_section_error_isolation(client, db_session) -> None:
    """引用失效如实：dateColumn 漂移/section 集删除 = 单段 error + 空 rows，
    其余段与 header 照常（spec「引用失效如实」场景）。"""
    batches_id, spray_id, harvest_id = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id, harvest_id))
    link = _share(client, artifact["id"])

    # dateColumn 从列快照移除（保存后漂移——append-only 不回溯）
    _drop_column(db_session, spray_id, "date")
    body = _fetch(client, link, "B2026-001").json()
    spray = body["sections"][0]
    assert spray["rows"] == []
    assert spray["truncated"] is False
    assert "date" in spray["error"]
    harvest = body["sections"][1]
    assert "error" not in harvest
    assert [row["date"] for row in harvest["rows"]] == ["2026-09-22", "2026-09-25"]
    assert body["found"] is True
    assert body["header"][0]["value"] == "阳光玫瑰葡萄"

    # section 数据集整表删除 → 同为单段 error（不泄露差异细节除外）
    db_session.execute(Dataset.__table__.delete().where(Dataset.id == harvest_id))
    db_session.commit()
    share_routes._reset_recordview_cache()  # 绕过短缓存看实时解析
    body2 = _fetch(client, link, "B2026-001").json()
    assert body2["sections"][1]["rows"] == []
    assert "不可用" in body2["sections"][1]["error"]
    assert body2["sections"][0] == body["sections"][0]  # 失败段照常


def test_public_fetch_uniform_404_matrix(client, db_session) -> None:
    """统一 404 矩阵：缺 t/篡改/v 不一致/结构畸形/产物不存在/非 record-view
    产物/归档断链——detail 恒 "not found"；k 缺失先 422（人机参数如实）。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    link = _share(client, artifact["id"])
    base, _, query = link["url"].partition("?")
    token = query.split("t=")[1].split("&")[0]
    version = link["version"]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    client.post(f"/api/v1/artifacts/{board['id']}/publish", headers=CUST)
    board_link = _share(client, board["id"])
    board_base, _, board_query = board_link["url"].partition("?")

    cases = [
        base,  # 缺 t 缺 k
        f"{base}?t={token}&v={version}",  # 缺 k（422 如实，非 404）
        f"{base}?t={tampered}&v={version}&k=B1",  # 篡改 mac
        f"{base}?t={token}&v=99&k=B1",  # v 不一致
        f"{base}?t=not-a-token&v=1&k=B1",  # 结构畸形
        "/api/v1/share/record-view/99999?t=1.2.ab&v=1&k=x",  # 产物不存在
        f"/api/v1/share/record-view/{board['id']}?{board_query}&k=B1",  # dashboard 令牌走不进
    ]
    for url in cases[2:]:
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert resp.json()["detail"] == "not found", url
    missing_k = client.get(cases[1])
    assert missing_k.status_code == 422
    assert client.get(cases[0]).status_code == 404

    # 归档即失效（撤销闭环：二维码指向统一 404 态）
    assert _fetch(client, link, "B2026-001").status_code == 200
    client.post(f"/api/v1/artifacts/{artifact['id']}/archive", headers=CUST)
    archived = _fetch(client, link, "B2026-001")
    assert archived.status_code == 404
    assert archived.json()["detail"] == "not found"


def test_public_fetch_pinned_version(client, db_session) -> None:
    """钉死版本：改版后旧令牌仍按旧版本取数（旧 header 形状），新令牌按新
    版本。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    old_link = _share(client, artifact["id"])

    inner = copy.deepcopy(_inner(batches_id, spray_id))
    inner["headerFields"] = [{"column": "origin", "label": "产地"}]
    put = client.put(
        f"/api/v1/artifacts/{artifact['id']}",
        json={"spec": {"title": TITLE, "spec": inner}},
        headers=CUST,
    )
    assert put.status_code == 200 and put.json()["version"] == artifact["version"] + 1

    old = _fetch(client, old_link, "B2026-001").json()
    assert old["version"] == artifact["version"]
    assert [h["column"] for h in old["header"]] == ["product", "origin"]
    new_link = _share(client, artifact["id"])
    new = _fetch(client, new_link, "B2026-001").json()
    assert new["version"] == put.json()["version"]
    assert [h["column"] for h in new["header"]] == ["origin"]


def test_public_fetch_correction_visible_after_share(client, db_session) -> None:
    """补录后扫码可见：分享生成后 insert 修正落入有效视图，下次扫码该节点
    出现在时间轴（缓存 TTL 内至多延迟 60 秒——用例直接复位缓存模拟到期）。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    link = _share(client, artifact["id"])
    before = _fetch(client, link, "B2026-001").json()
    assert [row["date"] for row in before["sections"][0]["rows"]] == [
        "2026-09-01",
        "2026-09-10",
    ]

    db_session.add(
        DatasetCorrection(
            dataset_id=spray_id,
            op="insert",
            value_json={"plot": "P1", "date": "2026-09-20", "chemical": "补录阿维菌素"},
            actor="test",
        )
    )
    db_session.commit()
    share_routes._reset_recordview_cache()
    after = _fetch(client, link, "B2026-001").json()
    assert [row["date"] for row in after["sections"][0]["rows"]] == [
        "2026-09-01",
        "2026-09-10",
        "2026-09-20",
    ]
    assert after["sections"][0]["rows"][-1]["__row_id"].startswith("c")


def test_public_fetch_cell_tier_declared_queries(client, db_session) -> None:
    """cell 双档：引擎收到且仅收到声明形状的查询——主记录（keyColumn 等值、
    limit 1）与 section（fromField 代入后的 filters、dateColumn asc 排序、
    limit+1）；载荷从引擎响应组装（行含 __row_id 时原样）。"""
    batches_cell = seed_dataset(
        db_session,
        key="rv_batch_cell",
        tier="cell",
        columns=BATCH_COLUMNS,
    )
    spray_cell = seed_dataset(
        db_session,
        key="rv_spray_cell",
        tier="cell",
        columns=SPRAY_COLUMNS,
    )
    inner = {
        "datasetId": batches_cell,
        "keyColumn": "batch_no",
        "headerFields": [{"column": "product", "label": "产品名称"}],
        "sections": [
            {
                "title": "打药记录",
                "datasetId": spray_cell,
                "dateColumn": "date",
                "filters": [
                    {"column": "plot", "op": "eq", "value": {"fromField": "plot"}}
                ],
                "limit": 3,
            }
        ],
    }
    artifact = make_record_view(client, inner)
    link = _share(client, artifact["id"])

    def scripted(arguments: dict) -> dict:
        if arguments.get("dataset_key") == "rv_batch_cell":
            return {
                "columns": [{"name": c["name"], "type": c["type"]} for c in BATCH_COLUMNS],
                "rows": [
                    {
                        "batch_no": "B1",
                        "product": "车厘子",
                        "origin": "智利",
                        "plot": "P9",
                        "__row_id": "0",
                    }
                ],
                "total": 1,
            }
        return {
            "columns": [{"name": c["name"], "type": c["type"]} for c in SPRAY_COLUMNS],
            "rows": [
                {"plot": "P9", "date": "2026-08-02", "chemical": "乙", "__row_id": "0"},
                {"plot": "P9", "date": "2026-08-01", "chemical": "甲", "__row_id": "1"},
            ],
            "total": 2,
        }

    class CountingEngine(ScriptedEngine):
        def call(self, tool: str, arguments: dict) -> dict:
            if tool == TOOL_QUERY:
                self.responses[TOOL_QUERY] = scripted(arguments)
            return super().call(tool, arguments)

    counting = CountingEngine()
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: counting
    )
    resp = _fetch(client, link, "B1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["header"] == [
        {"label": "产品名称", "column": "product", "value": "车厘子"}
    ]
    section = body["sections"][0]
    assert [row["date"] for row in section["rows"]] == ["2026-08-01", "2026-08-02"]
    assert section["truncated"] is False

    queries = counting.calls_for(TOOL_QUERY)
    assert len(queries) == 2
    main_query, section_query = queries
    assert main_query["dataset_key"] == "rv_batch_cell"
    assert main_query["filters"] == [{"column": "batch_no", "op": "eq", "value": "B1"}]
    assert main_query["limit"] == 1
    assert section_query["dataset_key"] == "rv_spray_cell"
    assert section_query["filters"] == [{"column": "plot", "op": "eq", "value": "P9"}]
    assert section_query["sort"] == [{"column": "date", "dir": "asc"}]
    assert section_query["limit"] == 4  # limit 3 + 1（截断可判定）


def test_public_fetch_engine_failure_matrix(client, db_session, engine_fake) -> None:
    """引擎失败分置：cell 主记录失败 = 502 fail-closed；免费主记录 + cell
    section 失败 = 单段 error、头部照常（隔离边界同列漂移）。"""
    from fd_wire_api.datasets.ingest import EngineCallError

    # cell 主记录 + 引擎失败 → 502
    batches_cell = seed_dataset(
        db_session, key="rv_fail_batch", tier="cell", columns=BATCH_COLUMNS
    )
    inner = {
        "datasetId": batches_cell,
        "keyColumn": "batch_no",
        "headerFields": [],
        "sections": [],
    }
    artifact = make_record_view(client, inner)
    link = _share(client, artifact["id"])
    engine_fake.responses[TOOL_QUERY] = EngineCallError("engine down")
    resp = _fetch(client, link, "B1")
    assert resp.status_code == 502
    assert "engine" in resp.json()["detail"]

    # 免费主记录 + cell section 失败 → 200 + 单段 error（其余照常）
    batches_api = seed_dataset(
        db_session, key="rv_fail_batch_api", columns=BATCH_COLUMNS
    )
    seed_rows(
        db_session,
        batches_api,
        [{"batch_no": "B1", "product": "梨", "origin": "河北", "plot": "P1"}],
    )
    spray_cell = seed_dataset(
        db_session, key="rv_fail_spray", tier="cell", columns=SPRAY_COLUMNS
    )
    inner2 = {
        "datasetId": batches_api,
        "keyColumn": "batch_no",
        "headerFields": [{"column": "product"}],
        "sections": [
            {"title": "打药", "datasetId": spray_cell, "dateColumn": "date"}
        ],
    }
    artifact2 = make_record_view(client, inner2)
    link2 = _share(client, artifact2["id"])
    mixed = _fetch(client, link2, "B1")
    assert mixed.status_code == 200, mixed.text
    body = mixed.json()
    assert body["found"] is True
    assert body["header"] == [{"label": "product", "column": "product", "value": "梨"}]
    assert body["sections"][0]["rows"] == []
    assert "engine down" in body["sections"][0]["error"]


def test_rate_limit_env_default_buckets_and_429(client, db_session, monkeypatch) -> None:
    """限流：缺省 240/h、env 惰性读（非法回退）；超限 429 带 Retry-After
    且不落查询（cell 档引擎调用数不增）；新令牌（新 mac）独立桶；与其他
    公开面限流器分桶（提交桶不受连坐）。"""
    assert rate_limit.recordview_rate_per_hour() == 240
    monkeypatch.setenv("FD_WIRE_SHARE_RECORDVIEW_RATE_PER_HOUR", "abc")
    assert rate_limit.recordview_rate_per_hour() == 240
    monkeypatch.setenv("FD_WIRE_SHARE_RECORDVIEW_RATE_PER_HOUR", "2")
    assert rate_limit.recordview_rate_per_hour() == 2

    batches_cell = seed_dataset(
        db_session, key="rv_rl_batch", tier="cell", columns=BATCH_COLUMNS
    )
    inner = {
        "datasetId": batches_cell,
        "keyColumn": "batch_no",
        "headerFields": [],
        "sections": [],
    }
    artifact = make_record_view(client, inner)
    link = _share(client, artifact["id"])

    static_response = {
        "columns": [{"name": c["name"], "type": c["type"]} for c in BATCH_COLUMNS],
        "rows": [{"batch_no": "B1", "product": "梨", "origin": "冀", "plot": "P1"}],
        "total": 1,
    }

    class CountingEngine(ScriptedEngine):
        def call(self, tool: str, arguments: dict) -> dict:
            if tool == TOOL_QUERY:
                self.responses[TOOL_QUERY] = static_response
            return super().call(tool, arguments)

    counting = CountingEngine()
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: counting
    )
    url_a = _fetch_url(link, "B1")
    url_b = _fetch_url(link, "B2")
    assert client.get(url_a).status_code == 200
    assert client.get(url_b).status_code == 200  # 不同 k 各占一个窗位
    limited = client.get(url_a)
    assert limited.status_code == 429
    assert "频繁" in limited.json()["detail"]
    assert int(limited.headers["Retry-After"]) >= 1
    assert len(counting.calls_for(TOOL_QUERY)) == 2  # 超限请求不落查询

    # 新令牌（新 mac）= 新桶
    fresh = _share(client, artifact["id"], ttl_days=7)
    assert _fetch(client, fresh, "B1").status_code == 200

    # 与其他公开面限流器分桶：record-view 桶占满后提交桶额度独立
    assert rate_limit.allow("share-form:9") == (True, 0)


def test_short_cache_query_counting(client, db_session) -> None:
    """60 秒短缓存（计数证明）：同（产物/版本/令牌 mac/k）第二击零底层
    查询且载荷逐字回放；新 k / 新令牌重建；TTL 内数据变化呈现缓存值。"""
    batches_cell = seed_dataset(
        db_session, key="rv_cache_batch", tier="cell", columns=BATCH_COLUMNS
    )
    spray_cell = seed_dataset(
        db_session, key="rv_cache_spray", tier="cell", columns=SPRAY_COLUMNS
    )
    inner = {
        "datasetId": batches_cell,
        "keyColumn": "batch_no",
        "headerFields": [{"column": "product"}],
        "sections": [
            {"title": "打药", "datasetId": spray_cell, "dateColumn": "date", "filters": []}
        ],
    }
    artifact = make_record_view(client, inner)
    link = _share(client, artifact["id"])

    state = {"product": "初值"}

    def scripted(arguments: dict) -> dict:
        if arguments.get("dataset_key") == "rv_cache_batch":
            return {
                "columns": [{"name": c["name"], "type": c["type"]} for c in BATCH_COLUMNS],
                "rows": [
                    {
                        "batch_no": "B1",
                        "product": state["product"],
                        "origin": "冀",
                        "plot": "P1",
                    }
                ],
                "total": 1,
            }
        return {
            "columns": [{"name": c["name"], "type": c["type"]} for c in SPRAY_COLUMNS],
            "rows": [{"plot": "P1", "date": "2026-09-01", "chemical": "甲"}],
            "total": 1,
        }

    class CountingEngine(ScriptedEngine):
        def call(self, tool: str, arguments: dict) -> dict:
            if tool == TOOL_QUERY:
                self.responses[TOOL_QUERY] = scripted(arguments)
            return super().call(tool, arguments)

    counting = CountingEngine()
    app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: counting
    )
    url = _fetch_url(link, "B1")
    first = client.get(url)
    assert first.status_code == 200, first.text
    assert len(counting.calls_for(TOOL_QUERY)) == 2
    body_first = first.json()

    # TTL 内第二击：零新增查询、载荷逐字回放（数据已变仍呈现缓存值）
    state["product"] = "已变化"
    second = client.get(url)
    assert second.status_code == 200
    assert second.json() == body_first
    assert len(counting.calls_for(TOOL_QUERY)) == 2

    # 新 k → 键不同 → 重建（再 +2），拿到新值
    third = client.get(_fetch_url(link, "B2"))
    assert third.status_code == 200
    assert third.json()["header"][0]["value"] == "已变化"
    assert len(counting.calls_for(TOOL_QUERY)) == 4

    # 新令牌（不同 mac）→ 键不同 → 重建（再 +2）
    fresh = _share(client, artifact["id"], ttl_days=7)
    fourth = _fetch(client, fresh, "B1")
    assert fourth.status_code == 200
    assert len(counting.calls_for(TOOL_QUERY)) == 6


def test_public_fetch_zero_tier_long_lived(client, db_session) -> None:
    """0 档令牌公开取数：expires_at=null、反复扫码恒 200（exp=0 跳过过期）。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    link = _share(client, artifact["id"], ttl_days=0)
    assert link["expires_at"] is None
    for _ in range(3):
        resp = _fetch(client, link, "B2026-001")
        assert resp.status_code == 200
        assert resp.json()["expires_at"] is None
        assert resp.json()["found"] is True


# ------------------------------------------------------- 4.2 preview 端点


def test_preview_same_payload_as_public(client, db_session) -> None:
    """preview 与公开端点同一组装函数：同 k 同载荷（除 expires_at 恒 null
    与版本来源）；draft 亦可预览（不验令牌不生成链接）。"""
    batches_id, spray_id, harvest_id = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id, harvest_id))
    link = _share(client, artifact["id"])

    public = _fetch(client, link, "B2026-001").json()
    resp = client.post(
        f"/api/v1/artifacts/{artifact['id']}/record-view/preview",
        json={"key": "B2026-001"},
        headers=CUST,
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["expires_at"] is None
    assert preview["title"] == public["title"]
    assert preview["version"] == public["version"]
    assert preview["found"] == public["found"]
    assert preview["header"] == public["header"]
    assert preview["sections"] == public["sections"]

    # 无此记录态同构
    miss = client.post(
        f"/api/v1/artifacts/{artifact['id']}/record-view/preview",
        json={"key": "NOPE"},
        headers=CUST,
    )
    assert miss.status_code == 200
    assert miss.json()["found"] is False
    assert set(miss.json()) == {"title", "version", "expires_at", "found"}

    # draft（未发布）可预览——构建器边配边看
    draft = make_record_view(client, _inner(batches_id, spray_id), publish=False)
    draft_preview = client.post(
        f"/api/v1/artifacts/{draft['id']}/record-view/preview",
        json={"key": "B2026-001"},
        headers=CUST,
    )
    assert draft_preview.status_code == 200
    assert draft_preview.json()["found"] is True


def test_preview_gates_and_current_version(client, db_session) -> None:
    """preview 闸：跨客户 404、kind≠record-view 422、key 空白 422、中间态
    422；当前版本语义——改版后预览随新版本；keyColumn 漂移 400 如实
    （与公开端点统一 404 分置）。"""
    batches_id, spray_id, _harvest = seed_traceability_fixtures(db_session)
    artifact = make_record_view(client, _inner(batches_id, spray_id))
    aid = artifact["id"]

    # 跨客户 404
    other = client.post(
        f"/api/v1/artifacts/{aid}/record-view/preview",
        json={"key": "B2026-001"},
        headers=OTHER,
    )
    assert other.status_code == 404

    # kind 门 422（dashboard）
    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    kind_resp = client.post(
        f"/api/v1/artifacts/{board['id']}/record-view/preview",
        json={"key": "x"},
        headers=CUST,
    )
    assert kind_resp.status_code == 422
    assert "record-view" in kind_resp.json()["detail"]

    # key 空白 422；缺 key 字段 FastAPI 422
    assert (
        client.post(
            f"/api/v1/artifacts/{aid}/record-view/preview", json={"key": " "}, headers=CUST
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/artifacts/{aid}/record-view/preview", json={}, headers=CUST
        ).status_code
        == 422
    )

    # 中间态（空白创建未配置）422
    blank = make_record_view(client, None, publish=False)
    unbound = client.post(
        f"/api/v1/artifacts/{blank['id']}/record-view/preview",
        json={"key": "B2026-001"},
        headers=CUST,
    )
    assert unbound.status_code == 422
    assert "配置" in unbound.json()["detail"]

    # 当前版本语义：改版换 headerFields 后预览随之（版本号同步递增）
    inner = copy.deepcopy(_inner(batches_id, spray_id))
    inner["headerFields"] = [{"column": "plot", "label": "地块"}]
    put = client.put(
        f"/api/v1/artifacts/{aid}",
        json={"spec": {"title": TITLE, "spec": inner}},
        headers=CUST,
    )
    assert put.status_code == 200
    preview = client.post(
        f"/api/v1/artifacts/{aid}/record-view/preview",
        json={"key": "B2026-001"},
        headers=CUST,
    ).json()
    assert preview["version"] == put.json()["version"]
    assert [h["column"] for h in preview["header"]] == ["plot"]

    # keyColumn 漂移：preview 400 如实带列名（公开端点为统一 404）
    _drop_column(db_session, batches_id, "batch_no")
    drifted = client.post(
        f"/api/v1/artifacts/{aid}/record-view/preview",
        json={"key": "B2026-001"},
        headers=CUST,
    )
    assert drifted.status_code == 400
    assert "batch_no" in drifted.json()["detail"]
    link = _share(client, aid)
    public_drifted = _fetch(client, link, "B2026-001")
    assert public_drifted.status_code == 404
    assert public_drifted.json()["detail"] == "not found"
