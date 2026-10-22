"""数据需求单三态状态机（wire-ux-consolidation 任务 3.1，自八态压缩）。

状态全集（4 态）：
    submitted → in_progress → delivered；submitted/in_progress 可 → rejected。

废除态（5 个，wire-ux-consolidation 前的八态工作流遗留）：
    evaluating / quoted / awaiting_confirm / building / delivery_acceptance
    ——不可迁入（迁入一律 409）。枚举成员仅为两类场景保留：
    1. 存量读窗口：部署与归并迁移（alembic 0019）之间的短暂窗口内，
       旧库行反序列化 ``DemandStatus("building")`` 等不炸；
    2. 如实回显：对废除态的迁移请求能以 409 + 明确原因回绝，而非 422。
    白名单没有任何指向废除态的边，二者都收敛到 409。

纪律：
- 迁移只经 ``ALLOWED_TRANSITIONS`` 白名单，非法迁移抛 ``TransitionError``
  （路由层统一转 409）。
- 进入 ``rejected`` 必带 ``reason``，留档于 ``rejection_reason``（沿用既有
  needs-reason 机制）。
- ``evaluating→quoted 需运营确认`` 与 ``building→delivery_acceptance 需
  conformance gate 通过`` 两类前置随八态工作流一并废除（前置的两端状态
  均不存在于三态机），本文件不再校验。
- ``status_history`` 只追加（append-only），任何迁移不得改写历史条目。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class DemandStatus(str, Enum):
    """需求单状态（客户全程可见）。

    现行三态 + rejected：
    - submitted    已收到（客户提交，1 个工作日内响应）
    - in_progress  处理中（派发/构建进行时——将来可换 agent 进度）
    - delivered    已交付
    - rejected     已拒绝（必带理由留档）

    其余五个成员为废除态（见模块 docstring），只读不迁。
    """

    submitted = "submitted"                    # 已收到
    in_progress = "in_progress"                # 处理中
    delivered = "delivered"                    # 已交付
    rejected = "rejected"                      # 已拒绝（带理由留档）
    # ---- 废除态（存量读窗口兼容，白名单不可迁入）----
    evaluating = "evaluating"                  # [废除] 评估中
    quoted = "quoted"                          # [废除] 已报价
    awaiting_confirm = "awaiting_confirm"      # [废除] 待确认
    building = "building"                      # [废除] 构建中
    delivery_acceptance = "delivery_acceptance"  # [废除] 交付验收


#: 现行可用状态全集（新单只在此集合内流转）。
ACTIVE_STATUSES: frozenset[DemandStatus] = frozenset(
    {
        DemandStatus.submitted,
        DemandStatus.in_progress,
        DemandStatus.delivered,
        DemandStatus.rejected,
    }
)

#: 废除态：不可迁入（迁入即 409）；存量行由 alembic 0019 归并为 in_progress。
LEGACY_STATUSES: frozenset[DemandStatus] = frozenset(
    {
        DemandStatus.evaluating,
        DemandStatus.quoted,
        DemandStatus.awaiting_confirm,
        DemandStatus.building,
        DemandStatus.delivery_acceptance,
    }
)

#: 终态：不再有任何出边。
TERMINAL_STATUSES: frozenset[DemandStatus] = frozenset(
    {DemandStatus.delivered, DemandStatus.rejected}
)

#: 合法迁移白名单（current → 允许的 target 集合）。
#:
#: - submitted→in_progress    派发台/内部接单开始处理
#: - submitted→rejected       受理即拒（必带 reason）
#: - in_progress→delivered    处理完成交付
#: - in_progress→rejected     处理中拒绝（必带 reason）
ALLOWED_TRANSITIONS: dict[DemandStatus, frozenset[DemandStatus]] = {
    DemandStatus.submitted: frozenset({DemandStatus.in_progress, DemandStatus.rejected}),
    DemandStatus.in_progress: frozenset({DemandStatus.delivered, DemandStatus.rejected}),
    DemandStatus.delivered: frozenset(),
    DemandStatus.rejected: frozenset(),
}


class TransitionError(Exception):
    """非法迁移或前置条件不满足（路由层转 409）。"""

    def __init__(self, current: DemandStatus, target: DemandStatus, why: str) -> None:
        self.current = current
        self.target = target
        self.why = why
        super().__init__(f"{current.value} → {target.value} 不允许：{why}")


def utc_now_iso() -> str:
    """历史时间戳：UTC ISO-8601（客户时间线的 ``at`` 字段）。"""
    return datetime.now(timezone.utc).isoformat()


def allowed_targets(current: DemandStatus) -> frozenset[DemandStatus]:
    """当前状态允许迁往的状态集合（废除态/终态一律空集）。"""
    return ALLOWED_TRANSITIONS.get(current, frozenset())


def ensure_transition(
    current: DemandStatus,
    target: DemandStatus,
    *,
    reason: str | None = None,
) -> None:
    """校验一次迁移的合法性与其前置条件，非法即抛 ``TransitionError``。

    前置条件（白名单之外额外强制）：
    - 任意→``rejected`` 必须携带非空 ``reason``（needs-reason 机制沿用）。

    旧八态的 ``confirmed_by_operator``（evaluating→quoted）与
    ``gate_passed``（building→delivery_acceptance）前置随两端状态废除，
    不再校验。
    """
    if target in LEGACY_STATUSES:
        raise TransitionError(
            current,
            target,
            "废除态不可迁入（三态机：submitted → in_progress → delivered，"
            "另 rejected 终态）",
        )
    if current in LEGACY_STATUSES:
        raise TransitionError(
            current,
            target,
            "当前状态为废除态（存量归并迁移前的读窗口）——须先经 alembic 0019 "
            "归并为 in_progress 后再迁移",
        )
    if current in TERMINAL_STATUSES:
        raise TransitionError(
            current, target, f"{current.value} 是终态，不允许再迁移"
        )
    if target not in allowed_targets(current):
        raise TransitionError(
            current,
            target,
            "不在合法迁移表内，允许的目标："
            + ("、".join(t.value for t in sorted(allowed_targets(current))) or "无"),
        )
    if target is DemandStatus.rejected and not (reason or "").strip():
        raise TransitionError(
            current, target, "拒绝必须携带理由（reason），并留档客户可见"
        )
