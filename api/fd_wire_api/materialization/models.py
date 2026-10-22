"""物化切片 ORM 模型：``materialized_artifacts``（Alembic 0009）。

一行 = 一个已转正（promoted）的产物：从 ``ai_draft_artifacts`` 拷贝的
spec 快照 + 调度登记 + 最近一次物化运行的结果摘要。

- ``artifact_id``：来源草稿的 id（逻辑引用，不设跨切片外键——ai 表归
  ai 切片引擎纪律；同一草稿只允许转正一次，唯一约束兜底）。
- ``schedule``：五字段 cron 表达式（naive UTC 评估，见 cron.py）；
  NULL = 不调度（仅手动/按需触发）。
- ``last_status``：success / partial_failure / failed / quota_exhausted /
  quota_unverifiable / failed_no_credential / failed_no_wire_entry /
  failed_spec / disabled。
- ``last_rows``：最近一次运行成功读取的行数合计（计费行数——这些读取
  经客户凭据走 wire 入口，registry audit 侧自动计量进客户账单）。
- ``last_run_detail``：逐项成败（code/ok/rows/error），JSON 留痕。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import MatBase, utc_now_naive


class MaterializedArtifact(MatBase):
    """已转正的产物：spec 快照 + 调度 + 运行结果摘要。"""

    __tablename__ = "materialized_artifacts"
    __table_args__ = (
        UniqueConstraint("artifact_id", name="uq_materialized_artifacts_artifact"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 来源草稿（ai_draft_artifacts.id）——同草稿只转正一次；派生调度行
    #: （wire-customer-data-engine 6.1）无草稿来源，置 NULL（唯一约束对
    #: NULL 放行，关联键走 ref_key）
    artifact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: 归属企业（与 ai 切片同款字符串客户 id，如 "cust-001"）
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 产物类型短名（dashboard / composite_indicator / research_report /
    #: derived_dataset——wire-customer-data-engine 6.1 定时重派生，经 ref_key 关联）
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 非草稿产物的关联键（derived_dataset → datasets.id）；草稿产物为 NULL
    ref_key: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    #: 转正时的 spec 快照（草稿 spec 列原样拷贝：{"title":..., "spec":{...}}）
    spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: 五字段 cron（naive UTC）；NULL = 不调度
    schedule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 见模块 docstring 的状态取值
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 最近一次运行成功行数合计（0 = 未跑或无成功项）
    last_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return (
            f"<MaterializedArtifact {self.id} artifact={self.artifact_id} "
            f"kind={self.kind} status={self.last_status}>"
        )
