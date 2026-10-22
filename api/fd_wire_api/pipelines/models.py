"""变换管线 ORM 模型（wire-transform-pipelines，design D6 五表）。

全部挂 ``DatasetsBase``（与 datasets 切片同库——输出槽位绑定输出数据集
登记时须与 ``datasets`` 同事务；生产走 alembic 0032，create_all 为开发
兜底）：

- ``transform_pipelines``：管线元信息 + ``current_version`` 指针。
- ``transform_pipeline_versions``：版本的完整步骤定义（``steps_json``
  有序 ``[{name, op, params}]``）。**append-only**——无 UPDATE 代码路径，
  历史版本不可改（spec「管线定义版本化且 append-only」）。
- ``pipeline_runs``：run 级留痕（触发方式/状态/起止/错误摘要）。只追加。
- ``pipeline_run_steps``：step 级留痕（序号/类型/输出数据集/行数/状态/
  耗时/错误）。只追加；失败后的剩余步骤**无行**（spec 失败隔离）。
- ``transform_pipeline_slots``：输出槽位（管线, 版本, 步骤序号）→ 稳定
  输出数据集 id。首跑创建绑定，同版本重跑原地刷新（design D2）。

id 均为逻辑外联（同 Base 不设 DB 级外键——照 assets/quality 先例）；
数据集删除后槽位行悬空，运行时如实报错（「输出数据集已删除」）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..datasets.db import DatasetsBase
from ..datasets.models import JSON_DUAL_DIALECT, new_uuid


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TransformPipeline(DatasetsBase):
    """管线元信息（步骤定义在版本表；``current_version`` 指当前生效版）。"""

    __tablename__ = "transform_pipelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    #: 首步输入数据集（datasets.id 逻辑外联；每版本可覆写——见版本表）
    base_dataset_id: Mapped[str] = mapped_column(String(36))
    #: 当前生效版本号（版本表 append-only，指针只前进）
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )


class TransformPipelineVersion(DatasetsBase):
    """管线版本：完整步骤定义快照（append-only，历史不可改）。"""

    __tablename__ = "transform_pipeline_versions"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "version", name="uq_pipeline_versions_id_ver"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    #: 本版本的基础数据集（首步输入；版本表保存当期口径）
    base_dataset_id: Mapped[str] = mapped_column(String(36))
    #: 有序步骤定义 [{name, op: derive|join|link, params}]（参数形状与既有
    #: 原子操作端点一致——spec「参数形状一致」；双方言 JSON 见 datasets 先例）
    steps_json: Mapped[list] = mapped_column(JSON_DUAL_DIALECT, default=list)
    #: 创建人（console 身份 sub；调度自动出版本不存在——只有人工保存出版）
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)


class PipelineRun(DatasetsBase):
    """run 级运行留痕（只追加不可改；status=running 是执行中的在飞标记）。"""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    #: manual | schedule（spec：调度触发与手动同等留痕并标注触发方式）
    trigger: Mapped[str] = mapped_column(String(16))
    #: running | success | failed
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class PipelineRunStep(DatasetsBase):
    """step 级运行留痕（只追加；失败步骤之后的剩余步骤无行——失败隔离）。"""

    __tablename__ = "pipeline_run_steps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    #: 步骤序号（1 起，与版本 steps_json 下标对应）
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    #: derive | join | link
    op: Mapped[str] = mapped_column(String(16))
    #: success | failed | skipped | quota_exhausted
    status: Mapped[str] = mapped_column(String(16))
    output_dataset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class TransformPipelineSlot(DatasetsBase):
    """输出槽位：（管线, 版本, 步骤序号）→ 稳定输出数据集（design D2）。

    该版本首跑创建输出数据集时绑定；同版本后续运行原地刷新（不新建）；
    新版本新槽位；旧版本输出以普通数据集存续（本表行保留作历史索引）。
    """

    __tablename__ = "transform_pipeline_slots"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_id", "version", "seq", name="uq_pipeline_slots_pipe_ver_seq"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    dataset_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
