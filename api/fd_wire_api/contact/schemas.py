"""contact 面的请求模型（add-contact-intake 任务 1.2）。

公开提交端点的入参故意**不带长度/必填约束**——校验在 service 层手工做，
超界/形态非法统一 400 如实定位字段（FastAPI pydantic 自动校验给 422，
与 spec「SHALL 400」不符）；字段类型畸形（如 name 传数字）属滥用面，
走框架标准 422 如实即可。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ContactIntakeIn(BaseModel):
    """公开商留表单入参（无认证公开面；同源调用，无 CORS 放开）。

    ``website`` 是 honeypot 机器人闸字段——真人在页面上不可见不可填
    （DOM 中隐藏），非空即机器人，400 拒绝不落库且不提示是 honeypot。
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    email: str | None = None
    company: str | None = None
    message: str | None = None
    # honeypot：机器人闸（service 层非空即 400，零落库）
    website: str | None = None


__all__ = ["ContactIntakeIn"]
