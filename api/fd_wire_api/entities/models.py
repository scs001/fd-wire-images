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

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"<EntityRelationship {self.from_entity_id}.{self.from_column}"
            f" ->({self.kind}) {self.to_entity_id}.{self.to_column}>"
        )
