"""表单分享与公开提交 API 测试（wire-form-builder 任务 3.1–3.4，design D3/D4）。

覆盖：
- 3.1 生成链接 kind 门与路径分派：form → /api/v1/share/form/{id}、dashboard
  仍 dashboard 路径、白名单外 kind 403、draft/archived 403、跨客户 404；
- 3.2 限流器单测：env 惰性读/非法回退、滑动窗与 Retry-After、被拒不占窗位、
  线程并发恰好放行 limit 次、mac 段键提取（每令牌独立桶）、reset 复位；
- 3.3 validate_form_values 纯函数矩阵（归一/未知列/必填/类型四类逐条）+
  公开 GET（钉死版本信封原样）+ 公开 POST submit（design D3 校验序全链）；
  3.3b visibleWhen 求值矩阵（wire-form-field-linkage：九算子/空值口径/
  双端十进制/无递归——与 web 侧同款对拍）+ 提交三态联动（隐藏必填跳过、
  隐藏携带值拒）；3.3c v1+ 联动（wire-form-linkage-v2）：visibleWhenGroup
  all/any 求值矩阵、optionsFrom 解析与空选项按隐藏、动态成员校验、公开
  提交级联正例与伪造非选项值 422（防篡改）；
- 3.4 公开提交全矩阵：成功落 insert 修正且 actor=share-form:{id}、改版后
  旧令牌按旧字段集校验、未知列/缺必填/类型错 422 零写入、限流 429+
  Retry-After+超限不落库、归档断链 404、datasetId 未绑/数据集被删/他企
  404 统一、钉死版本非 form 形状 404、列漂移 400 如实、免费档行数闸 429、
  cell 档透传引擎（假引擎工厂断言 actor 与报文）与引擎错 502、secret 未配
  三端点 503。

环境：ai（artifacts 两表）与 datasets 两库同引擎合库（生产分库拓扑由路由
依赖签名保证——get_db 与 get_datasets_db 各自注入，照 test_form_artifacts
先例）；引擎工厂假件记录 TOOL_CORRECTION_ADD 调用；限流器逐用例复位。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.ai.models import AIDraftArtifact
from fd_wire_api.ai.schemas import ConversationCreate
from fd_wire_api.ai.service import create_conversation
from fd_wire_api.app import app
from fd_wire_api.artifacts.form_spec import (
    evaluate_hidden_columns,
    resolve_select_options,
    validate_form_values,
)
from fd_wire_api.artifacts.models import ArtifactVersion
from fd_wire_api.datasets import api_store
from fd_wire_api.datasets.db import (
    DatasetsBase,
    get_db as get_datasets_db,
    reset_datasets_runtime,
)
from fd_wire_api.datasets.ingest import (
    TOOL_CORRECTION_ADD,
    EngineUnavailable,
    get_engine_client_factory,
)
from fd_wire_api.datasets.models import Dataset, DatasetColumn, DatasetCorrection
from fd_wire_api.share import rate_limit

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-share-form-001"
SUB_OTHER = "sub-share-form-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

SHARE_SECRET = "test-share-form-secret-32bytes"

#: 标准数据集列（region/amount/day/note——与标准字段集一一对应）
DS_COLUMNS = [
    {"name": "region", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
    {"name": "day", "type": "TEXT"},
    {"name": "note", "type": "TEXT"},
]

#: 标准字段集：select 必填 / number / date / text（后三个非必填）
FIELDS = [
    {
        "column": "region",
        "label": "区域",
        "input": "select",
        "required": True,
        "options": ["east", "west", "north"],
    },
    {"column": "amount", "label": "金额", "input": "number"},
    {"column": "day", "label": "日期", "input": "date"},
    {"column": "note", "label": "备注", "input": "text"},
]


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
    """覆写 ai/datasets 两库会话与引擎工厂假件的 TestClient；分享 secret
    已配置、限流器已复位（模块级状态不跨用例累计）。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", SHARE_SECRET)
    monkeypatch.delenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", raising=False)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    rate_limit.reset()
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
    reset_ai_runtime()


@pytest.fixture()
def tight_row_gate(monkeypatch):
    """免费档行数闸=1：改 env 后必须清 settings 缓存才生效（用后复位，
    不污染后续用例的 get_settings 单例）。"""
    monkeypatch.setenv("FD_WIRE_FREE_MAX_ROWS", "1")
    reset_datasets_runtime()
    yield
    reset_datasets_runtime()


# ------------------------------------------------------------------ 流程助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = "cust-001",
    key: str = "form_src",
    tier: str = "api",
    columns: list[dict] | None = None,
) -> str:
    """直插一条数据集登记（含 api 层列注册表），返回 dataset id。"""
    columns = DS_COLUMNS if columns is None else columns
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


def make_form(
    client,
    *,
    dataset_id: str | None = None,
    fields: list[dict] | None = None,
    success_message: str | None = "登记成功，感谢填写",
    publish: bool = True,
) -> dict:
    """空白建 form →（可选）PUT 字段配置 →（可选）发布，返回产物详情。

    带 fields 时产物版本 = 2（空白 v1 + 配置 v2）。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "form", "title": "访客登记"}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    if fields is not None:
        inner: dict = {"title": "访客登记", "datasetId": dataset_id, "fields": fields}
        if success_message is not None:
            inner["successMessage"] = success_message
        put = client.put(
            f"/api/v1/artifacts/{artifact['id']}",
            json={"spec": {"title": "访客登记", "spec": inner}},
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
    """生成分享链接；ttl_days 显式给出时带 body（改变 exp 即改变 mac——
    限流按令牌分桶时用于确定性造出「另一把令牌」）。"""
    json = None if ttl_days is None else {"ttl_days": ttl_days}
    resp = client.post(
        f"/api/v1/artifacts/{artifact_id}/share", headers=CUST, json=json
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _share_with_ttl(client, artifact_id: int, *, ttl_seconds: int) -> dict:
    """以 env TTL 覆盖生成（负值=生成即过期）。"""
    os.environ["FD_WIRE_SHARE_TTL_SECONDS"] = str(ttl_seconds)
    try:
        return _share(client, artifact_id)
    finally:
        os.environ.pop("FD_WIRE_SHARE_TTL_SECONDS", None)


def _submit(client, link_url: str, values: dict):
    """公开提交捷径：分享 url 的同 query 挂到 /submit。"""
    base, _, query = link_url.partition("?")
    return client.post(f"{base}/submit?{query}", json={"values": values})


def _corrections(db_session, dataset_id: str) -> list[DatasetCorrection]:
    return list(
        db_session.execute(
            select(DatasetCorrection).where(DatasetCorrection.dataset_id == dataset_id)
        ).scalars().all()
    )


# --------------------------------------------------------------- 3.2 限流器


def test_rate_limit_env_lazy_read_and_fallback(monkeypatch) -> None:
    """阈值惰性逐请求读 env：缺省 60、非法/非正数回退 60、合法值生效。"""
    monkeypatch.delenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", raising=False)
    assert rate_limit.rate_per_hour() == 60
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "3")
    assert rate_limit.rate_per_hour() == 3
    for bad in ("abc", "", "  ", "0", "-5", "2.5"):
        monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", bad)
        assert rate_limit.rate_per_hour() == 60, bad
    # 同进程内逐请求生效（改 env 即改阈值，无需重启）
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "2")
    assert rate_limit.rate_per_hour() == 2


def test_rate_limit_sliding_window_and_retry_after(monkeypatch) -> None:
    """滑动窗：超限拒绝带 Retry-After（窗首出队秒数，≥1）；被拒不占窗位；
    窗滑过首命中后恢复放行。"""
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "2")
    rate_limit.reset()
    assert rate_limit.allow("k", now=100.0) == (True, 0)
    assert rate_limit.allow("k", now=100.5) == (True, 0)
    # 超限：retry_after = 窗首(100.0) + 3600 - 101.0
    ok, retry_after = rate_limit.allow("k", now=101.0)
    assert ok is False
    assert retry_after == 3599
    # 连续被拒（不占窗位——窗位仍是 2，retry_after 随时间递减）
    assert rate_limit.allow("k", now=102.0) == (False, 3598)
    # 窗内（cutoff=0.6 < 窗首 100.0）：继续拒绝，retry_after = 100+3600-3600.6
    assert rate_limit.allow("k", now=3600.6) == (False, 99)
    # 窗滑过首命中（100.0 ≤ cutoff 100.1）后：只剩 1 个窗位 → 放行
    assert rate_limit.allow("k", now=3700.1) == (True, 0)
    # 不足 1 秒的 retry_after 向上取整到至少 1（Retry-After 头不出现 0）
    rate_limit.reset()
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "1")
    assert rate_limit.allow("k2", now=0.0) == (True, 0)
    ok, retry_after = rate_limit.allow("k2", now=3599.9)
    assert ok is False
    assert retry_after == 1
    rate_limit.reset()


def test_rate_limit_per_key_buckets(monkeypatch) -> None:
    """键 = 令牌 mac 段：不同令牌各自独立额度（重新生成链接=新桶）。"""
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "1")
    rate_limit.reset()
    assert rate_limit.allow("mac-a", now=10.0) == (True, 0)
    assert rate_limit.allow("mac-a", now=10.5) == (False, 3599)
    assert rate_limit.allow("mac-b", now=10.5) == (True, 0)  # 另一令牌不受连坐
    rate_limit.reset()


def test_rate_limit_concurrent_thread_safety(monkeypatch) -> None:
    """并发安全：8 线程 × 10 次打同一键、limit=50——恰好放行 50 次
    （锁内判定+记窗，无超放无重复）。"""
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "50")
    rate_limit.reset()
    barrier = threading.Barrier(8)
    counts: list[int] = []
    counts_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        ok_count = 0
        for _ in range(10):
            ok, _ = rate_limit.allow("shared-key")
            if ok:
                ok_count += 1
        with counts_lock:
            counts.append(ok_count)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(counts) == 50
    rate_limit.reset()


def test_rate_limit_mac_segment_and_reset() -> None:
    """mac 段提取（第三段；非三段 ValueError）；reset 清空窗位。"""
    assert rate_limit.mac_segment("1.2.deadbeef") == "deadbeef"
    assert rate_limit.mac_segment("v.e.m") == "m"
    with pytest.raises(ValueError, match="三段"):
        rate_limit.mac_segment("1.2")
    # reset 后同键立即恢复额度
    rate_limit.reset()
    assert rate_limit.allow("k3", now=1.0) == (True, 0)
    rate_limit.reset()
    assert rate_limit.allow("k3", now=1.1) == (True, 0)
    rate_limit.reset()


# ------------------------------------------- 3.3a validate_form_values 纯函数


def test_validate_form_values_matrix() -> None:
    """values 校验矩阵：归一（number 字符串→数值、text 标量 stringify）、
    未知列、必填缺失/空/null、四类 input 类型错、可选未传=不写键。"""
    # 合法 + 归一：number 字符串→float、text 数字→字符串、未传字段不写键
    normalized, error = validate_form_values(
        FIELDS,
        {"region": "east", "amount": "12.5", "day": "2026-10-12", "note": 3},
    )
    assert error is None
    assert normalized == {
        "region": "east",
        "amount": 12.5,
        "day": "2026-10-12",
        "note": "3",
    }
    # 原生数值保持类型；text 布尔按 JSON 口径 stringify
    normalized2, error2 = validate_form_values(
        FIELDS, {"region": "west", "amount": 7, "note": True}
    )
    assert error2 is None
    assert normalized2["amount"] == 7
    assert normalized2["note"] == "true"
    # 可选字段未传/传 null/空串 = 跳过（不写键）
    normalized3, error3 = validate_form_values(
        FIELDS, {"region": "west", "amount": None, "note": ""}
    )
    assert error3 is None
    assert normalized3 == {"region": "west"}
    # 空 values + 无必填字段 → 合法空对象（下游 insert 侧如实拒）
    empty, empty_error = validate_form_values([], {})
    assert empty_error is None
    assert empty == {}

    # values 非对象 / fields 非数组
    assert validate_form_values(FIELDS, ["x"]) == (
        None,
        "values 必须是 JSON 对象（整行值）",
    )
    assert validate_form_values(None, {}) == (None, "表单字段定义缺失（fields 必须是数组）")

    # 未知列：文案带列名与可用字段
    values, error = validate_form_values(FIELDS, {"region": "east", "ghost": 1})
    assert values is None
    assert "ghost" in error and "region" in error

    # 必填：缺失 / null / 空串 / 仅空白
    for bad in ({}, {"region": None}, {"region": ""}, {"region": "   "}):
        values, error = validate_form_values(FIELDS, bad)
        assert values is None, bad
        assert "必填字段" in error and "region" in error, bad

    # number：非数值串 / 布尔 / NaN / Inf / 列表
    for bad in ("abc", True, "nan", "inf", "-inf", [1]):
        values, error = validate_form_values(FIELDS, {"region": "east", "amount": bad})
        assert values is None, bad
        assert "金额" in error and "amount" in error, bad
    # 非有限原生浮点同样拒
    values, error = validate_form_values(
        FIELDS, {"region": "east", "amount": float("nan")}
    )
    assert values is None and "有限" in error

    # date：带时间 / 斜杠 / 紧凑数字 / 非字符串（null=未填，按可选跳过）
    for bad in ("2026-10-12T00:00:00", "2026/10/12", "20261012", 20261012):
        values, error = validate_form_values(FIELDS, {"region": "east", "day": bad})
        assert values is None, bad
        assert "YYYY-MM-DD" in error, bad

    # select：不在选项内 / 非字符串
    for bad in ("south", 1, True):
        values, error = validate_form_values([FIELDS[0]], {"region": bad})
        assert values is None, bad
        assert "选项" in error, bad

    # text：列表 / 字典拒
    for bad in ([1], {"a": 1}):
        values, error = validate_form_values(FIELDS, {"region": "east", "note": bad})
        assert values is None, bad
        assert "note" in error, bad


# --------------- 3.3b visibleWhen 求值与提交联动（wire-form-field-linkage）


def _linked_pair(rule: dict) -> list[dict]:
    """两字段：channel 控制列（select 必填）+ detail 受控列（visibleWhen=rule）。"""
    return [
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
    ]


def test_visible_when_evaluation_matrix() -> None:
    """hidden 求值矩阵（与 web 侧同款对拍断言，逐条写死——冻结契约）。"""

    def hidden(rule: dict, values: dict) -> set[str]:
        return evaluate_hidden_columns(_linked_pair(rule), values)

    # 1. eq 命中（ctrl=其他 value=其他 → 可见）；eq 不命中（"a" vs "b" → 隐藏）
    rule = {"field": "channel", "op": "eq", "value": "其他"}
    assert "detail" not in hidden(rule, {"channel": "其他"})
    assert "detail" in hidden({"field": "channel", "op": "eq", "value": "b"}, {"channel": "a"})
    # 2. eq 永远字符串比较（不走数值）："100" vs "100.0" → 隐藏
    rule = {"field": "channel", "op": "eq", "value": "100.0"}
    assert "detail" in hidden(rule, {"channel": "100"})
    # 3. gt 数值口径：1200 > 1000 → 可见；相等 gt 假 / gte 真
    assert "detail" not in hidden(
        {"field": "channel", "op": "gt", "value": "1000"}, {"channel": "1200"}
    )
    assert "detail" in hidden(
        {"field": "channel", "op": "gt", "value": "1200"}, {"channel": "1200"}
    )
    assert "detail" not in hidden(
        {"field": "channel", "op": "gte", "value": "1200"}, {"channel": "1200"}
    )
    # 双端十进制（负数/科学计数法）才走数值：-200 < -1.5e2 → 可见
    assert "detail" not in hidden(
        {"field": "channel", "op": "lt", "value": "-1.5e2"}, {"channel": "-200"}
    )
    # 4. gt 字典序：双非数值 "abc" > "abd" 假 → 隐藏
    assert "detail" in hidden(
        {"field": "channel", "op": "gt", "value": "abd"}, {"channel": "abc"}
    )
    # 5. gt 混合：一端数值一端非 → 字典序 "12" > "abc" 假 → 隐藏
    assert "detail" in hidden(
        {"field": "channel", "op": "gt", "value": "abc"}, {"channel": "12"}
    )
    # 6. empty：缺键/空白串 → 可见；"0"/数字 0 → 隐藏（0 不是空）
    rule = {"field": "channel", "op": "empty"}
    for values in ({}, {"channel": "  "}):
        assert "detail" not in hidden(rule, values), values
    for values in ({"channel": "0"}, {"channel": 0}):
        assert "detail" in hidden(rule, values), values
    # 7. notEmpty：有值 → 可见；缺键 → 隐藏
    assert "detail" not in hidden({"field": "channel", "op": "notEmpty"}, {"channel": "x"})
    assert "detail" in hidden({"field": "channel", "op": "notEmpty"}, {})
    # 8. 空值带值算子全假（含 neq；null 同缺键）
    for op, value in (
        ("neq", "z"),
        ("eq", "z"),
        ("gt", "0"),
        ("contains", "z"),
    ):
        rule = {"field": "channel", "op": op, "value": value}
        assert "detail" in hidden(rule, {}), op
        assert "detail" in hidden(rule, {"channel": None}), op
    # 9. contains："华东" in "华东大区" → 可见；不包含 → 隐藏
    assert "detail" not in hidden(
        {"field": "channel", "op": "contains", "value": "华东"}, {"channel": "华东大区"}
    )
    assert "detail" in hidden(
        {"field": "channel", "op": "contains", "value": "华北"}, {"channel": "华东大区"}
    )
    # bool ctrl 按 JSON 口径 true/false stringify（False 不是空值）
    assert "detail" not in hidden(
        {"field": "channel", "op": "eq", "value": "true"}, {"channel": True}
    )
    assert "detail" not in hidden(
        {"field": "channel", "op": "eq", "value": "false"}, {"channel": False}
    )
    # 10. 无递归：被控条件引用的字段自身隐藏，但其值仍在 values → 照常参与比较
    chained = [
        {"column": "master", "label": "主控", "input": "text"},
        {
            "column": "ctrl",
            "label": "控制",
            "input": "text",
            "visibleWhen": {"field": "master", "op": "eq", "value": "on"},
        },
        {
            "column": "target",
            "label": "目标",
            "input": "text",
            "visibleWhen": {"field": "ctrl", "op": "eq", "value": "go"},
        },
    ]
    assert evaluate_hidden_columns(chained, {"master": "off", "ctrl": "go"}) == {"ctrl"}
    # 存量零回归：无 visibleWhen 的字段集恒得空 hidden 集
    assert evaluate_hidden_columns(FIELDS, {"region": "east", "amount": 1}) == set()


def test_validate_form_values_linkage() -> None:
    """提交三态：可见必填照旧拦；隐藏必填跳过（不写键）；隐藏携带值拒。"""
    fields = _linked_pair({"field": "channel", "op": "eq", "value": "其他"})
    # 条件真（channel=其他）：明细可见——携带值正常收，必填缺失照旧拦
    normalized, error = validate_form_values(fields, {"channel": "其他", "detail": "x"})
    assert error is None
    assert normalized == {"channel": "其他", "detail": "x"}
    values, error = validate_form_values(fields, {"channel": "其他"})
    assert values is None
    assert "必填字段" in error and "明细" in error and "detail" in error
    # 条件假（channel=电话）：明细隐藏——必填缺失不再检查（过，不写键）
    normalized, error = validate_form_values(fields, {"channel": "电话"})
    assert error is None
    assert normalized == {"channel": "电话"}
    # 隐藏字段携带非空值 → 拒（不做静默丢弃；已定义列不触发未定义列拒）
    values, error = validate_form_values(fields, {"channel": "电话", "detail": "x"})
    assert values is None
    assert error == "字段 明细（detail）当前不可见，不能携带值"
    # 隐藏字段携带非字符串标量（0）也算携带值 → 拒
    values, error = validate_form_values(fields, {"channel": "电话", "detail": 0})
    assert values is None
    assert error == "字段 明细（detail）当前不可见，不能携带值"
    # 隐藏字段携带空串/纯空白/null → 过（不写键）
    for empty_value in ("", "   ", None):
        payload = {"channel": "电话", "detail": empty_value}
        normalized, error = validate_form_values(fields, payload)
        assert error is None, empty_value
        assert normalized == {"channel": "电话"}, empty_value


# ---------- 3.3c v1+ 联动求值与提交（wire-form-linkage-v2）


def _grouped_fields(mode: str) -> list[dict]:
    """三字段：kind(select) + amount(number) + detail（visibleWhenGroup
    mode=mode，条件 eq kind=A + gt amount=100）。"""
    return [
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
            "visibleWhenGroup": {
                "mode": mode,
                "conditions": [
                    {"field": "kind", "op": "eq", "value": "A"},
                    {"field": "amount", "op": "gt", "value": "100"},
                ],
            },
        },
    ]


def test_visible_when_group_evaluation_matrix() -> None:
    """组条件求值矩阵（与 web 侧同款对拍断言——冻结契约）：all 全真可见/
    任一假隐藏；any 一真可见/全假隐藏；组内控制值空走带值算子全假口径；
    组内不递归；存量零回归。"""
    all_fields = _grouped_fields("all")
    # all：两条件真 → 可见
    assert evaluate_hidden_columns(all_fields, {"kind": "A", "amount": 200}) == set()
    # all：条件一假（kind=B）/ 条件二假（amount=50 不 > 100）→ 隐藏
    assert evaluate_hidden_columns(all_fields, {"kind": "B", "amount": 200}) == {
        "detail"
    }
    assert evaluate_hidden_columns(all_fields, {"kind": "A", "amount": 50}) == {
        "detail"
    }
    # all：任一控制值空（缺键/null/空白）→ 该条件带值算子一律假 → 隐藏
    for values in ({}, {"kind": None}, {"kind": "A"}, {"kind": "  ", "amount": 200}):
        assert evaluate_hidden_columns(all_fields, values) == {"detail"}, values

    any_fields = _grouped_fields("any")
    # any：一真即可见（另一假 / 另一控制值空）
    assert evaluate_hidden_columns(any_fields, {"kind": "B", "amount": 200}) == set()
    assert evaluate_hidden_columns(any_fields, {"kind": "A"}) == set()
    # any：全假 → 隐藏；全控制值空 → 隐藏
    assert evaluate_hidden_columns(any_fields, {"kind": "B", "amount": 50}) == {
        "detail"
    }
    assert evaluate_hidden_columns(any_fields, {}) == {"detail"}

    # 组内单条件口径逐字复用（冻结语义同款对拍）：两个控制字段独立设真值——
    # contains 与 gt 数值/字典序口径在组内逐条生效
    mixed = [
        {"column": "region", "label": "大区", "input": "text"},
        {"column": "code", "label": "编码", "input": "text"},
        {
            "column": "target",
            "label": "目标",
            "input": "text",
            "visibleWhenGroup": {
                "mode": "all",
                "conditions": [
                    {"field": "region", "op": "contains", "value": "华东"},
                    {"field": "code", "op": "gt", "value": "100"},
                ],
            },
        },
    ]
    # contains 真 + gt 数值口径真（1200 > 100）→ 可见
    assert evaluate_hidden_columns(mixed, {"region": "华东大区", "code": "1200"}) == set()
    # gt 假（双非数值走字典序："0abc" < "100"；contains 仍真）→ 隐藏
    assert evaluate_hidden_columns(mixed, {"region": "华东大区", "code": "0abc"}) == {
        "target"
    }
    # contains 假（gt 仍真）→ 隐藏
    assert evaluate_hidden_columns(mixed, {"region": "华北", "code": "1200"}) == {
        "target"
    }
    # 组内空值算子：notEmpty 真 + contains 假（空白串）→ 隐藏
    blank_op = [
        {"column": "ctrl", "label": "控制", "input": "text"},
        {
            "column": "target",
            "label": "目标",
            "input": "text",
            "visibleWhenGroup": {
                "mode": "any",
                "conditions": [
                    {"field": "ctrl", "op": "empty"},
                    {"field": "ctrl", "op": "notEmpty"},
                ],
            },
        },
    ]
    # 任意 ctrl 状态下 empty/notEmpty 必有一真 → any 恒可见
    for values in ({}, {"ctrl": "x"}, {"ctrl": "  "}):
        assert evaluate_hidden_columns(blank_op, values) == set(), values

    # 组内不递归：被引用的控制字段自身隐藏，但其值在 values → 照常参与比较
    chained = [
        {"column": "master", "label": "主控", "input": "text"},
        {
            "column": "ctrl",
            "label": "控制",
            "input": "text",
            "visibleWhen": {"field": "master", "op": "eq", "value": "on"},
        },
        {
            "column": "target",
            "label": "目标",
            "input": "text",
            "visibleWhenGroup": {
                "mode": "all",
                "conditions": [
                    {"field": "ctrl", "op": "eq", "value": "go"},
                    {"field": "master", "op": "notEmpty"},
                ],
            },
        },
    ]
    assert evaluate_hidden_columns(chained, {"master": "off", "ctrl": "go"}) == {
        "ctrl"
    }
    assert evaluate_hidden_columns(chained, {"master": "on", "ctrl": "go"}) == set()
    # 存量零回归：无组键字段集恒得空 hidden 集
    assert evaluate_hidden_columns(FIELDS, {"region": "east", "amount": 1}) == set()


def _cascade_fields(*, fallback: list | None = None) -> list[dict]:
    """省→市级联字段集：province 静态三选 + city optionsFrom（cases 覆盖
    浙江/江苏；fallback 参数化——None 即无 fallback）。"""
    options_from: dict = {
        "field": "province",
        "cases": {"浙江": ["杭州", "宁波"], "江苏": ["南京"]},
    }
    if fallback is not None:
        options_from["fallback"] = fallback
    return [
        {
            "column": "province",
            "label": "省",
            "input": "select",
            "required": True,
            "options": ["浙江", "江苏", "新疆"],
        },
        {
            "column": "city",
            "label": "市",
            "input": "select",
            "required": True,
            "optionsFrom": options_from,
        },
    ]


def test_options_from_resolution_matrix() -> None:
    """optionsFrom 解析矩阵：命中/trim 匹配/未命中 fallback/无 fallback
    空选项→隐藏；空控制值三态；数字控制值字符串形态；静态 select 零漂移。"""
    fields = _cascade_fields(fallback=["其他"])
    city = fields[1]
    # 命中 → 该键选项列表
    assert resolve_select_options(city, {"province": "浙江"}) == ["杭州", "宁波"]
    # trim 口径：控制值带空白命中；cases 键带空白同样命中（双侧同口径 trim）
    assert resolve_select_options(city, {"province": " 浙江 "}) == ["杭州", "宁波"]
    spaced_key = {
        "field": "province",
        "cases": {"  浙江 ": ["杭州"]},
    }
    assert resolve_select_options(
        {"column": "city", "input": "select", "optionsFrom": spaced_key},
        {"province": "浙江"},
    ) == ["杭州"]
    # 未命中 → fallback
    assert resolve_select_options(city, {"province": "新疆"}) == ["其他"]
    # 数字控制值按 _scalar_text 字符串形态匹配（5 → "5"）
    numeric = {
        "field": "level",
        "cases": {"5": ["高"]},
    }
    field_numeric = {
        "column": "grade",
        "input": "select",
        "optionsFrom": numeric,
    }
    assert resolve_select_options(field_numeric, {"level": 5}) == ["高"]
    assert resolve_select_options(field_numeric, {"level": "5"}) == ["高"]
    assert resolve_select_options(field_numeric, {"level": 4}) == []

    # 空控制值（缺键/null/空白串）→ fallback（有则可见）
    for values in ({}, {"province": None}, {"province": "  "}):
        assert resolve_select_options(city, values) == ["其他"], values
        assert evaluate_hidden_columns(fields, values) == set(), values

    # 无 fallback：未命中 / 空控制值 → 空选项 → 并入隐藏集（不可填即不可见）
    bare = _cascade_fields()
    assert resolve_select_options(bare[1], {"province": "新疆"}) == []
    assert resolve_select_options(bare[1], {}) == []
    assert evaluate_hidden_columns(bare, {"province": "新疆"}) == {"city"}
    for values in ({}, {"province": None}, {"province": "  "}):
        assert evaluate_hidden_columns(bare, values) == {"city"}, values
    # 命中 → 不隐藏
    assert evaluate_hidden_columns(bare, {"province": "浙江"}) == set()

    # 静态 select 不参与空选项隐藏（存量零漂移锚点）：无 optionsFrom 恒可见
    assert evaluate_hidden_columns(FIELDS, {}) == set()
    # resolve_select_options 对静态字段返回原 options（单一出处零漂移）
    assert resolve_select_options(FIELDS[0], {}) == ["east", "west", "north"]


def test_validate_form_values_linkage_v2() -> None:
    """v1+ 提交三态：组条件隐藏（必填不检查/携带值拒/空值跳过）；动态
    成员校验（值 ∈ 解析后选项，文案带解析列表）；空选项列按隐藏同规。"""
    fields = _grouped_fields("all")
    # 组条件全真：明细可见——必填缺失照旧拦；携带值正常收
    values, error = validate_form_values(fields, {"kind": "A", "amount": 200})
    assert values is None
    assert "必填字段" in error and "明细" in error and "detail" in error
    normalized, error = validate_form_values(
        fields, {"kind": "A", "amount": 200, "detail": "x"}
    )
    assert error is None
    assert normalized == {"kind": "A", "amount": 200, "detail": "x"}
    # 组条件假：明细隐藏——必填不检查（过，不写键）；携带值拒（同现行文案）
    normalized, error = validate_form_values(fields, {"kind": "B", "amount": 200})
    assert error is None
    assert normalized == {"kind": "B", "amount": 200}
    values, error = validate_form_values(
        fields, {"kind": "B", "amount": 200, "detail": "x"}
    )
    assert values is None
    assert error == "字段 明细（detail）当前不可见，不能携带值"

    # 动态成员校验：值 ∈ 解析后选项过；伪造不在解析选项的值拒（文案带
    # 解析后选项列表——防篡改）
    cascade = _cascade_fields()
    normalized, error = validate_form_values(
        cascade, {"province": "浙江", "city": "杭州"}
    )
    assert error is None
    assert normalized == {"province": "浙江", "city": "杭州"}
    values, error = validate_form_values(
        cascade, {"province": "浙江", "city": "南京"}
    )
    assert values is None
    assert error == "字段 市（city） 必须是选项之一 ['杭州', '宁波']（收到 '南京'）"
    # 控制值切换后解析集随之变化：江苏 → 南京 合法
    normalized, error = validate_form_values(
        cascade, {"province": "江苏", "city": "南京"}
    )
    assert error is None
    assert normalized == {"province": "江苏", "city": "南京"}

    # 空选项列按隐藏同规：province 未命中且无 fallback → city 隐藏——
    # required 不检查（缺省过，不写键）；携带非空值拒；空值/null 跳过
    normalized, error = validate_form_values(cascade, {"province": "新疆"})
    assert error is None
    assert normalized == {"province": "新疆"}
    values, error = validate_form_values(
        cascade, {"province": "新疆", "city": "杭州"}
    )
    assert values is None
    assert error == "字段 市（city）当前不可见，不能携带值"
    for empty_value in ("", "   ", None):
        normalized, error = validate_form_values(
            cascade, {"province": "新疆", "city": empty_value}
        )
        assert error is None, empty_value
        assert normalized == {"province": "新疆"}, empty_value


def test_submit_options_from_tamper_422(client, db_session) -> None:
    """公开提交级联（省→市）：正例入库且值原样；伪造不属于解析选项的市
    422 文案带解析后选项（防篡改同规）；控制值未命中（无 fallback）空选项
    列按隐藏——携带值 422/缺省 201 不写键；零多余写入。"""
    dataset_id = seed_dataset(
        db_session,
        key="cascade_src",
        columns=[
            {"name": "province", "type": "TEXT"},
            {"name": "city", "type": "TEXT"},
        ],
    )
    fields = _cascade_fields()
    form = make_form(client, dataset_id=dataset_id, fields=fields)
    link = _share(client, form["id"])

    # 正例：浙江 → 杭州入库
    resp = _submit(client, link["url"], {"province": "浙江", "city": "杭州"})
    assert resp.status_code == 201, resp.text
    rows = _corrections(db_session, dataset_id)
    assert rows[-1].value_json == {"province": "浙江", "city": "杭州"}

    # 防篡改：city 不属于浙江的解析选项（南京 ∈ 江苏的列表）→ 422 带
    # 解析后选项；零写入
    tampered = _submit(client, link["url"], {"province": "浙江", "city": "南京"})
    assert tampered.status_code == 422, tampered.text
    detail = tampered.json()["detail"]
    assert "市（city）" in detail
    assert "['杭州', '宁波']" in detail and "'南京'" in detail
    assert len(_corrections(db_session, dataset_id)) == 1

    # 控制值未命中（新疆 无 fallback）→ 空选项列隐藏：缺省提交 201 不写
    # city 键（required 不检查）；携带值 422 不可见；空串/null 同缺省
    blank_ok = _submit(client, link["url"], {"province": "新疆"})
    assert blank_ok.status_code == 201, blank_ok.text
    rows = _corrections(db_session, dataset_id)
    assert rows[-1].value_json == {"province": "新疆"}
    carried = _submit(client, link["url"], {"province": "新疆", "city": "杭州"})
    assert carried.status_code == 422, carried.text
    assert carried.json()["detail"] == "字段 市（city）当前不可见，不能携带值"
    skipped = _submit(client, link["url"], {"province": "新疆", "city": ""})
    assert skipped.status_code == 201, skipped.text
    assert _corrections(db_session, dataset_id)[-1].value_json == {"province": "新疆"}
    # 江苏命中路径照常（南京 ∈ 解析选项）
    ok = _submit(client, link["url"], {"province": "江苏", "city": "南京"})
    assert ok.status_code == 201, ok.text
    assert _corrections(db_session, dataset_id)[-1].value_json == {
        "province": "江苏",
        "city": "南京",
    }


# --------------------------------------------------- 3.1 生成：kind 门与分派


def test_share_create_kind_path_dispatch(client, db_session) -> None:
    """form → /api/v1/share/form/{id}；dashboard 仍 dashboard 路径；
    draft 403；归档 403；跨客户 404；不存在 404；缺鉴权 401。"""
    dataset_id = seed_dataset(db_session, key="dispatch_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])
    assert link["version"] == form["version"]
    assert link["url"].startswith(f"/api/v1/share/form/{form['id']}?t=")
    assert f"&v={form['version']}" in link["url"]

    # dashboard 路径零漂移
    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    client.post(f"/api/v1/artifacts/{board['id']}/publish", headers=CUST)
    board_link = _share(client, board["id"])
    assert board_link["url"].startswith(f"/api/v1/share/dashboard/{board['id']}?t=")

    # draft form：403 提示先发布
    draft_form = make_form(client, dataset_id=dataset_id, fields=FIELDS, publish=False)
    rejected = client.post(
        f"/api/v1/artifacts/{draft_form['id']}/share", headers=CUST
    )
    assert rejected.status_code == 403
    assert "published" in rejected.json()["detail"]

    # published 后归档：断链 403
    archived_form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    client.post(f"/api/v1/artifacts/{archived_form['id']}/archive", headers=CUST)
    archived = client.post(
        f"/api/v1/artifacts/{archived_form['id']}/share", headers=CUST
    )
    assert archived.status_code == 403
    assert "published" in archived.json()["detail"]

    # 跨客户 404（不泄露他企存在性）；不存在 404；缺 Bearer 401
    assert (
        client.post(f"/api/v1/artifacts/{form['id']}/share", headers=OTHER).status_code
        == 404
    )
    assert (
        client.post("/api/v1/artifacts/99999/share", headers=CUST).status_code == 404
    )
    assert client.post(f"/api/v1/artifacts/{form['id']}/share").status_code == 401


def test_share_create_non_shareable_kind_403(client, db_session) -> None:
    """白名单外 kind（AI 产物类）403：经会话草稿转正出生的 alert_rule
    （文案同时点名 dashboard 与 form 两类可分享 kind）。"""
    conversation = create_conversation(
        db_session, customer_id="cust-001", payload=ConversationCreate(title="告警会话")
    )
    draft = AIDraftArtifact(
        conversation_id=conversation.id,
        customer_id="cust-001",
        kind="alert_rule",
        spec={"title": "告警", "spec": {"indicator": "X", "threshold": 1}},
        status="draft",
    )
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)
    resp = client.post(
        "/api/v1/artifacts/from-draft",
        json={"conversation_id": conversation.id, "draft_id": draft.id},
        headers=CUST,
    )
    assert resp.status_code == 201, resp.text
    alert_id = resp.json()["id"]
    client.post(f"/api/v1/artifacts/{alert_id}/publish", headers=CUST)
    rejected = client.post(f"/api/v1/artifacts/{alert_id}/share", headers=CUST)
    assert rejected.status_code == 403
    detail = rejected.json()["detail"]
    assert "dashboard" in detail and "form" in detail


# ---------------------------------------------------------- 3.3b 公开 GET


def test_form_public_get_pins_version(client, db_session) -> None:
    """公开 GET：200 信封原样（{title, spec, version, expires_at}）；改版后
    旧链接仍钉旧版本快照；重新生成返回新版本。"""
    dataset_id = seed_dataset(db_session, key="get_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])

    opened = client.get(link["url"])
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert set(body) == {"title", "spec", "version", "expires_at"}
    assert body["title"] == "访客登记"
    assert body["spec"] == form["spec"]  # 钉死版本信封原样（零行数据）
    assert body["version"] == form["version"]
    assert body["expires_at"] == link["expires_at"]

    # 改版出新版本（去掉 note、加 submitLabel）——旧链接仍钉旧快照
    v_new_inner = {
        "title": "访客登记",
        "datasetId": dataset_id,
        "fields": FIELDS[:3],
        "submitLabel": "登记",
    }
    put = client.put(
        f"/api/v1/artifacts/{form['id']}",
        json={"spec": {"title": "访客登记", "spec": v_new_inner}},
        headers=CUST,
    )
    assert put.status_code == 200
    assert put.json()["version"] == form["version"] + 1
    assert client.get(link["url"]).json()["spec"] == form["spec"]

    # 重新生成：钉新版本，打开即新快照
    link_new = _share(client, form["id"])
    assert link_new["version"] == form["version"] + 1
    opened_new = client.get(link_new["url"])
    assert opened_new.status_code == 200
    assert opened_new.json()["spec"]["spec"]["fields"] == FIELDS[:3]


def test_form_public_get_uniform_404_matrix(client, db_session) -> None:
    """公开 GET 统一 404 矩阵：缺 t/缺 v/v 不一致/篡改/重放/畸形/不存在/
    dashboard 令牌走 form 路径/过期/归档——detail 恒 "not found"。"""
    dataset_id = seed_dataset(db_session, key="gate_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])
    base, _, _ = link["url"].partition("?")
    token = link["url"].split("t=")[1].split("&")[0]
    version = form["version"]

    other = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    other_token = _share(client, other["id"])["url"].split("t=")[1].split("&")[0]

    # dashboard 产物令牌：同 id 走 form 路径（kind 门）
    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    client.post(f"/api/v1/artifacts/{board['id']}/publish", headers=CUST)
    board_token = _share(client, board["id"])["url"].split("t=")[1].split("&")[0]

    cases = [
        base,  # 缺 t 缺 v
        f"{base}?t={token}",  # 缺 v
        f"{base}?t={token}&v=99",  # v 与令牌内版本不一致
        f"{base}?t={token[:-1]}{('0' if token[-1] != '0' else '1')}&v={version}",  # 篡改 mac
        f"{base}?t={other_token}&v={version}",  # 换产物重放
        f"{base}?t=not-a-token&v=1",  # 结构畸形
        f"/api/v1/share/form/99999?t={token}&v={version}",  # 产物不存在
        f"/api/v1/share/form/{board['id']}?t={board_token}&v=1",  # 非 form 产物
    ]
    for url in cases:
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert resp.json()["detail"] == "not found", url

    # 过期（TTL 负值生成即过期）
    expired_link = _share_with_ttl(client, form["id"], ttl_seconds=-1)
    resp = client.get(expired_link["url"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"

    # 归档即断链
    archived = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    archived_link = _share(client, archived["id"])
    assert client.get(archived_link["url"]).status_code == 200
    client.post(f"/api/v1/artifacts/{archived['id']}/archive", headers=CUST)
    resp = client.get(archived_link["url"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


def test_secret_missing_503_generate_get_submit(client, db_session, monkeypatch) -> None:
    """FD_WIRE_SHARE_SECRET 未配置：生成/公开 GET/公开提交三端点 503 带原因
    （惰性 env）；配置回来即恢复。"""
    dataset_id = seed_dataset(db_session, key="secret_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])
    monkeypatch.delenv("FD_WIRE_SHARE_SECRET", raising=False)

    create = client.post(f"/api/v1/artifacts/{form['id']}/share", headers=CUST)
    assert create.status_code == 503
    assert "FD_WIRE_SHARE_SECRET" in create.json()["detail"]

    opened = client.get(link["url"])
    assert opened.status_code == 503
    assert "FD_WIRE_SHARE_SECRET" in opened.json()["detail"]

    submitted = _submit(client, link["url"], {"region": "east"})
    assert submitted.status_code == 503
    assert "FD_WIRE_SHARE_SECRET" in submitted.json()["detail"]

    monkeypatch.setenv("FD_WIRE_SHARE_SECRET", SHARE_SECRET)
    assert client.get(link["url"]).status_code == 200
    assert _submit(client, link["url"], {"region": "east"}).status_code == 201


# ------------------------------------------------------ 3.3b/3.4 公开提交


def test_submit_success_api_tier_audited(client, db_session, engine_fake) -> None:
    """公开提交成功（api 档）：201 {ok, message=successMessage}；落 insert
    修正 actor=share-form:{id}、值按字段归一（number 字符串→数值、未传字段
    不写键）；全链零引擎；未配 successMessage 时回退缺省文案。"""
    dataset_id = seed_dataset(db_session, key="submit_api")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])

    resp = _submit(
        client, link["url"], {"region": "east", "amount": "12.5", "note": "访客甲"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"ok": True, "message": "登记成功，感谢填写"}

    rows = _corrections(db_session, dataset_id)
    assert len(rows) == 1
    assert rows[0].op == "insert"
    assert rows[0].actor == f"share-form:{form['id']}"
    assert rows[0].value_json == {"region": "east", "amount": 12.5, "note": "访客甲"}
    assert engine_fake.calls == []  # api 档全链零引擎

    # 未配 successMessage → 缺省文案
    plain = make_form(
        client, dataset_id=dataset_id, fields=FIELDS, success_message=None
    )
    plain_link = _share(client, plain["id"], ttl_days=7)  # 不同 TTL=另一把令牌
    resp2 = _submit(client, plain_link["url"], {"region": "west"})
    assert resp2.status_code == 201, resp2.text
    assert resp2.json() == {"ok": True, "message": "提交成功"}


def test_submit_pins_old_field_set_after_new_version(client, db_session) -> None:
    """钉死版本校验：改版把 amount 变必填后，旧令牌（钉旧版本）仍按旧
    字段集校验通过；新令牌（钉新版本）缺 amount 即 422。"""
    dataset_id = seed_dataset(db_session, key="pin_src")
    old_fields = [FIELDS[0], FIELDS[3]]  # region(必填 select) + note(text)
    form = make_form(client, dataset_id=dataset_id, fields=old_fields)
    old_link = _share(client, form["id"])

    new_fields = [FIELDS[0], {**FIELDS[1], "required": True}]  # amount 变必填
    put = client.put(
        f"/api/v1/artifacts/{form['id']}",
        json={
            "spec": {
                "title": "访客登记",
                "spec": {
                    "title": "访客登记",
                    "datasetId": dataset_id,
                    "fields": new_fields,
                },
            }
        },
        headers=CUST,
    )
    assert put.status_code == 200, put.text

    # 旧令牌按旧字段集：{region} 即合法（旧版本无必填 amount）→ 201
    old = _submit(client, old_link["url"], {"region": "east"})
    assert old.status_code == 201, old.text

    # 新令牌按新字段集：缺必填 amount → 422；补齐 → 201
    new_link = _share(client, form["id"])
    fresh = _submit(client, new_link["url"], {"region": "east"})
    assert fresh.status_code == 422
    assert "amount" in fresh.json()["detail"]
    ok_new = _submit(client, new_link["url"], {"region": "east", "amount": 5})
    assert ok_new.status_code == 201


def test_submit_validation_422_matrix_zero_write(client, db_session) -> None:
    """公开提交字段校验矩阵：未知列/缺必填/number/date/select/text 各类
    422 带字段级文案，且零写入（修正表空）。"""
    dataset_id = seed_dataset(db_session, key="val_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])

    cases = [
        {"region": "east", "ghost": 1},  # 未知列
        {},  # 缺必填
        {"region": ""},  # 必填空串
        {"region": None},  # 必填 null
        {"region": "east", "amount": "abc"},  # number 非数值串
        {"region": "east", "amount": "inf"},  # number 非有限
        {"region": "east", "day": "2026/10/12"},  # date 格式
        {"region": "east", "day": "2026-10-12T08:00"},  # date 带时间
        {"region": "south"},  # select 越选项
        {"region": "east", "note": [1]},  # text 列表
    ]
    for values in cases:
        resp = _submit(client, link["url"], values)
        assert resp.status_code == 422, values
        assert resp.json()["detail"], values
    # 文案字段级定位抽查（未知列点名列名、number 点名字段）
    unknown_detail = _submit(
        client, link["url"], {"region": "east", "ghost": 1}
    ).json()["detail"]
    assert "ghost" in unknown_detail
    number_detail = _submit(
        client, link["url"], {"region": "east", "amount": "abc"}
    ).json()["detail"]
    assert "amount" in number_detail
    # 零写入
    assert _corrections(db_session, dataset_id) == []


def test_submit_rate_limited_429_retry_after_zero_write(
    client, db_session, monkeypatch
) -> None:
    """限流：阈值 2/小时——第 3 次 429 带 Retry-After（≥1 整数）且不落库；
    被拒不占窗位；额度按令牌独立（另一把令牌立即可用）。"""
    dataset_id = seed_dataset(db_session, key="rate_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"], ttl_days=7)
    monkeypatch.setenv("FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR", "2")

    assert _submit(client, link["url"], {"region": "east"}).status_code == 201
    assert _submit(client, link["url"], {"region": "west"}).status_code == 201
    third = _submit(client, link["url"], {"region": "north"})
    assert third.status_code == 429
    assert "频繁" in third.json()["detail"]
    retry_after = third.headers["Retry-After"]
    assert retry_after.isdigit() and int(retry_after) >= 1
    # 超限不落库（仍 2 条）+ 被拒不占窗位（再打仍是 429、Retry-After 仍在）
    assert len(_corrections(db_session, dataset_id)) == 2
    again = _submit(client, link["url"], {"region": "north"})
    assert again.status_code == 429
    assert int(again.headers["Retry-After"]) >= 1

    # 另一把令牌（不同 TTL → 不同 exp/mac → 新桶）：立即可提交
    fresh_link = _share(client, form["id"], ttl_days=30)
    assert _submit(client, fresh_link["url"], {"region": "north"}).status_code == 201
    assert len(_corrections(db_session, dataset_id)) == 3


def test_submit_token_and_status_gates_404(client, db_session) -> None:
    """提交闸 404 矩阵：缺 t/篡改/v 不一致/dashboard 产物 id 走 form 提交
    路径/归档断链——统一 detail "not found"。"""
    dataset_id = seed_dataset(db_session, key="gate2_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])
    base, _, query = link["url"].partition("?")
    token = link["url"].split("t=")[1].split("&")[0]
    version = form["version"]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

    cases = [
        f"{base}/submit",  # 缺 t 缺 v
        f"{base}/submit?t={tampered}&v={version}",  # 篡改 mac
        f"{base}/submit?t={token}&v=99",  # v 与令牌内版本不一致
        f"{base}/submit?t=1.2.abc&v={version}",  # 结构畸形
    ]
    for url in cases:
        resp = client.post(url, json={"values": {"region": "east"}})
        assert resp.status_code == 404, url
        assert resp.json()["detail"] == "not found", url

    # dashboard 产物 id 走 form 提交路径（kind 门）
    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    client.post(f"/api/v1/artifacts/{board['id']}/publish", headers=CUST)
    board_link = _share(client, board["id"])
    board_base, _, board_query = board_link["url"].partition("?")
    resp = client.post(
        f"{board_base.replace('/share/dashboard/', '/share/form/')}/submit?{board_query}",
        json={"values": {"region": "east"}},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"

    # 归档断链（提交面同样断）
    archived = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    archived_link = _share(client, archived["id"])
    assert _submit(client, archived_link["url"], {"region": "east"}).status_code == 201
    client.post(f"/api/v1/artifacts/{archived['id']}/archive", headers=CUST)
    resp = _submit(client, archived_link["url"], {"region": "east"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


def test_submit_dataset_gates_404(client, db_session) -> None:
    """数据集闸 404 统一：datasetId 未绑（构建中间态）/数据集被删/他企
    数据集——一律 "not found"，零写入。"""
    # 未绑数据集（datasetId=None 构建中间态）：GET 正常渲染，提交 404
    unbound = make_form(client, dataset_id=None, fields=FIELDS[:1])
    unbound_link = _share(client, unbound["id"])
    assert client.get(unbound_link["url"]).status_code == 200
    resp = _submit(client, unbound_link["url"], {"region": "east"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"

    # 数据集被删（先清本地行/列存储再删登记）
    dataset_id = seed_dataset(db_session, key="del_src")
    deleted = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    deleted_link = _share(client, deleted["id"])
    api_store.delete_local_rows(db_session, dataset_id=dataset_id)
    db_session.delete(db_session.get(Dataset, dataset_id))
    db_session.commit()
    resp = _submit(client, deleted_link["url"], {"region": "east"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"

    # 他企数据集（属主漂移——提交侧按产物属主核属主）
    other_id = seed_dataset(db_session, key="alien_src")
    alien = make_form(client, dataset_id=other_id, fields=FIELDS)
    alien_link = _share(client, alien["id"])
    dataset = db_session.get(Dataset, other_id)
    dataset.customer_id = "cust-002"
    db_session.commit()
    resp = _submit(client, alien_link["url"], {"region": "east"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


def test_submit_pinned_spec_not_form_shape_404(client, db_session) -> None:
    """钉死版本 spec 非 form 形状（无 fields）：提交 404 统一口径
    （白盒改版本行模拟——保存校验保证正常链路不出现）。"""
    dataset_id = seed_dataset(db_session, key="shape_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])

    row = db_session.execute(
        select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == form["id"],
            ArtifactVersion.version == form["version"],
        )
    ).scalar_one()
    row.spec = {"title": "坏信封", "spec": {"datasetId": dataset_id}}
    db_session.commit()

    resp = _submit(client, link["url"], {"region": "east"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"
    assert _corrections(db_session, dataset_id) == []


def test_submit_column_drift_400_zero_write(client, db_session) -> None:
    """列漂移：字段列不在数据集列图（保存不裁决、提交时如实）——经
    validate_form_values 后由本地修正层拒，400 带列名，零写入。"""
    dataset_id = seed_dataset(db_session, key="drift_src")
    fields = [
        {"column": "region", "label": "区域", "input": "text", "required": True},
        {"column": "ghost_col", "label": "幽灵列", "input": "text"},
    ]
    form = make_form(client, dataset_id=dataset_id, fields=fields)
    link = _share(client, form["id"])

    resp = _submit(client, link["url"], {"region": "east", "ghost_col": "x"})
    assert resp.status_code == 400
    assert "ghost_col" in resp.json()["detail"]
    assert _corrections(db_session, dataset_id) == []


def test_submit_free_row_gate_429(client, db_session, tight_row_gate) -> None:
    """免费档行数闸（阈值 1）：首提 201；第二提 429 quota 原因如实、不落库。"""
    dataset_id = seed_dataset(db_session, key="rowgate_src")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])

    first = _submit(client, link["url"], {"region": "east"})
    assert first.status_code == 201, first.text
    second = _submit(client, link["url"], {"region": "west"})
    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail.startswith("quota:")
    assert "上限" in detail
    assert len(_corrections(db_session, dataset_id)) == 1  # 超限不落库


def test_submit_cell_tier_engine_passthrough_actor(
    client, db_session, engine_fake
) -> None:
    """cell 档透传引擎：TOOL_CORRECTION_ADD 报文逐项一致（actor=share-form:
    {id}、值按字段归一）；引擎不可达 502（fail-closed 零半写）。"""
    dataset_id = seed_dataset(db_session, key="cell_src", tier="cell")
    form = make_form(client, dataset_id=dataset_id, fields=FIELDS)
    link = _share(client, form["id"])

    resp = _submit(
        client, link["url"], {"region": "east", "amount": "12.5", "day": "2026-10-12"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"ok": True, "message": "登记成功，感谢填写"}
    assert engine_fake.calls == [
        (
            TOOL_CORRECTION_ADD,
            {
                "dataset_key": "cell_src",
                "op": "insert",
                "anchor": None,
                "values": {"region": "east", "amount": 12.5, "day": "2026-10-12"},
                "actor": f"share-form:{form['id']}",
            },
        )
    ]
    assert _corrections(db_session, dataset_id) == []  # cell 档不落本地修正表

    # 引擎不可达 → 502（本地零写入）
    engine_fake.result = EngineUnavailable("引擎不可达（模拟）")
    failed = _submit(client, link["url"], {"region": "west"})
    assert failed.status_code == 502
    assert failed.json()["detail"].startswith("engine_unavailable")
    assert len(engine_fake.calls) == 2  # 第二次确实打到引擎后失败
