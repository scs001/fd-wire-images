"""告警评估持久状态与事件 ORM 模型（wire-alert-evaluation，Alembic 0028）。

- ``alert_states``：每条 alert_rule 产物一行的**持久状态**（artifact_id
  主键 + 唯一）——state ok|firing、last_value（最近一次成功归约值）、
  last_evaluated_at（due 判定驱动列，error 路径同样推进）、last_notified_at
  （冷却再通知的基准）、updated_at。
- ``alert_events``：append-only 事件流（firing/resolved/error/notify_failed
  四类）——带值与阈值；error/notify_failed 的 detail 存原因（≤500 截断）。
  索引 (customer_id, id desc) 与 (artifact_id, id desc) 服务管理端点的
  倒序时间线查询。

两表挂 ``AiBase``（与产物同库同会话面——alert_state 对 artifacts 是同库
外键，artifacts.id 真外键约束成立）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..ai.db import AiBase
from ..ai.models import utc_now_naive
# 注册 FK 目标表进同一 metadata（artifacts.models 不回依赖 alerts——零环）：
# 单独 create_all alerts 两表时 ForeignKey("artifacts.id") 才可解析。
from ..artifacts.models import Artifact  # noqa: F401,TID252

#: 状态机合法值
ALERT_STATES: tuple[str, ...] = ("ok", "firing")
#: 事件类别合法值
ALERT_EVENT_KINDS: tuple[str, ...] = ("firing", "resolved", "error", "notify_failed")
#: 事件 detail 截断上限
EVENT_DETAIL_LIMIT = 500


class AlertState(AiBase):
    """一条 alert_rule 产物的评估持久状态（边缘触发状态机的记忆位）。"""

    __tablename__ = "alert_states"

    #: 产物 id 即主键（一产物一行，天然唯一）
    artifact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("artifacts.id"), primary_key=True, autoincrement=False
    )
    #: 归属企业（客户面隔离；与 artifacts.customer_id 冗余存一份便于直查）
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ok | firing（边缘触发：状态翻转才记事件）
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 最近一次成功归约值（error 路径不覆盖）
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 最近一次评估时刻（naive UTC；due 判定驱动列，含 error 路径）
    last_evaluated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    #: 最近一次通知成功时刻（冷却再通知基准；未通知过为 NULL）
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<AlertState artifact={self.artifact_id} {self.state} value={self.last_value}>"


class AlertEvent(AiBase):
    """告警事件流（append-only：firing/resolved/error/notify_failed）。"""

    __tablename__ = "alert_events"
    __table_args__ = (
        # 管理端点倒序时间线：客户维度 / 产物维度各一 (…, id desc)
        Index("ix_alert_events_customer_id", "customer_id", text("id DESC")),
        Index("ix_alert_events_artifact_id", "artifact_id", text("id DESC")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("artifacts.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: firing | resolved | error | notify_failed
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 归约值（firing/resolved/notify_failed 携带；error 为 NULL）
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 规则阈值快照（condition.value）
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: error/notify_failed 的原因摘要（≤500 字截断）
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<AlertEvent {self.id} artifact={self.artifact_id} {self.kind}>"
