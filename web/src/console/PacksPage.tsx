// 控制台「源包」页（#/console/packs，wire-source-packs 任务 3.1/3.2/3.4）。
// - 包卡片墙：双语名（按当前语言取 name_zh/name_en）/描述/指标数徽标/
//   已订阅徽标 + 「仅看已订阅」过滤开关；免费档可用（无档位闸）。
// - 包详情：卡片点击内嵌展开（对齐 DatasetsConnectors 的展开风格）分页
//   指标清单（code/名称/domain），每行链到目录详情页 #/catalog/{code}。
// - 订阅/退订：按钮即时切换本地态并刷新卡片徽标（幂等端点，重复操作 200）。
// - 公开浏览不受影响：列表/详情为公开端点；订阅面未登录（401/无会话）时
//   按钮改为引导登录，页面其余部分照常。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  ConsoleApiError,
  fetchPackDetail,
  fetchPacks,
  fetchPackSubscriptions,
  subscribePack,
  unsubscribePack,
  type PackDetail,
  type PackSummary,
} from "./api";

/** 按当前语言取包名：zh 系 → name_zh，其余 → name_en（缺省兜底另一侧）。 */
export function packName(
  pack: { name_zh: string; name_en: string },
  language: string,
): string {
  const zh = language.toLowerCase().startsWith("zh");
  const primary = zh ? pack.name_zh : pack.name_en;
  return primary || pack.name_zh || pack.name_en;
}

export function PacksPage() {
  const { t, i18n } = useTranslation();
  // 包列表（公开端点）：null = 加载中。
  const [packs, setPacks] = useState<PackSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 订阅面（登录端点）：authed=false 即未登录/会话失效——公开浏览不受影响。
  const [subscribedIds, setSubscribedIds] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const [authed, setAuthed] = useState(true);
  const [onlySubscribed, setOnlySubscribed] = useState(false);
  // 展开/操作态。
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [loginHint, setLoginHint] = useState(false);

  const loadPacks = useCallback(async () => {
    setError(null);
    setPacks(null);
    try {
      setPacks(await fetchPacks());
    } catch (err) {
      setError(apiErrorText(err, t("console.packs.errorTitle")));
    }
  }, [t]);

  const loadSubscriptions = useCallback(async () => {
    try {
      const ids = await fetchPackSubscriptions();
      setSubscribedIds(new Set(ids));
      setAuthed(true);
    } catch (err) {
      // 401（无会话/令牌被拒）= 未登录态：订阅按钮引导登录，不算页面错误。
      if (err instanceof ConsoleApiError && err.status === 401) {
        setAuthed(false);
        return;
      }
      // 其余失败（网络/5xx）：订阅态未知，按空集呈现并提示。
      setSubscribedIds(new Set());
      setAuthed(true);
      setActionError(apiErrorText(err, t("console.packs.actionErrorTitle")));
    }
  }, [t]);

  useEffect(() => {
    void loadPacks();
  }, [loadPacks]);

  useEffect(() => {
    void loadSubscriptions();
  }, [loadSubscriptions]);

  /** 订阅/退订：即时切换本地态（幂等端点保证服务端一致），徽标随 state 刷新。 */
  const toggleSubscribe = async (packId: string) => {
    if (pendingId != null) return;
    if (!authed) {
      setLoginHint(true);
      return;
    }
    const subscribed = subscribedIds.has(packId);
    setPendingId(packId);
    setActionError(null);
    setLoginHint(false);
    try {
      if (subscribed) {
        await unsubscribePack(packId);
        setSubscribedIds((prev) => {
          const next = new Set(prev);
          next.delete(packId);
          return next;
        });
      } else {
        await subscribePack(packId);
        setSubscribedIds((prev) => new Set(prev).add(packId));
      }
    } catch (err) {
      if (err instanceof ConsoleApiError && err.status === 401) {
        // 会话中途失效：转未登录态并引导登录（浏览不受影响）。
        setAuthed(false);
        setLoginHint(true);
      } else {
        setActionError(apiErrorText(err, t("console.packs.actionErrorTitle")));
      }
    } finally {
      setPendingId(null);
    }
  };

  const visible = (packs ?? []).filter(
    (pack) => !onlySubscribed || subscribedIds.has(pack.id),
  );

  return (
    <div data-testid="packs-page">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("console.packs.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("console.packs.subtitle")}
          </p>
        </div>
        <label
          data-testid="packs-only-subscribed-wrapper"
          className="flex cursor-pointer items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600"
        >
          <input
            type="checkbox"
            data-testid="packs-only-subscribed"
            checked={onlySubscribed}
            onChange={(e) => setOnlySubscribed(e.target.checked)}
            className="h-3.5 w-3.5 accent-slate-800"
          />
          {t("console.packs.onlySubscribed")}
        </label>
      </div>

      {error && (
        <div
          data-testid="packs-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">{t("console.packs.errorTitle")}</p>
          {error && (
            <p className="mt-1 break-all font-mono text-[11px] text-red-500">
              {error}
            </p>
          )}
          <button
            type="button"
            data-testid="packs-retry"
            onClick={() => void loadPacks()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.packs.retry")}
          </button>
        </div>
      )}

      {packs == null && !error && (
        <p className="mt-6 text-xs text-slate-400" data-testid="packs-loading">
          {t("console.packs.loading")}
        </p>
      )}

      {loginHint && (
        <div
          data-testid="packs-login-hint"
          className="mt-4 rounded-md border border-amber-200 bg-amber-50/60 p-3"
        >
          <p className="text-sm text-amber-700">{t("console.packs.loginHint")}</p>
          <a
            href="#/login"
            data-testid="packs-login-cta"
            className="mt-2 inline-block rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
          >
            {t("console.packs.loginCta")}
          </a>
        </div>
      )}

      {actionError && (
        <p
          data-testid="packs-action-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-xs text-red-700"
        >
          {actionError}
        </p>
      )}

      {packs != null && visible.length === 0 && (
        <div
          data-testid="packs-empty"
          className="mt-6 rounded-lg border border-dashed border-slate-300 p-8 text-center"
        >
          <p className="text-sm text-slate-600">
            {onlySubscribed
              ? t("console.packs.emptySubscribed")
              : t("console.packs.empty")}
          </p>
        </div>
      )}

      {visible.length > 0 && (
        <ul data-testid="pack-list" className="mt-4 grid gap-3 md:grid-cols-2">
          {visible.map((pack) => {
            const subscribed = subscribedIds.has(pack.id);
            const expanded = expandedId === pack.id;
            return (
              <li
                key={pack.id}
                data-testid="pack-card"
                data-pack-id={pack.id}
                data-subscribed={subscribed ? "true" : "false"}
                className="rounded-lg border border-slate-200 bg-white p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    data-testid="pack-expand"
                    data-pack-id={pack.id}
                    onClick={() => setExpandedId(expanded ? null : pack.id)}
                    className="text-left text-sm font-medium text-slate-800 underline-offset-2 hover:underline"
                  >
                    {packName(pack, i18n.language)}
                  </button>
                  <span
                    data-testid="pack-indicator-count"
                    className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
                  >
                    {t("console.packs.indicatorCount", {
                      count: pack.indicator_count,
                    })}
                  </span>
                  {subscribed && (
                    <span
                      data-testid="pack-subscribed-badge"
                      className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700 ring-1 ring-emerald-200"
                    >
                      {t("console.packs.subscribedBadge")}
                    </span>
                  )}
                </div>
                <p
                  data-testid="pack-desc"
                  className="mt-1.5 text-xs leading-relaxed text-slate-500"
                >
                  {pack.desc}
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    data-testid="pack-toggle-subscribe"
                    data-pack-id={pack.id}
                    disabled={pendingId === pack.id}
                    onClick={() => void toggleSubscribe(pack.id)}
                    className={`rounded px-2.5 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                      subscribed
                        ? "border border-slate-300 bg-white text-slate-600 hover:bg-slate-100"
                        : "bg-slate-900 text-white hover:bg-slate-700"
                    }`}
                  >
                    {pendingId === pack.id
                      ? subscribed
                        ? t("console.packs.unsubscribing")
                        : t("console.packs.subscribing")
                      : subscribed
                        ? t("console.packs.unsubscribe")
                        : t("console.packs.subscribe")}
                  </button>
                  <button
                    type="button"
                    data-testid="pack-toggle-detail"
                    onClick={() => setExpandedId(expanded ? null : pack.id)}
                    className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                  >
                    {expanded
                      ? t("console.packs.collapse")
                      : t("console.packs.expand")}
                  </button>
                </div>
                {expanded && (
                  <PackIndicatorList key={pack.id} packId={pack.id} />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * 包详情内嵌展开区（任务 3.2）：分页指标清单，每行链到目录详情页。
 * 自持分页状态（key=packId 挂载即重置到第 1 页）。
 */
function PackIndicatorList({ packId }: { packId: string }) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  // 重试令牌：递增即重新拉取当前页（page 不变时 React 会跳过同值更新）。
  const [reloadToken, setReloadToken] = useState(0);
  const [detail, setDetail] = useState<PackDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchPackDetail(packId, page)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setDetail(null);
          setError(apiErrorText(err, t("console.packs.detail.errorTitle")));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [packId, page, reloadToken, t]);

  const pages =
    detail != null ? Math.max(1, Math.ceil(detail.total / detail.page_size)) : 1;

  return (
    <div
      data-testid="pack-detail"
      data-pack-id={packId}
      className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-3"
    >
      {loading && (
        <p
          data-testid="pack-detail-loading"
          className="text-xs text-slate-400"
        >
          {t("console.packs.detail.loading")}
        </p>
      )}
      {error && !loading && (
        <div data-testid="pack-detail-error">
          <p className="text-xs text-red-600">{error}</p>
          <button
            type="button"
            data-testid="pack-detail-retry"
            onClick={() => setReloadToken((n) => n + 1)}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.packs.detail.retry")}
          </button>
        </div>
      )}
      {!loading && !error && detail != null && detail.items.length === 0 && (
        <p data-testid="pack-detail-empty" className="text-xs text-slate-400">
          {t("console.packs.detail.empty")}
        </p>
      )}
      {!loading && !error && detail != null && detail.items.length > 0 && (
        <>
          <div className="overflow-x-auto">
            <table data-testid="pack-indicator-table" className="w-full text-xs">
              <thead>
                <tr>
                  <th className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-500">
                    {t("console.packs.detail.colCode")}
                  </th>
                  <th className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-500">
                    {t("console.packs.detail.colName")}
                  </th>
                  <th className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-500">
                    {t("console.packs.detail.colDomain")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {detail.items.map((indicator) => (
                  <tr key={indicator.code} data-testid="pack-indicator-row">
                    <td className="border-b border-slate-100 px-2 py-1 font-mono text-slate-700">
                      <a
                        data-testid="pack-indicator-link"
                        data-code={indicator.code}
                        href={`#/catalog/${encodeURIComponent(indicator.code)}`}
                        className="text-sky-700 underline-offset-2 hover:underline"
                      >
                        {indicator.code}
                      </a>
                    </td>
                    <td className="border-b border-slate-100 px-2 py-1 text-slate-700">
                      {indicator.name_zh}
                    </td>
                    <td className="border-b border-slate-100 px-2 py-1 text-slate-500">
                      {indicator.domain ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span
              data-testid="pack-detail-page"
              data-page={detail.page}
              className="text-[11px] text-slate-500"
            >
              {t("console.packs.detail.pageOf", {
                page: detail.page,
                pages,
                total: detail.total,
              })}
            </span>
            <button
              type="button"
              data-testid="pack-detail-prev"
              disabled={detail.page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:text-slate-300"
            >
              {t("console.packs.detail.prev")}
            </button>
            <button
              type="button"
              data-testid="pack-detail-next"
              disabled={detail.page >= pages}
              onClick={() => setPage((p) => p + 1)}
              className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:text-slate-300"
            >
              {t("console.packs.detail.next")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
