"""调度表 ORM 模型（wire-refresh-scheduler，Alembic 0023）。

``refresh_schedules``：一条定时刷新调度（统一调度器的唯一事实表）。
一行 = 某客户为某属主目标（派生数据集 / dashboard 产物）登记的周期刷新：

- ``target_type``：``dataset`` | ``artifact``（两类执行体分置 executor）；
- ``target_id``：数据集 uuid（String(36)）或产物 int id 的十进制串——
  跨两个库的弱引用，不设外键（与跨切片引用纪律一致），失存由执行器
  自动清行兜底；
- ``interval_minutes``：固定间隔（分钟）——cron 表达式属后续 change；
- ``next_run_at``：下次应执行时刻（naive UTC；due 查询的驱动列）；
- ``last_status``：``pending`` → ``running`` → ``ok`` | ``failed`` 的
  最近一次执行留痕（调度器全局串行，无并发写竞争）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import SchedulesBase


def utc_now_naive() -> datetime:
    """naive-UTC 当前时间（列形态与 packs/artifacts 0021/0022 同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


#: 目标类型合法值（路由/服务层校验依据）
TARGET_TYPES: tuple[str, ...] = ("dataset", "artifact")

#: last_status 合法值（pending 建行初始、running 执行前置、ok/failed 后置）
LAST_STATUSES: tuple[str, ...] = ("pending", "running", "ok", "failed")


class RefreshSchedule(SchedulesBase):
    """一条定时刷新调度（客户维度隔离；执行状态由 runner 推进）。"""

    __tablename__ = "refresh_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 客户面命名空间键（Customer.name，与其他客户面切片一致）
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: dataset | artifact
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 数据集 uuid / 产物 id（十进制串）——弱引用，失存自动清行
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 固定间隔（分钟）；地板由服务层按类型校验（env 可调）
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 下次应执行时刻（naive UTC；due 查询列）
    next_run_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: pending | running | ok | failed（最近一次执行的留痕）
    last_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utc_now_naive)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"<RefreshSchedule {self.id} {self.customer_id} "
            f"{self.target_type}:{self.target_id} every {self.interval_minutes}m>"
        )
