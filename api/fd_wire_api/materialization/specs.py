"""产物 spec 的读取清单提取（宽容解析，三类产物）。

物化运行器不从 spec 硬猜读什么——先把「需要读取的指标/实体」清单提取
出来；**提取不出非空清单就抛 :class:`SpecReadListError` 如实报错，
绝不硬跑**（宁可一次诚实的失败，不可一次静默的空转/错读）。

## 输入形状

草稿 ``ai_draft_artifacts.spec`` 列存的是产物工具入参原样 JSON：
``{"title": ..., "spec": {...}}``。``normalize_spec`` 宽容处理两种形状：
外层带 ``spec`` 键（取内层）或不带（原样使用）——与 web 的 spec 查看
器（自由 JSON）和 5.4 的 TOOL_SCHEMAS（spec 为自由对象）一致。

## 提取规则（v1，宽容但可解释）

指标 code 的载体别名（dict 内）：``code`` / ``metric`` / ``indicator`` /
``metric_code`` / ``indicator_code``（全小写比较）。

- **dashboard**：顶层 ``metrics`` / ``indicators`` 列表；面板类列表
  ``panels`` / ``charts`` / ``widgets`` / ``series`` 内每项的
  ``metric`` / ``metrics`` / ``indicator`` / ``indicators`` / ``code``。
- **composite_indicator**：``sources`` / ``inputs`` / ``source_metrics`` /
  ``components`` / ``metrics`` 列表（元素为 code 字符串或带 code 别名的
  dict）。
- **research_report**：``data_scope`` / ``data_range`` / ``scope`` dict 内
  的 ``metrics`` / ``indicators``；``sections`` / ``outline`` / ``chapters``
  列表内每项的 ``metrics`` / ``indicators``。

可选附加：item dict 携带 ``tool``（读数工具名，默认 ``read``）与
``params`` / ``arguments`` / ``filters``（并入读数入参）。

其它 kind（alert_rule / refresh_schedule）没有读取清单提取器——
物化读取只对三类数据产物有意义，报错说明不支持。清单去重保序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 支持物化读取清单提取的产物类型
MATERIALIZABLE_KINDS: frozenset[str] = frozenset(
    {"dashboard", "composite_indicator", "research_report"}
)

#: 默认读数工具（business-mcp 的通用读；read_range 等经 item["tool"] 覆盖）
DEFAULT_READ_TOOL = "read"

#: item dict 里指标 code 的载体别名（小写）
_CODE_ALIASES: tuple[str, ...] = (
    "code",
    "metric",
    "indicator",
    "metric_code",
    "indicator_code",
)

#: item dict 里读数参数的载体别名
_PARAM_ALIASES: tuple[str, ...] = ("params", "arguments", "filters")


class SpecReadListError(ValueError):
    """读取清单提取失败（形状不认识/清单为空/类型不支持）——如实报错不硬跑。"""


@dataclass(frozen=True)
class ReadItem:
    """一项物化读取：调哪个读数工具、取哪个指标、带什么参数。"""

    code: str
    tool: str = DEFAULT_READ_TOOL
    params: dict = field(default_factory=dict)
    #: 提取来源位置（错误信息可解释用，如 "panels[1].metric"）
    origin: str = ""


def normalize_spec(spec: Any) -> dict:
    """宽容归一：外层 ``{"title":..., "spec": {...}}`` 取内层，否则原样。"""
    if not isinstance(spec, dict):
        raise SpecReadListError(f"spec 须为 JSON 对象，收到 {type(spec).__name__}")
    inner = spec.get("spec")
    if isinstance(inner, dict):
        return inner
    return spec


def _code_from_item(item: Any) -> str | None:
    """从 item（str 或 dict）提取指标 code；提不出返回 None。"""
    if isinstance(item, str):
        text = item.strip()
        return text or None
    if isinstance(item, dict):
        for key in _CODE_ALIASES:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _params_from_item(item: Any) -> dict:
    if isinstance(item, dict):
        for key in _PARAM_ALIASES:
            value = item.get(key)
            if isinstance(value, dict):
                return dict(value)
    return {}


def _tool_of(item: Any, parent: Any = None) -> str:
    """读数工具名：item 自身优先，其次所在容器（面板级 tool 覆盖整组）。"""
    for source in (item, parent):
        if isinstance(source, dict):
            value = source.get("tool")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return DEFAULT_READ_TOOL


def _params_of(item: Any, parent: Any = None) -> dict:
    """读数参数：item 自身优先，其次所在容器（面板级 params 覆盖整组）。"""
    for source in (item, parent):
        params = _params_from_item(source)
        if params:
            return params
    return {}


def _collect_from_list(
    items: Any, origin: str, sink: list[ReadItem], *, parent: Any = None
) -> int:
    """从一段列表提取 code 项（元素为 str 或带 code 别名的 dict）。

    ``parent`` 为所在容器 dict（如面板）：item 自身未带 tool/params 时继承。
    """
    if not isinstance(items, list):
        return 0
    count = 0
    for index, item in enumerate(items):
        code = _code_from_item(item)
        if code is None:
            continue
        sink.append(
            ReadItem(
                code=code,
                tool=_tool_of(item, parent),
                params=_params_of(item, parent),
                origin=f"{origin}[{index}]",
            )
        )
        count += 1
    return count


def _extract_dashboard(spec: dict) -> list[ReadItem]:
    items: list[ReadItem] = []
    for key in ("metrics", "indicators"):
        _collect_from_list(spec.get(key), key, items)
    for container in ("panels", "charts", "widgets", "series"):
        entries = spec.get(container)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if isinstance(entry, str):
                code = _code_from_item(entry)
                if code:
                    items.append(ReadItem(code=code, origin=f"{container}[{index}]"))
                continue
            if not isinstance(entry, dict):
                continue
            for key in ("metric", "metrics", "indicator", "indicators", "code"):
                value = entry.get(key)
                if isinstance(value, str):
                    code = _code_from_item(value)
                    if code:
                        items.append(
                            ReadItem(
                                code=code,
                                tool=_tool_of(entry),
                                params=_params_of(entry),
                                origin=f"{container}[{index}].{key}",
                            )
                        )
                elif isinstance(value, list):
                    _collect_from_list(
                        value, f"{container}[{index}].{key}", items, parent=entry
                    )
    return items


def _extract_composite(spec: dict) -> list[ReadItem]:
    items: list[ReadItem] = []
    for key in ("sources", "inputs", "source_metrics", "components", "metrics"):
        _collect_from_list(spec.get(key), key, items)
    return items


def _extract_report(spec: dict) -> list[ReadItem]:
    items: list[ReadItem] = []
    for scope_key in ("data_scope", "data_range", "scope"):
        scope = spec.get(scope_key)
        if not isinstance(scope, dict):
            continue
        for key in ("metrics", "indicators"):
            _collect_from_list(scope.get(key), f"{scope_key}.{key}", items)
    for container in ("sections", "outline", "chapters"):
        entries = spec.get(container)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            for key in ("metrics", "indicators"):
                _collect_from_list(
                    entry.get(key), f"{container}[{index}].{key}", items
                )
    return items


#: kind → 提取器（宽容解析的收口；新增类型在此登记并写清规则）
_EXTRACTORS = {
    "dashboard": _extract_dashboard,
    "composite_indicator": _extract_composite,
    "research_report": _extract_report,
}


def extract_read_list(kind: str, spec: Any) -> list[ReadItem]:
    """提取读取清单；失败路径全部抛 :class:`SpecReadListError`。

    - kind 不在三类数据产物内 → 报「类型不支持」；
    - 归一后提取结果为空 → 报「清单为空」并附已扫描位置，不硬跑。
    """
    extractor = _EXTRACTORS.get(kind)
    if extractor is None:
        raise SpecReadListError(
            f"产物类型 {kind!r} 无读取清单提取器"
            f"（支持: {sorted(MATERIALIZABLE_KINDS)}；alert_rule/refresh_schedule"
            " 不产生物化读取）"
        )
    normalized = normalize_spec(spec)
    items = extractor(normalized)
    if not items:
        raise SpecReadListError(
            f"{kind} spec 提取不出非空读取清单（已扫描顶层 metrics/indicators、"
            "面板/来源/范围等约定位置）——拒绝硬跑，请修正 spec 形状"
        )
    # 去重保序（同 code 的二次出现通常是布局重复引用，不是二次读取）
    seen: set[str] = set()
    deduped: list[ReadItem] = []
    for item in items:
        if item.code in seen:
            continue
        seen.add(item.code)
        deduped.append(item)
    return deduped
