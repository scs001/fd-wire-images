// 控制台「我的需求」页（#/console/demands，wire-ux-consolidation 任务 4.3
// 自占位转正）：本人提交的需求单只读列表。
// - 数据：GET /api/v1/demands（Bearer 登录身份；跨客户隔离由服务端按映射
//   客户保证，前端不加任何过滤参数）。
// - 呈现：状态徽章（已收到/处理中/已交付；rejected 显示「未承接」+ 理由
//   摘要）+ 提交时间 + 需求摘要；无任何迁移操作控件——派发是内部面的事。
// - 编排时间线（wire-demand-generation-orchestration 任务 4.2 客户面）：
//   demand.orchestration 有则渲染「生成进度」区块（阶段中文标签 + 时间 +
//   PR 外链），旧单缺省该字段则整个区块不渲染。
// - 交付就绪（wire-demand-visibility 任务 3.1 客户面）：delivered 单且
//   demand.delivery 有信号时渲染「数据交付」区块（scope 已就绪/准备中 +
//   公开授权态）；只读 delivery 的三个安全子字段（scope_name/scope_status/
//   publish_authorized），delivery_events 全文与错误串绝不进客户面；非
//   delivered 单与旧单（delivery 缺省）整个区块不渲染。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  fetchMyDemands,
  type CustomerDemand,
  type OrchestrationEntry,
} from "./api";

const STATUS_BADGE_CLASS: Record<string, string> = {
  submitted: "bg-slate-100 text-slate-700 ring-slate-200",
  in_progress: "bg-amber-50 text-amber-700 ring-amber-200",
  delivered: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  rejected: "bg-rose-50 text-rose-700 ring-rose-200",
};

/** 编排时间线阶段圆点配色（blocked 红、merged 绿、pr_open 琥珀、入口灰蓝）。 */
const STAGE_DOT_CLASS: Record<OrchestrationEntry["stage"], string> = {
  workorder_submitted: "bg-slate-400",
  pr_open: "bg-amber-500",
  merged: "bg-emerald-500",
  blocked: "bg-rose-500",
};

/** 状态徽章文案键（旧八态名不再出现；未知状态兜底原样展示）。 */
function statusLabel(
  translate: (key: string) => string,
  status: string,
): string {
  const key = `console.demands.statuses.${status}`;
  const label = translate(key);
  return label === key ? status : label;
}

/** 编排阶段文案键（冻结四阶段；未知阶段兜底原样展示）。 */
function orchestrationStageLabel(
  translate: (key: string) => string,
  stage: string,
): string {
  const key = `console.demands.orchestration.stages.${stage}`;
  const label = translate(key);
  return label === key ? stage : label;
}

function formatSubmittedAt(iso: string, language: string): string {
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

/** 交付就绪区块（wire-demand-visibility 任务 3.1 客户面）：delivered 单的
 *  scope 交付信号——已就绪（绿）/准备中（琥珀）；公开授权态作次行。
 *  只读 delivery 的三个安全子字段（scope_name/scope_status/
 *  publish_authorized），delivery_events 全文与失败错误串绝不出现；
 *  scope_status 非 ok/pending（null/缺省/未知）不渲染任何壳——不虚构。 */
function DeliveryBlock({
  delivery,
}: {
  delivery: NonNullable<CustomerDemand["delivery"]>;
}) {
  const { t } = useTranslation();
  const status = delivery.scope_status;
  if (status !== "ok" && status !== "pending") return null;
  const ready = status === "ok";
  return (
    <div
      data-testid="demand-delivery"
      data-status={status}
      className={`mt-3 rounded-md border px-3 py-2 ${
        ready
          ? "border-emerald-100 bg-emerald-50/70"
          : "border-amber-100 bg-amber-50/70"
      }`}
    >
      <p className="text-xs font-medium text-slate-600">
        {t("console.demands.delivery.title")}
      </p>
      <p
        className={`mt-1 text-xs font-medium ${
          ready ? "text-emerald-700" : "text-amber-700"
        }`}
      >
        {t(
          ready
            ? "console.demands.delivery.ready"
            : "console.demands.delivery.pending",
        )}
      </p>
      {ready && delivery.scope_name && (
        <p
          data-testid="demand-delivery-scope-name"
          className="mt-1 font-mono text-[11px] text-slate-500"
        >
          {delivery.scope_name}
        </p>
      )}
      {ready && delivery.publish_authorized === true && (
        <p className="mt-0.5 text-xs text-emerald-600">
          {t("console.demands.delivery.publishAuthorized")}
        </p>
      )}
      {ready && delivery.publish_authorized === false && (
        <p className="mt-0.5 text-xs text-amber-700">
          {t("console.demands.delivery.publishRevoked")}
        </p>
      )}
      {!ready && (
        <p className="mt-0.5 text-xs text-amber-600">
          {t("console.demands.delivery.pendingHint")}
        </p>
      )}
    </div>
  );
}

export function MyDemandsPage() {
  const { t, i18n } = useTranslation();
  const [demands, setDemands] = useState<CustomerDemand[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDemands(await fetchMyDemands());
    } catch (err) {
      setDemands(null);
      setError(apiErrorText(err, t("console.demands.errorTitle")));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div>
        <h1 className="text-lg font-semibold">{t("console.demands.title")}</h1>
        <p className="mt-6 text-xs text-slate-400" data-testid="my-demands-loading">
          {t("console.demands.loading")}
        </p>
      </div>
    );
  }

  if (error || demands == null) {
    return (
      <div>
        <h1 className="text-lg font-semibold">{t("console.demands.title")}</h1>
        <div
          data-testid="my-demands-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">{t("console.demands.errorTitle")}</p>
          {error && (
            <p className="mt-1 break-all font-mono text-[11px] text-red-500">{error}</p>
          )}
          <button
            type="button"
            data-testid="my-demands-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.demands.retry")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div data-testid="my-demands-page">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("console.demands.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("console.demands.subtitle")}
          </p>
        </div>
        <a
          href="#/console/demands/new"
          data-testid="my-demands-new"
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 transition-colors"
        >
          {t("console.demands.newCta")}
        </a>
      </div>

      {demands.length === 0 ? (
        <div
          data-testid="my-demands-empty"
          className="mt-6 rounded-lg border border-slate-200 bg-white p-8 text-center"
        >
          <p className="text-sm font-medium text-slate-700">
            {t("console.demands.empty")}
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {t("console.demands.emptyHint")}
          </p>
        </div>
      ) : (
        <ul data-testid="my-demand-list" className="mt-4 space-y-2">
          {demands.map((demand) => (
            <li
              key={demand.id}
              data-testid="my-demand-item"
              className="rounded-lg border border-slate-200 bg-white p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-slate-400">{demand.code}</span>
                <span
                  data-testid="my-demand-status"
                  data-status={demand.status}
                  className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
                    STATUS_BADGE_CLASS[demand.status] ?? STATUS_BADGE_CLASS.submitted
                  }`}
                >
                  {statusLabel(t, demand.status)}
                </span>
                <span className="ml-auto text-xs text-slate-400" data-testid="my-demand-time">
                  {t("console.demands.submittedAt", {
                    time: formatSubmittedAt(demand.created_at, i18n.language),
                  })}
                </span>
              </div>
              <p className="mt-2 text-sm text-slate-800" data-testid="my-demand-summary">
                {demand.title}
              </p>
              {demand.status === "rejected" && demand.rejection_reason && (
                <p
                  data-testid="my-demand-rejection-reason"
                  className="mt-1 text-xs text-rose-600"
                >
                  {t("console.demands.rejectedReason", {
                    reason: demand.rejection_reason,
                  })}
                </p>
              )}
              {demand.orchestration && demand.orchestration.length > 0 && (
                <div
                  data-testid="demand-orchestration"
                  className="mt-3 rounded-md border border-slate-100 bg-slate-50/70 px-3 py-2"
                >
                  <p className="text-xs font-medium text-slate-600">
                    {t("console.demands.orchestration.title")}
                  </p>
                  <ol className="mt-1.5 space-y-1">
                    {demand.orchestration.map((entry, index) => (
                      <li
                        key={index}
                        data-testid="demand-orchestration-entry"
                        data-stage={entry.stage}
                        className="flex flex-wrap items-center gap-2 text-xs text-slate-700"
                      >
                        <span
                          aria-hidden
                          className={`h-1.5 w-1.5 rounded-full ${
                            STAGE_DOT_CLASS[entry.stage] ?? "bg-slate-300"
                          }`}
                        />
                        <span data-testid="demand-orchestration-stage">
                          {orchestrationStageLabel(t, entry.stage)}
                        </span>
                        <span className="text-slate-400">
                          {formatSubmittedAt(entry.at, i18n.language)}
                        </span>
                        {entry.pr_url && (
                          <a
                            href={entry.pr_url}
                            target="_blank"
                            rel="noreferrer"
                            data-testid="demand-orchestration-pr"
                            className="text-sky-600 underline hover:text-sky-800"
                          >
                            {t("console.demands.orchestration.prLink")}
                          </a>
                        )}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
              {demand.status === "delivered" && demand.delivery && (
                <DeliveryBlock delivery={demand.delivery} />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
