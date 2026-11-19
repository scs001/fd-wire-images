"""contact 切片（商留通道表单化 add-contact-intake）：``/api/v1/contact``。

公开商留通道：官网「联系开通」CTA 展开的站内表单卡经此落库（公开无认证、
同源、honeypot 闸、ip_hash/email 双维度 24h 防灌水闸），落库后
fire-and-forget 通知运营邮箱（SMTP 共享设施复用）。运营处置面在 admin
第六+一分区 contacts（``/internal/admin/contact-messages``，见 admin/routes）。

纪律：
- 来源 IP 只以 SHA-256 截断哈希落 ``ip_hash``——原始 IP 不落库（红线）；
- ``ip_hash`` 不进任何响应模型（红线：响应不回显内部标识）；
- 通知与提交解耦：SMTP 未配置显式告警一次（进程级 once）、失败仅日志、
  绝不影响 202 返回。
"""

from __future__ import annotations

from .db import ContactBase, get_db, init_db, reset_contact_runtime
from .models import ContactMessage
from .routes import router

__all__ = [
    "ContactBase",
    "ContactMessage",
    "get_db",
    "init_db",
    "reset_contact_runtime",
    "router",
]
