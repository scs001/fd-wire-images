"""资产目录 ORM 模型（wire-asset-catalog，design D1/D3/D7）。

三张表都挂 ``DatasetsBase``（与 datasets 切片同库同引擎——post-PG-consolidation
为同一平台元数据库；生产走 alembic 0030，create_all 为开发兜底）：

- ``dataset_asset_meta``：数据集资产元数据补全层（业务域/标签/描述/期望
  更新节奏/责任人/敏感度）。**懒初始化**——不写存量回填迁移，目录查询用
  LEFT JOIN + COALESCE 语义给默认值（缺行 = 未补全条目，域筛选时归入
  「未分类」——这是特性：目录的价值在于治理已治理的）。``dataset_id`` 是
  对 ``datasets.id`` 的逻辑外联（同 Base 不需要 DB 级外键；数据集删除时
  由调用方一并清理元数据与留痕）。
- ``asset_meta_events``：元数据修改留痕（append-only）。只在 PATCH 代码
  路径 INSERT，全仓无 UPDATE/DELETE 路径——append-only 由代码纪律保证
  （与 team_events 同款），DB 层不加不可变机器；政务版留存策略（≥3 年）
  未来叠在这套语义上加，不重建。
- ``dataset_parent_refs``：派生数据集的父集边（wire-sensitivity-propagation）。
  derive/join/link/管线步骤登记时由 ``_register_derived_child`` 统一写入
  （零第二条登记路径），存量 ``source_kind=derived`` 行由迁移 0041 从
  ``source_ref``（派生行=父集 key）回填（origin=legacy；**legacy link 的右父
  平台侧无记录不回填**——闭包如实标记不全、闸门 fail-closed，重新物化即
  修复）。有效敏感度的祖先闭包以此表为唯一数据源（零引擎调用）。

敏感度三级（design D3）：「常规/内部/敏感」，刻意不含「公开」——客户数据
集标「公开」会暗示可进公开面，而红线 3（公开目录与渲染层不含客户数据）
禁止。值域校验在路由层（422 返回合法清单），DB 不设 CHECK。

可见性阶梯（wire-dataset-marketplace，布尔 published 方案的修订形态）：
客户对单个数据集的开放程度是**四档枚举**而非一个开关——``private``
（默认私有，不出现在任何面）/ ``link``（签名链接可见，复用 share/ HMAC）
/ ``directed``（定向，仅 grant 行列出的客户）/ ``market``（市场，登录
客户可检索）。配套的 ``access_mode``（open|approval）只在 directed/market
两档有效。默认私有由**列默认值** + **查询面 SQL 层过滤** + **敏感档硬闸**
三层共同保证（design D2）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..datasets.db import DatasetsBase


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


#: 敏感度合法三级（design D3；「机密」「公开」等一律 422）
SENSITIVITY_LEVELS: tuple[str, ...] = ("常规", "内部", "敏感")

#: 敏感度缺省档（未补全条目的 COALESCE 默认值）
SENSITIVITY_DEFAULT = "常规"

#: 可见性四档（wire-dataset-marketplace；默认 private——不出现在任何面）
#: - private：私有（默认；市场/定向/链接面一律不出现）
#: - link：签名链接可见（有链接的人可看，不进市场——复用 share/ HMAC 机制）
#: - directed：定向（仅 dataset_share_grants 列出的客户可见）
#: - market：市场（登录客户可检索、可订阅）
VISIBILITY_LEVELS: tuple[str, ...] = ("private", "link", "directed", "market")

#: 可见性缺省档（**默认私有**——未补全条目 = 不出现在任何面，红线 3 核心）
VISIBILITY_DEFAULT = "private"

#: 准入模式（仅 directed/market 两档有效；private/link 时忽略）
ACCESS_MODES: tuple[str, ...] = ("open", "approval")

#: 准入模式缺省档
ACCESS_MODE_DEFAULT = "open"


class AssetMeta(DatasetsBase):
    """数据集资产元数据（每数据集至多一行；dataset_id 主键懒创建）。"""

    __tablename__ = "dataset_asset_meta"

    #: 逻辑外联 datasets.id（同 Base；不声明 ForeignKey，删除级联归调用方）
    dataset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: 业务域（如「销售」；空 = 未分类）
    domain: Mapped[str] = mapped_column(String(64), default="")
    #: 标签列表（JSON 数组；筛选 tag=单标签命中）
    tags_json: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    #: 期望更新节奏（daily|weekly|monthly 等；空 = 未声明 → 新鲜度「未知」）
    cadence_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 责任人备注（自由文本）
    owner_note: Mapped[str] = mapped_column(String(200), default="")
    #: 常规 | 内部 | 敏感（路由层校验；缺省常规）
    sensitivity: Mapped[str] = mapped_column(String(16), default=SENSITIVITY_DEFAULT)
    #: 最近修改人（Logto sub）
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )
    # --------------------------------------------------- 可见性阶梯（0034）
    #: private|link|directed|market（**默认私有** = 不出现在任何市场/检索面；
    #: 迁移侧 server_default 必须是 "private" 字符串，双方言同形）。
    #: 值域校验在路由层（422 带合法清单），DB 不设 CHECK（值域演进免迁移）。
    visibility: Mapped[str] = mapped_column(String(16), default=VISIBILITY_DEFAULT)
    #: open|approval（仅 directed/market 有效——approval 需发布方批准请求；
    #: private/link 档忽略本值但保留原值，切回 directed/market 时生效）
    access_mode: Mapped[str] = mapped_column(String(16), default=ACCESS_MODE_DEFAULT)
    #: 首次进入非 private 态的时刻（切回 private 时清空——即时生效）
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 发布时刻的版本锚点（数据集可变——连接器同步/管线刷新都会改内容；
    #: 消费方按锚点读，撤回再发布则更新锚点。design D3）
    published_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AssetMetaEvent(DatasetsBase):
    """资产元数据修改留痕（append-only：只 INSERT，无改删代码路径）。

    记 dataset_id/修改人/时间/变更前后 JSON（六字段快照；首次补全的
    before 为全默认值快照）。查询面 = GET …/asset-meta/events（客户鉴权）。
    """

    __tablename__ = "asset_meta_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    #: 修改人 Logto sub（服务端身份解析，不信请求头）
    actor_sub: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    #: 变更前六字段快照（JSON；nullable 兼容历史行，新行恒有值）
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 变更后六字段快照
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DatasetParentRef(DatasetsBase):
    """派生数据集的父集边（wire-sensitivity-propagation 任务 1.1/1.2）。

    有效敏感度的祖先闭包唯一数据源（零引擎调用）：derive/join/link/管线步骤
    登记时由 ``_register_derived_child`` 统一写入（零第二条登记路径）；存量
    ``source_kind=derived`` 行由迁移 0041 从 ``source_ref``（派生行=父集 key）
    回填（origin=legacy）。**legacy link 的右父平台侧无记录不回填**——闭包
    如实标记不全、设档闸 fail-closed，重新物化即修复（spec「不猜」纪律）。

    删除纪律（调用方负责，与 AssetMeta 同款）：子集删除 → 清其作为 child 的边；
    父集删除 → 指向它的边保留（dangling）→ 闭包标记不全 → 闸 fail-closed——
    这是刻意的保守：子集仍持有父集数据的副本，父集敏感度已不可考。
    """

    __tablename__ = "dataset_parent_refs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    child_dataset_id: Mapped[str] = mapped_column(String(36))
    parent_dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    #: derive|join|link|pipeline|legacy（登记路径标注；legacy=0041 回填）
    origin: Mapped[str] = mapped_column(String(16), default="derive")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)

    __table_args__ = (
        UniqueConstraint(
            "child_dataset_id", "parent_dataset_id", name="uq_parent_refs_child_parent"
        ),
    )
