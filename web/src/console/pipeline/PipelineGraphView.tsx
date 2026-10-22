// 数据页「管线」子视图之「图谱」页签（wire-transform-pipeline-view 任务
// 2.1/2.2 起，wire-transform-pipelines 5.1 拆出为独立页签组件）：
// 全企业数据集派生血缘图，GET /api/v1/pipeline/graph 一次拉全量。
// - 分层布局：按拓扑深度分列（depth 0 源最左，派生逐级向右；列内按 name 排序）；
// - 节点卡：名称 / 行数 / 更新时间（本地化短格式）/ tier 徽标（api=免费档、
//   cell）/ 调度徽标（schedule_cron 非空）/ kind 标注（derived=派生、
//   upload=上传、其余如实）；点击跳数据集详情（onOpenDataset 回调，
//   与 DatasetsPage 列表卡同一导航方式）；
// - SVG 连线父→子：列间三次贝塞尔曲线（测量卡片实际位置绘制，不强求
//   避让算法）；窄屏（<768px）不画 SVG，退化为按深度分组的卡片列表；
// - 如实呈现：加载失败错误态 + 重试；edges 为空显示空态引导（源数据集
//   照常列出）；truncated 截断提示；anomalies>0 异常边提示。
// v0 只读——不提供刷新/派生按钮（操作入口在数据集详情页）。
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  getPipelineGraph,
  type PipelineGraph,
  type PipelineGraphNode,
} from "../api";

/** 窄屏断点：<768px 退化为分组卡片列表（v0 不做响应式画布）。 */
const NARROW_QUERY = "(max-width: 767px)";

/** kind 标注复用列表卡来源徽标文案（其他来源如实显示原值）。 */
const KNOWN_SOURCE_KINDS = ["upload", "connector", "declared", "derived"];

/**
 * 后端时间（naive UTC）→ 本地化短格式（如 2026/10/5 18:00）。
 * 与 util.formatServerDate 同解析规则（无时区标记按 UTC），仅呈现更短；
 * 空值（api 侧缺 updated_at 的容错分支）如实回「—」。
 */
export function formatShortServerDate(naiveIso: string | null | undefined): string {
  if (typeof naiveIso !== "string" || !naiveIso) return "—";
  const normalized = /[Zz]|[+-]\d{2}:?\d{2}$/.test(naiveIso)
    ? naiveIso
    : `${naiveIso}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return naiveIso;
  return date.toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "short",
  });
}

/** 深度分列：depth 升序，列内按 name 排序。 */
export function columnsOfGraph(
  nodes: PipelineGraphNode[],
): Array<[number, PipelineGraphNode[]]> {
  const byDepth = new Map<number, PipelineGraphNode[]>();
  for (const node of nodes) {
    const list = byDepth.get(node.depth) ?? [];
    list.push(node);
    byDepth.set(node.depth, list);
  }
  return [...byDepth.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(
      ([depth, list]) =>
        [depth, [...list].sort((a, b) => a.name.localeCompare(b.name))] as [
          number,
          PipelineGraphNode[],
        ],
    );
}

/** 窄屏判定：matchMedia 不可用时按宽屏渲染（绝不整页崩）。 */
function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState<boolean>(
    () =>
      typeof window.matchMedia === "function"
        ? window.matchMedia(NARROW_QUERY).matches
        : false,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(NARROW_QUERY);
    const listener = (event: MediaQueryListEvent) => setNarrow(event.matches);
    if (typeof query.addEventListener !== "function") return;
    query.addEventListener("change", listener);
    return () => query.removeEventListener("change", listener);
  }, []);
  return narrow;
}

/** 计算后的边路径（含起止 dataset_id，供测试与 data-* 断言）。 */
interface EdgePath {
  from: string;
  to: string;
  op: string;
  d: string;
}

export function PipelineGraphView({
  onOpenDataset,
}: {
  /** 点击节点卡跳数据集详情（DatasetsPage 以 setSelectedId 接线）。 */
  onOpenDataset: (datasetId: string) => void;
}) {
  const { t } = useTranslation();
  const [graph, setGraph] = useState<PipelineGraph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const narrow = useNarrowViewport();

  const load = useCallback(async () => {
    setError(null);
    try {
      setGraph(await getPipelineGraph());
    } catch (err) {
      setGraph(null);
      setError(apiErrorText(err, t("console.datasets.pipeline.errorTitle")));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns = useMemo(
    () => (graph ? columnsOfGraph(graph.nodes) : []),
    [graph],
  );

  // ── 宽屏 SVG 连线：测量卡片实际位置，父右缘 → 子左缘三次贝塞尔 ──
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef(new Map<string, HTMLElement>());
  const [edgePaths, setEdgePaths] = useState<EdgePath[]>([]);
  const [svgSize, setSvgSize] = useState({ width: 0, height: 0 });

  const measureEdges = useCallback(() => {
    const container = containerRef.current;
    if (!container || !graph) return;
    const base = container.getBoundingClientRect();
    const paths: EdgePath[] = [];
    for (const edge of graph.edges) {
      const from = cardRefs.current.get(edge.from_dataset_id);
      const to = cardRefs.current.get(edge.to_dataset_id);
      if (!from || !to) continue;
      const a = from.getBoundingClientRect();
      const b = to.getBoundingClientRect();
      const x1 = a.right - base.left;
      const y1 = a.top + a.height / 2 - base.top;
      const x2 = b.left - base.left;
      const y2 = b.top + b.height / 2 - base.top;
      const dx = Math.max(24, Math.abs(x2 - x1) / 2);
      paths.push({
        from: edge.from_dataset_id,
        to: edge.to_dataset_id,
        op: edge.op,
        d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
      });
    }
    setEdgePaths(paths);
    setSvgSize({ width: container.scrollWidth, height: container.scrollHeight });
  }, [graph]);

  useLayoutEffect(() => {
    if (narrow) {
      setEdgePaths([]);
      return;
    }
    measureEdges();
    window.addEventListener("resize", measureEdges);
    return () => window.removeEventListener("resize", measureEdges);
  }, [narrow, measureEdges]);

  if (error != null) {
    return (
      <div
        data-testid="pipeline-error"
        className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
      >
        <p className="text-sm text-red-700">
          {t("console.datasets.pipeline.errorTitle")}
        </p>
        {error !== t("console.datasets.pipeline.errorTitle") && (
          <p className="mt-1 break-all font-mono text-[11px] text-red-500">
            {error}
          </p>
        )}
        <button
          type="button"
          data-testid="pipeline-retry"
          onClick={() => void load()}
          className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
        >
          {t("console.datasets.pipeline.retry")}
        </button>
      </div>
    );
  }

  if (graph == null) {
    return (
      <p className="mt-6 text-xs text-slate-400" data-testid="pipeline-loading">
        {t("console.datasets.pipeline.loading")}
      </p>
    );
  }

  return (
    <section data-testid="pipeline-graph-view" className="mt-2">
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.pipeline.subtitle")}
      </p>

      {/* 如实提示：无派生 / 截断 / 异常边（行内条，不阻塞节点呈现） */}
      {graph.edges.length === 0 && (
        <p
          data-testid="pipeline-empty-hint"
          className="mt-3 max-w-2xl rounded-md border border-dashed border-slate-300 bg-slate-50/60 px-3 py-2 text-xs text-slate-600"
        >
          {t("console.datasets.pipeline.emptyHint")}
        </p>
      )}
      {graph.truncated && (
        <p
          data-testid="pipeline-truncated"
          className="mt-3 max-w-2xl rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-xs text-amber-700"
        >
          {t("console.datasets.pipeline.truncated")}
        </p>
      )}
      {graph.anomalies > 0 && (
        <p
          data-testid="pipeline-anomalies"
          className="mt-3 max-w-2xl rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-xs text-amber-700"
        >
          {t("console.datasets.pipeline.anomalies", { count: graph.anomalies })}
        </p>
      )}

      {narrow ? (
        // 窄屏退化：按深度分组的卡片列表（无 SVG 画布）。
        <div data-testid="pipeline-list" className="mt-4 space-y-5">
          {columns.map(([depth, nodes]) => (
            <section
              key={depth}
              data-testid="pipeline-depth-group"
              data-depth={depth}
            >
              <p
                data-testid="pipeline-depth-label"
                className="text-[11px] font-medium uppercase tracking-wide text-slate-400"
              >
                {t("console.datasets.pipeline.columnLabel", { depth })}
              </p>
              <div className="mt-2 flex flex-col gap-2">
                {nodes.map((node) => (
                  <PipelineNodeCard
                    key={node.dataset_id}
                    node={node}
                    onOpen={onOpenDataset}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        // 宽屏：深度分列 + SVG 父→子连线（测量实际卡片位置绘制）。
        <div
          data-testid="pipeline-graph"
          ref={containerRef}
          className="relative mt-4 overflow-x-auto"
        >
          <div className="flex items-start gap-10 p-2">
            {columns.map(([depth, nodes]) => (
              <div
                key={depth}
                data-testid="pipeline-column"
                data-depth={depth}
                className="flex w-60 shrink-0 flex-col gap-3"
              >
                <p
                  data-testid="pipeline-column-label"
                  className="text-[11px] font-medium uppercase tracking-wide text-slate-400"
                >
                  {t("console.datasets.pipeline.columnLabel", { depth })}
                </p>
                {nodes.map((node) => (
                  <PipelineNodeCard
                    key={node.dataset_id}
                    node={node}
                    onOpen={onOpenDataset}
                    attachRef={(el) => {
                      if (el) cardRefs.current.set(node.dataset_id, el);
                      else cardRefs.current.delete(node.dataset_id);
                    }}
                  />
                ))}
              </div>
            ))}
          </div>
          <svg
            data-testid="pipeline-edges"
            className="pointer-events-none absolute inset-0"
            width={svgSize.width}
            height={svgSize.height}
            aria-hidden="true"
          >
            {edgePaths.map((path) => (
              <path
                key={`${path.from}->${path.to}`}
                data-testid="pipeline-edge"
                data-from={path.from}
                data-to={path.to}
                data-op={path.op}
                d={path.d}
                fill="none"
                strokeWidth="1.5"
                className="stroke-slate-300"
              >
                <title>{`${path.from} → ${path.to} (${path.op})`}</title>
              </path>
            ))}
          </svg>
        </div>
      )}
    </section>
  );
}

function PipelineNodeCard({
  node,
  onOpen,
  attachRef,
}: {
  node: PipelineGraphNode;
  onOpen: (datasetId: string) => void;
  /** 宽屏布局接线卡片 DOM（连线测量用）；窄屏列表不传。 */
  attachRef?: (el: HTMLButtonElement | null) => void;
}) {
  const { t } = useTranslation();
  const tier =
    node.storage_tier === "api" || node.storage_tier === "cell"
      ? node.storage_tier
      : null;
  const kindLabel = KNOWN_SOURCE_KINDS.includes(node.source_kind)
    ? t(`console.datasets.source.${node.source_kind}`)
    : node.source_kind;
  return (
    <button
      type="button"
      ref={attachRef}
      data-testid="pipeline-node"
      data-dataset-id={node.dataset_id}
      data-depth={node.depth}
      data-kind={node.source_kind}
      onClick={() => onOpen(node.dataset_id)}
      className="w-full rounded-lg border border-slate-200 bg-white p-3 text-left transition-colors hover:border-slate-400"
    >
      <span
        data-testid="pipeline-node-name"
        className="block truncate text-sm font-medium text-slate-800"
        title={node.name}
      >
        {node.name}
      </span>
      <span className="mt-1.5 flex flex-wrap gap-1">
        <span
          data-testid="pipeline-node-kind"
          data-kind={node.source_kind}
          className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
            node.source_kind === "derived"
              ? "bg-teal-50 text-teal-700 ring-teal-200"
              : "bg-slate-100 text-slate-600 ring-slate-200"
          }`}
        >
          {kindLabel}
        </span>
        {tier && (
          <span
            data-testid="pipeline-node-tier"
            data-tier={tier}
            className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
          >
            {t(`console.datasets.pipeline.tier.${tier}`)}
          </span>
        )}
        {node.schedule_cron && (
          <span
            data-testid="pipeline-node-scheduled"
            title={node.schedule_cron}
            className="rounded-full bg-sky-50 px-2 py-0.5 text-xs text-sky-700 ring-1 ring-sky-200"
          >
            {t("console.datasets.pipeline.scheduled")}
          </span>
        )}
      </span>
      <span className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-slate-500">
        <span data-testid="pipeline-node-rows">
          {t("console.datasets.rowCount", { count: node.row_count })}
        </span>
        <span>·</span>
        <span data-testid="pipeline-node-updated">
          {t("console.datasets.pipeline.updatedLabel")}{" "}
          {formatShortServerDate(node.updated_at)}
        </span>
      </span>
    </button>
  );
}
