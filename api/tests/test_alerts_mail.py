"""告警邮件通道测试（close-wire-implementation-gaps 1.4）。

覆盖（全 mock，绝不真打网/真发信）：
- 触发发信内容：主题含规则名与状态，正文含归约值/阈值/时间/产物链接；
  收件=notifyEmails 列表一封多发（逗号连接）
- 冷却内不重复、超冷却再发（cooldownMinutes 两渠道共用）；恢复沿同样发信
- 邮件失败 notify_failed（detail 尾注渠道=email）且 webhook 照发不阻塞；
  反向 webhook 失败同样标注渠道且邮件照发（前缀格式沿既有口径）
- 同轮双渠道都失败：各落一条 notify_failed（渠道维度去重不互吞）
- SMTP env 缺失：邮件跳过 + 结构化告警一次（进程生命周期一次，第二次
  不再告警），webhook 渠道行为不变
- 未配任何渠道零外呼
- 契约校验矩阵：notifyEmails 形态/上限/缺省/normalize 视图

fixture 沿 test_alerts.py（内存两库 + 脚本化引擎 + tick_once 直调），出站
webhook/邮件都 monkeypatch 模级出口（deliver_webhook / deliver_mail）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

# 导入期把默认引擎指向内存库（与 test_alerts 同款纪律；不导入 app——
# 本文件只走 tick_once 直调面，零 HTTP）
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase
from fd_wire_api.alerts import evaluator as evaluator_mod
from fd_wire_api.alerts.models import AlertEvent, AlertState
from fd_wire_api.alerts.spec_validate import (
    normalize_alert_spec,
    validate_alert_spec,
)
from fd_wire_api.artifacts.models import Artifact, ArtifactVersion
from fd_wire_api.datasets.db import DatasetsBase
from fd_wire_api.datasets.models import Dataset

#: 可注入的固定时钟基准（naive UTC，与 test_alerts 同基准）
T0 = datetime(2026, 10, 12, 12, 0, 0)


# ------------------------------------------------------------------ fakes


class ScriptedEngine:
    """cell 档引擎假件：记录调用、脚本化返回（test_alerts 同款裁剪）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = {
            "columns": [{"name": "amount", "type": "REAL"}],
            "rows": [{"amount": 150.0}],
            "total": 1,
        }

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        result = self.result or {}
        return {
            "columns": [dict(card) for card in result.get("columns", [])],
            "rows": [dict(row) if isinstance(row, dict) else row
                     for row in result.get("rows", [])],
            "total": len(result.get("rows", [])),
        }


class WebhookRecorder:
    """出站 webhook 假件：记录调用；fail=True 模拟投递不可达。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, url: str, payload: dict) -> None:
        self.calls.append({"url": url, "payload": dict(payload)})
        if self.fail:
            raise RuntimeError("webhook unreachable (simulated)")


class MailRecorder:
    """出站邮件假件（test_demands_notifications 的 RecordingSmtp 同款）：记录
    deliver_mail 调用形状；可注入失败。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, to_addr: str, subject: str, body: str, config: dict) -> None:
        self.calls.append(
            {"to_addr": to_addr, "subject": subject, "body": body, "config": config}
        )
        if self.error is not None:
            raise self.error


# ------------------------------------------------------------- 种子助手


def alert_inner(
    dataset_id: str = "ds-alert",
    *,
    op: str = "gt",
    value: object = 100,
    interval: int = 5,
    cooldown: int | None = None,
    webhook: str | None = None,
    notify_emails: list[str] | None = None,
) -> dict:
    """一份可过契约校验的规则内层（test_alerts 同款，加 notifyEmails）。"""
    inner: dict = {
        "query": {"dataset_id": dataset_id},
        "aggregate": {"column": "amount", "func": "sum"},
        "condition": {"op": op, "value": value},
        "intervalMinutes": interval,
    }
    if cooldown is not None:
        inner["cooldownMinutes"] = cooldown
    if webhook is not None:
        inner["webhookUrl"] = webhook
    if notify_emails is not None:
        inner["notifyEmails"] = notify_emails
    return inner


def seed_alert_rule(
    ai,
    *,
    customer_id: str = "cust-001",
    inner: dict | None = None,
    title: str = "金额阈值告警",
) -> int:
    """直插一条 published alert_rule 产物+版本行（test_alerts 同款路径）。"""
    if inner is None:
        inner = alert_inner()
    artifact = Artifact(
        customer_id=customer_id,
        kind="alert_rule",
        title=title,
        status="published",
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


def seed_cell_dataset(ds, *, dataset_id: str = "ds-alert") -> None:
    """cell 档数据集登记（查询经引擎假件）。"""
    ds.add(
        Dataset(
            id=dataset_id,
            customer_id="cust-001",
            key="alert_src",
            name="alert_src 数据源",
            source_kind="connector",
            storage_tier="cell",
            columns_json=[{"name": "amount", "type": "REAL"}],
        )
    )
    ds.commit()


def events_of(ai, artifact_id: int) -> list[dict]:
    stmt = (
        AlertEvent.__table__.select()
        .where(AlertEvent.artifact_id == artifact_id)
        .order_by(AlertEvent.id)
    )
    return [dict(row) for row in ai.execute(stmt).mappings().all()]


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def env():
    """ai/datasets 两库同引擎内存库 + 假引擎（tick_once 直调，零 HTTP）。"""
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

    yield SimpleNamespace(ai=ai, ds=ds, fake=fake, factory=factory)


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch):
    """每用例给足最小 SMTP 配置；邮件出站拦截 + 未配置告警旗标复位。

    ``_mail_skip_warned`` 是进程级全局（告警一次语义）——每用例复位保证
    断言确定性（monkeypatch 退出自动还原）。
    """
    monkeypatch.setenv("SMTP_HOST", "smtp.test.internal")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_FROM", "wire-notify@finddatatech.cloud")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("SMTP_STARTTLS", raising=False)
    monkeypatch.delenv("SMTP_USE_SSL", raising=False)
    monkeypatch.delenv("WIRE_ENTRY_URL", raising=False)  # 产物链接降级面确定性
    monkeypatch.setattr(evaluator_mod, "_mail_skip_warned", False)


@pytest.fixture()
def mail(monkeypatch):
    """出站邮件拦截（模块级函数 monkeypatch——绝不真发信）。"""
    recorder = MailRecorder()
    monkeypatch.setattr(evaluator_mod, "deliver_mail", recorder)
    return recorder


@pytest.fixture()
def webhook(monkeypatch):
    """出站 webhook 拦截（test_alerts 同款）。"""
    recorder = WebhookRecorder()
    monkeypatch.setattr(evaluator_mod, "deliver_webhook", recorder)
    return recorder


def tick(env, moment):
    """直调 tick_once（注入 ai/datasets 会话与假引擎工厂）。"""
    return evaluator_mod.tick_once(
        now=moment, session=env.ai, datasets_session=env.ds,
        engine_factory=env.factory,
    )


# --------------------------------------------- 1. 触发发信内容


def test_firing_sends_alert_mail_content(env, mail) -> None:
    """触发沿发信：主题含规则名与状态，正文含归约值/阈值/时间；配置 env 解析。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai, inner=alert_inner(notify_emails=["ops@cust.example"]))

    stats = tick(env, T0)
    assert stats["firing"] == 1 and stats["notified"] == 1
    assert len(mail.calls) == 1
    sent = mail.calls[0]
    # 收件 = notifyEmails 列表（单收件人原样）
    assert sent["to_addr"] == "ops@cust.example"
    # 主题含规则名与状态
    assert "金额阈值告警" in sent["subject"]
    assert "firing" in sent["subject"]
    # 正文含归约值/阈值/时间（webhook 同款 iso8601 UTC）与产物号
    assert "150.0" in sent["body"]
    assert "gt" in sent["body"] and "100.0" in sent["body"]
    assert "2026-10-12T12:00:00+00:00" in sent["body"]
    assert f"#{rule}" in sent["body"]
    # 配置从 env 解析（共享 mail.smtp_config 出口）
    assert sent["config"]["host"] == "smtp.test.internal"
    assert sent["config"]["port"] == 2525
    assert sent["config"]["from_addr"] == "wire-notify@finddatatech.cloud"
    # 投递成功推进冷却基准
    state = env.ai.get(AlertState, rule)
    assert state.last_notified_at == T0
    # firing 事件照记（通知失败与否不影响事件面）
    assert [r["kind"] for r in events_of(env.ai, rule)] == ["firing"]


def test_notify_emails_multiple_recipients_single_message(env, mail) -> None:
    """多收件人：一封多发（逗号连接），不逐人拆信。"""
    seed_cell_dataset(env.ds)
    seed_alert_rule(
        env.ai,
        inner=alert_inner(notify_emails=["a@x.example", "b@y.example"]),
    )

    tick(env, T0)
    assert len(mail.calls) == 1
    assert mail.calls[0]["to_addr"] == "a@x.example, b@y.example"


def test_mail_body_artifact_link_uses_entry_url(env, mail, monkeypatch) -> None:
    """产物链接：WIRE_ENTRY_URL 在则拼控制台锚点；未配置如实降级。"""
    seed_cell_dataset(env.ds)
    seed_alert_rule(env.ai, inner=alert_inner(notify_emails=["ops@cust.example"]))
    monkeypatch.setenv("WIRE_ENTRY_URL", "https://wire.example.com")
    tick(env, T0)
    assert "https://wire.example.com/#/console/artifacts" in mail.calls[0]["body"]

    monkeypatch.delenv("WIRE_ENTRY_URL")
    seed_alert_rule(env.ai, inner=alert_inner(notify_emails=["ops2@cust.example"]))
    tick(env, T0 + timedelta(minutes=5))  # 冷却内不再发信 → 用新规则触发
    # 第二条规则的信（上一条 T0 已发；此 tick 只评新规则）
    degrade = [c for c in mail.calls if "（平台未配置入口地址" in c["body"]]
    assert degrade and "请经控制台查看" in degrade[-1]["body"]


# --------------------------------------------- 2. 冷却与恢复沿


def test_cooldown_no_duplicate_then_renotify(env, mail) -> None:
    """持续 firing：冷却内不重复发信、超冷却再发（cooldownMinutes 共用语义）。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(notify_emails=["ops@cust.example"], cooldown=10),
    )

    def firing_rows(amount: float) -> None:
        env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                           "rows": [{"amount": amount}]}

    firing_rows(150.0)
    tick(env, T0)                                    # 首评 firing：发信 1
    firing_rows(160.0)
    stats = tick(env, T0 + timedelta(minutes=5))     # 同态、冷却内：零发信
    assert stats["notified"] == 0
    firing_rows(170.0)
    stats = tick(env, T0 + timedelta(minutes=10))    # 冷却到点：再发
    assert stats["notified"] == 1

    assert len(mail.calls) == 2
    assert "170.0" in mail.calls[1]["body"]
    # 同态不重复记事件
    assert [r["kind"] for r in events_of(env.ai, rule)] == ["firing"]
    state = env.ai.get(AlertState, rule)
    assert state.last_notified_at == T0 + timedelta(minutes=10)


def test_resolved_edge_also_mails(env, mail) -> None:
    """恢复沿同样发信：ok→firing→resolved 两封信，主题状态随沿变化。"""
    seed_cell_dataset(env.ds)
    seed_alert_rule(env.ai, inner=alert_inner(notify_emails=["ops@cust.example"]))

    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                       "rows": [{"amount": 10.0}]}
    tick(env, T0)                                    # 首评 ok 静默
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                       "rows": [{"amount": 200.0}]}
    tick(env, T0 + timedelta(minutes=5))             # firing
    env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                       "rows": [{"amount": 20.0}]}
    tick(env, T0 + timedelta(minutes=10))            # resolved

    assert [c["subject"].split()[-1] for c in mail.calls] == ["firing", "resolved"]


# --------------------------------------------- 3. 失败不阻塞 + 渠道标注


def test_mail_failure_notify_failed_webhook_unaffected(env, mail, webhook) -> None:
    """邮件失败：notify_failed 尾注渠道=email，webhook 照发不阻塞。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(
            webhook="https://hooks.example/a",
            notify_emails=["ops@cust.example"],
        ),
    )
    mail.error = ConnectionRefusedError("smtp.test.internal 拒绝连接")  # 注入失败

    stats = tick(env, T0)
    # webhook 渠道照发（双渠道互不阻塞）
    assert len(webhook.calls) == 1
    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["firing", "notify_failed"]
    failed = rows[1]
    assert "ConnectionRefusedError" in failed["detail"]
    assert failed["detail"].endswith("（渠道=email）")
    assert failed["value"] == 150.0 and failed["threshold"] == 100.0
    # webhook 成功 → 冷却基准照推进（任一渠道成功即算通知）
    state = env.ai.get(AlertState, rule)
    assert state.last_notified_at == T0
    assert stats["firing"] == 1


def test_webhook_failure_notify_failed_mail_unaffected(env, mail, webhook) -> None:
    """webhook 失败：notify_failed 尾注渠道=webhook（前缀格式沿既有口径），
    邮件照发不阻塞。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(
            webhook="https://hooks.example/a",
            notify_emails=["ops@cust.example"],
        ),
    )
    webhook.fail = True

    tick(env, T0)
    assert len(mail.calls) == 1  # 邮件照发
    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["firing", "notify_failed"]
    failed = rows[1]
    # 前缀沿既有 webhook 面（既有测试钉住 startswith），渠道以尾注增量标注
    assert failed["detail"].startswith("RuntimeError: webhook unreachable")
    assert failed["detail"].endswith("（渠道=webhook）")
    state = env.ai.get(AlertState, rule)
    assert state.last_notified_at == T0  # 邮件成功推进


def test_both_channels_fail_each_recorded(env, mail, webhook) -> None:
    """同轮双渠道都失败：各落一条 notify_failed（渠道维度去重不互吞）。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(
            webhook="https://hooks.example/a",
            notify_emails=["ops@cust.example"],
        ),
    )
    webhook.fail = True
    mail.error = ValueError("smtp refused (simulated)")

    tick(env, T0)
    assert len(webhook.calls) == 1
    assert len(mail.calls) == 1
    rows = events_of(env.ai, rule)
    assert [r["kind"] for r in rows] == ["firing", "notify_failed", "notify_failed"]
    channels = sorted(r["detail"].rsplit("（渠道=", 1)[-1].rstrip("）")
                      for r in rows if r["kind"] == "notify_failed")
    assert channels == ["email", "webhook"]
    state = env.ai.get(AlertState, rule)
    assert state.last_notified_at is None  # 两渠道都失败不算通知


# --------------------------------------------- 4. SMTP env 缺失不静默


def test_smtp_env_missing_skips_and_warns_once(env, mail, webhook, caplog,
                                               monkeypatch) -> None:
    """SMTP env 缺失：邮件跳过 + 结构化告警一次；webhook 渠道行为不变。"""
    # 迁移类测试经 alembic/env.py fileConfig 会把既有 logger 置 disabled=True
    # （disable_existing_loggers 默认开）——全量套件顺序下先复位被测 logger
    # （纯测试环境伪影，test_demands_notifications 同款）。
    target = logging.getLogger("fd_wire_api.alerts.evaluator")
    monkeypatch.setattr(target, "disabled", False)
    monkeypatch.delenv("SMTP_HOST", raising=False)

    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(
        env.ai,
        inner=alert_inner(
            webhook="https://hooks.example/a",
            notify_emails=["ops@cust.example"],
            cooldown=60,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="fd_wire_api.alerts.evaluator"):
        tick(env, T0)                                  # 第一次：告警一次
        assert len(webhook.calls) == 1                 # webhook 照发
        assert mail.calls == []                        # 邮件跳过
        state = env.ai.get(AlertState, rule)
        assert state.state == "firing"
        # 跳过不算失败：无 notify_failed 事件
        assert [r["kind"] for r in events_of(env.ai, rule)] == ["firing"]

        # 冷却到点再通知（第二次邮件尝试）：不再告警
        env.fake.result = {"columns": [{"name": "amount", "type": "REAL"}],
                           "rows": [{"amount": 160.0}]}
        tick(env, T0 + timedelta(minutes=61))
        assert len(webhook.calls) == 2

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    smtp_warnings = [r for r in warnings if "SMTP 未配置" in r.getMessage()]
    assert len(smtp_warnings) == 1  # 进程生命周期只告警一次
    assert "notifyEmails" in smtp_warnings[0].getMessage()
    assert mail.calls == []


def test_smtp_env_missing_warn_once_spans_rules(env, mail, caplog,
                                                monkeypatch) -> None:
    """告警一次语义跨规则：两条 notifyEmails 规则同轮各试邮件，告警仍只一次。"""
    target = logging.getLogger("fd_wire_api.alerts.evaluator")
    monkeypatch.setattr(target, "disabled", False)
    monkeypatch.delenv("SMTP_HOST", raising=False)

    seed_cell_dataset(env.ds)
    seed_alert_rule(env.ai, inner=alert_inner(notify_emails=["a@x.example"]))
    seed_alert_rule(env.ai, inner=alert_inner(notify_emails=["b@y.example"]))

    with caplog.at_level(logging.WARNING, logger="fd_wire_api.alerts.evaluator"):
        stats = tick(env, T0)
    assert stats["firing"] == 2
    smtp_warnings = [r for r in caplog.records if "SMTP 未配置" in r.getMessage()]
    assert len(smtp_warnings) == 1


# --------------------------------------------- 5. 零外呼


def test_no_channels_zero_outbound(env, mail, webhook) -> None:
    """未配任何渠道：状态与事件照记，零外呼（webhook/邮件各零次）。"""
    seed_cell_dataset(env.ds)
    rule = seed_alert_rule(env.ai)  # 无 webhook 无 notifyEmails

    tick(env, T0)
    assert webhook.calls == []
    assert mail.calls == []
    state = env.ai.get(AlertState, rule)
    assert state.state == "firing" and state.last_notified_at is None
    assert [r["kind"] for r in events_of(env.ai, rule)] == ["firing"]


# --------------------------------------------- 6. 契约校验矩阵（notifyEmails）


def test_validate_notify_emails_matrix() -> None:
    """notifyEmails 契约：形态/上限/缺省；文案定位字段。"""
    # 合法：单条 / 多条 / 空数组（缺省空语义）/ 缺省
    assert validate_alert_spec(
        alert_inner(notify_emails=["ops@cust.example"])
    ) is None
    assert validate_alert_spec(
        alert_inner(notify_emails=["a@x.example", "b@y.example"])
    ) is None
    assert validate_alert_spec(alert_inner(notify_emails=[])) is None
    assert validate_alert_spec(alert_inner()) is None
    # 合法边界：恰好 10 条
    ten = [f"p{i}@x.example" for i in range(10)]
    assert validate_alert_spec(alert_inner(notify_emails=ten)) is None

    # 非数组（字符串/对象）
    non_list = alert_inner()
    non_list["notifyEmails"] = "ops@cust.example"
    assert validate_alert_spec(non_list) == (
        "notifyEmails 可省，在则必须是字符串数组（收到 'ops@cust.example'）"
    )

    # 每项邮箱形态：无 @ / 无域名点 / 含空白 / 非字符串
    for bad in ("not-an-email", "a@nodot", "a b@x.example", "a@x .example", 123, None):
        message = validate_alert_spec(alert_inner(notify_emails=[bad]))  # type: ignore[list-item]
        assert message is not None and message.startswith("notifyEmails 每项"), bad

    # 上限：11 条拒，文案带上限值
    eleven = [f"p{i}@x.example" for i in range(11)]
    assert validate_alert_spec(alert_inner(notify_emails=eleven)) == (
        "notifyEmails 最多 10 条（收到 11 条）"
    )


def test_normalize_notify_emails_default_empty() -> None:
    """normalize 视图：notifyEmails 缺省补空列表，在则原样透传。"""
    inner = alert_inner(notify_emails=["ops@cust.example"])
    params = normalize_alert_spec(inner)
    assert params["notify_emails"] == ["ops@cust.example"]

    params_default = normalize_alert_spec(alert_inner())
    assert params_default["notify_emails"] == []
