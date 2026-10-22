// 控制台「工作台」页（#/console/workbench，wire-personal-workbench 任务 2.1/2.2）。
// - 单次拉取聚合端点 GET /api/v1/workbench（六 section 单往返），loading /
//   错误态照既有页范式（错误如实呈现 + 重试，绝不静默吞掉）。
// - 六段卡片：我的数据 / 我的源包 / 看板与产物 / 我的规则（告警）/ 我的
//   需求 + 壹座入口紧凑卡；计数与最近条目一律来自响应（不呈现虚构计数），
//   最近行深链到各既有页。
// - 免费档（free_quota != null）：数据集段内联配额条（与 DatasetsPage 的
//   QuotaBar 同视觉语言，本地小组件实现）——限额触达转 amber 内联升档提示，
//   绝不整页拦截；付费档（free_quota=null）不渲染配额条。
// - 空态（计数 0 且列表空）：上手 CTA——数据集「上传 CSV」→ #/console/datasets、
//   源包「浏览源包」→ #/console/packs、产物「去 AI 构建」→ #/console/chat。
// - 壹座入口卡与 ChatPage 引导卡同链接语义（主入口=官网 demo 页、次级=
//   platform.finddatatech.cloud）；卡片绝不复述测试账号邮箱或口令明文
//  （console-ai-entry 红线），也不发起任何网络请求。
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  ARTIFACT_KINDS,
  fetchWorkbench,
  type WorkbenchFreeQuota,
  type WorkbenchSummary,
} from "./api";
import { formatServerDate } from "./util";
import { packName } from "./PacksPage";
import { YIZUO_PLATFORM_URL } from "../components/footerConstants";
import { CONTACT_MAILTO } from "../views/HomeView";

/** by_kind 徽标键序：复用 api.ts 的 ARTIFACT_KINDS（六类，含表单构建器的
 * form——生产已有 form 产物，呈现口径必须与产物页一致，否则 active_total
 * 与徽标和不一致）。 */
const WORKBENCH_KINDS = ARTIFACT_KINDS;

/** i18n 键缺失时兜底原样展示（与 MyDemandsPage statusLabel 同范式）。 */
function labelOrRaw(translate: (key: string) => string, key: string): string {
  const label = translate(key);
  return label === key ? key.split(".").pop() ?? key : label;
}

/** 段卡片壳：标题 + 「查看全部」深链 + 内容；样式照仓库既有卡片语言。 */
function SectionCard({
  testid,
  title,
  openHref,
  openTestid,
  children,
}: {
  testid: string;
  title: string;
  openHref?: string;
  openTestid?: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section
      data-testid={testid}
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
        {openHref && (
          <a
            href={openHref}
            data-testid={openTestid}
            className="text-xs text-slate-500 underline-offset-2 hover:text-slate-900 hover:underline"
          >
            {t("console.workbench.openAll")} →
          </a>
        )}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

/** 段内空态（dashed 边框，照 DatasetsPage 空态视觉）+ 可选上手 CTA 链。 */
function SectionEmpty({
  testid,
  text,
  ctaTestid,
  ctaLabel,
  ctaHref,
}: {
  testid: string;
  text: string;
  ctaTestid?: string;
  ctaLabel?: string;
  ctaHref?: string;
}) {
  return (
    <div
      data-testid={testid}
      className="rounded-md border border-dashed border-slate-300 px-3 py-4 text-center"
    >
      <p className="text-xs text-slate-500">{text}</p>
      {ctaHref && ctaLabel && (
        <a
          href={ctaHref}
          data-testid={ctaTestid}
          className="mt-2 inline-block rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
        >
          {ctaLabel}
        </a>
      )}
    </div>
  );
}

/**
 * 免费档配额条（wire-personal-workbench 任务 2.1）：与 DatasetsPage 的
 * QuotaBar 同视觉语言——「已用 X/N 集、每集 ≤M 行」；触达上限转 amber 并附
 * 内联升档引导（mailto CTA）。只内联在数据集段内，绝不整页拦截。
 * 档位未知降级（wire-console-resilience D4）：max_* 任一为 null/undefined
 * （fail-closed 口径）→ 中性提示行——不渲染数值条/amber 态/升档 CTA，
 * 绝不抛错；有值路径逐字不变。
 */
function WorkbenchQuotaBar({ quota }: { quota: WorkbenchFreeQuota }) {
  const { t } = useTranslation();
  // max 未知（null）时比较无意义——按「永不触达」占位，走下方 unknown 降级。
  const exhausted =
    quota.max_datasets != null && quota.used_datasets >= quota.max_datasets;
  if (quota.max_datasets == null || quota.max_rows_per_dataset == null) {
    return (
      <div
        data-testid="workbench-quota"
        data-unknown="true"
        className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 px-3 py-2 text-xs text-slate-600"
      >
        {t("console.workbench.quota.unknown")}
      </div>
    );
  }
  return (
    <div
      data-testid="workbench-quota"
      data-exhausted={exhausted ? "true" : "false"}
      className={`mt-3 rounded-md border px-3 py-2 text-xs ${
        exhausted
          ? "border-amber-200 bg-amber-50/60 text-amber-700"
          : "border-slate-200 bg-slate-50/60 text-slate-600"
      }`}
    >
      {t("console.workbench.quota.bar", {
        used: quota.used_datasets,
        max: quota.max_datasets,
        rows: quota.max_rows_per_dataset.toLocaleString("en-US"),
      })}
      {exhausted && (
        <>
          <span className="mt-1 block">
            {t("console.workbench.quota.exhausted", { max: quota.max_datasets })}
          </span>
          <a
            data-testid="workbench-quota-upgrade"
            href={CONTACT_MAILTO}
            className="mt-1.5 inline-block rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
          >
            {t("console.workbench.quota.cta")}
          </a>
        </>
      )}
    </div>
  );
}

export function WorkbenchPage() {
  const { t, i18n } = useTranslation();
  const [summary, setSummary] = useState<WorkbenchSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSummary(await fetchWorkbench());
    } catch (err) {
      setSummary(null);
      setError(apiErrorText(err, t("console.workbench.errorTitle")));
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
        <h1 className="text-lg font-semibold">{t("console.workbench.title")}</h1>
        <p
          className="mt-6 text-xs text-slate-400"
          data-testid="workbench-loading"
        >
          {t("console.workbench.loading")}
        </p>
      </div>
    );
  }

  if (error || summary == null) {
    return (
      <div>
        <h1 className="text-lg font-semibold">{t("console.workbench.title")}</h1>
        <div
          data-testid="workbench-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">
            {t("console.workbench.errorTitle")}
          </p>
          {error && (
            <p className="mt-1 break-all font-mono text-[11px] text-red-500">
              {error}
            </p>
          )}
          <button
            type="button"
            data-testid="workbench-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.workbench.retry")}
          </button>
        </div>
      </div>
    );
  }

  const datasetsEmpty =
    summary.datasets.total === 0 && summary.datasets.recent.length === 0;
  const packsEmpty =
    summary.packs.subscribed === 0 && summary.packs.recent.length === 0;
  const artifactsEmpty =
    summary.artifacts.active_total === 0 &&
    summary.artifacts.recent.length === 0;
  const alertsEmpty =
    summary.alerts.alert_rules === 0 &&
    summary.alerts.firing === 0 &&
    summary.alerts.events_24h === 0;
  const demandsEmpty =
    summary.demands.open === 0 && summary.demands.latest == null;

  return (
    <div data-testid="workbench-root">
      <h1 className="text-lg font-semibold">{t("console.workbench.title")}</h1>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.workbench.subtitle")}
      </p>

      <div className="mt-4 grid items-start gap-4 md:grid-cols-2">
        {/* ── 我的数据：总数 + api/cell 分层徽标 + 最近列表 + 免费档配额条 ── */}
        <SectionCard
          testid="workbench-section-datasets"
          title={t("console.workbench.datasets.title")}
          openHref="#/console/datasets"
          openTestid="workbench-open-datasets"
        >
          {datasetsEmpty ? (
            <SectionEmpty
              testid="workbench-empty-datasets"
              text={t("console.workbench.datasets.empty")}
              ctaTestid="workbench-empty-datasets-cta"
              ctaLabel={t("console.workbench.datasets.emptyCta")}
              ctaHref="#/console/datasets"
            />
          ) : (
            <>
              <p className="text-xs text-slate-600">
                {t("console.workbench.datasets.totalLabel", {
                  count: summary.datasets.total,
                })}
              </p>
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <span
                  data-testid="workbench-tier-api"
                  className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
                >
                  {t("console.workbench.datasets.apiTier", {
                    count: summary.datasets.api_tier,
                  })}
                </span>
                <span
                  data-testid="workbench-tier-cell"
                  className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs text-indigo-700 ring-1 ring-indigo-200"
                >
                  {t("console.workbench.datasets.cellTier", {
                    count: summary.datasets.cell_tier,
                  })}
                </span>
              </div>
              {summary.datasets.recent.length > 0 && (
                <ul className="mt-3 space-y-1.5">
                  {summary.datasets.recent.map((dataset) => {
                    const tier =
                      dataset.storage_tier === "api" ||
                      dataset.storage_tier === "cell"
                        ? dataset.storage_tier
                        : null;
                    return (
                      <li key={dataset.id}>
                        <a
                          href="#/console/datasets"
                          data-testid="workbench-recent-dataset"
                          className="flex flex-wrap items-center gap-2 rounded-md border border-slate-100 px-2.5 py-1.5 text-xs text-slate-700 transition-colors hover:bg-slate-50"
                        >
                          <span className="font-medium">{dataset.name}</span>
                          {tier && (
                            <span
                              data-testid="workbench-recent-dataset-tier"
                              data-tier={tier}
                              className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${
                                tier === "cell"
                                  ? "bg-indigo-50 text-indigo-700 ring-indigo-200"
                                  : "bg-slate-100 text-slate-600 ring-slate-200"
                              }`}
                            >
                              {t(`console.datasets.tierBadge.${tier}`)}
                            </span>
                          )}
                          <span className="ml-auto text-slate-400">
                            {t("console.workbench.datasets.updatedLabel")}{" "}
                            {formatServerDate(dataset.updated_at)}
                          </span>
                        </a>
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
          {/* 免费档内联配额条：付费档（free_quota=null）不渲染；限额触达仅
              内联升档提示（amber），不拦截页面其余段落。 */}
          {summary.free_quota != null && (
            <WorkbenchQuotaBar quota={summary.free_quota} />
          )}
        </SectionCard>

        {/* ── 我的源包：订阅计数 + 最近订阅名称列表 ── */}
        <SectionCard
          testid="workbench-section-packs"
          title={t("console.workbench.packs.title")}
          openHref="#/console/packs"
          openTestid="workbench-open-packs"
        >
          {packsEmpty ? (
            <SectionEmpty
              testid="workbench-empty-packs"
              text={t("console.workbench.packs.empty")}
              ctaTestid="workbench-empty-packs-cta"
              ctaLabel={t("console.workbench.packs.emptyCta")}
              ctaHref="#/console/packs"
            />
          ) : (
            <>
              <p className="text-xs text-slate-600">
                {t("console.workbench.packs.subscribedLabel", {
                  count: summary.packs.subscribed,
                })}
              </p>
              {summary.packs.recent.length > 0 && (
                <ul className="mt-3 space-y-1.5">
                  {summary.packs.recent.map((pack) => (
                    <li key={pack.pack_id}>
                      <a
                        href="#/console/packs"
                        data-testid="workbench-recent-pack"
                        className="flex items-center gap-2 rounded-md border border-slate-100 px-2.5 py-1.5 text-xs text-slate-700 transition-colors hover:bg-slate-50"
                      >
                        <span className="font-medium">
                          {packName(pack, i18n.language)}
                        </span>
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </SectionCard>

        {/* ── 看板与产物：活跃总数 + 五类 by_kind 徽标 + 最近产物 ── */}
        <SectionCard
          testid="workbench-section-artifacts"
          title={t("console.workbench.artifacts.title")}
          openHref="#/console/artifacts"
          openTestid="workbench-open-artifacts"
        >
          {artifactsEmpty ? (
            <SectionEmpty
              testid="workbench-empty-artifacts"
              text={t("console.workbench.artifacts.empty")}
              ctaTestid="workbench-empty-artifacts-cta"
              ctaLabel={t("console.workbench.artifacts.emptyCta")}
              ctaHref="#/console/chat"
            />
          ) : (
            <>
              <p className="text-xs text-slate-600">
                {t("console.workbench.artifacts.activeLabel", {
                  count: summary.artifacts.active_total,
                })}
              </p>
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                {WORKBENCH_KINDS.map((kind) => (
                  <span
                    key={kind}
                    data-testid="workbench-kind-badge"
                    data-kind={kind}
                    className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
                  >
                    {labelOrRaw(t, `console.artifacts.kinds.${kind}`)}{" "}
                    {/* 新收录 kind 在旧版响应缺键时兜底 0（wire-record-view：
                        api/web 展示序两处一致追加，后端 by_kind 未上线过渡期）。 */}
                    {summary.artifacts.by_kind[kind] ?? 0}
                  </span>
                ))}
              </div>
              {summary.artifacts.recent.length > 0 && (
                <ul className="mt-3 space-y-1.5">
                  {summary.artifacts.recent.map((artifact) => (
                    <li key={artifact.id}>
                      <a
                        href="#/console/artifacts"
                        data-testid="workbench-recent-artifact"
                        className="flex flex-wrap items-center gap-2 rounded-md border border-slate-100 px-2.5 py-1.5 text-xs text-slate-700 transition-colors hover:bg-slate-50"
                      >
                        <span className="font-medium">{artifact.title}</span>
                        <span
                          data-testid="workbench-recent-artifact-status"
                          data-status={artifact.status}
                          className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${
                            artifact.status === "published"
                              ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                              : artifact.status === "archived"
                                ? "bg-slate-100 text-slate-500 ring-slate-200"
                                : "bg-amber-50 text-amber-700 ring-amber-200"
                          }`}
                        >
                          {labelOrRaw(
                            t,
                            `console.artifacts.statuses.${artifact.status}`,
                          )}
                        </span>
                        <span className="ml-auto text-slate-400">
                          {t("console.workbench.artifacts.updatedLabel")}{" "}
                          {formatServerDate(artifact.updated_at)}
                        </span>
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </SectionCard>

        {/* ── 我的规则（告警）：规则数 / 触发中 / 24h 事件 三个数字 ── */}
        <SectionCard
          testid="workbench-section-alerts"
          title={t("console.workbench.alerts.title")}
          openHref="#/console/artifacts"
          openTestid="workbench-open-alerts"
        >
          {alertsEmpty ? (
            <SectionEmpty
              testid="workbench-empty-alerts"
              text={t("console.workbench.alerts.empty")}
            />
          ) : (
            <div className="grid grid-cols-3 gap-2 text-center">
              <div
                data-testid="workbench-alert-rules"
                className="rounded-md bg-slate-50/60 px-2 py-2 text-[11px] text-slate-500"
              >
                {t("console.workbench.alerts.rulesLabel", {
                  count: summary.alerts.alert_rules,
                })}
              </div>
              <div
                data-testid="workbench-alert-firing"
                className={`rounded-md px-2 py-2 text-[11px] ${
                  summary.alerts.firing > 0
                    ? "bg-rose-50 text-rose-600"
                    : "bg-slate-50/60 text-slate-500"
                }`}
              >
                {t("console.workbench.alerts.firingLabel", {
                  count: summary.alerts.firing,
                })}
              </div>
              <div
                data-testid="workbench-alert-events"
                className="rounded-md bg-slate-50/60 px-2 py-2 text-[11px] text-slate-500"
              >
                {t("console.workbench.alerts.eventsLabel", {
                  count: summary.alerts.events_24h,
                })}
              </div>
            </div>
          )}
        </SectionCard>

        {/* ── 我的需求：进行中计数 + 最新一单（code/status）── */}
        <SectionCard
          testid="workbench-section-demands"
          title={t("console.workbench.demands.title")}
          openHref="#/console/demands"
          openTestid="workbench-open-demands"
        >
          {demandsEmpty ? (
            <SectionEmpty
              testid="workbench-empty-demands"
              text={t("console.workbench.demands.empty")}
            />
          ) : (
            <>
              <p className="text-xs text-slate-600">
                {t("console.workbench.demands.openLabel", {
                  count: summary.demands.open,
                })}
              </p>
              {summary.demands.latest != null && (
                <div
                  data-testid="workbench-demand-latest"
                  className="mt-3 flex flex-wrap items-center gap-2 rounded-md border border-slate-100 px-2.5 py-1.5 text-xs text-slate-700"
                >
                  <span className="text-slate-500">
                    {t("console.workbench.demands.latestLabel")}
                  </span>
                  <span className="font-mono">
                    {summary.demands.latest.code}
                  </span>
                  <span
                    data-testid="workbench-demand-latest-status"
                    data-status={summary.demands.latest.status}
                    className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600 ring-1 ring-slate-200"
                  >
                    {labelOrRaw(
                      t,
                      `console.demands.statuses.${summary.demands.latest.status}`,
                    )}
                  </span>
                  <span className="ml-auto text-slate-400">
                    {t("console.workbench.demands.updatedLabel")}{" "}
                    {formatServerDate(summary.demands.latest.updated_at)}
                  </span>
                </div>
              )}
            </>
          )}
        </SectionCard>

        {/* ── 壹座入口紧凑卡：与 ChatPage 引导卡同链接语义（主入口直落
            platform.finddatatech.cloud——壹座与柏讯共用同一 Logto IdP，
            已登录客户直达即登）；绝不复述测试账号邮箱/口令明文
            （console-ai-entry 红线），零网络请求。── */}
        <section
          data-testid="workbench-section-yizuo"
          className="rounded-lg border border-slate-200 bg-white p-4"
        >
          <h2 className="text-sm font-semibold text-slate-800">
            {t("console.workbench.yizuo.title")}
          </h2>
          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            {t("console.workbench.yizuo.body")}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <a
              data-testid="workbench-yizuo-demo-cta"
              href={YIZUO_PLATFORM_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white transition-colors hover:bg-slate-700"
            >
              {t("console.workbench.yizuo.tryCta")}
            </a>
          </div>
        </section>
      </div>
    </div>
  );
}
