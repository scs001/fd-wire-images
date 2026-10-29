"""客户域动作 ORM 模型（wire-domain-actions 任务 1.2）。

三表挂 ``AiBase``（与动作闸提议/审计同库同引擎——域动作是闸的动态注册
源，迁移 0043）。表语义见迁移 docstring；要点：

- 类型 = 挂实体声明（name 实体内唯一）；``dataset_id``/``pk_columns_json``
  为保存时刻快照（效果执行零实体库会话依赖）。
- 实例 = append-only 状态机台账；``args_json`` 全量表单值（执行事实源）；
  ``source_ref`` 原样透传（壹座身份行，不解析）。
- 级联：实体删除 → 该实体全部类型置 ``enabled=False``（service 显式，
  历史实例保留可查）——wire-domain-actions spec「声明与变更留痕」。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..ai.db import AiBase


def utc_now_naive() -> datetime:
    """naive-UTC 当前时间（ai 切片模型同口径）。"""
    from ..ai.models import utc_now_naive as _ai_utc  # noqa: PLC0415

    return _ai_utc()


#: 实例状态机（append-only：状态只能沿链前进，回退=新事件）
INSTANCE_STATUS_PENDING = "pending"
INSTANCE_STATUS_AWAITING = "awaiting_confirmation"
INSTANCE_STATUS_EXECUTED = "executed"
INSTANCE_STATUS_REJECTED = "rejected"
INSTANCE_STATUS_FAILED = "failed"
INSTANCE_STATUS_DISABLED = "disabled"

#: 提议通道
CHANNEL_CONSOLE = "console"
CHANNEL_MCP = "mcp"

#: 域动作风险枚举（v0：publish/cost 不在域动作语义内——对外暴露与计费
#: 动作不是业务域动作；声明时 422 带合法清单）
DOMAIN_RISKS: tuple[str, ...] = ("write", "destructive")

#: 表单字段 input 枚举（复用 form-apps 字段子集；select 仅字面量选项）
FIELD_INPUTS: tuple[str, ...] = ("text", "number", "date", "select")

#: 效果 op 枚举（v0：insert / update；delete 留 v1——destructive 语义
#: 需要更强的确认面，本 change 不开）
EFFECT_KINDS: tuple[str, ...] = ("insert", "update")


class DomainActionType(AiBase):
    """一条动作类型声明（客户维度隔离；挂实体；name 实体内唯一）。"""

    __tablename__ = "domain_action_types"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "entity_id", "name", name="uq_domain_action_types_name"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 挂载实体（entities 库跨库弱引用；service 校验属主）
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: 动作名（实体内唯一；如「批准」）
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: write | destructive（v0 枚举；决定闸确认档位）
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 表单字段 schema [{name, label?, input, required, options?}]
    form_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: 效果 spec {kind, value_mapping{column: {field|literal}}}
    effect: Mapped[dict] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: ── 保存时刻快照（实体绑定数据集 + 主键列；漂移在提议/执行处如实拒绝）
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    pk_columns_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<DomainActionType {self.customer_id}:{self.entity_id}:{self.name}>"


class DomainActionTypeEvent(AiBase):
    """类型变更留痕（append-only：只 INSERT，无改删代码路径）。"""

    __tablename__ = "domain_action_type_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    actor_sub: Mapped[str] = mapped_column(String(64), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DomainActionInstance(AiBase):
    """一条动作实例（台账行；append-only 状态机——状态只沿链前进）。"""

    __tablename__ = "domain_action_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 闸提议行（同库；确认/执行走闸管线——审计双线同源）
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    proposer_sub: Mapped[str] = mapped_column(String(64), nullable=False)
    #: console | mcp
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 来源标注（壹座身份行原样透传；≤256 字符不解析）
    source_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: 全量表单值（执行事实源）
    args_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 执行成功回填（corrections 落层 id）
    correction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"<DomainActionInstance {self.id} type={self.type_id} "
            f"status={self.status}>"
        )
