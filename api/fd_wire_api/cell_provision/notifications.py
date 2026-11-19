"""cell 配给请求的运营邮件通知（add-self-serve-cell-provision 任务 2.2）。

升档入队后向运营邮箱发提醒（fire-and-forget 后台线程，沿
``contact/notifications.py`` 的全链模式——通知与入队解耦，``notify_...``
自身绝不向上抛异常）。SMTP 设施复用共享模块 :mod:`fd_wire_api.common.mail`
（只读复用，不改其行为）。

收件人（support@ 同址运营邮箱）：
- env ``CELL_PROVISION_NOTIFY_EMAIL`` 显式覆盖；
- 缺省 = :data:`fd_wire_api.common.mail.DEFAULT_FROM_ADDR`
  （= support@finddatatech.cloud，b353b36 发件人钉鉴权账户口径）。

SMTP 未配置时显式告警一次（进程级 once，沿 contact 通知同款口径）；
投递失败仅日志留痕，绝不影响入队动作本身。测试纪律：调用方全部 mock
（monkeypatch :func:`_smtp_send` 或 :func:`_deliver_async`），绝不发真网。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from ..common import mail
from .models import CellProvisionRequest

logger = logging.getLogger(__name__)

#: 运营通知收件人缺省（support@ 同址；CELL_PROVISION_NOTIFY_EMAIL 可覆盖）。
DEFAULT_NOTIFY_TO = mail.DEFAULT_FROM_ADDR


def _smtp_config() -> dict | None:
    """解析 SMTP 配置（共享模块薄委托）；未配置（缺 SMTP_HOST）返回 None。"""
    return mail.smtp_config()


def render_notification(req: CellProvisionRequest) -> tuple[str, str]:
    """渲染通知邮件（纯函数）：主题带客户，正文含状态与错误（如有）。"""
    subject = f"[柏讯·cell 配给] {req.customer_id} 已入队（{req.state}）"
    body = (
        "客户升档触发 cell 自动配给，请在后台 provisions 分区关注执行状态。\n"
        "\n"
        "—— 配给请求 ——\n"
        f"客户：{req.customer_id}\n"
        f"请求 ID：{req.id}\n"
        f"状态：{req.state}\n"
        f"入队时间：{req.created_at.isoformat() if req.created_at else '（未知）'}\n"
        + (
            f"失败原因：{req.error}\n（任一步失败已回退人工链——cell 开户配方见 "
            "docs/paas-wire-line-install-gate-and-fleet-audit-requirements.md §七）\n"
            if req.error
            else ""
        )
    )
    return subject, body


def _smtp_send(subject: str, body: str, *, to_addr: str, config: dict) -> None:
    """经 smtplib 直发一封纯文本邮件（共享模块薄委托；测试拦截点）。"""
    mail.smtp_send(subject, body, to_addr=to_addr, config=config)


# SMTP 未配置的结构化告警只发一次（进程生命周期一次，不逐请求刷屏）——
# 沿 contact/notifications.py 的 _mail_skip_warned 口径。
_mail_skip_warned = False


def _warn_smtp_unconfigured_once() -> None:
    """配给通知因平台 SMTP env 缺失而跳过时，记一次结构化告警。"""
    global _mail_skip_warned
    if not _mail_skip_warned:
        _mail_skip_warned = True
        logger.warning(
            "cell 配给邮件通知跳过：SMTP 未配置（SMTP_HOST 为空）——配给请求照常"
            "入队，但运营收不到邮件提醒；部署面须补齐 SMTP_* env"
            "（收件人默认 support@ 同址，CELL_PROVISION_NOTIFY_EMAIL 可覆盖）"
        )


def _deliver(req: CellProvisionRequest) -> None:
    """渲染并发送一次通知；任何失败只记 warning，绝不向上抛。"""
    try:
        config = _smtp_config()
        if config is None:
            _warn_smtp_unconfigured_once()
            return
        to_addr = (os.getenv("CELL_PROVISION_NOTIFY_EMAIL") or DEFAULT_NOTIFY_TO).strip()
        subject, body = render_notification(req)
        _smtp_send(subject, body, to_addr=to_addr, config=config)
        logger.info("cell 配给邮件通知已发送：request=%s → %s", req.id, to_addr)
    except Exception as exc:  # 通知失败仅留痕，入队照常
        logger.warning(
            "cell 配给邮件通知失败（不阻塞入队，仅日志留痕）：request=%s 错误=%s",
            getattr(req, "id", "?"),
            exc,
        )


def _deliver_async(task: Callable[[], None]) -> None:
    """把通知投递丢进后台守护线程（默认出口；测试可替换为同步执行）。"""
    threading.Thread(target=task, daemon=True, name="cell-provision-notify").start()


def notify_provision_enqueued(req: CellProvisionRequest) -> None:
    """配给入队成功后的通知入口：异步投递，自身绝不抛异常（解耦入队路径）。"""
    try:
        _deliver_async(lambda: _deliver(req))
    except Exception as exc:  # 极端情况（如线程创建失败）也不拖垮入队
        logger.warning(
            "cell 配给邮件通知未能投递（不阻塞入队）：request=%s 错误=%s",
            getattr(req, "id", "?"),
            exc,
        )
