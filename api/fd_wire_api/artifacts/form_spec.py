"""form 产物 spec 结构校验（wire-form-builder 任务 1.1，design D1 第①层）
与提交 values 校验（任务 3.3，design D3）；字段显隐联动（wire-form-field-
linkage 任务 1-2）；多条件组合显隐与联动改选项（wire-form-linkage-v2 任务
1.1-1.3）；高级录入三新键与提交闸（wire-form-entry-advanced 任务 1.1/1.2
——additive，不带新键的存量 spec 零漂移）。

纯函数零依赖（不触库、不触引擎——artifacts 库内可独立测试）。校验对象是
spec 信封的 ``spec.spec`` 部分：``{datasetId, fields[], submitLabel?,
successMessage?, constraints?}``，field = ``{column, label, input, required?,
options?, visibleWhen?, visibleWhenGroup?, optionsFrom?, optionsFromDataset?,
fillFrom?, computed?}``：

- ``datasetId``：null（构建中间态——可保存不可提交）或非空字符串；
- ``fields``：数组（可为空）；每项必须是对象：
  - ``column`` 非空字符串且表单内唯一（绑定数据集列名）；
  - ``label`` 非空字符串 ≤200；
  - ``input`` ∈ :data:`FIELD_INPUTS`（text/number/date/select）；
  - ``required`` 可省略（缺省 false 语义），在则必须是布尔值；
  - ``input=select`` 必带 ``options``（非空字符串列表）；
  - ``visibleWhen`` 可省略（缺省恒可见），在则须是单条件显隐规则
    ``{field, op, value?}``（形状见 :data:`VISIBLE_WHEN_OPS`；``field``
    须引用同表单**另一**字段列——自引用/引用不存在列均拒）；
  - ``visibleWhenGroup`` 可省略（多条件组合显隐——wire-form-linkage-v2），
    在则须是 ``{mode: "all"|"any", conditions: [单条件, ...]}``：conditions
    非空数组 ≤10 条，每条复用单条件校验器（同一算子枚举同一带值规）；
    **与 ``visibleWhen`` 并存拒**（两套求值序是暗坑，保存时钉死互斥）；
    组内条件的引用存在性/自引用二趟裁决（同现行，支持前向引用）；
  - ``optionsFrom`` 可省略（联动改选项——wire-form-linkage-v2），仅
    ``input=select`` 字段可用：``{field, cases: {<控制值>: [选项, ...]},
    fallback?}``——cases 非空对象且键 trim 后非空、值为非空字符串列表
    （同 options 列表口径），fallback 可省略、在则同列表规；
    **select 必须恰有 ``options`` 与 ``optionsFrom`` 其一**（并存拒）；
    ``field`` 须引用同表单**另一**字段列（自引用/引用不存在列均拒，
    二趟裁决）；
- ``submitLabel`` / ``successMessage`` 可省略，在则须为非空字符串 ≤200。

新键（wire-form-entry-advanced——全部可省略，不带则行为逐字不变）：
- ``optionsFromDataset``：仅 ``input=select`` 可用 ``{datasetId, column,
  filters?, limit?}``（**select 选项三源 options/optionsFrom/
  optionsFromDataset 恰有其一**）；filters 为字面量条件
  ``[{column, op∈eq|in|gt|gte|lt|lte, value}]``（value 非空标量，数组形态
  仅 op=in）；limit 为 1..500 整数（缺省 200）。datasetId 的属主校验在
  service 层（同 datasetId 口径），列存在性不在此裁决（运行时如实标注）；
- ``fillFrom``：``{field, column}``——field 须引用同表单内**带
  optionsFromDataset 的 select** 字段（二趟裁决，不得自引用）；column 为
  引用数据集回填列名（存在性运行时裁决）；
- ``computed``：``{op:"add_days", date, days}``（v1 仅此一 op）——date
  须引用同表单内 ``input=date`` 的**另一**字段（二趟裁决）；days 为同表单
  ``input=number`` 字段列（字符串形态）或非负整数字面量；
- spec 级 ``constraints``：``[{id, message, when?, probe}]``——id 表单内
  唯一非空；message 非空 ≤500；when 为单条件显隐同形条件（引用存在性
  二趟裁决）；probe=``{datasetId, filters, expect}``（filters 1..10 条，
  value=非空标量|标量数组（仅 in）|``{field: <本表单字段列>}``——引用
  存在性二趟裁决；expect ∈ none/any）；constraints ≤10 条（保存校验）。


**列名是否仍在数据集中不在保存时裁决**（design D1 第③层定案——数据集列
会演进，钉死校验会让旧版本表单无法重存；列漂移的真相在提交时暴露，422
如实）。datasetId 的存在/属主校验是第②层，在 service 层经 datasets 库
会话完成（见 ``artifacts.service._require_form_dataset``）。

返回错误文案（None=合法）；文案带字段定位（如 ``fields[2].column 重复:
region``）逐条可断言。
"""

from __future__ import annotations

import math
import re

#: 表单字段输入形态枚举（提交面按此做类型校验的依据）
FIELD_INPUTS: tuple[str, ...] = ("text", "number", "date", "select")
#: 文案字段（label / submitLabel / successMessage）长度上限
TEXT_LIMIT = 200

#: date 输入的口径：YYYY-MM-DD（拒绝时间部分——design D3）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: visibleWhen 算子枚举（单条件显隐——wire-form-field-linkage）
VISIBLE_WHEN_OPS: tuple[str, ...] = (
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "empty",
    "notEmpty",
)

#: 无值算子（条件只看控制列空值，规则里不得带 value 键）
_VALUELESS_OPS: frozenset[str] = frozenset({"empty", "notEmpty"})

#: gt/gte/lt/lte 的数值口径：双端须同匹配该正则才按数值比较（双端十进制
#: ——防 JS ``Number("0x10")`` 与 Python ``float()`` 的解析分歧，否则回退
#: 字符串字典序）
_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def _text_error(value: object, *, what: str) -> str | None:
    """非空字符串 ≤ :data:`TEXT_LIMIT` 校验；返回错误文案或 None。"""
    if not isinstance(value, str) or not value.strip():
        return f"{what} 必须是非空字符串"
    if len(value) > TEXT_LIMIT:
        return f"{what} 长度超过 {TEXT_LIMIT} 上限"
    return None


def _visible_when_error(rule: object, where: str) -> str | None:
    """单条件显隐规则形状校验（不含引用存在性——那需全字段列齐后二趟）。

    ``where`` = 规则的完整定位路径：单条件传 ``fields[1].visibleWhen``；
    组内条件复用本器传 ``fields[1].visibleWhenGroup.conditions[0]``
    （wire-form-linkage-v2——文案即冻结口径，组定位带组内索引）。

    规则（wire-form-field-linkage 冻结契约）：
    - 必须是 JSON 对象 ``{field, op, value?}``；
    - ``field``：非空字符串（引用同表单另一字段的 column）；
    - ``op`` ∈ :data:`VISIBLE_WHEN_OPS`；
    - ``empty``/``notEmpty``：不得带 ``value`` 键（在则拒）；
    - 其余算子：``value`` 必须是非空标量（str/int/float/bool——dict/list/
      null 拒；字符串形态 trim 后非空，空串/纯空白拒）。
    """
    if not isinstance(rule, dict):
        return f"{where} 必须是 JSON 对象（{{field, op, value?}}）"
    target = rule.get("field")
    if not isinstance(target, str) or not target.strip():
        return f"{where}.field 必须是非空字符串（同表单另一字段的 column）"
    op = rule.get("op")
    if op not in VISIBLE_WHEN_OPS:
        return f"{where}.op 必须是 {'/'.join(VISIBLE_WHEN_OPS)}（收到 {op!r}）"
    if op in _VALUELESS_OPS:
        if "value" in rule:
            return f"{where}.{op} 不得带 value 键"
        return None
    if "value" not in rule:
        return f"{where}.value 必须是非空标量（op={op} 必带）"
    raw = rule["value"]
    if raw is None or not isinstance(raw, (str, int, float)):  # bool ⊂ int 过
        return f"{where}.value 必须是非空标量（收到 {raw!r}）"
    if isinstance(raw, str) and not raw.strip():
        return f"{where}.value 必须是非空标量（收到 {raw!r}）"
    return None


#: visibleWhenGroup 组内条件条数上限（冻结契约 1..10 条）
GROUP_CONDITIONS_LIMIT = 10

# ----------------------------- 高级录入新键常量（wire-form-entry-advanced）

#: optionsFromDataset.limit 缺省值与硬上限（冻结契约 1..500、缺省 200）
OPTIONS_DATASET_LIMIT_DEFAULT = 200
OPTIONS_DATASET_LIMIT_MAX = 500

#: optionsFromDataset.filters 条数上限（与数据面 QueryIn.filters max_length
#: 同界——console 运行时经该端点解析，保存时越界即拒）
OPTIONS_DATASET_FILTERS_LIMIT = 50

#: probe 过滤算子白名单（窄于显隐算子——无 contains/startswith，
#: 与 api_store 窄面查询的数值/等值语义对齐）
PROBE_FILTER_OPS: tuple[str, ...] = ("eq", "in", "gt", "gte", "lt", "lte")

#: 每表单 constraints 条数上限 / 每 probe filters 条数上限（冻结契约）
CONSTRAINTS_LIMIT = 10
PROBE_FILTERS_LIMIT = 10

#: constraint.message 长度上限（拦截文案要人读不要贴墙）
CONSTRAINT_MESSAGE_LIMIT = 500

#: computed op 枚举（v1 仅 add_days——design D4，后续走枚举追加不引表达式）
COMPUTED_OPS: tuple[str, ...] = ("add_days",)


def _is_literal_scalar(value: object) -> bool:
    """字面量过滤值的标量口径：str（trim 后非空）/int/float/bool；
    null 与容器形态一律非标量（bool ⊂ int——按 JSON 值域收）。"""
    if value is None or isinstance(value, (dict, list)):
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return isinstance(value, (str, int, float))


def _literal_filter_error(flt: object, where: str) -> str | None:
    """optionsFromDataset.filters 字面量条件形状校验：
    ``{column, op∈PROBE_FILTER_OPS, value}``，value 非空标量或标量数组
    （数组形态仅 op=in 可用）。"""
    if not isinstance(flt, dict):
        return f"{where} 必须是 JSON 对象（{{column, op, value}}）"
    column = flt.get("column")
    if not isinstance(column, str) or not column.strip():
        return f"{where}.column 必须是非空字符串（引用数据集列名）"
    op = flt.get("op")
    if op not in PROBE_FILTER_OPS:
        return f"{where}.op 必须是 {'/'.join(PROBE_FILTER_OPS)}（收到 {op!r}）"
    if "value" not in flt:
        return f"{where}.value 必须是非空标量或标量数组（op={op} 必带）"
    value = flt["value"]
    if isinstance(value, list):
        if op != "in":
            return f"{where}.value 数组形态仅 op=in 可用（收到 op={op!r}）"
        if not value or not all(_is_literal_scalar(item) for item in value):
            return f"{where}.value 必须是非空标量数组（收到 {value!r}）"
        return None
    if not _is_literal_scalar(value):
        return f"{where}.value 必须是非空标量（收到 {value!r}）"
    return None


def _probe_filter_error(flt: object, where: str) -> str | None:
    """probe.filters 条件形状校验：同字面量条件，另收 ``{field}`` 引用形态
    （value 为恰好含 field 键的对象——引用存在性二趟裁决）。"""
    if not isinstance(flt, dict):
        return f"{where} 必须是 JSON 对象（{{column, op, value}}）"
    column = flt.get("column")
    if not isinstance(column, str) or not column.strip():
        return f"{where}.column 必须是非空字符串（探测数据集列名）"
    op = flt.get("op")
    if op not in PROBE_FILTER_OPS:
        return f"{where}.op 必须是 {'/'.join(PROBE_FILTER_OPS)}（收到 {op!r}）"
    if "value" not in flt:
        return f"{where}.value 必须是非空标量、标量数组或 {{field}} 引用（op={op} 必带）"
    value = flt["value"]
    if isinstance(value, dict):
        if set(value) != {"field"} or not isinstance(value.get("field"), str) or not value["field"].strip():
            return (
                f"{where}.value 引用形态必须是 {{field: <本表单字段列>}}"
                f"（收到 {value!r}）"
            )
        return None
    if isinstance(value, list):
        if op != "in":
            return f"{where}.value 数组形态仅 op=in 可用（收到 op={op!r}）"
        if not value or not all(_is_literal_scalar(item) for item in value):
            return f"{where}.value 必须是非空标量数组（收到 {value!r}）"
        return None
    if not _is_literal_scalar(value):
        return f"{where}.value 必须是非空标量（收到 {value!r}）"
    return None


def _options_from_dataset_error(source: object, where: str) -> str | None:
    """optionsFromDataset 形状校验（引用数据集属主在 service 层裁决）。"""
    if not isinstance(source, dict):
        return (
            f"{where}.optionsFromDataset 必须是 JSON 对象"
            f"（{{datasetId, column, filters?, limit?}}）"
        )
    dataset_id = source.get("datasetId")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return f"{where}.optionsFromDataset.datasetId 必须是非空字符串（引用数据集 id）"
    column = source.get("column")
    if not isinstance(column, str) or not column.strip():
        return f"{where}.optionsFromDataset.column 必须是非空字符串（选项列名）"
    filters = source.get("filters")
    if filters is not None:
        if not isinstance(filters, list) or len(filters) > OPTIONS_DATASET_FILTERS_LIMIT:
            return (
                f"{where}.optionsFromDataset.filters 必须是数组"
                f"（≤{OPTIONS_DATASET_FILTERS_LIMIT} 条，收到 "
                f"{len(filters) if isinstance(filters, list) else filters!r}）"
            )
        for index, flt in enumerate(filters):
            error = _literal_filter_error(
                flt, f"{where}.optionsFromDataset.filters[{index}]"
            )
            if error is not None:
                return error
    limit = source.get("limit")
    if limit is not None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= OPTIONS_DATASET_LIMIT_MAX
        ):
            return (
                f"{where}.optionsFromDataset.limit 必须是"
                f" 1..{OPTIONS_DATASET_LIMIT_MAX} 的整数"
                f"（缺省 {OPTIONS_DATASET_LIMIT_DEFAULT}，收到 {limit!r}）"
            )
    return None


def _fill_from_error(fill: object, where: str) -> str | None:
    """fillFrom 形状校验（field 引用存在性/类型约束二趟裁决）。"""
    if not isinstance(fill, dict):
        return f"{where}.fillFrom 必须是 JSON 对象（{{field, column}}）"
    field = fill.get("field")
    if not isinstance(field, str) or not field.strip():
        return (
            f"{where}.fillFrom.field 必须是非空字符串"
            f"（同表单带 optionsFromDataset 的 select 字段列）"
        )
    column = fill.get("column")
    if not isinstance(column, str) or not column.strip():
        return f"{where}.fillFrom.column 必须是非空字符串（引用数据集回填列名）"
    return None


def _computed_error(computed: object, where: str) -> str | None:
    """computed 形状校验（date/days 引用类型约束二趟裁决）。"""
    if not isinstance(computed, dict):
        return f"{where}.computed 必须是 JSON 对象（{{op, date, days}}）"
    op = computed.get("op")
    if op not in COMPUTED_OPS:
        return f"{where}.computed.op 必须是 {'/'.join(COMPUTED_OPS)}（收到 {op!r}）"
    date = computed.get("date")
    if not isinstance(date, str) or not date.strip():
        return f"{where}.computed.date 必须是非空字符串（同表单 input=date 字段列）"
    days = computed.get("days")
    if isinstance(days, bool) or not isinstance(days, (str, int)):
        return (
            f"{where}.computed.days 必须是非空字符串（input=number 字段列）"
            f"或非负整数字面量（收到 {days!r}）"
        )
    if isinstance(days, int) and days < 0:
        return f"{where}.computed.days 字面量必须是非负整数（收到 {days!r}）"
    if isinstance(days, str) and not days.strip():
        return (
            f"{where}.computed.days 必须是非空字符串（input=number 字段列）"
            f"或非负整数字面量（收到 {days!r}）"
        )
    return None


def _probe_error(probe: object, where: str) -> str | None:
    """probe 形状校验（datasetId 属主在 service 层裁决；``{field}`` 引用
    存在性二趟裁决）。"""
    if not isinstance(probe, dict):
        return f"{where}.probe 必须是 JSON 对象（{{datasetId, filters, expect}}）"
    dataset_id = probe.get("datasetId")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return f"{where}.probe.datasetId 必须是非空字符串（探测数据集 id）"
    filters = probe.get("filters")
    if not isinstance(filters, list) or not filters:
        return f"{where}.probe.filters 必须是非空数组（1..{PROBE_FILTERS_LIMIT} 条）"
    if len(filters) > PROBE_FILTERS_LIMIT:
        return (
            f"{where}.probe.filters 不能超过 {PROBE_FILTERS_LIMIT} 条"
            f"（收到 {len(filters)} 条）"
        )
    for index, flt in enumerate(filters):
        error = _probe_filter_error(flt, f"{where}.probe.filters[{index}]")
        if error is not None:
            return error
    expect = probe.get("expect")
    if expect not in ("none", "any"):
        return f"{where}.probe.expect 必须是 none/any（收到 {expect!r}）"
    return None


def _constraints_error(constraints: object) -> str | None:
    """spec 级 constraints 形状校验（when/probe 的引用存在性二趟裁决）。"""
    if not isinstance(constraints, list):
        return "spec.spec.constraints 必须是数组（[{id, message, when?, probe}]）"
    if len(constraints) > CONSTRAINTS_LIMIT:
        return (
            f"spec.spec.constraints 不能超过 {CONSTRAINTS_LIMIT} 条"
            f"（收到 {len(constraints)} 条）"
        )
    seen_ids: set[str] = set()
    for index, constraint in enumerate(constraints):
        where = f"constraints[{index}]"
        if not isinstance(constraint, dict):
            return f"{where} 必须是 JSON 对象（{{id, message, when?, probe}}）"
        constraint_id = constraint.get("id")
        if not isinstance(constraint_id, str) or not constraint_id.strip():
            return f"{where}.id 必须是非空字符串（表单内唯一）"
        if constraint_id in seen_ids:
            return f"{where}.id 重复: {constraint_id}"
        seen_ids.add(constraint_id)
        message = constraint.get("message")
        if not isinstance(message, str) or not message.strip():
            return f"{where}.message 必须是非空字符串（命中拦截时呈现给填写者）"
        if len(message) > CONSTRAINT_MESSAGE_LIMIT:
            return f"{where}.message 长度超过 {CONSTRAINT_MESSAGE_LIMIT} 上限"
        if "when" in constraint:
            rule_error = _visible_when_error(constraint["when"], f"{where}.when")
            if rule_error is not None:
                return rule_error
        probe_error = _probe_error(constraint.get("probe"), where)
        if probe_error is not None:
            return probe_error
    return None



def _group_error(group: object, where: str) -> str | None:
    """visibleWhenGroup 形状校验（不含引用存在性——那需全字段列齐后二趟）。

    规则（wire-form-linkage-v2 冻结契约）：
    - 必须是 JSON 对象 ``{mode, conditions}``；
    - ``mode`` ∈ ``all``/``any``；
    - ``conditions``：非空数组且 ≤ :data:`GROUP_CONDITIONS_LIMIT` 条；
    - 每条复用 :func:`_visible_when_error` 单条件校验器（``where`` 定位带
      组内索引，如 ``fields[0].visibleWhenGroup.conditions[1]``）。
    """
    if not isinstance(group, dict):
        return f"{where}.visibleWhenGroup 必须是 JSON 对象（{{mode, conditions}}）"
    mode = group.get("mode")
    if mode not in ("all", "any"):
        return f"{where}.visibleWhenGroup.mode 必须是 all/any（收到 {mode!r}）"
    conditions = group.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return f"{where}.visibleWhenGroup.conditions 必须是非空数组（1..10 条）"
    if len(conditions) > GROUP_CONDITIONS_LIMIT:
        return (
            f"{where}.visibleWhenGroup.conditions 不能超过"
            f" {GROUP_CONDITIONS_LIMIT} 条（收到 {len(conditions)} 条）"
        )
    for index, condition in enumerate(conditions):
        error = _visible_when_error(
            condition, f"{where}.visibleWhenGroup.conditions[{index}]"
        )
        if error is not None:
            return error
    return None


def _option_list_error(options: object, what: str) -> str | None:
    """选项列表校验（options/optionsFrom.cases 值/fallback 同一口径）：
    非空字符串列表（项 trim 后非空）。"""
    if (
        not isinstance(options, list)
        or not options
        or not all(isinstance(option, str) and option.strip() for option in options)
    ):
        return f"{what} 必须是非空字符串列表"
    return None


def _options_from_error(options_from: object, where: str) -> str | None:
    """optionsFrom 形状校验（不含引用存在性——那需全字段列齐后二趟）。

    规则（wire-form-linkage-v2 冻结契约）：
    - 必须是 JSON 对象 ``{field, cases, fallback?}``；
    - ``field``：非空字符串（引用同表单另一字段的 column）；
    - ``cases``：非空 JSON 对象；键 trim 后非空（= 控制字段值的 trim 字符串
      形态）；值为非空字符串列表（同 options 列表口径）；
    - ``fallback`` 可省略，在则同列表规。
    """
    if not isinstance(options_from, dict):
        return f"{where}.optionsFrom 必须是 JSON 对象（{{field, cases, fallback?}}）"
    target = options_from.get("field")
    if not isinstance(target, str) or not target.strip():
        return f"{where}.optionsFrom.field 必须是非空字符串（同表单另一字段的 column）"
    cases = options_from.get("cases")
    if not isinstance(cases, dict) or not cases:
        return f"{where}.optionsFrom.cases 必须是非空对象（控制值 → 非空选项列表）"
    for key, case_options in cases.items():
        if not isinstance(key, str) or not key.strip():
            return f"{where}.optionsFrom.cases 的键必须 trim 后非空（收到 {key!r}）"
        error = _option_list_error(
            case_options, f"{where}.optionsFrom.cases[{key!r}]"
        )
        if error is not None:
            return error
    if "fallback" in options_from:
        error = _option_list_error(
            options_from["fallback"], f"{where}.optionsFrom.fallback"
        )
        if error is not None:
            return error
    return None


def validate_form_spec(inner: object) -> str | None:
    """form spec 结构校验（``spec.spec`` 部分）；返回错误文案（None=合法）。

    调用方（``artifacts.service``）已先过通用口径 validate_spec（信封
    title 非空 + spec.spec 非空对象）——本函数只管 form 结构层。
    """
    if not isinstance(inner, dict):
        return "spec.spec 必须是 JSON 对象"
    dataset_id = inner.get("datasetId")
    if dataset_id is not None and (
        not isinstance(dataset_id, str) or not dataset_id.strip()
    ):
        return "spec.spec.datasetId 必须是 null 或非空字符串（数据集 id）"
    fields = inner.get("fields")
    if not isinstance(fields, list):
        return "spec.spec.fields 必须是数组"
    seen_columns: set[str] = set()
    for index, field in enumerate(fields):
        where = f"fields[{index}]"
        if not isinstance(field, dict):
            return f"{where} 必须是 JSON 对象"
        column = field.get("column")
        if not isinstance(column, str) or not column.strip():
            return f"{where}.column 必须是非空字符串（绑定数据集列名）"
        if column in seen_columns:
            return f"{where}.column 重复: {column}"
        seen_columns.add(column)
        label_error = _text_error(field.get("label"), what=f"{where}.label")
        if label_error is not None:
            return label_error
        input_kind = field.get("input")
        if input_kind not in FIELD_INPUTS:
            return (
                f"{where}.input 必须是 {'/'.join(FIELD_INPUTS)}"
                f"（收到 {input_kind!r}）"
            )
        required = field.get("required", False)
        if not isinstance(required, bool):
            return f"{where}.required 必须是布尔值（可省略，缺省 false）"
        if input_kind == "select":
            # select 选项三源恰其一（wire-form-entry-advanced 扩三源——
            # 两两并存即互斥拒，求值序是暗坑，保存时钉死；前两源的既有
            # 文案逐字保留（零漂移），都无沿用现行文案）
            if "options" in field and "optionsFrom" in field:
                return f"{where} 同时携带 options 与 optionsFrom——两者互斥"
            if "options" in field and "optionsFromDataset" in field:
                return f"{where} 同时携带 options 与 optionsFromDataset——两者互斥"
            if "optionsFrom" in field and "optionsFromDataset" in field:
                return f"{where} 同时携带 optionsFrom 与 optionsFromDataset——两者互斥"
            if "optionsFromDataset" in field:
                source_error = _options_from_dataset_error(
                    field["optionsFromDataset"], where
                )
                if source_error is not None:
                    return source_error
            elif "optionsFrom" in field:
                mapping_error = _options_from_error(field["optionsFrom"], where)
                if mapping_error is not None:
                    return mapping_error
            elif (
                _option_list_error(field.get("options"), f"{where}.options")
                is not None
            ):
                return f"{where}.options 必须是非空字符串列表（input=select 必带）"
        elif "optionsFrom" in field:
            return (
                f"{where}.optionsFrom 仅 input=select 字段可携带"
                f"（收到 {input_kind!r}）"
            )
        elif "optionsFromDataset" in field:
            return (
                f"{where}.optionsFromDataset 仅 input=select 字段可携带"
                f"（收到 {input_kind!r}）"
            )
        if "visibleWhen" in field and "visibleWhenGroup" in field:
            return f"{where} 同时携带 visibleWhen 与 visibleWhenGroup——两者互斥"
        if "visibleWhen" in field:
            rule_error = _visible_when_error(
                field["visibleWhen"], f"{where}.visibleWhen"
            )
            if rule_error is not None:
                return rule_error
        if "visibleWhenGroup" in field:
            group_error = _group_error(field["visibleWhenGroup"], where)
            if group_error is not None:
                return group_error
        # 新键形状校验（wire-form-entry-advanced——引用存在性统一二趟裁决）
        if "fillFrom" in field:
            fill_error = _fill_from_error(field["fillFrom"], where)
            if fill_error is not None:
                return fill_error
        if "computed" in field:
            computed_error = _computed_error(field["computed"], where)
            if computed_error is not None:
                return computed_error
    # spec 级 constraints 形状校验（首趟——引用存在性在其后二趟）
    if "constraints" in inner:
        constraints_error = _constraints_error(inner.get("constraints"))
        if constraints_error is not None:
            return constraints_error
    # 联动键引用存在性（二趟——支持前向引用，须全字段列齐后裁决）：
    # visibleWhen / visibleWhenGroup 组内每条件 / optionsFrom.field /
    # fillFrom.field / computed.date / computed.days（wire-form-entry-advanced）
    fields_by_column: dict[str, dict] = {
        field.get("column"): field
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("column"), str)
    }
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        where = f"fields[{index}]"
        rule = field.get("visibleWhen")
        if isinstance(rule, dict):  # 非对象已在首趟拦
            target = rule.get("field")
            if target == field.get("column"):
                return f"{where}.visibleWhen.field 不能自引用本字段: {target}"
            if target not in seen_columns:
                return f"{where}.visibleWhen.field 引用了不存在的字段列: {target}"
        group = field.get("visibleWhenGroup")
        if isinstance(group, dict):
            conditions = group.get("conditions")
            if isinstance(conditions, list):
                for condition_index, condition in enumerate(conditions):
                    if not isinstance(condition, dict):  # 非对象已在首趟拦
                        continue
                    condition_where = (
                        f"{where}.visibleWhenGroup.conditions[{condition_index}]"
                    )
                    target = condition.get("field")
                    if target == field.get("column"):
                        return (
                            f"{condition_where}.field 不能自引用本字段: {target}"
                        )
                    if target not in seen_columns:
                        return (
                            f"{condition_where}.field"
                            f" 引用了不存在的字段列: {target}"
                        )
        options_from = field.get("optionsFrom")
        if isinstance(options_from, dict):
            target = options_from.get("field")
            if target == field.get("column"):
                return f"{where}.optionsFrom.field 不能自引用本字段: {target}"
            if target not in seen_columns:
                return f"{where}.optionsFrom.field 引用了不存在的字段列: {target}"
        # fillFrom.field：不得自引用，且必须指向带 optionsFromDataset 的 select
        fill_from = field.get("fillFrom")
        if isinstance(fill_from, dict):
            target = fill_from.get("field")
            if target == field.get("column"):
                return f"{where}.fillFrom.field 不能自引用本字段: {target}"
            target_field = fields_by_column.get(target)
            if target_field is None:
                return f"{where}.fillFrom.field 引用了不存在的字段列: {target}"
            if target_field.get("input") != "select" or not isinstance(
                target_field.get("optionsFromDataset"), dict
            ):
                return (
                    f"{where}.fillFrom.field 必须引用带 optionsFromDataset 的"
                    f" select 字段（收到 {target!r}）"
                )
        # computed.date：不得自引用，且必须指向 input=date 字段；
        # computed.days：字符串形态不得自引用，且必须指向 input=number 字段
        computed = field.get("computed")
        if isinstance(computed, dict):
            date_ref = computed.get("date")
            if date_ref == field.get("column"):
                return f"{where}.computed.date 不能自引用本字段: {date_ref}"
            date_field = fields_by_column.get(date_ref)
            if date_field is None:
                return f"{where}.computed.date 引用了不存在的字段列: {date_ref}"
            if date_field.get("input") != "date":
                return (
                    f"{where}.computed.date 必须引用 input=date 字段"
                    f"（收到 {date_ref!r}）"
                )
            days = computed.get("days")
            if isinstance(days, str):
                if days == field.get("column"):
                    return f"{where}.computed.days 不能自引用本字段: {days}"
                days_field = fields_by_column.get(days)
                if days_field is None:
                    return f"{where}.computed.days 引用了不存在的字段列: {days}"
                if days_field.get("input") != "number":
                    return (
                        f"{where}.computed.days 必须引用 input=number 字段"
                        f"或给非负整数字面量（收到 {days!r}）"
                    )
    # constraints 引用存在性（二趟）：when.field 与 probe filters 的
    # {field} 引用值——表单内不存在的列 422 定位（保存时钉死，防运行期悬空）
    constraints = inner.get("constraints")
    if isinstance(constraints, list):
        for c_index, constraint in enumerate(constraints):
            if not isinstance(constraint, dict):  # 非对象已在首趟拦
                continue
            where = f"constraints[{c_index}]"
            when = constraint.get("when")
            if isinstance(when, dict):  # 非对象已在首趟拦
                target = when.get("field")
                if target not in fields_by_column:
                    return f"{where}.when.field 引用了不存在的字段列: {target}"
            probe = constraint.get("probe")
            if not isinstance(probe, dict):  # 非对象已在首趟拦
                continue
            probe_filters = probe.get("filters")
            if not isinstance(probe_filters, list):  # 非数组已在首趟拦
                continue
            for f_index, flt in enumerate(probe_filters):
                if not isinstance(flt, dict):  # 非对象已在首趟拦
                    continue
                value = flt.get("value")
                if isinstance(value, dict) and isinstance(value.get("field"), str):
                    target = value["field"]
                    if target not in fields_by_column:
                        return (
                            f"{where}.probe.filters[{f_index}].value.field"
                            f" 引用了不存在的字段列: {target}"
                        )
    for key in ("submitLabel", "successMessage"):
        if key in inner:
            error = _text_error(inner[key], what=f"spec.spec.{key}")
            if error is not None:
                return error
    return None


# --------------------------------------------- 字段显隐求值（wire-form-field-linkage）


def _scalar_text(value: object) -> str:
    """标量的字符串形态：bool 按 JSON 口径 true/false，其余按 ``str()``。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _is_blank_value(value: object) -> bool:
    """空值口径：缺键/None/字符串形态 trim 后为空（数字 0、bool False 非空）。"""
    return value is None or _scalar_text(value).strip() == ""


def _condition_visible(rule: dict, row: dict, comparators: dict) -> bool:
    """单条件求值——visibleWhen 与 visibleWhenGroup 组内条件共用的单一出处
    （wire-form-linkage-v2 抽取，逻辑与 wire-form-field-linkage 冻结契约
    逐字一致）。

    - ``ctrl = row.get(rule.field)``（缺键/null → 空值）；求值只看 values
      本身，**不递归**判控制字段自身的可见性；
    - ``empty``：ctrl 为空值 → 真；``notEmpty`` 反之；
    - **带值算子在 ctrl 为空值时一律为假（含 neq）**；
    - 非空时 ``lhs = String(ctrl).trim()``、``rhs = String(value).trim()``：
      eq/neq/contains 按字符串；gt/gte/lt/lte 双端同匹配
      :data:`_DECIMAL_RE` 才按 float 数值比较，否则按字符串字典序；
    - 未知算子防御位：按可见收（保存校验已拦）。
    """
    target = rule.get("field")
    ctrl = row.get(target) if isinstance(target, str) else None
    blank = _is_blank_value(ctrl)
    op = rule.get("op")
    if op == "empty":
        return blank
    if op == "notEmpty":
        return not blank
    if blank:
        return False  # 带值算子在 ctrl 为空值时一律为假（含 neq）
    lhs = _scalar_text(ctrl).strip()
    rhs_raw = rule.get("value")
    rhs = (
        _scalar_text(rhs_raw).strip()
        if isinstance(rhs_raw, (str, int, float))
        else ""
    )
    if op == "eq":
        return lhs == rhs
    if op == "neq":
        return lhs != rhs
    if op == "contains":
        return rhs in lhs
    if op in comparators:
        if _DECIMAL_RE.match(lhs) and _DECIMAL_RE.match(rhs):
            lhs, rhs = float(lhs), float(rhs)
        return comparators[op](lhs, rhs)
    return True  # 未知算子防御位：按可见收


def resolve_select_options(field: object, row: object) -> list[str]:
    """select 字段当前可选列表解析——双端镜像的单一出处（wire-form-linkage-v2）。

    - 无 ``optionsFrom``（静态 options，或形状防御位）：返回 ``options``
      （非列表防御位收空列表）——与 wire-form-builder 静态口径零漂移；
    - 有 ``optionsFrom``：``ctrl = row.get(optionsFrom.field)``——
      控制值为空值（:func:`_is_blank_value` 口径）或 trim 字符串形态
      （:func:`_scalar_text`）不在 cases 键（键按同口径 trim 匹配）→
      ``fallback``（缺省/形状防御位收空列表）；命中 → 该键的选项列表。

    解析后为空列表即「当前无可选项」——调用方（``evaluate_hidden_columns``
    的隐藏集与 ``validate_form_values`` 的动态成员校验）按同一结果裁决。
    """
    if not isinstance(field, dict):
        return []
    options_from = field.get("optionsFrom")
    if not isinstance(options_from, dict):
        options = field.get("options")
        return options if isinstance(options, list) else []
    row = row if isinstance(row, dict) else {}
    target = options_from.get("field")
    ctrl = row.get(target) if isinstance(target, str) else None
    fallback = options_from.get("fallback")
    fallback = fallback if isinstance(fallback, list) else []
    if _is_blank_value(ctrl):
        return fallback
    cases = options_from.get("cases")
    if not isinstance(cases, dict):
        return []
    key = _scalar_text(ctrl).strip()
    for case_key, case_options in cases.items():
        if isinstance(case_key, str) and case_key.strip() == key:
            return case_options if isinstance(case_options, list) else []
    return fallback


def evaluate_hidden_columns(fields: object, values: object) -> set[str]:
    """按提交 raw ``values`` 求隐藏列集合——纯函数。三种隐藏来源同一出口：

    1. 单条件 ``visibleWhen``（wire-form-field-linkage）；
    2. 多条件组合 ``visibleWhenGroup``（wire-form-linkage-v2）——
       ``mode=all`` 全部条件真才可见；``mode=any`` 至少一真可见；
       单条件求值逻辑共用 :func:`_condition_visible`（含「带值算子在
       控制值为空值时一律为假（含 neq）」；组内不递归——控制字段值在
       values 里即参与比较）；
    3. select 字段 ``optionsFrom`` 解析后选项为空（:func:
       `resolve_select_options`）——不可填即不可见，同走隐藏集。

    与 web 侧逐字同规（冻结契约）：无联动键的字段恒可见（不在返回集合
    中）；求值只看 values 本身，**不递归**判控制字段自身的可见性。

    防御位：fields 非数组/values 非对象 → 空集（``validate_form_values``
    先做入参形态校验再调本函数；未知算子/未知 mode 按可见收、组内非对象
    条件跳过——保存校验已拦）。
    """
    if not isinstance(fields, list):
        return set()
    row = values if isinstance(values, dict) else {}
    comparators = {
        "gt": lambda lhs, rhs: lhs > rhs,
        "gte": lambda lhs, rhs: lhs >= rhs,
        "lt": lambda lhs, rhs: lhs < rhs,
        "lte": lambda lhs, rhs: lhs <= rhs,
    }
    hidden: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        column = field.get("column")
        if not isinstance(column, str) or not column:
            continue
        rule = field.get("visibleWhen")
        if isinstance(rule, dict):
            if not _condition_visible(rule, row, comparators):
                hidden.add(column)
        group = field.get("visibleWhenGroup")
        if isinstance(group, dict):
            mode = group.get("mode")
            if mode not in ("all", "any"):
                visible = True  # 未知 mode 防御位：按可见收（保存校验已拦）
            else:
                conditions = group.get("conditions")
                results = [
                    _condition_visible(condition, row, comparators)
                    for condition in (
                        conditions if isinstance(conditions, list) else []
                    )
                    if isinstance(condition, dict)
                ]
                visible = any(results) if mode == "any" else all(results)
            if not visible:
                hidden.add(column)
        # 空选项 = 隐藏（不可填即不可见）：仅 optionsFrom 走动态解析，
        # 静态 options 不参与（保存校验保证非空——零漂移）
        if (
            field.get("input") == "select"
            and isinstance(field.get("optionsFrom"), dict)
            and not resolve_select_options(field, row)
        ):
            hidden.add(column)
    return hidden


# ------------------------------------------------------- 提交 values 校验（D3）


def _field_label(field: dict, column: str) -> str:
    """字段的人读定位（label 优先，缺席回退 column）。"""
    label = field.get("label")
    if isinstance(label, str) and label.strip():
        return label
    return column


#: 动态选项集在成员校验文案里的展示上限（选项集来自数据集可达数百项——
#: 422 文案有界呈现，不整列表刷屏）
_OPTIONS_DISPLAY_LIMIT = 20


def _options_display(options: list) -> str:
    """选项集的有界展示形态（≤20 项原样 repr，超出带省略与总数）。"""
    if len(options) <= _OPTIONS_DISPLAY_LIMIT:
        return repr(options)
    return f"{options[:_OPTIONS_DISPLAY_LIMIT]!r}…（共 {len(options)} 项）"


def _normalize_field_value(
    column: str,
    field: dict,
    value: object,
    row: dict,
    dataset_options: dict | None = None,
) -> tuple[object, str | None]:
    """单字段值类型校验与归一；返回 (归一值, None) 或 (None, 错误文案)。

    ``row`` = 提交 raw values 整行（select 动态选项解析需要控制字段值——
    wire-form-linkage-v2；静态 options 字段不读 row，零漂移）。
    ``dataset_options`` = optionsFromDataset 字段的服务端实时解析结果
    ``{column: {"options": [...], "error": str|None}}``（wire-form-entry-
    advanced——提交校验两路径由 form_runtime 注入；缺省 None=未解析，
    动态源字段按空集裁决）。

    规则（design D3）：
    - ``number``：int/float 或可 ``float()`` 的字符串（NaN/Inf/非有限拒），
      归一为数值（字符串解析结果为 float；原生 int/float 保持原类型）；
      布尔值不是数值（Python bool 是 int 子类——显式拒）；
    - ``date``：字符串且 ``^\\d{4}-\\d{2}-\\d{2}$``（regex 口径，拒时间部分）；
    - ``select``：值必须 ∈ 当前解析选项——静态 options 与 optionsFrom 走
      :func:`resolve_select_options`（单一出处，错误文案带解析后选项列表）；
      optionsFromDataset 走服务端实时解析集（提交时刻有效视图，防篡改——
      解析失败如实标注源错误，非空值一律拒）；
    - ``text``：字符串原样；数字/布尔标量 stringify（布尔按 JSON 口径
      true/false）；list/dict 一律拒。
    """
    where = f"字段 {_field_label(field, column)}（{column}）"
    input_kind = field.get("input")
    if input_kind == "number":
        if isinstance(value, bool):
            return None, f"{where} 必须是数值（收到布尔值 {value}）"
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                return None, f"{where} 必须是有限数值（收到 {value!r}）"
            return value, None
        if isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                return None, (
                    f"{where} 必须是数值或可解析为数值的字符串（收到 {value!r}）"
                )
            if not math.isfinite(number):
                return None, f"{where} 必须是有限数值（收到 {value!r}）"
            return number, None
        return None, f"{where} 必须是数值或可解析为数值的字符串（收到 {value!r}）"
    if input_kind == "date":
        if not isinstance(value, str) or not _DATE_RE.match(value):
            return None, f"{where} 必须是 YYYY-MM-DD 格式日期（收到 {value!r}）"
        return value, None
    if input_kind == "select":
        if isinstance(field.get("optionsFromDataset"), dict):
            resolved = (
                dataset_options.get(column) if isinstance(dataset_options, dict) else None
            )
            options = (
                resolved.get("options")
                if isinstance(resolved, dict) and isinstance(resolved.get("options"), list)
                else []
            )
            source_error = (
                resolved.get("error")
                if isinstance(resolved, dict) and resolved.get("error")
                else None
            )
            if not isinstance(value, str) or value not in options:
                if source_error is not None:
                    return None, (
                        f"{where} 选项源当前不可用（{source_error}），"
                        f"不能提交非空值（收到 {value!r}）"
                    )
                return None, (
                    f"{where} 必须是选项之一 {_options_display(options)}"
                    f"（收到 {value!r}）"
                )
            return value, None
        options = resolve_select_options(field, row)
        if not isinstance(value, str) or value not in options:
            return None, f"{where} 必须是选项之一 {options}（收到 {value!r}）"
        return value, None
    # text（保存校验已拦越枚举 input；未知 input 兜底按 text 收——防御位）
    if isinstance(value, str):
        return value, None
    if isinstance(value, bool):
        return ("true" if value else "false"), None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None, f"{where} 必须是有限数值（收到 {value!r}）"
        return str(value), None
    return None, f"{where} 必须是字符串或标量（收到 {value!r}）"


def validate_form_values(
    fields: object,
    values: object,
    dataset_options: dict | None = None,
) -> tuple[dict | None, str | None]:
    """表单提交 values 校验（wire-form-builder design D3）——纯函数零依赖。

    入参：``fields`` = 钉死版本 ``spec.spec.fields``（字段定义数组）；
    ``values`` = 提交载荷的整行值；``dataset_options`` = optionsFromDataset
    字段的服务端实时解析结果（wire-form-entry-advanced——由调用方
    ``form_runtime`` 注入，缺省 None 时动态源字段按空集裁决，存量 spec
    零漂移）。返回 ``(归一后 values, None)`` 或 ``(None, 错误文案)``——
    文案字段级定位、可直接作 422 detail。

    规则：
    - ``values`` 必须是 JSON 对象；
    - 只收 ``fields`` 声明过的列——未定义列即拒（文案带可用列）；
    - **显隐联动**（wire-form-field-linkage / wire-form-linkage-v2）：
      先用提交 raw ``values`` 经 :func:`evaluate_hidden_columns` 求
      hidden 集（单条件/组条件/空选项三种隐藏来源同一出口），再逐字段——
      隐藏字段是已定义列（不触发未定义列拒）：携带非空值（非 None、非
      纯空白串）即拒（不做静默丢弃）；值为 None/空白/未传 → 跳过不写键
      且 **required 不检查**（隐藏必填不算缺失；空选项列同规——不可填
      即不可见，无需新逻辑）；
    - 可见字段照旧：必填字段缺失/空串/仅空白/null → 文案；
    - 非必填字段未传/传 null/空串 = 跳过（**不写键**——下游 insert 不落
      null 键，design D3「未勾字段不传即缺省」）；
    - 其余值按 ``_normalize_field_value`` 类型校验并归一——select 按
      raw ``values`` 重新解析当前选项做成员校验（optionsFrom 防篡改
      同规：伪造不属于解析选项的值即 422，文案带解析后选项列表）；
      optionsFromDataset 字段按 ``dataset_options`` 注入的服务端实时
      解析集做成员校验（提交时刻有效视图——防篡改，解析失败如实标注）。
    """
    if not isinstance(values, dict):
        return None, "values 必须是 JSON 对象（整行值）"
    if not isinstance(fields, list):
        return None, "表单字段定义缺失（fields 必须是数组）"
    known: dict[str, dict] = {}
    for field in fields:
        if not isinstance(field, dict):  # 防御位：保存校验已拦非对象字段
            continue
        column = field.get("column")
        if isinstance(column, str) and column:
            known[column] = field
    unknown = sorted(
        key for key in values if not isinstance(key, str) or key not in known
    )
    if unknown:
        available = "、".join(known) if known else "无"
        return None, (
            f"values 含未在表单中定义的列 {unknown[0]!r}（表单字段: {available}）"
        )
    hidden = evaluate_hidden_columns(fields, values)
    normalized: dict = {}
    for column, field in known.items():  # 声明序（首个错误即返回）
        value = values.get(column)
        if column in hidden:
            if value is None or (isinstance(value, str) and not value.strip()):
                continue  # 隐藏字段空值/未传 = 跳过不写键（required 不检查）
            return None, (
                f"字段 {_field_label(field, column)}（{column}）"
                "当前不可见，不能携带值"
            )
        if value is None or (isinstance(value, str) and not value.strip()):
            if field.get("required", False):
                return None, f"必填字段 {_field_label(field, column)}（{column}）缺失或为空"
            continue  # 非必填未传/空 = 跳过，不写键
        normalized_value, error = _normalize_field_value(
            column, field, value, values, dataset_options
        )
        if error is not None:
            return None, error
        normalized[column] = normalized_value
    return normalized, None
