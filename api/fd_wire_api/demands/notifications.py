"""新单邮件通知（wire-ux-consolidation 任务 3.3）。

新需求单进入 ``submitted`` 时向运营邮箱（wire@finddatatech.cloud）发送
通知邮件（需求摘要 + 联系方式）。仓内此前没有邮件设施——本模块即最简
smtplib 直发实现，不引入队列。

纪律（通知与提交解耦，spec 场景「通知失败不阻塞」）：
- 提交路径只投递后台线程，SMTP 往返全部在线程内完成；
- 未配置 SMTP（缺 ``SMTP_HOST``）记 warning 跳过；
- 发送失败（网络/SMTP 拒绝/渲染异常）仅日志留痕（warning），客户提交
  照常成功——``notify_new_demand`` 自身绝不向上抛异常。

设置项（env，与仓内 ``os.getenv`` 模式一致）：
- ``SMTP_HOST`` / ``SMTP_PORT``（缺 HOST 即未配置，默认端口 25）；
- ``SMTP_FROM``（缺省 ``wire@finddatatech.cloud``）；
- ``SMTP_USER`` / ``SMTP_PASSWORD``（可选，配置后 LOGIN）；
- ``SMTP_STARTTLS``（可选，"1"/"true" 时 STARTTLS）；
- ``DEMAND_NOTIFY_TO``（收件人，缺省 wire@finddatatech.cloud）。

测试纪律：全部 mock（monkeypatch :func:`_smtp_send` 或
:func:`_deliver_async`），绝不发真网。
"""

from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from typing import Callable

from .models import Demand
from .statemachine import utc_now_iso

logger = logging.getLogger(__name__)

#: 运营通知收件人（spec：wire@finddatatech.cloud）。
DEFAULT_NOTIFY_TO = "wire@finddatatech.cloud"

#: SMTP 发送超时（秒）——后台线程也不允许无限挂起。
_SMTP_TIMEOUT_SECONDS = 10.0


def _smtp_config() -> dict | None:
    """解析 SMTP 配置；未配置（缺 SMTP_HOST）返回 None。"""
    host = (os.getenv("SMTP_HOST") or "").strip()
    if not host:
        return None
    port = int(os.getenv("SMTP_PORT", "25").strip() or "25")
    return {
        "host": host,
        "port": port,
        "from_addr": (os.getenv("SMTP_FROM") or DEFAULT_NOTIFY_TO).strip(),
        "user": (os.getenv("SMTP_USER") or "").strip() or None,
        "password": os.getenv("SMTP_PASSWORD") or "",
        "starttls": (os.getenv("SMTP_STARTTLS") or "").strip().lower()
        in {"1", "true", "yes", "on"},
    }


def render_notification(demand: Demand) -> tuple[str, str]:
    """渲染通知邮件（纯函数）：主题带编码，正文含需求摘要+联系方式。"""
    subject = f"[柏讯·新需求单] {demand.code} {demand.title}"
    anchor = (
        "、".join(str(c) for c in (demand.linked_indicators or [])) or "（无）"
    )
    body = (
        f"新需求单已提交（submitted），请在 1 个工作日内响应。\n"
        f"\n"
        f"需求编码：{demand.code}\n"
        f"需求摘要：{demand.title}\n"
        f"\n"
        f"—— 需求描述 ——\n{demand.intent}\n"
        f"\n"
        f"—— 联系方式 ——\n"
        f"联系邮箱：{demand.contact_email or '（未留）'}\n"
        f"公司名：{demand.company or '（未留）'}\n"
        f"归属客户：{demand.customer_id}\n"
        f"目录锚点：{anchor}\n"
        f"提交时间：{utc_now_iso()}\n"
    )
    return subject, body


def _smtp_send(subject: str, body: str, *, to_addr: str, config: dict) -> None:
    """经 smtplib 直发一封纯文本邮件（登录/STARTTLS 可选）。"""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["from_addr"]
    message["To"] = to_addr
    message.set_content(body)
    with smtplib.SMTP(
        config["host"], config["port"], timeout=_SMTP_TIMEOUT_SECONDS
    ) as smtp:
        if config["starttls"]:
            smtp.starttls()
        if config["user"]:
            smtp.login(config["user"], config["password"])
        smtp.send_message(message)


def _deliver(demand: Demand) -> None:
    """渲染并发送一次通知；任何失败只记 warning，绝不向上抛。"""
    try:
        config = _smtp_config()
        if config is None:
            logger.warning(
                "新单邮件通知跳过：SMTP 未配置（SMTP_HOST 为空）——"
                "demand=%s 客户提交不受影响",
                demand.code,
            )
            return
        to_addr = (os.getenv("DEMAND_NOTIFY_TO") or DEFAULT_NOTIFY_TO).strip()
        subject, body = render_notification(demand)
        _smtp_send(subject, body, to_addr=to_addr, config=config)
        logger.info("新单邮件通知已发送：demand=%s → %s", demand.code, to_addr)
    except Exception as exc:  # 通知失败仅留痕，客户提交照常成功
        logger.warning(
            "新单邮件通知失败（不阻塞提交，仅日志留痕）：demand=%s 错误=%s",
            getattr(demand, "code", "?"),
            exc,
        )


def _deliver_async(task: Callable[[], None]) -> None:
    """把通知投递丢进后台守护线程（默认出口；测试可替换为同步执行）。"""
    threading.Thread(target=task, daemon=True, name="demand-notify").start()


def notify_new_demand(demand: Demand) -> None:
    """新单 submitted 通知入口：异步投递，自身绝不抛异常（解耦提交路径）。"""
    try:
        _deliver_async(lambda: _deliver(demand))
    except Exception as exc:  # 极端情况（如线程创建失败）也不拖垮提交
        logger.warning(
            "新单邮件通知未能投递（不阻塞提交）：demand=%s 错误=%s",
            getattr(demand, "code", "?"),
            exc,
        )
