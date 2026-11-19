"""商留运营邮件通知（add-contact-intake 任务 1.3）。

合法提交落库后向运营邮箱发送通知信（fire-and-forget 后台线程，沿
``demands/notifications.py`` 的全链模式——通知与提交解耦，``notify_...``
自身绝不向上抛异常）。SMTP 设施复用共享模块 :mod:`fd_wire_api.common.mail`
（只读复用，不改其行为）。

收件人（spec：SMTP_FROM 同址运营邮箱）：
- env ``CONTACT_NOTIFY_EMAIL`` 显式覆盖；
- 缺省 = :data:`fd_wire_api.common.mail.DEFAULT_FROM_ADDR`
  （``SMTP_FROM`` 未配时的兜底 = DEFAULT_FROM_ADDR = support@finddatatech.cloud）。

SMTP 未配置（缺 ``SMTP_HOST``）时**显式告警一次**（进程级 once，沿
b381fe8 metering driver 的 ``_poll_skip_warned`` 口径——alerts/evaluator
``_warn_smtp_unconfigured_once`` 同款）：「预期态」必须由告警证实，不由
静默默认。投递失败仅日志留痕（warning），绝不影响 202 返回。

测试纪律：调用方全部 mock（monkeypatch :func:`_smtp_send` 或
:func:`_deliver_async`），绝不发真网。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from ..common import mail
from .models import ContactMessage

logger = logging.getLogger(__name__)

#: 运营通知收件人缺省（SMTP_FROM 同址；CONTACT_NOTIFY_EMAIL 可覆盖）。
DEFAULT_NOTIFY_TO = mail.DEFAULT_FROM_ADDR


def _smtp_config() -> dict | None:
    """解析 SMTP 配置（共享模块薄委托）；未配置（缺 SMTP_HOST）返回 None。"""
    return mail.smtp_config()


def render_notification(msg: ContactMessage) -> tuple[str, str]:
    """渲染通知邮件（纯函数）：主题带姓名，正文含四字段摘要。"""
    subject = f"[柏讯·商留] 新联系提交 {msg.name}"
    body = (
        f"官网「联系开通」表单收到一条新提交，请在后台 contacts 分区处置。\n"
        f"\n"
        f"—— 提交内容 ——\n"
        f"姓名：{msg.name}\n"
        f"邮箱：{msg.email}\n"
        f"公司：{msg.company or '（未留）'}\n"
        f"提交时间：{msg.created_at.isoformat() if msg.created_at else '（未知）'}\n"
        f"\n"
        f"—— 需求描述 ——\n{msg.message}\n"
    )
    return subject, body


def _smtp_send(subject: str, body: str, *, to_addr: str, config: dict) -> None:
    """经 smtplib 直发一封纯文本邮件（共享模块薄委托；测试拦截点）。"""
    mail.smtp_send(subject, body, to_addr=to_addr, config=config)


# SMTP 未配置的结构化告警只发一次（进程生命周期一次，不逐提交刷屏）——
# 沿 b381fe8 metering driver 的 _poll_skip_warned 口径（alerts/evaluator
# _warn_smtp_unconfigured_once 同款拷贝）。
_mail_skip_warned = False


def _warn_smtp_unconfigured_once() -> None:
    """商留通知因平台 SMTP env 缺失而跳过时，记一次结构化告警。"""
    global _mail_skip_warned
    if not _mail_skip_warned:
        _mail_skip_warned = True
        logger.warning(
            "商留邮件通知跳过：SMTP 未配置（SMTP_HOST 为空）——商留提交照常"
            "落库与 202 返回，但运营收不到邮件提醒；部署面须补齐 SMTP_* env"
            "（收件人默认 SMTP_FROM 同址，CONTACT_NOTIFY_EMAIL 可覆盖）"
        )


def _deliver(msg: ContactMessage) -> None:
    """渲染并发送一次通知；任何失败只记 warning，绝不向上抛。"""
    try:
        config = _smtp_config()
        if config is None:
            _warn_smtp_unconfigured_once()
            return
        to_addr = (os.getenv("CONTACT_NOTIFY_EMAIL") or DEFAULT_NOTIFY_TO).strip()
        subject, body = render_notification(msg)
        _smtp_send(subject, body, to_addr=to_addr, config=config)
        logger.info("商留邮件通知已发送：contact=%s → %s", msg.id, to_addr)
    except Exception as exc:  # 通知失败仅留痕，客户提交照常 202
        logger.warning(
            "商留邮件通知失败（不阻塞提交，仅日志留痕）：contact=%s 错误=%s",
            getattr(msg, "id", "?"),
            exc,
        )


def _deliver_async(task: Callable[[], None]) -> None:
    """把通知投递丢进后台守护线程（默认出口；测试可替换为同步执行）。"""
    threading.Thread(target=task, daemon=True, name="contact-notify").start()


def notify_new_contact_message(msg: ContactMessage) -> None:
    """商留落库成功后的通知入口：异步投递，自身绝不抛异常（解耦提交路径）。"""
    try:
        _deliver_async(lambda: _deliver(msg))
    except Exception as exc:  # 极端情况（如线程创建失败）也不拖垮提交
        logger.warning(
            "商留邮件通知未能投递（不阻塞提交）：contact=%s 错误=%s",
            getattr(msg, "id", "?"),
            exc,
        )
