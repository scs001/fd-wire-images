"""Demand ORM 模型（表 ``demands``）。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .statemachine import DemandStatus, utc_now_iso


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Demand(Base):
    """数据需求单：客户提交（submitted）→ 处理（in_progress）→ 交付（delivered）。

    wire-ux-consolidation 任务 3.2 起表单收窄为四入参：需求描述
    （description → intent 全文，title 存截断摘要）、联系邮箱
    （contact_email，必填）、公司名（company，选填）、目录锚点
    （indicator code → linked_indicators，选填、服务端原样存储）。
    wire-demand-generation-orchestration 1.1 起加合规门两入参：
    数据源地址（source_urls，公开源 URL 列表）、公开数据声明
    （public_data_asserted，必勾=True）——旧单两列均为 None。
    entities_hint / frequency_expect 保留为旧列（新表单不再写入）。
    """

    __tablename__ = "demands"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 业务编码 DE-YYYYMMDD-XX（按日递增两位序号）
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    #: 归属企业（客户侧按此隔离列表与详情）
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 摘要（列表展示用；新表单由需求描述截断生成，旧单为手填标题）
    title: Mapped[str] = mapped_column(String(200))
    #: 需求描述全文（自由文本，按将来对话第一条消息的形态设计）
    intent: Mapped[str] = mapped_column(Text)
    #: 联系邮箱（新表单必填；旧单无值，可空）
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    #: 公司名（选填）
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: 实体线索（自由 JSON：地域/行业/关键词等；旧表单遗留，新表单不再写入）
    entities_hint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 期望更新频率（如 daily/weekly；旧表单遗留，新表单不再写入）
    frequency_expect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 目录预填的 indicator code 列表（提交时携带的锚点，服务端原样存储）
    linked_indicators: Mapped[list] = mapped_column(JSON, default=list)
    #: 数据源地址（wire-demand-generation-orchestration 1.1：公开源 URL 列表，
    #: 服务端硬校验后原样存储；新表单必填，旧单无值=None）
    source_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: 公开数据声明（客户确认所求数据为公开可获取——合规门勾选项；
    #: 新表单必勾=True，旧单无值=None）
    public_data_asserted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 当前状态（DemandStatus 值）
    status: Mapped[str] = mapped_column(
        String(32), default=DemandStatus.submitted.value, index=True
    )
    #: 状态时间线（append-only）：[{status, at, by, note}]
    status_history: Mapped[list] = mapped_column(JSON, default=list)
    #: AI 预评估结论概要（客户可见）
    evaluation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: AI 预评估内部全文（verdict/confidence/reasons/hints/flags——客户不可见）
    evaluation_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 拒绝理由（必留档，客户可见）
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ---- 报价字段（旧八态工作流遗留，随旧单保留可查；三态机不再写入）----
    #: 报价金额（元，展示用；旧 quoted 态写入）
    quote_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    #: 报价金额（微元整数，账内权威单位；与 billing.money 约定一致）
    quote_amount_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 报价说明
    quote_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 报价有效期（旧单展示用）
    quote_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    #: 报价操作人（内部留档，客户不可见）
    quote_operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 报价历史（append-only）：[{action, at, operator, amount_micro, note, valid_until}]
    quote_history: Mapped[list] = mapped_column(JSON, default=list)
    #: 流水线事件（append-only，任务 6.5）：[{type: pipeline_run|gate_result, at, by, ...}]
    pipeline_events: Mapped[list] = mapped_column(JSON, default=list)
    #: 交付事件（append-only，任务 6.6）：
    #: [{type: scope_created|scope_create_failed|publish_authorization, at, by, ...}]
    delivery_events: Mapped[list] = mapped_column(JSON, default=list)
    #: 私有 scope 交付状态：None=未发起 / "ok"=scope 已建 / "pending"=失败待重试
    scope_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 已建客户私有 scope 名（``demand-<code>``）
    scope_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )

    def append_history(self, *, status: DemandStatus, by: str, note: str | None) -> None:
        """只追加一条时间线记录——绝不改写既有条目（审计要求）。"""
        entry = {"status": status.value, "at": utc_now_iso(), "by": by, "note": note}
        history = list(self.status_history or [])
        history.append(entry)
        self.status_history = history

    def append_quote(
        self,
        *,
        operator: str,
        amount_micro: int,
        note: str | None = None,
        valid_until: datetime | None = None,
    ) -> None:
        """只追加一条报价留档（append-only）：操作人/金额/有效期必在案。"""
        entry = {
            "action": "quote",
            "at": utc_now_iso(),
            "operator": operator,
            "amount_micro": int(amount_micro),
            "note": note,
            "valid_until": valid_until.isoformat() if valid_until else None,
        }
        history = list(self.quote_history or [])
        history.append(entry)
        self.quote_history = history

    def append_pipeline_event(self, event: dict) -> None:
        """只追加一条流水线事件（append-only，任务 6.5）：时间戳在此盖。"""
        entry = {"at": utc_now_iso(), **event}
        events = list(self.pipeline_events or [])
        events.append(entry)
        self.pipeline_events = events

    def append_delivery_event(self, event: dict) -> None:
        """只追加一条交付事件（append-only，任务 6.6）：时间戳在此盖。"""
        entry = {"at": utc_now_iso(), **event}
        events = list(self.delivery_events or [])
        events.append(entry)
        self.delivery_events = events

    # ------------------------------------------------ 计算视图（响应组装用）

    @property
    def delivery(self) -> dict:
        """客户可见交付视图：私有 scope 交付状态 + 授权转公开状态。

        只含客户应见字段——内部留档（delivery_events 全文、失败错误串、
        grant 明细）绝不在此出现。
        """
        authorized: bool | None = None
        for event in self.delivery_events or []:
            if event.get("type") == "publish_authorization":
                authorized = bool(event.get("granted"))
        return {
            "scope_name": self.scope_name,
            "scope_status": self.scope_status,
            "publish_authorized": authorized,
        }

    @property
    def gate(self) -> dict:
        """内部 gate 门禁视图：是否有过通过记录 + 最近一次 gate 结果。"""
        events = list(self.pipeline_events or [])
        gate_results = [e for e in events if e.get("type") == "gate_result"]
        pipeline_runs = [e for e in events if e.get("type") == "pipeline_run"]
        return {
            "passed": any(e.get("passed") is True for e in gate_results),
            "attempts": len(gate_results),
            "pipeline_runs": len(pipeline_runs),
            "latest": gate_results[-1] if gate_results else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<Demand {self.code} customer={self.customer_id} status={self.status}>"
