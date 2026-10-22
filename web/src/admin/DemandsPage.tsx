// 需求单派发台（#/admin/demands）：全客户列表 + 状态筛选 + 派发操作。
// 迁移下拉按状态机白名单禁用非法项（客户端只是提示面，服务端 409 仍是
// 唯一裁决）；三态机（wire-ux-consolidation 任务 3.5）：submitted →
// in_progress → delivered，rejected 必带理由；报价确认流程已随八态废除。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AdminErrorBox, AdminStates } from "./AdminLayout";
import {
  dispatchDemandWanxing,
  exportDemandWorkorder,
  fetchDemands,
  retryDemandScope,
  transitionDemand,
  type AdminDemand,
  type AdminWanxingResult,
  type AdminWorkorderResult,
  type DemandStatus,
} from "./api";
import { ALL_DEMAND_STATUSES, allowedTargets, needsReason } from "./demandsMachine";

const STATUS_BADGE_CLASS: Record<string, string> = {
  submitted: "bg-slate-100 text-slate-700 ring-slate-200",
  in_progress: "bg-amber-50 text-amber-700 ring-amber-200",
  delivered: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  rejected: "bg-rose-50 text-rose-700 ring-rose-200",
};

/** 进度面阶段全集（wire-demand-visibility 任务 2.2 冻结七类；
 * wire-demand-auto-export 任务 2.1 增第八类 auto_export_failed）。
 * gate_result 由 gate 摘要覆盖不重复渲染；未知类型静默跳过，绝不猜状态。 */
const PROGRESS_STAGES = [
  "workorder_exported",
  "auto_export_failed",
  "pipeline_run",
  "workorder_submitted",
  "pr_open",
  "merged",
  "blocked",
  "workorder_lost",
] as const;
type ProgressStage = (typeof PROGRESS_STAGES)[number];

/** 阶段圆点配色：受阻/丢失/自动出单被拒红、待审琥珀、点亮绿、其余中性灰。 */
const PROGRESS_DOT_CLASS: Record<ProgressStage, string> = {
  workorder_exported: "bg-slate-400",
  auto_export_failed: "bg-rose-500",
  pipeline_run: "bg-slate-400",
  workorder_submitted: "bg-slate-400",
  pr_open: "bg-amber-500",
  merged: "bg-emerald-500",
  blocked: "bg-rose-500",
  workorder_lost: "bg-rose-500",
};

function isProgressStage(value: unknown): value is ProgressStage {
  return (
    typeof value === "string" &&
    (PROGRESS_STAGES as readonly string[]).includes(value)
  );
}

/** 事件时间戳格式化（与「我的需求」页同款口径；坏值原样回显）。 */
function formatEventAt(iso: unknown, language: string): string | null {
  if (typeof iso !== "string" || !iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const locale = language === "zh" ? "zh-CN" : "en-US";
  try {
    return date.toLocaleString(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function DemandsPage() {
  const { t } = useTranslation();
  const [demands, setDemands] = useState<AdminDemand[] | null>(null);
  const [statusFilter, setStatusFilter] = useState<DemandStatus | "all">("all");
  const [error, setError] = useState<{ status?: number; message: string; detail?: string } | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [progressId, setProgressId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setDemands(null);
    try {
      const items = await fetchDemands(
        statusFilter === "all" ? undefined : statusFilter,
      );
      setDemands(items);
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("admin.demands.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("admin.demands.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="demand-status-filter" className="text-xs text-slate-500">
            {t("admin.demands.statusFilter")}
          </label>
          <select
            id="demand-status-filter"
            data-testid="demand-status-filter"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as DemandStatus | "all")}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="all">{t("admin.demands.statusAll")}</option>
            {ALL_DEMAND_STATUSES.map((status) => (
              <option key={status} value={status}>
                {t(`admin.demands.statuses.${status}`)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && <AdminErrorBox error={error} onRetry={() => void load()} />}
      {actionError && (
        <div
          data-testid="admin-dispatch-conflict"
          className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-700"
        >
          {actionError}
        </div>
      )}

      <AdminStates
        loading={demands == null && !error}
        empty={(demands?.length ?? 0) === 0 && !error}
        emptyTestId="admin-demands-empty"
      >
        {demands != null && demands.length > 0 && (
          <ul data-testid="admin-demand-list" className="mt-4 space-y-2">
          {demands!.map((demand) => (
            <li
              key={demand.id}
              data-testid="admin-demand-item"
              className="rounded-lg border border-slate-200 bg-white p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-slate-400">{demand.code}</span>
                <span className="text-sm font-medium text-slate-800">
                  {demand.title}
                </span>
                <span
                  data-testid="admin-demand-status"
                  data-status={demand.status}
                  className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
                    STATUS_BADGE_CLASS[demand.status] ?? STATUS_BADGE_CLASS.submitted
                  }`}
                >
                  {t(`admin.demands.statuses.${demand.status}`, demand.status)}
                </span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500">
                  {demand.customer_id}
                </span>
                <button
                  type="button"
                  data-testid="admin-demand-progress-toggle"
                  aria-expanded={progressId === demand.id}
                  onClick={() =>
                    setProgressId(progressId === demand.id ? null : demand.id)
                  }
                  className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                >
                  {progressId === demand.id
                    ? t("admin.demands.progress.collapse")
                    : t("admin.demands.progress.expand")}
                </button>
                <button
                  type="button"
                  data-testid="admin-demand-dispatch-toggle"
                  onClick={() => {
                    setOpenId(openId === demand.id ? null : demand.id);
                    setActionError(null);
                  }}
                  disabled={allowedTargets(demand.status).length === 0}
                  className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                  title={
                    allowedTargets(demand.status).length === 0
                      ? t("admin.demands.terminalHint")
                      : undefined
                  }
                >
                  {t("admin.demands.dispatch")}
                </button>
              </div>
              {demand.evaluation_summary && (
                <p
                  data-testid="admin-demand-evaluation"
                  className="mt-2 text-xs text-slate-500"
                >
                  {demand.evaluation_summary}
                </p>
              )}
              {demand.status_history.length > 0 && (
                <p
                  data-testid="admin-demand-last-event"
                  className="mt-1 text-[11px] text-slate-400"
                >
                  {t("admin.demands.lastEvent", {
                    status: t(
                      `admin.demands.statuses.${demand.status_history[demand.status_history.length - 1]!.status}`,
                    ),
                    by: demand.status_history[demand.status_history.length - 1]!.by,
                  })}
                </p>
              )}
              {openId === demand.id && (
                <>
                  <DispatchPanel
                    demand={demand}
                    onDone={async () => {
                      setOpenId(null);
                      await load();
                    }}
                    onConflict={(message) => setActionError(message)}
                  />
                  {demand.status === "in_progress" && (
                    <WorkorderSection
                      demandId={demand.id}
                      onConflict={(message) => setActionError(message)}
                      wanxingFact={latestWanxingFact(demand.pipeline_events)}
                    />
                  )}
                </>
              )}
              {progressId === demand.id && <ProgressSection demand={demand} />}
            </li>
          ))}
          </ul>
        )}
      </AdminStates>
    </div>
  );
}

function DispatchPanel({
  demand,
  onDone,
  onConflict,
}: {
  demand: AdminDemand;
  onDone: () => Promise<void>;
  onConflict: (message: string) => void;
}) {
  const { t } = useTranslation();
  const allowed = allowedTargets(demand.status);
  const [target, setTarget] = useState<DemandStatus>(allowed[0] ?? demand.status);
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await transitionDemand(demand.id, {
        target_status: target,
        by: "admin-console",
        ...(note.trim() ? { note: note.trim() } : {}),
        ...(needsReason(target) && reason.trim() ? { reason: reason.trim() } : {}),
      });
      setNote("");
      setReason("");
      await onDone();
    } catch (err) {
      const apiErr = err as { status?: number; detail?: string; message: string };
      onConflict(
        apiErr.detail ??
          (apiErr.status === 409
            ? t("admin.demands.conflictDefault")
            : apiErr.message),
      );
    } finally {
      setBusy(false);
    }
  };

  const reasonRequired = needsReason(target);

  return (
    <div
      data-testid="admin-dispatch-panel"
      className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
    >
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor={`target-${demand.id}`} className="block text-xs text-slate-500">
            {t("admin.demands.targetLabel")}
          </label>
          <select
            id={`target-${demand.id}`}
            data-testid="admin-dispatch-target"
            value={target}
            onChange={(e) => setTarget(e.target.value as DemandStatus)}
            className="mt-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            {ALL_DEMAND_STATUSES.map((status) => {
              const legal = allowed.includes(status);
              return (
                <option key={status} value={status} disabled={!legal}>
                  {t(`admin.demands.statuses.${status}`)}
                  {legal ? "" : `（${t("admin.demands.illegalSuffix")}）`}
                </option>
              );
            })}
          </select>
        </div>
        <div>
          <label htmlFor={`note-${demand.id}`} className="block text-xs text-slate-500">
            {t("admin.demands.noteLabel")}
          </label>
          <input
            id={`note-${demand.id}`}
            data-testid="admin-dispatch-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className="mt-1 w-56 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            placeholder={t("admin.demands.notePlaceholder")}
          />
        </div>
        {reasonRequired && (
          <div>
            <label htmlFor={`reason-${demand.id}`} className="block text-xs text-slate-500">
              {t("admin.demands.reasonLabel")}
            </label>
            <input
              id={`reason-${demand.id}`}
              data-testid="admin-dispatch-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="mt-1 w-56 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              placeholder={t("admin.demands.reasonPlaceholder")}
            />
          </div>
        )}
        <button
          type="button"
          data-testid="admin-dispatch-submit"
          disabled={busy || (reasonRequired && !reason.trim()) || allowed.length === 0}
          onClick={() => void submit()}
          className="rounded-md bg-slate-900 px-4 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {busy ? t("admin.common.working") : t("admin.demands.submit")}
        </button>
      </div>
      <p className="mt-2 text-[11px] text-slate-400">
        {t("admin.demands.machineNote")}
      </p>
    </div>
  );
}

/** 编排进度面（wire-demand-visibility 任务 2.2）：每单可展开的全事件时间线。

 * 数据源 = 列表端点已返回的 pipeline_events/gate/scope_status（不新增聚合
 * 端点）；事件按落库顺序映射人类阶段（含时间、ticket_path、PR 链接），
 * 未发生即不渲染；未出单如实占位「尚未出单」，绝不虚构进度（spec 红线）。
 * 自动出单（wire-demand-auto-export 任务 2.1）：trigger=auto 的
 * workorder_exported 带自动徽标；auto_export_failed 渲染失败原因，且
 * 有任一尝试留档（含失败）即不再显示「尚未出单」占位。
 * gate_result 不重复渲染（gate 摘要覆盖）；未知事件类型静默跳过不崩溃。
 * scope 重试（wire-scope-delivery-live 任务 2.1）：delivered+pending 单在
 * scope 状态行旁呈现「重试开 scope」——结果如实（成功呈现 scope 名，
 * 失败呈现最新 scope_create_failed 文案），非 pending 态不渲染按钮。
 */
function ProgressSection({ demand }: { demand: AdminDemand }) {
  const { t, i18n } = useTranslation();
  const [scopeRetryBusy, setScopeRetryBusy] = useState(false);
  // 最新一次重试的响应（比列表快照新——成功后 scope 行即时反映，不必整页刷新）
  const [scopeRetryResult, setScopeRetryResult] = useState<AdminDemand | null>(
    null,
  );
  const [scopeRetryError, setScopeRetryError] = useState<string | null>(null);

  const retryScope = async () => {
    setScopeRetryBusy(true);
    setScopeRetryError(null);
    try {
      const updated = await retryDemandScope(demand.id);
      setScopeRetryResult(updated);
      if (updated.scope_status !== "ok") {
        // 200 但 scope 仍未建立（诚实降级）：取最新 scope_create_failed 的 error
        const failed = [...updated.delivery_events]
          .reverse()
          .find((event) => event.type === "scope_create_failed");
        setScopeRetryError(
          typeof failed?.error === "string"
            ? failed.error
            : t("admin.demands.scopeRetry.errorDefault"),
        );
      }
    } catch (err) {
      const apiErr = err as { detail?: string; message: string };
      setScopeRetryError(
        apiErr.detail ?? t("admin.demands.scopeRetry.errorDefault"),
      );
    } finally {
      setScopeRetryBusy(false);
    }
  };

  // 阶段事件按序过滤；其余（gate_result 等）不进时间线。
  const entries = demand.pipeline_events.flatMap((event) => {
    const type = event.type;
    if (!isProgressStage(type)) return [];
    return [{ type, event }];
  });

  // 「尚未出单」占位语义（wire-demand-auto-export 任务 2.1）：只在无任何
  // 出单尝试留档时显示——有 auto_export_failed 即视为已有尝试，不占位。
  const hasExportAttempt =
    entries.length > 0 ||
    demand.pipeline_events.some((event) => event.type === "auto_export_failed");

  const scopeStatus = scopeRetryResult?.scope_status ?? demand.scope_status;
  const scopeName = scopeRetryResult?.scope_name ?? demand.scope_name;
  const scopeKey =
    scopeStatus === "ok"
      ? "ready"
      : scopeStatus === "pending"
        ? "pending"
        : "none";

  return (
    <div
      data-testid="admin-demand-progress"
      className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
    >
      <h3 className="text-sm font-medium text-slate-800">
        {t("admin.demands.progress.title")}
      </h3>
      {!hasExportAttempt ? (
        <p
          data-testid="admin-demand-progress-empty"
          className="mt-2 text-xs text-slate-400"
        >
          {t("admin.demands.progress.empty")}
        </p>
      ) : (
        <ol className="mt-2 space-y-1.5">
          {entries.map(({ type, event }, index) => {
            const at = formatEventAt(event.at, i18n.language);
            const ticketPath =
              typeof event.ticket_path === "string" ? event.ticket_path : null;
            const pipelineRunId =
              typeof event.pipeline_run_id === "string"
                ? event.pipeline_run_id
                : null;
            const prUrl = typeof event.pr_url === "string" ? event.pr_url : null;
            // 自动出单标注：仅 workorder_exported 带 trigger=auto（人工不带此键）。
            const autoTrigger =
              type === "workorder_exported" && event.trigger === "auto";
            // 自动出单被拒：如实呈现服务端拒绝文案（非字符串不猜）。
            const failReason =
              type === "auto_export_failed" && typeof event.reason === "string"
                ? event.reason
                : null;
            return (
              <li
                key={index}
                data-testid="admin-demand-progress-entry"
                data-stage={type}
                className="flex flex-wrap items-center gap-2 text-xs text-slate-700"
              >
                <span
                  aria-hidden
                  className={`h-1.5 w-1.5 rounded-full ${PROGRESS_DOT_CLASS[type]}`}
                />
                <span className="font-medium">
                  {t(`admin.demands.progress.stages.${type}`)}
                </span>
                {autoTrigger && (
                  <span
                    data-testid="admin-demand-progress-auto"
                    className="rounded bg-sky-50 px-1.5 py-0.5 text-[11px] text-sky-700 ring-1 ring-sky-200"
                  >
                    {t("admin.demands.progress.autoTrigger")}
                  </span>
                )}
                {at && <span className="text-slate-400">{at}</span>}
                {ticketPath && (
                  <span className="font-mono text-[11px] text-slate-400">
                    {ticketPath}
                  </span>
                )}
                {pipelineRunId && (
                  <span className="font-mono text-[11px] text-slate-400">
                    {pipelineRunId}
                  </span>
                )}
                {prUrl && (
                  <a
                    href={prUrl}
                    target="_blank"
                    rel="noreferrer"
                    data-testid="admin-demand-progress-pr"
                    className="text-sky-600 underline hover:text-sky-800"
                  >
                    {t("admin.demands.progress.prLink")}
                  </a>
                )}
                {failReason && (
                  <span
                    data-testid="admin-demand-progress-reason"
                    className="text-slate-400"
                  >
                    {t("admin.demands.progress.reasonLabel")}
                    {failReason}
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      )}
      {demand.gate && (
        <p
          data-testid="admin-demand-gate"
          data-passed={demand.gate.passed ? "true" : "false"}
          className={`mt-2 text-xs ${
            demand.gate.passed ? "text-emerald-700" : "text-slate-500"
          }`}
        >
          {t(
            demand.gate.passed
              ? "admin.demands.progress.gatePassed"
              : "admin.demands.progress.gateNotPassed",
          )}
          {" · "}
          {t("admin.demands.progress.gateCounts", {
            attempts: demand.gate.attempts,
            runs: demand.gate.pipeline_runs,
          })}
        </p>
      )}
      <p
        data-testid="admin-demand-scope"
        data-scope-status={scopeStatus ?? "none"}
        className={`mt-1 text-xs ${
          scopeKey === "ready"
            ? "text-emerald-700"
            : scopeKey === "pending"
              ? "text-amber-700"
              : "text-slate-400"
        }`}
      >
        {t(`admin.demands.progress.scope.${scopeKey}`)}
        {scopeKey === "ready" && scopeName && (
          <span
            data-testid="admin-demand-scope-name"
            className="ml-1 font-mono text-[11px] text-slate-500"
          >
            {scopeName}
          </span>
        )}
      </p>
      {demand.status === "delivered" && scopeStatus === "pending" && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="admin-demand-scope-retry"
            disabled={scopeRetryBusy}
            onClick={() => void retryScope()}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {scopeRetryBusy
              ? t("admin.common.working")
              : t("admin.demands.scopeRetry.submit")}
          </button>
          {scopeRetryError && (
            <span
              data-testid="admin-demand-scope-retry-error"
              className="text-[11px] text-rose-600"
            >
              {scopeRetryError}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** 「转生成工单」区（wire-workorder-console-action）：仅 in_progress 单渲染。
 *
 * 出单入舰队拉取队列，出单不迁移状态；成功必须展示工单全文（人工推送
 * 降级）与「未投递」诚实文案——绝不呈现自动化投递成功假象（spec 红线）。
 * 萬星直连（wire-wanxing-direct-dispatch 任务 3）：同区第二通道按钮——
 * 出单即时直投萬星 A2A（同步回执，不入拉取队列）；成功呈现回执面，502
 * 投递失败时工单全文随响应呈现（人工推送/改走拉取通道降级），闸拒绝
 * （409/422/503 字符串 detail）照实回显，绝不虚构投递成功。
 */
/** 直连留痕（wire-wanxing-direct-dispatch）：时间线只认冻结的舰队阶段集，
 * 直连事件被静默跳过——通道事实在此独立呈现，刷新/重进后仍可见。 */
interface WanxingFact {
  type: "wanxing_dispatched" | "wanxing_dispatch_failed";
  replyExcerpt: string | null;
  error: string | null;
}

function latestWanxingFact(
  events: Array<Record<string, unknown>>,
): WanxingFact | null {
  let fact: WanxingFact | null = null;
  for (const event of events) {
    if (event.type === "wanxing_dispatched") {
      const reply = event.reply_excerpt;
      fact = {
        type: "wanxing_dispatched",
        replyExcerpt: typeof reply === "string" ? reply : null,
        error: null,
      };
    } else if (event.type === "wanxing_dispatch_failed") {
      const error = event.error;
      fact = {
        type: "wanxing_dispatch_failed",
        replyExcerpt: null,
        error: typeof error === "string" ? error : null,
      };
    }
  }
  return fact;
}

function WorkorderSection({
  demandId,
  onConflict,
  wanxingFact,
}: {
  demandId: number;
  onConflict: (message: string) => void;
  wanxingFact: WanxingFact | null;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AdminWorkorderResult | null>(null);
  const [wanxingBusy, setWanxingBusy] = useState(false);
  const [wanxingResult, setWanxingResult] = useState<AdminWanxingResult | null>(
    null,
  );
  const [wanxingError, setWanxingError] = useState<{
    message: string;
    ticketYaml: string | null;
  } | null>(null);

  const submit = async () => {
    setBusy(true);
    try {
      setResult(await exportDemandWorkorder(demandId));
    } catch (err) {
      const apiErr = err as { status?: number; detail?: string; message: string };
      onConflict(apiErr.detail ?? t("admin.demands.workorder.errorDefault"));
    } finally {
      setBusy(false);
    }
  };

  const dispatchWanxing = async () => {
    setWanxingBusy(true);
    setWanxingResult(null);
    setWanxingError(null);
    try {
      setWanxingResult(await dispatchDemandWanxing(demandId));
    } catch (err) {
      const apiErr = err as {
        status?: number;
        detail?: string;
        detailPayload?: Record<string, unknown>;
        message: string;
      };
      // 502 投递失败：detail 是对象（message + 工单全文随响应——人工降级红线）
      const payload = apiErr.detailPayload;
      const payloadMessage =
        payload && typeof payload.message === "string" ? payload.message : null;
      const payloadYaml =
        payload && typeof payload.ticket_yaml === "string"
          ? payload.ticket_yaml
          : null;
      setWanxingError({
        message:
          payloadMessage ??
          apiErr.detail ??
          t("admin.demands.wanxing.errorDefault"),
        ticketYaml: payloadYaml,
      });
    } finally {
      setWanxingBusy(false);
    }
  };

  return (
    <div
      data-testid="admin-workorder-section"
      className="mt-3 rounded-md border border-slate-200 bg-white p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium text-slate-800">
            {t("admin.demands.workorder.title")}
          </h3>
          <p className="mt-1 max-w-xl text-[11px] text-slate-500">
            {t("admin.demands.workorder.hint")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="admin-workorder-submit"
            disabled={busy}
            onClick={() => void submit()}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? t("admin.common.working") : t("admin.demands.workorder.submit")}
          </button>
          <button
            type="button"
            data-testid="admin-demand-wanxing-dispatch"
            disabled={wanxingBusy}
            onClick={() => void dispatchWanxing()}
            title={t("admin.demands.wanxing.hint")}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {wanxingBusy
              ? t("admin.common.working")
              : t("admin.demands.wanxing.submit")}
          </button>
        </div>
      </div>

      {wanxingFact && (
        <p
          data-testid={
            wanxingFact.type === "wanxing_dispatched"
              ? "admin-wanxing-fact"
              : "admin-wanxing-fact-failed"
          }
          className={
            wanxingFact.type === "wanxing_dispatched"
              ? "mt-2 text-[11px] text-emerald-700"
              : "mt-2 text-[11px] text-rose-600"
          }
        >
          {wanxingFact.type === "wanxing_dispatched"
            ? t("admin.demands.wanxing.factDelivered", {
                reply: wanxingFact.replyExcerpt ?? "",
              })
            : t("admin.demands.wanxing.factFailed", {
                error: wanxingFact.error ?? "",
              })}
        </p>
      )}

      {result && (
        <div
          data-testid="admin-workorder-result"
          className="mt-3 rounded border border-slate-200 bg-slate-50/60 px-3 py-2.5 text-xs text-slate-600"
        >
          <p>
            <span className="text-slate-500">
              {t("admin.demands.workorder.pathLabel")}：
            </span>
            <span data-testid="admin-workorder-path" className="font-mono">
              {result.ticket_path}
            </span>
          </p>
          <p className="mt-1" data-testid="admin-workorder-slug">
            {result.slug_check === "free"
              ? t("admin.demands.workorder.slugFree")
              : t("admin.demands.workorder.slugUnverified")}
          </p>
          <p
            data-testid="admin-workorder-undelivered"
            className="mt-1 text-amber-700"
          >
            {t("admin.demands.workorder.undelivered")}
          </p>
          <details className="mt-2">
            <summary className="cursor-pointer text-slate-500">
              {t("admin.demands.workorder.yamlSummary")}
            </summary>
            <pre
              data-testid="admin-workorder-yaml"
              className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded bg-white p-2 font-mono text-[11px] text-slate-700"
            >
              {result.ticket_yaml}
            </pre>
          </details>
        </div>
      )}

      {wanxingResult && (
        <div
          data-testid="admin-wanxing-result"
          className="mt-3 rounded border border-emerald-200 bg-emerald-50/60 px-3 py-2.5 text-xs text-slate-600"
        >
          <p
            data-testid="admin-wanxing-delivered"
            className="text-emerald-700"
          >
            {t("admin.demands.wanxing.delivered")}
          </p>
          <p className="mt-1">
            <span className="text-slate-500">
              {t("admin.demands.wanxing.replyLabel")}：
            </span>
            <span
              data-testid="admin-wanxing-reply"
              className="font-mono text-[11px]"
            >
              {wanxingResult.a2a.reply_excerpt}
            </span>
          </p>
          <p className="mt-1">
            <span className="text-slate-500">
              {t("admin.demands.wanxing.messageIdLabel")}：
            </span>
            <span data-testid="admin-wanxing-message-id" className="font-mono">
              {wanxingResult.a2a.message_id}
            </span>
          </p>
          <p className="mt-1">
            <span className="text-slate-500">
              {t("admin.demands.wanxing.idempotencyLabel")}：
            </span>
            <span
              data-testid="admin-wanxing-idempotency-key"
              className="font-mono"
            >
              {wanxingResult.a2a.idempotency_key}
            </span>
          </p>
          <p className="mt-1">
            <span className="text-slate-500">
              {t("admin.demands.wanxing.slugLabel")}：
            </span>
            <span data-testid="admin-wanxing-slug" className="font-mono">
              {wanxingResult.slug}
            </span>
          </p>
        </div>
      )}

      {wanxingError && (
        <div
          data-testid="admin-wanxing-error"
          className="mt-3 rounded border border-rose-200 bg-rose-50/60 px-3 py-2.5 text-xs text-rose-700"
        >
          <p data-testid="admin-wanxing-error-message">{wanxingError.message}</p>
          {wanxingError.ticketYaml && (
            <details className="mt-2">
              <summary className="cursor-pointer text-slate-500">
                {t("admin.demands.wanxing.yamlSummary")}
              </summary>
              <pre
                data-testid="admin-wanxing-error-yaml"
                className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded bg-white p-2 font-mono text-[11px] text-slate-700"
              >
                {wanxingError.ticketYaml}
              </pre>
            </details>
          )}
          <p className="mt-1 text-slate-500">
            {t("admin.demands.wanxing.fallback")}
          </p>
        </div>
      )}
    </div>
  );
}
