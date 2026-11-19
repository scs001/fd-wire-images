// 仪表盘快照渲染器（wire-dashboard-renderer 任务 2；wire-chart-contract-v2
// 任务 2.1–2.7 与 3.1–3.4 扩展）。
// - 3 列栅格（span 1/2/3）；kpi 大数卡、图表走 echarts、table 简表（超 20 行
//   内部滚动）。图表的 option 构建全部委托 chartOption.ts（纯函数层）——
//   本文件只管栅格、容器、生命周期与工具栏。
// - v2：类型谱（candlestick/scatter/pie/heatmap/gauge/radar/treemap/sankey/
//   funnel/boxplot）、编码通道与轴类型、多窗格副图、叠加层、声明高度、
//   raw_option 直通。无 v2 字段的面板走 v1 回退分支，渲染结果零漂移。
// - 工具栏（toolbar prop，默认关闭——分享页红线靠默认值天然守住）：全屏 /
//   导出图片 / 图数切换 / 复制数据。工具栏是纯只读能力，不改 spec、不发请求。
// - 容错：非法面板单卡标注跳过（不整板失败）；空 snapshot 如实显「无数据」；
//   旧自由 JSON（无 panels）整体显「旧版自由格式」+ 只读 JSON 查看。
// - 底部小字标注快照口径（快照数据，实时刷新后续开通）。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import * as echarts from "echarts";
import {
  parseDashboardSpec,
  type DashboardPanel,
  type DashboardPanelSpan,
} from "./spec";
import { buildPanelOption } from "./chartOption";

/** 栅格跨度 → Tailwind 列宽类（静态映射：Tailwind JIT 不识别拼接类名）。 */
const SPAN_CLASS: Record<DashboardPanelSpan, string> = {
  1: "md:col-span-1",
  2: "md:col-span-2",
  3: "md:col-span-3",
};

/** table 面板超此行数启用内部滚动。 */
const TABLE_SCROLL_THRESHOLD = 20;

export interface DashboardRendererProps {
  spec: unknown;
  /**
   * 面板工具栏（全屏/导出图片/图数切换/复制数据）。**默认关闭**——
   * 公开分享页与市场渲染不传即无任何工具栏入口（公开只读红线）。
   */
  toolbar?: boolean;
}

/**
 * 渲染仪表盘产物 spec（信封或本体均可——见 parseDashboardSpec）。
 * 编辑器的实时预览复用本组件。
 */
export function DashboardRenderer({ spec, toolbar = false }: DashboardRendererProps) {
  const { t } = useTranslation();
  const parsed = useMemo(() => parseDashboardSpec(spec), [spec]);

  if (parsed.kind === "legacy") {
    return (
      <div data-testid="dashboard-legacy" className="rounded-md border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
            {t("console.dashboard.legacy.badge")}
          </span>
          <span className="text-xs text-slate-500">
            {t("console.dashboard.legacy.note")}
          </span>
        </div>
        <pre
          data-testid="dashboard-legacy-json"
          className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-3 font-mono text-[11px] text-slate-700"
        >
          {JSON.stringify(parsed.raw, null, 2)}
        </pre>
      </div>
    );
  }

  return (
    <div data-testid="dashboard-renderer" data-toolbar={toolbar ? "on" : "off"}>
      {parsed.title && (
        <h2 data-testid="dashboard-title" className="text-sm font-semibold text-slate-800">
          {parsed.title}
        </h2>
      )}
      {parsed.panels.length === 0 && parsed.badPanels.length === 0 && (
        <p data-testid="dashboard-empty-panels" className="mt-2 text-xs text-slate-400">
          {t("console.dashboard.emptyPanels")}
        </p>
      )}
      {(parsed.panels.length > 0 || parsed.badPanels.length > 0) && (
        <div className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-3">
          {parsed.panels.map((panel) => (
            <div
              key={panel.id}
              data-testid="dashboard-panel"
              data-panel-id={panel.id}
              data-panel-type={panel.type}
              className={`rounded-lg border border-slate-200 bg-white p-4 ${SPAN_CLASS[panel.span]}`}
            >
              <PanelCard panel={panel} toolbar={toolbar} />
            </div>
          ))}
          {parsed.badPanels.map((bad) => (
            <div
              key={`bad-${bad.index}`}
              data-testid="dashboard-bad-panel"
              data-bad-reason={bad.reason}
              data-index={bad.index}
              className="rounded-lg border border-dashed border-amber-300 bg-amber-50/50 p-4 md:col-span-1"
            >
              <p className="text-xs text-amber-700">
                {t("console.dashboard.badPanel", { index: bad.index + 1 })}
                {t(`console.dashboard.badPanelReason.${bad.reason}`)}
              </p>
            </div>
          ))}
        </div>
      )}
      <p data-testid="dashboard-snapshot-note" className="mt-2 text-[11px] text-slate-400">
        {t("console.dashboard.snapshotNote")}
      </p>
    </div>
  );
}

/** 单个面板：标题 + 可选来源标注 + 类型化主体（+ 可选工具栏）。 */
function PanelCard({ panel, toolbar }: { panel: DashboardPanel; toolbar: boolean }) {
  const { t } = useTranslation();
  // 图数切换只对图表面板有意义（kpi 无表格语义，table 本来就是表）。
  const isChart = panel.type !== "kpi" && panel.type !== "table" && panel.type !== "list";
  const [showTable, setShowTable] = useState(false);

  const body =
    panel.type === "kpi" ? (
      <KpiBody panel={panel} />
    ) : panel.type === "table" ? (
      <TableBody panel={panel} />
    ) : panel.type === "list" ? (
      <ListBody panel={panel} />
    ) : (
      <ChartBody panel={panel} showTable={showTable} />
    );

  return (
    <section className="flex h-full flex-col" data-testid="dashboard-panel-card">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 data-testid="dashboard-panel-title" className="text-sm font-medium text-slate-700">
          {panel.title || t(`console.dashboard.type.${panel.type}`)}
        </h3>
        {panel.source && (
          <span
            data-testid="dashboard-panel-source"
            className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
          >
            {panel.source}
          </span>
        )}
        {panel.raw_option !== undefined && (
          <span
            data-testid="dashboard-panel-raw-badge"
            className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700"
          >
            {t("console.dashboard.rawOptionBadge")}
          </span>
        )}
        {toolbar && isChart && (
          <PanelToolbar
            panel={panel}
            showTable={showTable}
            onToggleTable={() => setShowTable((v) => !v)}
          />
        )}
      </div>
      <div className="mt-2 flex-1">{body}</div>
    </section>
  );
}

/** 无数据占位（空 snapshot / 无可画系列的图表如实呈现）。 */
function NoData() {
  const { t } = useTranslation();
  return (
    <div
      data-testid="dashboard-no-data"
      className="flex h-24 items-center justify-center rounded border border-dashed border-slate-200 text-xs text-slate-400"
    >
      {t("console.dashboard.noData")}
    </div>
  );
}

/** kpi 大数卡：值取 snapshot.rows[0][0]，配 config.unit / config.subtitle。 */
function KpiBody({ panel }: { panel: DashboardPanel }) {
  const value = panel.snapshot?.rows?.[0]?.[0];
  const unit = panel.config?.unit ?? "";
  const subtitle = panel.config?.subtitle ?? "";
  if (value === undefined || value === null) return <NoData />;
  return (
    <div data-testid="dashboard-kpi" className="flex flex-col justify-center py-2">
      <p className="flex items-baseline gap-1">
        <span data-testid="dashboard-kpi-value" className="text-3xl font-semibold tabular-nums text-slate-900">
          {value}
        </span>
        {unit && (
          <span data-testid="dashboard-kpi-unit" className="text-sm text-slate-500">
            {unit}
          </span>
        )}
      </p>
      {subtitle && (
        <span data-testid="dashboard-kpi-subtitle" className="mt-1 text-xs text-slate-400">
          {subtitle}
        </span>
      )}
    </div>
  );
}

/** 图表面板主体：option 由 chartOption.ts 构建；showTable 时换表格视图。 */
function ChartBody({
  panel,
  showTable,
}: {
  panel: DashboardPanel;
  showTable: boolean;
}) {
  const { t } = useTranslation();
  const built = useMemo(() => buildPanelOption(panel), [panel]);

  if (built === null) return <NoData />;

  return (
    <div data-testid="dashboard-chart-wrap">
      {showTable ? (
        <ChartDataTable panel={panel} />
      ) : (
        <EChartBox option={built.option} height={built.height} panel={panel} />
      )}
      {built.panesDegraded && (
        <p data-testid="dashboard-panes-degraded" className="mt-1 text-[11px] text-amber-600">
          {t("console.dashboard.panesDegraded")}
        </p>
      )}
      <span className="sr-only" data-testid="dashboard-chart-label">
        {t(`console.dashboard.type.${panel.type}`)}
      </span>
    </div>
  );
}

/** echarts 绑定：init/dispose 生命周期 + option 变化重画 + 尺寸自适应。
 *  渲染器固定 SVG（测试环境无 canvas 也能跑；视觉与 canvas 无差别口径）。
 *  容器高度由 props 决定（v2 可声明高度；缺省 DEFAULT_PANEL_HEIGHT）。
 *  全屏通过 scale 覆盖层实现：不移动 DOM，图表实例原地 resize。 */
function EChartBox({
  option,
  height,
  panel,
}: {
  option: echarts.EChartsOption;
  height: number;
  panel: DashboardPanel;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const [fullscreen, setFullscreen] = useState(false);

  // 全屏状态由渲染器外部工具栏驱动（见 PanelToolbar 的 window 事件）。
  useEffect(() => {
    const onToggle = (event: Event) => {
      const detail = (event as CustomEvent<{ panelId: string }>).detail;
      if (detail?.panelId === panel.id) setFullscreen((v) => !v);
    };
    window.addEventListener("wire-dashboard-fullscreen", onToggle);
    return () => window.removeEventListener("wire-dashboard-fullscreen", onToggle);
  }, [panel.id]);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, undefined, {
      renderer: "svg",
    });
    chartRef.current = chart;
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    // 容器尺寸变化（全屏切换/侧栏折叠）也要重绘——window.resize 覆盖不到。
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => chart.resize());
    observer?.observe(containerRef.current);
    return () => {
      window.removeEventListener("resize", onResize);
      observer?.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    // notMerge=true：面板数据整体替换（残留旧系列比清空更糟）。
    chartRef.current?.setOption(option, true);
  }, [option]);

  // 全屏切换后容器尺寸变了，显式 resize 一次（ResizeObserver 已兜底，
  // 但同步调用能让首帧就不闪烁）。
  useEffect(() => {
    chartRef.current?.resize();
  }, [fullscreen]);

  const style = fullscreen
    ? { height: "calc(100vh - 96px)", position: "fixed" as const, inset: "48px 24px 24px", zIndex: 50, background: "#fff", padding: "12px", borderRadius: "8px", boxShadow: "0 8px 32px rgba(15,23,42,.18)" }
    : { height };

  return (
    <div
      ref={containerRef}
      data-testid="dashboard-chart"
      data-fullscreen={fullscreen ? "on" : "off"}
      style={style}
    />
  );
}

/** 图表面板的数据表视图（与图形同源快照；raw_option 面板按系列抽取）。 */
function ChartDataTable({ panel }: { panel: DashboardPanel }) {
  const { t } = useTranslation();
  const columns = panel.snapshot?.columns ?? [];
  const rows = panel.snapshot?.rows ?? [];

  // 快照有数据 → 直接按快照列呈现（与图形同源）。
  if (columns.length > 0 && rows.length > 0) {
    return <SnapshotTable columns={columns} rows={rows} />;
  }

  // raw_option 面板：按系列名抽 x/值两列；抽不出如实标注。
  const extracted = extractRawSeries(panel);
  if (extracted === null) {
    return (
      <div
        data-testid="dashboard-raw-not-tabular"
        className="flex h-24 items-center justify-center rounded border border-dashed border-slate-200 text-xs text-slate-400"
      >
        {t("console.dashboard.rawNotTabular")}
      </div>
    );
  }
  return <SnapshotTable columns={extracted.columns} rows={extracted.rows} />;
}

/**
 * 从 raw_option 的 series 抽取表格数据：每个系列一列，x 轴类目作首列。
 * 取不到（无 series / 系列无 data / 数据不是数组）返回 null。
 */
export function extractRawSeries(
  panel: DashboardPanel,
): { columns: string[]; rows: Array<Array<string | number | null>> } | null {
  const raw = panel.raw_option;
  if (raw === undefined) return null;
  const series = raw.series;
  if (!Array.isArray(series) || series.length === 0) return null;

  const seriesList = series.filter(
    (entry): entry is Record<string, unknown> =>
      typeof entry === "object" && entry !== null && !Array.isArray(entry),
  );
  if (seriesList.length === 0) return null;

  const columns: string[] = [];
  const valueColumns: Array<Array<string | number | null>> = [];
  for (const entry of seriesList) {
    const name =
      typeof entry.name === "string" && entry.name !== ""
        ? entry.name
        : `series${valueColumns.length + 1}`;
    const data = entry.data;
    if (!Array.isArray(data)) return null;
    // 坐标点对 [x, y] → 取 y；纯数值直接用。
    const values = data.map((cell): string | number | null => {
      if (Array.isArray(cell)) {
        const candidate = cell[1];
        return typeof candidate === "number" || typeof candidate === "string"
          ? candidate
          : null;
      }
      return typeof cell === "number" || typeof cell === "string" ? cell : null;
    });
    columns.push(name);
    valueColumns.push(values);
  }

  const rowCount = Math.max(...valueColumns.map((values) => values.length));
  if (rowCount === 0) return null;

  // x 轴类目作首列（若 raw 提供 category 轴 data）。
  const xAxis = raw.xAxis;
  const xAxisList = Array.isArray(xAxis) ? xAxis : [xAxis];
  const firstAxis = xAxisList.find(
    (axis): axis is Record<string, unknown> =>
      typeof axis === "object" && axis !== null && !Array.isArray(axis),
  );
  const categories = firstAxis?.data;
  const hasCategories = Array.isArray(categories) && categories.length > 0;

  const allColumns = hasCategories ? ["x", ...columns] : columns;
  const rows = Array.from({ length: rowCount }, (_, r) => [
    ...(hasCategories
      ? [
          typeof categories[r] === "number" || typeof categories[r] === "string"
            ? (categories[r] as string | number)
            : null,
        ]
      : []),
    ...valueColumns.map((values) => values[r] ?? null),
  ]);
  return { columns: allColumns, rows };
}

/** 快照表格（面板数据表视图与 table 面板共用）。 */
function SnapshotTable({
  columns,
  rows,
  testId = "dashboard-table",
}: {
  columns: string[];
  rows: Array<Array<string | number | null>>;
  testId?: string;
}) {
  const scroll = rows.length > TABLE_SCROLL_THRESHOLD;
  return (
    <div
      className={
        scroll
          ? "max-h-56 overflow-y-auto rounded border border-slate-200"
          : "rounded border border-slate-200"
      }
    >
      <table data-testid={testId} className="w-full border-collapse text-xs">
        <thead className="sticky top-0 bg-slate-50">
          <tr>
            {columns.map((column, i) => (
              <th
                key={`${column}-${i}`}
                className="border-b border-slate-200 px-2 py-1.5 text-left font-medium text-slate-600"
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="even:bg-slate-50/50">
              {columns.map((_, c) => {
                const cell = row[c];
                return (
                  <td key={c} className="px-2 py-1.5 text-slate-700 tabular-nums">
                    {cell === null || cell === undefined ? (
                      <span className="text-slate-300">-</span>
                    ) : (
                      String(cell)
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** table 面板：列头 + 行；超 20 行启用内部滚动（不整页拉长）。 */
function TableBody({ panel }: { panel: DashboardPanel }) {
  const columns = panel.snapshot?.columns ?? [];
  const rows = panel.snapshot?.rows ?? [];
  if (rows.length === 0) return <NoData />;
  return <SnapshotTable columns={columns} rows={rows} />;
}

// ── 工具栏（wire-chart-contract-v2 任务 3.1–3.4）───────────────────────

/**
 * 面板工具栏：全屏 / 导出图片 / 图数切换 / 复制数据。
 * 全屏通过 window 事件通知对应的 EChartBox（面板 id 定向，避免多面板串扰）。
 */
function PanelToolbar({
  panel,
  showTable,
  onToggleTable,
}: {
  panel: DashboardPanel;
  showTable: boolean;
  onToggleTable: () => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const chartHostRef = useRef<Element | null>(null);

  // 找到本面板的图表容器（导出图片要拿它的 canvas/svg）。
  const findChartHost = useCallback((): Element | null => {
    if (chartHostRef.current?.isConnected) return chartHostRef.current;
    const section = document.querySelector(
      `[data-panel-id="${CSS.escape(panel.id)}"]`,
    );
    const host = section?.querySelector('[data-testid="dashboard-chart"]') ?? null;
    chartHostRef.current = host;
    return host;
  }, [panel.id]);

  const toggleFullscreen = () => {
    window.dispatchEvent(
      new CustomEvent("wire-dashboard-fullscreen", {
        detail: { panelId: panel.id },
      }),
    );
  };

  // 导出图片：从已渲染的 SVG 序列化（零新端点、零 echarts 依赖）。
  const exportImage = () => {
    const host = findChartHost();
    const svg = host?.querySelector("svg");
    if (!svg) return;
    const source = new XMLSerializer().serializeToString(svg);
    const blob = new Blob([`<?xml version="1.0" encoding="UTF-8"?>\n${source}`], {
      type: "image/svg+xml;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${panel.title || panel.id}.svg`;
    link.click();
    URL.revokeObjectURL(url);
  };

  // 复制该面板数据（TSV，可直接粘贴进表格软件）。
  const copyData = async () => {
    const extracted =
      panel.snapshot !== undefined && (panel.snapshot.rows.length ?? 0) > 0
        ? { columns: panel.snapshot.columns, rows: panel.snapshot.rows }
        : extractRawSeries(panel);
    if (extracted === null) return;
    const text = [
      extracted.columns.join("\t"),
      ...extracted.rows.map((row) =>
        row.map((cell) => (cell === null ? "" : String(cell))).join("\t"),
      ),
    ].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用（无权限/非安全上下文）：静默——不阻塞渲染 */
    }
  };

  const buttonClass =
    "rounded border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800";

  return (
    <span data-testid="dashboard-panel-toolbar" className="ml-auto flex items-center gap-1">
      <button type="button" className={buttonClass} onClick={toggleFullscreen} data-testid="panel-tool-fullscreen">
        {t("console.dashboard.toolbar.fullscreen")}
      </button>
      <button type="button" className={buttonClass} onClick={onToggleTable} data-testid="panel-tool-table">
        {showTable
          ? t("console.dashboard.toolbar.showChart")
          : t("console.dashboard.toolbar.showTable")}
      </button>
      {!showTable && (
        <button type="button" className={buttonClass} onClick={exportImage} data-testid="panel-tool-export">
          {t("console.dashboard.toolbar.exportImage")}
        </button>
      )}
      <button type="button" className={buttonClass} onClick={() => void copyData()} data-testid="panel-tool-copy">
        {copied
          ? t("console.dashboard.toolbar.copied")
          : t("console.dashboard.toolbar.copyData")}
      </button>
    </span>
  );
}

/**
 * List 面板（wire-list-panel）：按快照行序渲染图文卡片流。
 * 角色映射：title（必填）/ summary / image / time / source / link——
 * 值均为快照列名；时间只作原文展示（顺序由快照决定，不在这里重排）。
 * 图片与外链只认 http(s)；图片加载失败整块消失（不留破图占位）。
 */
function httpUrl(value: string | number | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const url = value.trim();
  return /^https?:\/\//i.test(url) ? url : null;
}

/** 卡片图片：懒加载 + 无 referrer；失败即卸载（state，不直接改 DOM）。 */
function ListImage({ url }: { url: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      className="h-36 w-full object-cover"
      onError={() => setFailed(true)}
    />
  );
}

function ListBody({ panel }: { panel: DashboardPanel }) {
  const columns = panel.snapshot?.columns ?? [];
  const allRows = panel.snapshot?.rows ?? [];
  const cap = panel.config?.limit ?? 20;
  const rows = allRows.slice(0, cap);
  const lag = panel.info_lag;
  const lagNote = lag ? (
    <div
      data-testid="dashboard-list-lag"
      className="mb-2 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 ring-1 ring-amber-200"
    >
      数据可能滞后：{lag.reason}（{lag.at}）。当前展示的是上次成功快照。
    </div>
  ) : null;
  if (rows.length === 0) {
    return (
      <>
        {lagNote}
        <NoData />
      </>
    );
  }

  const indexOf = (
    role: "title" | "summary" | "image" | "time" | "source" | "link",
  ): number => {
    const column = panel.encoding?.[role];
    return typeof column === "string" ? columns.indexOf(column) : -1;
  };
  const titleCol = indexOf("title");
  const summaryCol = indexOf("summary");
  const imageCol = indexOf("image");
  const timeCol = indexOf("time");
  const sourceCol = indexOf("source");
  const linkCol = indexOf("link");

  const rawCell = (row: Array<string | number | null>, index: number) =>
    index >= 0 ? row[index] : null;
  const cellText = (row: Array<string | number | null>, index: number): string => {
    const cell = rawCell(row, index);
    return cell === null || cell === undefined ? "" : String(cell);
  };

  return (
    <div
      data-testid="dashboard-list"
      className={
        rows.length > 3
          ? "max-h-96 space-y-3 overflow-y-auto pr-1"
          : "space-y-3"
      }
    >
      {lagNote}
      {rows.map((row, index) => {
        const title = cellText(row, titleCol);
        const summary = cellText(row, summaryCol);
        const time = cellText(row, timeCol);
        const source = cellText(row, sourceCol);
        const imageUrl = httpUrl(rawCell(row, imageCol));
        const linkUrl = httpUrl(rawCell(row, linkCol));

        const card = (
          <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
            {imageUrl !== null && <ListImage url={imageUrl} />}
            <div className="p-3">
              <div className="text-sm font-medium text-slate-800 line-clamp-2">
                {title}
              </div>
              {summary !== "" && (
                <p className="mt-1 text-xs text-slate-600 line-clamp-3">
                  {summary}
                </p>
              )}
              {(time !== "" || source !== "") && (
                <div className="mt-1 text-[11px] text-slate-400">
                  {time}
                  {time !== "" && source !== "" ? " · " : ""}
                  {source}
                </div>
              )}
            </div>
          </div>
        );

        return (
          <div key={index} data-testid="dashboard-list-item">
            {linkUrl !== null ? (
              <a
                href={linkUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="block hover:opacity-90"
              >
                {card}
              </a>
            ) : (
              card
            )}
          </div>
        );
      })}
    </div>
  );
}
