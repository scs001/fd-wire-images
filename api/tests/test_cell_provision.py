"""cell 开通自助化测试（add-self-serve-cell-provision 任务 3.1）。

覆盖（全 mock，绝不发真网/真 docker）：
- resolve_cell_endpoint 三源顺序：env map 显式命中 > DB 注册表（done 且
  endpoint_url 非空）> 约定派生；显式口径下 map 已配且 DB 亦未命中 →
  None（既有 test_ai_infra 断言的语义保持不动）；表缺失/库故障静默走派生
- 幂等 enqueue：同 customer 不重复建行；failed 复位重入队
- 领任务原子：sqlite 同库多线程并发领取，同 id 只被领一次（exactly-once）
- 回写：done 落 endpoints 表（幂等 upsert）；failed 留 error+step 痕；
  状态闸（非 running 409）、未知 id 404、缺字段 422
- 内部端点：无任务 204；X-Internal-Token 缺失/错误 401
- 升档挂点：admin PATCH 改档至付费档 → 自动入队 + 运营邮件（support@）；
  免费档不入队；重复改档幂等跳过
- admin 状态页：GET /internal/admin/cell-provision-requests 只读列表
- 迁移链：0053_cell_provision 钉 down_revision=0052_bench_ontology_snapshots
  （并发会话 0052 已在 versions/ 成链；0052 在途改动区六个测试文件不动，
  链断言由本文件承载）

测试库布局：cell_provision 两表落独立 sqlite 文件库（env 指库 + reset 运行
时）；admin 改档用例另起 admin/billing 同库文件库（照 test_admin_api 手法）。
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app
from fd_wire_api.cell_provision import notifications as cp_notifications
from fd_wire_api.cell_provision.db import (
    CellProvisionBase,
    get_db as get_cell_provision_db,
    reset_cell_provision_runtime,
)
from fd_wire_api.cell_provision.models import CellProvisionRequest, CustomerCellEndpoint
from fd_wire_api.cell_provision import service as cp_service
from fd_wire_api.ai.executor import resolve_cell_endpoint
from fd_wire_api.ai.db import reset_ai_runtime

INTERNAL_TOKEN = "test-internal-token"
INTERNAL = {"X-Internal-Token": INTERNAL_TOKEN}

ADMIN_TOKEN = "test-admin-token"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


class RecordingSmtp:
    """记录 _smtp_send 调用形状的假出口（可注入失败）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, subject, body, *, to_addr, config):
        self.calls.append(
            {"subject": subject, "body": body, "to_addr": to_addr, "config": config}
        )


@pytest.fixture()
def cp_env(tmp_path, monkeypatch):
    """cell_provision 独立文件库 + HTTP 客户端（不走 lifespan）。

    admin 列表端点与内部端点共用同一覆盖会话（admin/routes 经
    cell_provision.db.get_db 直查）；升级钩子经 new_session() 同库写。
    """
    db_url = f"sqlite:///{tmp_path/'cell_provision.db'}"
    monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    reset_cell_provision_runtime()

    engine = create_engine(
        db_url, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    CellProvisionBase.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_cell_provision_db] = override_get_db
    # 通知改同步执行（生产为后台线程，此处保证断言确定性）
    monkeypatch.setattr(cp_notifications, "_deliver_async", lambda task: task())
    client = TestClient(app)  # 不走 lifespan：避免默认引擎副作用
    yield SimpleNamespace(client=client, engine=engine, sf=TestingSession, db_url=db_url)
    app.dependency_overrides.pop(get_cell_provision_db, None)
    engine.dispose()
    reset_cell_provision_runtime()


@pytest.fixture()
def smtp_env(monkeypatch):
    """给足最小 SMTP 配置 + 假出口（记录调用，不发真网）。"""
    monkeypatch.setenv("SMTP_HOST", "smtp.test.internal")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_FROM", "wire-notify@finddatatech.cloud")
    monkeypatch.delenv("CELL_PROVISION_NOTIFY_EMAIL", raising=False)
    monkeypatch.setattr(cp_notifications, "_mail_skip_warned", False)
    recorder = RecordingSmtp()
    monkeypatch.setattr(cp_notifications, "_smtp_send", recorder)
    return recorder


def _enqueue(cp_env, customer_id: str, state: str = "queued") -> CellProvisionRequest:
    db = cp_env.sf()
    try:
        row = CellProvisionRequest(customer_id=customer_id, state=state, steps="[]")
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# resolve_cell_endpoint 三源顺序
# ---------------------------------------------------------------------------


class TestResolveThreeTier:
    def test_env_map_hit_wins_over_db(self, tmp_path, monkeypatch):
        """env map 显式命中 > DB 注册表（第一优先口径不变）。"""
        db_url = f"sqlite:///{tmp_path/'cp.db'}"
        monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
        monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "acme=http://map-acme/mcp")
        monkeypatch.delenv("AI_CELL_ENDPOINT_BASE", raising=False)
        reset_cell_provision_runtime()
        reset_ai_runtime()
        try:
            engine = create_engine(db_url)
            CellProvisionBase.metadata.create_all(engine)
            sess = sessionmaker(bind=engine)()
            sess.add(
                CustomerCellEndpoint(
                    customer_id="acme", endpoint_url="https://db-acme/mcp", state="done"
                )
            )
            sess.commit()
            sess.close()
            engine.dispose()
            assert resolve_cell_endpoint("acme") == "http://map-acme/mcp"
        finally:
            reset_ai_runtime()
            reset_cell_provision_runtime()

    def test_db_fallback_hit(self, tmp_path, monkeypatch):
        """map 未配（或未含该客户且 DB 命中）→ 注册表值；派生不再兜底。"""
        db_url = f"sqlite:///{tmp_path/'cp.db'}"
        monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
        monkeypatch.delenv("AI_CELL_ENDPOINT_MAP", raising=False)
        reset_cell_provision_runtime()
        reset_ai_runtime()
        try:
            engine = create_engine(db_url)
            CellProvisionBase.metadata.create_all(engine)
            sess = sessionmaker(bind=engine)()
            sess.add(
                CustomerCellEndpoint(
                    customer_id="selfserve-1",
                    endpoint_url="https://mcp.finddatatech.cloud/fd-wire-cell-selfserve-1/mcp",
                    state="done",
                )
            )
            # 非 done / 空 URL 行不命中（读路径口径：state=done 且非空）
            sess.add(
                CustomerCellEndpoint(
                    customer_id="pending-1", endpoint_url="https://x/mcp", state="running"
                )
            )
            sess.add(
                CustomerCellEndpoint(
                    customer_id="empty-url", endpoint_url="", state="done"
                )
            )
            sess.commit()
            sess.close()
            engine.dispose()
            assert (
                resolve_cell_endpoint("selfserve-1")
                == "https://mcp.finddatatech.cloud/fd-wire-cell-selfserve-1/mcp"
            )
            # 未命中行照旧走派生（免费路径不变）
            assert (
                resolve_cell_endpoint("pending-1")
                == "https://mcp.finddatatech.cloud/fd-wire-cell-pending-1/mcp"
            )
            assert (
                resolve_cell_endpoint("empty-url")
                == "https://mcp.finddatatech.cloud/fd-wire-cell-empty-url/mcp"
            )
        finally:
            reset_ai_runtime()
            reset_cell_provision_runtime()

    def test_explicit_map_miss_no_db_hits_none(self, tmp_path, monkeypatch):
        """显式部署口径：map 已配、DB 未命中 → None（test_ai_infra 语义保持）。"""
        db_url = f"sqlite:///{tmp_path/'cp.db'}"
        monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
        monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "other=http://cell-1/mcp")
        reset_cell_provision_runtime()
        reset_ai_runtime()
        try:
            engine = create_engine(db_url)
            CellProvisionBase.metadata.create_all(engine)
            engine.dispose()  # 表存在但无行
            assert resolve_cell_endpoint("nobody") is None
        finally:
            reset_ai_runtime()
            reset_cell_provision_runtime()

    def test_table_missing_falls_back_to_derivation(self, tmp_path, monkeypatch):
        """表缺失/库读失败 → 静默走约定派生（不破免费路径）。"""
        db_url = f"sqlite:///{tmp_path/'empty.db'}"
        monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
        monkeypatch.delenv("AI_CELL_ENDPOINT_MAP", raising=False)
        monkeypatch.setenv("AI_CELL_ENDPOINT_BASE", "https://registry.example")
        reset_cell_provision_runtime()
        reset_ai_runtime()
        try:
            # 引擎指向未建表的空库——查询 OperationalError 被吞掉
            assert (
                resolve_cell_endpoint("cust-042")
                == "https://registry.example/fd-wire-cell-cust-042/mcp"
            )
        finally:
            reset_ai_runtime()
            reset_cell_provision_runtime()


# ---------------------------------------------------------------------------
# 队列 service：幂等 enqueue / 原子 claim / 回写
# ---------------------------------------------------------------------------


class TestQueueService:
    def test_enqueue_idempotent_and_requeue(self, tmp_path, monkeypatch):
        db_url = f"sqlite:///{tmp_path/'q.db'}"
        monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
        reset_cell_provision_runtime()
        try:
            engine = create_engine(db_url)
            CellProvisionBase.metadata.create_all(engine)
            sess = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
            first = cp_service.enqueue(sess, "acme")
            assert first.created is True
            again = cp_service.enqueue(sess, "acme")
            assert again.created is False and again.row.id == first.row.id
            # failed 复位重入队（不另建行；error 清空）
            db2 = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
            row = db2.get(CellProvisionRequest, first.row.id)
            row.state = "failed"
            row.error = "boom"
            db2.commit()
            db2.close()
            third = cp_service.enqueue(sess, "acme")
            assert third.created is False and third.requeued is True
            sess.refresh(third.row)
            assert third.row.state == "queued" and third.row.error is None
            sess.close()
            engine.dispose()
        finally:
            reset_cell_provision_runtime()

    def test_claim_next_is_atomic_exactly_once(self, tmp_path, monkeypatch):
        """sqlite 同库并发领取：同 id 只被领一次（exactly-once 裁决在 UPDATE）。"""
        db_url = f"sqlite:///{tmp_path/'q.db'}"
        monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
        reset_cell_provision_runtime()
        try:
            engine = create_engine(db_url)
            CellProvisionBase.metadata.create_all(engine)
            sess = sessionmaker(bind=engine)()
            for name in ("c1", "c2", "c3"):
                sess.add(CellProvisionRequest(customer_id=name, state="queued", steps="[]"))
            sess.commit()
            sess.close()
            engine.dispose()

            # 共享引擎（file 库）多连接并发；领到的 id 汇总后逐一判重
            def _worker() -> list[int]:
                from fd_wire_api.cell_provision.db import new_session

                claimed: list[int] = []
                session = new_session()
                try:
                    for _ in range(10):
                        row = cp_service.claim_next(session)
                        if row is None:
                            break
                        claimed.append(row.id)
                finally:
                    session.close()
                return claimed

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: _worker(), range(4)))
            all_ids = [rid for chunk in results for rid in chunk]
            assert sorted(all_ids) == [1, 2, 3]  # 三行各被领一次、无人重复领
            # 状态全部 running
            eng = create_engine(db_url)
            sess = sessionmaker(bind=eng)()
            states = {
                r.id: r.state for r in sess.query(CellProvisionRequest).all()
            }
            sess.close()
            eng.dispose()
            assert set(states.values()) == {"running"}
        finally:
            reset_cell_provision_runtime()

    def test_complete_upserts_endpoint_and_fail_records(self, cp_env):
        db = cp_env.sf()
        try:
            row = _enqueue(cp_env, "acme")
            claimed = cp_service.claim_next(db)
            assert claimed is not None and claimed.id == row.id
            done = cp_service.complete(
                db, row.id, endpoint_url="https://mcp.example/fd-wire-cell-acme/mcp",
                steps=[{"step": "provision", "ok": True}],
            )
            assert done.state == "done"
            # done 落 endpoints 表（幂等 upsert：二次回写同 URL 不炸不重复）
            endpoint = (
                db.query(CustomerCellEndpoint)
                .filter_by(customer_id="acme")
                .one()
            )
            assert endpoint.endpoint_url == "https://mcp.example/fd-wire-cell-acme/mcp"
            assert endpoint.state == "done"
            with pytest.raises(cp_service.InvalidState):
                cp_service.complete(db, row.id, endpoint_url="https://again/mcp")
            # failed：error + step 痕留档
            row2 = _enqueue(cp_env, "beta")
            cp_service.claim_next(db)
            failed = cp_service.fail(
                db, row2.id, error="socat exited 1",
                steps=[{"step": "relay", "ok": False, "error": "socat exited 1"}],
            )
            assert failed.state == "failed" and failed.error == "socat exited 1"
            assert "relay" in failed.steps
            row2 = db.get(CellProvisionRequest, row2.id, populate_existing=True)
            assert row2.state == "failed"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 内部端点（领任务 / 回写 / token 闸 / 204）
# ---------------------------------------------------------------------------


class TestInternalEndpoints:
    def test_next_204_when_empty(self, cp_env):
        resp = cp_env.client.post("/internal/cell-provision/next", headers=INTERNAL)
        assert resp.status_code == 204

    def test_token_gate_401(self, cp_env):
        for headers in ({}, {"X-Internal-Token": "wrong"}):
            resp = cp_env.client.post("/internal/cell-provision/next", headers=headers)
            assert resp.status_code == 401

    def test_claim_then_done_roundtrip(self, cp_env):
        _enqueue(cp_env, "acme")
        resp = cp_env.client.post("/internal/cell-provision/next", headers=INTERNAL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["customer_id"] == "acme" and body["state"] == "running"
        # 再领一次 → 204（同一任务不重复下发）
        assert (
            cp_env.client.post("/internal/cell-provision/next", headers=INTERNAL).status_code
            == 204
        )
        # 回写 done → endpoints 表落行 + request done
        resp = cp_env.client.post(
            f"/internal/cell-provision/{body['id']}/result",
            headers=INTERNAL,
            json={
                "status": "done",
                "endpoint_url": "https://mcp.example/fd-wire-cell-acme/mcp",
                "steps": [{"step": "provision"}],
            },
        )
        assert resp.status_code == 200 and resp.json()["state"] == "done"
        db = cp_env.sf()
        try:
            assert (
                db.query(CustomerCellEndpoint).filter_by(customer_id="acme").count() == 1
            )
            assert db.get(CellProvisionRequest, body["id"]).state == "done"
        finally:
            db.close()

    def test_result_failed_keeps_trace(self, cp_env):
        row = _enqueue(cp_env, "beta")
        cp_env.client.post("/internal/cell-provision/next", headers=INTERNAL)
        resp = cp_env.client.post(
            f"/internal/cell-provision/{row.id}/result",
            headers=INTERNAL,
            json={
                "status": "failed",
                "error": "grant: identity missing",
                "steps": [{"step": "grant", "ok": False}],
            },
        )
        assert resp.status_code == 200 and resp.json()["state"] == "failed"
        db = cp_env.sf()
        try:
            stored = db.get(CellProvisionRequest, row.id)
            assert stored.error == "grant: identity missing"
            assert "grant" in stored.steps
        finally:
            db.close()

    def test_result_validation_matrix(self, cp_env):
        row = _enqueue(cp_env, "gamma")
        base = f"/internal/cell-provision/{row.id}/result"
        # 未知 id 404
        assert (
            cp_env.client.post(
                "/internal/cell-provision/999/result",
                headers=INTERNAL,
                json={"status": "failed", "error": "x"},
            ).status_code
            == 404
        )
        # done 缺 endpoint_url 422 / failed 缺 error 422 / 未知 status 422
        assert (
            cp_env.client.post(
                base, headers=INTERNAL, json={"status": "done"}
            ).status_code
            == 422
        )
        assert (
            cp_env.client.post(
                base, headers=INTERNAL, json={"status": "failed"}
            ).status_code
            == 422
        )
        assert (
            cp_env.client.post(
                base, headers=INTERNAL, json={"status": "weird", "error": "x"}
            ).status_code
            == 422
        )
        # 领走后回写两次：第二次 409（状态闸——仅 running 可回写）
        cp_env.client.post("/internal/cell-provision/next", headers=INTERNAL)
        assert (
            cp_env.client.post(
                base, headers=INTERNAL, json={"status": "failed", "error": "first"}
            ).status_code
            == 200
        )
        assert (
            cp_env.client.post(
                base, headers=INTERNAL, json={"status": "failed", "error": "again"}
            ).status_code
            == 409
        )


# ---------------------------------------------------------------------------
# 升档挂点（admin PATCH → enqueue + 邮件）与 admin 状态页
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_env(tmp_path, monkeypatch):
    """admin/billing 与 cell_provision 同一临时文件库（改档挂点用例）。"""
    db_url = f"sqlite:///{tmp_path/'admin.db'}"
    monkeypatch.setenv("FD_WIRE_ADMIN_DATABASE_URL", db_url)
    monkeypatch.setenv("FD_WIRE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("FD_WIRE_ADMIN_BEARER_GATE", "off")
    monkeypatch.setenv("FD_WIRE_CELL_PROVISION_DATABASE_URL", db_url)
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    from fd_wire_api.admin import reset_admin_runtime
    from fd_wire_api.admin.db import AdminBase
    from fd_wire_api.billing import Base as BillingBase, seed_plans

    reset_admin_runtime()
    reset_cell_provision_runtime()
    engine = create_engine(
        db_url, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AdminBase.metadata.create_all(engine)
    BillingBase.metadata.create_all(engine)
    CellProvisionBase.metadata.create_all(engine)
    seed_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with seed_session() as sess:
        seed_plans(sess)
        sess.commit()

    from fd_wire_api.admin.db import get_db as get_admin_db

    app.dependency_overrides[get_admin_db] = override_session(seed_session)
    app.dependency_overrides[get_cell_provision_db] = override_session(seed_session)
    monkeypatch.setattr(cp_notifications, "_deliver_async", lambda task: task())
    client = TestClient(app)
    yield SimpleNamespace(client=client, sf=seed_session, engine=engine, db_url=db_url)
    app.dependency_overrides.pop(get_admin_db, None)
    app.dependency_overrides.pop(get_cell_provision_db, None)
    engine.dispose()
    reset_admin_runtime()
    reset_cell_provision_runtime()


def override_session(factory):
    """请求级会话覆盖（语义对齐 admin.db.get_db：成功提交、异常回滚）。"""

    def _override():
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return _override


def _make_customer(admin_env, name: str = "acme", plan_code: str = "free") -> int:
    resp = admin_env.client.post(
        "/internal/admin/customers",
        headers=AUTH,
        json={"name": name, "plan_code": plan_code, "sub2api_user_id": f"u-{name}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestUpgradeHook:
    def test_patch_to_paid_enqueues_and_notifies(self, admin_env, smtp_env):
        customer_id = _make_customer(admin_env)
        resp = admin_env.client.patch(
            f"/internal/admin/customers/{customer_id}",
            headers=AUTH,
            json={"plan_code": "basic"},
        )
        assert resp.status_code == 200
        db = admin_env.sf()
        try:
            rows = db.query(CellProvisionRequest).filter_by(customer_id="acme").all()
            assert len(rows) == 1 and rows[0].state == "queued"
        finally:
            db.close()
        # 运营邮件（support@ 缺省收件人）
        assert len(smtp_env.calls) == 1
        call = smtp_env.calls[0]
        assert call["to_addr"] == "support@finddatatech.cloud"
        assert "acme" in call["subject"]

    def test_patch_to_free_never_enqueues(self, admin_env, smtp_env):
        customer_id = _make_customer(admin_env)
        resp = admin_env.client.patch(
            f"/internal/admin/customers/{customer_id}",
            headers=AUTH,
            json={"plan_code": "standard"},
        )
        assert resp.status_code == 200
        # 升到 standard 再降回 free：standard 入队一次，free 不入队
        resp = admin_env.client.patch(
            f"/internal/admin/customers/{customer_id}",
            headers=AUTH,
            json={"plan_code": "free"},
        )
        assert resp.status_code == 200
        db = admin_env.sf()
        try:
            assert db.query(CellProvisionRequest).count() == 1
        finally:
            db.close()
        assert len(smtp_env.calls) == 1

    def test_repatch_same_customer_is_idempotent(self, admin_env, smtp_env):
        customer_id = _make_customer(admin_env)
        for _ in range(2):
            resp = admin_env.client.patch(
                f"/internal/admin/customers/{customer_id}",
                headers=AUTH,
                json={"plan_code": "basic"},
            )
            assert resp.status_code == 200
        db = admin_env.sf()
        try:
            assert db.query(CellProvisionRequest).count() == 1
        finally:
            db.close()
        # 幂等跳过不发新邮件
        assert len(smtp_env.calls) == 1


class TestAdminListPage:
    def test_list_readonly(self, admin_env):
        db = admin_env.sf()
        try:
            db.add_all(
                [
                    CellProvisionRequest(customer_id="a1", state="done", steps="[]"),
                    CellProvisionRequest(
                        customer_id="a2", state="failed", error="boom",
                        steps='[{"step": "relay"}]',
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()
        resp = admin_env.client.get(
            "/internal/admin/cell-provision-requests", headers=AUTH
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert [it["customer_id"] for it in body["items"]] == ["a2", "a1"]  # 新→旧
        failed = body["items"][0]
        assert failed["error"] == "boom"
        assert failed["steps"] == [{"step": "relay"}]  # JSON 文本解析成数组


# ---------------------------------------------------------------------------
# 迁移链（0053 钉 0052；六个在途钉子文件不动，链断言由本文件承载）
# ---------------------------------------------------------------------------


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0053_chain():
    versions = (
        Path(__file__).resolve().parent.parent / "alembic" / "versions"
    )
    migration = _load_migration(versions / "20261106_0053_cell_provision.py")
    assert migration.revision == "0053_cell_provision"
    # versions/ 已有并发会话的 0052 文件 → down_revision 指它（报告口径）
    assert migration.down_revision == "0052_bench_ontology_snapshots"
    # 单头：0052 只被 0053 引用（无第二分支）
    revisers = []
    for file in versions.glob("*.py"):
        mod = _load_migration(file)
        if getattr(mod, "down_revision", None) == "0052_bench_ontology_snapshots":
            revisers.append(mod.revision)
    assert revisers == ["0053_cell_provision"]
