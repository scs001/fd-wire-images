"""前提 → 告警规则的映射（add-decision-consistency-and-premise-watch 任务 3.1b）。

为什么是一个"映射器"而不是"创建器"
----------------------------------
规则的**创建**归 `artifacts` 面（walk: POST artifact kind=alert_rule），那里有鉴权、
版本、市场可见性。本模块只做一件事：把**已经判定为可监控**的前提翻成一份合法的
`alert_rule` spec，并明确告诉调用方"哪些没法翻、为什么"。

这样切分的好处：映射是纯函数（可单测、无 IO），而创建留在既有单一入口——不会多出
第二条写路径。

算子一一对应，不需要翻译
------------------------
`Decision.preconditions[].comparator` 的取值集合与
`alerts.spec_validate.CONDITION_OPS` **完全相同**（gt/gte/lt/lte/eq/neq）。
这不是巧合而是刻意对齐：两套契约若各自演化出不同的算子集，映射层迟早要写翻译表，
而翻译表就是漂移的温床。本模块因此直接透传，并在测试里钉住这一点。

window → intervalMinutes
------------------------
前提的观察窗是 `[start, end]`（一次结算窗口），而告警的 `intervalMinutes` 是
**多久评一次**。两者不是同一个量，绝不能相等传递。本模块要求调用方显式给
`interval_minutes`，缺省取 `DEFAULT_INTERVAL_MINUTES`，并在返回里回显所用值。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from .spec_validate import CONDITION_OPS, COOLDOWN_MAX, COOLDOWN_MIN, INTERVAL_MAX, INTERVAL_MIN

#: 前提默认评测间隔（分钟）。取 60：宏观/经营类指标多为月频，小时级巡检足够发现
#: "前提已不成立"，又不至于把评估频率抬高到无意义。
DEFAULT_INTERVAL_MINUTES = 60

#: 无法映射的原因码。分码才能被计数与巡检。
REASON_NOT_MONITORABLE = "not_monitorable"
REASON_BINDING_MISSING = "dataset_binding_missing"
REASON_COMPARATOR_UNSUPPORTED = "comparator_unsupported"
REASON_INTERVAL_OUT_OF_RANGE = "interval_out_of_range"


def premise_to_alert_spec(
    monitorable: Mapping[str, Any],
    *,
    decision_id: str,
    decision_title: Optional[str] = None,
    dataset_binding: Optional[Mapping[str, Any]] = None,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    cooldown_minutes: Optional[int] = None,
    notify_emails: Optional[Sequence[str]] = None,
) -> dict:
    """把一条**可监控**前提翻成 `alert_rule` 的 spec 内层。

    Input:
      monitorable —— `classify_preconditions()` 产出的 monitorable 条目
        （含 index / metric_ref / comparator / threshold / as_of / window）。
      decision_id —— 回指决策；会写进 `meta`，使一次触发能溯源到具体判断与前提序号。
      dataset_binding —— `metric_ref` → 数据面的解析结果：
        `{"dataset_id": ..., "value_column": ..., "filters": [...]?}`。
        **不给就返回 `binding_missing`**：本模块不去猜指标对应哪个数据集，
        猜错会让告警盯错指标，而且不会报错。
      interval_minutes —— 评测间隔；越界直接拒（不夹取：静默夹取会让调用方以为
        自己设的值生效了）。
      cooldown_minutes / notify_emails —— 透传给 spec，做与不做的判断留给调用方。

    Returns:
      成功 → `{"ok": True, "spec": {...}, "meta": {...}}`
      失败 → `{"ok": False, "reason": <码>, "detail": <可读原因>}`
    """
    if not isinstance(monitorable, Mapping):
        return {"ok": False, "reason": REASON_NOT_MONITORABLE,
                "detail": f"monitorable 必须是对象，收到 {type(monitorable).__name__}"}

    comparator = monitorable.get("comparator")
    if comparator not in CONDITION_OPS:
        return {
            "ok": False, "reason": REASON_COMPARATOR_UNSUPPORTED,
            "detail": (f"comparator {comparator!r} 不在告警算子集 {list(CONDITION_OPS)} 内"
                       "——两套契约本应一致，出现此错说明契约已分叉"),
        }

    if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool) \
            or not (INTERVAL_MIN <= interval_minutes <= INTERVAL_MAX):
        return {
            "ok": False, "reason": REASON_INTERVAL_OUT_OF_RANGE,
            "detail": (f"interval_minutes 必须是整数且介于 {INTERVAL_MIN}..{INTERVAL_MAX}"
                       f"（收到 {interval_minutes!r}；不静默夹取）"),
        }

    if not isinstance(dataset_binding, Mapping) or not dataset_binding.get("dataset_id"):
        return {
            "ok": False, "reason": REASON_BINDING_MISSING,
            "detail": (f"metric_ref {monitorable.get('metric_ref')!r} 未绑定到数据面"
                       "（需 dataset_id + value_column；本模块不猜）"),
        }

    threshold = monitorable.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return {
            "ok": False, "reason": REASON_NOT_MONITORABLE,
            "detail": f"threshold 必须是数值（收到 {threshold!r}；布尔值不算数值）",
        }

    inner: dict[str, Any] = {
        "query": {
            "dataset_id": dataset_binding["dataset_id"],
            "columns": [dataset_binding["value_column"]]
            if dataset_binding.get("value_column") else [],
            "filters": list(dataset_binding.get("filters") or []),
            "sort": [{"column": dataset_binding["value_column"], "dir": "desc"}]
            if dataset_binding.get("value_column") else [],
            "limit": 1,
        },
        "aggregate": {
            "func": dataset_binding.get("aggregate_func", "max"),
            **({"column": dataset_binding["value_column"]}
               if dataset_binding.get("value_column") else {}),
        },
        "condition": {"op": comparator, "value": float(threshold)},
        "intervalMinutes": interval_minutes,
    }
    if cooldown_minutes is not None:
        if isinstance(cooldown_minutes, bool) or not isinstance(cooldown_minutes, int) \
                or not (COOLDOWN_MIN <= cooldown_minutes <= COOLDOWN_MAX):
            return {
                "ok": False, "reason": REASON_NOT_MONITORABLE,
                "detail": (f"cooldown_minutes 需为整数且介于 {COOLDOWN_MIN}..{COOLDOWN_MAX}"
                           f"（收到 {cooldown_minutes!r}）"),
            }
        inner["cooldownMinutes"] = cooldown_minutes
    if notify_emails:
        inner["notifyEmails"] = list(notify_emails)

    index = monitorable.get("index")
    return {
        "ok": True,
        "spec": inner,
        "meta": {
            "decision_id": decision_id,
            "precondition_index": index,
            "metric_ref": monitorable.get("metric_ref"),
            "title": (decision_title or f"前提监控 · {monitorable.get('metric_ref')}")
                     + (f" · 前提{index}" if index is not None else ""),
        },
    }


def build_premise_rules(
    monitorable_items: Sequence[Mapping[str, Any]],
    *,
    decision_id: str,
    bindings: Mapping[Optional[str], Mapping[str, Any]],
    **kwargs: Any,
) -> dict:
    """批量映射：每条可监控前提 → 一条规则 spec；翻不了的进 `unmapped`。

    `bindings` 以 `metric_ref` 为键。缺失的键不报错、不猜——进 `unmapped` 并给出
    原因，调用方能立刻看出"哪些前提其实没被监控"。
    """
    rules: list[dict] = []
    unmapped: list[dict] = []
    for item in monitorable_items:
        metric_ref = item.get("metric_ref")
        res = premise_to_alert_spec(
            item, decision_id=decision_id,
            dataset_binding=bindings.get(metric_ref), **kwargs,
        )
        if res["ok"]:
            rules.append({"spec": res["spec"], "meta": res["meta"]})
        else:
            unmapped.append({"precondition_index": item.get("index"),
                             "metric_ref": metric_ref,
                             "reason": res["reason"], "detail": res["detail"]})
    total = len(monitorable_items)
    return {
        "rules": rules,
        "unmapped": unmapped,
        "counts": {"input": total, "mapped": len(rules), "unmapped": len(unmapped)},
        # 自证：输入每一条要么变成规则、要么进 unmapped，不许凭空消失。
        "complete": len(rules) + len(unmapped) == total,
    }
