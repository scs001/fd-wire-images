"""流水线衔接服务（wire-platform-v1 任务 6.5：wire-demand-pipeline）。

真实流水线执行（scraw-builder 批量生成、conformance gate、测试舰队）住在
fd-industry-data 等外部仓——本模块只做「事实留档」：

- :func:`record_pipeline_run`——处理期（in_progress）内部端点把流水线
  事实（pipeline_run_id/repo/branch/note）append-only 落 ``pipeline_events``；
- :func:`record_gate_result`——conformance gate 结果（真实命令为
  ``python3 fd-industry-data/scripts/conformance_gate.py``，从 finddata 根
  跑、只读；本仓绝不代跑）：wire-ux-consolidation 3.1 起 gate 不再门禁
  状态迁移（building→delivery_acceptance 前置随废除态移除），仅作构建
  质量观测留档（design D1：in_progress 观测挂点，为萬星闭环预留）；
- :func:`has_gate_pass`——门禁视图仍供内部参考（不再被状态机消费）。

所有事件 append-only（与 status_history 同一审计纪律）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import Demand
from .statemachine import DemandStatus, TransitionError

EVENT_PIPELINE_RUN = "pipeline_run"
EVENT_GATE_RESULT = "gate_result"


def _require_in_progress(demand: Demand, *, action: str) -> None:
    """流水线事实只在 in_progress 状态记录（接单之后、交付之前）。"""
    current = DemandStatus(demand.status)
    if current is not DemandStatus.in_progress:
        raise TransitionError(
            current,
            DemandStatus.in_progress,
            f"{action}只能在 in_progress 状态记录（当前 {current.value}）——"
            "流水线事实属于处理期（submitted→in_progress 之后）",
        )


def record_pipeline_run(
    db: Session,
    demand: Demand,
    *,
    pipeline_run_id: str,
    repo: str | None = None,
    branch: str | None = None,
    note: str | None = None,
    by: str = "pipeline",
) -> Demand:
    """记录一次流水线事实（append-only）：run id / 仓 / 分支 / 备注。"""
    _require_in_progress(demand, action="流水线事实")
    run_id = (pipeline_run_id or "").strip()
    if not run_id:
        raise TransitionError(
            DemandStatus(demand.status),
            DemandStatus.in_progress,
            "pipeline_run_id 不能为空（流水线事实必须可追溯到具体 run）",
        )
    demand.append_pipeline_event(
        {
            "type": EVENT_PIPELINE_RUN,
            "pipeline_run_id": run_id,
            "repo": repo,
            "branch": branch,
            "note": note,
            "by": by,
        }
    )
    db.commit()
    db.refresh(demand)
    return demand


def record_gate_result(
    db: Session,
    demand: Demand,
    *,
    passed: bool,
    report_path: str | None = None,
    note: str | None = None,
    by: str = "pipeline",
) -> Demand:
    """记录一次 conformance gate 结果（append-only，观测留档）。

    wire-ux-consolidation 3.1 起 gate 不再门禁状态迁移：passed 与否都
    只记录（note 如实入档），需求单状态不受影响——三态机的
    in_progress→delivered 由派发操作裁决。
    """
    _require_in_progress(demand, action="gate 结果")
    demand.append_pipeline_event(
        {
            "type": EVENT_GATE_RESULT,
            "passed": bool(passed),
            "report_path": report_path,
            "note": note,
            "by": by,
        }
    )
    db.commit()
    db.refresh(demand)
    return demand


def has_gate_pass(demand: Demand) -> bool:
    """是否已有至少一条 gate 通过记录（内部观测视图；不再门禁迁移）。"""
    return any(
        event.get("type") == EVENT_GATE_RESULT and event.get("passed") is True
        for event in (demand.pipeline_events or [])
    )
