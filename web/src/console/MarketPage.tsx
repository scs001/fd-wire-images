/**
 * 数据市场（wire-dataset-marketplace 任务 6.2；wire-artifact-marketplace 任务
 * 5.2 加产物页签）：登录客户检索其他客户开放的数据集 / 看板 / 研究报告，
 * 订阅（open 模式）或申请（approval 模式）后只读使用。
 *
 * 红线纪律：
 * - 列表由服务端在 **SQL 层**只查 visibility='market'——私有数据/产物永不出
 *   现在本页（前端不做也不该做"过滤"兜底，服务端保证）；
 * - 数据集详情响应只含元数据 + 列 schema + 行数，**无任何数据值**——本页只
 *   渲染服务端给的白名单字段，不请求样本行；
 * - 产物详情只含元数据 + 面板结构摘要（{id,type,title} 三字段 + 报告
 *   data_scope/data_range）——快照/查询配置不进本页（wire-artifact-marketplace
 *   Q8）；市场详情与渲染页如实披露「公开看板/报告包含发布时刻的数据快照」
 *   （Q1，禁止「仅结构不含数据」措辞——见产物页选择器区块的常驻披露）。
 *
 * 订阅语义：open → 订阅即得；approval → 走申请（access-requests），按钮态
 * 按此显示（数据集页签沿用既有 subscribed 字段；产物卡片契约无 subscribed
 * 字段——订阅后就地标记，刷新即回服务端真实态）。
 *
 * 复制语义（wire-artifact-marketplace）：复制到我的账号 = spec 结构复制
 * （快照已由服务端剥离）+ 面板绑定重映射报告（bound/unbound 逐面板，
 * unbound 数据集清单如实呈现，提示需申请绑定）；按钮仅在详情内、且
 * allow_copy=true 时出现——allow_copy=false 的 403 detail 如实呈现。
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { API_BASE } from "../catalog/api";
import {
  apiErrorText,
  ConsoleApiError,
  fetchMarketDataset,
  fetchMarketDatasets,
  subscribeMarketDataset,
  unsubscribeMarketDataset,
  type MarketDatasetCard,
  type MarketDatasetDetail,
} from "./api";
import { loadSession } from "./auth";

//: 无头浏览器的 noindex 纪律（与 ShareView 同款）——市场页不进搜索引擎。
function useNoIndex(): void {
  useEffect(() => {
    const meta = document.createElement("meta");
    meta.name = "robots";
    meta.content = "noindex";
    document.head.appendChild(meta);
    return () => {
      document.head.removeChild(meta);
    };
  }, []);
}

// ── 产物市场面（wire-artifact-marketplace 5.2）────────────────────────────
// 契约（api 仓 market/artifact_routes.py，proposal 冻结形状）：
// - GET  /api/v1/market/artifacts?kind=dashboard|research_report → {items,total}
//   （卡片零数据值：元数据 + 轻摘要；SQL 层只查 market 档）；
// - GET  /api/v1/market/artifacts/{id} → 详情：元数据 + panels 结构摘要
//   （仅 {id,type,title}）或报告 data_scope/data_range——无 snapshot/config/query；
// - POST /api/v1/market/artifacts/{id}/subscribe（open 档；approval 档 409
//   引导走申请）与 DELETE 同路径（退订）；
// - POST /api/v1/market/artifacts/{id}/access-requests（approval 档申请）；
// - POST /api/v1/market/artifacts/{id}/copy → {artifact_id,title,kind,
//   copied_from_artifact_id,binding_report:{bound,unbound,unbound_datasets}}
//   （快照剥离 + 绑定重映射；allow_copy=false / 未订阅 403 detail 如实）。

/** 产物市场卡片（列表项）：元数据 + 轻摘要；绝无列 schema 或数据值。 */
export interface MarketArtifactCard {
  id: number;
  kind: string;
  title: string;
  version: number;
  publisher_customer_id: string;
  /** 自有产物经市场面可见时 true（便于标注）。 */
  is_mine?: boolean;
  /** 平台自营发布方（wire-showcase-market）：登录客户据此区分平台自有内容。 */
  is_platform?: boolean;
  access_mode: "open" | "approval";
  allow_copy: boolean;
  published_at: string | null;
  /** 可选轻摘要（列表契约不保证下发；有则渲染、无则如实不渲染）。 */
  description?: string | null;
  panel_count?: number | null;
}

/** 详情面板结构摘要（Q8：只有 id/type/title 三字段）。 */
export interface MarketArtifactPanelSummary {
  id: string;
  type: string;
  title: string;
}

/** 产物市场详情：卡片 + 面板结构摘要 / 报告范围元信息（零 snapshot/config）。 */
export interface MarketArtifactDetail extends MarketArtifactCard {
  description?: string | null;
  version_status?: string;
  access_source?: string | null;
  panel_count?: number | null;
  /** 看板：面板结构摘要清单。 */
  panels?: MarketArtifactPanelSummary[];
  /** 研究报告：数据范围元信息（有则显示）。 */
  data_scope?: unknown;
  data_range?: unknown;
}

/** 复制绑定报告（逐面板：bound/unbound 计数 + unbound 数据集清单）。 */
export interface ArtifactBindingReport {
  bound: number;
  unbound: number;
  unbound_datasets: string[];
}

/** 复制响应（POST /market/artifacts/{id}/copy）。 */
export interface MarketArtifactCopyResult {
  artifact_id: number;
  title: string;
  kind: string;
  copied_from_artifact_id: number;
  binding_report: ArtifactBindingReport;
}

/** 本页专属产物端点的轻量请求器（api.ts requestJson 同语义：Bearer 会话头、
 *  非 2xx 抛 ConsoleApiError（带后端 detail 文案，如复制 403 说明）、204 → null）。 */
async function marketArtifactRequest(
  path: string,
  method: "GET" | "POST" | "DELETE",
  body?: unknown,
): Promise<unknown> {
  const session = loadSession();
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        ...(session?.idToken
          ? { Authorization: `Bearer ${session.idToken}` }
          : {}),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (err) {
    throw new ConsoleApiError(
      `network error fetching ${path}: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data: unknown = await response.json();
      if (
        data &&
        typeof data === "object" &&
        typeof (data as { detail?: unknown }).detail === "string"
      ) {
        detail = (data as { detail: string }).detail;
      }
    } catch {
      /* 无 JSON body：保留 undefined（走兜底文案） */
    }
    throw new ConsoleApiError(
      `market artifacts api ${response.status} for ${path}`,
      response.status,
      detail,
    );
  }
  if (response.status === 204) return null;
  return response.json();
}

/** 市场产物列表（kind 筛选；SQL 层 market 档过滤由服务端保证）。 */
async function fetchMarketArtifacts(kind: string): Promise<{
  items: MarketArtifactCard[];
  total: number;
}> {
  const params = new URLSearchParams({ kind });
  const body = (await marketArtifactRequest(
    `/v1/market/artifacts?${params}`,
    "GET",
  )) as { items?: unknown; total?: unknown };
  const items = Array.isArray(body.items)
    ? (body.items as MarketArtifactCard[])
    : [];
  const total =
    typeof body.total === "number" ? body.total : items.length;
  return { items, total };
}

/** 市场产物详情（面板结构摘要 / 报告范围元信息——零数据值）。 */
function fetchMarketArtifact(id: number): Promise<MarketArtifactDetail> {
  return marketArtifactRequest(
    `/v1/market/artifacts/${id}`,
    "GET",
  ) as Promise<MarketArtifactDetail>;
}

/** 订阅市场产物（open 档订阅即得；幂等）。 */
function subscribeMarketArtifact(id: number): Promise<unknown> {
  return marketArtifactRequest(`/v1/market/artifacts/${id}/subscribe`, "POST");
}

/** 退订市场产物（幂等）。 */
function unsubscribeMarketArtifact(id: number): Promise<unknown> {
  return marketArtifactRequest(
    `/v1/market/artifacts/${id}/subscribe`,
    "DELETE",
  );
}

/** 发起使用申请（approval 档；幂等——已有在途申请回现状）。 */
function requestMarketArtifactAccess(id: number): Promise<unknown> {
  return marketArtifactRequest(
    `/v1/market/artifacts/${id}/access-requests`,
    "POST",
    {},
  );
}

/** 复制到我的账号（快照剥离 + 绑定重映射；403 detail 如实上抛）。 */
function copyMarketArtifact(id: number): Promise<MarketArtifactCopyResult> {
  return marketArtifactRequest(
    `/v1/market/artifacts/${id}/copy`,
    "POST",
  ) as Promise<MarketArtifactCopyResult>;
}

interface MarketItemProps {
  item: MarketDatasetCard;
  onSubscribe: (id: string) => Promise<void>;
  onUnsubscribe: (id: string) => Promise<void>;
  onOpenDetail: (id: string) => void;
  busy: boolean;
}

function MarketItem({ item, onSubscribe, onUnsubscribe, onOpenDetail, busy }: MarketItemProps) {
  const { t } = useTranslation();
  const approval = item.access_mode === "approval";
  const [subscribed, setSubscribed] = useState(item.subscribed);

  useEffect(() => {
    setSubscribed(item.subscribed);
  }, [item.subscribed]);

  return (
    <li
      data-testid={`market-item-${item.id}`}
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <button
          type="button"
          onClick={() => onOpenDetail(item.id)}
          className="text-left"
        >
          <h3 className="text-sm font-semibold text-slate-800 hover:underline">
            {item.name}
          </h3>
          {item.description && (
            <p className="mt-1 line-clamp-2 text-xs text-slate-500">
              {item.description}
            </p>
          )}
        </button>
        <button
          type="button"
          data-testid={`market-subscribe-${item.id}`}
          disabled={busy}
          onClick={async () => {
            if (subscribed) {
              await onUnsubscribe(item.id);
              setSubscribed(false);
            } else {
              await onSubscribe(item.id);
              // approval 模式下订阅转申请：不置 subscribed（等批准）。
              if (!approval) setSubscribed(true);
            }
          }}
          className={`shrink-0 rounded-md px-3 py-1.5 text-xs transition-colors disabled:opacity-40 ${
            subscribed
              ? "bg-slate-100 text-slate-600 hover:bg-slate-200"
              : "bg-slate-900 text-white hover:bg-slate-700"
          }`}
        >
          {subscribed
            ? t("console.market.unsubscribe")
            : approval
              ? t("console.market.requestAccess")
              : t("console.market.subscribe")}
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
        {item.domain && (
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">
            {item.domain}
          </span>
        )}
        {item.tags.slice(0, 4).map((tag) => (
          <span key={tag} className="rounded bg-slate-50 px-1.5 py-0.5">
            #{tag}
          </span>
        ))}
        <span>{t("console.market.rowsLabel", { count: item.row_count })}</span>
        {item.published_at && (
          <span>{t("console.market.publishedAt", { at: item.published_at })}</span>
        )}
      </div>
    </li>
  );
}

/** 产物市场卡片（wire-artifact-marketplace）：标题/描述/发布方/面板数/版本 +
 *  订阅（open）或申请使用（approval）按钮；点标题展开详情。 */
function ArtifactMarketItem({
  item,
  busy,
  onSubscribe,
  onUnsubscribe,
  onRequestAccess,
  onOpenDetail,
}: {
  item: MarketArtifactCard;
  busy: boolean;
  onSubscribe: (id: number) => Promise<void>;
  onUnsubscribe: (id: number) => Promise<void>;
  onRequestAccess: (id: number) => Promise<void>;
  onOpenDetail: (id: number) => void;
}) {
  const { t } = useTranslation();
  const approval = item.access_mode === "approval";
  // 产物卡片契约无 subscribed 字段：订阅成功后就地标记（刷新回服务端真实态）。
  const [subscribed, setSubscribed] = useState(false);

  return (
    <li
      data-testid={`market-artifact-item-${item.id}`}
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <button
          type="button"
          onClick={() => onOpenDetail(item.id)}
          className="text-left"
        >
          <h3 className="text-sm font-semibold text-slate-800 hover:underline">
            {item.title}
          </h3>
          {item.description && (
            <p className="mt-1 line-clamp-2 text-xs text-slate-500">
              {item.description}
            </p>
          )}
        </button>
        {approval ? (
          <button
            type="button"
            data-testid={`market-artifact-request-${item.id}`}
            disabled={busy}
            onClick={() => void onRequestAccess(item.id)}
            className="shrink-0 rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white transition-colors hover:bg-slate-700 disabled:opacity-40"
          >
            {t("console.market.requestAccess")}
          </button>
        ) : (
          <button
            type="button"
            data-testid={`market-artifact-subscribe-${item.id}`}
            disabled={busy}
            onClick={async () => {
              if (subscribed) {
                await onUnsubscribe(item.id);
                setSubscribed(false);
              } else {
                await onSubscribe(item.id);
                setSubscribed(true);
              }
            }}
            className={`shrink-0 rounded-md px-3 py-1.5 text-xs transition-colors disabled:opacity-40 ${
              subscribed
                ? "bg-slate-100 text-slate-600 hover:bg-slate-200"
                : "bg-slate-900 text-white hover:bg-slate-700"
            }`}
          >
            {subscribed
              ? t("console.market.unsubscribe")
              : t("console.market.subscribe")}
          </button>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">
          {t("console.market.artifacts.publisherLabel")}
          {item.publisher_customer_id}
        </span>
        {item.is_platform && (
          <span
            data-testid="market-platform-badge"
            className="rounded bg-indigo-50 px-1.5 py-0.5 font-medium text-indigo-700"
          >
            {t("console.market.artifacts.platformBadge")}
          </span>
        )}
        {item.panel_count != null && (
          <span>
            {t("console.market.artifacts.panelsLabel", {
              count: item.panel_count,
            })}
          </span>
        )}
        <span>{t("console.artifacts.versionLabel", { version: item.version })}</span>
        {item.published_at && (
          <span>{t("console.market.publishedAt", { at: item.published_at })}</span>
        )}
      </div>
    </li>
  );
}

type MarketTab = "datasets" | "dashboards" | "reports";

/** 产物页签 → 服务端 kind 参数。 */
const ARTIFACT_TAB_KIND: Record<Exclude<MarketTab, "datasets">, string> = {
  dashboards: "dashboard",
  reports: "research_report",
};

interface ArtifactTabState {
  items: MarketArtifactCard[];
  /** null = 尚未取到（徽标暂不渲染——不编造 0）。 */
  total: number | null;
  loading: boolean;
  error: string | null;
}

/** 市场页三页签（Q14 裁决：默认数据集页签；产物页签带计数徽章）。 */
const MARKET_TAB_IDS: MarketTab[] = ["datasets", "dashboards", "reports"];

const EMPTY_ARTIFACT_TAB: ArtifactTabState = {
  items: [],
  total: null,
  loading: true,
  error: null,
};

export function MarketPage() {
  const { t } = useTranslation();
  useNoIndex();

  const [tab, setTab] = useState<MarketTab>("datasets");

  // ── 数据集页签态（wire-dataset-marketplace；现有逻辑原样）──
  const [items, setItems] = useState<MarketDatasetCard[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const [domain, setDomain] = useState("");
  const [tag, setTag] = useState("");
  const [detail, setDetail] = useState<MarketDatasetDetail | null>(null);

  // ── 产物页签态（wire-artifact-marketplace 5.2）──
  const [artifactTabs, setArtifactTabs] = useState<
    Record<Exclude<MarketTab, "datasets">, ArtifactTabState>
  >({
    dashboards: { ...EMPTY_ARTIFACT_TAB },
    reports: { ...EMPTY_ARTIFACT_TAB },
  });
  const [artifactDetail, setArtifactDetail] =
    useState<MarketArtifactDetail | null>(null);
  const [copyReport, setCopyReport] = useState<{
    artifactId: number;
    report: ArtifactBindingReport;
  } | null>(null);
  const [artifactActionError, setArtifactActionError] = useState<string | null>(
    null,
  );
  const [artifactBusy, setArtifactBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await fetchMarketDatasets({
        q: q.trim() || undefined,
        domain: domain.trim() || undefined,
        tag: tag.trim() || undefined,
      });
      setItems(page.items);
      setTotal(page.total);
    } catch (err) {
      setError(apiErrorText(err, t("console.market.loadError")));
    } finally {
      setLoading(false);
    }
  }, [q, domain, tag, t]);

  useEffect(() => {
    void load();
  }, [load]);

  /** 产物页签列表加载（kind 两档各自独立；失败只影响本页签）。 */
  const loadArtifacts = useCallback(
    async (tabId: Exclude<MarketTab, "datasets">) => {
      setArtifactTabs((prev) => ({
        ...prev,
        [tabId]: { ...prev[tabId], loading: true, error: null },
      }));
      try {
        const page = await fetchMarketArtifacts(ARTIFACT_TAB_KIND[tabId]);
        setArtifactTabs((prev) => ({
          ...prev,
          [tabId]: {
            items: page.items,
            total: page.total,
            loading: false,
            error: null,
          },
        }));
      } catch (err) {
        setArtifactTabs((prev) => ({
          ...prev,
          [tabId]: {
            ...prev[tabId],
            loading: false,
            error: apiErrorText(err, t("console.market.artifacts.loadError")),
          },
        }));
      }
    },
    [t],
  );

  // 挂载即拉三个页签（页签计数徽章在未切换时也要有数——各自失败互不影响）。
  useEffect(() => {
    void loadArtifacts("dashboards");
    void loadArtifacts("reports");
  }, [loadArtifacts]);

  const handleSubscribe = useCallback(
    async (id: string) => {
      setBusy(true);
      setActionError(null);
      try {
        await subscribeMarketDataset(id);
        await load();
      } catch (err) {
        setActionError(apiErrorText(err, t("console.market.actionError")));
      } finally {
        setBusy(false);
      }
    },
    [load, t],
  );

  const handleUnsubscribe = useCallback(
    async (id: string) => {
      setBusy(true);
      setActionError(null);
      try {
        await unsubscribeMarketDataset(id);
        await load();
      } catch (err) {
        setActionError(apiErrorText(err, t("console.market.actionError")));
      } finally {
        setBusy(false);
      }
    },
    [load, t],
  );

  const openDetail = useCallback(
    async (id: string) => {
      setActionError(null);
      try {
        setDetail(await fetchMarketDataset(id));
      } catch (err) {
        setActionError(apiErrorText(err, t("console.market.actionError")));
      }
    },
    [t],
  );

  /** 产物订阅 / 退订 / 申请（产物页签动作；失败 detail 如实呈现）。 */
  const runArtifactAction = useCallback(
    async (id: number, action: "subscribe" | "unsubscribe" | "request") => {
      setArtifactBusy(true);
      setArtifactActionError(null);
      try {
        const caller =
          action === "subscribe"
            ? subscribeMarketArtifact
            : action === "unsubscribe"
              ? unsubscribeMarketArtifact
              : requestMarketArtifactAccess;
        await caller(id);
        await loadArtifacts(tab === "reports" ? "reports" : "dashboards");
      } catch (err) {
        setArtifactActionError(
          apiErrorText(err, t("console.market.actionError")),
        );
      } finally {
        setArtifactBusy(false);
      }
    },
    [loadArtifacts, tab, t],
  );

  const handleArtifactSubscribe = useCallback(
    (id: number) => runArtifactAction(id, "subscribe"),
    [runArtifactAction],
  );

  const handleArtifactUnsubscribe = useCallback(
    (id: number) => runArtifactAction(id, "unsubscribe"),
    [runArtifactAction],
  );

  const handleArtifactRequest = useCallback(
    (id: number) => runArtifactAction(id, "request"),
    [runArtifactAction],
  );

  /** 产物详情（点卡片标题；切换产物时清除上一份复制报告）。 */
  const openArtifactDetail = useCallback(
    async (id: number) => {
      setArtifactActionError(null);
      setCopyReport(null);
      try {
        setArtifactDetail(await fetchMarketArtifact(id));
      } catch (err) {
        setArtifactDetail(null);
        setArtifactActionError(
          apiErrorText(err, t("console.market.actionError")),
        );
      }
    },
    [t],
  );

  /** 复制到我的账号（403 detail 如实呈现；成功展示逐面板绑定报告）。 */
  const handleCopy = useCallback(
    async (id: number) => {
      if (artifactBusy) return;
      setArtifactBusy(true);
      setArtifactActionError(null);
      setCopyReport(null);
      try {
        const result = await copyMarketArtifact(id);
        setCopyReport({ artifactId: id, report: result.binding_report });
      } catch (err) {
        setArtifactActionError(
          apiErrorText(err, t("console.market.actionError")),
        );
      } finally {
        setArtifactBusy(false);
      }
    },
    [artifactBusy, t],
  );

  const switchTab = useCallback((next: MarketTab) => {
    setTab(next);
    if (next !== "datasets") {
      // 进产物页签时清数据集详情，避免两页签的面板同时在场。
      setDetail(null);
    } else {
      setArtifactDetail(null);
      setCopyReport(null);
      setArtifactActionError(null);
    }
  }, []);

  /** 页签徽标计数（对应的列表接口 total；未取到不渲染）。 */
  const tabCount = (tabId: MarketTab): number | null =>
    tabId === "datasets" ? total : artifactTabs[tabId].total;

  // ── 数据集页签（现有全部逻辑，原样拆出）──
  const datasetsTab = (
    <>
      <form
        className="mt-4 flex flex-wrap items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <input
          data-testid="market-search-input"
          type="search"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          placeholder={t("console.market.searchPlaceholder")}
          className="min-w-56 flex-1 rounded-md border border-slate-200 px-3 py-1.5 text-sm"
        />
        <input
          type="text"
          value={domain}
          onChange={(event) => setDomain(event.target.value)}
          placeholder={t("console.market.domainPlaceholder")}
          className="w-32 rounded-md border border-slate-200 px-3 py-1.5 text-sm"
        />
        <input
          type="text"
          value={tag}
          onChange={(event) => setTag(event.target.value)}
          placeholder={t("console.market.tagPlaceholder")}
          className="w-32 rounded-md border border-slate-200 px-3 py-1.5 text-sm"
        />
        <button
          type="submit"
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
        >
          {t("console.market.search")}
        </button>
      </form>

      {actionError && (
        <p data-testid="market-action-error" className="mt-3 text-xs text-rose-600">
          {actionError}
        </p>
      )}

      {loading && (
        <p className="mt-6 text-sm text-slate-500">{t("console.market.loading")}</p>
      )}
      {error && !loading && (
        <div className="mt-6 text-sm text-rose-600">
          {error}
          <button
            type="button"
            onClick={() => void load()}
            className="ml-2 text-xs underline"
          >
            {t("console.market.retry")}
          </button>
        </div>
      )}
      {!loading && !error && items.length === 0 && (
        <p data-testid="market-empty" className="mt-6 text-sm text-slate-500">
          {t("console.market.empty")}
        </p>
      )}
      {!loading && !error && items.length > 0 && (
        <>
          <p className="mt-4 text-xs text-slate-400">
            {t("console.market.total", { total: total ?? items.length })}
          </p>
          <ul data-testid="market-list" className="mt-2 space-y-3">
            {items.map((item) => (
              <MarketItem
                key={item.id}
                item={item}
                busy={busy}
                onSubscribe={handleSubscribe}
                onUnsubscribe={handleUnsubscribe}
                onOpenDetail={openDetail}
              />
            ))}
          </ul>
        </>
      )}

      {detail && (
        <section
          data-testid="market-detail"
          className="mt-6 rounded-lg border border-slate-200 bg-slate-50/60 p-4"
        >
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-800">
              {t("console.market.detailTitle")}：{detail.name}
            </h2>
            <button
              type="button"
              onClick={() => setDetail(null)}
              className="text-xs text-slate-500 underline"
            >
              {t("console.market.close")}
            </button>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            {t("console.market.rowsLabel", { count: detail.row_count })}
          </p>
          <h3 className="mt-3 text-xs font-semibold text-slate-600">
            {t("console.market.columnsTitle")}
          </h3>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {detail.columns.map((column) => (
              <li
                key={column.name}
                className="rounded bg-white px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
              >
                {column.name}
                <span className="ml-1 text-slate-400">{column.type}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );

  // ── 产物页签（看板 / 报告同构；kind 由页签分派）──
  const artifactsTab = (tabId: Exclude<MarketTab, "datasets">) => {
    const state = artifactTabs[tabId];
    return (
      <>
        {artifactActionError && (
          <p data-testid="market-artifact-error" className="mt-3 text-xs text-rose-600">
            {artifactActionError}
          </p>
        )}

        {state.loading && (
          <p className="mt-6 text-sm text-slate-500">
            {t("console.market.artifacts.loading")}
          </p>
        )}
        {!state.loading && state.error && (
          <div
            data-testid="market-artifact-error"
            className="mt-6 text-sm text-rose-600"
          >
            {state.error}
            <button
              type="button"
              onClick={() => void loadArtifacts(tabId)}
              className="ml-2 text-xs underline"
            >
              {t("console.market.retry")}
            </button>
          </div>
        )}
        {!state.loading && !state.error && state.items.length === 0 && (
          <p data-testid="market-artifact-empty" className="mt-6 text-sm text-slate-500">
            {t("console.market.artifacts.empty")}
          </p>
        )}
        {!state.loading && !state.error && state.items.length > 0 && (
          <ul data-testid="market-artifact-list" className="mt-4 space-y-3">
            {state.items.map((item) => (
              <ArtifactMarketItem
                key={item.id}
                item={item}
                busy={artifactBusy}
                onSubscribe={handleArtifactSubscribe}
                onUnsubscribe={handleArtifactUnsubscribe}
                onRequestAccess={handleArtifactRequest}
                onOpenDetail={(id) => void openArtifactDetail(id)}
              />
            ))}
          </ul>
        )}

        {artifactDetail && (
          <section
            data-testid="market-artifact-detail"
            className="mt-6 rounded-lg border border-slate-200 bg-slate-50/60 p-4"
          >
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-800">
                {t("console.market.artifacts.detailTitle")}：{artifactDetail.title}
              </h2>
              <button
                type="button"
                onClick={() => {
                  setArtifactDetail(null);
                  setCopyReport(null);
                }}
                className="text-xs text-slate-500 underline"
              >
                {t("console.market.close")}
              </button>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">
                {t("console.market.artifacts.publisherLabel")}
                {artifactDetail.publisher_customer_id}
              </span>
              <span>
                {t("console.artifacts.versionLabel", {
                  version: artifactDetail.version,
                })}
              </span>
              {artifactDetail.published_at && (
                <span>
                  {t("console.market.publishedAt", {
                    at: artifactDetail.published_at,
                  })}
                </span>
              )}
            </div>
            {artifactDetail.description && (
              <p className="mt-2 text-xs text-slate-500">
                {artifactDetail.description}
              </p>
            )}

            {/* 看板：面板结构摘要（Q8——仅 id/type/title，无 config/snapshot）。 */}
            {artifactDetail.panels != null && (
              <>
                <h3 className="mt-3 text-xs font-semibold text-slate-600">
                  {t("console.market.artifacts.panelsTitle")}
                </h3>
                <ul
                  data-testid="market-artifact-panels"
                  className="mt-1 flex flex-wrap gap-1.5"
                >
                  {artifactDetail.panels.map((panel) => (
                    <li
                      key={panel.id}
                      data-panel-id={panel.id}
                      className="rounded bg-white px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
                    >
                      {panel.title || panel.id}
                      <span className="ml-1 text-slate-400">{panel.type}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}

            {/* 研究报告：数据范围元信息（有则显示——Q11）。 */}
            {artifactDetail.data_scope != null && (
              <p className="mt-2 text-xs text-slate-500">
                {t("console.market.artifacts.dataScopeLabel")}：
                {typeof artifactDetail.data_scope === "string"
                  ? artifactDetail.data_scope
                  : JSON.stringify(artifactDetail.data_scope)}
              </p>
            )}
            {artifactDetail.data_range != null && (
              <p className="mt-1 text-xs text-slate-500">
                {t("console.market.artifacts.dataRangeLabel")}：
                {typeof artifactDetail.data_range === "string"
                  ? artifactDetail.data_range
                  : JSON.stringify(artifactDetail.data_range)}
              </p>
            )}

            {/* Q1 披露纪律：公开看板/报告包含发布时刻数据快照——常驻如实
                标注（禁止「仅结构不含数据」措辞）。 */}
            <p
              data-testid="market-artifact-snapshot-disclosure"
              className="mt-2 max-w-2xl rounded border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-[11px] text-amber-700"
            >
              {t("console.market.artifacts.snapshotDisclosure")}
            </p>

            {/* 复制入口：仅在详情里、发布方允许复制（allow_copy=true）时显示。 */}
            {artifactDetail.allow_copy && (
              <button
                type="button"
                data-testid={`market-artifact-copy-${artifactDetail.id}`}
                disabled={artifactBusy}
                onClick={() => void handleCopy(artifactDetail.id)}
                className="mt-3 rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {artifactBusy
                  ? t("console.market.artifacts.copying")
                  : t("console.market.artifacts.copy")}
              </button>
            )}

            {/* 复制结果：逐面板绑定报告（bound/unbound + unbound 数据集清单）。 */}
            {copyReport != null && copyReport.artifactId === artifactDetail.id && (
              <div
                data-testid="market-artifact-copy-report"
                data-bound={copyReport.report.bound}
                data-unbound={copyReport.report.unbound}
                className="mt-3 rounded border border-emerald-200 bg-emerald-50/60 p-3 text-xs"
              >
                <p className="font-medium text-emerald-800">
                  {t("console.market.artifacts.bindReportTitle")}
                </p>
                <p className="mt-1 text-emerald-700">
                  {t("console.market.artifacts.bindReportCounts", {
                    bound: copyReport.report.bound,
                    unbound: copyReport.report.unbound,
                  })}
                </p>
                {copyReport.report.unbound_datasets.length > 0 && (
                  <>
                    <p className="mt-1.5 text-amber-700">
                      {t("console.market.artifacts.bindReportUnbound")}
                    </p>
                    <ul className="mt-1 flex flex-wrap gap-1.5">
                      {copyReport.report.unbound_datasets.map((datasetId) => (
                        <li
                          key={datasetId}
                          className="rounded bg-white px-2 py-0.5 font-mono text-[11px] text-amber-700 ring-1 ring-amber-200"
                        >
                          {datasetId}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}
          </section>
        )}
      </>
    );
  };

  // 宽度由外壳统一给出（wire-console-shell 宽度契约）：页内不再自设整页
  // 宽度与水平内边距，避免宽屏下自我收窄、与其他页签左边缘错位。
  return (
    <div>
      <header>
        <h1 className="text-lg font-semibold text-slate-900">
          {t("console.market.title")}
        </h1>
        <p className="mt-1 text-xs text-slate-500">{t("console.market.subtitle")}</p>
      </header>

      {/* 三页签（Q14：默认数据集；产物页签带计数徽章——total 来自各自列表接口）。 */}
      <div
        role="tablist"
        className="mt-4 flex flex-wrap gap-1 border-b border-slate-200"
      >
        {MARKET_TAB_IDS.map((tabId) => {
          const count = tabCount(tabId);
          return (
            <button
              key={tabId}
              type="button"
              role="tab"
              aria-selected={tab === tabId}
              data-testid={`market-tab-${tabId}`}
              onClick={() => switchTab(tabId)}
              className={`-mb-px flex items-center gap-1.5 rounded-t-md border border-b-0 px-3 py-1.5 text-xs transition-colors ${
                tab === tabId
                  ? "border-slate-200 bg-white font-semibold text-slate-900"
                  : "border-transparent text-slate-500 hover:text-slate-800"
              }`}
            >
              {t(`console.market.tabs.${tabId}`)}
              {count != null && (
                <span
                  data-testid={`market-tab-count-${tabId}`}
                  className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500"
                >
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {tab === "datasets" && datasetsTab}
      {tab !== "datasets" && artifactsTab(tab)}
    </div>
  );
}
