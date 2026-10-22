"""质量快照 ORM 模型（wire-asset-catalog 任务组 4，design D2/D4/D6）。

``dataset_quality_snapshots`` 挂 ``DatasetsBase``（与 datasets 切片同库
同引擎；生产走 alembic 0031，``create_all`` 为开发兜底）：

- **只追加**（append-only）：全模块只有 INSERT 与保留期清理（裁剪最旧
  行），不存在任何 UPDATE 路径与改/删端点——历史快照不可改，趋势可信
  （spec「历史不可改」；与 asset_meta_events 同款代码纪律，DB 层不加
  不可变机器）。
- **只有聚合计数与比率，绝无数据值**（红线 2 的存储面落地）：行数、
  主键唯一性比例、按列样本空值率（列名 + 比率 + 抽样行数）——写入前经
  :func:`fd_wire_api.quality.service._sanitize_null_rates` 白名单过滤，
  引擎多给的任何字段（含可能的样本行）一律丢弃。
- 失败如实：引擎不支持（旧 cell 无质量工具）落 ``unsupported``、其他
  失败落 ``failed`` + ``error_text``，绝不用旧快照冒充当前值。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..datasets.db import DatasetsBase


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


#: 触发方式枚举（spec「快照触发时机」：创建/同步/派生/手动）
TRIGGERED_BY_VALUES: tuple[str, ...] = ("create", "sync", "materialize", "manual")

#: 快照状态常量（ok=正常；unsupported=引擎暂不支持；failed=计算失败）
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"

#: 快照状态枚举（spec「引擎不支持时降级显示」）
STATUS_VALUES: tuple[str, ...] = (STATUS_OK, STATUS_FAILED, STATUS_UNSUPPORTED)


class DatasetQualitySnapshot(DatasetsBase):
    """数据集质量快照（每数据集保留最近 N 条，N 见 quality/settings）。

    列语义（除 ``error_text`` 外全部来自引擎 ``customer_dataset_quality``
    工具的聚合输出；缺字段诚实置 NULL，绝不猜数）：

    - ``computed_at``：引擎侧计算时间（解析失败回落平台当前时间）；
    - ``row_count``：数据集行数（引擎聚合计数）；
    - ``pk_declared`` / ``pk_unique_ratio``：是否声明键列 / 键唯一性比例；
      **未声明键列时比例恒为 NULL**（不给伪造数字，spec「诚实处理」）；
    - ``null_rates_json``：``[{column, null_rate, sampled_rows}]``（按列
      样本空值率；白名单过滤，只承载聚合比率）；
    - ``sample_limit_hit``：空值率抽样是否触顶（超限标注，不谎报全量）。
    """

    __tablename__ = "dataset_quality_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    #: create | sync | materialize | manual
    triggered_by: Mapped[str] = mapped_column(String(16))
    #: ok | failed | unsupported
    status: Mapped[str] = mapped_column(String(16))
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pk_unique_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    pk_declared: Mapped[bool] = mapped_column(Boolean, default=False)
    null_rates_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    sample_limit_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


# ------------------------------------------------------------------ 规则引擎
# wire-quality-rules：规则配置可编辑（updated_by 留痕），结果 append-only。

#: 规则指标封闭枚举（两代）：
#: 计数代（v1）= 一次计数查询可表达或快照已有；
#: 内容代（v2）= 引擎侧 pandera 进程内检查（wire-quality-rules-v2）。
RULE_METRICS: tuple[str, ...] = (
    "row_count",
    "null_count",
    "null_rate",
    "filled_rate",
    "value_violations",
    "key_uniqueness",
)
#: 内容代检查 kind（v2）——引擎 customer_dataset_check 执行
RULE_CONTENT_METRICS: tuple[str, ...] = ("regex", "col_compare", "range")

#: 规则比较操作符封闭枚举（SodaCL 语法形）
RULE_OPS: tuple[str, ...] = ("<", "<=", "=", ">=", ">")

#: 求值结果状态（GX 结果契约的柏讯面）
RULE_STATUS_PASS = "pass"
RULE_STATUS_FAIL = "fail"
RULE_STATUS_ERROR = "error"
RULE_STATUS_VALUES: tuple[str, ...] = (
    RULE_STATUS_PASS,
    RULE_STATUS_FAIL,
    RULE_STATUS_ERROR,
)

#: 求值通道：query=编译成引擎 count_only 计数；snapshot=复用最近 ok 快照；
#: pandera=引擎 content 检查（进程内执行，wire-quality-rules-v2）
RULE_CHANNEL_QUERY = "query"
RULE_CHANNEL_SNAPSHOT = "snapshot"
RULE_CHANNEL_CHECK = "pandera"


class DatasetQualityRule(DatasetsBase):
    """数据集质量规则（配置，可编辑；每数据集上限见 quality/settings）。

    ``check_json`` 是封闭枚举的结构化检查体（写入前经
    :func:`fd_wire_api.quality.rules.validate_check` 校验——指标/操作符/
    列名/白名单值逐项白名单，编译器只组装 filter 字典，永不拼表达式
    字符串，design D3 注入防线）::

        {"metric": "null_rate", "column": "age", "op": "<=", "threshold": 0.05}
        {"metric": "value_violations", "column": "status", "op": "=",
         "threshold": 0, "allowed": ["active", "closed"]}
    """

    __tablename__ = "dataset_quality_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    #: 客户自拟规则名（可空——空时控制台用指标人话化兜底）
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    check_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 最近一次变更的操作者（服务端身份，不信请求头）
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )


class DatasetQualityRuleResult(DatasetsBase):
    """规则求值结果（append-only；GX 契约三数 + 实测值 + 基准时刻）。

    - ``element_count`` / ``unexpected_count`` / ``pass_rate``：Great
      Expectations 结果契约的柏讯面（row_count 类无比率语义的指标恒 NULL）；
    - ``observed``：实测值（计数类=个数、比率类=比率、快照类=键唯一率）；
    - ``channel``：query（实算）| snapshot（复用快照）；
    - ``basis_at``：求值基准时刻（query=引擎计数时刻、snapshot=所依据
      快照的 computed_at——如实披露结论依据的快照年龄）；
    - ``error_text``：status=error 时的原因摘要（绝不用旧结果冒充当前）。
    """

    __tablename__ = "dataset_quality_rule_results"

    # 注意：SQLite 只有 INTEGER PRIMARY KEY 才自动增量（BIGINT 不行）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    #: pass | fail | error
    status: Mapped[str] = mapped_column(String(8))
    element_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unexpected_count: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: query | snapshot
    channel: Mapped[str] = mapped_column(String(8))
    basis_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: create | sync | materialize | manual（与快照同集）
    triggered_by: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
