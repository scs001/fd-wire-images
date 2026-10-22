// 控制台「用量」页（#/console/usage，wire-self-serve-launch 任务 6.2）。
// - 数据源：GET /api/console/usage（登录客户自己且仅自己的当月聚合，
//   rows_returned 口径，与行数门/结算同源）——「上线中」占位退位。
// - 余量低（<20%）warning；耗尽态明示「额度已用完，次月恢复」并给升档
//   引导（耗尽语义与「余额不足」区分文案，portal-pricing/usage-transparency spec）。
// - 数字一律来自端点渲染（千分位为呈现格式化），源码不携带额度面量。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiErrorText, fetchConsoleUsage, type ConsoleUsage } from "./api";
import { CONTACT_MAILTO } from "../views/HomeView";

/** 千分位呈现（数据来自端点，非源码面量）。 */
export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

/** 档位展示名：i18n 按 code 取（translate 传入以保持本函数无 hook），未知名兜底原样展示 code。 */
export function planDisplayName(
  translate: (key: string) => string,
  planCode: string,
): string {
  const key = `pricing.tiers.${planCode}.name`;
  const label = translate(key);
  // i18next 对缺失键返回键名本身——以含点判断为未命中，兜底展示原始 code。
  return label === key ? planCode : label;
}

export function UsagePage() {
  const { t } = useTranslation();
  const [usage, setUsage] = useState<ConsoleUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setUsage(await fetchConsoleUsage());
    } catch (err) {
      setUsage(null);
      setError(apiErrorText(err, t("console.usage.errorTitle")));
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
        <h1 className="text-lg font-semibold">{t("console.usage.title")}</h1>
        <p className="mt-6 text-xs text-slate-400" data-testid="usage-loading">
          {t("console.usage.loading")}
        </p>
      </div>
    );
  }

  if (error || usage == null) {
    return (
      <div>
        <h1 className="text-lg font-semibold">{t("console.usage.title")}</h1>
        <div
          data-testid="usage-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">{t("console.usage.errorTitle")}</p>
          {error && (
            <p className="mt-1 break-all font-mono text-[11px] text-red-500">
              {error}
            </p>
          )}
          <button
            type="button"
            data-testid="usage-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.usage.retry")}
          </button>
        </div>
      </div>
    );
  }

  const included = usage.included_lines;
  const used = usage.used_lines;
  const remaining = usage.remaining_lines;
  // 低量阈值 20%：余量占含额比例（含额为 0 时不判低量，直接按耗尽语义呈现）。
  const lowRatio = included > 0 ? remaining / included : 0;
  const exhausted = remaining <= 0;
  const low = !exhausted && lowRatio < 0.2;

  return (
    <div data-testid="usage-page">
      <h1 className="text-lg font-semibold">{t("console.usage.title")}</h1>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.usage.subtitle")}
      </p>

      {exhausted ? (
        <div
          data-testid="usage-exhausted-banner"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm font-medium text-red-700">
            {t("console.usage.exhaustedTitle")}
          </p>
          <p className="mt-1 text-xs text-red-600">
            {t("console.usage.exhaustedBody")}
          </p>
          <a
            data-testid="usage-upgrade-cta"
            href={CONTACT_MAILTO}
            className="mt-3 inline-block rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
          >
            {t("console.usage.upgradeCta")}
          </a>
        </div>
      ) : low ? (
        <div
          data-testid="usage-low-banner"
          className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-4"
        >
          <p className="text-sm font-medium text-amber-700">
            {t("console.usage.lowWarning")}
          </p>
        </div>
      ) : null}

      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div
          data-testid="usage-card-period"
          className="rounded-lg border border-slate-200 p-4"
        >
          <p className="text-xs text-slate-400">{t("console.usage.periodLabel")}</p>
          <p className="mt-1 font-mono text-sm text-slate-800">{usage.period}</p>
        </div>
        <div
          data-testid="usage-card-plan"
          className="rounded-lg border border-slate-200 p-4"
        >
          <p className="text-xs text-slate-400">{t("console.usage.planLabel")}</p>
          <p className="mt-1 text-sm text-slate-800">
            {planDisplayName(t, usage.plan_code)}
          </p>
        </div>
        <div
          data-testid="usage-card-used"
          className="rounded-lg border border-slate-200 p-4"
        >
          <p className="text-xs text-slate-400">{t("console.usage.usedLabel")}</p>
          <p className="mt-1 text-sm text-slate-800">
            {formatCount(used)}
            <span className="ml-1 text-xs text-slate-400">{t("admin.ledger.rowsUnit")}</span>
          </p>
        </div>
        <div
          data-testid="usage-card-included"
          className="rounded-lg border border-slate-200 p-4"
        >
          <p className="text-xs text-slate-400">
            {t("console.usage.includedLabel")}
          </p>
          <p className="mt-1 text-sm text-slate-800">
            {formatCount(included)}
            <span className="ml-1 text-xs text-slate-400">{t("admin.ledger.rowsUnit")}</span>
          </p>
        </div>
      </div>

      <div
        data-testid="usage-card-remaining"
        className={`mt-4 rounded-lg border p-4 ${
          exhausted
            ? "border-red-200 bg-red-50/50"
            : low
              ? "border-amber-200 bg-amber-50/50"
              : "border-slate-200"
        }`}
      >
        <p className="text-xs text-slate-400">{t("console.usage.remainingLabel")}</p>
        <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">
          {formatCount(remaining)}
          <span className="ml-1 text-sm font-normal text-slate-400">
            {t("admin.ledger.rowsUnit")}
          </span>
        </p>
        <p className="mt-2 text-xs text-slate-500">{t("console.usage.stopNote")}</p>
      </div>
    </div>
  );
}
