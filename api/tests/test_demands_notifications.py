"""新单邮件通知测试（wire-ux-consolidation 任务 3.3）。

覆盖（全 mock，绝不发真网）：
- 通知送达：submitted 时异步发信 wire@finddatatech.cloud（默认收件人），
  主题带编码、正文含需求摘要+联系方式（邮箱/公司/客户）+目录锚点
- 通知失败不阻塞：SMTP 抛错 → 提交仍 201，失败仅日志留痕（warning）
- 未配置 SMTP（SMTP_HOST 空）→ 记 warning 跳过，提交照常成功
- 异步纪律：默认出口投后台线程；测试经 ``_deliver_async`` 替换为同步执行
- 纯渲染：render_notification 的主题/正文形状（不依赖 FastAPI 栈）
"""

from __future__ import annotations

import logging
import os

# 在导入 app 前把默认引擎指向内存库（与 test_demands_statemachine 同款纪律；
# wire-dedicated-postgres 起该 env 升为元数据库收敛开关——只在导入期间生效，
# 导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]
from fd_wire_api.demands import get_db, notifications
from fd_wire_api.demands.db import Base

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-cust-001"
CUST = bearer(mint_id_token(SUB_CUST))

SUBMIT_PAYLOAD = {
    "description": "需要全国光伏装机月度数据，用于行业研究",
    "contact_email": "pv-research@example.com",
    "company": "某新能源研究院",
    "indicator_code": "IND-PV-001",
    # 合规门两入参（wire-demand-generation-orchestration 1.1 起必填）
    "source_urls": ["https://www.nea.gov.cn/"],
    "public_data_asserted": True,
}


class RecordingSmtp:
    """记录 _smtp_send 调用形状的假出口（可注入失败）。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, subject, body, *, to_addr, config):
        self.calls.append(
            {"subject": subject, "body": body, "to_addr": to_addr, "config": config}
        )
        if self.error is not None:
            raise self.error


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """内存库 TestClient + 通知同步执行（_deliver_async 替换）+ SMTP 出口可注入。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # 测试纪律：通知改同步执行（生产为后台线程，此处保证断言确定性）
    monkeypatch.setattr(notifications, "_deliver_async", lambda task: task())
    install_fake_jwks(monkeypatch)
    make_console_env(tmp_path, monkeypatch, customers=[(SUB_CUST, "cust-001")])
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch):
    """每用例给足最小 SMTP 配置（发信路径可达配置态）；个别用例自行撤销。"""
    monkeypatch.setenv("SMTP_HOST", "smtp.test.internal")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_FROM", "wire-notify@finddatatech.cloud")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("DEMAND_NOTIFY_TO", raising=False)


def use_smtp(monkeypatch, recorder: RecordingSmtp) -> RecordingSmtp:
    monkeypatch.setattr(notifications, "_smtp_send", recorder)
    return recorder


def submit(client, payload=None) -> dict:
    resp = client.post(
        "/api/v1/demands", json=payload or SUBMIT_PAYLOAD, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------- 通知送达


def test_submitted_demand_notifies_operations_mailbox(client, monkeypatch):
    recorder = use_smtp(monkeypatch, RecordingSmtp())
    d = submit(client)

    assert len(recorder.calls) == 1
    sent = recorder.calls[0]
    # 收件人是运营邮箱（默认 wire@finddatatech.cloud）
    assert sent["to_addr"] == "wire@finddatatech.cloud"
    # 主题带需求编码与摘要
    assert d["code"] in sent["subject"]
    assert "光伏装机" in sent["subject"]
    # 正文含需求摘要+全文+联系方式
    assert d["code"] in sent["body"]
    assert "需要全国光伏装机月度数据" in sent["body"]
    assert "pv-research@example.com" in sent["body"]
    assert "某新能源研究院" in sent["body"]
    assert "cust-001" in sent["body"]
    assert "IND-PV-001" in sent["body"]
    # 配置从 env 解析（FROM 覆盖生效）
    assert sent["config"]["host"] == "smtp.test.internal"
    assert sent["config"]["port"] == 2525
    assert sent["config"]["from_addr"] == "wire-notify@finddatatech.cloud"


def test_notify_recipient_overridable_and_only_on_submit(client, monkeypatch):
    monkeypatch.setenv("DEMAND_NOTIFY_TO", "ops-alt@finddatatech.cloud")
    recorder = use_smtp(monkeypatch, RecordingSmtp())

    d = submit(client)
    assert recorder.calls[0]["to_addr"] == "ops-alt@finddatatech.cloud"

    # 后续状态迁移不再发信（只有新单 submitted 通知）
    assert (
        client.post(
            f"/internal/demands/{d['id']}/transition",
            json={"target_status": "in_progress", "by": "ops-001"},
            headers={"X-Internal-Token": "wire-internal-dev"},
        ).status_code
        == 200
    )
    assert len(recorder.calls) == 1


# ------------------------------------------------------- 通知失败不阻塞


def test_smtp_failure_does_not_block_submission(client, monkeypatch, caplog):
    use_smtp(
        monkeypatch,
        RecordingSmtp(error=ConnectionRefusedError("smtp.test.internal 拒绝连接")),
    )
    # 迁移类测试经 alembic/env.py 的 fileConfig 会把既有 logger 置
    # disabled=True（disable_existing_loggers 默认开）——全量套件顺序下
    # 先复位被测 logger（纯测试环境伪影，与 test_provenance_selfcheck 同款）。
    target = logging.getLogger("fd_wire_api.demands.notifications")
    monkeypatch.setattr(target, "disabled", False)
    with caplog.at_level(logging.WARNING, logger="fd_wire_api.demands.notifications"):
        resp = client.post("/api/v1/demands", json=SUBMIT_PAYLOAD, headers=CUST)

    # spec 场景「通知失败不阻塞」：客户提交照常成功，不向客户报错
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "submitted"
    # 失败留痕可查（warning 日志，含需求编码与错误）
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("通知失败" in r.getMessage() for r in warnings)
    assert any(resp.json()["code"] in r.getMessage() for r in warnings)


def test_unconfigured_smtp_skips_with_warning(client, monkeypatch, caplog):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    recorder = use_smtp(monkeypatch, RecordingSmtp())
    target = logging.getLogger("fd_wire_api.demands.notifications")
    monkeypatch.setattr(target, "disabled", False)

    with caplog.at_level(logging.WARNING, logger="fd_wire_api.demands.notifications"):
        resp = client.post("/api/v1/demands", json=SUBMIT_PAYLOAD, headers=CUST)

    assert resp.status_code == 201, resp.text
    assert recorder.calls == []  # 未配置 → 不发信
    assert any("未配置" in r.getMessage() for r in caplog.records)


def test_notify_never_raises_even_in_thread_spawn_failure(client, monkeypatch):
    """投递出口本身炸（线程创建失败）也不拖垮提交路径。"""
    def boom(task):
        raise RuntimeError("线程资源不可用")

    monkeypatch.setattr(notifications, "_deliver_async", boom)
    resp = client.post("/api/v1/demands", json=SUBMIT_PAYLOAD, headers=CUST)
    assert resp.status_code == 201, resp.text


# ------------------------------------------------------- 渲染纯函数


def test_render_notification_shape():
    from types import SimpleNamespace

    demand = SimpleNamespace(
        code="DE-20261010-07",
        title="航运运价周度数据",
        intent="需要主干航线集装箱运价周度数据",
        contact_email="ops@example.com",
        company=None,
        customer_id="cust-009",
        linked_indicators=["IND-FRT-002"],
    )
    subject, body = notifications.render_notification(demand)
    assert subject.startswith("[柏讯·新需求单] DE-20261010-07")
    assert "航运运价周度数据" in subject
    assert "DE-20261010-07" in body
    assert "ops@example.com" in body
    assert "（未留）" in body  # 公司缺省如实展示
    assert "IND-FRT-002" in body
    assert "1 个工作日" in body  # 响应承诺口径
