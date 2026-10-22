"""质量规则引擎（wire-quality-rules）：DSL 校验 + 编译器 + 求值。

设计与红线（design D1-D5）：

- **借形不借码**：SodaCL 的 DSL 语法形（``指标 列 操作符 阈值``，存结构化
  JSON 不存自由文本）+ Great Expectations 的结果契约（element_count /
  unexpected_count / pass_rate）+ frictionless 的约束语义。零新依赖——
  开源四家（Soda/GX/pandera/dbt/frictionless）或许可证禁托管（ELv2）、
  或要求平台进程接触数据内容（撞红线 2），全部不可直接采用。
- **求值经引擎计数，平台不直读**（红线 2）：query 通道把规则编译成引擎
  ``customer_dataset_query`` 的 ``count_only`` 调用（filter 结构化字典，
  **代码中不存在任何表达式字符串拼接路径**——design D3 注入防线）；
  ``key_uniqueness`` 无法用计数查询表达（需 GROUP BY），复用最近一条
  ok 快照的键唯一率（snapshot 通道，``basis_at`` 如实披露快照年龄）。
- **封闭枚举校验在先**：指标/操作符/列名（须在 ``datasets.columns_json``
  注册表内）/白名单值逐项白名单——校验失败 422 且零引擎调用。
- **失败隔离**：单规则求值任何异常只落一条 ``error`` 结果行，绝不外抛
  （自动触发路径不影响同步/派生主流程；手动 run 端点逐条返回部分成功）。
- **结果 append-only**：同快照纪律，只有 INSERT 与保留期清理。

口径约定（诚实边界，测试钉死）：

- 空数据集的比率指标按「空真」处理：``null_rate`` 实测 0.0、
  ``filled_rate`` 实测 1.0、违规数 0（element_count=0 如实可见）；
- ``value_violations`` 语义 = **非空值不在白名单**（SQL ``NOT IN`` 同款：
  NULL 不算违规——缺失由 null 类指标单独盯）；
- ``row_count`` 无比率语义：``unexpected_count``/``pass_rate`` 恒 NULL，
  pass/fail 仅由计数比较得出。
"""

from __future__ import annotations

import logging
import math
import re as _re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..datasets.ingest import EngineCallError, EngineUnavailable, TOOL_QUERY
from ..datasets.models import Dataset
from .models import (
    RULE_CHANNEL_CHECK,
    RULE_CHANNEL_QUERY,
    RULE_CHANNEL_SNAPSHOT,
    RULE_CONTENT_METRICS,
    RULE_METRICS,
    RULE_OPS,
    RULE_STATUS_ERROR,
    RULE_STATUS_FAIL,
    RULE_STATUS_PASS,
    DatasetQualityRule,
    DatasetQualityRuleResult,
    DatasetQualitySnapshot,
    STATUS_OK as SNAPSHOT_OK,
)
from .service import (
    _is_unsupported_error,
    _resolve_engine_client,
    _sanitize_float,
    _sanitize_int,
    _utcnow_naive,
)
from .settings import get_settings

logger = logging.getLogger(__name__)

#: 引擎内容代检查工具（v2；旧 cell 无此工具 → 诚实 unsupported 降级）
TOOL_CHECK = "customer_dataset_check"

#: 指标 → 是否需要 column
_METRIC_NEEDS_COLUMN = frozenset(
    {"null_count", "null_rate", "filled_rate", "value_violations"}
)
#: col_compare 六比较符（含 !=，与引擎 _QUERY_OPS 的 ne 对齐）
_COL_COMPARE_OPS = ("<", "<=", "=", "!=", ">=", ">")
#: 内容代检查单次引擎调用上限（Q8：配置不限、执行分批）
_CONTENT_BATCH_SIZE = 5
#: pattern 编译闸（与引擎侧同款：长度 + 灾难回溯构造黑名单）
_PATTERN_MAX_LEN = 128
_PATTERN_NESTED_QUANT = _re.compile(r"\([^()]*[+*][^()]*\)[+*]")
#: 比率类指标（observed 是比率而非计数）
_RATE_METRICS = frozenset({"null_rate", "filled_rate"})
#: 白名单值的结构与长度上限（design D3：封闭面压到最小）
_MAX_ALLOWED_VALUES = 32
_MAX_VALUE_LEN = 64
_MAX_NAME_LEN = 200
#: threshold 数值边界（有限数即可；比率类另限 [0, 1]）
_OP_FUNCS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "=": lambda a, b: a == b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
}


class RuleValidationError(ValueError):
    """DSL 校验失败（路由层翻译成 422，字段级原因在 message）。"""


# ------------------------------------------------------------------ DSL 校验


def validate_check(raw: Any, columns: list[str]) -> dict:
    """校验并归一一条规则检查体；非法抛 :class:`RuleValidationError`。

    封闭枚举（design D3 注入防线第一层）：计数代指标六选一 + 操作符
    五选一 + 列名逐字符比对注册表 + 白名单值逐个类型校验；内容代
    （regex/col_compare/range，v2）另走 :func:`_validate_content_check`。
    **校验不过绝不触引擎**。
    """
    if not isinstance(raw, dict):
        raise RuleValidationError("检查体必须是对象")

    metric = raw.get("metric")
    if metric in RULE_CONTENT_METRICS:
        # 内容代（v2）：独立校验体（无 op/threshold 语义）
        return _validate_content_check(metric, raw, columns)
    if metric not in RULE_METRICS:
        raise RuleValidationError(
            f"未知指标 {metric!r}（允许: {', '.join(RULE_METRICS + RULE_CONTENT_METRICS)}）"
        )

    op = raw.get("op")
    if op not in RULE_OPS:
        raise RuleValidationError(
            f"未知操作符 {op!r}（允许: {', '.join(RULE_OPS)}）"
        )

    threshold = raw.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise RuleValidationError("阈值必须是数值")
    if not math.isfinite(float(threshold)):
        raise RuleValidationError("阈值必须是有限数值")
    if metric in ("null_rate", "filled_rate", "key_uniqueness") and not (
        0.0 <= float(threshold) <= 1.0
    ):
        raise RuleValidationError("比率类指标的阈值必须在 [0, 1] 内")

    column = raw.get("column")
    if metric in _METRIC_NEEDS_COLUMN:
        if not isinstance(column, str) or not column:
            raise RuleValidationError(f"指标 {metric} 必须指定列（column）")
        if column not in columns:
            raise RuleValidationError(
                f"未知列 {column!r}（不在该数据集的列注册表内）"
            )
    else:
        column = None

    allowed: list[str] | None = None
    if metric == "value_violations":
        raw_allowed = raw.get("allowed")
        if not isinstance(raw_allowed, list) or not (2 <= len(raw_allowed) <= _MAX_ALLOWED_VALUES):
            raise RuleValidationError(
                f"value_violations 必须附 2-{_MAX_ALLOWED_VALUES} 个白名单值"
            )
        seen: set[str] = set()
        for v in raw_allowed:
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise RuleValidationError("白名单值必须是字符串或数值")
            if not math.isfinite(float(v)) if isinstance(v, float) else False:
                raise RuleValidationError("白名单数值必须是有限数")
            sv = str(v)
            if len(sv) > _MAX_VALUE_LEN:
                raise RuleValidationError(
                    f"白名单值过长（>{_MAX_VALUE_LEN} 字符）"
                )
            if sv in seen:
                raise RuleValidationError(f"白名单值重复: {sv!r}")
            seen.add(sv)
        allowed = sorted(seen)

    return {"metric": metric, "column": column, "op": op,
            "threshold": float(threshold), **({"allowed": allowed} if allowed else {})}


def _validate_content_check(metric: str, raw: dict, columns: list[str]) -> dict:
    """内容代检查（v2）校验体——与引擎 customer_dataset_check 契约一致。

    - regex：column（注册表内）+ pattern（长度 ≤128、无嵌套量词构造）；
    - col_compare：col_a/col_b（均注册表内、不相同）+ op（六符含 !=）；
    - range：column + min/max（至少一个、min ≤ max）。
    """

    def _require_column(value, what: str) -> str:
        if not isinstance(value, str) or not value:
            raise RuleValidationError(f"{what} 必须指定列名")
        if value not in columns:
            raise RuleValidationError(f"未知列 {value!r}（不在该数据集的列注册表内）")
        return value

    if metric == "regex":
        column = _require_column(raw.get("column"), "regex")
        pattern = raw.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise RuleValidationError("regex 必须给 pattern")
        if len(pattern) > _PATTERN_MAX_LEN:
            raise RuleValidationError(
                f"pattern 过长（>{_PATTERN_MAX_LEN} 字符）"
            )
        if _PATTERN_NESTED_QUANT.search(pattern):
            raise RuleValidationError(
                "pattern 含嵌套量词构造（灾难回溯风险），拒绝"
            )
        try:
            _re.compile(pattern)
        except _re.error as exc:
            raise RuleValidationError(f"pattern 编译失败: {exc}") from exc
        return {"metric": "regex", "column": column, "pattern": pattern}

    if metric == "col_compare":
        col_a = _require_column(raw.get("col_a"), "col_compare 左侧")
        col_b = _require_column(raw.get("col_b"), "col_compare 右侧")
        if col_a == col_b:
            raise RuleValidationError("col_compare 两侧不能是同一列")
        op = raw.get("op")
        if op not in _COL_COMPARE_OPS:
            raise RuleValidationError(
                f"col_compare 操作符必须是 {'/'.join(_COL_COMPARE_OPS)} 之一"
            )
        return {"metric": "col_compare", "col_a": col_a, "op": op, "col_b": col_b}

    # range
    column = _require_column(raw.get("column"), "range")
    lo, hi = raw.get("min"), raw.get("max")
    for value, name in ((lo, "min"), (hi, "max")):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RuleValidationError(f"range 的 {name} 必须是有限数值")
    if lo is None and hi is None:
        raise RuleValidationError("range 至少给 min 或 max 之一")
    if lo is not None and hi is not None and float(lo) > float(hi):
        raise RuleValidationError("range 的 min 不能大于 max")
    out = {"metric": "range", "column": column}
    if lo is not None:
        out["min"] = float(lo)
    if hi is not None:
        out["max"] = float(hi)
    return out


def validate_name(raw: Any) -> str | None:
    """规则名可空；非空时限长（超限 422）。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RuleValidationError("规则名必须是字符串")
    name = raw.strip()
    if len(name) > _MAX_NAME_LEN:
        raise RuleValidationError(f"规则名过长（>{_MAX_NAME_LEN} 字符）")
    return name or None


def dataset_columns(dataset: Dataset) -> list[str]:
    """数据集列注册表（``datasets.columns_json``——漂移基线，建档时写入）。"""
    names: list[str] = []
    for col in dataset.columns_json or []:
        if isinstance(col, dict) and isinstance(col.get("name"), str):
            names.append(col["name"])
    return names


# ------------------------------------------------------------------ 编译器


def compile_filters(check: dict) -> list[dict]:
    """规则 → 引擎 count 查询的 filter 列表（**结构化组装，永不拼字符串**）。

    引擎算子面（customer_dataset_tools._QUERY_OPS）：
    ``isnull / notnull / in / notin`` 等，值全参数化。每条规则至多一组
    filter（一次计数查询 + 一次无 filter 全量计数）。
    """
    metric, column = check["metric"], check.get("column")
    if metric in ("null_count", "null_rate"):
        return [{"column": column, "op": "isnull"}]
    if metric == "filled_rate":
        return [{"column": column, "op": "notnull"}]
    if metric == "value_violations":
        return [{"column": column, "op": "notin", "values": list(check["allowed"])}]
    # row_count / key_uniqueness：无 filter 计数 / 走快照通道
    return []


def _fmt_rate(value: float) -> float:
    return round(float(value), 6)


# ------------------------------------------------------------------ 求值


def _engine_count(dataset: Dataset, filters: list[dict], client: Any) -> int:
    """一次引擎 count_only 计数（平台只见计数，红线 2）。"""
    engine = _resolve_engine_client(dataset.customer_id, client)
    payload = engine.call(
        TOOL_QUERY,
        {"dataset_key": dataset.key, "filters": filters, "count_only": True},
    )
    count = payload.get("count") if isinstance(payload, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise EngineCallError(
            f"引擎计数返回形态异常（期望非负整数 count，收到 {type(count).__name__}）"
        )
    return count


def _record_result(
    db: Session,
    *,
    rule: DatasetQualityRule,
    status: str,
    triggered_by: str,
    element_count: int | None = None,
    unexpected_count: int | None = None,
    pass_rate: float | None = None,
    observed: float | None = None,
    channel: str = RULE_CHANNEL_QUERY,
    basis_at: datetime | None = None,
    error_text: str | None = None,
) -> DatasetQualityRuleResult:
    row = DatasetQualityRuleResult(
        rule_id=rule.id,
        dataset_id=rule.dataset_id,
        status=status,
        element_count=element_count,
        unexpected_count=unexpected_count,
        pass_rate=pass_rate,
        observed=observed,
        threshold_json=dict(rule.check_json),
        channel=channel,
        basis_at=basis_at,
        error_text=error_text,
        triggered_by=triggered_by,
    )
    db.add(row)
    db.flush()
    _prune_results(db, rule.id)
    return row


def _prune_results(db: Session, rule_id: int) -> None:
    """每规则保留最近 N 条（append-only 的保留期清理，同快照剪枝）。"""
    keep = get_settings().quality_rule_results_retention
    ids = db.execute(
        select(DatasetQualityRuleResult.id)
        .where(DatasetQualityRuleResult.rule_id == rule_id)
        .order_by(DatasetQualityRuleResult.id.desc())
        .limit(keep)
    ).scalars().all()
    if ids:
        db.execute(
            delete(DatasetQualityRuleResult).where(
                DatasetQualityRuleResult.rule_id == rule_id,
                DatasetQualityRuleResult.id.notin_(ids),
            )
        )


def _evaluate_snapshot_metric(
    db: Session, rule: DatasetQualityRule, triggered_by: str
) -> DatasetQualityRuleResult:
    """key_uniqueness：复用最近一条 ok 快照（basis_at 披露快照年龄）。"""
    snapshot = db.execute(
        select(DatasetQualitySnapshot)
        .where(
            DatasetQualitySnapshot.dataset_id == rule.dataset_id,
            DatasetQualitySnapshot.status == SNAPSHOT_OK,
        )
        .order_by(DatasetQualitySnapshot.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snapshot is None:
        return _record_result(
            db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
            channel=RULE_CHANNEL_SNAPSHOT,
            error_text="尚无可用质量快照（键唯一性依据快照计算，请先刷新快照）",
        )
    if not snapshot.pk_declared or snapshot.pk_unique_ratio is None:
        return _record_result(
            db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
            channel=RULE_CHANNEL_SNAPSHOT,
            basis_at=snapshot.computed_at,
            error_text="数据集未声明键列，键唯一性不可求值（不给伪造比例）",
        )
    ratio = float(snapshot.pk_unique_ratio)
    check = rule.check_json
    ok = _OP_FUNCS[check["op"]](ratio, float(check["threshold"]))
    return _record_result(
        db, rule=rule,
        status=RULE_STATUS_PASS if ok else RULE_STATUS_FAIL,
        triggered_by=triggered_by,
        element_count=snapshot.row_count,
        unexpected_count=None,
        pass_rate=_fmt_rate(ratio),
        observed=_fmt_rate(ratio),
        channel=RULE_CHANNEL_SNAPSHOT,
        basis_at=snapshot.computed_at,
    )


def _engine_check(dataset: Dataset, checks: list[dict], client: Any) -> list[dict]:
    """引擎内容代检查一次调用（≤5 条/批；调用方保证批量上限）。

    返回逐检查契约列表；形态异常抛 EngineCallError（上层落 error）。
    """
    engine = _resolve_engine_client(dataset.customer_id, client)
    payload = engine.call(
        TOOL_CHECK, {"dataset_key": dataset.key, "checks": checks}
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise EngineCallError(
            f"引擎检查返回形态异常（期望 results 数组，收到 {type(payload).__name__}）"
        )
    return payload["results"]


def _check_spec_of(check: dict) -> dict:
    """平台检查体 → 引擎 customer_dataset_check 的 spec（列名直传——
    两侧同封关闭集校验，结构化字典无拼串）。"""
    metric = check["metric"]
    if metric == "regex":
        return {"kind": "regex", "column": check["column"], "pattern": check["pattern"]}
    if metric == "col_compare":
        return {
            "kind": "col_compare",
            "col_a": check["col_a"],
            "op": check["op"],
            "col_b": check["col_b"],
        }
    return {
        "kind": "range",
        "column": check["column"],
        **({"min": check["min"]} if "min" in check else {}),
        **({"max": check["max"]} if "max" in check else {}),
    }


def _evaluate_content_check(
    db: Session, *, dataset: Dataset, rule: DatasetQualityRule,
    triggered_by: str, client: Any,
) -> DatasetQualityRuleResult:
    """内容代检查求值（v2）：引擎单条调用（批量编排在 run 层）。

    observed 语义（design D5b）= 未达标行数。旧 cell（无 check 工具/
    pandera 缺失）→ 诚实 error「引擎暂不支持该检查类型」。
    """
    now = _utcnow_naive()
    check = rule.check_json
    try:
        results = _engine_check(dataset, [_check_spec_of(check)], client)
    except EngineUnavailable as exc:
        return _record_result(
            db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
            channel=RULE_CHANNEL_CHECK, basis_at=now,
            error_text=f"引擎不可用: {exc}",
        )
    except EngineCallError as exc:
        text = str(exc)
        if _is_unsupported_error(exc):
            text = "引擎暂不支持该检查类型（cell 镜像未升级）"
        return _record_result(
            db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
            channel=RULE_CHANNEL_CHECK, basis_at=now, error_text=text,
        )
    if not results:
        return _record_result(
            db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
            channel=RULE_CHANNEL_CHECK, basis_at=now,
            error_text="引擎检查无逐条结果（形态异常）",
        )
    item = results[0]
    return _record_check_item(
        db, rule=rule, item=item, triggered_by=triggered_by, basis_at=now,
    )


def _record_check_item(
    db: Session, *, rule: DatasetQualityRule, item: dict,
    triggered_by: str, basis_at: datetime,
) -> DatasetQualityRuleResult:
    """引擎逐检查契约 → 结果行（D5b 映射：observed=未达标行数、
    threshold_json=检查定义原文——UI 分类渲染）。"""
    status = item.get("status")
    if status not in (RULE_STATUS_PASS, RULE_STATUS_FAIL):
        return _record_result(
            db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
            channel=RULE_CHANNEL_CHECK, basis_at=basis_at,
            error_text=item.get("error") or "检查执行失败",
        )
    return _record_result(
        db, rule=rule, status=status, triggered_by=triggered_by,
        element_count=_sanitize_int(item.get("element_count")),
        unexpected_count=_sanitize_int(item.get("unexpected_count")),
        pass_rate=_sanitize_float(item.get("pass_rate")),
        observed=_sanitize_float(item.get("unexpected_count")),
        channel=RULE_CHANNEL_CHECK,
        basis_at=basis_at,
    )


def evaluate_rule(
    db: Session,
    *,
    dataset: Dataset,
    rule: DatasetQualityRule,
    triggered_by: str,
    client: Any = None,
) -> DatasetQualityRuleResult:
    """求值一条规则（任何异常 → ``error`` 结果行，绝不外抛）。"""
    try:
        check = rule.check_json
        if check.get("metric") == "key_uniqueness":
            return _evaluate_snapshot_metric(db, rule, triggered_by)
        if check.get("metric") in RULE_CONTENT_METRICS:
            return _evaluate_content_check(
                db, dataset=dataset, rule=rule,
                triggered_by=triggered_by, client=client,
            )

        now = _utcnow_naive()
        try:
            total = _engine_count(dataset, [], client)
            filters = compile_filters(check)
            matched = (
                _engine_count(dataset, filters, client) if filters else total
            )
        except EngineUnavailable as exc:
            return _record_result(
                db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
                basis_at=now, error_text=f"引擎不可用: {exc}",
            )
        except EngineCallError as exc:
            return _record_result(
                db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
                basis_at=now, error_text=str(exc),
            )

        metric = check["metric"]
        threshold = float(check["threshold"])
        op = check["op"]

        observed: float | None
        if metric == "row_count":
            observed = float(total)
            ok = _OP_FUNCS[op](total, threshold)
            element, unexpected, pass_rate = total, None, None
        elif metric in ("null_count", "null_rate"):
            nulls = matched  # isnull 计数
            if metric == "null_count":
                observed = float(nulls)
                ok = _OP_FUNCS[op](nulls, threshold)
            else:
                rate = _fmt_rate(nulls / total) if total else 0.0
                observed = rate
                ok = _OP_FUNCS[op](rate, threshold)
            element = total
            unexpected = nulls
            pass_rate = _fmt_rate(1 - nulls / total) if total else 1.0
        elif metric == "filled_rate":
            filled = matched  # notnull 计数
            rate = _fmt_rate(filled / total) if total else 1.0
            observed = rate
            ok = _OP_FUNCS[op](rate, threshold)
            element, unexpected, pass_rate = total, total - filled, rate
        else:  # value_violations（notin 计数；SQL NOT IN 不计 NULL）
            violations = matched
            observed = float(violations)
            ok = _OP_FUNCS[op](violations, threshold)
            element = total
            unexpected = violations
            pass_rate = _fmt_rate(1 - violations / total) if total else 1.0

        return _record_result(
            db, rule=rule,
            status=RULE_STATUS_PASS if ok else RULE_STATUS_FAIL,
            triggered_by=triggered_by,
            element_count=element,
            unexpected_count=unexpected,
            pass_rate=pass_rate,
            observed=observed,
            channel=RULE_CHANNEL_QUERY,
            basis_at=now,
        )
    except Exception as exc:  # noqa: BLE001 - 单规则失败只落 error 行
        logger.warning(
            "质量规则求值失败（已隔离）rule_id=%s: %s: %s",
            getattr(rule, "id", "?"), type(exc).__name__, exc,
        )
        try:
            return _record_result(
                db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
                error_text=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 - 落库也失败：记日志放弃
            logger.exception("质量规则 error 结果落库失败 rule_id=%s", rule.id)
            raise


def evaluate_dataset_rules(
    db: Session,
    *,
    dataset: Dataset,
    triggered_by: str,
    client: Any = None,
    rule_ids: list[int] | None = None,
) -> list[DatasetQualityRuleResult]:
    """求值一个数据集的全部启用规则（逐条隔离，部分成功如实返回）。"""
    stmt = select(DatasetQualityRule).where(
        DatasetQualityRule.dataset_id == dataset.id,
        DatasetQualityRule.enabled.is_(True),
    )
    if rule_ids is not None:
        if not rule_ids:
            return []
        stmt = stmt.where(DatasetQualityRule.id.in_(rule_ids))
    rules = db.execute(stmt.order_by(DatasetQualityRule.id)).scalars().all()
    results: list[DatasetQualityRuleResult] = []

    # 内容代检查批量编排（Q8）：同批 ≤5 条一次引擎调用，多批串行、
    # 逐批独立失败隔离（一批失败该批全体 error，不影响计数代与其他批）。
    content_rules = [r for r in rules if r.check_json.get("metric") in RULE_CONTENT_METRICS]
    others = [r for r in rules if r not in content_rules]

    for rule in others:
        results.append(
            evaluate_rule(
                db, dataset=dataset, rule=rule,
                triggered_by=triggered_by, client=client,
            )
        )
    for start in range(0, len(content_rules), _CONTENT_BATCH_SIZE):
        batch = content_rules[start : start + _CONTENT_BATCH_SIZE]
        results.extend(
            _evaluate_content_batch(
                db, dataset=dataset, rules=batch,
                triggered_by=triggered_by, client=client,
            )
        )
    # 保持规则原顺序返回（跨批/混合场景下调用方依赖有序）
    order = {rule.id: i for i, rule in enumerate(rules)}
    results.sort(key=lambda row: order.get(row.rule_id, 0))
    db.commit()
    return results


def _evaluate_content_batch(
    db: Session, *, dataset: Dataset, rules: list[DatasetQualityRule],
    triggered_by: str, client: Any,
) -> list[DatasetQualityRuleResult]:
    """一批内容代检查（≤5）一次引擎调用；整批失败全体 error 不外抛。"""
    now = _utcnow_naive()
    specs = [_check_spec_of(r.check_json) for r in rules]
    try:
        items = _engine_check(dataset, specs, client)
    except EngineUnavailable as exc:
        return [
            _record_result(
                db, rule=r, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
                channel=RULE_CHANNEL_CHECK, basis_at=now,
                error_text=f"引擎不可用: {exc}",
            )
            for r in rules
        ]
    except EngineCallError as exc:
        text = (
            "引擎暂不支持该检查类型（cell 镜像未升级）"
            if _is_unsupported_error(exc) else str(exc)
        )
        return [
            _record_result(
                db, rule=r, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
                channel=RULE_CHANNEL_CHECK, basis_at=now, error_text=text,
            )
            for r in rules
        ]
    out: list[DatasetQualityRuleResult] = []
    for idx, rule in enumerate(rules):
        item = items[idx] if idx < len(items) and isinstance(items[idx], dict) else None
        if item is None:
            out.append(_record_result(
                db, rule=rule, status=RULE_STATUS_ERROR, triggered_by=triggered_by,
                channel=RULE_CHANNEL_CHECK, basis_at=now,
                error_text="引擎检查无逐条结果（形态异常）",
            ))
            continue
        out.append(_record_check_item(
            db, rule=rule, item=item, triggered_by=triggered_by, basis_at=now,
        ))
    return out


def auto_evaluate_rules(
    *,
    dataset: Dataset,
    triggered_by: str,
    client: Any = None,
) -> None:
    """自动触发入口（快照钩子后顺跑）——失败隔离，绝不影响主流程。

    api 档跳过（与快照同款档位闸：数据不在引擎侧，不假执行）。
    """
    if getattr(dataset, "storage_tier", "cell") == "api":
        return
    from ..datasets.db import get_session  # noqa: PLC0415 - 惰性防环（同 service）

    try:
        db = get_session()
        try:
            evaluate_dataset_rules(
                db, dataset=dataset, triggered_by=triggered_by, client=client
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 规则求值绝不影响同步/派生主流程
        logger.exception(
            "质量规则自动求值失败（已隔离）dataset_id=%s trigger=%s",
            getattr(dataset, "id", "?"), triggered_by,
        )


# ------------------------------------------------------------------ 视图


def rule_to_dict(rule: DatasetQualityRule) -> dict:
    return {
        "id": rule.id,
        "dataset_id": rule.dataset_id,
        "name": rule.name,
        "enabled": rule.enabled,
        "check": dict(rule.check_json),
        "updated_by": rule.updated_by,
        "updated_at": rule.updated_at.isoformat() + "Z" if rule.updated_at else None,
    }


def result_to_dict(row: DatasetQualityRuleResult) -> dict:
    return {
        "id": row.id,
        "rule_id": row.rule_id,
        "status": row.status,
        "element_count": row.element_count,
        "unexpected_count": row.unexpected_count,
        "pass_rate": row.pass_rate,
        "observed": row.observed,
        "threshold": dict(row.threshold_json) if row.threshold_json else None,
        "channel": row.channel,
        "basis_at": row.basis_at.isoformat() + "Z" if row.basis_at else None,
        "error_text": row.error_text,
        "triggered_by": row.triggered_by,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
    }
