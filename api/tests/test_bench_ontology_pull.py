"""知验台本体拉取测试（add-bench-ontology-pull-client 任务 4.1–4.5）。

全 mock、绝不发真网：client 层经 httpx.MockTransport（transport= 注入，
design D5）；pull_and_store / 调度器路径经 monkeypatch 把 transport 注入
wrapper 挂到 client.pull_ontology 上（保留真实 env 解析/异常映射/脱敏全链）。

覆盖：
- 4.1 契约（client）：8 键形状 round-trip；形状漂移→PullTransportError；
  401/403 三态/404 两态如实透传；网络错误/非 JSON→PullTransportError；
  env 缺失→PullConfigError 指明 env 名；token 永不出现在异常消息（对端
  回显 detail 也被脱敏）。
- 4.2 落库幂等（store）：同 (domain, digest) 二存零新行 + 拉取日志完整；
  多版本共存/最新版定位；历史冻结（无 UPDATE 路径）；pull_and_store
  编排（成功落快照记 ok、失败只记脱敏日志零快照）。
- 4.3 投影（projection）：关系身份重建（键约定 + 结构化端点回落）、
  核心键严格/未知键宽容、kind 过滤、空域如实（空集不报错）。
- 4.4 调度纪律（scheduler）：enabled 缺省 false 零行为；两域一好一坏
  单轮 ok=1/failed=1、失败域日志保留对端语义、好域快照落库、下一轮继续；
  间隔解析（非法/非正回落默认）。
- 4.5 内部路由（routes）：X-Internal-Token 鉴权；空态读面返回空集不 500；
  POST pull 成功 200 落快照；失败按映射 502/503/404（对端语义保留在
  detail）且失败已入拉取日志。

测试库布局：bench 快照两表落独立内存 SQLite（StaticPool 单连接共享，
sessionmaker 直接注入——routes 覆盖 ``bench_pull.store.get_db``，调度器
monkeypatch ``scheduler._session_factory``），与全局 FD_WIRE_DATABASE_URL
完全隔离；autouse 关掉建表兜底（不落盘）并清 BENCH_PULL_* env。
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app
from fd_wire_api.bench_pull import client as bench_client
from fd_wire_api.bench_pull import projection as bench_projection
from fd_wire_api.bench_pull import scheduler as bench_scheduler
from fd_wire_api.bench_pull import store as bench_store
from fd_wire_api.bench_pull.projection import rebuild_identity
from fd_wire_api.bench_pull.store import (
    BenchOntologySnapshot,
    BenchPullBase,
    BenchPullLog,
    get_latest_snapshot,
    get_snapshot,
    list_snapshots,
    log_pull,
    pull_and_store,
    save_snapshot,
)

INTERNAL_TOKEN = "route-internal-token"
AUTH = {"X-Internal-Token": INTERNAL_TOKEN}
#: 真 token 形态（≥8 字符——低于脱敏最小长度的占位串不做替换）。
LONG_TOKEN = "tok-" + "x" * 40

BASE = "https://bench.test"


# ---------------------------------------------------------------- 环境卫生


@pytest.fixture(autouse=True)
def _bench_env_hygiene(monkeypatch):
    """BENCH_PULL_* env 全清（防真环境泄漏进测试）；关掉建表兜底不落盘。"""
    for name in (
        "BENCH_PULL_URL",
        "BENCH_CONSUMER_TOKEN",
        "BENCH_PULL_ENABLED",
        "BENCH_PULL_DOMAINS",
        "BENCH_PULL_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FD_WIRE_BENCH_PULL_AUTO_CREATE_TABLES", "0")


def _make_session_factory():
    """bench 两表独立内存库（StaticPool 单连接共享，跨 session 可见）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    BenchPullBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture()
def bench_db():
    """store/projection 直测用会话（用完即关）。"""
    sf = _make_session_factory()
    db = sf()
    try:
        yield db, sf
    finally:
        db.close()


# ---------------------------------------------------------------- mock 上游


def _ok(payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def _err(status: int, detail: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": detail})

    return handler


def _unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return handler


def _router_transport(responses: dict, requests_log: list | None = None):
    """按 domain_key 路由的 MockTransport；可选捕获请求供断言。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests_log is not None:
            requests_log.append(request)
        prefix = "/api/v1/distribution/"
        path = request.url.path
        assert path.startswith(prefix) and path.endswith("/ontology"), path
        domain_key = path[len(prefix) : -len("/ontology")]
        return responses[domain_key](request)

    return httpx.MockTransport(handler)


def _inject_transport(monkeypatch, transport) -> None:
    """把 transport 注入 client.pull_ontology（pull_and_store 内部在调用时
    才 ``from .client import pull_ontology``——patch 模块属性即可全链生效，
    同时保留真实 env 解析/异常映射/脱敏代码路径）。"""
    real_pull = bench_client.pull_ontology

    def _with_transport(*, domain_key, version=None, **kwargs):
        return real_pull(
            domain_key=domain_key, version=version, transport=transport, **kwargs
        )

    monkeypatch.setattr(bench_client, "pull_ontology", _with_transport)


def _env(monkeypatch, *, base=BASE, token=LONG_TOKEN) -> None:
    monkeypatch.setenv("BENCH_PULL_URL", base)
    monkeypatch.setenv("BENCH_CONSUMER_TOKEN", token)


# ---------------------------------------------------------------- 载荷 fixture

DIGEST_V1 = "b" * 64
DIGEST_V2 = "c" * 64


def _payload(
    *,
    domain="pv-industry",
    version=1,
    digest=DIGEST_V1,
    entities=None,
    relationships=None,
) -> dict:
    """对端 C3 契约 8 顶层键 fixture（形状以 law-bench distribution 面
    docstring 为正本；此处为消费端测试副本）。"""
    base_entities = [
        {
            "kind": "node",
            "external_key": "c1",
            "body": {"name": "上游-多晶硅"},
            "tier": 1,
            "sensitivity": "public",
            "attrs": {},
        },
        {
            "kind": "node",
            "external_key": "c2",
            "body": {"name": "下游-组件"},
            "tier": 2,
            "sensitivity": "public",
            "attrs": {},
        },
    ]
    base_relationships = [
        {
            "kind": "belongs_to",
            "external_key": "c1->belongs_to:c2",
            "attrs": {
                "from": {"external_key": "c1"},
                "to": {"external_key": "c2"},
                "from_key": "c1",
                "to_key": "c2",
            },
        },
    ]
    return {
        "domain": domain,
        "template_name": "industry-chain",
        "template_version": "1.0.0",
        "publication_version": version,
        "digest": digest,
        "entities": entities if entities is not None else base_entities,
        "relationships": relationships if relationships is not None else base_relationships,
        "changelog": [{"at": "2026-01-01T00:00:00Z", "note": f"v{version} 首发"}],
    }


# ================================================================ 4.1 契约（client）


def test_pull_200_roundtrip_all_8_keys(monkeypatch):
    _env(monkeypatch)
    payload = _payload()
    result = bench_client.pull_ontology(
        domain_key="pv-industry", transport=httpx.MockTransport(_ok(payload))
    )
    assert set(result.keys()) == set(bench_client.PAYLOAD_TOP_KEYS)
    assert result == payload  # 全量 round-trip，8 键一个不少


def test_pull_request_shape_url_header_version(monkeypatch):
    """请求形状：URL 模板、X-Consumer-Token 头、?version=N 钉版。"""
    _env(monkeypatch)
    log: list[httpx.Request] = []
    transport = _router_transport(
        {"pv-industry": _ok(_payload())}, requests_log=log
    )
    bench_client.pull_ontology(domain_key="pv-industry", transport=transport)
    assert log[0].url.path == "/api/v1/distribution/pv-industry/ontology"
    assert log[0].headers["X-Consumer-Token"] == LONG_TOKEN
    assert "version" not in log[0].url.params  # 缺省 = 最新发布

    bench_client.pull_ontology(
        domain_key="pv-industry", version=7, transport=transport
    )
    assert log[1].url.params["version"] == "7"


def test_pull_missing_core_key_is_shape_drift(monkeypatch):
    _env(monkeypatch)
    broken = {k: v for k, v in _payload().items() if k != "digest"}
    with pytest.raises(bench_client.PullTransportError, match="unexpected payload shape"):
        bench_client.pull_ontology(
            domain_key="pv-industry", transport=httpx.MockTransport(_ok(broken))
        )


def test_pull_top_level_not_object(monkeypatch):
    _env(monkeypatch)
    with pytest.raises(bench_client.PullTransportError, match="顶层不是对象"):
        bench_client.pull_ontology(
            domain_key="pv-industry",
            transport=httpx.MockTransport(_ok(["not", "a", "dict"])),
        )


def test_pull_401_auth_error_preserves_semantics(monkeypatch):
    _env(monkeypatch)
    with pytest.raises(bench_client.PullAuthError) as exc_info:
        bench_client.pull_ontology(
            domain_key="pv-industry",
            transport=httpx.MockTransport(
                _err(401, "缺少机器凭据：X-Consumer-Token 请求头")
            ),
        )
    assert "凭据" in str(exc_info.value)


@pytest.mark.parametrize(
    "detail", ["令牌未登记", "令牌已吊销", "该域不在绑定范围"]
)
def test_pull_403_three_states_preserved_verbatim(monkeypatch, detail):
    _env(monkeypatch)
    with pytest.raises(bench_client.PullAuthError) as exc_info:
        bench_client.pull_ontology(
            domain_key="pv-industry", transport=httpx.MockTransport(_err(403, detail))
        )
    assert detail in str(exc_info.value)  # 三态可区分、逐字保留


def test_pull_403_messages_distinguishable(monkeypatch):
    """未登记 vs 已吊销 → 同类异常但消息可区分（对端三态不塌缩）。"""
    _env(monkeypatch)
    messages = set()
    for detail in ("令牌未登记", "令牌已吊销"):
        with pytest.raises(bench_client.PullAuthError) as exc_info:
            bench_client.pull_ontology(
                domain_key="pv-industry",
                transport=httpx.MockTransport(_err(403, detail)),
            )
        messages.add(str(exc_info.value))
    assert len(messages) == 2


@pytest.mark.parametrize("detail", ["该域尚无发布", "该域无版本 v7"])
def test_pull_404_not_found_preserved(monkeypatch, detail):
    _env(monkeypatch)
    with pytest.raises(bench_client.PullNotFoundError) as exc_info:
        bench_client.pull_ontology(
            domain_key="pv-industry", transport=httpx.MockTransport(_err(404, detail))
        )
    assert detail in str(exc_info.value)


def test_pull_network_error_is_transport_error(monkeypatch):
    _env(monkeypatch)
    with pytest.raises(bench_client.PullTransportError, match="拉取不可达"):
        bench_client.pull_ontology(
            domain_key="pv-industry",
            transport=httpx.MockTransport(_unreachable()),
        )


def test_pull_non_json_body_is_transport_error(monkeypatch):
    _env(monkeypatch)
    html = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>oops</html>")
    )
    with pytest.raises(bench_client.PullTransportError, match="不是合法 JSON"):
        bench_client.pull_ontology(domain_key="pv-industry", transport=html)


def test_pull_missing_url_env_config_error(monkeypatch):
    monkeypatch.setenv("BENCH_CONSUMER_TOKEN", LONG_TOKEN)
    with pytest.raises(bench_client.PullConfigError) as exc_info:
        bench_client.pull_ontology(domain_key="pv-industry")
    assert "BENCH_PULL_URL" in str(exc_info.value)
    # 空白串等价未设置
    monkeypatch.setenv("BENCH_PULL_URL", "   ")
    with pytest.raises(bench_client.PullConfigError, match="BENCH_PULL_URL"):
        bench_client.pull_ontology(domain_key="pv-industry")


def test_pull_missing_token_env_config_error(monkeypatch):
    monkeypatch.setenv("BENCH_PULL_URL", BASE)
    with pytest.raises(bench_client.PullConfigError) as exc_info:
        bench_client.pull_ontology(domain_key="pv-industry")
    assert "BENCH_CONSUMER_TOKEN" in str(exc_info.value)


def test_pull_token_never_leaks_into_echoed_detail(monkeypatch):
    """token 纪律：对端把 token 回显进 403 detail → 异常消息里零泄漏。"""
    echo = f"令牌已吊销：{LONG_TOKEN}"
    with pytest.raises(bench_client.PullAuthError) as exc_info:
        bench_client.pull_ontology(
            domain_key="pv-industry",
            base_url=BASE,
            token=LONG_TOKEN,
            transport=httpx.MockTransport(_err(403, echo)),
        )
    message = str(exc_info.value)
    assert LONG_TOKEN not in message  # token 不落消息
    assert "令牌已吊销" in message  # 对端语义保留
    assert "***" in message  # 脱敏标记在场


# ================================================================ 4.2 落库幂等（store）


def test_save_snapshot_idempotent_and_pull_log_complete(bench_db):
    db, sf = bench_db
    payload = _payload()
    first = save_snapshot(
        db, domain_key="pv-industry", version=1, digest=DIGEST_V1, payload=payload
    )
    second = save_snapshot(
        db, domain_key="pv-industry", version=1, digest=DIGEST_V1, payload=payload
    )
    assert first["id"] == second["id"]  # 同 digest 二存命中既有行
    db2 = sf()
    try:
        count = db2.scalar(
            select(func.count()).select_from(BenchOntologySnapshot)
        )
        assert count == 1  # 零新快照行
        # 拉取史完整：两次拉取各留一条日志
        log_pull(db2, domain_key="pv-industry", version=1, digest=DIGEST_V1, ok=True)
        log_pull(db2, domain_key="pv-industry", version=1, digest=DIGEST_V1, ok=True)
        assert db2.scalar(select(func.count()).select_from(BenchPullLog)) == 2
    finally:
        db2.close()


def test_versions_coexist_latest_and_desc_order(bench_db):
    db, sf = bench_db
    v1 = save_snapshot(
        db, domain_key="pv-industry", version=1, digest=DIGEST_V1, payload=_payload(version=1)
    )
    v2 = save_snapshot(
        db,
        domain_key="pv-industry",
        version=2,
        digest=DIGEST_V2,
        payload=_payload(version=2, digest=DIGEST_V2),
    )
    assert v1["id"] != v2["id"]  # 不同版本各自成行
    rows = list_snapshots(db, domain_key="pv-industry")
    assert [row["version"] for row in rows] == [2, 1]  # version 倒序
    latest = get_latest_snapshot(db, domain_key="pv-industry")
    assert latest["version"] == 2  # get_latest = max version
    assert latest["digest"] == DIGEST_V2


def test_history_frozen_after_new_version(bench_db):
    """历史冻结：v2 落库后 v1 行逐字段不变（append-only，无 UPDATE 路径）。"""
    db, sf = bench_db
    frozen = save_snapshot(
        db, domain_key="pv-industry", version=1, digest=DIGEST_V1, payload=_payload(version=1)
    )
    save_snapshot(
        db,
        domain_key="pv-industry",
        version=2,
        digest=DIGEST_V2,
        payload=_payload(version=2, digest=DIGEST_V2),
    )
    reread = get_snapshot(db, domain_key="pv-industry", version=1)
    assert reread == frozen  # id/digest/payload/pulled_at 逐字段相等
    assert reread["digest"] == DIGEST_V1  # digest 逐字节不变


def test_pull_and_store_success_orchestration(monkeypatch, bench_db):
    """编排器：成功 → 快照落库 + ok=True 日志；二拉幂等（1 快照 2 日志）。"""
    _env(monkeypatch)
    db, sf = bench_db
    _inject_transport(
        monkeypatch, httpx.MockTransport(_ok(_payload()))
    )
    snapshot = pull_and_store(db, domain_key="pv-industry")
    assert snapshot["version"] == 1
    assert snapshot["digest"] == DIGEST_V1
    assert snapshot["payload"] == _payload()

    again = pull_and_store(db, domain_key="pv-industry")
    assert again["id"] == snapshot["id"]  # 重复拉取幂等
    db2 = sf()
    try:
        assert db2.scalar(select(func.count()).select_from(BenchOntologySnapshot)) == 1
        logs = list(db2.scalars(select(BenchPullLog)).all())
        assert len(logs) == 2  # 拉取史完整
        assert all(row.ok for row in logs)
        assert all(row.digest == DIGEST_V1 for row in logs)
    finally:
        db2.close()


def test_pull_and_store_auth_failure_no_snapshot_redacted_log(monkeypatch, bench_db):
    """失败如实不误标：403 → ok=False 脱敏日志，零快照行，异常原样抛出。"""
    _env(monkeypatch)
    db, sf = bench_db
    echo_detail = f"令牌已吊销：{LONG_TOKEN}"
    _inject_transport(
        monkeypatch,
        httpx.MockTransport(_err(403, echo_detail)),
    )
    with pytest.raises(bench_client.PullAuthError):
        pull_and_store(db, domain_key="pv-industry")

    db2 = sf()
    try:
        assert db2.scalar(select(func.count()).select_from(BenchOntologySnapshot)) == 0
        logs = list(db2.scalars(select(BenchPullLog)).all())
        assert len(logs) == 1
        row = logs[0]
        assert row.ok is False
        assert row.digest is None  # 失败无 digest，不误标成功
        assert "令牌已吊销" in row.error  # 对端语义保留
        assert LONG_TOKEN not in row.error  # token 不落库
    finally:
        db2.close()


# ================================================================ 4.3 投影（projection）


def test_rebuild_identity_full_rel():
    rel = {
        "kind": "belongs_to",
        "external_key": "c1->belongs_to:c2",
        "attrs": {
            "from": {"external_key": "c1"},
            "to": {"external_key": "c2"},
            "from_key": "c1",
            "to_key": "c2",
        },
    }
    identity = rebuild_identity(rel)
    assert identity == {
        "from_key": "c1",
        "kind": "belongs_to",
        "to_key": "c2",
        "external_key": "c1->belongs_to:c2",  # 键约定重建
    }


def test_rebuild_identity_endpoint_fallback():
    """显式 from_key/to_key 缺省 → 回落结构化端点 external_key。"""
    rel = {
        "kind": "belongs_to",
        "attrs": {
            "from": {"external_key": "c1"},
            "to": {"external_key": "c2"},
        },
    }
    assert rebuild_identity(rel)["external_key"] == "c1->belongs_to:c2"


def test_rebuild_identity_missing_from_key_strict():
    """核心键严格：from_key（连同端点回落）解不出 → ValueError。"""
    rel = {"kind": "belongs_to", "attrs": {"to": {"external_key": "c2"}}}
    with pytest.raises(ValueError, match="from_key"):
        rebuild_identity(rel)


def test_rebuild_identity_unknown_extra_keys_tolerated():
    """D6 宽容读：对端演进出的未知键不报错。"""
    rel = {
        "kind": "belongs_to",
        "attrs": {"from_key": "c1", "to_key": "c2", "future_field": {"any": 1}},
        "note": "对端新增键",
    }
    assert rebuild_identity(rel)["to_key"] == "c2"


def _seed_two_versions(db):
    save_snapshot(
        db, domain_key="pv-industry", version=1, digest=DIGEST_V1, payload=_payload(version=1)
    )
    v2_entities = [
        {
            "kind": "node",
            "external_key": "c1",
            "body": {"name": "上游-多晶硅-v2"},
            "tier": 1,
            "sensitivity": "public",
            "attrs": {},
            "note": "对端新增键",  # 未知键宽容：原样透传
        },
        {
            "kind": "material",
            "external_key": "m1",
            "body": {"name": "硅料"},
            "tier": 3,
            "sensitivity": "internal",
            "attrs": {},
        },
    ]
    save_snapshot(
        db,
        domain_key="pv-industry",
        version=2,
        digest=DIGEST_V2,
        payload=_payload(version=2, digest=DIGEST_V2, entities=v2_entities),
    )


def test_list_entities_kind_filter_and_unknown_key_passthrough(bench_db):
    db, _sf = bench_db
    _seed_two_versions(db)
    nodes = bench_projection.list_entities(db, domain_key="pv-industry", kind="node")
    assert [item["external_key"] for item in nodes] == ["c1"]
    assert nodes[0]["body"] == {"name": "上游-多晶硅-v2"}
    assert nodes[0]["note"] == "对端新增键"  # 未知键不丢
    materials = bench_projection.list_entities(db, domain_key="pv-industry", kind="material")
    assert [item["external_key"] for item in materials] == ["m1"]


def test_list_entities_version_none_is_latest_pinned_works(bench_db):
    db, _sf = bench_db
    _seed_two_versions(db)
    latest = bench_projection.list_entities(db, domain_key="pv-industry")
    assert latest[0]["body"] == {"name": "上游-多晶硅-v2"}  # version=None → 最新
    pinned = bench_projection.list_entities(
        db, domain_key="pv-industry", version=1
    )
    assert pinned[0]["body"] == {"name": "上游-多晶硅"}  # 钉历史版本


def test_list_entities_unknown_domain_empty_no_raise(bench_db):
    """空域如实：从未拉取过的域返回空集，不报错。"""
    db, _sf = bench_db
    assert bench_projection.list_entities(db, domain_key="ghost-domain") == []
    assert bench_projection.list_relationships(db, domain_key="ghost-domain") == []
    assert get_latest_snapshot(db, domain_key="ghost-domain") is None  # 无快照标记
    assert list_snapshots(db, domain_key="ghost-domain") == []


def test_list_entities_core_key_missing_raises(bench_db):
    """D6 严格半边：实体缺核心键（body）→ ValueError 宁红不静默丢身份。"""
    db, _sf = bench_db
    save_snapshot(
        db,
        domain_key="broken-domain",
        version=1,
        digest="d" * 64,
        payload={
            "entities": [{"external_key": "x", "kind": "node"}],  # 缺 body
            "relationships": [],
        },
    )
    with pytest.raises(ValueError, match="缺少核心键"):
        bench_projection.list_entities(db, domain_key="broken-domain")


def test_list_relationships_carry_rebuilt_identity(bench_db):
    db, _sf = bench_db
    save_snapshot(
        db, domain_key="pv-industry", version=1, digest=DIGEST_V1, payload=_payload()
    )
    rels = bench_projection.list_relationships(db, domain_key="pv-industry")
    assert len(rels) == 1
    entry = rels[0]
    assert entry["from_key"] == "c1"
    assert entry["kind"] == "belongs_to"
    assert entry["to_key"] == "c2"
    assert entry["external_key"] == "c1->belongs_to:c2"


def test_list_domains_with_snapshots_and_empty_domain(bench_db):
    db, _sf = bench_db
    save_snapshot(
        db, domain_key="pv-industry", version=1, digest=DIGEST_V1, payload=_payload()
    )
    save_snapshot(
        db,
        domain_key="ai-models",
        version=3,
        digest="e" * 64,
        payload=_payload(domain="ai-models", version=3, digest="e" * 64),
    )
    domains = bench_projection.list_domains_with_snapshots(db)
    by_key = {item["domain_key"]: item for item in domains}
    assert by_key["pv-industry"]["version"] == 1
    assert by_key["pv-industry"]["digest"] == DIGEST_V1
    assert by_key["ai-models"]["version"] == 3
    # 从未拉取的域不进清单；查询空域如实（空集 + None 标记，不报错）
    assert "ghost-domain" not in by_key
    assert bench_projection.list_versions(db, domain_key="ghost-domain") == []


# ================================================================ 4.4 调度纪律（scheduler）


def test_scheduler_start_disabled_is_noop(monkeypatch):
    """BENCH_PULL_ENABLED 缺省/false/0 → start 即返回，零任务零循环。"""
    bench_scheduler.stop_bench_pull_scheduler()  # 清可能的全局残留
    monkeypatch.delenv("BENCH_PULL_ENABLED", raising=False)
    bench_scheduler.start_bench_pull_scheduler()
    assert bench_scheduler._task is None

    for value in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("BENCH_PULL_ENABLED", value)
        bench_scheduler.start_bench_pull_scheduler()
        assert bench_scheduler._task is None


def test_domains_parse_dedupe_keep_order(monkeypatch):
    monkeypatch.setenv("BENCH_PULL_DOMAINS", " b-domain , a-domain ,b-domain,  ")
    assert bench_scheduler._domains() == ["b-domain", "a-domain"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 3600.0),  # 缺省
        ("120", 120.0),  # 合法值被尊重
        (" 90 ", 90.0),
        ("abc", 3600.0),  # 非法回落默认
        ("-5", 3600.0),  # 非正回落默认
        ("0", 3600.0),
    ],
)
def test_interval_seconds_parse(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("BENCH_PULL_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("BENCH_PULL_INTERVAL_SECONDS", raw)
    assert bench_scheduler._interval_seconds() == expected


def test_tick_partial_failure_continues_next_round(monkeypatch):
    """多域部分失败：一轮 ok=1/failed=1；好域快照落库；失败域日志保留对端
    语义；下一轮继续不崩（spec「多域部分失败」场景）。"""
    sf = _make_session_factory()
    monkeypatch.setattr(bench_scheduler, "_session_factory", lambda: sf)
    monkeypatch.setattr(bench_scheduler, "init_db", lambda: None)
    _env(monkeypatch)
    # 去重保序：pv-industry 出现两次也只拉一次
    monkeypatch.setenv("BENCH_PULL_DOMAINS", "pv-industry, locked-domain, pv-industry")
    _inject_transport(
        monkeypatch,
        _router_transport(
            {
                "pv-industry": _ok(_payload()),
                "locked-domain": _err(403, "令牌已吊销"),
            }
        ),
    )

    summary = bench_scheduler._tick_once()
    assert summary == {"ok": 1, "failed": 1}

    db = sf()
    try:
        snapshots = list(db.scalars(select(BenchOntologySnapshot)).all())
        assert len(snapshots) == 1  # 好域快照落库
        assert snapshots[0].domain_key == "pv-industry"
        assert snapshots[0].digest == DIGEST_V1
        logs = {
            row.domain_key: row
            for row in db.scalars(select(BenchPullLog)).all()
        }
        assert logs["pv-industry"].ok is True
        assert logs["locked-domain"].ok is False
        assert "令牌已吊销" in logs["locked-domain"].error  # 对端语义保留
        assert LONG_TOKEN not in logs["locked-domain"].error  # token 不落日志
    finally:
        db.close()

    # 下一轮继续：同摘要、快照仍 1 行（幂等）、日志各累积到 2 条
    assert bench_scheduler._tick_once() == {"ok": 1, "failed": 1}
    db = sf()
    try:
        assert db.scalar(select(func.count()).select_from(BenchOntologySnapshot)) == 1
        ok_logs = [
            row
            for row in db.scalars(select(BenchPullLog)).all()
            if row.ok and row.domain_key == "pv-industry"
        ]
        failed_logs = [
            row
            for row in db.scalars(select(BenchPullLog)).all()
            if not row.ok and row.domain_key == "locked-domain"
        ]
        assert len(ok_logs) == 2
        assert len(failed_logs) == 2
    finally:
        db.close()


def test_tick_empty_domains_is_zero_summary(monkeypatch):
    monkeypatch.setattr(bench_scheduler, "init_db", lambda: None)
    monkeypatch.delenv("BENCH_PULL_DOMAINS", raising=False)
    assert bench_scheduler._tick_once() == {"ok": 0, "failed": 0}


# ================================================================ 4.5 内部路由（routes）


@pytest.fixture()
def routes_env(monkeypatch):
    """内部路由测试态：内存库覆盖 bench get_db + TestClient（不起 lifespan）
    + X-Internal-Token env 配齐（_require_internal 按 env 比对）。"""
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    sf = _make_session_factory()

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[bench_store.get_db] = override_get_db
    try:
        yield sf
    finally:
        app.dependency_overrides.pop(bench_store.get_db, None)


def test_routes_require_internal_token(routes_env, monkeypatch):
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    url = "/api/v1/internal/bench-ontology/domains"
    assert routes_env is not None
    client = TestClient(app)
    assert client.get(url).status_code == 401  # 缺头
    assert client.get(url, headers={"X-Internal-Token": "wrong"}).status_code == 401
    assert client.get(url, headers=AUTH).status_code == 200


def test_routes_empty_state_reads_are_empty_not_500(routes_env, monkeypatch):
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    client = TestClient(app)
    prefix = "/api/v1/internal/bench-ontology"
    resp = client.get(f"{prefix}/domains", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == []
    for tail in ("versions", "entities", "relationships"):
        resp = client.get(f"{prefix}/ghost-domain/{tail}", headers=AUTH)
        assert resp.status_code == 200  # 空域如实：空集不报错
        assert resp.json() == []


def test_routes_pull_happy_path_lands_snapshot(routes_env, monkeypatch):
    _env(monkeypatch)
    log: list[httpx.Request] = []
    _inject_transport(monkeypatch, _router_transport(
        {"pv-industry": _ok(_payload())}, requests_log=log
    ))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/internal/bench-ontology/pv-industry/pull",
        json={"version": 1},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain_key"] == "pv-industry"
    assert body["version"] == 1
    assert body["digest"] == DIGEST_V1
    assert log[0].url.params["version"] == "1"  # body.version 钉版直达上游

    # 快照真落库 + 域清单可见
    db = routes_env()
    try:
        assert db.scalar(select(func.count()).select_from(BenchOntologySnapshot)) == 1
    finally:
        db.close()
    domains = client.get("/api/v1/internal/bench-ontology/domains", headers=AUTH).json()
    assert domains == [
        {"domain_key": "pv-industry", "version": 1, "digest": DIGEST_V1}
    ]


def test_routes_pull_auth_failure_502_preserves_upstream(routes_env, monkeypatch):
    _env(monkeypatch)
    _inject_transport(
        monkeypatch, _router_transport({"pv-industry": _err(403, "令牌未登记")})
    )
    client = TestClient(app)
    resp = client.post(
        "/api/v1/internal/bench-ontology/pv-industry/pull", headers=AUTH
    )
    assert resp.status_code == 502
    assert "令牌未登记" in resp.json()["detail"]  # 对端语义保留在 detail
    # 失败已如实入拉取日志（store 层收口），零快照
    db = routes_env()
    try:
        assert db.scalar(select(func.count()).select_from(BenchOntologySnapshot)) == 0
        logs = list(db.scalars(select(BenchPullLog)).all())
        assert len(logs) == 1 and logs[0].ok is False
    finally:
        db.close()


def test_routes_pull_transport_failure_502(routes_env, monkeypatch):
    _env(monkeypatch)
    _inject_transport(
        monkeypatch, _router_transport({"pv-industry": _unreachable()})
    )
    client = TestClient(app)
    resp = client.post(
        "/api/v1/internal/bench-ontology/pv-industry/pull", headers=AUTH
    )
    assert resp.status_code == 502
    assert "拉取不可达" in resp.json()["detail"]


def test_routes_pull_config_failure_503_names_env(routes_env, monkeypatch):
    monkeypatch.setenv("BENCH_CONSUMER_TOKEN", LONG_TOKEN)
    monkeypatch.delenv("BENCH_PULL_URL", raising=False)  # 基址未配置
    client = TestClient(app)
    resp = client.post(
        "/api/v1/internal/bench-ontology/pv-industry/pull", headers=AUTH
    )
    assert resp.status_code == 503
    assert "BENCH_PULL_URL" in resp.json()["detail"]  # detail 指明缺哪个 env


def test_routes_pull_not_found_404(routes_env, monkeypatch):
    _env(monkeypatch)
    _inject_transport(
        monkeypatch, _router_transport({"pv-industry": _err(404, "该域无版本 v7")})
    )
    client = TestClient(app)
    resp = client.post(
        "/api/v1/internal/bench-ontology/pv-industry/pull",
        json={"version": 7},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert "该域无版本 v7" in resp.json()["detail"]


def test_routes_corrupt_snapshot_projection_500(routes_env):
    """快照数据坏了（实体缺核心键）→ 500 如实暴露，不伪装 4xx。"""
    db = routes_env()
    try:
        save_snapshot(
            db,
            domain_key="corrupt-domain",
            version=1,
            digest="d" * 64,
            payload={"entities": [{"external_key": "x", "kind": "node"}]},
        )
    finally:
        db.close()
    client = TestClient(app)
    resp = client.get(
        "/api/v1/internal/bench-ontology/corrupt-domain/entities", headers=AUTH
    )
    assert resp.status_code == 500
    assert "缺少核心键" in resp.json()["detail"]


# ================================================ 6. 登录态只读读面（console 面）
# add-bench-wire-seam-contract 任务 2.1/2.2：/api/v1/console/bench-ontology
# 与内部面**分属两套信任边界**——这一组用例并存地把两件事同时钉住：
#   (1) 新面只要有 console 登录即 200，且不认 X-Internal-Token；
#   (2) 旧内部面仍然不认 console 登录（无 X-Internal-Token 就 401）。
# 任何把它们合并成一套鉴权边界的改动都会让其中一半变红。

CONSOLE_SUB = "sub-bench-console-a"
CONSOLE_CUST = "bench-console-cust-a"


@pytest.fixture()
def console_env(tmp_path, monkeypatch):
    """平台库 + bench 快照库 + TestClient 登录态（复用 console_fixture）。"""
    from console_fixture import install_fake_jwks, make_console_env

    install_fake_jwks(monkeypatch)
    env = make_console_env(
        tmp_path, monkeypatch, customers=[(CONSOLE_SUB, CONSOLE_CUST)]
    )
    sf = _make_session_factory()

    def override_get_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[bench_store.get_db] = override_get_db
    try:
        yield env, sf
    finally:
        app.dependency_overrides.pop(bench_store.get_db, None)


def test_console_face_requires_login(console_env):
    env, _sf = console_env
    client = TestClient(app)
    url = "/api/v1/console/bench-ontology"
    assert client.get(url).status_code == 401  # 未登录
    assert client.get(url, headers=env.headers_for(CONSOLE_SUB)).status_code == 200


def test_console_face_does_not_accept_internal_token(console_env, monkeypatch):
    """内部令牌不是这张脸的凭据：不带 console 登录仍是 401。"""
    env, _sf = console_env
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    client = TestClient(app)
    url = "/api/v1/console/bench-ontology"
    assert client.get(url, headers=AUTH).status_code == 401


def test_internal_face_does_not_accept_console_login(console_env):
    """反向边界：内部面不认 console 登录（两套信任边界未被合并）。"""
    env, _sf = console_env
    client = TestClient(app)
    url = "/api/v1/internal/bench-ontology/domains"
    assert client.get(url, headers=env.headers_for(CONSOLE_SUB)).status_code == 401


def test_console_overview_empty_is_empty_list(console_env):
    """从未拉取过 → domains 为空列表（「还没拉过」不是 404）。"""
    env, _sf = console_env
    client = TestClient(app)
    resp = client.get(
        "/api/v1/console/bench-ontology", headers=env.headers_for(CONSOLE_SUB)
    )
    assert resp.status_code == 200
    assert resp.json() == {"domains": []}


def test_console_overview_metadata_only_no_payload(console_env):
    """有快照时只出元数据：版本/digest/计数/时间，响应体不含正文。"""
    env, sf = console_env
    db = sf()
    try:
        save_snapshot(
            db,
            domain_key="pv-industry",
            version=1,
            digest=DIGEST_V1,
            payload=_payload(),
        )
    finally:
        db.close()
    client = TestClient(app)
    resp = client.get(
        "/api/v1/console/bench-ontology", headers=env.headers_for(CONSOLE_SUB)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == ["domains"]
    entries = body["domains"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["domain_key"] == "pv-industry"
    assert entry["version"] == 1
    assert entry["digest"] == DIGEST_V1
    assert entry["entity_count"] == 2
    assert entry["relationship_count"] == len(_payload()["relationships"])
    assert entry["pulled_at"]  # ISO 文本，非空
    # 正文不可见：任何条目正文/载荷键都不得出现在响应里
    raw = resp.text
    assert "payload" not in raw
    assert "上游-多晶硅" not in raw  # body 里的实体名
