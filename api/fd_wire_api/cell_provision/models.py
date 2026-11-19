"""cell 开通自助化的 ORM 模型（add-self-serve-cell-provision 任务 1.1/2.1，
Alembic 0053）。

两表落平台库（与 contact/admin 同库）：

- ``customer_cell_endpoints``：客户 cell 入口注册表（自助配给链回写）——
  ``ai.executor.resolve_cell_endpoint`` 的 DB 兜底源（env map 之后的
  第二级）。``customer_id`` 唯一（幂等 upsert；与 AI 侧解析键同口径，
  即 ``billing_customers.name``）；``endpoint_url`` 必须是 registry 入口
  形态（计量归集在 registry 审计侧，容器直连地址会让 AI 探索绕过计量门）。
- ``cell_provision_requests``：配给任务队列（cheap1 cell-manager runner
  领取执行）。``customer_id`` 幂等唯一（升档重触发不重复建行；failed 行
  重入队=原行复位）；``steps`` 为 JSON 文本（执行步骤留痕，runner 回写；
  失败时承载 step 痕供运营排查）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import CellProvisionBase

#: 配给请求状态词表（queued 队列头 | running 被领走 | done 配给完成 |
#: failed 任一步失败——回退人工链）
REQUEST_STATES = ("queued", "running", "done", "failed")

#: 注册表行的状态词表（done=入口可用；首期只有 done 一种落库形态）
ENDPOINT_STATES = ("done",)


def utc_now_naive() -> datetime:
    """naive-UTC 当前时间（列形态与 contact/xingyan/packs 同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CustomerCellEndpoint(CellProvisionBase):
    """一条客户 cell 入口注册（DB 兜底源；customer_id 唯一）。"""

    __tablename__ = "customer_cell_endpoints"
    __table_args__ = (
        UniqueConstraint("customer_id", name="uq_customer_cell_endpoints_customer_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 客户标识（AI 侧解析键=billing_customers.name；唯一=幂等 upsert 维度）
    customer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    #: cell 引擎入口 URL（registry 入口形态 <registry>/<path>/mcp）
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: done=入口可用（首期唯一落库形态；预留禁用/重建扩展位）
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="done")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<CustomerCellEndpoint {self.customer_id} {self.state}>"


class CellProvisionRequest(CellProvisionBase):
    """一条 cell 配给请求（任务队列行；customer_id 幂等唯一）。"""

    __tablename__ = "cell_provision_requests"
    __table_args__ = (
        # 幂等键：同客户至多一条在途/历史请求行（failed 复位重入队，不另建行）
        UniqueConstraint("customer_id", name="uq_cell_provision_requests_customer_id"),
        # 领任务扫描维度（队头=最小 id 的 queued 行）
        Index("ix_cell_provision_requests_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 客户标识（幂等键；与 cell-manager 的 customer_id / AI 解析键同口径）
    customer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    #: queued | running | done | failed
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    #: 执行步骤留痕（JSON 文本：[{step, at, detail?}, ...]；runner 回写）
    steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: 失败原因（failed 时运营排查入口；done/queued 为空）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<CellProvisionRequest {self.id} {self.customer_id} {self.state}>"


__all__ = [
    "ENDPOINT_STATES",
    "REQUEST_STATES",
    "CellProvisionRequest",
    "CustomerCellEndpoint",
    "utc_now_naive",
]
