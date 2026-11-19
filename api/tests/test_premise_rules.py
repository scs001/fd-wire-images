"""前提 → 告警规则映射（add-decision-consistency-and-premise-watch 任务 3.1b / 3.2）。

纯函数，无 IO、不创建规则、不真打网。守四件事：
1. 算子一一对应，两套契约不许分叉；
2. `metric_ref` 没绑数据面时**不猜**，进 unmapped 并给原因；
3. `interval_minutes` 越界不静默夹取；
4. 输入每一条要么变规则、要么进 unmapped，`complete` 自证不丢项。
"""

from __future__ import annotations

import pytest

from fd_wire_api.alerts.premise_rules import (
    DEFAULT_INTERVAL_MINUTES,
    REASON_BINDING_MISSING,
    REASON_COMPARATOR_UNSUPPORTED,
    REASON_INTERVAL_OUT_OF_RANGE,
    build_premise_rules,
    premise_to_alert_spec,
)
from fd_wire_api.alerts.spec_validate import CONDITION_OPS, validate_alert_spec


def mon(index=0, metric_ref="cell/store_gross_margin", comparator="gte", threshold=0.42):
    return {"index": index, "metric_ref": metric_ref, "comparator": comparator,
            "threshold": threshold, "as_of": "2026-11-10",
            "window": ["2026-11-10", "2027-05-10"]}


BINDING = {"dataset_id": "ds-store-margin", "value_column": "gross_margin",
           "filters": [{"column": "store_id", "op": "eq", "value": "store-0413"}]}


def test_operator_set_is_pinned_to_the_decision_contract():
    """算子集合钉死为 Decision v2 的六个值。

    为什么用字面量而不是 `from fd_quant... import COMPARATORS`：fd-wire 不依赖
    fd-quant（跨仓 import 会把两个服务的发布节奏绑在一起）。所以两侧各自钉一份
    字面量：任一侧改了算子，**对应侧的这个测试立刻红**，由人来对齐——这比
    静默引入一张翻译表可靠。
    """
    assert set(CONDITION_OPS) == {"gt", "gte", "lt", "lte", "eq", "neq"}


def test_mapped_spec_passes_the_real_alert_validator():
    """映射产物必须能过告警侧自己的契约校验——否则等于造了一个存不进去的形态。"""
    res = premise_to_alert_spec(mon(), decision_id="d-1", dataset_binding=BINDING)

    assert res["ok"] is True
    assert validate_alert_spec(res["spec"]) is None


def test_meta_carries_decision_and_precondition_index():
    res = premise_to_alert_spec(mon(index=2), decision_id="d-9",
                                dataset_binding=BINDING)

    assert res["meta"]["decision_id"] == "d-9"
    assert res["meta"]["precondition_index"] == 2
    assert "前提2" in res["meta"]["title"]


def test_missing_binding_is_reported_not_guessed():
    res = premise_to_alert_spec(mon(), decision_id="d-1", dataset_binding=None)

    assert res["ok"] is False
    assert res["reason"] == REASON_BINDING_MISSING
    assert "不猜" in res["detail"] or "未绑定" in res["detail"]


def test_unsupported_comparator_is_reported():
    res = premise_to_alert_spec(mon(comparator="约等于"), decision_id="d-1",
                                dataset_binding=BINDING)

    assert res["ok"] is False
    assert res["reason"] == REASON_COMPARATOR_UNSUPPORTED


@pytest.mark.parametrize("interval", [0, 4, 1441, 60.5, True])
def test_interval_out_of_range_is_refused_not_clamped(interval):
    res = premise_to_alert_spec(mon(), decision_id="d-1",
                                dataset_binding=BINDING, interval_minutes=interval)

    assert res["ok"] is False
    assert res["reason"] == REASON_INTERVAL_OUT_OF_RANGE
    assert "不静默夹取" in res["detail"]


def test_default_interval_is_within_validator_range():
    assert 5 <= DEFAULT_INTERVAL_MINUTES <= 1440


def test_batch_is_complete_when_some_bindings_missing():
    items = [mon(index=0, metric_ref="a"), mon(index=1, metric_ref="b")]
    out = build_premise_rules(items, decision_id="d-1",
                              bindings={"a": BINDING})

    assert out["counts"] == {"input": 2, "mapped": 1, "unmapped": 1}
    assert out["complete"] is True
    assert out["unmapped"][0]["metric_ref"] == "b"
    assert out["unmapped"][0]["reason"] == REASON_BINDING_MISSING


def test_no_second_scheduler_is_introduced():
    """任务 3.2：复用既有评测与状态机，本模块不得引入调度。

    判据是**代码走查**而非行为：模块里不出现 asyncio/task 句柄/定时器。
    """
    import inspect

    import fd_wire_api.alerts.premise_rules as mod

    src = inspect.getsource(mod)
    for banned in ("asyncio", "create_task", "Timer", "schedule.every",
                   "while True", "sleep("):
        assert banned not in src, f"映射层不应出现调度原语：{banned}"
