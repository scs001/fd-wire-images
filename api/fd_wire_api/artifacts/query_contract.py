"""看板查询契约：确定性刷新（wire-dashboard-query-contract 任务 1.1）。

面板可选 ``query`` 引用（与数据面 query 端点入参同形同界）：
``{dataset_id, filters?, sort?, limit?, columns?}``——刷新时 ≥1 面板带
合法 query 即走**契约路径**：逐面板确定性执行查询（零 LLM），快照按
契约归一写回。三段分工：

- :func:`parse_panel_query`：宽容解析/归一——任一字段非法 → 整个 query
  丢弃（None），面板不受影响（与 web 端 normalizePanelQuery 同界）；
- :func:`contract_refresh_plan`：从 spec（信封/本体两种形状）提取合法
  契约面板计划；
- :func:`refresh_spec_with_contracts`：确定性执行——api 档（免费档）
  本地 ``api_store.execute_query``（零引擎调用），cell 档经引擎
  ``customer_dataset_query``（EngineClientFactory 注入）；面板级失败
  隔离，全部契约面板失败抛 :class:`~.refresh.RefreshFailed`（零半写）。
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..datasets import api_store, service as datasets_service
from ..datasets.ingest import (
    EngineClientFactory,
    EngineCallError,
    EngineUnavailable,
    TOOL_QUERY,
)
from ..datasets.routes import _dataset_or_404
from .refresh import RefreshFailed

#: 契约过滤算子白名单（与数据面 query 入参同界；复用 api_store 的窄面集合）
_FILTER_OPS = frozenset(api_store.SUPPORTED_FILTER_OPS)
#: 排序方向白名单
_SORT_DIRS = frozenset(("asc", "desc"))
#: 结构上限（与数据面 QueryIn 的 max_length 同界；超限视为非法整体丢弃）
_MAX_FILTERS = 50
_MAX_SORT = 10
#: limit 夹取区间（数据面同界；越界夹取不拒收）
_LIMIT_FLOOR = 1
_LIMIT_CEIL = 1000


def parse_panel_query(raw: object) -> dict | None:
    """宽容解析面板 query 引用并归一；任一字段非法 → 返回 None（不抛）。

    归一口径：filters 仅保留 column/op/value（value 任意、缺键视同 null）、
    sort 缺 dir 补 asc、limit 夹取 1..1000、columns 去重保序（空列表视同
    缺省=全列）。dataset_id 必须是非空字符串。
    """
    if not isinstance(raw, dict):
        return None
    dataset_id = raw.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return None

    filters_raw = raw.get("filters")
    if filters_raw is None:
        filters_raw = []
    if not isinstance(filters_raw, list) or len(filters_raw) > _MAX_FILTERS:
        return None
    filters: list[dict] = []
    for flt in filters_raw:
        if not isinstance(flt, dict):
            return None
        column, op = flt.get("column"), flt.get("op")
        if not isinstance(column, str) or not column:
            return None
        if not isinstance(op, str) or op not in _FILTER_OPS:
            return None
        filters.append({"column": column, "op": op, "value": flt.get("value")})

    sort_raw = raw.get("sort")
    if sort_raw is None:
        sort_raw = []
    if not isinstance(sort_raw, list) or len(sort_raw) > _MAX_SORT:
        return None
    sort: list[dict] = []
    for item in sort_raw:
        if not isinstance(item, dict):
            return None
        column = item.get("column")
        if not isinstance(column, str) or not column:
            return None
        # 缺 dir 补 asc（与 api_store/引擎侧的缺省方向一致）
        direction = item.get("dir") or "asc"
        if direction not in _SORT_DIRS:
            return None
        sort.append({"column": column, "dir": direction})

    limit_raw = raw.get("limit")
    if limit_raw is None:
        limit = 100
    elif isinstance(limit_raw, bool) or not isinstance(limit_raw, (int, float)):
        return None
    elif isinstance(limit_raw, float):
        if not limit_raw.is_integer():
            return None
        limit = int(limit_raw)
    else:
        limit = limit_raw
    limit = max(_LIMIT_FLOOR, min(_LIMIT_CEIL, limit))

    columns_raw = raw.get("columns")
    if columns_raw is None:
        columns_raw = []
    if not isinstance(columns_raw, list):
        return None
    columns: list[str] = []
    for name in columns_raw:
        if not isinstance(name, str) or not name:
            return None
        if name not in columns:  # 去重保序（投影列序=快照列序，重复无意义）
            columns.append(name)

    return {
        "dataset_id": dataset_id,
        "filters": filters,
        "sort": sort,
        "limit": limit,
        # 空 columns 归 None=缺省全列（按响应列序投影）
        "columns": columns or None,
    }


def _panel_entries(spec: object) -> list | None:
    """spec（信封/本体两种形状，参照 web parseDashboardSpec 宽容口径）
    → 面板数组；无面板数组返回 None。"""
    if not isinstance(spec, dict):
        return None
    inner = spec.get("spec")
    if isinstance(inner, dict) and isinstance(inner.get("panels"), list):
        return inner["panels"]
    if isinstance(spec.get("panels"), list):
        return spec["panels"]
    return None


def contract_refresh_plan(spec: object) -> list[tuple[int, dict, dict]]:
    """提取契约刷新计划：[(panel_index, panel, query)]（仅带合法 query 的面板）。

    panels 非数组（旧自由 JSON / legacy）→ 空计划（刷新走既有 AI 路径）。
    **raw_option 面板不入计划**（wire-chart-contract-v2）：其数据自含于图表
    配置本身（无独立 snapshot 列），查询契约无从回写，刷新时按 kept 保留。
    """
    panels = _panel_entries(spec)
    if panels is None:
        return []
    plan: list[tuple[int, dict, dict]] = []
    for index, panel in enumerate(panels):
        if not isinstance(panel, dict):
            continue
        if panel.get("raw_option") is not None:
            continue
        query = parse_panel_query(panel.get("query"))
        if query is not None:
            plan.append((index, panel, query))
    return plan


def _panel_id(panel: dict, index: int) -> str:
    """面板标识（响应 panel_id 用）：缺 id 的面板按序号给稳定回退标识。"""
    panel_id = panel.get("id")
    return panel_id if isinstance(panel_id, str) and panel_id else f"panel-{index}"


def _norm_cell(value: Any) -> Any:
    """快照值归一：None/数值/字符串原样，bool 与其余类型降格 str(v) 不丢数据。

    快照契约值域是 str|number|null（web 渲染器 DashboardSnapshot）；bool
    直接 JSON 序列化会被部分图表组件当 0/1 渲染，降格字符串保真呈现
    （bool 判定必须先于 int——bool 是 int 的子类）。
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _query_dataset(
    db: Session,
    *,
    dataset_id: str,
    customer_id: str,
    query: dict,
    engine_factory: EngineClientFactory,
    distinct_column: str | None = None,
) -> dict:
    """按数据集档位执行一次契约查询，返回引擎/本地查询响应（columns+rows；
    ``distinct_column`` 给定时返回 distinct 值响应——wire-form-entry-
    advanced 动态选项解析复用，语义与数据面 query 端点同口径）。

    档位分流与数据面 query 端点同口径：``storage_tier=api`` 本地执行
    （零引擎调用、零计量），否则经引擎 query 工具（平台凭据轨道）。
    """
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if getattr(dataset, "storage_tier", datasets_service.TIER_CELL) == datasets_service.TIER_API:
        return api_store.execute_query(
            db,
            dataset_id=dataset.id,
            filters=query["filters"],
            sort=query["sort"],
            limit=query["limit"],
            distinct_column=distinct_column,
        )
    arguments: dict = {
        "dataset_key": dataset.key,
        "filters": query["filters"],
        "sort": query["sort"],
        "limit": query["limit"],
    }
    if distinct_column is not None:
        arguments["distinct_column"] = distinct_column
    client = engine_factory(customer_id)
    return client.call(TOOL_QUERY, arguments)


def _build_snapshot(query: dict, result: object) -> tuple[dict, str | None]:
    """查询响应 → 面板快照；形状/投影非法返回 (原值 None, 错误文案)。"""
    if not isinstance(result, dict):
        return None, f"查询返回形态异常: {result!r}"
    cards = result.get("columns")
    if not isinstance(cards, list):
        return None, "查询返回缺少 columns 列卡数组"
    names: list[str] = []
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("name"), str):
            return None, f"查询返回列卡形态异常: {card!r}"
        names.append(card["name"])
    rows = result.get("rows")
    if not isinstance(rows, list):
        return None, "查询返回缺少 rows 行数组"

    # 投影=query.columns（缺省全列按响应列序）；投影列必须存在于响应列
    projection = query["columns"] if query["columns"] is not None else names
    for column in projection:
        if column not in names:
            return None, f"列不存在: {column}"

    snapshot_rows: list[list] = []
    for row in rows:
        if isinstance(row, dict):
            snapshot_rows.append([_norm_cell(row.get(column)) for column in projection])
        elif isinstance(row, list):
            # 位置形态（数组行）：按响应列序取位，短行补 null（缺列即无值）
            position = {name: idx for idx, name in enumerate(names)}
            snapshot_rows.append(
                [
                    _norm_cell(row[position[column]] if position[column] < len(row) else None)
                    for column in projection
                ]
            )
        else:
            return None, f"查询返回行形态异常: {row!r}"
    return {"columns": list(projection), "rows": snapshot_rows}, None


def refresh_spec_with_contracts(
    datasets_db: Session,
    *,
    spec: dict,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> tuple[dict, list[dict]]:
    """契约路径确定性刷新：逐面板执行合法 query → 快照写回，返回
    (新 spec〔信封原形状〕, 逐面板结果列表)。

    ``datasets_db`` 必须是 **datasets 库会话**（datasets.db 依赖）：生产
    PG 拓扑下 wire_platform 与 wire_datasets 分库，datasets 表只在后者
    ——端点注入正确会话，本函数不自行开库（与 schedules/executor 同口径）。

    失败语义：单面板失败（数据集不存在/本地查询拒绝/引擎失败/投影列
    不存在）→ 该面板保留原快照、结果记 failed 带原因；无 query 面板 →
    kept；**全部**契约面板失败 → :class:`RefreshFailed`（消息含各面板
    原因摘要，端点 502 零半写）。
    """
    new_spec = copy.deepcopy(spec)  # 不就地改调用方（版本行对象）的嵌套结构
    panels = _panel_entries(new_spec)
    assert panels is not None  # 调用方保证：非空计划才进契约路径
    plan = {index: query for index, _panel, query in contract_refresh_plan(new_spec)}

    results: list[dict] = []
    refreshed = 0
    for index, panel in enumerate(panels):
        if not isinstance(panel, dict):
            continue  # 非法面板条目与 AI 路径同口径：渲染侧宽容跳过
        panel_id = _panel_id(panel, index)
        query = plan.get(index)
        if query is None:  # 无 query（或 query 非法被宽容丢弃）→ 保留原快照
            results.append({"panel_id": panel_id, "status": "kept"})
            continue
        try:
            result = _query_dataset(
                datasets_db,
                dataset_id=query["dataset_id"],
                customer_id=customer_id,
                query=query,
                engine_factory=engine_factory,
            )
        except HTTPException:  # _dataset_or_404：不存在或他企（不泄露存在性）
            results.append(
                {
                    "panel_id": panel_id,
                    "status": "failed",
                    "error": f"数据集不存在: {query['dataset_id']}",
                }
            )
            continue
        except api_store.LocalQueryError as exc:  # 免费窄面拒绝（列/算子/值）
            results.append({"panel_id": panel_id, "status": "failed", "error": str(exc)})
            continue
        except (EngineUnavailable, EngineCallError) as exc:  # cell 未开通/引擎失败
            results.append({"panel_id": panel_id, "status": "failed", "error": str(exc)})
            continue
        if isinstance(result, dict) and result.get("error"):
            results.append({"panel_id": panel_id, "status": "failed", "error": str(result["error"])})
            continue
        snapshot, error = _build_snapshot(query, result)
        if error is not None:
            results.append({"panel_id": panel_id, "status": "failed", "error": error})
            continue
        # 只换 snapshot：面板其余字段（query/source/config/type/span/title/id）不动
        panel["snapshot"] = snapshot
        results.append({"panel_id": panel_id, "status": "refreshed"})
        refreshed += 1

    if plan and refreshed == 0:
        summary = "；".join(
            f"{item['panel_id']}: {item['error']}" for item in results if item["status"] == "failed"
        )
        raise RefreshFailed(f"契约刷新全部面板失败，未产生新版本——{summary}")
    return new_spec, results
