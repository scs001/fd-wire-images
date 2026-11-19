"""api 层轻量行存储引擎（wire-free-tier-data-gate §2，design D2）。

免费档（无 cell）数据集的存取通道：列元数据（``dataset_columns``）+
JSON 行（``dataset_rows``）。**全链路不触碰 EngineToolClient**——读取同
样不计 metering_records（D7 口径：客户数据读取不过计量轨道）。

- 写入：复用 :mod:`.uploads` 的 CSV 解析与类型推断（``iter_ingest_chunks``
  + ``coerce_value``，不复制实现）；数值列按 dtype 归一后入 JSON 行。
- 查询面（窄）：过滤 eq/in/gt/gte/lt/lte/contains/startswith、排序、分页、
  count、distinct——引擎面支持的其余算子（ne/notin/like/isnull…）在此
  明确拒绝（400，如实标注免费面差异）。
- 聚合面：group by ≤3 列 × count/sum/avg/min/max；sum/avg 仅数值列。
- 双方言：JSON 取值经 SQLAlchemy JSON 抽象（``data["列"].as_string()``）
  + 数值 CAST——SQLite 渲染 ``CAST(JSON_EXTRACT(...) AS FLOAT)``、PG 渲染
  ``CAST(data ->> '列' AS FLOAT)``；``data`` 列 PG 端为 JSONB（models）。
- 本地修正层（wire-dataset-form-editing，镜像引擎覆盖层语义）：修正只追加
  落 ``dataset_corrections``、基行永不改写；读取面统一在「有效视图」上
  呈现——存在未回滚修正时 query/aggregate 切 Python 合并路径（窄面语义
  与 SQL 路径逐项对齐），零修正走原 SQL 快路径（零回归）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import service, uploads
from .models import Dataset, DatasetColumn, DatasetCorrection, DatasetRow
from .settings import get_settings

#: 查询/聚合支持的过滤算子（窄面；与 tasks §2.2 逐字对齐）
SUPPORTED_FILTER_OPS = ("eq", "in", "gt", "gte", "lt", "lte", "contains", "startswith")
#: 聚合允许的度量函数
AGGREGATE_FNS = ("count", "sum", "avg", "min", "max")
#: 聚合分组列数上限（design D2：group by ≤3 列）
AGGREGATE_MAX_GROUP_COLUMNS = 3
#: 聚合返回的分组数上限（10k 行规模的全展开保护；截断如实标记）
AGGREGATE_MAX_GROUPS = 1000
#: 行写入的会话内分块（防 10k 行 ORM 会话膨胀）
_WRITE_FLUSH_ROWS = 1000


class LocalQueryError(ValueError):
    """本地窄查询面的参数/契约错误（路由层转 400，如实提示免费面差异）。"""


# ------------------------------------------------------------------ 列注册表


def _column_map(db: Session, dataset_id: str) -> dict[str, str]:
    """dataset_columns → {列名: dtype}（查询面的列校验与取值依据）。"""
    rows = db.execute(
        select(DatasetColumn.name, DatasetColumn.dtype)
        .where(DatasetColumn.dataset_id == dataset_id)
        .order_by(DatasetColumn.ordinal, DatasetColumn.id)
    ).all()
    return {name: dtype for name, dtype in rows}


def _require_column(columns: dict[str, str], name: Any, *, what: str) -> tuple[str, str]:
    if not isinstance(name, str) or name not in columns:
        raise LocalQueryError(f"{what}引用了不存在的列 {name!r}（可用列: {sorted(columns)}）")
    return name, columns[name]


# ------------------------------------------------------------------ 表达式（双方言）


def _text_expr(name: str):
    """JSON 取值的文本形态：SQLite=JSON_EXTRACT、PG=data ->>（as_string 抽象）。"""
    return DatasetRow.data[name].as_string()


def _numeric_expr(name: str):
    """JSON 取值的数值形态：双方言经 CAST(... AS FLOAT)（整数亦走 FLOAT，
    避免 PG text/int 比较报错与 SQLite 整型截断分歧；展示端再按 dtype 归整）。"""
    return sa.cast(_text_expr(name), sa.Float)


def _typed_expr(name: str, dtype: str):
    """按 dtype 选择取值形态：INTEGER/REAL → 数值 CAST；TEXT → 文本。"""
    if dtype in (uploads.INTEGER, uploads.REAL):
        return _numeric_expr(name)
    return _text_expr(name)


def _as_number(value: Any, *, column: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LocalQueryError(
            f"列 {column!r} 是数值列，过滤值必须是数值（收到 {value!r}）"
        ) from exc


def _as_text(value: Any) -> str:
    if value is None:
        raise LocalQueryError("contains/startswith 过滤值不能为 null")
    return str(value)


def _norm_value(dtype: str, value: Any) -> Any:
    """SQL 产出值的展示归整：INTEGER 列的整值浮点（1.0）还原为 int。"""
    if dtype == uploads.INTEGER and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ------------------------------------------------------------------ 过滤/排序编译


def _compile_filters(columns: dict[str, str], filters: list[dict]) -> list:
    conditions: list = []
    for flt in filters or []:
        if not isinstance(flt, dict):
            raise LocalQueryError(f"过滤项必须是对象（收到 {flt!r}）")
        op = str(flt.get("op") or "")
        if op not in SUPPORTED_FILTER_OPS:
            raise LocalQueryError(
                f"api 层数据集不支持过滤算子 {op!r}"
                f"（支持: {', '.join(SUPPORTED_FILTER_OPS)}；完整算子面需 cell 层/付费档）"
            )
        name, dtype = _require_column(columns, flt.get("column"), what="过滤")
        value = flt.get("value")
        if dtype in (uploads.INTEGER, uploads.REAL):
            expr = _numeric_expr(name)
            if op == "in":
                values = _in_values(flt)
                conditions.append(
                    expr.in_([_as_number(v, column=name) for v in values])
                )
            elif op == "contains":
                raise LocalQueryError(
                    f"数值列 {name!r} 不支持 contains（文本匹配仅 TEXT 列）"
                )
            elif op == "startswith":
                raise LocalQueryError(
                    f"数值列 {name!r} 不支持 startswith（文本匹配仅 TEXT 列）"
                )
            elif value is None:
                conditions.append(expr.is_(None))
            else:
                number = _as_number(value, column=name)
                conditions.append(
                    {
                        "eq": expr == number,
                        "gt": expr > number,
                        "gte": expr >= number,
                        "lt": expr < number,
                        "lte": expr <= number,
                    }[op]
                )
        else:
            expr = _text_expr(name)
            if op == "contains":
                conditions.append(expr.like(f"%{_as_text(value)}%"))
            elif op == "startswith":
                conditions.append(expr.like(f"{_as_text(value)}%"))
            elif op == "in":
                values = _in_values(flt)
                conditions.append(expr.in_([str(v) for v in values]))
            else:
                text = None if value is None else str(value)
                if op == "eq":
                    conditions.append(expr == text)  # None → IS NULL
                else:
                    conditions.append(
                        {"gt": expr > text, "gte": expr >= text, "lt": expr < text, "lte": expr <= text}[op]
                    )
    return conditions


def _in_values(flt: dict) -> list:
    """in 算子取值：兼容引擎契约的 values 数组与 value 数组两种形态。"""
    values = flt.get("values")
    if values is None:
        values = flt.get("value")
    if not isinstance(values, list) or not values:
        raise LocalQueryError("in 过滤必须带非空 values 数组")
    return values


def _compile_order_by(columns: dict[str, str], sort: list[dict]) -> list:
    order: list = []
    for item in sort or []:
        if not isinstance(item, dict):
            raise LocalQueryError(f"排序项必须是对象（收到 {item!r}）")
        name, dtype = _require_column(columns, item.get("column"), what="排序")
        direction = str(item.get("dir") or "asc").lower()
        if direction not in ("asc", "desc"):
            raise LocalQueryError(f"排序方向必须是 asc/desc（收到 {direction!r}）")
        expr = _typed_expr(name, dtype)
        order.append(expr.desc() if direction == "desc" else expr.asc())
    order.append(DatasetRow.row_index.asc())  # 同键行按建集序（兜底稳定序）
    return order


# ------------------------------------------------------------------ 写入（§2.1）


def ingest_local(
    db: Session, *, dataset: Dataset, path: Path, columns: list[dict]
) -> int:
    """api 层物化：登记列 + 流式写行（复用 uploads 的暂存 CSV 解析）。

    行值经 ``coerce_value`` 按声明 dtype 归一（INTEGER→int、REAL→float、
    TEXT→str、空串→None）；行数闸在途中兜底（超限抛 QuotaExceeded，调用
    方 rollback 即无半写状态）。返回写入行数。
    """
    max_rows = get_settings().free_tier_max_rows
    # 列注册表（先清后写：本入口只服务新建集，清写保持幂等）
    db.execute(sa.delete(DatasetColumn).where(DatasetColumn.dataset_id == dataset.id))
    db.execute(sa.delete(DatasetRow).where(DatasetRow.dataset_id == dataset.id))
    for ordinal, column in enumerate(columns):
        db.add(
            DatasetColumn(
                dataset_id=dataset.id,
                name=str(column.get("name")),
                dtype=str(column.get("type") or uploads.TEXT),
                ordinal=ordinal,
            )
        )
    db.flush()
    total = 0
    size_bytes = 0
    pending = 0
    for chunk in uploads.iter_ingest_chunks(path, columns):
        if total + len(chunk) > max_rows:
            raise service.QuotaExceeded(
                f"行数 {total + len(chunk)} 超过免费档单数据集上限 {max_rows}——"
                "请拆分数据，或升级到基础档及以上套餐"
            )
        for offset, row in enumerate(chunk):
            db.add(DatasetRow(dataset_id=dataset.id, row_index=total + offset, data=row))
            size_bytes += len(json.dumps(row, ensure_ascii=False, default=str))
        total += len(chunk)
        pending += len(chunk)
        if pending >= _WRITE_FLUSH_ROWS:
            db.flush()
            pending = 0
    dataset.row_count = total
    dataset.size_bytes = size_bytes
    return total


def ingest_indicator_rows(
    db: Session, *, dataset: Dataset, columns: list[dict],
    rows: list[dict], size_bytes: int,
) -> int:
    """api 层物化（指标建集变体，wire-indicator-to-dataset）：行已按列形状
    归一（indicator_id/date/value/unit），不再走 CSV 解析。行数闸在途中
    兜底（超限抛 QuotaExceeded，调用方 rollback 即无半写状态）。返回写入行数。
    """
    max_rows = get_settings().free_tier_max_rows
    db.execute(sa.delete(DatasetColumn).where(DatasetColumn.dataset_id == dataset.id))
    db.execute(sa.delete(DatasetRow).where(DatasetRow.dataset_id == dataset.id))
    for ordinal, column in enumerate(columns):
        db.add(
            DatasetColumn(
                dataset_id=dataset.id,
                name=str(column.get("name")),
                dtype=str(column.get("type") or uploads.TEXT),
                ordinal=ordinal,
            )
        )
    db.flush()
    total = 0
    for offset, row in enumerate(rows):
        if total + 1 > max_rows:
            raise service.QuotaExceeded(
                f"行数 {total + 1} 超过免费档单数据集上限 {max_rows}——"
                "请缩小时间范围，或升级到基础档及以上套餐"
            )
        db.add(DatasetRow(dataset_id=dataset.id, row_index=offset, data=row))
        total += 1
    dataset.row_count = total
    dataset.size_bytes = size_bytes
    return total


def delete_local_rows(db: Session, *, dataset_id: str) -> None:
    """删除 api 层行/列/修正覆盖层数据（数据集登记行由调用方删；不留孤儿行）。"""
    db.execute(sa.delete(DatasetRow).where(DatasetRow.dataset_id == dataset_id))
    db.execute(sa.delete(DatasetColumn).where(DatasetColumn.dataset_id == dataset_id))
    db.execute(
        sa.delete(DatasetCorrection).where(DatasetCorrection.dataset_id == dataset_id)
    )


# ------------------------------------------------------------------ schema 预览（§2.4）


def schema_of(db: Session, *, dataset_id: str) -> dict:
    """列/类型/行数统计（dataset_columns 聚合；详情端点与配额条共用）。

    行数口径（wire-dataset-form-editing）：存在未回滚修正时 = 有效行数
    （与 dataset.row_count 同步值、预览 total 同口径）；零修正走基行计数
    （既有快路径行为不变）。
    """
    columns = _column_map(db, dataset_id)
    if has_active_corrections(db, dataset_id):
        rows = _effective_row_count(db, dataset_id)
    else:
        rows = int(
            db.execute(
                select(func.count(DatasetRow.id)).where(
                    DatasetRow.dataset_id == dataset_id
                )
            ).scalar_one()
        )
    return {
        "columns": [
            {"name": name, "type": dtype} for name, dtype in columns.items()
        ],  # _column_map 已按 ordinal 排序
        "row_count": rows,
    }


# ------------------------------------------------------------------ 查询（§2.2）


def execute_query(
    db: Session,
    *,
    dataset_id: str,
    filters: list[dict] | None = None,
    sort: list[dict] | None = None,
    limit: int = 100,
    offset: int = 0,
    count_only: bool = False,
    distinct_column: str | None = None,
) -> dict:
    """本地查询：过滤/排序/分页/count/distinct（响应形态对齐引擎 query）。

    有效视图分流（wire-dataset-form-editing）：数据集存在未回滚修正时在
    Python 合并路径（``_effective_rows``）上重算同一窄面语义；零修正走
    原 SQL 快路径（行为一字不改，零回归）。
    """
    if has_active_corrections(db, dataset_id):
        return _execute_query_merged(
            db,
            dataset_id=dataset_id,
            filters=filters,
            sort=sort,
            limit=limit,
            offset=offset,
            count_only=count_only,
            distinct_column=distinct_column,
        )
    return _execute_query_sql(
        db,
        dataset_id=dataset_id,
        filters=filters,
        sort=sort,
        limit=limit,
        offset=offset,
        count_only=count_only,
        distinct_column=distinct_column,
    )


def _execute_query_sql(
    db: Session,
    *,
    dataset_id: str,
    filters: list[dict] | None = None,
    sort: list[dict] | None = None,
    limit: int = 100,
    offset: int = 0,
    count_only: bool = False,
    distinct_column: str | None = None,
) -> dict:
    """零修正快路径（既有 SQL 执行面——保持原行为，不改一字）。"""
    columns = _column_map(db, dataset_id)
    conditions = _compile_filters(columns, filters or [])
    order = _compile_order_by(columns, sort or [])
    base = sa.select(DatasetRow).where(DatasetRow.dataset_id == dataset_id, *conditions)
    total = int(
        db.execute(
            sa.select(func.count()).select_from(base.subquery())
        ).scalar_one()
    )
    column_cards = [
        {"name": name, "type": dtype} for name, dtype in columns.items()
    ]
    if count_only:
        return {
            "columns": column_cards,
            "rows": [],
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": False,
        }
    if distinct_column is not None:
        name, dtype = _require_column(columns, distinct_column, what="distinct")
        expr = _typed_expr(name, dtype)
        values = [
            _norm_value(dtype, value)
            for (value,) in db.execute(
                sa.select(expr)
                .distinct()
                .where(DatasetRow.dataset_id == dataset_id, *conditions)
                .order_by(expr)
            ).all()
        ]
        return {
            "columns": column_cards,
            "distinct_column": name,
            "values": values,
            "count": len(values),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": False,
        }
    entities = (
        db.execute(base.order_by(*order).offset(offset).limit(limit)).scalars().all()
    )
    rows = []
    for entity in entities:
        row = dict(entity.data or {})
        row["__row_id"] = str(entity.row_index)
        rows.append(row)
    return {
        "columns": column_cards,
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": total > offset + len(rows),
    }


# ------------------------------------------------------------------ 聚合（§2.3）


def _parse_measures(
    columns: dict[str, str], measures: list[dict]
) -> list[tuple[str, str, str, Any]]:
    """度量解析 → [(输出键, fn, 列名, SQL 表达式)]；fn 语法/数值性校验。"""
    parsed: list[tuple[str, str, str, Any]] = []
    for measure in measures or []:
        if not isinstance(measure, dict):
            raise LocalQueryError(f"度量项必须是对象（收到 {measure!r}）")
        fn = str(measure.get("fn") or measure.get("op") or "").lower()
        if fn not in AGGREGATE_FNS:
            raise LocalQueryError(
                f"不支持的聚合函数 {fn!r}（支持: {', '.join(AGGREGATE_FNS)}）"
            )
        name = measure.get("column")
        if fn == "count" and name in (None, "", "*"):
            # 无列 count = 组内行数（按主键计数；ORM 语义下 count(*) 不能
            # 与其他表达式同列输出，行数口径等价）
            parsed.append(("count", "count", "", func.count(DatasetRow.id)))
            continue
        name, dtype = _require_column(columns, name, what="聚合")
        if fn in ("sum", "avg") and dtype not in (uploads.INTEGER, uploads.REAL):
            raise LocalQueryError(
                f"聚合 {fn} 仅支持数值列（列 {name!r} 是 {dtype}）——"
                "文本列可聚合 count/min/max"
            )
        expr = _typed_expr(name, dtype)
        sql_fn = {"count": func.count, "sum": func.sum, "avg": func.avg, "min": func.min, "max": func.max}[fn]
        key = "count" if fn == "count" else f"{name}_{fn}"
        parsed.append((key, fn, name, sql_fn(expr)))
    return parsed


def execute_aggregate(
    db: Session,
    *,
    dataset_id: str,
    group_by: list[str],
    measures: list[dict],
    filters: list[dict] | None = None,
) -> dict:
    """本地聚合：group by ≤3 列 × count/sum/avg/min/max（形态对齐引擎）。

    有效视图分流（wire-dataset-form-editing）：存在未回滚修正时在 Python
    合并路径上重算；零修正走原 SQL 快路径（行为一字不改，零回归）。
    """
    if has_active_corrections(db, dataset_id):
        return _execute_aggregate_merged(
            db,
            dataset_id=dataset_id,
            group_by=group_by,
            measures=measures,
            filters=filters,
        )
    return _execute_aggregate_sql(
        db,
        dataset_id=dataset_id,
        group_by=group_by,
        measures=measures,
        filters=filters,
    )


def _execute_aggregate_sql(
    db: Session,
    *,
    dataset_id: str,
    group_by: list[str],
    measures: list[dict],
    filters: list[dict] | None = None,
) -> dict:
    """零修正快路径（既有 SQL 执行面——保持原行为，不改一字）。"""
    if not group_by:
        raise LocalQueryError("aggregate 必须带 group_by（全局聚合请用单组哨兵列）")
    if len(group_by) > AGGREGATE_MAX_GROUP_COLUMNS:
        raise LocalQueryError(
            f"api 层数据集的聚合分组列数上限为 {AGGREGATE_MAX_GROUP_COLUMNS}"
            f"（收到 {len(group_by)}）；更宽的聚合面需 cell 层/付费档"
        )
    columns = _column_map(db, dataset_id)
    group_specs = [
        _require_column(columns, name, what="group_by") for name in group_by
    ]
    parsed = _parse_measures(columns, measures)
    conditions = _compile_filters(columns, filters or [])
    group_exprs = [_typed_expr(name, dtype) for name, dtype in group_specs]
    stmt = (
        sa.select(*group_exprs, *[item[3] for item in parsed])
        .where(DatasetRow.dataset_id == dataset_id, *conditions)
        .group_by(*group_exprs)
        .order_by(*group_exprs)
        .limit(AGGREGATE_MAX_GROUPS + 1)
    )
    result = db.execute(stmt).all()
    truncated = len(result) > AGGREGATE_MAX_GROUPS
    if truncated:
        result = result[:AGGREGATE_MAX_GROUPS]
    dict(group_specs)
    groups = []
    for row in result:
        group: dict[str, Any] = {}
        for (name, dtype), value in zip(group_specs, row[: len(group_specs)]):
            group[name] = _norm_value(dtype, value)
        for (key, _fn, _name, _expr), value in zip(parsed, row[len(group_specs) :]):
            group[key] = _norm_number_out(value)
        groups.append(group)
    return {
        "group_by": [name for name, _ in group_specs],
        "groups": groups,
        "group_count": len(groups),
        "truncated": truncated,
    }


def _norm_number_out(value: Any) -> Any:
    """数值聚合产出的展示归整（1.0 → 1；None 保持 None）。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ------------------------------------------------------------------ 行预览（控制台）


def preview_rows(
    db: Session, *, dataset_id: str, page: int, page_size: int
) -> dict:
    """控制台行预览（本地执行；形态对齐引擎 preview：columns/rows/total）。

    全量走有效视图合并（wire-dataset-form-editing）：api 档控制台预览必须
    呈现修正后的有效视图（基行 − 未回滚 delete + 未回滚 insert/update 的
    内存合并，基行永不改写）；零修正时合并结果与原基行视图逐字节一致。
    """
    columns = _column_map(db, dataset_id)
    rows = _effective_rows(db, dataset_id)
    start = (page - 1) * page_size
    return {
        "columns": [
            {"name": name, "type": dtype} for name, dtype in columns.items()
        ],
        "rows": rows[start : start + page_size],
        "total": len(rows),
    }


# ------------------------------------------------------------------ 本地修正层（wire-dataset-form-editing）


class CorrectionNotFound(LookupError):
    """修正不存在（或不属于该数据集）——路由层转 404。"""


class CorrectionAlreadyReverted(RuntimeError):
    """修正已回滚过（幂等拒绝二次回滚）——路由层转 409。"""


def has_active_corrections(db: Session, dataset_id: str) -> bool:
    """该数据集是否存在未回滚修正（查询面 SQL 快路径 / Python 合并路径的分流依据）。"""
    return (
        db.execute(
            select(DatasetCorrection.id)
            .where(
                DatasetCorrection.dataset_id == dataset_id,
                DatasetCorrection.reverted_at.is_(None),
            )
            .limit(1)
        ).first()
        is not None
    )


def _active_corrections(db: Session, dataset_id: str) -> list[DatasetCorrection]:
    """未回滚修正（按 id 序 = 落层序；后落覆盖先落）。"""
    return (
        db.execute(
            select(DatasetCorrection)
            .where(
                DatasetCorrection.dataset_id == dataset_id,
                DatasetCorrection.reverted_at.is_(None),
            )
            .order_by(DatasetCorrection.id)
        )
        .scalars()
        .all()
    )


def _effective_rows(db: Session, dataset_id: str) -> list[dict]:
    """有效视图：基行（row_index 序）+ 未回滚修正的内存合并。

    纪律（wire-dataset-form-editing，镜像引擎覆盖层语义）：
    - 基行**永不改写**——合并只在内存视图上进行，云端不破坏基行、永不丢
      原始数据；
    - deletes：锚集合剔除（基行锚 = row_index 字符串，插入行锚 = "c{id}"，
      两者都可作为删除锚）；
    - updates：(锚, 列) → 值，按落层序后写覆盖先写（同格多次改值取最新）；
    - inserts：按修正 id 序追加，合成 ``__row_id="c{correction_id}"``
      （基行 ``__row_id``=row_index 字符串既有口径）。
    """
    corrections = _active_corrections(db, dataset_id)
    entities = (
        db.execute(
            sa.select(DatasetRow)
            .where(DatasetRow.dataset_id == dataset_id)
            .order_by(DatasetRow.row_index)
        )
        .scalars()
        .all()
    )
    rows: list[dict] = []
    for entity in entities:
        row = dict(entity.data or {})
        row["__row_id"] = str(entity.row_index)
        rows.append(row)
    deleted: set[str] = set()
    updates_by_anchor: dict[str, dict[str, Any]] = {}
    inserts: list[dict] = []
    for correction in corrections:
        if correction.op == "delete":
            if correction.row_anchor:
                deleted.add(correction.row_anchor)
        elif correction.op == "update":
            cell_updates = updates_by_anchor.setdefault(correction.row_anchor or "", {})
            if correction.column_name is not None:
                cell_updates[correction.column_name] = correction.value_json
        elif correction.op == "insert":
            row = dict(correction.value_json or {})
            row["__row_id"] = f"c{correction.id}"
            inserts.append(row)
    merged = [row for row in rows if row["__row_id"] not in deleted]
    merged.extend(row for row in inserts if row["__row_id"] not in deleted)
    for row in merged:
        cell_updates = updates_by_anchor.get(row["__row_id"])
        if cell_updates:
            row.update(cell_updates)
    return merged


def _effective_row_count(db: Session, dataset_id: str) -> int:
    """有效行数 = 有效视图长度（基行 − 未回滚 delete + 未回滚 insert）。"""
    return len(_effective_rows(db, dataset_id))


def apply_local_correction(
    db: Session,
    *,
    dataset: Dataset,
    op: str,
    row_anchor: str | None = None,
    column_name: str | None = None,
    value: Any = None,
    actor: str,
) -> int:
    """落一条本地修正（返回 correction_id；insert 时 value=整行 dict）。

    - 列校验：update 的 column / insert 的 values 键必须在列图，未知列
      LocalQueryError（路由层转 400「列不存在: X」）；
    - 悬空语义：update/delete 的锚行不在基表也**允许落层**（列表时标注
      dangling），与引擎悬空语义一致——不 400、不静默丢弃；
    - 行数闸：insert 使有效行数超免费档单集上限（free_tier_max_rows）
      → service.QuotaExceeded（路由层转 429），落行前判定、零部分执行；
    - 值按列 dtype 归一（与 ingest 同一 coerce_value 语义，保证合并路径
      的类型化比较口径一致）；
    - 写后同步 dataset.row_count = 有效行数，随后 commit。
    """
    columns = _column_map(db, dataset.id)
    anchor = row_anchor if isinstance(row_anchor, str) and row_anchor else None
    if op == "update":
        if not anchor:
            raise LocalQueryError("update 必须带 row_anchor（行锚）")
        if not isinstance(column_name, str) or column_name not in columns:
            raise LocalQueryError(f"列不存在: {column_name}")
        record = DatasetCorrection(
            dataset_id=dataset.id,
            op="update",
            row_anchor=anchor,
            column_name=column_name,
            value_json=uploads.coerce_value(value, columns[column_name]),
            actor=actor,
        )
    elif op == "delete":
        if not anchor:
            raise LocalQueryError("delete 必须带 row_anchor（行锚）")
        record = DatasetCorrection(
            dataset_id=dataset.id,
            op="delete",
            row_anchor=anchor,
            column_name=None,
            value_json=None,
            actor=actor,
        )
    elif op == "insert":
        if not isinstance(value, dict) or not value:
            raise LocalQueryError("insert 必须带 values（整行 dict）")
        unknown = sorted(
            key for key in value if not isinstance(key, str) or key not in columns
        )
        if unknown:
            raise LocalQueryError(f"列不存在: {unknown[0]}")
        limit = get_settings().free_tier_max_rows
        current = _effective_row_count(db, dataset.id)
        if current + 1 > limit:
            # 行数闸：先于任何落行判定（调用方 rollback 即无半写状态）
            raise service.QuotaExceeded(
                f"行数 {current + 1} 超过免费档单数据集上限 {limit}——"
                "请拆分数据，或升级到基础档及以上套餐"
            )
        record = DatasetCorrection(
            dataset_id=dataset.id,
            op="insert",
            row_anchor=None,
            column_name=None,
            value_json={
                key: uploads.coerce_value(val, columns[key])
                for key, val in value.items()
            },
            actor=actor,
        )
    else:
        raise LocalQueryError(f"未知修正操作 {op!r}（支持: update/delete/insert）")
    db.add(record)
    db.flush()  # 先取 record.id（插入行合成锚 c{id} 要引用）
    dataset.row_count = _effective_row_count(db, dataset.id)
    db.commit()
    return record.id


def list_local_corrections(
    db: Session,
    dataset_id: str,
    *,
    include_reverted: bool = True,
    include_dangling: bool = True,
) -> list[dict]:
    """修正历史（字段名镜像 web 客户端 Correction 类型；审计留痕不静默丢弃）。

    - insert 的 row_id=null、values=整行；update 的 row_id=row_anchor +
      column/value=单值；delete 的 row_id=row_anchor；
    - dangling（悬空）：非 insert 修正的锚行既不在基表、也解析不到未回滚
      插入行 → dangling_at/dangling_reason 如实标注；
    - 回滚修正保留呈现（reverted_at 置位），include_reverted=False 时过滤。
    """
    corrections = (
        db.execute(
            select(DatasetCorrection)
            .where(DatasetCorrection.dataset_id == dataset_id)
            .order_by(DatasetCorrection.id.desc())
        )
        .scalars()
        .all()
    )
    active = [c for c in corrections if c.reverted_at is None]
    # 可解析锚集合：基行 row_index 字符串 + 未回滚插入行合成锚（插入行即使
    # 又被删除，锚仍可解析——dangling 只标真正无处可指的锚）
    base_anchors = {
        str(index)
        for (index,) in db.execute(
            select(DatasetRow.row_index).where(DatasetRow.dataset_id == dataset_id)
        ).all()
    }
    live_insert_anchors = {f"c{c.id}" for c in active if c.op == "insert"}
    resolvable = base_anchors | live_insert_anchors
    items: list[dict] = []
    for c in corrections:
        dangling = (
            c.reverted_at is None
            and c.op != "insert"
            and (c.row_anchor or "") not in resolvable
        )
        if not include_reverted and c.reverted_at is not None:
            continue
        if not include_dangling and dangling:
            continue
        items.append(
            {
                "id": c.id,
                "dataset_id": c.dataset_id,
                "op": c.op,
                "row_id": c.row_anchor,
                "column": c.column_name if c.op == "update" else None,
                "value": c.value_json if c.op == "update" else None,
                "values": c.value_json if c.op == "insert" else None,
                "actor": c.actor,
                "channel": "console",
                "created_at": c.created_at,
                "reverted_at": c.reverted_at,
                "dangling_at": c.created_at if dangling else None,
                "dangling_reason": "锚行不在基表" if dangling else None,
            }
        )
    return items


def revert_local_correction(
    db: Session, *, dataset: Dataset, correction_id: int
) -> None:
    """单条回滚：仅置 reverted_at（修正留档、永不删行），同步有效行数。

    不存在/不属于本数据集 → CorrectionNotFound（路由层 404）；
    已回滚再回滚 → CorrectionAlreadyReverted（路由层 409，幂等拒绝）。
    """
    correction = db.execute(
        select(DatasetCorrection).where(
            DatasetCorrection.id == int(correction_id),
            DatasetCorrection.dataset_id == dataset.id,
        )
    ).scalar_one_or_none()
    if correction is None:
        raise CorrectionNotFound(f"修正 {correction_id} 不存在")
    if correction.reverted_at is not None:
        raise CorrectionAlreadyReverted(
            f"修正 {correction_id} 已回滚过（不能重复回滚）"
        )
    correction.reverted_at = service.utcnow()
    db.flush()  # 会话 autoflush=False：先落 reverted_at 再重算有效视图
    dataset.row_count = _effective_row_count(db, dataset.id)
    db.commit()


# ------------------------------------------------------------------ 合并路径（Python 重算窄面）


def _py_number(value: Any) -> float | None:
    """数值单元格的 Python 取值（不可数值化的残留原值按 NULL 口径处理）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compile_py_filters(
    columns: dict[str, str], filters: list[dict]
) -> list[Any]:
    """Python 合并路径的过滤谓词（校验与类型口径照 ``_compile_filters`` 对齐：
    数值列数值比较、文本列字符串比较、contains/startswith 仅 TEXT）。"""
    predicates: list[Any] = []
    for flt in filters or []:
        if not isinstance(flt, dict):
            raise LocalQueryError(f"过滤项必须是对象（收到 {flt!r}）")
        op = str(flt.get("op") or "")
        if op not in SUPPORTED_FILTER_OPS:
            raise LocalQueryError(
                f"api 层数据集不支持过滤算子 {op!r}"
                f"（支持: {', '.join(SUPPORTED_FILTER_OPS)}；完整算子面需 cell 层/付费档）"
            )
        name, dtype = _require_column(columns, flt.get("column"), what="过滤")
        value = flt.get("value")
        if dtype in (uploads.INTEGER, uploads.REAL):
            if op == "in":
                numbers = [_as_number(v, column=name) for v in _in_values(flt)]

                def in_numeric(row: dict, n: str = name, ns: list[float] = numbers) -> bool:
                    number = _py_number(row.get(n))
                    return number is not None and number in ns

                predicates.append(in_numeric)
            elif op == "contains":
                raise LocalQueryError(
                    f"数值列 {name!r} 不支持 contains（文本匹配仅 TEXT 列）"
                )
            elif op == "startswith":
                raise LocalQueryError(
                    f"数值列 {name!r} 不支持 startswith（文本匹配仅 TEXT 列）"
                )
            elif value is None:

                def is_null(row: dict, n: str = name) -> bool:
                    return row.get(n) is None

                predicates.append(is_null)
            else:
                number = _as_number(value, column=name)
                compare = {
                    "eq": lambda a, b: a == b,
                    "gt": lambda a, b: a > b,
                    "gte": lambda a, b: a >= b,
                    "lt": lambda a, b: a < b,
                    "lte": lambda a, b: a <= b,
                }[op]

                def numeric_cmp(row: dict, n: str = name, num: float = number) -> bool:
                    cell = _py_number(row.get(n))
                    return cell is not None and compare(cell, num)

                predicates.append(numeric_cmp)
        else:
            if op == "contains":
                text = _as_text(value)

                def contains(row: dict, n: str = name, t: str = text) -> bool:
                    cell = row.get(n)
                    return cell is not None and t in str(cell)

                predicates.append(contains)
            elif op == "startswith":
                text = _as_text(value)

                def startswith(row: dict, n: str = name, t: str = text) -> bool:
                    cell = row.get(n)
                    return cell is not None and str(cell).startswith(t)

                predicates.append(startswith)
            elif op == "in":
                texts = [str(v) for v in _in_values(flt)]

                def in_text(row: dict, n: str = name, ts: list[str] = texts) -> bool:
                    cell = row.get(n)
                    return cell is not None and str(cell) in ts

                predicates.append(in_text)
            else:
                text = None if value is None else str(value)
                if op == "eq":

                    def eq_text(row: dict, n: str = name, t: str | None = text) -> bool:
                        cell = row.get(n)
                        return cell is None if t is None else cell is not None and str(cell) == t

                    predicates.append(eq_text)
                else:
                    compare = {
                        "gt": lambda a, b: a > b,
                        "gte": lambda a, b: a >= b,
                        "lt": lambda a, b: a < b,
                        "lte": lambda a, b: a <= b,
                    }[op]

                    def text_cmp(row: dict, n: str = name, t: str = text) -> bool:
                        cell = row.get(n)
                        return cell is not None and compare(str(cell), t)

                    predicates.append(text_cmp)
    return predicates


def _py_sort_key(dtype: str, name: str):
    """合并路径的排序键（数值/文本按 dtype 取值；None 按 SQLite 口径
    asc 在最前、desc 在最后——不可数值化残留与 CAST(... AS FLOAT)=0 同口径）。"""
    if dtype in (uploads.INTEGER, uploads.REAL):
        return lambda row: (row.get(name) is not None, _py_number(row.get(name)) or 0.0)
    return lambda row: (
        row.get(name) is not None,
        "" if row.get(name) is None else str(row.get(name)),
    )


def _py_sort(rows: list[dict], columns: dict[str, str], sort: list[dict]) -> list[dict]:
    """合并路径排序（校验照 ``_compile_order_by``；末位键优先稳定叠加，
    同键行保持有效视图序 = SQL 路径的 row_index 兜底稳定序）。"""
    specs: list[tuple[str, str, str]] = []
    for item in sort or []:
        if not isinstance(item, dict):
            raise LocalQueryError(f"排序项必须是对象（收到 {item!r}）")
        name, dtype = _require_column(columns, item.get("column"), what="排序")
        direction = str(item.get("dir") or "asc").lower()
        if direction not in ("asc", "desc"):
            raise LocalQueryError(f"排序方向必须是 asc/desc（收到 {direction!r}）")
        specs.append((name, dtype, direction))
    ordered = rows
    for name, dtype, direction in reversed(specs):
        ordered = sorted(
            ordered, key=_py_sort_key(dtype, name), reverse=(direction == "desc")
        )
    return ordered


def _execute_query_merged(
    db: Session,
    *,
    dataset_id: str,
    filters: list[dict] | None = None,
    sort: list[dict] | None = None,
    limit: int = 100,
    offset: int = 0,
    count_only: bool = False,
    distinct_column: str | None = None,
) -> dict:
    """有未回滚修正时的查询执行面：在有效视图上重算过滤/排序/分页/
    count/distinct（响应字段与 SQL 快路径逐项一致）。"""
    columns = _column_map(db, dataset_id)
    column_cards = [{"name": name, "type": dtype} for name, dtype in columns.items()]
    predicates = _compile_py_filters(columns, filters or [])
    matched = [
        row
        for row in _effective_rows(db, dataset_id)
        if all(predicate(row) for predicate in predicates)
    ]
    total = len(matched)
    if count_only:
        return {
            "columns": column_cards,
            "rows": [],
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": False,
        }
    if distinct_column is not None:
        name, dtype = _require_column(columns, distinct_column, what="distinct")
        cells: set = set()
        for row in matched:
            cell = row.get(name)
            if dtype in (uploads.INTEGER, uploads.REAL):
                cell = _py_number(cell)
            elif cell is not None:
                cell = str(cell)
            cells.add(cell)
        # 与 SQL 面同口径：distinct + order by（None 排最前，SQLite 口径）
        values = [
            _norm_value(dtype, cell)
            for cell in sorted(
                cells, key=lambda v: (v is not None, v if v is not None else 0)
            )
        ]
        return {
            "columns": column_cards,
            "distinct_column": name,
            "values": values,
            "count": len(values),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": False,
        }
    ordered = _py_sort(matched, columns, sort or [])
    page = ordered[offset : offset + limit]
    return {
        "columns": column_cards,
        "rows": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": total > offset + len(page),
    }


def _parse_measures_py(
    columns: dict[str, str], measures: list[dict]
) -> list[tuple[str, str, str | None]]:
    """合并路径的度量解析 → [(输出键, fn, 列名)]；校验与报文照
    ``_parse_measures`` 对齐（fn 白名单、sum/avg 仅数值列）。"""
    parsed: list[tuple[str, str, str | None]] = []
    for measure in measures or []:
        if not isinstance(measure, dict):
            raise LocalQueryError(f"度量项必须是对象（收到 {measure!r}）")
        fn = str(measure.get("fn") or measure.get("op") or "").lower()
        if fn not in AGGREGATE_FNS:
            raise LocalQueryError(
                f"不支持的聚合函数 {fn!r}（支持: {', '.join(AGGREGATE_FNS)}）"
            )
        name = measure.get("column")
        if fn == "count" and name in (None, "", "*"):
            parsed.append(("count", "count", None))  # 无列 count = 组内行数
            continue
        name, dtype = _require_column(columns, name, what="聚合")
        if fn in ("sum", "avg") and dtype not in (uploads.INTEGER, uploads.REAL):
            raise LocalQueryError(
                f"聚合 {fn} 仅支持数值列（列 {name!r} 是 {dtype}）——"
                "文本列可聚合 count/min/max"
            )
        key = "count" if fn == "count" else f"{name}_{fn}"
        parsed.append((key, fn, name))
    return parsed


def _py_group_cell(row: dict, name: str, dtype: str) -> Any:
    """分组的类型化取值（数值列取 float 使 1 与 1.0 同组——与 SQL CAST 口径
    一致；文本列取 str；缺失/空值统一 None 成独立组）。"""
    cell = row.get(name)
    if cell is None:
        return None
    if dtype in (uploads.INTEGER, uploads.REAL):
        return _py_number(cell)
    return str(cell)


def _execute_aggregate_merged(
    db: Session,
    *,
    dataset_id: str,
    group_by: list[str],
    measures: list[dict],
    filters: list[dict] | None = None,
) -> dict:
    """有未回滚修正时的聚合执行面：分组列数闸/度量校验与 SQL 面同款，
    聚合在有效视图上重算（group_by 升序、组数截断口径对齐）。"""
    if not group_by:
        raise LocalQueryError("aggregate 必须带 group_by（全局聚合请用单组哨兵列）")
    if len(group_by) > AGGREGATE_MAX_GROUP_COLUMNS:
        raise LocalQueryError(
            f"api 层数据集的聚合分组列数上限为 {AGGREGATE_MAX_GROUP_COLUMNS}"
            f"（收到 {len(group_by)}）；更宽的聚合面需 cell 层/付费档"
        )
    columns = _column_map(db, dataset_id)
    group_specs = [
        _require_column(columns, name, what="group_by") for name in group_by
    ]
    parsed = _parse_measures_py(columns, measures)
    predicates = _compile_py_filters(columns, filters or [])
    matched = [
        row
        for row in _effective_rows(db, dataset_id)
        if all(predicate(row) for predicate in predicates)
    ]
    buckets: dict[tuple, list[dict]] = {}
    for row in matched:
        key = tuple(
            _py_group_cell(row, name, dtype) for name, dtype in group_specs
        )
        buckets.setdefault(key, []).append(row)
    # 组序对齐 SQL 面：按分组键升序（None 在前，SQLite 口径）
    ordered_keys = sorted(
        buckets,
        key=lambda key: tuple(
            (cell is not None, cell if cell is not None else 0) for cell in key
        ),
    )
    truncated = len(ordered_keys) > AGGREGATE_MAX_GROUPS
    if truncated:
        ordered_keys = ordered_keys[:AGGREGATE_MAX_GROUPS]
    dict(group_specs)
    groups = []
    for key in ordered_keys:
        bucket = buckets[key]
        group: dict[str, Any] = {
            name: _norm_value(dtype, cell)
            for (name, dtype), cell in zip(group_specs, key)
        }
        for out_key, fn, name in parsed:
            if fn == "count" and name is None:
                group[out_key] = _norm_number_out(len(bucket))
                continue
            cells = [row.get(name) for row in bucket]
            if fn == "count":
                group[out_key] = _norm_number_out(
                    sum(1 for cell in cells if cell is not None)
                )
                continue
            values = [cell for cell in cells if cell is not None]
            if not values:
                group[out_key] = None  # 空组聚合与 SQL NULL 口径一致
                continue
            if fn in ("sum", "avg"):
                numbers = [_py_number(v) for v in values]
                numbers = [n for n in numbers if n is not None]
                if not numbers:
                    group[out_key] = None
                    continue
                total = sum(numbers)
                group[out_key] = _norm_number_out(
                    total if fn == "sum" else total / len(numbers)
                )
            elif fn == "min":
                group[out_key] = _norm_number_out(min(values, key=_measure_sort_key))
            else:  # max
                group[out_key] = _norm_number_out(max(values, key=_measure_sort_key))
        groups.append(group)
    return {
        "group_by": [name for name, _ in group_specs],
        "groups": groups,
        "group_count": len(groups),
        "truncated": truncated,
    }


def _measure_sort_key(value: Any) -> tuple:
    """min/max 的跨型排序键（数值按数值序、文本按字典序；None 已剔除）。"""
    number = _py_number(value)
    if number is not None:
        return (0, number, "")
    return (1, 0.0, str(value))
