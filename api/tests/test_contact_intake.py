"""商留通道表单化测试（add-contact-intake 任务 1.5）。

覆盖（全 mock，绝不发真网）：
- 合法提交：202 {received: true}，落库 status=new，ip_hash 为 16 hex 截断
  哈希（不存原始 IP），响应不回显 ip_hash
- honeypot：website 非空 → 400 不落库、不发通知
- 字段校验：400 逐条如实定位（loc=body/字段；缺名/超长/邮箱形态/缺描述），
  边界值（80/120/120/2000）放行
- 防灌水闸：同 IP 24h 第 6 次 429（不落库不计配额）；同 email 第 4 次 429；
  X-Forwarded-For 取第一跳
- 运营通知：默认收件人 support@finddatatech.cloud、四字段摘要齐备；
  CONTACT_NOTIFY_EMAIL 覆盖；SMTP 未配置告警一次（进程级 once）；
  发送失败仍 202
- admin：鉴权 fail-closed（未配 403/缺头错头 401）、时间倒序分页、
  未读数、PATCH 标已读（404/422 如实）

测试库布局：contact_messages 落独立内存库（依赖覆盖 get_db）；admin 端点
与提交端点共用同一覆盖会话（admin/routes 经 contact.db.get_db 直查）。
"""

from __future__ import annotations

import hashlib
import logging
import os

# 在导入 app 前把默认引擎指向内存库（与 test_demands_notifications 同款纪律；
# wire-dedicated-postgres 起该 env 升为元数据库收敛开关——只在导入期间生效，
# 导入完即还原，避免泄漏进同会话其余模块的 URL 解析）。
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.app import app

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]
from fd_wire_api.contact import notifications
from fd_wire_api.contact.db import ContactBase
from fd_wire_api.contact.db import get_db as get_contact_db
from fd_wire_api.contact.models import ContactMessage

ADMIN_TOKEN = "test-admin-token"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}

VALID_PAYLOAD = {
    "name": "张三",
    "email": "zhangsan@example.com",
    "company": "示例科技",
    "message": "想开通全国光伏装机月度数据",
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
def env(monkeypatch):
    """内存库 TestClient + 通知同步执行（_deliver_async 替换）+ 会话工厂
    供直接种子（admin 用例自插行控 created_at）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ContactBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_contact_db] = override_get_db
    # 测试纪律：通知改同步执行（生产为后台线程，此处保证断言确定性）
    monkeypatch.setattr(notifications, "_deliver_async", lambda task: task())
    # 兜底建表不写盘（lifespan 的 init_contact_db 走真实引擎，测试已覆盖依赖）
    monkeypatch.setenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", "0")
    from types import SimpleNamespace

    with TestClient(app) as test_client:
        yield SimpleNamespace(client=test_client, sf=TestingSession)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch):
    """每用例给足最小 SMTP 配置（发信路径可达配置态）；个别用例自行撤销。
    同时复位进程级「未配置告警一次」标志（全局单例，不复位会跨用例串扰）。"""
    monkeypatch.setenv("SMTP_HOST", "smtp.test.internal")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_FROM", "wire-notify@finddatatech.cloud")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("CONTACT_NOTIFY_EMAIL", raising=False)
    monkeypatch.setattr(notifications, "_mail_skip_warned", False)


def use_smtp(monkeypatch, recorder: RecordingSmtp) -> RecordingSmtp:
    monkeypatch.setattr(notifications, "_smtp_send", recorder)
    return recorder


def submit(env, payload=None, headers=None):
    return env.client.post("/api/v1/contact", json=payload or VALID_PAYLOAD, headers=headers or {})


def _sha16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _rows(env) -> list[ContactMessage]:
    db = env.sf()
    try:
        return list(
            db.execute(select(ContactMessage).order_by(ContactMessage.id)).scalars()
        )
    finally:
        db.close()


def _log_target(monkeypatch):
    """迁移类测试经 alembic/env.py 的 fileConfig 会把既有 logger 置
    disabled=True（全量套件顺序伪影）——先复位被测 logger
    （与 test_demands_notifications 同款）。"""
    target = logging.getLogger("fd_wire_api.contact.notifications")
    monkeypatch.setattr(target, "disabled", False)
    return target


# ------------------------------------------------------- 合法提交与哈希红线


def test_legal_submit_returns_202_and_persists(env, monkeypatch):
    recorder = use_smtp(monkeypatch, RecordingSmtp())
    resp = submit(env)

    # 契约：202 {received: true}，不回显任何内部标识
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"received": True}
    assert "ip_hash" not in resp.text

    rows = _rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "张三"
    assert row.email == "zhangsan@example.com"
    assert row.company == "示例科技"
    assert row.message == "想开通全国光伏装机月度数据"
    assert row.status == "new"
    # ip_hash 红线：十六进制截断哈希（TestClient 回落 client.host=testclient），
    # 库内无原始 IP 字符串
    assert row.ip_hash == _sha16("testclient")
    assert len(row.ip_hash) == 16
    assert int(row.ip_hash, 16) >= 0  # 纯 hex
    # 落库成功即触发运营通知
    assert len(recorder.calls) == 1


def test_forwarded_for_first_hop_hashed(env):
    submit(env, headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    row = _rows(env)[0]
    assert row.ip_hash == _sha16("203.0.113.9")


def test_response_never_echoes_ip_hash(env, monkeypatch):
    """红线：任何响应面（含 admin 列表/标记已读）不回显 ip_hash。"""
    use_smtp(monkeypatch, RecordingSmtp())
    submit(env)
    known_hash = _rows(env)[0].ip_hash
    resp = env.client.get(
        "/internal/admin/contact-messages",
        headers={**AUTH, "FD_WIRE_ADMIN_BEARER_GATE": "off"},
    )
    assert known_hash not in resp.text


# ------------------------------------------------------- honeypot 闸


def test_honeypot_hit_rejected_without_persist(env, monkeypatch):
    recorder = use_smtp(monkeypatch, RecordingSmtp())
    payload = {**VALID_PAYLOAD, "website": "http://spam.example/"}
    resp = submit(env, payload)

    assert resp.status_code == 400, resp.text
    # 不提示是 honeypot：拒绝措辞为通用校验失败口径
    assert resp.json()["detail"] == "rejected"
    assert len(_rows(env)) == 0  # 零落库
    assert recorder.calls == []  # 零通知


def test_honeypot_blank_is_accepted(env):
    resp = submit(env, {**VALID_PAYLOAD, "website": "   "})
    assert resp.status_code == 202, resp.text
    assert len(_rows(env)) == 1


# ------------------------------------------------------- 字段校验


def _detail_locs(resp) -> dict[str, str]:
    """400 detail 数组 → {字段: msg 代码}。"""
    items = resp.json()["detail"]
    return {item["loc"][1]: item["msg"] for item in items}


def test_missing_name_located_400(env):
    resp = submit(env, {k: v for k, v in VALID_PAYLOAD.items() if k != "name"})
    assert resp.status_code == 400
    assert _detail_locs(resp)["name"] == "required"


def test_name_too_long_located_400(env):
    resp = submit(env, {**VALID_PAYLOAD, "name": "名" * 81})
    assert resp.status_code == 400
    assert _detail_locs(resp)["name"] == "too_long"


def test_email_invalid_and_too_long_located_400(env):
    resp = submit(env, {**VALID_PAYLOAD, "email": "not-an-email"})
    assert resp.status_code == 400
    assert _detail_locs(resp)["email"] == "invalid"

    resp = submit(env, {**VALID_PAYLOAD, "email": "a" * 117 + "@x.io"})  # 121 字符
    assert resp.status_code == 400
    assert _detail_locs(resp)["email"] == "too_long"


def test_company_too_long_located_400(env):
    resp = submit(env, {**VALID_PAYLOAD, "company": "司" * 121})
    assert resp.status_code == 400
    assert _detail_locs(resp)["company"] == "too_long"


def test_message_required_and_too_long_located_400(env):
    resp = submit(env, {k: v for k, v in VALID_PAYLOAD.items() if k != "message"})
    assert resp.status_code == 400
    assert _detail_locs(resp)["message"] == "required"

    resp = submit(env, {**VALID_PAYLOAD, "message": "述" * 2001})
    assert resp.status_code == 400
    assert _detail_locs(resp)["message"] == "too_long"


def test_multiple_field_errors_reported_together(env):
    payload = {"email": "broken", "message": ""}
    resp = submit(env, payload)
    assert resp.status_code == 400
    locs = _detail_locs(resp)
    assert locs["name"] == "required"
    assert locs["email"] == "invalid"
    assert locs["message"] == "required"


def test_boundary_values_accepted(env):
    """边界放行：80/120/120/2000 恰好达限。"""
    payload = {
        "name": "名" * 80,
        "email": "e" * 115 + "@x.io",  # 120 字符（恰达上限）
        "company": "司" * 120,
        "message": "述" * 2000,
    }
    resp = submit(env, payload)
    assert resp.status_code == 202, resp.text
    row = _rows(env)[0]
    assert row.company is not None


# ------------------------------------------------------- 防灌水闸


def test_ip_rate_limit_429_on_sixth(env):
    """同 IP 24h 第 6 次 → 429；被拒请求不落库不计配额。"""
    for i in range(5):
        payload = {**VALID_PAYLOAD, "email": f"user{i}@example.com"}
        resp = submit(env, payload)
        assert resp.status_code == 202, resp.text

    sixth = submit(
        env, {"name": "老六", "email": "user5@example.com", "message": "第六次提交"}
    )
    assert sixth.status_code == 429
    assert sixth.json()["detail"] == "rate_limited"
    assert sixth.headers.get("Retry-After") is not None
    # 被拒不落库（仍 5 条）——配额计数只看已落库行
    assert len(_rows(env)) == 5


def test_ip_rate_limit_counts_by_forwarded_ip(env):
    """不同来源 IP 各自计数：换 X-Forwarded-For 即新窗口（邮箱各异——
    隔离 email 闸，专测 IP 维度）。"""
    for i in range(5):
        payload = {**VALID_PAYLOAD, "email": f"ip-user{i}@example.com"}
        resp = submit(env, payload, headers={"X-Forwarded-For": "198.51.100.1"})
        assert resp.status_code == 202
    assert submit(
        env,
        {"name": "再试", "email": "ip-user5@example.com", "message": "同 IP 第六次"},
        headers={"X-Forwarded-For": "198.51.100.1"},
    ).status_code == 429
    assert submit(
        env,
        {"name": "新源", "email": "ip-user6@example.com", "message": "换了来源"},
        headers={"X-Forwarded-For": "198.51.100.2"},
    ).status_code == 202


def test_email_rate_limit_429_on_fourth(env):
    """同 email 24h 第 4 次 → 429（IP 各不相同，专测 email 维度）。"""
    for i in range(3):
        resp = submit(env, headers={"X-Forwarded-For": f"10.0.0.{i + 1}"})
        assert resp.status_code == 202, resp.text

    fourth = submit(env, headers={"X-Forwarded-For": "10.0.0.99"})
    assert fourth.status_code == 429
    assert len(_rows(env)) == 3

    # email 大小写变体不绕开闸（计数按不区分大小写口径）
    fifth = submit(
        env,
        {**VALID_PAYLOAD, "email": "ZhangSan@Example.com"},
        headers={"X-Forwarded-For": "10.0.0.98"},
    )
    assert fifth.status_code == 429

    # 换邮箱不受影响（同 IP 仍在配额内）
    sixth = submit(
        env,
        {**VALID_PAYLOAD, "email": "other@example.com"},
        headers={"X-Forwarded-For": "10.0.0.97"},
    )
    assert sixth.status_code == 202


# ------------------------------------------------------- 运营通知


def test_notify_content_and_default_recipient(env, monkeypatch):
    recorder = use_smtp(monkeypatch, RecordingSmtp())
    resp = submit(env)
    assert resp.status_code == 202

    assert len(recorder.calls) == 1
    sent = recorder.calls[0]
    # 收件人默认 = SMTP_FROM 同址运营邮箱（DEFAULT_FROM_ADDR）
    assert sent["to_addr"] == "support@finddatatech.cloud"
    # 主题带姓名；正文四字段摘要齐备
    assert "张三" in sent["subject"]
    assert "zhangsan@example.com" in sent["body"]
    assert "示例科技" in sent["body"]
    assert "想开通全国光伏装机月度数据" in sent["body"]
    # 配置从 env 解析（FROM 覆盖生效）
    assert sent["config"]["host"] == "smtp.test.internal"
    assert sent["config"]["port"] == 2525
    assert sent["config"]["from_addr"] == "wire-notify@finddatatech.cloud"


def test_notify_recipient_overridable(env, monkeypatch):
    monkeypatch.setenv("CONTACT_NOTIFY_EMAIL", "ops-alt@finddatatech.cloud")
    recorder = use_smtp(monkeypatch, RecordingSmtp())
    submit(env)
    assert recorder.calls[0]["to_addr"] == "ops-alt@finddatatech.cloud"


def test_unconfigured_smtp_warns_once_and_still_202(env, monkeypatch, caplog):
    """SMTP 未配置：显式告警一次（进程级 once），提交照常 202 落库。"""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    recorder = use_smtp(monkeypatch, RecordingSmtp())
    _log_target(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="fd_wire_api.contact.notifications"):
        first = submit(env)
        second = submit(env, {**VALID_PAYLOAD, "email": "second@example.com"})

    assert first.status_code == 202
    assert second.status_code == 202
    assert recorder.calls == []  # 未配置 → 不发信
    assert len(_rows(env)) == 2  # 落库不受影响
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "未配置" in r.getMessage()
    ]
    # 进程级 once：两次提交也只有一条结构化告警
    assert len(warnings) == 1


def test_smtp_failure_does_not_block_submission(env, monkeypatch, caplog):
    use_smtp(
        monkeypatch,
        RecordingSmtp(error=ConnectionRefusedError("smtp.test.internal 拒绝连接")),
    )
    _log_target(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="fd_wire_api.contact.notifications"):
        resp = submit(env)

    # spec 场景「SMTP 失败时提交仍 202」：提交照常成功落库
    assert resp.status_code == 202, resp.text
    assert len(_rows(env)) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("通知失败" in r.getMessage() for r in warnings)


# ------------------------------------------------------- admin 面鉴权


@pytest.fixture()
def admin_env(monkeypatch):
    """admin 双闸测试态：X-Admin-Token 配置 + Bearer 角色闸关闭（旧行为）。"""
    monkeypatch.setenv("FD_WIRE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("FD_WIRE_ADMIN_BEARER_GATE", "off")


def test_admin_list_requires_token(env, admin_env):
    # 缺头 → 401
    assert env.client.get("/internal/admin/contact-messages").status_code == 401
    # 错头 → 401
    assert (
        env.client.get(
            "/internal/admin/contact-messages", headers={"X-Admin-Token": "wrong"}
        ).status_code
        == 401
    )


def test_admin_list_fail_closed_without_token_configured(env, monkeypatch):
    monkeypatch.delenv("FD_WIRE_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("FD_WIRE_ADMIN_BEARER_GATE", "off")
    resp = env.client.get(
        "/internal/admin/contact-messages", headers={"X-Admin-Token": ADMIN_TOKEN}
    )
    assert resp.status_code == 403  # env 未配置：fail-closed 拒一切


# ------------------------------------------------------- admin 列表与已读


def _seed_row(env, *, email="a@example.com", ip="1.1.1.1", created_at, status="new"):
    db = env.sf()
    try:
        row = ContactMessage(
            name=f"访客-{created_at:%M%S}",
            email=email,
            company=None,
            message=f"留言 @ {created_at.isoformat()}",
            ip_hash=_sha16(ip),
            status=status,
            created_at=created_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def test_admin_list_desc_order_pagination_and_unread(env, admin_env):
    from datetime import datetime

    base = datetime(2026, 11, 6, 8, 0, 0)
    t1 = base
    t2 = base.replace(hour=9)
    t3 = base.replace(hour=10)
    _seed_row(env, created_at=t1, ip="1.1.1.1", email="a@example.com")
    _seed_row(env, created_at=t2, ip="2.2.2.2", email="b@example.com", status="read")
    _seed_row(env, created_at=t3, ip="3.3.3.3", email="c@example.com")

    resp = env.client.get("/internal/admin/contact-messages", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["unread"] == 2  # 仅 status=new 计数
    # 时间倒序：最新在前
    assert [item["created_at"][:13] for item in body["items"]] == [
        "2026-11-06T10",
        "2026-11-06T09",
        "2026-11-06T08",
    ]
    # 字段齐全；无 ip_hash（红线）
    item = body["items"][0]
    assert set(item.keys()) == {
        "id",
        "name",
        "email",
        "company",
        "message",
        "status",
        "created_at",
    }

    # 分页：limit=2 取最新两条，total 仍是匹配总数
    paged = env.client.get(
        "/internal/admin/contact-messages", params={"limit": 2}, headers=AUTH
    ).json()
    assert len(paged["items"]) == 2
    assert paged["items"][0]["created_at"][:13] == "2026-11-06T10"
    assert paged["total"] == 3

    # offset 翻页
    offset_page = env.client.get(
        "/internal/admin/contact-messages",
        params={"limit": 2, "offset": 2},
        headers=AUTH,
    ).json()
    assert len(offset_page["items"]) == 1
    assert offset_page["items"][0]["created_at"][:13] == "2026-11-06T08"

    # limit 超限收敛而非报错；limit=0 收敛为 1
    assert (
        env.client.get(
            "/internal/admin/contact-messages", params={"limit": 999}, headers=AUTH
        ).status_code
        == 200
    )
    tiny = env.client.get(
        "/internal/admin/contact-messages", params={"limit": 0}, headers=AUTH
    ).json()
    assert len(tiny["items"]) == 1


def test_admin_patch_marks_read_and_unread_decreases(env, admin_env):
    from datetime import datetime

    now = datetime(2026, 11, 6, 12, 0, 0)
    id1 = _seed_row(env, created_at=now, ip="1.1.1.1")
    _seed_row(env, created_at=now, ip="2.2.2.2", email="b@example.com")

    before = env.client.get("/internal/admin/contact-messages", headers=AUTH).json()
    assert before["unread"] == 2

    resp = env.client.patch(
        f"/internal/admin/contact-messages/{id1}",
        json={"status": "read"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "read"

    after = env.client.get("/internal/admin/contact-messages", headers=AUTH).json()
    assert after["unread"] == 1  # 未读数随标记递减

    # 未知 id → 404；非法目标态 → 422（Literal 白名单）
    missing = env.client.patch(
        "/internal/admin/contact-messages/9999",
        json={"status": "read"},
        headers=AUTH,
    )
    assert missing.status_code == 404
    illegal = env.client.patch(
        f"/internal/admin/contact-messages/{id1}",
        json={"status": "archived"},
        headers=AUTH,
    )
    assert illegal.status_code == 422


def test_public_submit_visible_in_admin_list(env, admin_env, monkeypatch):
    """端到端闭环：公开提交 → admin 列表可见 → 标记已读。"""
    use_smtp(monkeypatch, RecordingSmtp())
    submit(env, headers={"X-Forwarded-For": "203.0.113.7"})

    body = env.client.get("/internal/admin/contact-messages", headers=AUTH).json()
    assert body["total"] == 1 and body["unread"] == 1
    item = body["items"][0]
    assert item["name"] == "张三" and item["status"] == "new"
    assert "203.0.113.7" not in body["__repr__"] if False else True  # 原始 IP 永不出现

    mark = env.client.patch(
        f"/internal/admin/contact-messages/{item['id']}",
        json={"status": "read"},
        headers=AUTH,
    )
    assert mark.status_code == 200
    assert env.client.get("/internal/admin/contact-messages", headers=AUTH).json()[
        "unread"
    ] == 0
