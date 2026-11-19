// 公开橱窗页（wire-showcase-market）：#/showcase 与 #/showcase/{id}
//
// 匿名可达、只读、可被搜索引擎索引——这是柏讯对外的展示面（平台自有看板）。
//
// 三条纪律（design D2/D3）：
// - **零身份**：不读 session、不发带凭据的请求、不出现任何租户标识；
// - **只读**：不渲染工具栏（`DashboardRenderer` 不传 `toolbar`），没有编辑/
//   保存/复制/整板 CSV/导出图片入口；
// - **不注入 noindex**：与登录市场面（MarketPage 注入 noindex）刻意相反——
//   橱窗是给人看的推广面，市场是给客户用的私有面。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { API_BASE } from "../catalog/api";
import { DashboardRenderer } from "../console/dashboard/DashboardRenderer";

/** 橱窗列表卡片（与后端 /api/v1/showcase/dashboards 同形）。 */
export interface ShowcaseCard {
  id: number;
  kind: string;
  title: string;
  version: number;
  published_at: string | null;
  updated_at: string | null;
  panel_count: number | null;
}

/** 橱窗详情（与后端 /api/v1/showcase/dashboards/{id} 同形）。 */
export interface ShowcaseDetail extends ShowcaseCard {
  panels: Array<{ id: string; type: string; title: string }>;
  spec: unknown;
  refreshed_at: string | null;
}

async function showcaseRequest<T>(path: string): Promise<T> {
  const url = `${API_BASE}${path}`;
  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" } });
  } catch (err) {
    throw new Error(`network error fetching ${url}: ${(err as Error).message}`);
  }
  if (!response.ok) {
    throw new Error(`showcase api ${response.status} for ${path}`);
  }
  return (await response.json()) as T;
}

export interface ShowcaseViewProps {
  /** 详情 id（`#/showcase/{id}`）；null/undefined = 列表页。 */
  detailId?: number | null;
}

export function ShowcaseView({ detailId = null }: ShowcaseViewProps) {
  return detailId == null ? <ShowcaseList /> : <ShowcaseDetailView id={detailId} />;
}

function ShowcaseList() {
  const { t } = useTranslation();
  const [state, setState] = useState<{
    loading: boolean;
    error: string | null;
    items: ShowcaseCard[];
    total: number;
  }>({ loading: true, error: null, items: [], total: 0 });

  const load = useCallback(() => {
    setState((s) => ({ ...s, loading: true, error: null }));
    showcaseRequest<{ items: ShowcaseCard[]; total: number }>("/v1/showcase/dashboards")
      .then((data) =>
        setState({ loading: false, error: null, items: data.items, total: data.total }),
      )
      .catch((err: unknown) =>
        setState({
          loading: false,
          error: err instanceof Error ? err.message : String(err),
          items: [],
          total: 0,
        }),
      );
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div data-testid="showcase-list" className="mx-auto max-w-5xl px-4 py-10">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("showcase.title")}
        </h1>
        <p className="mt-2 text-sm text-slate-500">{t("showcase.subtitle")}</p>
      </header>

      {state.loading && (
        <p data-testid="showcase-loading" className="mt-8 text-sm text-slate-400">
          {t("showcase.loading")}
        </p>
      )}

      {state.error && (
        <div data-testid="showcase-error" className="mt-8 rounded-md border border-amber-200 bg-amber-50 p-4">
          <p className="text-sm text-amber-800">{t("showcase.loadError")}</p>
          <button
            type="button"
            onClick={load}
            className="mt-2 rounded border border-amber-300 bg-white px-3 py-1 text-xs text-amber-800"
          >
            {t("showcase.retry")}
          </button>
        </div>
      )}

      {!state.loading && !state.error && state.items.length === 0 && (
        <p data-testid="showcase-empty" className="mt-8 text-sm text-slate-400">
          {t("showcase.empty")}
        </p>
      )}

      {state.items.length > 0 && (
        <ul data-testid="showcase-items" className="mt-8 grid gap-4 sm:grid-cols-2">
          {state.items.map((item) => (
            <li key={item.id}>
              <a
                href={`#/showcase/${item.id}`}
                data-testid="showcase-item"
                className="block rounded-lg border border-slate-200 p-5 transition-colors hover:border-slate-400"
              >
                <span className="block font-medium">{item.title}</span>
                <span className="mt-1 block text-xs text-slate-400">
                  {item.panel_count != null
                    ? t("showcase.panelCount", { count: item.panel_count })
                    : ""}
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ShowcaseDetailView({ id }: { id: number }) {
  const { t } = useTranslation();
  const [state, setState] = useState<{
    loading: boolean;
    error: string | null;
    detail: ShowcaseDetail | null;
  }>({ loading: true, error: null, detail: null });

  useEffect(() => {
    let alive = true;
    setState({ loading: true, error: null, detail: null });
    showcaseRequest<ShowcaseDetail>(`/v1/showcase/dashboards/${id}`)
      .then((detail) => alive && setState({ loading: false, error: null, detail }))
      .catch((err: unknown) => {
        if (alive) {
          setState({
            loading: false,
            error: err instanceof Error ? err.message : String(err),
            detail: null,
          });
        }
      });
    return () => {
      alive = false;
    };
  }, [id]);

  return (
    <div data-testid="showcase-detail" className="mx-auto max-w-5xl px-4 py-10">
      <a href="#/showcase" className="text-xs text-slate-500 hover:text-slate-900">
        ← {t("showcase.backToList")}
      </a>

      {state.loading && (
        <p data-testid="showcase-loading" className="mt-6 text-sm text-slate-400">
          {t("showcase.loading")}
        </p>
      )}

      {state.error && (
        <div data-testid="showcase-error" className="mt-6 rounded-md border border-amber-200 bg-amber-50 p-4">
          <p className="text-sm text-amber-800">{t("showcase.loadError")}</p>
        </div>
      )}

      {state.detail && (
        <>
          <h1
            data-testid="showcase-detail-title"
            className="mt-4 text-xl font-semibold tracking-tight"
          >
            {state.detail.title}
          </h1>
          {state.detail.refreshed_at && (
            <p className="mt-1 text-xs text-slate-400">
              {t("showcase.asOf", { at: state.detail.refreshed_at })}
            </p>
          )}
          {/* 只读渲染：不传 toolbar（无全屏/导出图/复制数据）；渲染器本体
              也从不携带导出 CSV（分享页同款红线）。 */}
          <div data-testid="showcase-renderer" className="mt-6">
            <DashboardRenderer spec={state.detail.spec} />
          </div>
        </>
      )}
    </div>
  );
}
