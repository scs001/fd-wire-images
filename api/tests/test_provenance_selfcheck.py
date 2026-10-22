"""账本完整性自检测试（wire-provenance-ledger 任务 9.1）。

覆盖：健康账本通过；假回执（Merkle 重算不符）定位；区间不衔接
（first_seq 跳号遗漏业务事件）检出；断链检出；只读不变式（前后快照
完全一致）；告警日志接线；selfcheck 路由（404/200/401）；循环起停干净。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from fd_wire_api.app import app as fastapi_app
from fd_wire_api.datasets.db import get_session, reset_datasets_runtime
from fd_wire_api.provenance import (
    envelope,
    init_provenance_db,
    ledger,
    reset_provenance_runtime,
    selfcheck,
)
from fd_wire_api.provenance.selfcheck import reset_selfcheck_runtime
from fd_wire_api.provenance.models import LedgerEvent

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_A = "sub-cust-001"
CUSTOMER_A = bearer(mint_id_token(SUB_A))


@pytest.fixture()
def db(monkeypatch):
    """隔离运行时（内存库）+ 直连 Session；清 selfcheck 模块态。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    reset_datasets_runtime()
    reset_provenance_runtime()
    selfcheck.reset_selfcheck_runtime()
    init_provenance_db()
    session = get_session()
    yield session
    session.rollback()
    session.close()
    selfcheck.reset_selfcheck_runtime()
    reset_datasets_runtime()
    reset_provenance_runtime()


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    """隔离运行时 + 全量 app TestClient（与 test_provenance_routes 同款）。"""
    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FD_WIRE_DATASETS_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("FD_WIRE_DATASETS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("FD_WIRE_PROVENANCE_INGEST_TOKEN", raising=False)
    monkeypatch.delenv(
        selfcheck.SELFCHECK_INTERVAL_ENV, raising=False
    )
    install_fake_jwks(monkeypatch)
    make_console_env(tmp_path, monkeypatch, customers=[(SUB_A, "cust-001")])
    reset_datasets_runtime()
    reset_provenance_runtime()
    selfcheck.reset_selfcheck_runtime()
    with TestClient(fastapi_app) as client:
        yield SimpleNamespace(client=client, tmp_path=tmp_path, monkeypatch=monkeypatch)
    selfcheck.reset_selfcheck_runtime()
    reset_provenance_runtime()
    reset_datasets_runtime()


# ------------------------------------------------------------- 造数 / 工具


def _event(db, ledger_name: str = "cust-001", n: int = 1):
    return ledger.append_event(
        db,
        ledger=ledger_name,
        type="dataset.correction.added",
        actor={"kind": "customer", "id": ledger_name, "sig": None},
        subject={"kind": "dataset", "key": "sales_2026"},
        payload={"op": "update", "anchor": f"pk:{n}", "n": n},
    )


def _receipt(
    db,
    ledger_name: str,
    rows,
    *,
    epoch: int = 1,
    first_seq: int | None = None,
    last_seq: int | None = None,
    merkle_override: str | None = None,
):
    """构造封存回执（区间 Merkle 根默认按真实事件重算）。"""
    first = rows[0].seq if first_seq is None else first_seq
    last = rows[-1].seq if last_seq is None else last_seq
    root = merkle_override or envelope.merkle_root(
        [
            envelope.merkle_leaf(r.payload_hash)
            for r in rows
            if first <= r.seq <= last
        ]
    )
    return ledger.append_event(
        db,
        ledger=ledger_name,
        type="ledger.epoch.sealed",
        actor={"kind": "platform", "id": "sealer"},
        subject={"kind": "ledger", "id": ledger_name},
        payload={
            "epoch": epoch,
            "first_seq": first,
            "last_seq": last,
            "merkle_root": root,
            "backend": "customer",
            "external_ref": f"epoch-{epoch}",
            "ts_external": "2026-10-07T01:00:00Z",
            "algo": "sha256",
        },
    )


def _snapshot(db) -> list[tuple]:
    """全部账本行全列快照（只读不变式比对用）。"""
    return sorted(
        tuple(str(getattr(r, c)) for c in (
            "id", "ledger", "seq", "type", "ts_platform", "actor", "subject",
            "payload", "payload_hash", "nonce", "prev_hash", "algo",
            "event_hash", "created_at",
        ))
        for r in db.execute(select(LedgerEvent)).scalars()
    )


# ------------------------------------------------------------- run_selfcheck


def test_healthy_ledger_passes(db) -> None:
    """健康账本（事件 + 合法回执，两 epoch 衔接）→ ok=True 零破损。"""
    first_epoch = [_event(db, n=i) for i in (1, 2, 3)]
    _receipt(db, "cust-001", first_epoch)
    _event(db, n=4)  # 未封存尾部（如实滞后，不算异常）
    db.commit()

    report = selfcheck.run_selfcheck(db)
    assert report.ok is True
    assert report.checked_ledgers == 1
    assert report.broken == []
    assert report.receipts_checked == 1
    assert report.receipts_bad == 0
    assert report is selfcheck.get_last_report()


def test_healthy_two_epochs_chained(db) -> None:
    """两 epoch 首尾衔接（回执占一 seq）→ 通过（衔接规则不吃回执 seq）。"""
    epoch1 = [_event(db, n=i) for i in (1, 2, 3)]
    _receipt(db, "cust-001", epoch1)
    epoch2 = [_event(db, n=i) for i in (4, 5)]
    _receipt(db, "cust-001", epoch2, epoch=2)
    db.commit()

    report = selfcheck.run_selfcheck(db)
    assert report.ok is True, report.broken
    assert report.receipts_checked == 2 and report.receipts_bad == 0


def test_fake_receipt_merkle_root_detected(db) -> None:
    """假回执（声称的 merkle_root 与区间重算不符）→ receipts_bad≥1、定位。"""
    rows = [_event(db, n=i) for i in (1, 2, 3)]
    fake = _receipt(db, "cust-001", rows, merkle_override="f" * 64)
    db.commit()

    report = selfcheck.run_selfcheck(db)
    assert report.ok is False
    assert report.receipts_checked == 1
    assert report.receipts_bad == 1
    assert len(report.broken) == 1
    entry = report.broken[0]
    assert entry.ledger == "cust-001"
    assert entry.at == fake.seq  # 定位到回执本身（seq=4）
    assert "merkle_root" in entry.reason


def test_interval_gap_detected(db) -> None:
    """区间不衔接（first_seq 跳号遗漏业务事件）→ 检出（Merkle 本身吻合）。"""
    epoch1 = [_event(db, n=i) for i in (1, 2, 3)]
    _receipt(db, "cust-001", epoch1)
    _event(db, n=4)
    fifth = _event(db, n=5)
    # 第二条回执声称 [6,6]：Merkle 根按真实事件重算吻合（不走根比对
    # 分支），但业务事件 4（seq=5）被跳过——纯衔接遗漏，只有区间构成
    # 检查能发现。
    _receipt(
        db, "cust-001", [fifth], epoch=2, first_seq=fifth.seq, last_seq=fifth.seq
    )
    db.commit()

    report = selfcheck.run_selfcheck(db)
    assert report.ok is False
    assert report.receipts_bad == 1  # 第二条回执坏，第一条健康
    entry = report.broken[0]
    assert entry.at == 7  # 第二条回执 seq=7
    assert "遗漏" in entry.reason
    assert "merkle_root" not in entry.reason  # 纯衔接问题，非根不符


def test_first_epoch_head_missing_detected(db) -> None:
    """首个封存区间未自 seq 1 起（头部遗漏）→ 检出。"""
    rows = [_event(db, n=i) for i in (1, 2, 3)]
    # 声称 [2,3]：根按事件 2/3 重算吻合，但事件 1 未被封存覆盖
    _receipt(db, "cust-001", rows[1:], first_seq=2, last_seq=3)
    db.commit()

    report = selfcheck.run_selfcheck(db)
    assert report.ok is False
    assert "头部" in report.broken[0].reason


def test_epoch_not_increasing_detected(db) -> None:
    """epoch 不递增（回写两条同号回执）→ 检出。"""
    epoch1 = [_event(db, n=i) for i in (1, 2)]
    _receipt(db, "cust-001", epoch1)
    epoch2 = [_event(db, n=i) for i in (3, 4)]
    _receipt(db, "cust-001", epoch2, epoch=1)  # 重号
    db.commit()

    report = selfcheck.run_selfcheck(db)
    assert report.ok is False
    assert any("epoch 不递增" in b.reason for b in report.broken)


def test_chain_break_detected(db) -> None:
    """断链（伪造 prev_hash 直插行）→ 链核验分支定位 broken_at。"""
    _event(db, n=1)
    _event(db, n=2)
    db.commit()
    forged_prev = "sha256:" + "f" * 64
    env = envelope.build_envelope(
        ledger="cust-001",
        seq=3,
        type="dataset.correction.added",
        actor={"kind": "engine", "id": "cell-1"},
        subject={"kind": "dataset", "key": "sales_2026"},
        payload={"op": "insert", "anchor": "ins:1"},
        event_id="evt_forged_selfcheck",
        ts_platform="2026-10-07T00:00:00Z",
        nonce="00" * 16,
        prev_hash=forged_prev,
    )
    db.execute(
        text(
            "INSERT INTO ledger_events (id, ledger, seq, type, ts_platform, actor,"
            " subject, payload, payload_hash, nonce, prev_hash, algo, event_hash,"
            " created_at) VALUES (:id, :ledger, :seq, :type, :ts, :actor, :subject,"
            " :payload, :ph, :nonce, :prev, :algo, :eh, :created)"
        ),
        {
            "id": env["id"],
            "ledger": env["ledger"],
            "seq": env["seq"],
            "type": env["type"],
            "ts": env["ts_platform"],
            "actor": json.dumps(env["actor"]),
            "subject": json.dumps(env["subject"]),
            "payload": json.dumps(env["payload"]),
            "ph": env["payload_hash"],
            "nonce": env["nonce"],
            "prev": forged_prev,
            "algo": env["algo"],
            "eh": envelope.event_hash(env),
            "created": "2026-10-07 00:00:00",
        },
    )
    db.commit()

    report = selfcheck.run_selfcheck(db)
    assert report.ok is False
    entry = report.broken[0]
    assert entry.at == 3
    assert "prev_hash" in entry.reason


def test_ledgers_isolated_and_readonly(db) -> None:
    """健康/受损账本互不影响 + **只读**：跑前后账本行数与内容完全一致。"""
    healthy = [_event(db, "cust-001", n=i) for i in (1, 2)]
    _receipt(db, "cust-001", healthy)
    broken_rows = [_event(db, "cust-002", n=i) for i in (1, 2)]
    _receipt(db, "cust-002", broken_rows, merkle_override="e" * 64)
    db.commit()

    before = _snapshot(db)
    report = selfcheck.run_selfcheck(db)
    after = _snapshot(db)

    assert report.ok is False
    assert report.checked_ledgers == 2
    assert [b.ledger for b in report.broken] == ["cust-002"]
    assert after == before  # 快照逐字段一致：零 append/UPDATE/DELETE


def test_alert_logged_on_broken(db, caplog, monkeypatch) -> None:
    """发现异常 → 既有日志通道 error（日志聚合拾取）。

    注：迁移类测试经 ``alembic/env.py`` 的 ``fileConfig`` 会把此前已创建
    的 logger 置 ``disabled=True``（``disable_existing_loggers`` 默认开），
    全量套件顺序下须先复位被测 logger 才能观测告警——纯测试环境伪影，
    生产无进程内 fileConfig。
    """
    target = logging.getLogger("fd_wire_api.provenance")
    monkeypatch.setattr(target, "disabled", False)

    rows = [_event(db, n=1)]
    _receipt(db, "cust-001", rows, merkle_override="e" * 64)
    db.commit()

    with caplog.at_level(logging.ERROR, logger="fd_wire_api.provenance"):
        selfcheck.run_selfcheck(db)
    assert any("自检发现异常" in rec.getMessage() for rec in caplog.records)


def test_empty_db_passes(db) -> None:
    report = selfcheck.run_selfcheck(db)
    assert report.ok is True
    assert report.checked_ledgers == 0 and report.receipts_checked == 0


# ------------------------------------------------------------- 路由


def test_selfcheck_routes(client_env) -> None:
    """GET 未运行 404 → POST run 200 报告 → GET 回放同一报告；401 分支。"""
    client = client_env.client
    # app lifespan 接线后 start_selfcheck_loop() 启动即跑一次并填充 last_report；
    # 本测试验证"未运行 404"契约本身，先复位模块态隔离。
    # 竞态消除：启动首跑在 to_thread 里可能仍在途——等它落定（last_report
    # 非 None）再复位，否则复位后线程补写会把 404 变 200（偶发假失败；
    # 纯测试确定性修复，生产语义不变）。
    _deadline = time.monotonic() + 5.0
    while selfcheck.get_last_report() is None and time.monotonic() < _deadline:
        time.sleep(0.01)
    reset_selfcheck_runtime()

    no_header = client.get("/api/v1/provenance/selfcheck")
    assert no_header.status_code == 401
    no_header_post = client.post("/api/v1/provenance/selfcheck/run")
    assert no_header_post.status_code == 401

    fresh = client.get("/api/v1/provenance/selfcheck", headers=CUSTOMER_A)
    assert fresh.status_code == 404

    ran = client.post("/api/v1/provenance/selfcheck/run", headers=CUSTOMER_A)
    assert ran.status_code == 200, ran.text
    report = ran.json()
    assert report["ok"] is True
    assert report["checked_ledgers"] == 0
    assert report["broken"] == []
    assert report["receipts_checked"] == 0
    assert report["receipts_bad"] == 0
    assert report["ran_at"]

    cached = client.get("/api/v1/provenance/selfcheck", headers=CUSTOMER_A)
    assert cached.status_code == 200
    assert cached.json()["ran_at"] == report["ran_at"]


# ------------------------------------------------------------- 周期循环


def test_loop_start_stop_clean(db, monkeypatch) -> None:
    """循环：启动即跑、幂等启动、stop 干净（任务取消、无悬挂）。"""
    monkeypatch.setenv(selfcheck.SELFCHECK_INTERVAL_ENV, "0.05")

    async def scenario() -> None:
        task = selfcheck.start_selfcheck_loop()
        assert selfcheck.start_selfcheck_loop() is task  # 幂等：不双开
        deadline = time.monotonic() + 5.0
        while selfcheck.get_last_report() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        stopped = selfcheck.stop_selfcheck_loop()
        await asyncio.gather(stopped, return_exceptions=True)
        assert stopped.cancelled()  # 取消收尾，非异常退出

    asyncio.run(scenario())

    report = selfcheck.get_last_report()
    assert report is not None
    assert report.ok is True  # 空账本通过；循环内自检真实执行
    assert selfcheck._loop_task is None
    # stop 后不再跑：ran_at 不再前进
    frozen = report.ran_at
    time.sleep(0.15)
    assert selfcheck.get_last_report().ran_at == frozen


def test_loop_survives_db_failure(db, monkeypatch) -> None:
    """单次自检抛异常：循环不死（记录后继续下个周期）。"""
    monkeypatch.setenv(selfcheck.SELFCHECK_INTERVAL_ENV, "0.05")
    calls = {"n": 0}

    def boom() -> selfcheck.SelfcheckReport:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("模拟库故障")
        report = selfcheck.SelfcheckReport(checked_ledgers=0, ok=True)
        selfcheck.last_report = report
        return report

    monkeypatch.setattr(selfcheck, "_run_once", boom)

    async def scenario() -> None:
        selfcheck.start_selfcheck_loop()
        deadline = time.monotonic() + 5.0
        while calls["n"] < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        stopped = selfcheck.stop_selfcheck_loop()
        await asyncio.gather(stopped, return_exceptions=True)

    asyncio.run(scenario())
    assert calls["n"] >= 2  # 第一次炸了，第二个周期照常执行
