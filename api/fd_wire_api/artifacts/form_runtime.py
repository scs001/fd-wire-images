"""表单高级录入原语的服务端运行时（wire-form-entry-advanced 任务 2.1/3.1）。

四个原语的**服务端唯一执法面**（web 侧 FormRuntime/formSpec.ts 只是 UX
镜像，提交语义以本模块为准）：

- **动态选项解析**（design D2）：``optionsFromDataset`` 的选项集 =
  引用数据集有效视图（含修正）按 filters 过滤后选项列的去重非空值、
  升序、≤limit（缺省 200 硬上限 500）、超限截断标注 truncated；引用失效
  （集删/列不在快照/引擎失败）= 空集 + 如实错误标注，不静默。执行复用
  ``query_contract._query_dataset``（tier 分流：免费档本地 distinct、
  cell 档引擎 TOOL_QUERY），与数据面 query 端点同一执行面；
- **提交闸**（design D5）：``constraints`` 的 ``expect none|any`` 存在性
  探测——``when`` 单条件以提交 raw values 求值（真才探测）；filters 的
  ``{field}`` 值从归一后提交 values 代入（按字段 input 类型转义）；
  探测 limit=1 走 ``_query_dataset``；命中 422 带 constraint id+message；
- **共享提交执法点**（design D1）：:func:`validate_form_submission`
  = 字段校验（含三源选项实时裁决）→〔stage_gate：公开路径在此插令牌
  限流，冻结校验序 422→429 不变〕→ constraints 提交闸。console
  corrections（携 artifact_id，当前版本）与公开 submit（钉死版本）共用；
- **公开选项端点载荷**（design D3）：:func:`build_options_payload`
  只含钉死版本声明了 optionsFromDataset 的字段之去重选项与 fillFrom
  回填映射（每选项值一行声明列取值，选项列等值 limit 1——D6），不开放
  任何其他行/列。

探测/解析的执行错误（本地窄面拒/引擎失败/探测集不存在）**原样上抛域
异常**由路由层按既有口径转译（400/502/404），绝不静默放行闸与选项裁决。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..datasets import api_store
from ..datasets.ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
)
from .form_spec import (
    OPTIONS_DATASET_LIMIT_DEFAULT,
    OPTIONS_DATASET_LIMIT_MAX,
    _condition_visible,
    _scalar_text,
    validate_form_values,
)
from .query_contract import _query_dataset


def _source_limit(source: dict) -> int:
    """optionsFromDataset.limit 的运行时夹取（保存校验已拦越界——防御位，
    不放大也不归零）。"""
    raw = source.get("limit")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return OPTIONS_DATASET_LIMIT_DEFAULT
    return max(1, min(raw, OPTIONS_DATASET_LIMIT_MAX))


def resolve_dataset_options(
    datasets_db: Session,
    *,
    source: dict,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> dict:
    """解析一个 optionsFromDataset 源的选项集。

    返回 ``{"options": [str, ...], "truncated": bool, "error": str | None}``：
    选项 = 有效视图（含修正）按 filters 过滤后选项列去重非空值的字符串
    形态、升序（查询面原生序）、≤limit、超限截断标注。引用失效（集删/
    他企→404、列不在快照→LocalQueryError、引擎失败）= 空集 + 如实错误
    标注（调用方如实呈现/裁决，不静默空白）。

    底层查询带 ``limit+1``（本地 distinct 面不施 limit、引擎面以此保证
    截断可判定——见 design D2「不谎报全量」）。
    """
    limit = _source_limit(source)
    filters = source.get("filters")
    query = {
        "filters": [
            dict(flt) for flt in (filters if isinstance(filters, list) else []) if isinstance(flt, dict)
        ],
        "sort": [],
        "limit": limit + 1,
        "columns": None,
    }
    try:
        result = _query_dataset(
            datasets_db,
            dataset_id=source.get("datasetId"),
            customer_id=customer_id,
            query=query,
            engine_factory=engine_factory,
            distinct_column=source.get("column"),
        )
    except HTTPException as exc:  # _dataset_or_404：集删/他企（不泄露存在性）
        return {"options": [], "truncated": False, "error": f"引用数据集不可用（{exc.detail}）"}
    except (api_store.LocalQueryError, EngineUnavailable, EngineCallError) as exc:
        return {"options": [], "truncated": False, "error": str(exc)}
    values = result.get("values") if isinstance(result, dict) else None
    if not isinstance(values, list):
        return {"options": [], "truncated": False, "error": "选项源查询返回形态异常"}
    options: list[str] = []
    for raw in values:
        if raw is None:
            continue  # 去重非空值：null 不进选项集
        text = _scalar_text(raw)
        if not text.strip():
            continue
        if text not in options:  # 去重（查询面一般已去重——防御位）
            options.append(text)
    truncated = len(options) > limit
    return {"options": options[:limit], "truncated": truncated, "error": None}


def dataset_option_fields(inner: dict) -> list[tuple[str, dict]]:
    """spec 内层中声明了 optionsFromDataset 的 ``(column, source)`` 列表
    （钉死/当前版本通用——公开端点与提交校验同一提取口径）。"""
    fields = inner.get("fields") if isinstance(inner, dict) else None
    result: list[tuple[str, dict]] = []
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        source = field.get("optionsFromDataset")
        column = field.get("column")
        if isinstance(source, dict) and isinstance(column, str) and column:
            result.append((column, source))
    return result


def resolve_submission_options(
    inner: dict,
    values: object,
    *,
    datasets_db: Session,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> dict[str, dict]:
    """提交校验用的动态选项集：只为**携带非空值**的动态 select 解析
    （空值/未传字段不触发查询——省读、cell 档省引擎调用）。"""
    resolved: dict[str, dict] = {}
    for column, source in dataset_option_fields(inner):
        value = values.get(column) if isinstance(values, dict) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        resolved[column] = resolve_dataset_options(
            datasets_db,
            source=source,
            customer_id=customer_id,
            engine_factory=engine_factory,
        )
    return resolved


# ------------------------------------------------------------ 提交闸（D5）


def _substitute_probe_filters(filters: object, normalized: dict) -> list[dict]:
    """probe filters 的 ``{field}`` 值代入：按该字段 input 类型归一后的
    提交 values 取值（字段未填/被隐藏跳过 → None——如实按 null 探测）；
    字面量值原样透传。"""
    substituted: list[dict] = []
    for flt in filters if isinstance(filters, list) else []:
        if not isinstance(flt, dict):
            continue
        value = flt.get("value")
        if isinstance(value, dict) and isinstance(value.get("field"), str):
            value = normalized.get(value["field"])
        substituted.append({"column": flt.get("column"), "op": flt.get("op"), "value": value})
    return substituted


def _probe_rows_exist(
    datasets_db: Session,
    *,
    dataset_id: str,
    customer_id: str,
    filters: list[dict],
    engine_factory: EngineClientFactory,
) -> bool:
    """limit=1 存在性探测：探测集有任一行匹配即真（design D5）。

    探测集不存在/他企（HTTPException 404）、本地窄面拒（LocalQueryError）、
    引擎失败（EngineUnavailable/EngineCallError/引擎侧 error 信封）原样
    上抛——绝不静默放行闸（fail-closed 由路由层如实转译）。
    """
    result = _query_dataset(
        datasets_db,
        dataset_id=dataset_id,
        customer_id=customer_id,
        query={"filters": filters, "sort": [], "limit": 1, "columns": None},
        engine_factory=engine_factory,
    )
    if isinstance(result, dict) and result.get("error"):
        raise EngineCallError(str(result["error"]))
    rows = result.get("rows") if isinstance(result, dict) else None
    return isinstance(rows, list) and len(rows) > 0


def evaluate_form_constraints(
    inner: dict,
    *,
    normalized: dict,
    raw: object,
    datasets_db: Session,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> str | None:
    """逐条执行提交闸；返回 None（全过）或命中文案（带 constraint id 与
    message，可直接作 422 detail）。

    - ``when`` 单条件显隐同形条件以提交 **raw** values 求值（与字段显隐
      同一函数同一口径），假则跳过探测不拦截；
    - ``expect none``：探测到行即拦；``expect any``：零行即拦；
    - 声明序执行，首个命中即返回（探测次数上界 = constraints 条数 ≤10）。
    """
    constraints = inner.get("constraints") if isinstance(inner, dict) else None
    if not isinstance(constraints, list):
        return None
    comparators = {
        "gt": lambda lhs, rhs: lhs > rhs,
        "gte": lambda lhs, rhs: lhs >= rhs,
        "lt": lambda lhs, rhs: lhs < rhs,
        "lte": lambda lhs, rhs: lhs <= rhs,
    }
    row = raw if isinstance(raw, dict) else {}
    for constraint in constraints:
        if not isinstance(constraint, dict):  # 防御位：保存校验已拦
            continue
        when = constraint.get("when")
        if isinstance(when, dict) and not _condition_visible(when, row, comparators):
            continue  # 条件闸按需生效：when 假 → 不探测不拦截
        probe = constraint.get("probe")
        if not isinstance(probe, dict):  # 防御位：保存校验已拦
            continue
        exists = _probe_rows_exist(
            datasets_db,
            dataset_id=probe.get("datasetId"),
            customer_id=customer_id,
            filters=_substitute_probe_filters(probe.get("filters"), normalized),
            engine_factory=engine_factory,
        )
        hit = exists if probe.get("expect") == "none" else not exists
        if hit:
            return (
                f"提交被约束 {constraint.get('id')} 拦截：{constraint.get('message')}"
            )
    return None


# ------------------------------------------------- 共享提交执法点（D1）


def validate_form_submission(
    inner: dict,
    values: object,
    *,
    datasets_db: Session,
    customer_id: str,
    engine_factory: EngineClientFactory,
    stage_gate=None,
) -> tuple[dict | None, str | None]:
    """两提交路径共用的统一执法点（design D1）。

    ``inner`` = spec 内层（console corrections 传**当前版本**、公开 submit
    传**钉死版本**）；执行序：动态选项实时解析（仅携带非空值的动态 select）
    → 字段校验（``validate_form_values`` 扩展三源裁决，422 字段级）→
    ``stage_gate()``（公开路径在此插令牌级限流——冻结校验序「422 字段级
    先于 429」不变；console 传 None）→ constraints 提交闸（命中 422 带
    constraint id+message）。返回 ``(归一 values, None)`` 或 ``(None, 422
    文案)``；字段校验失败先于闸呈现（需求书冻结序）。

    探测/解析的执行错误原样上抛域异常（LocalQueryError/EngineUnavailable/
    EngineCallError/HTTPException）——由路由层按既有口径转译，绝不静默。
    """
    dataset_options = resolve_submission_options(
        inner,
        values,
        datasets_db=datasets_db,
        customer_id=customer_id,
        engine_factory=engine_factory,
    )
    fields = inner.get("fields") if isinstance(inner, dict) else None
    normalized, error = validate_form_values(
        fields, values, dataset_options=dataset_options
    )
    if error is not None:
        return None, error
    if stage_gate is not None:
        stage_gate()
    gate_error = evaluate_form_constraints(
        inner,
        normalized=normalized if isinstance(normalized, dict) else {},
        raw=values,
        datasets_db=datasets_db,
        customer_id=customer_id,
        engine_factory=engine_factory,
    )
    if gate_error is not None:
        return None, gate_error
    return normalized, None


# ------------------------------------------- 公开选项端点载荷（D3/D6）


def _row_cell(result: dict, column: str):
    """查询响应首行的指定列取值（dict 行取键；数组行按列卡序取位）。"""
    rows = result.get("rows") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if isinstance(row, dict):
        return row.get(column)
    if isinstance(row, list):
        columns = result.get("columns")
        names = [
            card.get("name")
            for card in (columns if isinstance(columns, list) else [])
            if isinstance(card, dict)
        ]
        for index, name in enumerate(names):
            if name == column and index < len(row):
                return row[index]
    return None


def _fill_lookup(
    datasets_db: Session,
    *,
    source: dict,
    customer_id: str,
    engine_factory: EngineClientFactory,
    option: str,
    fill_column: str,
):
    """单选项值的回填取值：引用集「选项列=option」等值 limit 1 取声明回填
    列值（design D6）。填不到（无行/列漂移/查询失败）留空不报错。"""
    try:
        result = _query_dataset(
            datasets_db,
            dataset_id=source.get("datasetId"),
            customer_id=customer_id,
            query={
                "filters": [
                    {"column": source.get("column"), "op": "eq", "value": option}
                ],
                "sort": [],
                "limit": 1,
                "columns": None,
            },
            engine_factory=engine_factory,
        )
    except (HTTPException, api_store.LocalQueryError, EngineUnavailable, EngineCallError):
        return None  # 填不到留空不报错（运行时回填同语义）
    if isinstance(result, dict) and result.get("error"):
        return None
    value = _row_cell(result, fill_column)
    # 载荷口径与选项值一致：字符串化（公开页 fills 查表按字符串取值）。
    return None if value is None else str(value)


def _fill_targets(inner: dict) -> dict[str, list[str]]:
    """fillFrom 目标映射：动态 select 列 → 回填声明列列表（去重保序）。"""
    targets: dict[str, list[str]] = {}
    fields = inner.get("fields") if isinstance(inner, dict) else None
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        fill = field.get("fillFrom")
        if not isinstance(fill, dict):
            continue
        target, column = fill.get("field"), fill.get("column")
        if isinstance(target, str) and isinstance(column, str):
            declared = targets.setdefault(target, [])
            if column not in declared:
                declared.append(column)
    return targets


def build_options_payload(
    inner: dict,
    *,
    datasets_db: Session,
    customer_id: str,
    engine_factory: EngineClientFactory,
) -> dict:
    """公开选项解析端点载荷（design D3）：仅含声明了 optionsFromDataset
    的字段——``{"fields": {column: {"options", "truncated", "fills",
    ("error")}}}``；fills = {回填声明列: {选项值: 该行回填列值}}（每选项
    值等值 limit 1——D6）。未声明动态选项的表单得空 fields（非 404）。"""
    targets = _fill_targets(inner)
    fields_out: dict[str, dict] = {}
    for column, source in dataset_option_fields(inner):
        resolved = resolve_dataset_options(
            datasets_db,
            source=source,
            customer_id=customer_id,
            engine_factory=engine_factory,
        )
        fills: dict[str, dict] = {}
        for fill_column in targets.get(column, []):
            fills[fill_column] = {
                option: _fill_lookup(
                    datasets_db,
                    source=source,
                    customer_id=customer_id,
                    engine_factory=engine_factory,
                    option=option,
                    fill_column=fill_column,
                )
                for option in resolved["options"]
            }
        entry = {
            "options": resolved["options"],
            "truncated": resolved["truncated"],
            "fills": fills,
        }
        if resolved["error"]:
            entry["error"] = resolved["error"]  # 引用失效如实标注（不静默）
        fields_out[column] = entry
    return {"fields": fields_out}
