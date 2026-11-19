"""实体语义层 ORM 模型（wire-entity-layer，Alembic 0026；v2=wire-entity-v2 0042）。

- ``entities``：实体声明 = 业务名 + 绑定数据集 + 主键列。客户内业务名
  唯一 ``(customer_id, name)``；一集一体 ``(customer_id, dataset_id)``。
  列真相在 ``Dataset.columns_json``（datasets 库），声明时校验、之后
  漂移不追改——与 form 的列纪律同口径；**不存列快照副本**。
  v2 增可选属性元数据：``display_column``（对象列表/详情/AI 上下文的展示列）
  与 ``column_notes``（``{column: 注记}``，单位/格式纯描述）。
- ``entity_relationships``：源实体列 → 目标实体**主键列**的有向关系。
  v0 kind ∈ ``n:1`` | ``1:1``；v2 补 ``1:n`` 与 ``m:n``：
  - ``1:n`` 为 n:1 的反向声明——**归一化存储**：from/to 对调、``kind="n:1"``、
    ``declared_kind="1:n"``（存储形状不变，图谱/遍历按声明方向呈现；
    正反向查重按归一化后四元组）；
  - ``m:n`` 经**中间数据集**声明（``junction_dataset_id`` +
    ``junction_source_column``/``junction_target_column`` 两列 ∈ 中间集列
    快照、语义指向两端实体主键）；v0 物化端点不支持 m:n（如实 422）。
  四元组（源实体/源列/目标实体/目标列）唯一；自环（同一实体）允许。

级联删除走 service 显式（同事务先删两端关系再删实体），不依赖 DB
CASCADE——SQLite/PG 双方言行为一致，照 corrections 惯例。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import EntitiesBase


def utc_now_naive() -> datetime:
    """naive-UTC 当前时间（列形态与 packs 0022 / artifacts 0021 同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Entity(EntitiesBase):
    """一条实体声明（客户维度隔离；每数据集至多一个实体）。"""

    __tablename__ = "entities"
    __table_args__ = (
        # 客户内业务名唯一：实体名是图谱与 AI 上下文的指称键
        UniqueConstraint("customer_id", "name", name="uq_entities_customer_name"),
        # 一集一体：同一数据集至多声明一个实体
        UniqueConstraint(
            "customer_id", "dataset_id", name="uq_entities_customer_dataset"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 客户面命名空间键（与其他客户面切片一致）
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 业务名（非空 strip ≤100，service 校验）
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: 绑定数据集 id（datasets 库跨库弱引用，不设外键；service 校验属主）
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: 主键列（非空字符串数组，⊆ dataset.columns_json；语义层不自证类型）
    pk_columns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ------------------------------------------------ 属性元数据（0042 v2）
    #: 对象展示列（∈ 数据集列快照；对象列表/详情与 AI 上下文的对象名）
    display_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: 列注记 {column: 注记}（单位/格式纯描述文本，≤50 字/列；service 校验）
    column_notes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # ------------------------------------------------ 内核附加袋（0055）
    #: 内核 Unit 投影的附加属性（folding 的原写法 ``aliases``、``folded_by`` 等）。
    #: **只放投影附加物，不放业务字段**——业务字段有自己的列且被 service 校验。
    kernel_attrs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # ------------------------------------------------ 知识档位（0050）
    #: 升格档位（add-entity-bench-contract）：raw/bronze/silver/gold 或 null
    #: （null=未入升格流水线；词表 service 层单点校验）
    tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 敏感级（add-entity-bench-contract）：public/internal/confidential 或 null
    sensitivity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<Entity {self.customer_id}:{self.name} ds={self.dataset_id}>"


class EntityRelationship(EntitiesBase):
    """一条有向关系声明：源实体列 → 目标实体主键列。"""

    __tablename__ = "entity_relationships"
    __table_args__ = (
        # 四元组唯一：同一指向不重复声明
        UniqueConstraint(
            "from_entity_id",
            "from_column",
            "to_entity_id",
            "to_column",
            name="uq_entity_relationships_quad",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_entity_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True
    )
    #: 源列（须 ∈ 源实体数据集列，service 校验）
    from_column: Mapped[str] = mapped_column(String(200), nullable=False)
    to_entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: 目标列（纪律：须 ∈ 目标实体 pk_columns——关系指向身份）
    to_column: Mapped[str] = mapped_column(String(200), nullable=False)
    #: "n:1" | "1:1" | "m:n"（1:n 声明归一化存储为 n:1，本列存归一化后基数）
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 声明方向标记（v2）：1:n 声明归一化存储后此处记 "1:n"；
    #: n:1/1:1/m:n 与 kind 同值或缺省——图谱/遍历按声明方向呈现
    declared_kind: Mapped[str | None] = mapped_column(String(8), nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # ------------------------------------------------ m:n 中间集（0042 v2）
    #: 中间数据集 id（仅 kind="m:n"；跨库弱引用，service 校验属主与列）
    junction_dataset_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    #: 中间集两列（∈ 中间集列快照；语义指向源/目标实体主键）
    junction_source_column: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    junction_target_column: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    # ------------------------------------------------ 内核附加袋（0055）
    #: 内核 Unit 投影的附加属性（同 ``Entity.kernel_attrs`` 的边界）
    kernel_attrs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # ------------------------------------------------ 知识档位（0050）
    #: 升格档位（add-entity-bench-contract）：raw/bronze/silver/gold 或 null
    tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 敏感级（add-entity-bench-contract）：public/internal/confidential 或 null
    sensitivity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"<EntityRelationship {self.from_entity_id}.{self.from_column}"
            f" ->({self.kind}) {self.to_entity_id}.{self.to_column}>"
        )


# ------------------------------------------------------------------ 版本（0049）
#
# 本体版本机制（wire-ontology-versioning）两表与本体**同库同事务**
# （design D1）：事件写失败即本体写失败，不存在「本体变了但没有事件」的窗口。
# 事件表 append-only——系统不提供改删接口（service 唯一写入口）。


class OntologyEvent(EntitiesBase):
    """一条本体变更事件（append-only；before/after 全量 JSON）。

    ``seq`` 是客户内顺序号（同客户单调递增；``(customer_id, seq)`` 建索引
    供折叠与未发布变更计数），``id`` 是全局自增（跨客户唯一）。
    """

    __tablename__ = "ontology_events"
    __table_args__ = (
        Index("ix_ontology_events_customer_seq", "customer_id", "seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 客户内顺序号（1 起；插入时 max+1，同事务内取值）
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: create | update | delete
    op: Mapped[str] = mapped_column(String(16), nullable=False)
    #: entity | relationship
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 语义键（0054；跨环境可移植的身份）——实体=实体名，关系=
    #: ``{from_entity_name}->{kind}:{to_entity_name}``。换底前的历史行为 null
    #: 且不回填（当时的名字无从得知，反解出来的只会是今天的名字）；读侧由
    #: 适配器从 ``target_id`` 反解。写入路径见 ``versioning.record_event``。
    target_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: 变更前全量（create 为 null）
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 变更后全量（delete 为 null）
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 谁改的（控制台=成员 sub；闸执行=domain-action:{type}:{instance}；回滚=owner sub）
    actor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: action_gate | console | rollback
    via: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 舰队血缘（经动作闸执行时从实例原样带入；控制台/回滚路径为 null）
    source_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"<OntologyEvent {self.customer_id}#{self.seq} "
            f"{self.op} {self.target_kind}:{self.target_id}>"
        )


class OntologySnapshot(EntitiesBase):
    """一条本体版本快照（客户内版本号单调；manifest 当前态全量）。"""

    __tablename__ = "ontology_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "version", name="uq_ontology_snapshots_customer_version"
        ),
        Index(
            "ix_ontology_snapshots_customer_created", "customer_id", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 客户内版本号（1 起单调递增）
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 规范 manifest 的 sha256（导出物完整性校验值同源）
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: {"entities": [...], "relationships": [...]}（规范形态，键序固定）
    manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: daily | manual（日版=时间线，手动版=发布决策）
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 手动版标签（≤80 字；日版为 null）
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: 创建人（手动版=成员 sub；日任务为 null）
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"<OntologySnapshot {self.customer_id}v{self.version} "
            f"{self.trigger} {self.digest[:8]}>"
        )
