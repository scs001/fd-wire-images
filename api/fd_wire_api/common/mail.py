"""共享 SMTP 直发设施（close-wire-implementation-gaps 1.1）。

demands 新单通知（``demands/notifications.py``）全链验证过的 smtplib 直发
提为仓内公共面——demands 与 alerts（告警邮件通道）同源调用：env 驱动、
纯文本信、10s 超时、不引入邮件服务依赖、不引入队列。

设置项（env，与仓内 ``os.getenv`` 模式一致）：

- ``SMTP_HOST`` / ``SMTP_PORT``（缺 HOST 即未配置，默认端口 25）；
- ``SMTP_FROM``（缺省 = DEFAULT_FROM_ADDR，见下方鉴权账户约束）；
- ``SMTP_USER`` / ``SMTP_PASSWORD``（可选，配置后 LOGIN）；
- ``SMTP_STARTTLS``（可选，"1"/"true" 时 STARTTLS）；
- ``SMTP_USE_SSL``（可选，"1"/"true" 时隐式 SSL——smtpdm.aliyun.com:465
  生产 DirectMail 姿态，与 CRM chatwoot 同款；SSL 在则忽略 STARTTLS，
  两者都不配即明文）。

测试纪律：调用方全部 mock（monkeypatch 各自的出站出口），绝不发真网。
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

#: 平台默认发件地址（未配 SMTP_FROM 的兜底；demands 运营邮箱同址同源）。
# DirectMail 鉴权账户约束（11-06 生产实弹 436 实锤）：MAIL FROM 必须与
# SMTP 鉴权账户（support@，CRM 同源账号）一致，任意其他发件地址被拒。
DEFAULT_FROM_ADDR = "support@finddatatech.cloud"

#: SMTP 发送超时（秒）——后台线程也不允许无限挂起。
SMTP_TIMEOUT_SECONDS = 10.0

#: 布尔 env 的真值集合（与 demands notifications 原 STARTTLS 口径一致）
_TRUTHY = {"1", "true", "yes", "on"}


def smtp_config() -> dict | None:
    """解析 SMTP 配置；未配置（缺 SMTP_HOST）返回 None。"""
    host = (os.getenv("SMTP_HOST") or "").strip()
    if not host:
        return None
    port = int(os.getenv("SMTP_PORT", "25").strip() or "25")
    return {
        "host": host,
        "port": port,
        "from_addr": (os.getenv("SMTP_FROM") or DEFAULT_FROM_ADDR).strip(),
        "user": (os.getenv("SMTP_USER") or "").strip() or None,
        "password": os.getenv("SMTP_PASSWORD") or "",
        "starttls": (os.getenv("SMTP_STARTTLS") or "").strip().lower()
        in _TRUTHY,
        "use_ssl": (os.getenv("SMTP_USE_SSL") or "").strip().lower()
        in _TRUTHY,
    }


def smtp_send(subject: str, body: str, *, to_addr: str, config: dict) -> None:
    """经 smtplib 直发一封纯文本邮件（隐式 SSL/STARTTLS/登录可选）。

    ``to_addr`` 是单个地址或逗号连接的多个收件人（告警 notifyEmails 列表
    一封多发）；调用方保证 config 来自 :func:`smtp_config`（非 None）。
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["from_addr"]
    message["To"] = to_addr
    message.set_content(body)
    if config.get("use_ssl"):
        with smtplib.SMTP_SSL(
            config["host"], config["port"], timeout=SMTP_TIMEOUT_SECONDS
        ) as smtp:
            if config["user"]:
                smtp.login(config["user"], config["password"])
            smtp.send_message(message)
        return
    with smtplib.SMTP(
        config["host"], config["port"], timeout=SMTP_TIMEOUT_SECONDS
    ) as smtp:
        if config["starttls"]:
            smtp.starttls()
        if config["user"]:
            smtp.login(config["user"], config["password"])
        smtp.send_message(message)
