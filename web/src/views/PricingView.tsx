// 定价页（#/pricing，wire-self-serve-launch 任务 7.1）——四卡活取。
// - 三张公开卡从 GET /api/v1/plans 渲染（免费/基础/标准；价格与含额行数为
//   后端数据，源码零档位面量）+ 一张静态「自定义」卡：主 CTA 站内跳需求单
//   表单（wire-ux-consolidation 任务 4.4），mailto 联系降为次级小链接。
// - 副标题为「用完即停、次月恢复」口径；全部档位无超额单价。
// - 请求失败回退 = 无数字卡片（沿用目录页 fallback 范式：名字/能力仍在，
//   数字退场），绝不回退到写死的价格字面量。
// - 目录规模数字活取 /v1/catalog/stats（千位向下取整）；取不到时回退
//   不带数字的文案。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { CONTACT_MAILTO } from "./HomeView";
import { demandNewHref } from "../catalog/api";
import {
  fetchPublicPlans,
  formatIncludedLines,
  formatPlanPriceYuan,
  type PublicPlan,
} from "../pricing/api";
import { formatCatalogTotal } from "../catalog/api";
import { useCatalogStats } from "../catalog/useCatalogStats";

/** 档位卡展示名：i18n 按 code 取；未登记的 code 兜底原样展示。 */
function tierName(translate: (key: string) => string, code: string): string {
  const key = `pricing.tiers.${code}.name`;
  const label = translate(key);
  return label === key ? code : label;
}

function tierDesc(translate: (key: string) => string, code: string): string {
  const key = `pricing.tiers.${code}.desc`;
  const label = translate(key);
  return label === key ? "" : label;
}

export function PricingView() {
  const { t } = useTranslation();
  const stats = useCatalogStats();
  const catalogTotal = formatCatalogTotal(stats?.registered ?? 0);

  // null = 加载中；failed = 活取失败（回退无数字卡片）。
  const [plans, setPlans] = useState<PublicPlan[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchPublicPlans()
      .then((value) => {
        if (!cancelled) setPlans(value);
      })
      .catch(() => {
        if (!cancelled) {
          setPlans([]);
          setFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const features = [
    catalogTotal
      ? t("pricing.featCatalog", { total: catalogTotal })
      : t("pricing.featCatalogNoCount"),
    t("pricing.featMcp"),
    t("pricing.featDemand"),
  ];

  const cardShell = (emphasized: boolean, dashed = false) =>
    `relative rounded-lg border p-6 ${
      emphasized
        ? "border-slate-900 ring-1 ring-slate-900"
        : dashed
          ? "border-dashed border-slate-300"
          : "border-slate-200"
    }`;

  return (
    <div>
      <section className="py-10 text-center">
        <h1 className="text-3xl font-semibold tracking-tight">
          {t("pricing.title")}
        </h1>
        <p className="mt-3 text-slate-500 max-w-2xl mx-auto text-sm">
          {t("pricing.subtitle")}
        </p>
      </section>

      {failed && (
        <p
          data-testid="pricing-load-failed"
          className="mx-auto max-w-2xl rounded-md border border-amber-200 bg-amber-50 px-4 py-2 text-center text-xs text-amber-700"
        >
          {t("pricing.loadFailedNote")}
        </p>
      )}

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 items-start">
        {plans == null && failed === false && (
          <p className="col-span-full text-center text-xs text-slate-400" data-testid="pricing-loading">
            {t("pricing.loading")}
          </p>
        )}
        {(plans ?? []).map((plan) => {
          const showNumbers = !failed;
          return (
            <div key={plan.code} data-testid={`tier-${plan.code}`} className={cardShell(false)}>
              <h2 className="font-medium">{tierName(t, plan.code)}</h2>
              <p className="mt-1 text-xs text-slate-500">
                {tierDesc(t, plan.code)}
              </p>
              {showNumbers && (
                <>
                  <p className="mt-4 text-3xl font-semibold tracking-tight">
                    {formatPlanPriceYuan(plan.monthly_price_yuan)}
                    <span className="ml-1 text-sm font-normal text-slate-400">
                      {t("pricing.perMonth")}
                    </span>
                  </p>
                  <p className="mt-2 text-sm text-slate-600">
                    {t("pricing.includedLines", {
                      count: formatIncludedLines(plan.included_lines),
                    })}
                  </p>
                  <p className="text-sm text-slate-600">
                    {t("pricing.stopAtQuotaLine")}
                  </p>
                </>
              )}
              <ul className="mt-4 space-y-2 border-t border-slate-100 pt-4">
                {features.map((f) => (
                  <li key={f} className="flex items-start gap-2 text-sm">
                    <span
                      aria-hidden
                      className="mt-0.5 inline-block h-1.5 w-1.5 rounded-full bg-slate-400"
                    />
                    {f}
                  </li>
                ))}
              </ul>
              <a
                href={CONTACT_MAILTO}
                className="mt-6 block rounded-md px-4 py-2 text-center text-sm transition-colors border border-slate-300 hover:bg-slate-100"
              >
                {t("pricing.cta")}
              </a>
            </div>
          );
        })}

        {/* 静态「自定义」询价卡：不进公开档位列表。主 CTA 站内跳需求单表单
           （wire-ux-consolidation 任务 4.4），mailto 降为次级小链接保留。 */}
        <div data-testid="tier-custom" className={cardShell(false, true)}>
          <h2 className="font-medium">{tierName(t, "custom")}</h2>
          <p className="mt-1 text-xs text-slate-500">{tierDesc(t, "custom")}</p>
          <p className="mt-4 text-3xl font-semibold tracking-tight">
            {t("pricing.customQuote")}
          </p>
          <ul className="mt-4 space-y-2 border-t border-slate-100 pt-4">
            {features.map((f) => (
              <li key={f} className="flex items-start gap-2 text-sm">
                <span
                  aria-hidden
                  className="mt-0.5 inline-block h-1.5 w-1.5 rounded-full bg-slate-400"
                />
                {f}
              </li>
            ))}
          </ul>
          <a
            href={demandNewHref()}
            data-testid="tier-custom-cta"
            className="mt-6 block rounded-md bg-slate-900 px-4 py-2 text-center text-sm text-white hover:bg-slate-700 transition-colors"
          >
            {t("pricing.ctaCustom")}
          </a>
          <a
            href={CONTACT_MAILTO}
            data-testid="tier-custom-mailto"
            className="mt-2 block text-center text-xs text-slate-500 underline hover:text-slate-900"
          >
            {t("pricing.ctaCustomMailto")}
          </a>
        </div>
      </section>
      <section className="mt-8 space-y-1 text-xs text-slate-400">
        <p>{t("pricing.fnDemand")}</p>
        <p>{t("pricing.fnLegal")}</p>
      </section>
    </div>
  );
}
