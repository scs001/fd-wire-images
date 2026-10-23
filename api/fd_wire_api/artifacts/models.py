"""产物生命周期 ORM 模型（wire-artifacts-persistence，Alembic 0021）。

- ``artifacts``：产物一等实体——跨会话持久化（kind 五类、title、spec
  当前指针经版本表间接持有、状态机 draft/published/archived、版本计数、
  来源会话指针）。
- ``artifact_versions``：spec 版本链（append-only——每次保存追加一行，
  历史版本不可改写；当前 spec = 最大 version 那行）。
- ``artifact_drafts``：doc 产物草稿层（wire-rich-text-docs design D3）——
  自动保存的覆盖写落点，一对一连产物（唯一外键、产物删除级联清除），
  SHALL NOT 参与版本链/状态机/分享钉版。

表落在 ai 切片引擎（``AiBase``）：产物由会话草稿转正而来，
``origin_conversation_id`` 逻辑引用 ``ai_conversations``（不设跨模块
外键，与 materialization 切片对 ai 表的引用纪律一致）。
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from ..ai.db import AiBase
from ..ai.models import utc_now_naive

#: 五类产物短名（与 ai.tools.ARTIFACT_TOOL_KINDS 的值域对表维护）。
ARTIFACT_KINDS: tuple[str, ...] = (
    "dashboard",
    "composite_indicator",
    "research_report",
    "alert_rule",
    "refresh_schedule",
)


class ArtifactStatus(str, enum.Enum):
    """产物状态机：draft → published → archived 单向；archived → draft 恢复。"""

    draft = "draft"
    published = "published"
    archived = "archived"


class Artifact(AiBase):
    """产物一等实体：跨会话持久化、版本化、带状态机。"""

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 归属企业（客户面按此隔离列表与详情）
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 五类产物短名（ARTIFACT_KINDS）
    kind: Mapped[str] = mapped_column(String(64), index=True)
    #: 产物名称（spec.title 的投影，列表展示用）
    title: Mapped[str] = mapped_column(String(200))
    #: 状态机当前位（ArtifactStatus 值）
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    #: 版本计数（当前 spec = artifact_versions 中 version=该值 的行）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 来源会话指针（会话草稿导入时保留；直接创建为 NULL）
    origin_conversation_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    #: 状态迁移操作记录（append-only：{status, at, by, note} 列表，
    #: 与 demands.status_history 同款留痕口径）
    status_history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # ── 可见性阶梯（wire-artifact-marketplace；值域与数据集市场逐字一致）──
    #: private（默认）| link | directed | market——默认私有，不主动选永不进市场
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", server_default=sa_text("'private'"), index=True
    )
    #: open（订阅即可看）| approval（需发布方批准）；仅 market/directed 有效
    access_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default=sa_text("'open'")
    )
    #: 进入非 private 档位时刻（撤回时清空）
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 进入非 private 档位时刻的版本锚点（审计用；市场渲染跟最新发布版本）
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 允许复制（Q4/Q6：复制=分析结构且快照剥离；发布方显式允许才可复制）
    allow_copy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    #: 复制溯源指针（Q7：仅审计，无跨客户更新提示）
    copied_from_artifact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<Artifact {self.id} kind={self.kind} status={self.status} v={self.version}>"


class ArtifactVersion(AiBase):
    """产物版本链：一行 = 一个版本的 spec 快照（append-only 不可改写）。"""

    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="uq_artifact_versions_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id"), index=True
    )
    #: 版本号（从 1 起，逐次保存 +1）
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 该版本的 spec 快照（工具入参原样 JSON：{"title":..., "spec":{...}}）
    spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: 写入者留痕（console:<customer_id> / migration:<id>）
    operator: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 生产来源指针（wire-content-fleet D6）：部署/岗位/回合三元组的自由文本。
    #: 只由产物构建 MCP 写入；人工与控制台 AI 创建的版本行为空。与 operator
    #: 语义正交——operator 是 console 身份，本列是 agent 运行时来源。
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<ArtifactVersion artifact={self.artifact_id} v={self.version}>"


# ── 草稿层（wire-rich-text-docs design D3）──────────────────────────────


class ArtifactDraft(AiBase):
    """doc 产物的草稿层：自动保存的**覆盖写**工作区（一对一连产物）。

    为什么独立成表而非版本链的行（design D3）：版本链是 append-only 的对外
    契约（分享钉版、市场快照、物化引用都依赖它）——自动保存若进版本链，
    敲字期间会产生几百个版本，把历史炸掉、把分享钉版语义搅浑。分离后：
    草稿层是「工作区」（高频覆盖写、不产生版本行），版本链是「里程碑」
    （显式「保存版本」才追加）。

    为什么一对一连产物（唯一约束而非多行历史）：工作区不需要历史——每个
    产物只需要「最新未落版正文」一行；多行历史即版本链的退化复制品。

    为什么独立表而非 ``artifacts`` 加列：产物表是全系统最热的表（列表/
    工作台/市场都扫），为只有一个 kind 需要的功能给全局表加列是污染；
    独立表 + 外键级联删除，热点路径零变化（design D3「表而非列」）。
    """

    __tablename__ = "artifact_drafts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 归属产物（一对一：unique 索引即工作区语义；产物删除级联清除草稿）
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), unique=True, index=True
    )
    #: 草稿正文（Markdown 源文本；限长校验与版本保存同一口径）
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 最后自动保存时刻（仅草稿层自用，不参与产物 updated_at 排序）
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    #: 写入者留痕（console:<customer_id>）
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<ArtifactDraft artifact={self.artifact_id}>"


# ── 产物市场三表（wire-artifact-marketplace；与 dataset 市场三表同构）────


class ArtifactMarketSubscription(AiBase):
    """产物订阅（唯一约束：customer_id × artifact_id）。

    订阅授予「只读看」权（渲染该产物版本 spec）；撤回/退订即时失效。
    """

    __tablename__ = "artifact_market_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "artifact_id",
            name="uq_artifact_market_subscriptions_customer_artifact",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class ArtifactShareGrant(AiBase):
    """产物定向授权名单（唯一约束：artifact_id × customer_id）。"""

    __tablename__ = "artifact_share_grants"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "customer_id",
            name="uq_artifact_share_grants_artifact_customer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)


class ArtifactAccessRequest(AiBase):
    """产物访问申请（status: pending|approved|rejected；同产物同申请方唯一）。"""

    __tablename__ = "artifact_access_requests"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "requester_customer_id",
            name="uq_artifact_access_requests_artifact_requester",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(Integer, index=True)
    requester_customer_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
