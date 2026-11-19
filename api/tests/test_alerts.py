"""告警评估引擎测试（wire-alert-evaluation 任务 1-4）。

覆盖：
- 规则契约矩阵：validate_alert_spec 纯函数逐条文案（query 契约/聚合枚举
  与 column 必填性/条件算子与数值（bool 拒）/区间/webhook 前缀/合法全过）；
- 保存接线：kind=alert_rule PUT 保存跑契约校验，非法 400 零半写、合法
  版本递增（form 的 422 面不受影响——既有套件回归）；
- 评估器（tick_once 直调，注入时钟/datasets 会话/引擎假件；webhook 经
  monkeypatch 出站函数，绝不真打网）：
  - 状态机往返：首评 firing 记事件+通知；首评 ok 静默；ok→firing→resolved
    事件序列恰两条；持续 firing 不重复记 firing 事件；
  - 冷却再通知：同态 firing 距 last_notified_at ≥ cooldown 再发（不记事件）；
  - notify_failed 如实（detail=异常类型+摘要）且冷却去重；
  - error 事件：查询失败/非数值列/avg 空行/存量不合形 spec——不崩溃且
    last_evaluated_at 推进；error 后按 interval 重试；error 事件冷却去重；
  - due 门（未到期跳过）与 published 门（draft 不扫）；
  - 免费档（api tier）本地执行零引擎调用；
- 试运行端点：零副作用（库无新行零外呼）、404/422/400 语义；
- 管理端点：states（未评估不出键/last_error_at/客户隔离）、events
  （倒序/limit 夹取/artifact_id 过滤/他企空集不泄露/未登录 401）；
- 迁移链 0028：建表/索引、幂等重跑、与 ORM 漂移检查、降级；
- 运行时开关：FD_WIRE_ALERTS_ENABLED=false 时 start 直接跳过。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

# 在导入 app 前把默认引擎指向内存库：TestClient 启动钩子不落盘。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.alerts import evaluator as evaluator_mod
from fd_wire_api.alerts.db import reset_alert_runtime
from fd_wire_api.alerts.models import AlertEvent, AlertState
from fd_wire_api.alerts.settings import get_settings as get_alert_settings
from fd_wire_api.alerts.spec_validate import validate_alert_spec
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import Artifact, ArtifactVersion
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.ingest import TOOL_QUERY, get_engine_client_factory
from fd_wire_api.datasets.models import Dataset, DatasetColumn, DatasetRow

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

API_DIR = Path(__file__).resolve().parent.parent

SUB_CUST = "sub-alert-001"
SUB_OTHER = "sub-alert-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

#: 可注入的固定时钟基准（naive UTC）
T0 = datetime(2026, 10, 12, 12, 0, 0)


# ------------------------------------------------------------------ fakes


class ScriptedEngine:
    """cell 档引擎假件：记录调用、脚本化返回（可在测试中途改 result）。"""

    def __init__(self, result: dict | None = None, error: str | None = None,
                 exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else {
            "columns": [{"name": "amount", "type": "REAL"}],
            "rows": [{"amount": 150.0}],
            "total": 1,
        }
        self.error = error
        self.exc = exc

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if self.exc is not None:
            raise self.exc
        if self.error is not None:
            return {"error": self.error}
        result = self.result or {}
        return {
            "columns": [dict(card) for card in result.get("columns", [])],
            "rows": [dict(row) if isinstance(row, dict) else row
                     for row in result.get("rows", [])],
            "total": len(result.get("rows", [])),
        }


class BoomFactory:
    """被调即抛的引擎工厂（免费档零引擎断言用）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, customer_id: str) -> object:
        self.calls += 1
        raise AssertionError("免费档（api tier）评估必须零引擎调用")


class WebhookRecorder:
    """出站 webhook 假件：记录调用；fail=True 模拟投递不可达。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, url: str, payload: dict) -> None:
        self.calls.append({"url": url, "payload": dict(payload)})
        if self.fail:
            raise RuntimeError("webhook unreachable (simulated)")


# ------------------------------------------------------------- 种子助手


def alert_inner(
    dataset_id: str = "ds-alert",
    *,
    func: str = "sum",
    column: str | None = "amount",
    op: str = "gt",
    value: object = 100,
    interval: int = 5,
    cooldown: int | None = None,
    webhook: str | None = None,
) -> dict:
    """一份可过契约校验的规则内层（各字段可覆写）。"""
    aggregate: dict = {"func": func}
    if column is not None:
        aggregate["column"] = column
    inner: dict = {
        "query": {"dataset_id": dataset_id},
        "aggregate": aggregate,
        "condition": {"op": op, "value": value},
        "intervalMinutes": interval,
    }
    if cooldown is not None:
        inner["cooldownMinutes"] = cooldown
    if webhook is not None:
        inner["webhookUrl"] = webhook
    return inner


def seed_alert_rule(
    ai,
    *,
    customer_id: str = "cust-001",
    inner: dict | None = None,
    status: str = "published",
    kind: str = "alert_rule",
    title: str = "金额阈值告警",
) -> int:
    """直插一条产物+版本行（绕过保存校验——legacy/调试场景同款路径）。"""
    if inner is None:
        inner = alert_inner()
    artifact = Artifact(
        customer_id=customer_id,
        kind=kind,
        title=title,
        status=status,
        version=1,
        status_history=[],
    )
    ai.add(artifact)
    ai.flush()
    ai.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            version=1,
            spec={"title": title, "spec": inner},
            operator="test",
        )
    )
    ai.commit()
    return artifact.id


def seed_cell_dataset(
    ds,
    *,
    dataset_id: str = "ds-alert",
    customer_id: str = "cust-001",
    key: str = "alert_src",
) -> str:
    """cell 档数据集登记（查询经引擎假件）。"""
    ds.add(
        Dataset(
            id=dataset_id,
            customer_id=customer_id,
            key=key,
            name=f"{key} 数据源",
            source_kind="connector",
            storage_tier="cell",
            columns_json=[{"name": "amount", "type": "REAL"}],
        )
    )
    ds.commit()
    return dataset_id


def seed_api_dataset(
    ds,
    *,
    dataset_id: str = "ds-local",
    customer_id: str = "cust-001",
    key: str = "alert_local",
    rows=(("east", 60.0), ("west", 70.0)),
) -> str:
    """免费档（api tier）数据集：列注册表 + JSON 行（本地执行零引擎）。"""
    ds.add(
        Dataset(
            id=dataset_id,
            customer_id=customer_id,
            key=key,
            name=f"{key} 数据源",
            source_kind="upload",
            storage_tier="api",
            columns_json=[
                {"name": "region", "type": "TEXT"},
                {"name": "amount", "type": "REAL"},
            ],
        )
    )
    ds.flush()
    for ordinal, column in enumerate(("region", "amount")):
        dtype = "TEXT" if column == "region" else "REAL"
        ds.add(
            DatasetColumn(
                dataset_id=dataset_id,
                name=column,
                dtype=dtype,
                ordinal=ordinal,
            )
        )
    for index, (region, amount) in enumerate(rows):
        ds.add(
            DatasetRow(
                dataset_id=dataset_id,
                row_index=index,
                data={"region": region, "amount": amount},
            )
        )
    ds.commit()
    return dataset_id


def events_of(ai, artifact_id: int | None = None) -> list[AlertEvent]:
    stmt = AlertEvent.__table__.select().order_by(AlertEvent.id)
    if artifact_id is not None:
        stmt = stmt.where(AlertEvent.artifact_id == artifact_id)
    return list(ai.execute(stmt).mappings().all())


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """ai/datasets 两库同引擎内存库 + 假引擎 + TestClient（console 身份真验签）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AiBase.metadata.create_all(bind=engine)
    DatasetsBase.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    ai, ds = Session(), Session()
    fake = ScriptedEngine()

    def factory(customer_id: str) -> ScriptedEngine:
        return fake

    reset_ai_runtime()

    def override_ai():
        yield ai

    def override_ds():
        yield ds

    app.dependency_overrides[get_ai_db] = override_ai
    app.dependency_overrides[get_datasets_db] = override_ds
    app.dependency_overrides[get_engine_client_factory] = lambda: factory
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUST, "cust-001"), (SUB_OTHER, "cust-002")],
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, ai=ai, ds=ds, fake=fake, factory=factory, app=app
        )
    app.dependency_overrides.clear()
    reset_ai_runtime()


@pytest.fixture()
def webhook(monkeypatch):
    """出站 webhook 拦截（模块级函数 monkeypatch——绝不真打网）。"""
    recorder = WebhookRecorder()
    monkeypatch.setattr(evaluator_mod, "deliver_webhook", recorder)
    return recorder


def tick(env, moment):
    """直调 tick_once（注入 ai/datasets 会话与假引擎工厂）。"""
    return evaluator_mod.tick_once(
        now=moment, session=env.ai, datasets_session=env.ds,
        engine_factory=env.factory,
    )


# --------------------------------------------- 1. 规则契约矩阵（纯函数）


def test_validate_alert_spec_matrix() -> None:
    """validate_alert_spec 逐条规则与文案（None=合法）。"""
    # 合法：全字段
    full = {
        "query": {"dataset_id": "ds-1", "filters": [{"column": "a", "op": "eq", "value": 1}]},
        "aggregate": {"column": "amt", "func": "sum"},
        "condition": {"op": "gt", "value": 100},
        "intervalMinutes": 30,
        "webhookUrl": "https://hooks.example/x",
        "cooldownMinutes": 60,
    }
    assert validate_alert_spec(full) is None
    # 合法：最小形态（count 不带 column、无 webhook/cooldown）
    minimal = {
        "query": {"dataset_id": "ds-1"},
        "aggregate": {"func": "count"},
        "condition": {"op": "eq", "value": 0},
        "intervalMinutes": 5,
    }
    assert validate_alert_spec(minimal) is None
    # 合法：边界值（interval 5/1440、cooldown 1/1440、http 前缀、int value）
    assert validate_alert_spec(alert_inner(interval=5, cooldown=1, webhook="http://h/x")) is None
    assert validate_alert_spec(alert_inner(interval=1440, cooldown=1440)) is None
    assert validate_alert_spec(alert_inner(value=0, op="neq")) is None

    # 非 dict
    assert validate_alert_spec(["x"]) == "spec.spec 必须是 JSON 对象（alert_rule 规则契约）"
    # query：缺 dataset_id / 非法算子 / query 非对象
    bad_query = (
        "query 必须是合法的面板查询契约：dataset_id 必须是非空字符串；"
    )
    no_dataset = alert_inner()
    no_dataset["query"] = {"filters": []}
    assert validate_alert_spec(no_dataset).startswith(bad_query)
    bad_op = alert_inner()
    bad_op["query"] = {"dataset_id": "ds-1", "filters": [{"column": "a", "op": "like", "value": 1}]}
    assert validate_alert_spec(bad_op).startswith(bad_query)

    # aggregate：非对象 / func 越枚举 / sum 缺 column / column 空串 / count 可缺
    agg_bad = alert_inner()
    agg_bad["aggregate"] = ["sum"]
    assert validate_alert_spec(agg_bad) == "aggregate 必须是 JSON 对象 {column?, func}"
    assert validate_alert_spec(alert_inner(func="median")) == (
        "aggregate.func 必须是 sum/avg/min/max/count（收到 'median'）"
    )
    assert validate_alert_spec(alert_inner(func="sum", column=None)) == (
        "aggregate.column 必须是非空字符串（func=sum 必带数值列）"
    )
    assert validate_alert_spec(alert_inner(func="avg", column="  ")) == (
        "aggregate.column 必须是非空字符串（func=avg 必带数值列）"
    )
    assert validate_alert_spec(alert_inner(func="count", column=None)) is None
    count_blank = alert_inner(func="count", column="  ")
    assert validate_alert_spec(count_blank) == (
        "aggregate.column 必须是非空字符串（func=count 可整体省略）"
    )

    # condition：非对象 / op 越枚举 / value 布尔（bool 是 int 子类须拒）/ value 字符串
    cond_bad = alert_inner()
    cond_bad["condition"] = "gt"
    assert validate_alert_spec(cond_bad) == "condition 必须是 JSON 对象 {op, value}"
    assert validate_alert_spec(alert_inner(op="between")) == (
        "condition.op 必须是 gt/gte/lt/lte/eq/neq（收到 'between'）"
    )
    assert validate_alert_spec(alert_inner(value=True)) == (
        "condition.value 必须是数值（收到 True；布尔值不算数值）"
    )
    assert validate_alert_spec(alert_inner(value="100")) == (
        "condition.value 必须是数值（收到 '100'；布尔值不算数值）"
    )

    # intervalMinutes：越界 / 浮点 / 字符串 / 布尔
    for bad in (4, 1441, 30.5, "30", True, None):
        message = validate_alert_spec(alert_inner(interval=bad))
        assert message is not None and message.startswith("intervalMinutes"), bad

    # cooldownMinutes：越界；可省
    assert validate_alert_spec(alert_inner(cooldown=0)).startswith("cooldownMinutes")
    assert validate_alert_spec(alert_inner(cooldown=1441)).startswith("cooldownMinutes")
    assert validate_alert_spec(alert_inner(cooldown=1)) is None

    # webhookUrl：非 http(s) 前缀 / 非字符串；可省
    assert validate_alert_spec(alert_inner(webhook="ftp://hooks/x")).startswith("webhookUrl")
    assert validate_alert_spec(alert_inner(webhook="hooks.example/x")).startswith("webhookUrl")
    assert validate_alert_spec(alert_inner(webhook=123)).startswith("webhookUrl")
    assert validate_alert_spec(alert_inner()) is None


# --------------------------------------------- 2. 保存接线（400 面）


def test_put_alert_rule_rejects_invalid_spec(env) -> None:
    """kind=alert_rule 保存：契约不过 400 detail=文案，零半写（版本行不增）。"""
    artifact_id = seed_alert_rule(env.ai, status="draft")
    before = len(env.ai.query(ArtifactVersion).all())

    resp = env.client.put(
        f"/api/v1/artifacts/{artifact_id}",
        json={"spec": {"title": "金额阈值告警", "spec": alert_inner(interval=3)}},
        headers=CUST,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"].startswith("intervalMinutes")
    assert len(env.ai.query(ArtifactVersion).all()) == before  # 零半写

    # 布尔阈值同样 400
    resp2 = env.client.put(
        f"/api/v1/artifacts/{artifact_id}",
        json={"spec": {"title": "金额阈值告警", "spec": alert_inner(value=True)}},
        headers=CUST,
    )
    assert resp2.status_code == 400
    assert "condition.value" in resp2.json()["detail"]


def test_put_alert_rule_accepts_valid_spec(env) -> None:
    """合法规则保存：200 版本递增；form 的 422 面不受影响（分派隔离）。"""
    artifact_id = seed_alert_rule(env.ai, status="draft")
    resp = env.client.put(
        f"/api/v1/artifacts/{artifact_id}",
        json={"spec": {"title": "金额阈值告警", "spec": alert_inner(cooldown=30)}},
        headers=CUST,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2


def test_put_form_spec_still_422(env) -> None:
    """form 结构错仍走 422（kind 分派——alert 的 400 面不外溢）。"""
    artifact_id = seed_alert_rule(env.ai, status="draft", kind="form")
    resp = env.client.put(
        f"/api/v1/artifacts/{artifact_id}",
        json={"spec": {"title": "表单", "spec": {"fields": "x"}}},
        headers=CUST,
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------- 3. 评估器状态机


def test_first_eval_firing_records_event_and_notifies(env, webhook) -> None:
    """首评 would_fire：建 firing 行 + firing 事件 + webhook 一次。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai, inner=alert_inner(webhook="https://hooks.example/a"))

    stats = tick(env, T0)
    assert stats["scanned"] == 1 and stats["evaluated"] == 1 and stats["firing"] == 1
    # 查询经引擎 TOOL_QUERY（cell 档同一执行路径：dataset_key/filters/sort/limit）
    assert env.fake.calls[0][0] == TOOL_QUERY
    assert env.fake.calls[0][1]["dataset_key"] == "alert_src"
    state = env.ai.get(AlertState, rule)
    assert state.state == "firing" and state.last_value == 150.0
    assert state.last_evaluated_at == T0 and state.last_notified_at == T0
    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["firing"]
    assert rows[0]["value"] == 150.0 and rows[0]["threshold"] == 100.0
    assert len(webhook.calls) == 1
    payload = webhook.calls[0]["payload"]
    assert payload["artifact_id"] == rule
    assert payload["state"] == "firing"
    assert payload["value"] == 150.0 and payload["threshold"] == 100.0 and payload["op"] == "gt"
    assert payload["at"].endswith("+00:00")  # iso8601 UTC


def test_first_eval_ok_is_silent(env, webhook) -> None:
    """首评 not would_fire：初始 ok 静默（零事件零通知）。"""
    seed_cell_dataset(env.ds)
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                       "rows": [{"amount": 50.0}]}
    rule = seed_alert_rule(env.ai, inner=alert_inner(webhook="https://hooks.example/a"))

    stats = tick(env, T0)
    assert stats["firing"] == 0 and stats["evaluated"] == 1
    state = env.ai.get(AlertState, rule)
    assert state.state == "ok" and state.last_value == 50.0
    assert state.last_notified_at is None
    assert events_of(env.ai, rule) == []
    assert webhook.calls == []


def test_round_trip_ok_firing_resolved(env, webhook) -> None:
    """ok→firing→resolved：事件序列恰两条（边缘触发），通知两次。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai, inner=alert_inner(webhook="https://hooks.example/a"))

    # T0：首评 ok（静默）
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}], "rows": [{"amount": 10.0}]}
    tick(env, T0)
    # T+5m：越限 → firing
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}], "rows": [{"amount": 200.0}]}
    stats = tick(env, T0 + timedelta(minutes=5))
    assert stats["firing"] == 1
    # T+10m：回落 → resolved
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}], "rows": [{"amount": 20.0}]}
    stats = tick(env, T0 + timedelta(minutes=10))
    assert stats["resolved"] == 1

    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["firing", "resolved"]  # 恰两条
    assert [r["value"] for r in rows] == [200.0, 20.0]
    state = env.ai.get(AlertState, rule)
    assert state.state == "ok" and state.last_value == 20.0
    assert [c["payload"]["state"] for c in webhook.calls] == ["firing", "resolved"]


def test_persistent_firing_no_duplicate_event_cooldown_renotify(env, webhook) -> None:
    """持续 firing：同态不记事件；距上次通知 ≥ cooldown 再通知（不记事件）。"""
    seed_cell_dataset(env.ds)
    # interval=5m、cooldown=10m
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(webhook="https://hooks.example/a", interval=5, cooldown=10),
    )

    def firing_rows(amount: float) -> None:
        env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                            "rows": [{"amount": amount}]}

    firing_rows(150.0)
    tick(env, T0)                       # 首评 firing：事件 1 + 通知 1
    firing_rows(160.0)
    stats = tick(env, T0 + timedelta(minutes=5))   # 同态、冷却内：零事件零通知
    assert stats["firing"] == 0 and stats["notified"] == 0
    firing_rows(170.0)
    stats = tick(env, T0 + timedelta(minutes=10))  # 冷却到点：再通知不记事件
    assert stats["notified"] == 1 and stats["firing"] == 0

    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["firing"]  # 仍只有一条 firing 事件
    assert len(webhook.calls) == 2
    assert webhook.calls[1]["payload"]["value"] == 170.0
    state = env.ai.get(AlertState, rule)
    assert state.state == "firing" and state.last_notified_at == T0 + timedelta(minutes=10)


def test_no_webhook_configured_no_outbound(env, webhook) -> None:
    """未配 webhookUrl：状态与事件照记，零外呼。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai)  # 无 webhook

    tick(env, T0)
    assert webhook.calls == []
    state = env.ai.get(AlertState, rule)
    assert state.state == "firing" and state.last_notified_at is None
    assert [r["kind"] for r in events_of(env.ai, rule)] == ["firing"]


def test_notify_failed_event_honest_and_deduped(env, monkeypatch) -> None:
    """投递失败：notify_failed 如实（异常类型+摘要）且冷却去重。"""
    seed_cell_dataset(env.ds)
    # interval=5m、cooldown=60m：两轮失败同冷却窗 → 只一条 notify_failed
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(webhook="https://hooks.example/a", interval=5, cooldown=60),
    )
    recorder = WebhookRecorder(fail=True)
    monkeypatch.setattr(evaluator_mod, "deliver_webhook", recorder)

    tick(env, T0)
    tick(env, T0 + timedelta(minutes=5))
    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["firing", "notify_failed"]  # firing 照记
    failed = rows[1]
    assert failed["detail"].startswith("RuntimeError: webhook unreachable")
    assert failed["value"] == 150.0 and failed["threshold"] == 100.0
    state = env.ai.get(AlertState, rule)
    assert state.state == "firing" and state.last_notified_at is None  # 未记成功通知

    # 冷却过后（T+61m 评估）：再失败 → 第二条 notify_failed
    tick(env, T0 + timedelta(minutes=61))
    kinds = [r["kind"] for r in events_of(env.ai, rule)]
    assert kinds.count("notify_failed") == 2
    assert len(recorder.calls) == 3


# --------------------------------------------- 4. error 事件面


def test_error_event_missing_dataset(env) -> None:
    """查询失败（数据集不存在）：error 事件 + last_evaluated_at 推进不崩溃。"""
    rule = seed_alert_rule(env.ai, inner=alert_inner(dataset_id="ds-missing"))
    # 同轮另一条好规则照常评估（失败隔离）
    seed_cell_dataset(env.ds)
    good = seed_alert_rule(env.ai, inner=alert_inner(dataset_id="ds-alert"))

    stats = tick(env, T0)
    assert stats["error"] == 1 and stats["firing"] == 1
    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["error"]
    assert rows[0]["detail"] == "数据集不存在: ds-missing"
    state = env.ai.get(AlertState, rule)
    assert state.state == "ok" and state.last_evaluated_at == T0
    assert env.ai.get(AlertState, good).state == "firing"


def test_error_event_engine_failure(env) -> None:
    """引擎失败（cell 未达/调用错）→ error 事件如实。"""
    from fd_wire_api.datasets.ingest import EngineUnavailable

    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai)
    env.fake.exc = EngineUnavailable("引擎不可达（注入失败）")

    stats = tick(env, T0)
    assert stats["error"] == 1
    assert events_of(env.ai, rule)[0]["detail"] == "引擎不可达（注入失败）"


def test_error_event_non_numeric_column(env) -> None:
    """数值列存在非数值行 → error 事件「列 X 存在非数值行」。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai)
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                       "rows": [{"amount": 150.0}, {"amount": "abc"}]}

    stats = tick(env, T0)
    assert stats["error"] == 1
    assert events_of(env.ai, rule)[0]["detail"] == "列 amount 存在非数值行"


def test_error_event_avg_empty_rows(env) -> None:
    """avg 空行集 → error 事件「无行可归约」（sum 空行集=0.0 对照）。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai, inner=alert_inner(func="avg"))
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}], "rows": []}

    stats = tick(env, T0)
    assert stats["error"] == 1
    assert events_of(env.ai, rule)[0]["detail"] == "无行可归约"

    # sum 空行集 = 0.0（不触发阈值则落 ok）
    rule_sum = seed_alert_rule(
        env.ai, inner=alert_inner(func="sum", dataset_id="ds-alert")
    )
    stats = tick(env, T0 + timedelta(minutes=5))
    state = env.ai.get(AlertState, rule_sum)
    assert state.state == "ok" and state.last_value == 0.0


def test_error_event_legacy_spec_no_crash(env) -> None:
    """存量不合形 spec：error 事件（带 spec 文案）不崩溃、last_evaluated_at 推进。"""
    rule = seed_alert_rule(env.ai, inner={"foo": "bar"})  # 绕过保存校验直插

    stats = tick(env, T0)
    assert stats["scanned"] == 1 and stats["error"] == 1
    rows = events_of(env.ai, rule)
    assert rows[0]["kind"] == "error"
    assert rows[0]["detail"].startswith("规则 spec 非法: ")
    assert env.ai.get(AlertState, rule).last_evaluated_at == T0

    # 不合形（读不出 interval）→ 每 tick 都重评（视为到期），事件冷却去重
    stats = tick(env, T0 + timedelta(seconds=30))
    assert stats["evaluated"] == 1 and stats["error"] == 1  # 评估发生
    assert len(events_of(env.ai, rule)) == 1  # error 事件冷却内不重复
    assert env.ai.get(AlertState, rule).last_evaluated_at == T0 + timedelta(seconds=30)


def test_error_event_retry_after_interval_and_cooldown_dedupe(env) -> None:
    """error 后按 interval 重试；error 事件按冷却窗去重（过窗再记）。"""
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(dataset_id="ds-missing", interval=5, cooldown=1),
    )

    tick(env, T0)                              # error #1
    stats = tick(env, T0 + timedelta(minutes=1))  # interval 5m 未到 → 跳过
    assert stats["evaluated"] == 0
    stats = tick(env, T0 + timedelta(minutes=5))  # 到点重评（cooldown 1m 已过）
    assert stats["evaluated"] == 1 and stats["error"] == 1
    rows = events_of(env.ai, rule)
    assert len(rows) == 2 and all(r["kind"] == "error" for r in rows)


# --------------------------------------------- 5. due 门与档位门


def test_due_gate_skips_within_interval(env, webhook) -> None:
    """未到期跳过（evaluated=0）；到点重评。"""
    seed_cell_dataset(env.ds)
    seed_alert_rule(env.ai, inner=alert_inner(interval=5))

    assert tick(env, T0)["evaluated"] == 1
    assert tick(env, T0 + timedelta(minutes=4))["evaluated"] == 0
    assert tick(env, T0 + timedelta(minutes=5))["evaluated"] == 1


def test_only_published_rules_scanned(env) -> None:
    """draft 不武装、archived 即解除：只扫 published。"""
    seed_cell_dataset(env.ds)
    seed_alert_rule(env.ai, status="draft")
    seed_alert_rule(env.ai, status="archived")

    stats = tick(env, T0)
    assert stats["scanned"] == 0 and stats["evaluated"] == 0


def test_free_tier_local_execution_zero_engine(env, webhook) -> None:
    """免费档（api tier）：本地执行零引擎调用，评估照常。"""
    seed_api_dataset(env.ds, rows=(("east", 60.0), ("west", 70.0)))
    rule = seed_alert_rule(env.ai, inner=alert_inner(dataset_id="ds-local", value=100))

    boom = BoomFactory()
    stats = evaluator_mod.tick_once(
        now=T0, session=env.ai, datasets_session=env.ds, engine_factory=boom
    )
    assert stats["firing"] == 1
    assert boom.calls == 0  # 零引擎
    state = env.ai.get(AlertState, rule)
    assert state.last_value == 130.0  # 60 + 70


# --------------------------------------------- 6. 试运行端点（零副作用）


def test_alert_test_endpoint_zero_side_effects(env, webhook) -> None:
    """试运行：即时评估返回 value/would_fire；零 states/events/零外呼。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai, inner=alert_inner(webhook="https://hooks.example/a"))
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                       "rows": [{"amount": 120.0}, {"amount": 80.0}]}

    resp = env.client.post(f"/api/v1/artifacts/{rule}/alert/test", headers=CUST)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"value": 200.0, "would_fire": True}

    assert env.ai.query(AlertState).all() == []   # 零状态行
    assert env.ai.query(AlertEvent).all() == []   # 零事件行
    assert webhook.calls == []                    # 零外呼

    # would_fire=False 面
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}], "rows": [{"amount": 10.0}]}
    resp2 = env.client.post(f"/api/v1/artifacts/{rule}/alert/test", headers=CUST)
    assert resp2.json() == {"value": 10.0, "would_fire": False}


def test_alert_test_endpoint_404_semantics(env) -> None:
    """不存在/他企/非 alert_rule 一律 404。"""
    seed_cell_dataset(env.ds)
    mine = seed_alert_rule(env.ai)
    other = seed_alert_rule(env.ai, customer_id="cust-002", inner=alert_inner(dataset_id="ds-x"))
    dash = seed_alert_rule(env.ai, kind="dashboard")

    assert env.client.post("/api/v1/artifacts/999999/alert/test", headers=CUST).status_code == 404
    assert env.client.post(f"/api/v1/artifacts/{other}/alert/test", headers=CUST).status_code == 404
    assert env.client.post(f"/api/v1/artifacts/{dash}/alert/test", headers=CUST).status_code == 404
    assert env.client.post(f"/api/v1/artifacts/{mine}/alert/test", headers=OTHER).status_code == 404


def test_alert_test_endpoint_422_invalid_spec(env) -> None:
    """存量不合形 spec：422 detail=校验文案（试面如实递回）。"""
    rule = seed_alert_rule(env.ai, inner=alert_inner(interval=2))  # 绕过保存校验

    resp = env.client.post(f"/api/v1/artifacts/{rule}/alert/test", headers=CUST)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("intervalMinutes")


def test_alert_test_endpoint_400_eval_failure(env) -> None:
    """查询/归约失败：400 detail=原因（调试面如实）。"""
    missing = seed_alert_rule(env.ai, inner=alert_inner(dataset_id="ds-missing"))
    resp = env.client.post(f"/api/v1/artifacts/{missing}/alert/test", headers=CUST)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "数据集不存在: ds-missing"

    seed_cell_dataset(env.ds)
    bad_col = seed_alert_rule(env.ai, inner=alert_inner(column="nope"))
    resp2 = env.client.post(f"/api/v1/artifacts/{bad_col}/alert/test", headers=CUST)
    assert resp2.status_code == 400
    assert "nope" in resp2.json()["detail"]


# --------------------------------------------- 7. states / events 端点


def test_states_endpoint_shape_and_isolation(env) -> None:
    """states：本客户全部 alert_rule 状态；未评估不出键；last_error_at 可空。"""
    seed_cell_dataset(env.ds)
    ok_rule = seed_alert_rule(env.ai, inner=alert_inner(dataset_id="ds-alert"))
    err_rule = seed_alert_rule(
        env.ai, inner=alert_inner(dataset_id="ds-missing", op="lte")
    )
    seed_alert_rule(
        env.ai, status="draft", inner=alert_inner(dataset_id="ds-alert", op="neq", value=-1)
    )  # draft 不武装 → 从未评估（states 不出键）
    other = seed_alert_rule(env.ai, customer_id="cust-002", inner=alert_inner())

    tick(env, T0)

    resp = env.client.get("/api/v1/alerts/states", headers=CUST)
    assert resp.status_code == 200, resp.text
    states = resp.json()["states"]
    assert set(states) == {str(ok_rule), str(err_rule)}  # never/他企不出键
    assert states[str(ok_rule)]["state"] == "firing"
    assert states[str(ok_rule)]["last_value"] == 150.0
    assert states[str(ok_rule)]["last_error_at"] is None
    assert states[str(err_rule)]["state"] == "ok"
    assert states[str(err_rule)]["last_error_at"] is not None
    assert states[str(ok_rule)]["last_evaluated_at"].startswith("2026-10-12T12:00:00")

    # 客户隔离：他企只见自己的
    resp_other = env.client.get("/api/v1/alerts/states", headers=OTHER)
    assert set(resp_other.json()["states"]) == {str(other)}


def test_states_endpoint_unauthenticated(env) -> None:
    assert env.client.get("/api/v1/alerts/states").status_code == 401


def test_events_endpoint_order_limit_filter_isolation(env) -> None:
    """events：id 倒序、limit 夹取 1..200、artifact_id 过滤、他企空集。"""
    seed_cell_dataset(env.ds)
    rule_a = seed_alert_rule(env.ai)
    rule_b = seed_alert_rule(
        env.ai, inner=alert_inner(dataset_id="ds-alert", op="lte", value=-1)
    )
    rule_other = seed_alert_rule(env.ai, customer_id="cust-002")

    # 直插一批事件（固定 id 顺序）：A×3、B×2、他企×1
    for index in range(3):
        env.ai.add(AlertEvent(artifact_id=rule_a, customer_id="cust-001",
                              kind="firing", value=float(index), threshold=100.0,
                              created_at=T0 + timedelta(minutes=index)))
    for index in range(2):
        env.ai.add(AlertEvent(artifact_id=rule_b, customer_id="cust-001",
                              kind="resolved", value=1.0, threshold=-1.0,
                              created_at=T0 + timedelta(minutes=index)))
    env.ai.add(AlertEvent(artifact_id=rule_other, customer_id="cust-002",
                          kind="firing", value=9.0, threshold=1.0, created_at=T0))
    env.ai.commit()

    resp = env.client.get("/api/v1/alerts/events", headers=CUST)
    assert resp.status_code == 200
    body = resp.json()["events"]
    assert [e["id"] for e in body] == sorted([e["id"] for e in body], reverse=True)
    assert len(body) == 5  # 他企不泄露
    first = body[0]
    assert set(first) == {"id", "artifact_id", "kind", "value", "threshold",
                          "detail", "created_at"}

    # limit 夹取：0 → 1；999 → 全量（≤200）
    assert len(env.client.get("/api/v1/alerts/events?limit=0", headers=CUST).json()["events"]) == 1
    assert len(env.client.get("/api/v1/alerts/events?limit=999", headers=CUST).json()["events"]) == 5
    assert len(env.client.get("/api/v1/alerts/events?limit=2", headers=CUST).json()["events"]) == 2

    # artifact_id 过滤
    only_b = env.client.get(f"/api/v1/alerts/events?artifact_id={rule_b}", headers=CUST).json()["events"]
    assert len(only_b) == 2 and all(e["artifact_id"] == rule_b for e in only_b)

    # 他企 artifact_id：结果为空不泄露
    empty = env.client.get(f"/api/v1/alerts/events?artifact_id={rule_other}", headers=CUST).json()["events"]
    assert empty == []

    # 未登录 401
    assert env.client.get("/api/v1/alerts/events").status_code == 401


# --------------------------------------------- 8. 迁移链 0028


@pytest.fixture()
def migrated_0028(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade 至 0028（链首 0001 起，全链真实迁移）。"""
    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    db_url = f"sqlite:///{tmp_path}/alerts_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "0028_alert_evaluation")
    return cfg, db_url


def test_migration_0028_creates_alert_tables(migrated_0028) -> None:
    """0028 建两表 + 倒序索引；幂等重跑无恙。"""
    cfg, db_url = migrated_0028
    command.upgrade(cfg, "0028_alert_evaluation")  # 幂等重跑

    engine = create_engine(db_url)
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("alert_states")} == {
        "artifact_id", "customer_id", "state", "last_value",
        "last_evaluated_at", "last_notified_at", "updated_at",
    }
    assert {c["name"] for c in inspector.get_columns("alert_events")} == {
        "id", "artifact_id", "customer_id", "kind", "value", "threshold",
        "detail", "created_at",
    }
    assert {ix["name"] for ix in inspector.get_indexes("alert_events")} >= {
        "ix_alert_events_customer_id",
        "ix_alert_events_artifact_id",
    }
    # artifact_id 是 alert_states 主键（一产物一行）
    assert inspector.get_pk_constraint("alert_states")["constrained_columns"] == ["artifact_id"]
    engine.dispose()


def test_migration_0028_schema_matches_orm(migrated_0028) -> None:
    """drift check：迁移产出列集与 ORM 声明（AiBase 两表）一致。"""
    _, db_url = migrated_0028
    engine = create_engine(db_url)
    inspector = inspect(engine)
    for table in ("alert_states", "alert_events"):
        migrated = {c["name"] for c in inspector.get_columns(table)}
        declared = set(AiBase.metadata.tables[table].columns.keys())
        assert migrated == declared, f"{table} 迁移与 ORM 列漂移: {migrated ^ declared}"
    engine.dispose()


def test_migration_0028_downgrade_drops_tables(migrated_0028) -> None:
    cfg, db_url = migrated_0028
    command.downgrade(cfg, "0027_team_spaces")
    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    assert "alert_states" not in tables and "alert_events" not in tables
    assert "artifacts" in tables  # 前置迁移不受影响
    engine.dispose()


# --------------------------------------------- 9. 运行时开关


def test_alert_settings_defaults() -> None:
    """缺省：启用、tick 30s。"""
    reset_alert_runtime()
    settings = get_alert_settings()
    assert settings.alerts_enabled is True
    assert settings.alert_tick_seconds == 30
    reset_alert_runtime()


def test_start_alert_evaluator_disabled_noop(monkeypatch) -> None:
    """FD_WIRE_ALERTS_ENABLED=false：start 直接跳过（不建后台任务）。"""
    monkeypatch.setenv("FD_WIRE_ALERTS_ENABLED", "false")
    reset_alert_runtime()

    async def main() -> None:
        evaluator_mod.start_alert_evaluator()

    asyncio.run(main())
    assert evaluator_mod._task is None
    evaluator_mod.stop_alert_evaluator()
    reset_alert_runtime()
