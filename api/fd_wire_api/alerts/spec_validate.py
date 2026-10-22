"""alert_rule 规则契约校验（wire-alert-evaluation 任务 1，纯函数零库面）。

校验对象是产物 spec 信封的 ``spec.spec`` 内层（与 form_spec 校验同一挂点
口径）::

    {
      "query":   {面板查询同形：dataset_id, filters?, sort?, limit?, columns?},
      "aggregate": {"column": "amt", "func": "sum|avg|min|max|count"},
      "condition": {"op": "gt|gte|lt|lte|eq|neq", "value": 100},
      "intervalMinutes": 30,
      "webhookUrl": "https://…",       # 可选
      "cooldownMinutes": 60            # 可选，缺省 60
    }

- ``query`` 必须过 :func:`fd_wire_api.artifacts.query_contract.parse_panel_query`
  （不过 → 错误文案带其口径——评估口径=刷新口径，同一契约面）；
- ``aggregate.func`` ∈ sum/avg/min/max/count；func≠count 时 ``column``
  必填非空字符串（count 数行不取列，column 可省）；
- ``condition.op`` ∈ gt/gte/lt/lte/eq/neq；``value`` 必须数值（布尔拒收
  ——Python bool 是 int 子类，须显式先判）；
- ``intervalMinutes``：整数 ∈ [5, 1440]；``cooldownMinutes`` 可省（缺省
  60），在则整数 ∈ [1, 1440]；
- ``webhookUrl`` 可省，在则以 http:// 或 https:// 开头（前缀校验，不做
  网络探测）。

返回错误文案（None=合法）；文案定位字段（如 ``intervalMinutes 必须…``）
可直接作保存面 400 detail 与试运行面 422 detail。
"""

from __future__ import annotations

from ..artifacts import query_contract

#: 聚合函数枚举（与 api_store.AGGREGATE_FNS 同值域）
AGGREGATE_FUNCS: tuple[str, ...] = ("sum", "avg", "min", "max", "count")
#: 条件算子枚举
CONDITION_OPS: tuple[str, ...] = ("gt", "gte", "lt", "lte", "eq", "neq")
#: 评估间隔区间（分钟）
INTERVAL_MIN, INTERVAL_MAX = 5, 1440
#: 冷却区间（分钟）；缺省值见 :data:`DEFAULT_COOLDOWN_MINUTES`
COOLDOWN_MIN, COOLDOWN_MAX = 1, 1440
#: 冷却缺省（分钟）
DEFAULT_COOLDOWN_MINUTES = 60


def _int_in_range(value: object, *, low: int, high: int) -> bool:
    """区间判定：真整数（bool 拒——bool 是 int 子类须先判）且 low ≤ v ≤ high。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return low <= value <= high


def _number_error(value: object) -> bool:
    """数值判定（bool 拒）：int/float 视为数值。"""
    return isinstance(value, bool) or not isinstance(value, (int, float))


def validate_alert_spec(inner: object) -> str | None:
    """alert_rule 规则契约校验（``spec.spec`` 内层）；返回错误文案或 None。"""
    if not isinstance(inner, dict):
        return "spec.spec 必须是 JSON 对象（alert_rule 规则契约）"

    # query：与面板/试取同一解析面（parse_panel_query），不过即拒
    query = query_contract.parse_panel_query(inner.get("query"))
    if query is None:
        return (
            "query 必须是合法的面板查询契约：dataset_id 必须是非空字符串；"
            "filters ≤50 项且每项 column 非空、op ∈ {eq,in,gt,gte,lt,lte,"
            "contains,startswith}；sort ≤10 项且 dir ∈ {asc,desc}；limit 为"
            "整数（夹取 1..1000）；columns 为非空字符串数组（去重保序）"
        )

    # aggregate：func 枚举 + column 必填性（count 数行不取列）
    aggregate = inner.get("aggregate")
    if not isinstance(aggregate, dict):
        return "aggregate 必须是 JSON 对象 {column?, func}"
    func = aggregate.get("func")
    if func not in AGGREGATE_FUNCS:
        return f"aggregate.func 必须是 {'/'.join(AGGREGATE_FUNCS)}（收到 {func!r}）"
    column = aggregate.get("column")
    if func == "count":
        if column is not None and (not isinstance(column, str) or not column.strip()):
            return "aggregate.column 必须是非空字符串（func=count 可整体省略）"
    elif not isinstance(column, str) or not column.strip():
        return f"aggregate.column 必须是非空字符串（func={func} 必带数值列）"

    # condition：算子枚举 + value 数值（bool 拒——bool 是 int 子类须显式先判）
    condition = inner.get("condition")
    if not isinstance(condition, dict):
        return "condition 必须是 JSON 对象 {op, value}"
    op = condition.get("op")
    if op not in CONDITION_OPS:
        return f"condition.op 必须是 {'/'.join(CONDITION_OPS)}（收到 {op!r}）"
    value = condition.get("value")
    if _number_error(value):
        return f"condition.value 必须是数值（收到 {value!r}；布尔值不算数值）"

    # intervalMinutes：整数 ∈ [5,1440]
    interval = inner.get("intervalMinutes")
    if not _int_in_range(interval, low=INTERVAL_MIN, high=INTERVAL_MAX):
        return (
            f"intervalMinutes 必须是整数且介于 {INTERVAL_MIN}..{INTERVAL_MAX}"
            f"（收到 {interval!r}）"
        )

    # cooldownMinutes：可省（缺省 60），在则整数 ∈ [1,1440]
    if "cooldownMinutes" in inner and not _int_in_range(
        inner["cooldownMinutes"], low=COOLDOWN_MIN, high=COOLDOWN_MAX
    ):
        return (
            f"cooldownMinutes 可省（缺省 {DEFAULT_COOLDOWN_MINUTES}），"
            f"在则必须是整数且介于 {COOLDOWN_MIN}..{COOLDOWN_MAX}"
            f"（收到 {inner['cooldownMinutes']!r}）"
        )

    # webhookUrl：可省，在则 http(s):// 前缀（不做网络探测）
    if "webhookUrl" in inner:
        url = inner["webhookUrl"]
        if not isinstance(url, str) or not (
            url.startswith("http://") or url.startswith("https://")
        ):
            return f"webhookUrl 可省，在则必须以 http:// 或 https:// 开头（收到 {url!r}）"

    return None


def normalize_alert_spec(inner: dict) -> dict:
    """评估器消费视图：校验后的内层 → 带缺省补全的参数 dict。

    调用方保证先过 :func:`validate_alert_spec`（None 才进本函数）；
    ``cooldownMinutes`` 补缺省 60，其余原样。
    """
    cooldown = inner.get("cooldownMinutes")
    if not isinstance(cooldown, int) or isinstance(cooldown, bool):
        cooldown = DEFAULT_COOLDOWN_MINUTES
    return {
        "query": query_contract.parse_panel_query(inner["query"]),
        "column": inner["aggregate"].get("column"),
        "func": inner["aggregate"]["func"],
        "op": inner["condition"]["op"],
        "value": float(inner["condition"]["value"]),
        "interval_minutes": inner["intervalMinutes"],
        "cooldown_minutes": cooldown,
        "webhook_url": inner.get("webhookUrl") or None,
    }
