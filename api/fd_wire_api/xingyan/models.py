"""星研内容快照的 ORM 模型（xingyan-content-supply，Alembic 0046）。

三张表：

- ``xingyan_items``：条目快照（一滑一行，键=slug）。双语标题/摘要/正文、
  日期、分区、claim 计数、来源指纹与摄取时间。``status`` 是撤销态开关
  （``active`` | ``withdrawn``）——上游撤稿=从 feed 消失，我们标撤销而非
  删行（可逆：再现即回 active）。
- ``xingyan_claims``：claim 骨架（键=(slug, claim_id)）。**section 双语各存
  一份**——锚点是小节标题文本，随语言变；claim ID 跨语言相同，故跨语言合并
  必须按 ID 而非 section 匹配（llms.txt 已声明该口径）。撤销的 claim 保留
  登记并标 ``withdrawn``——消费者须拿到显式撤销态，而不是"未找到"。
- ``xingyan_sync_state``：摄取管道自身的水位（单行）。``source_sha`` 是
  上游 manifest 的指纹，同指纹短路；``status`` 是最近一次执行结果
  （``ok`` | ``stale`` | ``schema_mismatch`` | ``upstream_error``）。陈旧标记
  就挂在这张表上，读面据此对外暴露 staleness。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import XingyanBase

ITEM_STATUSES = ("active", "withdrawn")
SYNC_STATUSES = ("ok", "stale", "schema_mismatch", "upstream_error")


def utc_now_naive() -> datetime:
    """naive-UTC 当前时间（列形态与 packs/alerts 同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class XingyanItem(XingyanBase):
    """一条星研内容条目快照（键=slug，跨分区唯一）。"""

    __tablename__ = "xingyan_items"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_xingyan_items_slug"),
        Index("ix_xingyan_items_zone", "zone"),
        Index("ix_xingyan_items_status", "status"),
        Index("ix_xingyan_items_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 条目 slug（官网文件名，跨语言相同；全局 ID 的前半段）
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 分区：briefings | academy | research
    zone: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 分区内类型（研究 kind / 学院 level），无则为空串
    item_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 教学顺序（学院分区用；其余为 NULL）
    item_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title_zh: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    title_en: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    summary_zh: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_en: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 日期（YYYY-MM-DD；研究/内参用发布日，学院用 updated）
    published_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tags_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    body_zh: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_en: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sources_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: claim 总数（双语并集；目录列表的 claim_count 即此列）
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 双语页面地址 {en, zh}（正文指针：消费者据此回官网取渲染页）
    url_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: active | withdrawn
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: 本条所属的上游快照指纹（与 sync_state.source_sha 同值；对账逐区比对用）
    source_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<XingyanItem {self.zone}/{self.slug} {self.status}>"


class XingyanClaim(XingyanBase):
    """一条 claim 骨架（键=(slug, claim_id)）。"""

    __tablename__ = "xingyan_claims"
    __table_args__ = (
        UniqueConstraint("slug", "claim_id", name="uq_xingyan_claims_slug_claim"),
        Index("ix_xingyan_claims_claim_id", "claim_id"),
        Index("ix_xingyan_claims_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    #: claim ID（cNNNN，跨语言相同；全局 ID 的后半段）
    claim_id: Mapped[str] = mapped_column(String(16), nullable=False)
    #: number | causal | promise
    claim_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: 锚点小节标题（各语言各自的文本；跨语言合并按 ID 不按本列）
    section_zh: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    section_en: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    #: 证据来源 key 列表（src-N，指向条目 sources_json 的 id 项）
    evidence_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: active | withdrawn（保留登记，绝不因上游撤销而消失）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    source_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<XingyanClaim {self.slug}/{self.claim_id} {self.status}>"


class XingyanSyncState(XingyanBase):
    """摄取管道水位（单行，id=1）。"""

    __tablename__ = "xingyan_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    #: 上游 manifest 指纹（三区 sha + 条目数归一后的摘要）
    source_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 上游 feed 契约版本（本版摄取的取值）
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: ok | stale | schema_mismatch | upstream_error
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="stale")
    #: 最近一次成功摄取的时刻（陈旧标记的基准；从未成功为 NULL）
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 最近一次尝试的时刻（成败都写）
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 最近一次失败原因摘要（成功时清空）
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claims_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<XingyanSyncState {self.status} sha={self.source_sha[:8]}>"
