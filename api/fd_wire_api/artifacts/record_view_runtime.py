"""record-view 取数组装的服务端唯一出处（wire-record-view 任务 3.2/4.2）。

公开取数端点（``GET /api/v1/share/record-view/{id}?t&v&k``，钉死版本 spec）
与 console 试预览端点（``POST /api/v1/artifacts/{id}/record-view/preview``，
当前版本 spec）**共用同一组装函数** :func:`build_record_view_payload`——
预览所见即扫码所得（杜绝两套口径，照 form_runtime 的共享执法点先例）。

读边界（spec「公开实时取数端点与读边界」——代码即契约）：

- 仅执行 spec 声明的查询：主记录「keyColumn 等值、limit 1」+ 每 section
  「声明 filters（``{fromField}`` 以主记录行值代入）+ dateColumn 升序 +
  ≤limit 行」——绝不开放任意查询/任意列/任意数据集透传；
- 主记录等值查零行 → ``{"found": False}``（200 非错误，页面如实呈现
  「无此记录」，不泄露记录是否曾存在）；
- 多行命中取首行（查询面稳定序：api 档 row_index 兜底序，引擎面其原生
  稳定序）；k 直接作 eq value 传入 :func:`query_contract._query_dataset`
  （tier 分流：免费档本地有效视图含修正、cell 档引擎 TOOL_QUERY），
  类型语义交给查询层；
- header = 主记录行 headerFields 值（label 缺省=列名，value 原样 JSON 值）；
- 每 section 查询失败（列漂移/集删/引擎失败）= **单段 error 字段 + 空
  rows**（不静默空段），其余 section 与头部照常组装；
- 升序 = 服务端按 dateColumn 排序（查询面 typed 排序）+ 页内稳定重排兜底
  （tier 无关地钉死「日期形值升序在前、空/非法日期值排末尾稳定序」——
  查询层不保证该语义，Python 侧补齐）；截断如实：底层查 ``limit+1``，
  超出 limit 即 truncated（与 optionsFromDataset 截断判定同手法）。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..datasets import api_store
from ..datasets.ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
)
from .query_contract import _query_dataset
from .record_view_spec import (
    _DATE_PREFIX_RE,
    SECTION_LIMIT_DEFAULT,
    SECTION_LIMIT_MAX,
)


def _section_limit(section: dict) -> int:
    """section.limit 的运行时夹取（保存校验已拦越界——防御位，不放大
    也不归零）。"""
    raw = section.get("limit")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return SECTION_LIMIT_DEFAULT
    return max(1, min(raw, SECTION_LIMIT_MAX))


def _substitute_filters(filters: object, main_row: dict) -> list[dict]:
    """section filters 的 ``{fromField}`` 值代入：引用形态取主记录行该列值
    （缺键/null 原样按 null 过滤——如实），字面量值原样透传。"""
    substituted: list[dict] = []
    for flt in filters if isinstance(filters, list) else []:
        if not isinstance(flt, dict):
            continue
        value = flt.get("value")
        if isinstance(value, dict) and isinstance(value.get("fromField"), str):
            value = main_row.get(value["fromField"])
        substituted.append(
            {"column": flt.get("column"), "op": flt.get("op"), "value": value}
        )
    return substituted


def _result_rows(result: object) -> list[dict] | None:
    """查询响应 → 行 dict 列表（dict 行原样；数组行按列卡序转 dict——
    引擎面防御位）；形态异常返回 None。"""
    if not isinstance(result, dict):
        return None
    rows = result.get("rows")
    if not isinstance(rows, list):
        return None
    names = [
        card.get("name")
        for card in (result.get("columns") if isinstance(result.get("columns"), list) else [])
        if isinstance(card, dict) and isinstance(card.get("name"), str)
    ]
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, list):
            out.append(
                {name: (row[index] if index < len(row) else None) for index, name in enumerate(names)}
            )
        else:
            return None
    return out


def _sort_rows_by_date(rows: list[dict], date_column: str) -> list[dict]:
    """页内稳定重排（契约钉死，tier 无关——引擎/本地排序语义不齐时兜底）：

    dateColumn 值为日期形（``YYYY-MM-DD`` 前缀字符串）的行按值升序在前，
    空/非法日期值沉底（同组保持查询面底层序——稳定排序）。
    """
    def sort_key(row: dict) -> tuple:
        value = row.get(date_column)
        if isinstance(value, str) and _DATE_PREFIX_RE.match(value):
            return (0, value)
        return (1,)

    return sorted(rows, key=sort_key)


def _query_main_record(
    datasets_db: Session,
    *,
    inner: dict,
    key: str,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> dict | None:
    """主记录等值查（limit 1）：返回行 dict 或 None（零行）。执行错误
    （集删/列漂移/引擎失败/引擎 error 信封）原样上抛 EngineCallError——
    由调用方按各自面转译（公开端点统一 404/502、preview 如实 4xx）。"""
    result = _query_dataset(
        datasets_db,
        dataset_id=inner["datasetId"],
        customer_id=customer_id,
        query={
            "filters": [
                {"column": inner["keyColumn"], "op": "eq", "value": key}
            ],
            "sort": [],
            "limit": 1,
            "columns": None,
        },
        engine_factory=engine_factory,
    )
    if isinstance(result, dict) and result.get("error"):
        raise EngineCallError(str(result["error"]))
    rows = _result_rows(result)
    if rows is None:
        raise EngineCallError("主记录查询返回形态异常")
    return rows[0] if rows else None


def _build_section(
    datasets_db: Session,
    *,
    section: dict,
    main_row: dict,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> dict:
    """单个 section 组装：``{title, dateColumn, rows, truncated}``，失败时
    ``{title, dateColumn, rows: [], truncated: False, error}``（单段失败
    不拖垮其余段——spec「引用失效如实」场景）。"""
    title = section.get("title")
    date_column = section.get("dateColumn")
    limit = _section_limit(section)
    error: str | None = None
    rows: list[dict] = []
    truncated = False
    try:
        result = _query_dataset(
            datasets_db,
            dataset_id=section.get("datasetId"),
            customer_id=customer_id,
            query={
                "filters": _substitute_filters(section.get("filters"), main_row),
                "sort": [{"column": date_column, "dir": "asc"}],
                # limit+1：截断可判定（与 optionsFromDataset 同手法）
                "limit": limit + 1,
                "columns": None,
            },
            engine_factory=engine_factory,
        )
        if isinstance(result, dict) and result.get("error"):
            error = str(result["error"])
        else:
            fetched = _result_rows(result)
            if fetched is None:
                error = "查询返回形态异常"
            else:
                truncated = len(fetched) > limit
                rows = _sort_rows_by_date(fetched[:limit], date_column)
    except HTTPException as exc:  # _dataset_or_404：集删/他企（不泄露存在性）
        error = f"引用数据集不可用（{exc.detail}）"
    except (api_store.LocalQueryError, EngineUnavailable, EngineCallError) as exc:
        # 列漂移（dateColumn/过滤列不在快照）、免费窄面拒、引擎失败——单段
        # error 如实标注（不静默空段）
        error = str(exc)
    payload: dict[str, Any] = {
        "title": title,
        "dateColumn": date_column,
        "rows": rows,
        "truncated": truncated,
    }
    if error is not None:
        payload["error"] = error
    return payload


def build_record_view_payload(
    inner: dict,
    key: str,
    *,
    datasets_db: Session,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> dict:
    """公开取数与 console 试预览共用的组装函数（同一出处）。

    入参 ``inner`` = spec 内层 ``{datasetId, keyColumn, headerFields,
    sections}``（公开端点传钉死版本、preview 传当前版本——调用方保证已过
    保存校验形状；未过形状由调用方先行 404/422）。返回：

    - 零行命中 → ``{"found": False}``（无 header/sections 键）；
    - 命中 → ``{"found": True, "header": [{label, column, value}],
      "sections": [{title, dateColumn, rows, truncated, (error)}]}``。

    主记录查询失败原样上抛（HTTPException/LocalQueryError/EngineUnavailable/
    EngineCallError）——公开端点与 preview 各自按面转译；section 查询失败
    吞为单段 error。
    """
    main_row = _query_main_record(
        datasets_db,
        inner=inner,
        key=key,
        customer_id=customer_id,
        engine_factory=engine_factory,
    )
    if main_row is None:
        return {"found": False}
    header: list[dict] = []
    raw_header = inner.get("headerFields")
    for field in raw_header if isinstance(raw_header, list) else []:
        if not isinstance(field, dict):
            continue
        column = field.get("column")
        if not isinstance(column, str):
            continue
        label = field.get("label")
        header.append(
            {
                "label": label if isinstance(label, str) and label.strip() else column,
                "column": column,
                "value": main_row.get(column),  # 原样 JSON 值（缺列=None）
            }
        )
    raw_sections = inner.get("sections")
    sections_list = raw_sections if isinstance(raw_sections, list) else []
    sections = [
        _build_section(
            datasets_db,
            section=section,
            main_row=main_row,
            customer_id=customer_id,
            engine_factory=engine_factory,
        )
        for section in sections_list
        if isinstance(section, dict)
    ]
    return {"found": True, "header": header, "sections": sections}
