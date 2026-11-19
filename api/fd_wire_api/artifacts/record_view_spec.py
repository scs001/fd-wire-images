"""record-view 产物 spec 结构校验（wire-record-view 任务 1.2，同层
form_spec——design D1 第①层）。

纯函数零依赖（不触库、不触引擎——artifacts 库内可独立测试）。校验对象是
spec 信封的 ``spec.spec`` 部分：``{datasetId, keyColumn, headerFields[],
sections[]}``：

- ``datasetId``：null（构建中间态——可保存不可生成分享）或非空字符串；
  **为 null 时 keyColumn 必须为 null、headerFields/sections 必须为空数组**
  （零半写：不允许「绑了键列/头部/时间段却没绑主数据集」的半配置，422
  定位字段）；
- ``keyColumn``：datasetId 非空时必须是非空字符串（主记录键列，如批次号
  ——发布后扫码按该列等值取主记录）；∈ 主数据集列快照在 service 层裁决
  （第②层，保存时刻列快照口径）；
- ``headerFields``：数组 ≤50；每项 ``{column, label?}``——column 非空
  字符串且页内唯一（去重）、label 可省略（缺省=列名）、在则非空 ≤200；
  column ∈ 主集列快照在 service 层裁决；
- ``sections``：数组 ≤10；每项 ``{title, datasetId, dateColumn, filters?,
  limit?}``——title 非空 ≤200；datasetId 非空字符串（本企业现存、可与主集
  相同——属主在 service 层裁决）；dateColumn 非空字符串（∈该集列快照在
  service 层裁决）；filters 数组 ≤10、每项 ``{column, op, value}``——
  column 非空字符串、op ∈ :data:`RECORD_VIEW_FILTER_OPS`（eq/in/gt/gte/
  lt/lte，与 query_contract 过滤白名单同界）、value=非空标量 | 标量数组
  （数组形态仅 op=in）| ``{fromField: <主记录列>}``（fromField 非空字符串，
  ∈ 主集列快照在 service 层裁决）；limit 1..500 整数（缺省 200）。

列快照与属主校验是第②层（``artifacts.service._require_record_view_
datasets`` 经 datasets 库会话完成，照 form 的 ``_require_form_dataset``
口径：本企业现存否则 422 零半写）。section 数据集的**过滤列**与日期列后续
漂移不回溯存量版本（append-only），取数时以单段 error 如实标注（不静默
空段）——见 ``artifacts.record_view_runtime``。

返回错误文案（None=合法）；文案带字段定位（如 ``sections[0].dateColumn
不在数据集 … 列快照内``）逐条可断言。
"""

from __future__ import annotations

import re

#: 文案字段（label / title）长度上限（同 form_spec.TEXT_LIMIT）
TEXT_LIMIT = 200

#: headerFields 条数上限（冻结契约 ≤50）
HEADER_FIELDS_LIMIT = 50

#: sections 条数上限（冻结契约 ≤10——每扫码 section 查询放大上界）
SECTIONS_LIMIT = 10

#: 每 section filters 条数上限（冻结契约 ≤10）
SECTION_FILTERS_LIMIT = 10

#: section.limit 缺省值与硬上限（冻结契约 1..500、缺省 200——与
#: optionsFromDataset.limit 同界）
SECTION_LIMIT_DEFAULT = 200
SECTION_LIMIT_MAX = 500

#: section 过滤算子白名单（与 query_contract._FILTER_OPS / probe 同界——
#: 公开取数端点只执行钉死版本声明的这六种算子）
RECORD_VIEW_FILTER_OPS: tuple[str, ...] = ("eq", "in", "gt", "gte", "lt", "lte")

#: 运行时按 dateColumn 升序的「日期形」口径（前缀匹配——收 YYYY-MM-DD 与
#: 带时间部分两形态；空/非法值取数时排末尾，保存时不裁决值形状）
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _text_error(value: object, *, what: str) -> str | None:
    """非空字符串 ≤ :data:`TEXT_LIMIT` 校验；返回错误文案或 None（同 form_spec）。"""
    if not isinstance(value, str) or not value.strip():
        return f"{what} 必须是非空字符串"
    if len(value) > TEXT_LIMIT:
        return f"{what} 长度超过 {TEXT_LIMIT} 上限"
    return None


def _is_literal_scalar(value: object) -> bool:
    """字面量过滤值的标量口径：str（trim 后非空）/int/float/bool；
    null 与容器形态一律非标量（bool ⊂ int——按 JSON 值域收）。"""
    if value is None or isinstance(value, (dict, list)):
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return isinstance(value, (str, int, float))


def _section_filter_error(flt: object, where: str) -> str | None:
    """section filters 条件形状校验：``{column, op, value}``，value=非空标量、
    标量数组（仅 op=in）或 ``{fromField}`` 引用形态（fromField 引用主记录列
    ——列快照存在性在 service 层裁决）。"""
    if not isinstance(flt, dict):
        return f"{where} 必须是 JSON 对象（{{column, op, value}}）"
    column = flt.get("column")
    if not isinstance(column, str) or not column.strip():
        return f"{where}.column 必须是非空字符串（过滤列名）"
    op = flt.get("op")
    if op not in RECORD_VIEW_FILTER_OPS:
        return f"{where}.op 必须是 {'/'.join(RECORD_VIEW_FILTER_OPS)}（收到 {op!r}）"
    if "value" not in flt:
        return f"{where}.value 必须是非空标量、标量数组或 {{fromField}} 引用（op={op} 必带）"
    value = flt["value"]
    if isinstance(value, dict):
        if set(value) != {"fromField"} or not isinstance(
            value.get("fromField"), str
        ) or not value["fromField"].strip():
            return (
                f"{where}.value 引用形态必须是 {{fromField: <主记录列>}}"
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


def _section_error(section: object, where: str) -> str | None:
    """单个 section 形状校验：``{title, datasetId, dateColumn, filters?, limit?}``。"""
    if not isinstance(section, dict):
        return f"{where} 必须是 JSON 对象（{{title, datasetId, dateColumn, filters?, limit?}}）"
    title_error = _text_error(section.get("title"), what=f"{where}.title")
    if title_error is not None:
        return title_error
    dataset_id = section.get("datasetId")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return f"{where}.datasetId 必须是非空字符串（时间轴源数据集 id，可与主数据集相同）"
    date_column = section.get("dateColumn")
    if not isinstance(date_column, str) or not date_column.strip():
        return f"{where}.dateColumn 必须是非空字符串（时间轴排序日期列）"
    filters = section.get("filters")
    if filters is not None:
        if (
            not isinstance(filters, list)
            or len(filters) > SECTION_FILTERS_LIMIT
        ):
            return (
                f"{where}.filters 必须是数组（≤{SECTION_FILTERS_LIMIT} 条，收到 "
                f"{len(filters) if isinstance(filters, list) else filters!r}）"
            )
        for index, flt in enumerate(filters):
            error = _section_filter_error(flt, f"{where}.filters[{index}]")
            if error is not None:
                return error
    limit = section.get("limit")
    if limit is not None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= SECTION_LIMIT_MAX
        ):
            return (
                f"{where}.limit 必须是 1..{SECTION_LIMIT_MAX} 的整数"
                f"（缺省 {SECTION_LIMIT_DEFAULT}，收到 {limit!r}）"
            )
    return None


def validate_record_view_spec(inner: object) -> str | None:
    """record-view spec 结构校验（``spec.spec`` 部分）；返回错误文案
    （None=合法）。

    调用方（``artifacts.service``）已先过通用口径 validate_spec（信封
    title 非空 + spec.spec 非空对象）——本函数只管 record-view 结构层，
    含「datasetId 为空 ⇒ keyColumn/headerFields/sections 全空」的中间态
    半写拦截。
    """
    if not isinstance(inner, dict):
        return "spec.spec 必须是 JSON 对象"
    dataset_id = inner.get("datasetId")
    if dataset_id is not None and (
        not isinstance(dataset_id, str) or not dataset_id.strip()
    ):
        return "spec.spec.datasetId 必须是 null 或非空字符串（主记录数据集 id）"
    if dataset_id is None:
        # 未绑数据集的中间态：keyColumn 必须为空、headerFields/sections
        # 必须为空数组（零半写——头部/时间轴引用的列快照无从校验）
        if inner.get("keyColumn") is not None:
            return "spec.spec.keyColumn 必须为空（datasetId 未绑定时不可配置键列）"
        header_fields = inner.get("headerFields")
        if header_fields is None:
            return "spec.spec.headerFields 必须是数组"
        if header_fields != []:
            return "spec.spec.headerFields 必须为空数组（datasetId 未绑定时不可配置头部字段）"
        sections = inner.get("sections")
        if sections is None:
            return "spec.spec.sections 必须是数组"
        if sections != []:
            return "spec.spec.sections 必须为空数组（datasetId 未绑定时不可配置时间轴段）"
        return None
    # datasetId 已绑定：keyColumn 必须就位（发布后扫码按它等值取主记录）
    key_column = inner.get("keyColumn")
    if not isinstance(key_column, str) or not key_column.strip():
        return "spec.spec.keyColumn 必须是非空字符串（主记录键列，如批次号）"
    header_fields = inner.get("headerFields")
    if not isinstance(header_fields, list):
        return "spec.spec.headerFields 必须是数组"
    if len(header_fields) > HEADER_FIELDS_LIMIT:
        return (
            f"spec.spec.headerFields 不能超过 {HEADER_FIELDS_LIMIT} 条"
            f"（收到 {len(header_fields)} 条）"
        )
    seen_columns: set[str] = set()
    for index, field in enumerate(header_fields):
        where = f"headerFields[{index}]"
        if not isinstance(field, dict):
            return f"{where} 必须是 JSON 对象（{{column, label?}}）"
        column = field.get("column")
        if not isinstance(column, str) or not column.strip():
            return f"{where}.column 必须是非空字符串（主记录数据集列名）"
        if column in seen_columns:
            return f"{where}.column 重复: {column}"
        seen_columns.add(column)
        if "label" in field:
            label_error = _text_error(field["label"], what=f"{where}.label")
            if label_error is not None:
                return label_error
    sections = inner.get("sections")
    if not isinstance(sections, list):
        return "spec.spec.sections 必须是数组"
    if len(sections) > SECTIONS_LIMIT:
        return (
            f"spec.spec.sections 不能超过 {SECTIONS_LIMIT} 条"
            f"（收到 {len(sections)} 条）"
        )
    for index, section in enumerate(sections):
        error = _section_error(section, f"sections[{index}]")
        if error is not None:
            return error
    return None
