// 控制台「我的数据」页（#/console/datasets，wire-customer-local-data 任务组 3）。
// - 列表卡片：名称/来源徽标/存储层徽标/行数/大小/同步状态（六态如实，暂停带
//   原因）/最近更新；空态引导上传。
// - 上传流：拖拽或选择文件 → 上传解析预览（列名+推断类型+行数）→ 命名建档 → 刷新。
// - 连接器管理在 DatasetsConnectors.tsx、数据集详情在 DatasetDetail.tsx（本页组合三者）。
// - 免费档放行（wire-free-tier-data-gate 4.1-4.3）：页面完整渲染（列表/上传/
//   配额条内联）；升级墙退役为限额触达（建集超限/行数超限）时的内联升档提示；
//   连接器区可见但标注付费。付费档渲染零变化。
// - 资产目录升级（wire-asset-catalog D5：升级本页、不另建资产页）：列表改经
//   GET /api/v1/assets（域/标签/敏感度/来源类型/新鲜度组合筛选 + 名称描述
//   搜索 + 分页），与 GET /api/v1/datasets 详情按 id 连接补齐卡片既有字段
//   （同步状态/大小等——两查询各自失败互不拖垮）；页内质量徽章按本页 id 批量
//   拉 GET /api/v1/quality/summary（fresh=正常/stale=过期 amber/unknown=未知
//   中性；缺失键不渲染徽章；端点失败静默无徽章——绝不断列表）。
// 契约：openspec/changes/wire-customer-local-data/reports/interfaces.md §3.3/§4、
// wire-asset-catalog / wire-dataset-quality specs。
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  ConsoleApiError,
  createDataset,
  fetchAssetCatalog,
  fetchDatasetQuota,
  fetchDatasets,
  fetchQualitySummary,
  uploadDataset,
  type AssetCatalogFilters,
  type AssetCatalogItem,
  type AssetCatalogPage,
  type AssetSensitivity,
  type Dataset,
  type DatasetQuota,
  type DatasetSourceKind,
  type QualityFreshnessValue,
  type QualitySummaryEntry,
  type QualitySummaryMap,
  type SyncStatus,
  type UploadPreview,
} from "./api";
import { formatServerDate } from "./util";
import { ConnectorsSection } from "./DatasetsConnectors";
import { DatasetDetail } from "./DatasetDetail";
import { PipelineView } from "./pipeline/PipelineView";
import { CONTACT_MAILTO } from "../views/HomeView";

const SYNC_BADGE_CLASS: Record<SyncStatus, string> = {
  idle: "bg-slate-100 text-slate-600 ring-slate-200",
  syncing: "bg-sky-50 text-sky-700 ring-sky-200",
  ok: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
  paused_drift: "bg-amber-50 text-amber-700 ring-amber-200",
  paused_quota: "bg-rose-50 text-rose-700 ring-rose-200",
};

/** 质量徽章三态视觉（stale=过期 warning 调、unknown=未知中性）。 */
const QUALITY_BADGE_CLASS: Record<QualityFreshnessValue, string> = {
  fresh: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  stale: "bg-amber-50 text-amber-700 ring-amber-200",
  unknown: "bg-slate-100 text-slate-600 ring-slate-200",
};

/** 资产目录分页页大小（列表页固定；详情分页另有口径）。 */
const ASSET_PAGE_SIZE = 20;

/** 来源类型筛选项（与 DatasetSourceKind 同词汇，标签复用 source.* i18n）。 */
const SOURCE_KIND_OPTIONS: DatasetSourceKind[] = [
  "upload",
  "connector",
  "declared",
  "derived",
  "indicator",
];

/** 新鲜度筛选项（fresh/stale/unknown——unknown 含未声明节奏的数据集）。 */
const FRESHNESS_OPTIONS: QualityFreshnessValue[] = ["fresh", "stale", "unknown"];

/**
 * 配额语义识别（wire-free-tier-data-gate D3）：api 对建集/上传超限返回
 * 4xx 且 detail 带余量/上限文案（中英关键词容错匹配）；命中即内联升档提示。
 */
export function isQuotaError(err: unknown): boolean {
  if (!(err instanceof ConsoleApiError)) return false;
  if (
    err.status !== 400 &&
    err.status !== 402 &&
    err.status !== 403 &&
    err.status !== 422
  ) {
    return false;
  }
  return /限额|配额|超限|上限|quota|limit/i.test(err.detail ?? "");
}

/** 字节数 → 人类可读（单位换算，非业务数字文案）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

export function DatasetsPage({ planCode }: { planCode: string }) {
  const { t } = useTranslation();
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [error, setError] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [quota, setQuota] = useState<DatasetQuota | null>(null);
  // 子视图切换（wire-transform-pipeline-view 任务 2.2）：列表 / 管线二选一；
  // 详情态（selectedId）优先于两者，返回后回到切换前的子视图。
  const [view, setView] = useState<"list" | "pipeline">("list");

  // ── 资产目录态（wire-asset-catalog 5.1）──
  // catalog=GET /assets 当前页（筛选+搜索+分页的请求面）；datasets=详情连接
  // 用全量索引（卡片同步状态/大小等既有字段经 id 连接补齐）；filters=已提交
  // 筛选（请求依据），draft=文本输入草稿（「筛选」/回车才提交，与查询条同款
  // 交互）；summary=本页数据集的质量徽章汇总（缺失键/失败=无徽章）。
  const [catalog, setCatalog] = useState<AssetCatalogPage | null>(null);
  const [filters, setFilters] = useState<AssetCatalogFilters>({});
  const [draft, setDraft] = useState({ domain: "", tag: "", q: "" });
  const [page, setPage] = useState(1);
  const [summary, setSummary] = useState<QualitySummaryMap | null>(null);

  // 免费档放行（wire-free-tier-data-gate）：不再整页门禁，数据面请求照常发起。
  const free = planCode === "free";

  const load = useCallback(async () => {
    setError(false);
    setSummary(null);
    try {
      // 目录为主源（筛选/搜索/分页在服务端）；详情列表并行拉取做卡片连接，
      // 失败不拖垮目录（卡片字段容错缺省）。
      const [catalogPage, detailList] = await Promise.all([
        fetchAssetCatalog({ ...filters, page, page_size: ASSET_PAGE_SIZE }),
        fetchDatasets().catch(() => null),
      ]);
      setCatalog(catalogPage);
      setDatasets(detailList ?? []);
      // 质量徽章：按本页 id 批量拉汇总；失败/缺失键一律无徽章（静默降级）。
      const ids = catalogPage.items.map((item) => item.id);
      if (ids.length > 0) {
        fetchQualitySummary(ids)
          .then((result) => setSummary(result.statuses))
          .catch(() => setSummary(null));
      }
    } catch {
      setCatalog(null);
      setDatasets([]);
      setError(true);
    }
  }, [filters, page]);

  const loadQuota = useCallback(async () => {
    // 仅免费档取配额；端点未就绪/失败 → 配额条静默隐藏，绝不阻塞页面。
    if (!free) return;
    try {
      setQuota(await fetchDatasetQuota());
    } catch {
      setQuota(null);
    }
  }, [free]);

  useEffect(() => {
    // 管线子视图自取图数据；列表/配额仅在列表视图加载（切回时重取）。
    if (view !== "list") return;
    setDatasets(null);
    setCatalog(null);
    void load();
    void loadQuota();
  }, [view, load, loadQuota]);

  // 执行成功事件刷新（wire-copilot-action-gate 任务 4.1，design D9）：闸
  // 执行成功后 api.ts 广播 window 级 wire:action-executed（resources 按
  // action_id 前缀映射）；「我的数据」命中 "datasets" 即重跑既有列表加载
  // ——只重取数据（filters/page 状态原样参与请求），不重置筛选与滚动。
  useEffect(() => {
    if (view !== "list") return;
    const onExecuted = (event: Event) => {
      const detail = (event as CustomEvent<{ resources?: string[] }>).detail;
      if (detail?.resources?.includes("datasets")) void load();
    };
    window.addEventListener("wire:action-executed", onExecuted);
    return () => window.removeEventListener("wire:action-executed", onExecuted);
  }, [view, load]);

  // ── 筛选交互：下拉即席提交（页码回 1），文本经「筛选」/回车提交 ──
  const filtersActive =
    Boolean(filters.domain) ||
    Boolean(filters.tag) ||
    Boolean(filters.sensitivity) ||
    Boolean(filters.source_kind) ||
    Boolean(filters.freshness) ||
    Boolean(filters.q);

  const applyDraft = () => {
    setFilters((prev) => ({
      ...prev,
      domain: draft.domain.trim() || undefined,
      tag: draft.tag.trim() || undefined,
      q: draft.q.trim() || undefined,
    }));
    setPage(1);
  };

  const resetFilters = () => {
    setDraft({ domain: "", tag: "", q: "" });
    setFilters({});
    setPage(1);
  };

  const setSelectFilter = (
    key: "sensitivity" | "source_kind" | "freshness",
    value: string,
  ) => {
    setFilters((prev) => ({ ...prev, [key]: value || undefined }));
    setPage(1);
  };

  const datasetById = new Map((datasets ?? []).map((d) => [d.id, d]));
  const total = catalog?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / ASSET_PAGE_SIZE));

  if (selectedId != null) {
    return (
      <DatasetDetail
        datasetId={selectedId}
        onBack={() => {
          setSelectedId(null);
          if (view === "list") {
            void load();
            void loadQuota();
          }
        }}
      />
    );
  }

  return (
    <div>
      <h1 className="text-lg font-semibold">{t("console.datasets.title")}</h1>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.subtitle")}
      </p>

      {/* 子视图入口（管线）：分段切换器，活跃项反白，与页内过滤器同风格。 */}
      <div
        data-testid="datasets-subview"
        className="mt-3 inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50/60 p-1"
        role="tablist"
      >
        <button
          type="button"
          role="tab"
          aria-selected={view === "list"}
          data-testid="datasets-subview-list"
          onClick={() => setView("list")}
          className={`rounded-md px-3 py-1 text-xs transition-colors ${
            view === "list"
              ? "bg-slate-900 text-white"
              : "text-slate-600 hover:bg-slate-100"
          }`}
        >
          {t("console.datasets.pipeline.viewList")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "pipeline"}
          data-testid="datasets-subview-pipeline"
          onClick={() => setView("pipeline")}
          className={`rounded-md px-3 py-1 text-xs transition-colors ${
            view === "pipeline"
              ? "bg-slate-900 text-white"
              : "text-slate-600 hover:bg-slate-100"
          }`}
        >
          {t("console.datasets.pipeline.tab")}
        </button>
      </div>

      {view === "pipeline" ? (
        <PipelineView onOpenDataset={(datasetId) => setSelectedId(datasetId)} />
      ) : (
        <>
          {free && quota != null && <QuotaBar quota={quota} />}

          {/* 资产目录筛选栏（wire-asset-catalog 5.1）：域/标签/敏感度/来源
              类型/新鲜度 + 名称描述搜索；下拉即席、文本回车或「筛选」提交、
              「重置」清空——全部联动 GET /api/v1/assets（服务端过滤）。 */}
          <div
            data-testid="assets-filter-bar"
            className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-slate-50/60 p-2 text-xs"
          >
            <input
              data-testid="assets-filter-q"
              value={draft.q}
              onChange={(e) => setDraft((d) => ({ ...d, q: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === "Enter") applyDraft();
              }}
              placeholder={t("console.datasets.assets.searchPlaceholder")}
              aria-label={t("console.datasets.assets.searchPlaceholder")}
              className="w-44 rounded border border-slate-300 px-2 py-1"
            />
            <input
              data-testid="assets-filter-domain"
              value={draft.domain}
              onChange={(e) => setDraft((d) => ({ ...d, domain: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === "Enter") applyDraft();
              }}
              placeholder={t("console.datasets.assets.domainPlaceholder")}
              aria-label={t("console.datasets.assets.domainPlaceholder")}
              className="w-28 rounded border border-slate-300 px-2 py-1"
            />
            <input
              data-testid="assets-filter-tag"
              value={draft.tag}
              onChange={(e) => setDraft((d) => ({ ...d, tag: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === "Enter") applyDraft();
              }}
              placeholder={t("console.datasets.assets.tagPlaceholder")}
              aria-label={t("console.datasets.assets.tagPlaceholder")}
              className="w-24 rounded border border-slate-300 px-2 py-1"
            />
            <select
              data-testid="assets-filter-sensitivity"
              value={filters.sensitivity ?? ""}
              onChange={(e) => setSelectFilter("sensitivity", e.target.value)}
              aria-label={t("console.datasets.assets.sensitivityLabel")}
              className="rounded border border-slate-300 px-1.5 py-1"
            >
              <option value="">
                {t("console.datasets.assets.sensitivityAll")}
              </option>
              {(["常规", "内部", "敏感"] as AssetSensitivity[]).map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <select
              data-testid="assets-filter-source-kind"
              value={filters.source_kind ?? ""}
              onChange={(e) => setSelectFilter("source_kind", e.target.value)}
              aria-label={t("console.datasets.assets.sourceKindLabel")}
              className="rounded border border-slate-300 px-1.5 py-1"
            >
              <option value="">
                {t("console.datasets.assets.sourceKindAll")}
              </option>
              {SOURCE_KIND_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {t(`console.datasets.source.${value}`)}
                </option>
              ))}
            </select>
            <select
              data-testid="assets-filter-freshness"
              value={filters.freshness ?? ""}
              onChange={(e) => setSelectFilter("freshness", e.target.value)}
              aria-label={t("console.datasets.assets.freshnessLabel")}
              className="rounded border border-slate-300 px-1.5 py-1"
            >
              <option value="">
                {t("console.datasets.assets.freshnessAll")}
              </option>
              {FRESHNESS_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {t(`console.datasets.assets.qualityBadge.${value}`)}
                </option>
              ))}
            </select>
            <button
              type="button"
              data-testid="assets-filter-apply"
              onClick={applyDraft}
              className="rounded bg-slate-900 px-2.5 py-1 text-white hover:bg-slate-700"
            >
              {t("console.datasets.assets.apply")}
            </button>
            <button
              type="button"
              data-testid="assets-filter-reset"
              onClick={resetFilters}
              className="rounded border border-slate-300 px-2.5 py-1 text-slate-600 hover:bg-slate-100"
            >
              {t("console.datasets.assets.reset")}
            </button>
          </div>

          {error && (
            <div
              data-testid="datasets-error"
              className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
            >
              <p className="text-sm text-red-700">
                {t("console.datasets.errorTitle")}
              </p>
              <button
                type="button"
                data-testid="datasets-retry"
                onClick={() => void load()}
                className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
              >
                {t("console.datasets.retry")}
              </button>
            </div>
          )}

          {catalog == null && !error && (
            <p
              className="mt-6 text-xs text-slate-400"
              data-testid="datasets-loading"
            >
              {t("console.datasets.loading")}
            </p>
          )}

          {catalog != null && catalog.total === 0 && !error && filtersActive && (
            <div
              data-testid="assets-filtered-empty"
              className="mt-6 rounded-lg border border-dashed border-slate-300 p-8 text-center"
            >
              <p className="text-sm text-slate-600">
                {t("console.datasets.assets.filteredEmpty")}
              </p>
              <button
                type="button"
                data-testid="assets-filtered-reset"
                onClick={resetFilters}
                className="mt-3 rounded border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-50"
              >
                {t("console.datasets.assets.reset")}
              </button>
            </div>
          )}

          {catalog != null && catalog.total === 0 && !error && !filtersActive && (
            <div
              data-testid="datasets-empty"
              className="mt-6 rounded-lg border border-dashed border-slate-300 p-8 text-center"
            >
              <p className="text-sm text-slate-600">
                {t("console.datasets.empty")}
              </p>
              <p className="mt-1 text-xs text-slate-400">
                {t("console.datasets.emptyHint")}
              </p>
            </div>
          )}

          {catalog != null && catalog.items.length > 0 && (
            <>
              <ul data-testid="dataset-list" className="mt-4 space-y-2">
                {catalog.items.map((item) => (
                  <DatasetCard
                    key={item.id}
                    item={item}
                    dataset={datasetById.get(item.id)}
                    freshness={summary?.[item.id]}
                    onOpen={() => setSelectedId(item.id)}
                  />
                ))}
              </ul>
              {total > ASSET_PAGE_SIZE && (
                <div
                  data-testid="assets-pagination"
                  className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-500"
                >
                  <span data-testid="assets-pagination-info">
                    {t("console.datasets.assets.pagination", {
                      total,
                      page,
                      pages,
                    })}
                  </span>
                  <button
                    type="button"
                    data-testid="assets-prev-page"
                    disabled={page <= 1}
                    onClick={() => setPage((current) => Math.max(1, current - 1))}
                    className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {t("console.datasets.assets.prev")}
                  </button>
                  <button
                    type="button"
                    data-testid="assets-next-page"
                    disabled={page >= pages}
                    onClick={() => setPage((current) => current + 1)}
                    className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {t("console.datasets.assets.next")}
                  </button>
                </div>
              )}
            </>
          )}

          <UploadPanel
            planCode={planCode}
            onCreated={() => {
              void load();
              void loadQuota();
            }}
          />

          <ConnectorsSection
            planCode={planCode}
            onCreated={() => void load()}
          />
        </>
      )}
    </div>
  );
}

/**
 * 免费档配额条（wire-free-tier-data-gate D6）：内联呈现「已用 X/N 集、
 * 每集 ≤M 行」；触达上限时转 amber 并附升档引导（非整页墙）。
 * 档位未知降级（wire-console-resilience D4）：max_* 任一为 null/undefined
 * （fail-closed 口径）→ 中性提示行——不渲染数值条/amber 态/升档 CTA
 * （数值未知时 CTA 语义不成立），绝不抛错；有值路径逐字不变。
 */
function QuotaBar({ quota }: { quota: DatasetQuota }) {
  const { t } = useTranslation();
  // max 未知（null）时比较无意义——按「永不触达」占位，走下方 unknown 降级。
  const exhausted =
    quota.max_datasets != null && quota.used_datasets >= quota.max_datasets;
  if (quota.max_datasets == null || quota.max_rows_per_dataset == null) {
    return (
      <div
        data-testid="datasets-quota"
        data-unknown="true"
        className="mt-3 max-w-2xl rounded-md border border-slate-200 bg-slate-50/60 px-3 py-2 text-xs text-slate-600"
      >
        {t("console.datasets.quota.unknown")}
      </div>
    );
  }
  return (
    <div
      data-testid="datasets-quota"
      data-exhausted={exhausted ? "true" : "false"}
      className={`mt-3 max-w-2xl rounded-md border px-3 py-2 text-xs ${
        exhausted
          ? "border-amber-200 bg-amber-50/60 text-amber-700"
          : "border-slate-200 bg-slate-50/60 text-slate-600"
      }`}
    >
      <span data-testid="datasets-quota-usage">
        {t("console.datasets.quota.bar", {
          used: quota.used_datasets,
          max: quota.max_datasets,
          rows: quota.max_rows_per_dataset.toLocaleString("en-US"),
        })}
      </span>
      {exhausted && (
        <>
          <span className="mt-1 block">
            {t("console.datasets.quota.exhausted", { max: quota.max_datasets })}
          </span>
          <a
            data-testid="datasets-quota-upgrade"
            href={CONTACT_MAILTO}
            className="mt-1.5 inline-block rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
          >
            {t("console.datasets.quota.cta")}
          </a>
        </>
      )}
    </div>
  );
}

function DatasetCard({
  item,
  dataset,
  freshness,
  onOpen,
}: {
  /** 目录条目（/assets 响应项——名称/来源/行数/存储层/资产元数据）。 */
  item: AssetCatalogItem;
  /** 详情连接（/datasets 响应按 id 匹配；目录先到时容错缺省——同步状态/
   *  大小等卡片既有字段逐项守卫渲染）。 */
  dataset?: Dataset;
  /** 质量徽章汇总条目（缺失=无徽章）。 */
  freshness?: QualitySummaryEntry;
  onOpen: () => void;
}) {
  const { t } = useTranslation();
  // 存储层徽标（wire-free-tier-data-gate）：api 层与 cell 层数据集同列表，
  // 响应带 storage_tier 才显示（容错——api 侧未下发时不渲染）。
  const storageTier = item.storage_tier;
  const showTierBadge = storageTier === "api" || storageTier === "cell";
  // asset 段容错（wire-console-ux-fixes 任务 1.2）：后端契约切换期个别条目
  // 可能缺 asset 键——全部改安全读法，缺席时元数据行整体不渲染（不白屏）。
  const sensitivity = item.asset?.sensitivity;
  // 有效敏感度（wire-sensitivity-propagation）：徽标按闭包取严口径显示，
  // 继承而来的标注「（继承）」；父集不全时退回自身声明 + 不全警示（如实）。
  const eff = item.asset?.sensitivity_effective;
  const incomplete = item.asset?.sensitivity_incomplete === true;
  const badgeSensitivity = incomplete ? sensitivity : (eff ?? sensitivity);
  const sensitivityInherited = item.asset?.sensitivity_inherited === true && !incomplete;
  // 敏感度默认常规不占位；内部（amber）/敏感（rose）如实标注。
  const showSensitivity = badgeSensitivity === "内部" || badgeSensitivity === "敏感";
  const showAssetMeta =
    Boolean(item.asset?.domain) ||
    (item.asset?.tags?.length ?? 0) > 0 ||
    showSensitivity;
  return (
    <li
      data-testid="dataset-card"
      data-status={dataset?.sync_status}
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          data-testid="dataset-name"
          className="text-sm font-medium text-slate-800"
        >
          {item.name}
        </span>
        <span
          data-testid="dataset-source-badge"
          data-kind={item.source_kind}
          className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
            item.source_kind === "connector"
              ? "bg-indigo-50 text-indigo-700 ring-indigo-200"
              : item.source_kind === "indicator"
                ? "bg-amber-50 text-amber-700 ring-amber-200"
                : "bg-teal-50 text-teal-700 ring-teal-200"
          }`}
        >
          {t(`console.datasets.source.${item.source_kind}`)}
        </span>
        {dataset && (
          <span
            data-testid="dataset-sync-status"
            data-status={dataset.sync_status}
            className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
              SYNC_BADGE_CLASS[dataset.sync_status] ??
              "bg-slate-100 text-slate-600 ring-slate-200"
            }`}
          >
            {t(`console.datasets.syncStatus.${dataset.sync_status}`)}
          </span>
        )}
        {/* 受限徽标（wire-dataset-acl）：acl_mode=restricted 才渲染；缺席 =
            默认 team（与后端默认值同口径，绝不猜）。 */}
        {dataset?.acl_mode === "restricted" && (
          <span
            data-testid="dataset-acl-badge"
            data-acl-mode="restricted"
            className="rounded-full bg-rose-50 px-2 py-0.5 text-xs text-rose-700 ring-1 ring-rose-200"
          >
            {t("console.datasets.aclBadge.restricted")}
          </span>
        )}
        {/* 质量徽章（wire-dataset-quality）：fresh=正常 / stale=过期（warning）
            / unknown=未知（中性）；summary 缺失键不渲染。 */}
        {freshness && (
          <span
            data-testid="dataset-quality-badge"
            data-freshness={freshness.freshness}
            className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
              QUALITY_BADGE_CLASS[freshness.freshness] ??
              "bg-slate-100 text-slate-600 ring-slate-200"
            }`}
          >
            {t(`console.datasets.assets.qualityBadge.${freshness.freshness}`)}
          </span>
        )}
        {showTierBadge && (
          <span
            data-testid="dataset-tier-badge"
            data-tier={storageTier}
            className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
          >
            {t(`console.datasets.tierBadge.${storageTier}`)}
          </span>
        )}
        {item.source_kind === "connector" &&
          (dataset?.source_name || dataset?.source_ref) && (
            <span
              data-testid="dataset-source-ref"
              className="font-mono text-[11px] text-slate-400"
            >
              {[dataset.source_name, dataset.source_ref]
                .filter(Boolean)
                .join(" · ")}
            </span>
          )}
      </div>
      {/* 资产元数据行（wire-asset-catalog）：业务域 / 标签 / 敏感度标注。 */}
      {showAssetMeta && (
        <div
          data-testid="dataset-asset-meta"
          className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px]"
        >
          {item.asset?.domain && (
            <span
              data-testid="dataset-asset-domain"
              className="rounded bg-sky-50 px-1.5 py-0.5 text-sky-700"
            >
              {item.asset?.domain}
            </span>
          )}
          {(item.asset?.tags ?? []).map((tag) => (
            <span
              key={tag}
              data-testid="dataset-asset-tag"
              className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600"
            >
              #{tag}
            </span>
          ))}
          {showSensitivity && (
            <span
              data-testid="dataset-sensitivity-badge"
              data-sensitivity={badgeSensitivity}
              data-inherited={sensitivityInherited || undefined}
              className={`rounded px-1.5 py-0.5 ${
                badgeSensitivity === "敏感"
                  ? "bg-rose-50 text-rose-700"
                  : "bg-amber-50 text-amber-700"
              }`}
            >
              {badgeSensitivity}
              {sensitivityInherited &&
                t("console.datasets.assets.sensitivityInherited")}
            </span>
          )}
          {incomplete && (
            <span
              data-testid="dataset-sensitivity-incomplete"
              className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-500"
              title={t("console.datasets.assets.sensitivityIncompleteHint")}
            >
              {t("console.datasets.assets.sensitivityIncomplete")}
            </span>
          )}
        </div>
      )}
      {dataset?.paused_reason && (
        <p
          data-testid="dataset-paused-reason"
          className="mt-1.5 text-[11px] text-amber-700"
        >
          {t("console.datasets.pausedReason", { reason: dataset.paused_reason })}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span data-testid="dataset-rows">
          {t("console.datasets.rowCount", { count: item.row_count })}
        </span>
        {dataset && (
          <>
            <span>·</span>
            <span data-testid="dataset-size">
              {t("console.datasets.sizeLabel")} {formatBytes(dataset.size_bytes)}
            </span>
            <span>·</span>
            <span data-testid="dataset-updated">
              {t("console.datasets.updatedLabel")}{" "}
              {formatServerDate(dataset.updated_at)}
            </span>
          </>
        )}
        <button
          type="button"
          data-testid="dataset-open"
          onClick={onOpen}
          className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
        >
          {t("console.datasets.openDetail")}
        </button>
      </div>
    </li>
  );
}

function UploadPanel({
  planCode,
  onCreated,
}: {
  /** 客户档位（wire-free-tier-data-gate 4.3）：free=超限内联升档提示。 */
  planCode: string;
  onCreated: () => void;
}) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [parsing, setParsing] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<{ title: string; detail: string } | null>(null);
  const [created, setCreated] = useState(false);
  // 配额触达标记：上传/建档被 api 按限额拒绝时叠加内联升档引导（amber）。
  const [quotaHit, setQuotaHit] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const free = planCode === "free";

  const handleFile = async (file: File | null | undefined) => {
    if (!file || parsing) return;
    setParsing(true);
    setError(null);
    setPreview(null);
    setCreated(false);
    setQuotaHit(false);
    try {
      const result = await uploadDataset(file);
      setPreview(result);
      setName(file.name.replace(/\.[^.]+$/, ""));
    } catch (err) {
      setError({
        title: t("console.datasets.upload.errorTitle"),
        detail: apiErrorText(err, t("console.datasets.upload.errorTitle")),
      });
      if (free && isQuotaError(err)) setQuotaHit(true);
    } finally {
      setParsing(false);
    }
  };

  const create = async () => {
    if (!preview || !name.trim() || creating) return;
    setCreating(true);
    setError(null);
    setQuotaHit(false);
    try {
      await createDataset({
        name: name.trim(),
        source: { kind: "upload", upload_id: preview.upload_id },
      });
      setPreview(null);
      setName("");
      setCreated(true);
      onCreated();
    } catch (err) {
      setError({
        title: t("console.datasets.upload.createErrorTitle"),
        detail: apiErrorText(err, t("console.datasets.upload.createErrorTitle")),
      });
      if (free && isQuotaError(err)) setQuotaHit(true);
    } finally {
      setCreating(false);
    }
  };

  return (
    <section data-testid="upload-section" className="mt-8">
      <h2 className="text-base font-semibold">{t("console.datasets.upload.title")}</h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.upload.hint")}
      </p>

      <div
        data-testid="upload-dropzone"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          void handleFile(e.dataTransfer?.files?.[0]);
        }}
        className="mt-3 flex flex-wrap items-center gap-3 rounded-lg border border-dashed border-slate-300 bg-slate-50/60 px-4 py-4"
      >
        <span className="text-xs text-slate-500">
          {t("console.datasets.upload.dropzone")}
        </span>
        <button
          type="button"
          data-testid="upload-choose"
          onClick={() => fileInputRef.current?.click()}
          className="rounded-md border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
        >
          {t("console.datasets.upload.choose")}
        </button>
        {parsing && (
          <span data-testid="upload-parsing" className="text-xs text-slate-400">
            {t("console.datasets.upload.parsing")}
          </span>
        )}
        <input
          ref={fileInputRef}
          data-testid="upload-file-input"
          type="file"
          accept=".csv,.xlsx,.xls"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0] ?? null;
            e.target.value = "";
            void handleFile(file);
          }}
        />
      </div>

      {error && (
        <div
          data-testid="upload-error"
          className="mt-3 rounded-md border border-red-200 bg-red-50 p-3"
        >
          <p className="text-sm text-red-700">{error.title}</p>
          <p className="mt-1 break-all font-mono text-[11px] text-red-500">
            {error.detail}
          </p>
        </div>
      )}

      {free && quotaHit && (
        <div
          data-testid="upload-quota-hint"
          className="mt-3 rounded-md border border-amber-200 bg-amber-50/60 p-3"
        >
          <p className="text-sm text-amber-700">
            {t("console.datasets.quota.hitTitle")}
          </p>
          <p className="mt-1 max-w-2xl text-xs text-amber-700">
            {t("console.datasets.quota.hintBody")}
          </p>
          <a
            data-testid="upload-quota-cta"
            href={CONTACT_MAILTO}
            className="mt-2 inline-block rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
          >
            {t("console.datasets.quota.cta")}
          </a>
        </div>
      )}

      {created && !preview && (
        <p data-testid="upload-created" className="mt-3 text-xs text-emerald-600">
          {t("console.datasets.upload.created")}
        </p>
      )}

      {preview && (
        <div
          data-testid="upload-preview"
          className="mt-3 rounded-lg border border-slate-200 bg-white p-4"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">
              {t("console.datasets.upload.previewTitle")}
            </h3>
            <span
              data-testid="upload-preview-rows"
              className="text-xs text-slate-500"
            >
              {t("console.datasets.upload.previewRows", {
                count: preview.row_count,
              })}
            </span>
          </div>

          {!preview.within_limit && (
            <p
              data-testid="upload-over-limit"
              className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
            >
              {t("console.datasets.upload.overLimit")}
            </p>
          )}

          <ul data-testid="upload-preview-columns" className="mt-3 flex flex-wrap gap-2">
            {preview.columns.map((column) => (
              <li
                key={column.name}
                data-testid="upload-preview-column"
                data-column={column.name}
                data-type={column.type}
                className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] text-slate-600"
              >
                <span className="font-medium">{column.name}</span>
                <span className="ml-1.5 font-mono text-slate-400">
                  {column.type}
                </span>
              </li>
            ))}
          </ul>

          {preview.sample_rows.length > 0 && (
            <div className="mt-3 overflow-x-auto">
              <table data-testid="upload-sample" className="text-xs">
                <thead>
                  <tr>
                    {preview.columns.map((column) => (
                      <th
                        key={column.name}
                        className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-500"
                      >
                        {column.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.sample_rows.map((row, index) => (
                    <tr key={index} data-testid="upload-sample-row">
                      {preview.columns.map((column) => (
                        <td
                          key={column.name}
                          className="border-b border-slate-100 px-2 py-1 text-slate-700"
                        >
                          {row[column.name] == null ? "" : String(row[column.name])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-3 flex flex-wrap items-end gap-3">
            <div>
              <label
                htmlFor="upload-name"
                className="block text-xs text-slate-500"
              >
                {t("console.datasets.upload.nameLabel")}
              </label>
              <input
                id="upload-name"
                data-testid="upload-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t("console.datasets.upload.namePlaceholder")}
                className="mt-1 w-56 rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </div>
            <button
              type="button"
              data-testid="upload-create"
              disabled={!name.trim() || !preview.within_limit || creating}
              onClick={() => void create()}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {creating
                ? t("console.datasets.upload.creating")
                : t("console.datasets.upload.create")}
            </button>
            <button
              type="button"
              data-testid="upload-cancel"
              onClick={() => {
                setPreview(null);
                setName("");
                setError(null);
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100"
            >
              {t("console.datasets.upload.cancel")}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}