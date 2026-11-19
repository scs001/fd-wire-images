// 公开指标目录列表页（#/catalog，不登录可访问）：
// 关键词搜索（防抖）+ domain 下拉 + status 筛选 + 分页。
// 只消费目录元数据（无观测数值）；行内导流到详情页。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  fetchIndicators,
  displayName,
  type IndicatorListResponse,
  type IndicatorStatus,
} from "../catalog/api";
import { useDebouncedValue } from "../catalog/useDebouncedValue";
import { useDomains } from "../catalog/useDomains";
import { DomainBadge, StatusBadge } from "../components/IndicatorBadges";
import { DAAS_INSTALL_URL, DAAS_MARKET_URL } from "../catalog/daas";

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;

type ListState = {
  loading: boolean;
  error: string | null;
  data: IndicatorListResponse | null;
};

export function CatalogView() {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const q = useDebouncedValue(input, SEARCH_DEBOUNCE_MS);
  const [domain, setDomain] = useState("");
  const [status, setStatus] = useState<IndicatorStatus | "">("");
  const [page, setPage] = useState(1);
  const [reloadToken, setReloadToken] = useState(0);
  const domains = useDomains();
  const [state, setState] = useState<ListState>({
    loading: true,
    error: null,
    data: null,
  });

  useEffect(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    fetchIndicators({ q, domain, status, page, page_size: PAGE_SIZE })
      .then((data) => {
        if (alive) setState({ loading: false, error: null, data });
      })
      .catch((err: unknown) => {
        if (alive)
          setState({
            loading: false,
            error: err instanceof Error ? err.message : String(err),
            data: null,
          });
      });
    return () => {
      alive = false;
    };
  }, [q, domain, status, page, reloadToken]);

  const onSearch = (value: string) => {
    setInput(value);
    setPage(1);
  };
  const onDomain = (value: string) => {
    setDomain(value);
    setPage(1);
  };
  const onStatus = (value: IndicatorStatus | "") => {
    setStatus(value);
    setPage(1);
  };

  const data = state.data;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / Math.max(1, data.page_size))) : 1;
  const showInitialLoading = state.loading && data == null;
  const showError = state.error != null && data == null;

  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-4">
        <h1 className="text-2xl font-semibold tracking-tight">{t("catalog.title")}</h1>
        <p className="text-sm text-slate-500">{t("catalog.subtitle")}</p>
      </div>

      {/* 自助分析工作区（协同变更 wire-daas-analysis-referral）：常驻静态能力
          条目，与指标条目并列但独立于目录数据管线——不参与搜索/筛选/分页，
          亦不随同步源变化；数据出结果前（loading/error/empty）同样常驻。 */}
      <section
        className="mt-6 rounded-lg border border-slate-200 bg-slate-50 p-5"
        data-testid="daas-analysis-referral"
        aria-label={t("catalog.daasReferralTitle")}
      >
        <h2 className="text-sm font-semibold text-slate-900">
          {t("catalog.daasReferralTitle")}
        </h2>
        <p className="mt-1 text-sm text-slate-600">{t("catalog.daasReferralNote")}</p>
        <div className="mt-4 flex flex-wrap gap-3">
          <a
            href={DAAS_MARKET_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 transition-colors"
          >
            {t("catalog.daasMarketCta")}
          </a>
          <a
            href={DAAS_INSTALL_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 transition-colors"
          >
            {t("catalog.daasInstallCta")}
          </a>
        </div>
      </section>

      <div className="mt-6 flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={input}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={t("catalog.searchPlaceholder")}
          aria-label={t("catalog.searchLabel")}
          className="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-slate-500"
        />
        <select
          value={domain}
          onChange={(e) => onDomain(e.target.value)}
          aria-label={t("catalog.domainLabel")}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-slate-500"
        >
          <option value="">{t("catalog.domainAll")}</option>
          {domains.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => onStatus(e.target.value as IndicatorStatus | "")}
          aria-label={t("catalog.statusLabel")}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-slate-500"
        >
          <option value="">{t("catalog.statusAll")}</option>
          <option value="verified">{t("catalog.statusVerified")}</option>
          <option value="registered">{t("catalog.statusRegistered")}</option>
        </select>
      </div>

      {showInitialLoading && (
        <p className="mt-10 text-center text-sm text-slate-400" data-testid="catalog-loading">
          {t("catalog.loading")}
        </p>
      )}

      {showError && (
        <div
          className="mt-8 rounded-lg border border-red-200 bg-red-50 p-6 text-center"
          data-testid="catalog-error"
        >
          <p className="font-medium text-red-700">{t("catalog.errorTitle")}</p>
          <p className="mt-1 text-sm text-red-600">{t("catalog.errorHint")}</p>
          <button
            type="button"
            onClick={() => setReloadToken((n) => n + 1)}
            className="mt-4 rounded-md border border-red-300 px-4 py-1.5 text-sm text-red-700 hover:bg-red-100 transition-colors"
          >
            {t("catalog.retry")}
          </button>
        </div>
      )}

      {data && data.total === 0 && !state.loading && (
        <div
          className="mt-8 rounded-lg border border-slate-200 p-10 text-center"
          data-testid="catalog-empty"
        >
          <p className="font-medium text-slate-700">{t("catalog.emptyTitle")}</p>
          <p className="mt-1 text-sm text-slate-500">{t("catalog.emptyHint")}</p>
        </div>
      )}

      {data && data.total > 0 && (
        <div
          className={`mt-6 overflow-x-auto transition-opacity ${
            state.loading ? "pointer-events-none opacity-50" : ""
          }`}
        >
          <table className="w-full text-left text-sm" data-testid="catalog-table">
            <thead>
              <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400">
                <th className="py-2 pr-4 font-medium">{t("catalog.colIndicator")}</th>
                <th className="px-4 py-2 font-medium">{t("catalog.colDomain")}</th>
                <th className="px-4 py-2 font-medium">{t("catalog.colFrequency")}</th>
                <th className="px-4 py-2 font-medium">{t("catalog.colStatus")}</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.indicator_id} className="border-b border-slate-100 last:border-0">
                  <td className="py-3 pr-4">
                    <a
                      href={`#/catalog/${encodeURIComponent(item.indicator_id)}`}
                      className="font-medium text-slate-900 hover:underline"
                    >
                      {displayName(item)}
                    </a>
                    <div className="mt-0.5 font-mono text-xs text-slate-500">{item.code}</div>
                  </td>
                  <td className="px-4 py-3">
                    <DomainBadge domain={item.domain} />
                  </td>
                  <td className="px-4 py-3 text-slate-600">{item.frequency?.trim() || "—"}</td>
                  <td className="px-4 py-3">
                    <StatusBadge status={item.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="mt-4 flex items-center justify-between text-sm text-slate-500">
            <span>
              {t("catalog.paginationSummary", {
                total: data.total,
                page: data.page,
                pages: totalPages,
              })}
            </span>
            <span className="flex items-center gap-2">
              <button
                type="button"
                disabled={data.page <= 1}
                onClick={() => setPage(data.page - 1)}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:cursor-not-allowed disabled:opacity-40 hover:bg-slate-100 transition-colors"
              >
                {t("catalog.prevPage")}
              </button>
              <button
                type="button"
                disabled={data.page >= totalPages}
                onClick={() => setPage(data.page + 1)}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:cursor-not-allowed disabled:opacity-40 hover:bg-slate-100 transition-colors"
              >
                {t("catalog.nextPage")}
              </button>
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
