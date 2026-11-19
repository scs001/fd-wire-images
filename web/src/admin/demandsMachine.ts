// 需求单三态状态机的客户端镜像（api 仓 fd_wire_api/demands/statemachine.py
// 的 ALLOWED_TRANSITIONS 白名单，wire-ux-consolidation 任务 3.5 自八态压缩）。
// 派发台下拉用它禁用非法迁移项——服务端仍是唯一裁决面（409 如实回显）。
// 八态工作流的 evaluating/quoted/awaiting_confirm/building/delivery_acceptance
// 五个旧态已废除：不进全集、不进白名单，前端任何文案不再出现。
import type { DemandStatus } from "./api";

export const ALL_DEMAND_STATUSES: DemandStatus[] = [
  "submitted",
  "in_progress",
  "delivered",
  "rejected",
];

export const ALLOWED_TRANSITIONS: Record<DemandStatus, DemandStatus[]> = {
  submitted: ["in_progress", "rejected"],
  in_progress: ["delivered", "rejected"],
  delivered: [],
  rejected: [],
};

/** 当前状态下允许迁往的目标（终态为空数组 → 下拉整体禁用）。 */
export function allowedTargets(current: DemandStatus): DemandStatus[] {
  return ALLOWED_TRANSITIONS[current] ?? [];
}

/** 迁移到 rejected 必带理由（状态机硬前置）。 */
export function needsReason(target: DemandStatus): boolean {
  return target === "rejected";
}
