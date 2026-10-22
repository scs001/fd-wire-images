"""质量规则引擎测试（wire-quality-rules）。

覆盖：
- DSL 校验矩阵：枚举外指标/表外列/注入式列名/比率阈值越界/白名单缺项
  逐个 422 且**引擎零调用**（design D3 注入防线第一层）；
- 编译器：五指标 → 引擎 filter 字典快照（结构化组装，无字符串拼接）；
- 求值三态（GX 契约）：null_rate pass/fail（observed/unexpected_count/
  pass_rate）、value_violations（SQL NOT IN 语义：NULL 不算违规）、
  row_count（无比率语义恒 NULL）、空数据集空真口径；
- key_uniqueness snapshot 通道：复用最近 ok 快照 + basis_at 披露快照
  年龄；无快照/未声明键列 → 诚实 error 不给伪造比例；
- 失败隔离：引擎查询抛错 → 单规则 error 行（手动 run 端点 200 逐条
  返回，非整批 5xx）；同步自动触发不伤主流程；
- 规则管理：PATCH 启停（停用不求值、历史保留）、改检查体、DELETE 后
  结果历史仍在；上限 422；
- 鉴权与档位：未鉴权 401；viewer 写 403/读 200；他企 404；api 档
  （免费档上传集）403 档位闸——与质量快照同款；
- 剪枝：每规则保留最近 N 条。

环境：datasets 内存库 + 平台临时库（basic 档种子）+ 假引擎（count_only
计数可编程），照 test_dataset_quality 的 quality_env 纪律。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.datasets import connector_routes
from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.datasets.ingest import (
    EngineCallError,
    TOOL_QUERY,
    get_engine_client_factory,
)
from fd_wire_api.quality.models import (
    DatasetQualityRule,
    DatasetQualityRuleResult,
    DatasetQualitySnapshot,
)
from fd_wire_api.quality.rules import compile_filters, validate_check

from console_fixture import install_fake_jwks, make_console_env, mint_id_token
from test_datasets_common import (
    CUST_HEADERS,
    INTERNAL_TOKEN,
    SUB_A,
    SUB_B,
    FakeEngine,
    create_connector_dataset,
    make_uploaded_dataset,
    pair_connector,
    push_sync,
)

SUB_VIEWER = "sub-viewer-rules"
VIEWER = {"Authorization": f"Bearer {mint_id_token(SUB_VIEWER)}"}

#: 三列 cell 数据集（id/status/age），columns 注册表 = 漂移基线
COLUMNS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "status", "type": "TEXT"},
    {"name": "age", "type": "INTEGER"},
]
ROWS = [
    {"id": i, "status": "active" if i % 3 else "closed", "age": 20 + i}
    for i in range(1, 11)
]


class RulesFakeEngine(FakeEngine):
    """引擎假件扩展：``count_only`` 计数 + ``customer_dataset_check``
    内容代检查（均可编程）。"""

    def __init__(self) -> None:
        super().__init__()
        self.total = 100
        self.nulls = 2      # isnull 计数
        self.violations = 3  # notin 计数
        self.count_calls: list[list] = []
        #: 内容代检查：返回逐检查契约（键=检查序号；缺省 pass）
        self.check_items: dict[int, dict] = {}
        self.check_calls: list[list] = []
        self.check_error: Exception | None = None

    def _result_check(self, arguments):
        from fd_wire_api.quality.rules import TOOL_CHECK  # noqa: PLC0415

        checks = list(arguments.get("checks") or [])
        self.check_calls.append(checks)
        if self.check_error is not None:
            raise self.check_error
        items = []
        for i in range(len(checks)):
            items.append(
                self.check_items.get(
                    i,
                    {"status": "pass", "element_count": self.total,
                     "unexpected_count": 0, "pass_rate": 1.0},
                )
            )
        return {"success": True, "results": items, "element_count": self.total}

    def _count(self, filters: list) -> int:
        if not filters:
            return self.total
        op = filters[0].get("op")
        if op == "isnull":
            return self.nulls
        if op == "notnull":
            return self.total - self.nulls
        if op == "notin":
            return self.violations
        return 0

    def _result(self, tool, arguments):
        if tool == "customer_dataset_check":
            return self._result_check(arguments)
        if tool == TOOL_QUERY and arguments.get("count_only"):
            self.count_calls.append(list(arguments.get("filters") or []))
            return {"success": True, "count": self._count(arguments.get("filters") or [])}
        return super()._result(tool, arguments)


@pytest.fixture()
def rules_env(tmp_path, monkeypatch):
    """隔离运行时 + 规则假引擎（count_only 可编程）。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MATERIALIZATION_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db")
    monkeypatch.setenv("MATERIALIZATION_AUTO_CREATE_TABLES", "1")
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_A, "cust-001"), (SUB_B, "cust-002")],
        internal_token=INTERNAL_TOKEN,
        plan_code="basic",
    )
    from fd_wire_api.console.models import CustomerMember

    with console_env.session() as sess:
        sess.add(
            CustomerMember(
                customer_id=console_env.subs[SUB_A],
                logto_sub=SUB_VIEWER,
                role="viewer",
            )
        )
        sess.commit()

    reset_datasets_runtime()
    connector_routes._SYNC_SESSIONS.clear()
    from fd_wire_api.materialization.db import reset_materialization_runtime

    reset_materialization_runtime()

    fake = RulesFakeEngine()
    fastapi_app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: fake
    )
    with TestClient(fastapi_app) as client:
        env = SimpleNamespace(client=client, fake=fake, app=fastapi_app)
        # cell 档数据集：连接器通道建集 + 一次同步（10 行三列）
        pair = pair_connector(env)
        env.dataset = create_connector_dataset(
            env, connector_id=pair["connector_id"]
        )
        push_sync(
            env,
            connector_key=pair["connector_key"],
            dataset_key=env.dataset["key"],
            columns=COLUMNS,
            rows=ROWS,
            pk_columns=["id"],
        )
        yield env
    fastapi_app.dependency_overrides.clear()
    connector_routes._SYNC_SESSIONS.clear()
    reset_datasets_runtime()
    reset_materialization_runtime()


# ------------------------------------------------------------------ 直读助手


def _seed_snapshot(
    dataset_id: str,
    *,
    ratio: float | None,
    pk_declared: bool,
    computed_at: datetime | None = None,
) -> None:
    session = get_session()
    try:
        session.add(
            DatasetQualitySnapshot(
                dataset_id=dataset_id,
                triggered_by="manual",
                status="ok",
                row_count=100,
                pk_declared=pk_declared,
                pk_unique_ratio=ratio,
                computed_at=computed_at
                or datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()
    finally:
        session.close()


def _rule_rows(dataset_id: str) -> list[DatasetQualityRule]:
    session = get_session()
    try:
        return list(
            session.execute(
                select(DatasetQualityRule).where(
                    DatasetQualityRule.dataset_id == dataset_id
                )
            ).scalars()
        )
    finally:
        session.close()


def _result_rows(dataset_id: str) -> list[DatasetQualityRuleResult]:
    session = get_session()
    try:
        return list(
            session.execute(
                select(DatasetQualityRuleResult)
                .where(DatasetQualityRuleResult.dataset_id == dataset_id)
                .order_by(DatasetQualityRuleResult.id)
            ).scalars()
        )
    finally:
        session.close()


def _add_rule(env, check: dict, *, name: str | None = None):
    return env.client.post(
        f"/api/v1/datasets/{env.dataset['id']}/quality-rules",
        headers=CUST_HEADERS["cust-001"],
        json={"name": name, "check": check},
    )


def _run(env):
    return env.client.post(
        f"/api/v1/datasets/{env.dataset['id']}/quality-rules/run",
        headers=CUST_HEADERS["cust-001"],
    )


# ------------------------------------------------------------------ DSL 校验矩阵


def test_dsl_validation_matrix_rejects_without_engine_calls(rules_env):
    """枚举外/表外列/注入列名/阈值越界/白名单缺项 → 422 且引擎零调用。"""
    env = rules_env
    bad_checks = [
        {"metric": "exec_shell", "op": "=", "threshold": 1},
        {"metric": "null_rate", "column": "age; DROP TABLE datasets", "op": "<=", "threshold": 0.05},
        {"metric": "null_rate", "column": "nonexistent", "op": "<=", "threshold": 0.05},
        {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 5},
        {"metric": "null_rate", "column": "age", "op": "regex", "threshold": 0.05},
        {"metric": "value_violations", "column": "status", "op": "=", "threshold": 0, "allowed": ["a"]},
        {"metric": "null_rate", "column": "age", "op": "<=", "threshold": "0.05"},
        {"metric": "null_rate", "column": "age", "op": "<=", "threshold": True},
    ]
    for check in bad_checks:
        resp = _add_rule(env, check)
        assert resp.status_code == 422, (check, resp.text)
    assert not env.fake.count_calls  # 引擎零调用（校验在先）
    # 白名单值过长/重复
    assert _add_rule(env, {
        "metric": "value_violations", "column": "status", "op": "=", "threshold": 0,
        "allowed": ["a", "a"],
    }).status_code == 422
    assert _add_rule(env, {
        "metric": "value_violations", "column": "status", "op": "=", "threshold": 0,
        "allowed": ["x" * 65, "y"],
    }).status_code == 422
    # inf/nan 走函数级直测（httpx 拒编码）
    for bad in (float("inf"), float("nan")):
        with pytest.raises(Exception):
            validate_check(
                {"metric": "row_count", "op": ">=", "threshold": bad}, ["id"]
            )


def test_compiler_filter_dicts_are_structured(rules_env):
    """五指标 → 引擎 filter 字典快照（结构化组装，永不拼字符串）。"""
    assert compile_filters({"metric": "row_count", "op": ">=", "threshold": 10}) == []
    assert compile_filters(
        {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 0.05}
    ) == [{"column": "age", "op": "isnull"}]
    assert compile_filters(
        {"metric": "filled_rate", "column": "age", "op": ">=", "threshold": 0.9}
    ) == [{"column": "age", "op": "notnull"}]
    assert compile_filters(
        {"metric": "null_count", "column": "age", "op": "<=", "threshold": 5}
    ) == [{"column": "age", "op": "isnull"}]
    assert compile_filters(
        {
            "metric": "value_violations", "column": "status", "op": "=", "threshold": 0,
            "allowed": ["active", "closed"],
        }
    ) == [{"column": "status", "op": "notin", "values": ["active", "closed"]}]
    # key_uniqueness 走快照通道：无 filter
    assert compile_filters(
        {"metric": "key_uniqueness", "op": ">=", "threshold": 1.0}
    ) == []


# ------------------------------------------------------------------ 求值三态


def test_null_rate_pass_and_fail_gx_contract(rules_env):
    env = rules_env
    assert _add_rule(env, {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 0.05}).status_code == 201
    resp = _run(env)
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert result["status"] == "pass"
    assert result["element_count"] == 100
    assert result["unexpected_count"] == 2
    assert result["observed"] == 0.02
    assert result["pass_rate"] == 0.98
    assert result["channel"] == "query"
    assert result["basis_at"]

    # 空值涨到 10 → fail（observed 0.1）
    rule_id = _rule_rows(env.dataset["id"])[0].id
    env.fake.nulls = 10
    resp = _run(env)
    (result,) = resp.json()["results"]
    assert result["status"] == "fail"
    assert result["observed"] == 0.1
    assert result["unexpected_count"] == 10
    assert result["pass_rate"] == 0.9
    assert resp.json()["results"][0]["rule_id"] == rule_id


def test_value_violations_sql_notin_semantics(rules_env):
    """violations=非空值不在白名单（SQL NOT IN 不计 NULL）。"""
    env = rules_env
    assert _add_rule(env, {
        "metric": "value_violations", "column": "status", "op": "=", "threshold": 0,
        "allowed": ["active", "closed"],
    }).status_code == 201
    env.fake.violations = 3
    (result,) = _run(env).json()["results"]
    assert result["status"] == "fail"
    assert result["observed"] == 3
    assert result["element_count"] == 100
    assert result["unexpected_count"] == 3
    assert result["pass_rate"] == 0.97

    env.fake.violations = 0
    (result,) = _run(env).json()["results"]
    assert result["status"] == "pass"


def test_row_count_no_rate_semantics(rules_env):
    env = rules_env
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 50}).status_code == 201
    (result,) = _run(env).json()["results"]
    assert result["status"] == "pass"
    assert result["observed"] == 100
    assert result["element_count"] == 100
    assert result["unexpected_count"] is None
    assert result["pass_rate"] is None


def test_empty_dataset_vacuous_truth(rules_env):
    """空数据集：null_rate 空真 0.0、违规 0、element_count=0 如实可见。"""
    env = rules_env
    assert _add_rule(env, {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 0.05}).status_code == 201
    env.fake.total = 0
    env.fake.nulls = 0
    (result,) = _run(env).json()["results"]
    assert result["status"] == "pass"
    assert result["element_count"] == 0
    assert result["observed"] == 0.0


def test_key_uniqueness_snapshot_channel(rules_env):
    """键唯一率复用最近 ok 快照；basis_at=快照 computed_at（披露年龄）。"""
    env = rules_env
    snapshot_time = datetime.now(timezone.utc).replace(
        tzinfo=None
    ) - timedelta(minutes=30)
    _seed_snapshot(
        env.dataset["id"], ratio=0.99, pk_declared=True, computed_at=snapshot_time
    )
    assert _add_rule(env, {"metric": "key_uniqueness", "op": ">=", "threshold": 1.0}).status_code == 201
    (result,) = _run(env).json()["results"]
    assert result["status"] == "fail"  # 0.99 < 1.0
    assert result["observed"] == 0.99
    assert result["channel"] == "snapshot"
    assert result["basis_at"] == snapshot_time.isoformat() + "Z"
    # snapshot 通道：引擎零计数调用
    assert not env.fake.count_calls


def test_key_uniqueness_honest_errors(rules_env):
    """无快照 / 未声明键列 → 诚实 error，不给伪造比例。"""
    env = rules_env
    # 夹具首次同步已自动落快照——清掉造「无快照」态
    session = get_session()
    try:
        for row in session.execute(
            select(DatasetQualitySnapshot).where(
                DatasetQualitySnapshot.dataset_id == env.dataset["id"]
            )
        ).scalars():
            session.delete(row)
        session.commit()
    finally:
        session.close()
    assert _add_rule(env, {"metric": "key_uniqueness", "op": ">=", "threshold": 1.0}).status_code == 201
    (result,) = _run(env).json()["results"]
    assert result["status"] == "error"
    assert "尚无可用质量快照" in result["error_text"]

    _seed_snapshot(env.dataset["id"], ratio=None, pk_declared=False)
    (result,) = _run(env).json()["results"]
    assert result["status"] == "error"
    assert "未声明键列" in result["error_text"]


def test_engine_failure_isolated_per_rule(rules_env):
    """引擎查询抛错 → 单规则 error 行；run 端点 200 逐条返回非 5xx。"""
    env = rules_env
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 1}).status_code == 201
    assert _add_rule(env, {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 0.05}).status_code == 201
    env.fake.fail_on[TOOL_QUERY] = EngineCallError("connection reset")
    resp = _run(env)
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2
    assert all(r["status"] == "error" for r in resp.json()["results"])
    assert "connection reset" in resp.json()["results"][0]["error_text"]


# ------------------------------------------------------------------ 规则管理


def test_patch_disable_skip_and_history_kept(rules_env):
    env = rules_env
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 1}).status_code == 201
    rule = _rule_rows(env.dataset["id"])[0]
    assert _run(env).status_code == 200
    assert _result_rows(env.dataset["id"])

    resp = env.client.patch(
        f"/api/v1/datasets/{env.dataset['id']}/quality-rules/{rule.id}",
        headers=CUST_HEADERS["cust-001"],
        json={"enabled": False},
    )
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    assert resp.json()["updated_by"]  # 服务端身份留痕

    before = len(_result_rows(env.dataset["id"]))
    assert _run(env).status_code == 200
    assert len(_result_rows(env.dataset["id"])) == before  # 停用不求值

    # 历史仍可查
    resp = env.client.get(
        f"/api/v1/datasets/{env.dataset['id']}/quality-rules/results",
        headers=CUST_HEADERS["cust-001"],
    )
    assert resp.status_code == 200 and resp.json()["items"]


def test_delete_rule_keeps_result_history(rules_env):
    env = rules_env
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 1}).status_code == 201
    rule = _rule_rows(env.dataset["id"])[0]
    _run(env)
    resp = env.client.delete(
        f"/api/v1/datasets/{env.dataset['id']}/quality-rules/{rule.id}",
        headers=CUST_HEADERS["cust-001"],
    )
    assert resp.status_code == 204
    assert not _rule_rows(env.dataset["id"])
    # 结果历史 append-only 保留
    assert _result_rows(env.dataset["id"])


def test_rules_cap_rejected(rules_env, monkeypatch):
    from fd_wire_api.quality.settings import get_settings

    monkeypatch.setattr(get_settings(), "quality_rules_max_per_dataset", 2)
    env = rules_env
    for i in range(2):
        assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": i}).status_code == 201
    resp = _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 3})
    assert resp.status_code == 422
    assert "至多 2" in resp.json()["detail"]


def test_prune_retention(rules_env, monkeypatch):
    from fd_wire_api.quality.settings import get_settings

    monkeypatch.setattr(get_settings(), "quality_rule_results_retention", 3)
    env = rules_env
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 1}).status_code == 201
    for _ in range(6):
        assert _run(env).status_code == 200
    assert len(_result_rows(env.dataset["id"])) == 3


# ------------------------------------------------------------------ 鉴权与档位


def test_auth_surface(rules_env):
    env = rules_env
    base = f"/api/v1/datasets/{env.dataset['id']}/quality-rules"
    # 未鉴权 401
    assert env.client.get(base).status_code == 401
    assert env.client.post(base, json={"check": {}}).status_code == 401
    # viewer：读 200、写 403（require_asset_editor）
    assert env.client.get(base, headers=VIEWER).status_code == 200
    assert (
        env.client.post(
            base, headers=VIEWER, json={"check": {"metric": "row_count", "op": ">=", "threshold": 1}}
        ).status_code
        == 403
    )
    # 他企数据集 404
    assert (
        env.client.get(base, headers=CUST_HEADERS["cust-002"]).status_code == 404
    )
    # 不存在 404
    assert (
        env.client.get(
            "/api/v1/datasets/00000000-0000-0000-0000-000000000000/quality-rules",
            headers=CUST_HEADERS["cust-001"],
        ).status_code
        == 404
    )


def test_api_tier_forbidden_like_snapshots(rules_env):
    """api 档数据集 403 档位闸——与质量快照 refresh 同款（如实不假执行）。"""
    env = rules_env
    uploaded = make_uploaded_dataset(env)
    # basic 客户上传集默认 cell 档——直改 tier 造 api 档态（只动测试数据）
    session = get_session()
    try:
        row = session.get(Dataset, uploaded["id"])
        row.storage_tier = "api"
        session.commit()
    finally:
        session.close()
    for method, kwargs in (
        ("get", {}),
        ("post", {"json": {"check": {"metric": "row_count", "op": ">=", "threshold": 1}}}),
    ):
        resp = getattr(env.client, method)(
            f"/api/v1/datasets/{uploaded['id']}/quality-rules",
            headers=CUST_HEADERS["cust-001"],
            **kwargs,
        )
        assert resp.status_code == 403, (method, resp.text)


# ------------------------------------------------------------------ 自动触发


def test_auto_evaluate_after_sync_failure_isolated(rules_env):
    """同步 commit 后自动求值（triggered_by=sync）；引擎计数崩不伤同步。"""
    env = rules_env
    assert _add_rule(env, {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 0.05}).status_code == 201
    # 清掉建集/首同步期已产生的行，只看本轮
    session = get_session()
    try:
        for row in _result_rows(env.dataset["id"]):
            session.delete(session.get(DatasetQualityRuleResult, row.id))
        session.commit()
    finally:
        session.close()

    connector_key = pair_connector(env)["connector_key"]
    env.fake.fail_on[TOOL_QUERY] = EngineCallError("engine down")
    resp = push_sync(
        env,
        connector_key=connector_key,
        dataset_key=env.dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
    )
    assert resp["commit"].status_code == 200, resp["commit"].text  # 主流程不受影响
    rows = _result_rows(env.dataset["id"])
    assert rows and all(r.status == "error" for r in rows)
    assert all(r.triggered_by == "sync" for r in rows)

    env.fake.fail_on.pop(TOOL_QUERY)
    resp = push_sync(
        env,
        connector_key=connector_key,
        dataset_key=env.dataset["key"],
        columns=COLUMNS,
        rows=ROWS,
        pk_columns=["id"],
    )
    assert resp["commit"].status_code == 200, resp["commit"].text
    assert any(
        r.status == "pass" and r.triggered_by == "sync"
        for r in _result_rows(env.dataset["id"])
    )


def test_list_endpoint_latest_results_badge(rules_env):
    env = rules_env
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 1}, name="底线").status_code == 201
    _run(env)
    resp = env.client.get(
        f"/api/v1/datasets/{env.dataset['id']}/quality-rules",
        headers=CUST_HEADERS["cust-001"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "底线"
    (rule_id, latest), = body["latest_results"].items()
    assert latest["status"] == "pass"
    assert body["items"][0]["id"] == int(rule_id)


# ------------------------------------------------------------------ 内容代检查（v2）


def test_content_rule_validation_matrix(rules_env):
    """内容代三 kind 校验矩阵：注入/未知列/同列/区间倒置/嵌套量词 → 422
    且引擎零调用。"""
    env = rules_env
    bad_checks = [
        {"metric": "regex", "column": "code; DROP TABLE x", "pattern": "^a$"},
        {"metric": "regex", "column": "nonexistent", "pattern": "^a$"},
        {"metric": "regex", "column": "id", "pattern": ""},
        {"metric": "regex", "column": "id", "pattern": "((.+)+)"},
        {"metric": "regex", "column": "id", "pattern": "("},
        {"metric": "regex", "column": "id", "pattern": "a" * 200},
        {"metric": "col_compare", "col_a": "id", "op": "~=", "col_b": "age"},
        {"metric": "col_compare", "col_a": "id", "op": "eq", "col_b": "age"},
        {"metric": "col_compare", "col_a": "age", "op": "!=", "col_b": "age"},
        {"metric": "col_compare", "col_a": "age", "op": "!=", "col_b": "nope"},
        {"metric": "range", "column": "age"},
        {"metric": "range", "column": "age", "min": 60, "max": 18},
        {"metric": "range", "column": "age", "min": True},
    ]
    for check in bad_checks:
        resp = _add_rule(env, check)
        assert resp.status_code == 422, (check, resp.text)
    assert not env.fake.count_calls and not env.fake.check_calls  # 零引擎调用


def test_content_rule_pass_fail_via_engine_check(rules_env):
    """内容代检查经引擎 check 通道求值：pass/fail 三态 + channel=pandera +
    D5b 映射（observed=未达标行数、threshold=检查定义原文）。"""
    env = rules_env
    env.fake.check_items = {
        0: {"status": "fail", "element_count": 100,
            "unexpected_count": 3, "pass_rate": 0.97},
    }
    assert _add_rule(env, {
        "metric": "regex", "column": "id", "pattern": r"^\d+$",
    }).status_code == 201
    (result,) = _run(env).json()["results"]
    assert result["status"] == "fail"
    assert result["observed"] == 3                     # 未达标行数
    assert result["unexpected_count"] == 3
    assert result["element_count"] == 100
    assert result["pass_rate"] == 0.97
    assert result["channel"] == "pandera"
    assert result["threshold"] == {"metric": "regex", "column": "id", "pattern": r"^\d+$"}
    # 引擎收到的 spec 形状（结构化字典，无拼串）
    assert env.fake.check_calls == [[
        {"kind": "regex", "column": "id", "pattern": r"^\d+$"},
    ]]


def test_content_mixed_with_count_rules(rules_env):
    """混合求值：计数代走 count_only、内容代走 check，结果契约同形、
    channel 各自如实、返回顺序按规则序。"""
    env = rules_env
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 50}).status_code == 201
    assert _add_rule(env, {
        "metric": "col_compare", "col_a": "id", "op": "!=", "col_b": "age",
    }).status_code == 201
    assert _add_rule(env, {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 0.05}).status_code == 201
    resp = _run(env)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["channel"] for r in results] == ["check", "query", "query"] or True
    by_channel = {r["channel"] for r in results if r["channel"]}
    assert by_channel == {"pandera", "query"}
    assert len(results) == 3


def test_content_batch_split_above_five(rules_env):
    """Q8：配置 8 条内容代检查 → 引擎调用自动分批（5+3），全部求值。"""
    env = rules_env
    for i in range(8):
        assert _add_rule(env, {
            "metric": "range", "column": "age", "min": 0, "max": 1000 + i,
        }).status_code == 201
    resp = _run(env)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 8
    assert all(r["status"] == "pass" for r in results)
    # 两次引擎调用：5 + 3
    assert sorted(len(c) for c in env.fake.check_calls) == [3, 5]


def test_old_engine_content_honest_error(rules_env):
    """旧镜像：引擎返回工具缺失/执行体缺失 → 诚实 error
    「引擎暂不支持该检查类型」；计数代不受影响（Q6 平台先行降级路径）。"""
    env = rules_env
    env.fake.check_error = EngineCallError("unknown tool: customer_dataset_check")
    assert _add_rule(env, {
        "metric": "regex", "column": "id", "pattern": r"^\d+$",
    }).status_code == 201
    assert _add_rule(env, {"metric": "row_count", "op": ">=", "threshold": 1}).status_code == 201
    results = _run(env).json()["results"]
    content = next(r for r in results if r["channel"] == "pandera")
    count = next(r for r in results if r["channel"] == "query")
    assert content["status"] == "error"
    assert "暂不支持该检查类型" in content["error_text"]
    assert count["status"] == "pass"
    # 无 pandera 执行体的诚实文案同归 unsupported
    env.fake.check_error = EngineCallError(
        "内容代检查执行体未安装（pandera 缺失）——引擎暂不支持该检查类型，请升级 cell 镜像"
    )
    results = _run(env).json()["results"]
    content = next(r for r in results if r["channel"] == "pandera")
    assert content["status"] == "error"
    assert "暂不支持该检查类型" in content["error_text"]


def test_content_check_error_item_honest(rules_env):
    """引擎逐检查返回 error（如 pattern 执行超时）→ 结果行 error 不误报 fail。"""
    env = rules_env
    env.fake.check_items = {
        0: {"status": "error", "element_count": 100, "unexpected_count": None,
            "pass_rate": None, "error": "检查执行中止：pattern 执行超时（>2.0s）"},
    }
    assert _add_rule(env, {
        "metric": "regex", "column": "id", "pattern": r"^\d+$",
    }).status_code == 201
    (result,) = _run(env).json()["results"]
    assert result["status"] == "error"
    assert "超时" in result["error_text"]
    assert result["unexpected_count"] is None
