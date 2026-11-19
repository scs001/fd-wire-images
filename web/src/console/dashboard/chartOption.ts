// 面板 → echarts option 构建（wire-chart-contract-v2 任务 2.1–2.7）。
//
// 三档构建路径（按面板携带的字段判定，见 spec.ts 的 isV2Panel）：
// 1. **v1 回退**：无任何 v2 字段——轴与系列映射与升级前逐字段一致
//    （category 轴 / 首列 x / 其余列系列），只附加交互（十字光标、缩放）。
// 2. **v2 类型化**：encoding / panes / overlays 在场——按编码通道建轴与系列，
//    支持 time/value/log 轴、多 y 列、系列分组、多窗格与叠加层。
// 3. **raw_option 直通**：完整配置原样作为基底，类型化字段（若同时在场）
//    覆盖其上——「类型化优先、raw 补齐缺位」（design D2）。
//
// 本模块是纯函数层：不引 echarts 运行时（只借其类型），不碰 DOM，便于单测
// 对 option 做结构化断言。渲染器（DashboardRenderer）只负责生命周期与容器。
import type { EChartsOption } from "echarts";
import type {
  DashboardAxisType,
  DashboardPanel,
  DashboardPanelEncoding,
  DashboardPanelOverlays,
  DashboardPanelPane,
  DashboardPanelType,
} from "./spec";

/** 未声明 height 时的默认图高（v1 视觉基线：升级前写死的 220）。 */
export const DEFAULT_PANEL_HEIGHT = 220;

/** 多窗格时单个窗格的最小高度（低于此值标签会互相压盖）。 */
export const MIN_PANE_HEIGHT = 60;

/** 行数超过此值时，除内嵌滚轮缩放外再挂一个缩放滑块。 */
export const ZOOM_SLIDER_THRESHOLD = 60;

/** 栅格左右留白与图例/轴标签预留（px）。 */
const GRID_LEFT = 52;
const GRID_RIGHT = 20;
const LEGEND_HEIGHT = 30;
const X_LABEL_HEIGHT = 26;
const PANE_GAP = 18;

type Cell = string | number | null;

/** 走笛卡尔坐标（x/y 轴）的类型；其余为无轴类型（饼/仪表/雷达/矩形树/桑基/漏斗）。 */
const CARTESIAN_TYPES: ReadonlySet<DashboardPanelType> = new Set([
  "line",
  "bar",
  "scatter",
  "candlestick",
  "heatmap",
  "boxplot",
]);

function isCartesian(type: DashboardPanelType): boolean {
  return CARTESIAN_TYPES.has(type);
}

// ── 值转换 ──────────────────────────────────────────────────────────────

/** 单元格 → number | null（字符串数字也认；空/非数归 null 让图断线）。 */
function toNumber(cell: Cell): number | null {
  if (typeof cell === "number") return Number.isFinite(cell) ? cell : null;
  if (typeof cell === "string") {
    const trimmed = cell.trim();
    if (trimmed === "") return null;
    const n = Number(trimmed);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/**
 * 时间轴取值：把快照首列的字符串解析成毫秒时间戳。
 * 支持 `YYYY` / `YYYY-MM` / `YYYY-MM-DD` / `YYYY/MM/DD` / ISO 带时间；
 * 已是数字则原样（调用方可能已给时间戳）。解析失败归 null——echarts 会
 * 在该点断线（而非把非法值当 0 错位，那会伪造出一段并不存在的趋势）。
 */
export function toTimestamp(cell: Cell | undefined): number | null {
  if (typeof cell === "number") return Number.isFinite(cell) ? cell : null;
  if (typeof cell !== "string") return null;
  const text = cell.trim();
  if (text === "") return null;

  const yearOnly = /^(\d{4})$/.exec(text);
  if (yearOnly) return Date.UTC(Number(yearOnly[1]), 0, 1);

  const yearMonth = /^(\d{4})[-/](\d{1,2})$/.exec(text);
  if (yearMonth) {
    const month = Number(yearMonth[2]);
    if (month < 1 || month > 12) return null;
    return Date.UTC(Number(yearMonth[1]), month - 1, 1);
  }

  const day = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?/.exec(
    text,
  );
  if (day) {
    const month = Number(day[2]);
    const date = Number(day[3]);
    if (month < 1 || month > 12 || date < 1 || date > 31) return null;
    return Date.UTC(
      Number(day[1]),
      month - 1,
      date,
      Number(day[4] ?? 0),
      Number(day[5] ?? 0),
      Number(day[6] ?? 0),
    );
  }
  return null;
}

/** 列名 → 索引（找不到返回 -1）。 */
function columnIndex(columns: string[], name: string | undefined): number {
  if (name === undefined) return -1;
  return columns.indexOf(name);
}

// ── 编码通道解析 ────────────────────────────────────────────────────────

/** 解析后的编码：列索引已定位，轴类型已定。 */
interface ResolvedEncoding {
  xIndex: number;
  /** y 列索引（按声明序）；radial 类型只用第一个。 */
  yIndices: number[];
  seriesIndex: number;
  sizeIndex: number;
  xType: DashboardAxisType;
  yType: DashboardAxisType;
  xName: string | undefined;
  yName: string | undefined;
}

/**
 * 编码解析：声明优先、缺省回退 v1 约定（首列 x、其余列 y）。
 * 声明的列名在当前快照里不存在时**回退到位置约定**并保留轴类型——
 * 这样「换了数据集但忘了改编码」不会画出空白图。
 */
export function resolveEncoding(
  panel: DashboardPanel,
  snapshotColumns: string[],
  encoding: DashboardPanelEncoding | undefined = panel.encoding,
): ResolvedEncoding {
  const columns = snapshotColumns;
  const declaredX = columnIndex(columns, encoding?.x);
  const xIndex = declaredX >= 0 ? declaredX : 0;

  const declaredY = (encoding?.y ?? [])
    .map((name) => columnIndex(columns, name))
    .filter((index) => index >= 0 && index !== xIndex);
  const yIndices =
    declaredY.length > 0
      ? declaredY
      : columns.map((_, i) => i).filter((i) => i !== xIndex);

  const seriesIndex = columnIndex(columns, encoding?.series);
  const sizeIndex = columnIndex(columns, encoding?.size);

  return {
    xIndex,
    yIndices,
    seriesIndex,
    sizeIndex,
    xType: encoding?.xType ?? "category",
    yType: encoding?.yType ?? "value",
    xName: encoding?.xName ?? panel.config?.xName,
    yName: encoding?.yName ?? panel.config?.yName,
  };
}

/** x 轴原始值（类目名 / 时间字符串或时间戳）。 */
function xValues(rows: Cell[][], xIndex: number): Cell[] {
  return rows.map((row) => row[xIndex] ?? null);
}

/** y 轴某列的值序列。 */
function yValues(rows: Cell[][], yIndex: number): Array<number | null> {
  return rows.map((row) => toNumber(row[yIndex] ?? null));
}

/** 类目名（缺值归空串——类目轴不接受 null 类目）。 */
function categoryLabels(values: Cell[]): string[] {
  return values.map((value) => (value === null ? "" : String(value)));
}

// ── 叠加层 ──────────────────────────────────────────────────────────────

/** 叠加层 → echarts 的 markLine/markArea/markPoint 三段。 */
function overlayParts(
  overlays: DashboardPanelOverlays | undefined,
  xType: DashboardAxisType,
): {
  markLine?: Record<string, unknown>;
  markArea?: Record<string, unknown>;
  markPoint?: Record<string, unknown>;
} {
  if (overlays === undefined) return {};
  const parts: {
    markLine?: Record<string, unknown>;
    markArea?: Record<string, unknown>;
    markPoint?: Record<string, unknown>;
  } = {};

  if (overlays.markLine !== undefined && overlays.markLine.length > 0) {
    parts.markLine = {
      silent: false,
      symbol: "none",
      data: overlays.markLine.map((line) => {
        // axis=x 画垂直线；缺省 y 画水平线。
        const coord =
          line.axis === "x"
            ? { xAxis: xType === "time" ? (toTimestamp(line.value) ?? line.value) : line.value }
            : { yAxis: line.value };
        return {
          ...coord,
          ...(line.label !== undefined
            ? { label: { formatter: line.label, position: "insideEndTop" } }
            : { label: { show: false } }),
          ...(line.lineStyle !== undefined
            ? { lineStyle: { type: line.lineStyle } }
            : {}),
        };
      }),
    };
  }

  if (overlays.markArea !== undefined && overlays.markArea.length > 0) {
    const conv = (value: string | number): string | number =>
      xType === "time" && typeof value === "string"
        ? (toTimestamp(value) ?? value)
        : value;
    parts.markArea = {
      silent: true,
      data: overlays.markArea.map((area) => [
        {
          xAxis: conv(area.from),
          ...(area.label !== undefined
            ? { label: { formatter: area.label, position: "insideTop" } }
            : { label: { show: false } }),
        },
        { xAxis: conv(area.to) },
      ]),
    };
  }

  if (overlays.markPoint !== undefined && overlays.markPoint.length > 0) {
    parts.markPoint = {
      data: overlays.markPoint.map((point) => ({
        coord:
          xType === "time"
            ? [toTimestamp(point.x) ?? point.x, point.y]
            : [point.x, point.y],
        ...(point.label !== undefined ? { value: point.label } : {}),
      })),
    };
  }

  return parts;
}

// ── 系列构建 ────────────────────────────────────────────────────────────

/** 笛卡尔轴配置（含 time 轴的人性化标签）。 */
function cartesianAxes(
  resolved: ResolvedEncoding,
  values: Cell[],
  gridIndex: number,
  paneCount: number,
): { xAxis: Record<string, unknown>; yAxis: Record<string, unknown> } {
  const showXLabels = gridIndex === paneCount - 1;
  const xAxis: Record<string, unknown> = {
    type: resolved.xType,
    gridIndex,
    ...(resolved.xName !== undefined ? { name: resolved.xName } : {}),
    ...(showXLabels ? {} : { axisLabel: { show: false } }),
    ...(resolved.xType === "time"
      ? { axisLabel: { ...(showXLabels ? {} : { show: false }), hideOverlap: true } }
      : resolved.xType === "category"
        ? { data: categoryLabels(values) }
        : {}),
    ...(resolved.xType === "log" ? { logBase: 10 } : {}),
  };
  const yAxis: Record<string, unknown> = {
    type: resolved.yType,
    gridIndex,
    scale: true,
    ...(resolved.yName !== undefined ? { name: resolved.yName } : {}),
    ...(resolved.yType === "log" ? { logBase: 10 } : {}),
  };
  return { xAxis, yAxis };
}

/** 一条系列的基础字段。 */
function baseSeries(
  type: string,
  name: string,
  index: number,
): Record<string, unknown> {
  return { name, type, xAxisIndex: index, yAxisIndex: index };
}

/** 按面板类型构建系列（笛卡尔类）。 */
function cartesianSeries(
  panel: DashboardPanel,
  resolved: ResolvedEncoding,
  rows: Cell[][],
  gridIndex: number,
  overlays: DashboardPanelOverlays | undefined,
): Record<string, unknown>[] {
  const xRaw = xValues(rows, resolved.xIndex);
  const overlay = overlayParts(overlays, resolved.xType);
  const nameOf = (columnIndexValue: number): string =>
    panel.snapshot?.columns[columnIndexValue] ?? `系列${columnIndexValue}`;

  switch (panel.type) {
    case "candlestick": {
      // y 列序约定 [open, high, low, close]；echarts 要 [open, close, low, high]。
      const [openIdx, highIdx, lowIdx, closeIdx] = resolved.yIndices;
      const data = rows.map((row) => {
        const ts = resolved.xType === "time" ? toTimestamp(row[resolved.xIndex] ?? null) : null;
        const o = openIdx !== undefined ? toNumber(row[openIdx] ?? null) : null;
        const h = highIdx !== undefined ? toNumber(row[highIdx] ?? null) : null;
        const l = lowIdx !== undefined ? toNumber(row[lowIdx] ?? null) : null;
        const c = closeIdx !== undefined ? toNumber(row[closeIdx] ?? null) : null;
        const values = [o, c, l, h];
        return resolved.xType === "time" ? [ts, ...values] : values;
      });
      return [
        {
          ...baseSeries("candlestick", panel.snapshot?.columns[closeIdx ?? -1] ?? "K线", gridIndex),
          data,
          ...overlay,
        },
      ];
    }
    case "scatter": {
      // 多 y 列 → 多系列；每点 [x, y]（time 轴转时间戳）。
      return resolved.yIndices.map((yIndex, i) => {
        const data = rows.map((row) => {
          const xRawValue = row[resolved.xIndex] ?? null;
          const x =
            resolved.xType === "time" ? toTimestamp(xRawValue) : toNumber(xRawValue) ?? xRawValue;
          const size =
            resolved.sizeIndex >= 0 ? toNumber(row[resolved.sizeIndex] ?? null) : null;
          const y = toNumber(row[yIndex] ?? null);
          return size !== null ? [x, y, size] : [x, y];
        });
        return {
          ...baseSeries("scatter", nameOf(yIndex), gridIndex),
          data,
          ...(i === 0 ? overlay : {}),
        };
      });
    }
    case "heatmap": {
      // 三代目：x 类目、series 列（或第二列）为 y 类目、y[0] 为值。
      const xLabels = categoryLabels(xRaw);
      const yColumn = resolved.seriesIndex >= 0 ? resolved.seriesIndex : resolved.yIndices[0];
      const yLabels = yColumn !== undefined ? categoryLabels(rows.map((r) => r[yColumn] ?? null)) : [];
      const valueColumn = resolved.yIndices[resolved.seriesIndex >= 0 ? 0 : 1] ?? resolved.yIndices[0];
      const uniqueX = [...new Set(xLabels)];
      const uniqueY = [...new Set(yLabels)];
      const data = rows.map((row, i) => [
        uniqueX.indexOf(xLabels[i] ?? ""),
        uniqueY.indexOf(yLabels[i] ?? ""),
        valueColumn !== undefined ? toNumber(row[valueColumn] ?? null) : null,
      ]);
      return [
        {
          ...baseSeries("heatmap", panel.title || "热力", gridIndex),
          data,
          label: { show: false },
          ...overlay,
        },
      ];
    }
    case "boxplot": {
      // y 列序约定 [min, q1, median, q3, max]。
      const data = rows.map((row) =>
        resolved.yIndices.slice(0, 5).map((index) => toNumber(row[index] ?? null)),
      );
      return [
        {
          ...baseSeries("boxplot", panel.title || "箱线", gridIndex),
          data,
          ...overlay,
        },
      ];
    }
    case "bar":
    case "line":
    default: {
      const seriesType = panel.type === "bar" ? "bar" : "line";
      // 系列分组列在场：按该列去重值拆成多条系列（长表形态）。
      if (resolved.seriesIndex >= 0) {
        const groupColumn = resolved.seriesIndex;
        const yColumn = resolved.yIndices[0];
        const groups = [...new Set(rows.map((row) => String(row[groupColumn] ?? "")))];
        return groups.map((group, i) => {
          const data = rows.map((row) =>
            String(row[groupColumn] ?? "") === group && yColumn !== undefined
              ? toNumber(row[yColumn] ?? null)
              : null,
          );
          return {
            ...baseSeries(seriesType, group, gridIndex),
            data,
            ...(i === 0 ? overlay : {}),
          };
        });
      }
      return resolved.yIndices.map((yIndex, i) => {
        const values = yValues(rows, yIndex);
        const data =
          resolved.xType === "time"
            ? xRaw.map((cell, r) => [toTimestamp(cell), values[r] ?? null])
            : values;
        return {
          ...baseSeries(seriesType, nameOf(yIndex), gridIndex),
          data,
          ...(seriesType === "line" ? { smooth: false, showSymbol: false } : {}),
          ...(i === 0 ? overlay : {}),
        };
      });
    }
  }
}

/** 无轴类型（饼/仪表/雷达/矩形树/桑基/漏斗）的系列与坐标系。 */
function radialOption(
  panel: DashboardPanel,
  resolved: ResolvedEncoding,
  rows: Cell[][],
  overlays: DashboardPanelOverlays | undefined,
): {
  series: Record<string, unknown>[];
  extra: Record<string, unknown>;
} {
  const xRaw = xValues(rows, resolved.xIndex);
  const names = categoryLabels(xRaw);
  const valueIndex = resolved.yIndices[0];
  const values =
    valueIndex !== undefined ? yValues(rows, valueIndex) : [];
  const pairs = names.map((name, i) => ({ name, value: values[i] ?? null }));
  const overlay = overlayParts(overlays, resolved.xType);

  switch (panel.type) {
    case "pie":
      return {
        series: [
          {
            name: panel.title || "占比",
            type: "pie",
            radius: ["38%", "68%"],
            data: pairs,
            label: { formatter: "{b} {d}%" },
            ...overlay,
          },
        ],
        extra: {},
      };
    case "funnel":
      return {
        series: [
          {
            name: panel.title || "漏斗",
            type: "funnel",
            data: pairs,
            label: { formatter: "{b}: {c}" },
            ...overlay,
          },
        ],
        extra: {},
      };
    case "gauge": {
      const first = pairs[0];
      return {
        series: [
          {
            name: panel.title || "指标",
            type: "gauge",
            min: 0,
            max: Math.max(1, ...values.filter((v): v is number => v !== null)),
            data: [{ name: first?.name ?? "", value: first?.value ?? 0 }],
            ...overlay,
          },
        ],
        extra: {},
      };
    }
    case "radar": {
      const maxValue = Math.max(1, ...values.filter((v): v is number => v !== null)) * 1.1;
      const indicators = names.map((name) => ({
        name,
        max: maxValue,
      }));
      return {
        series: [
          {
            name: panel.title || "雷达",
            type: "radar",
            data: [
              {
                value: values.map((v) => v ?? 0),
                name: panel.title || "数值",
              },
            ],
            ...overlay,
          },
        ],
        extra: { radar: { indicator: indicators } },
      };
    }
    case "treemap":
      return {
        series: [
          {
            name: panel.title || "矩形树",
            type: "treemap",
            data: pairs,
            ...overlay,
          },
        ],
        extra: {},
      };
    case "sankey": {
      const targetColumn = resolved.seriesIndex >= 0 ? resolved.seriesIndex : resolved.yIndices[0];
      const valueColumn = resolved.sizeIndex >= 0 ? resolved.sizeIndex : resolved.yIndices[1];
      const links = rows.map((row) => ({
        source: String(row[resolved.xIndex] ?? ""),
        target: targetColumn !== undefined ? String(row[targetColumn] ?? "") : "",
        value: valueColumn !== undefined ? (toNumber(row[valueColumn] ?? null) ?? 0) : 0,
      }));
      const nodes = [
        ...new Set(links.flatMap((link) => [link.source, link.target])),
      ].map((name) => ({ name }));
      return {
        series: [
          {
            name: panel.title || "桑基",
            type: "sankey",
            data: nodes,
            links,
            ...overlay,
          },
        ],
        extra: {},
      };
    }
    default:
      // 理论不可达：无轴类型集合与 CARTESIAN 集合互补。
      return { series: [], extra: {} };
  }
}

// ── 深合并（raw_option 与类型化字段）──────────────────────────────────

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/**
 * 深合并（design D2）：对象逐键递归；**数组整体替换**（不按下标合并——
 * 半合并出的系列列表既不来自 raw 也不来自表单，是最难排查的那类错）。
 * override 中的 undefined 不覆盖 base 的值（缺省字段不应抹掉 raw 的配置）。
 */
export function deepMergeOptions(
  base: Record<string, unknown>,
  override: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(override)) {
    if (value === undefined) continue;
    const existing = out[key];
    out[key] =
      isPlainObject(existing) && isPlainObject(value)
        ? deepMergeOptions(existing, value)
        : value;
  }
  return out;
}

// ── 多窗格布局 ─────────────────────────────────────────────────────────

/** 窗格相对权重：缺省主窗格 3、副窗格 1。 */
function paneWeights(panes: DashboardPanelPane[]): number[] {
  return panes.map((pane, i) => pane.height ?? (i === 0 ? 3 : 1));
}

/** 窗格高度是否足够（任一低于下限即判定为不足，降级为只渲染主窗格）。 */
export function panesFitInHeight(
  panes: DashboardPanelPane[],
  totalHeight: number,
): boolean {
  const weights = paneWeights(panes);
  const sum = weights.reduce((a, b) => a + b, 0);
  const usable = totalHeight - LEGEND_HEIGHT - X_LABEL_HEIGHT - PANE_GAP * (panes.length - 1);
  return weights.every((w) => (usable * w) / sum >= MIN_PANE_HEIGHT);
}

interface PaneLayout {
  grid: Record<string, unknown>;
  top: number;
  height: number;
}

function paneLayouts(weights: number[], totalHeight: number): PaneLayout[] {
  const sum = weights.reduce((a, b) => a + b, 0);
  const usable =
    totalHeight - LEGEND_HEIGHT - X_LABEL_HEIGHT - PANE_GAP * (weights.length - 1);
  let cursor = LEGEND_HEIGHT;
  return weights.map((weight) => {
    const height = (usable * weight) / sum;
    const layout: PaneLayout = {
      top: cursor,
      height,
      grid: {
        left: GRID_LEFT,
        right: GRID_RIGHT,
        top: cursor,
        height,
        containLabel: false,
      },
    };
    cursor += height + PANE_GAP;
    return layout;
  });
}

// ── 主入口 ─────────────────────────────────────────────────────────────

/** 构建结果：option + 渲染层需要知道的元信息。 */
export interface BuiltPanelOption {
  option: EChartsOption;
  /** 容器高度（px）：声明值或默认值。 */
  height: number;
  /** panes 因高度不足被降级（渲染层据此标注，不静默）。 */
  panesDegraded: boolean;
}

/**
 * raw_option 的最小坐标骨架（只在作者未声明时补齐）。
 *
 * 为什么不能纯直通：echarts 对「有 series 但缺 yAxis」直接抛
 * `yAxis "0" not found`，整个看板白屏。作者写 raw 时常常省略它认为
 * "显然"的轴配置——补齐骨架让半成品配置也能呈现，且不覆盖任何已声明的键。
 */
function rawSkeleton(raw: Record<string, unknown>): Record<string, unknown> {
  const needsCartesian =
    raw.series !== undefined &&
    !("radar" in raw) &&
    !("polar" in raw) &&
    !("geo" in raw) &&
    !("calendar" in raw);
  if (!needsCartesian) return raw;

  const series = Array.isArray(raw.series) ? raw.series : [];
  // 全为无轴类型（pie/gauge/treemap/sankey/funnel）时不需要坐标轴。
  const axisFree = new Set(["pie", "gauge", "treemap", "sankey", "funnel", "sunburst", "graph"]);
  const allAxisFree =
    series.length > 0 &&
    series.every(
      (entry) =>
        typeof entry === "object" &&
        entry !== null &&
        axisFree.has(String((entry as Record<string, unknown>).type)),
    );
  if (allAxisFree) return raw;

  return {
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    grid: { left: GRID_LEFT, right: GRID_RIGHT, top: 24, bottom: X_LABEL_HEIGHT },
    xAxis: { type: "category" },
    yAxis: { type: "value", scale: true },
    ...raw,
  };
}

/**
 * 面板 → option。返回 null 表示无数据可画（渲染层显示无数据占位）。
 *
 * `interactive` 控制是否附加十字光标/缩放等交互键——默认开启；分享页与
 * 编辑器预览同样开启（交互是只读能力，不触碰数据）。
 */
export function buildPanelOption(
  panel: DashboardPanel,
  options: { interactive?: boolean } = {},
): BuiltPanelOption | null {
  const interactive = options.interactive ?? true;
  const height = panel.height ?? DEFAULT_PANEL_HEIGHT;
  const columns = panel.snapshot?.columns ?? [];
  const rows = panel.snapshot?.rows ?? [];

  // ① raw_option 独占（无类型化数据）：骨架补齐后原样直通，尊重作者的配置。
  if (panel.raw_option !== undefined && columns.length === 0) {
    return {
      option: rawSkeleton(structuredClone(panel.raw_option)) as EChartsOption,
      height,
      panesDegraded: false,
    };
  }

  // ② 无快照且无 raw：无数据。
  if (rows.length === 0 || columns.length === 0) return null;

  // ③ 类型化构建。
  const typed = buildTypedOption(panel, columns, rows, { interactive, height });

  // ④ raw_option 与类型化共存：类型化优先、raw 补齐缺位。
  if (panel.raw_option !== undefined) {
    const base = rawSkeleton(structuredClone(panel.raw_option));
    return {
      ...typed,
      option: deepMergeOptions(
        base,
        typed.option as unknown as Record<string, unknown>,
      ) as EChartsOption,
    };
  }
  return typed;
}

function buildTypedOption(
  panel: DashboardPanel,
  columns: string[],
  rows: Cell[][],
  ctx: { interactive: boolean; height: number },
): BuiltPanelOption {
  const resolved = resolveEncoding(panel, columns);
  const interactive = ctx.interactive && isCartesian(panel.type);
  const slideZoom = ctx.interactive && isCartesian(panel.type) && rows.length > ZOOM_SLIDER_THRESHOLD;

  // 无轴类型：单坐标系，无多窗格语义。
  if (!isCartesian(panel.type)) {
    const radial = radialOption(panel, resolved, rows, panel.overlays);
    return {
      height: ctx.height,
      panesDegraded: false,
      option: {
        tooltip: { trigger: "item" },
        ...radial.extra,
        series: radial.series,
      } as EChartsOption,
    };
  }

  // 多窗格：≥2 个窗格才生效；高度不足则降级为只渲染主窗格并标注。
  const declaredPanes = panel.panes;
  const panesDegraded =
    declaredPanes !== undefined &&
    declaredPanes.length >= 2 &&
    !panesFitInHeight(declaredPanes, ctx.height);
  const panes: DashboardPanelPane[] | undefined =
    declaredPanes !== undefined && declaredPanes.length >= 2 && !panesDegraded
      ? declaredPanes
      : undefined;

  const legends: string[] = [];

  if (panes === undefined) {
    const axes = cartesianAxes(resolved, xValues(rows, resolved.xIndex), 0, 1);
    const series = cartesianSeries(panel, resolved, rows, 0, panel.overlays);
    for (const s of series) {
      const name = s.name;
      if (typeof name === "string" && name !== "") legends.push(name);
    }
    const option: Record<string, unknown> = {
      tooltip: {
        trigger: "axis",
        ...(interactive ? { axisPointer: { type: "cross" } } : {}),
      },
      ...(legends.length > 1 ? { legend: { data: legends, top: 2 } } : {}),
      grid: {
        left: GRID_LEFT,
        right: GRID_RIGHT,
        top: legends.length > 1 ? LEGEND_HEIGHT : 8,
        bottom: X_LABEL_HEIGHT,
      },
      xAxis: axes.xAxis,
      yAxis: axes.yAxis,
      series,
      ...(interactive
        ? {
            dataZoom: [
              { type: "inside", xAxisIndex: [0] },
              ...(slideZoom ? [{ type: "slider", xAxisIndex: [0], height: 16, bottom: 4 }] : []),
            ],
          }
        : {}),
    };
    return {
      height: ctx.height,
      panesDegraded,
      option: option as EChartsOption,
    };
  }

  // 多窗格：每格一对轴、共享 axisPointer、缩放覆盖全部窗格。
  const weights = paneWeights(panes);
  const layouts = paneLayouts(weights, ctx.height);
  const xAxes: Record<string, unknown>[] = [];
  const yAxes: Record<string, unknown>[] = [];
  const series: Record<string, unknown>[] = [];

  panes.forEach((pane, index) => {
    // 窗格未声明编码时继承主窗格编码（保证共享同一 x 刻度）。
    const paneEncoding = pane.encoding ?? (index === 0 ? panel.encoding : panel.encoding);
    const paneResolved = resolveEncoding(panel, columns, paneEncoding);
    const sharedResolved =
      index === 0
        ? paneResolved
        : { ...paneResolved, xIndex: resolved.xIndex, xType: resolved.xType };
    const axes = cartesianAxes(
      sharedResolved,
      xValues(rows, sharedResolved.xIndex),
      index,
      panes.length,
    );
    xAxes.push(axes.xAxis);
    yAxes.push(axes.yAxis);
    const paneSeries = cartesianSeries(
      panel,
      sharedResolved,
      rows,
      index,
      pane.overlays,
    );
    for (const s of paneSeries) {
      const name = s.name;
      if (typeof name === "string" && name !== "") legends.push(name);
    }
    series.push(...paneSeries);
  });

  const allIndices = panes.map((_, i) => i);
  const option: Record<string, unknown> = {
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    ...(legends.length > 1 ? { legend: { data: legends, top: 2 } } : {}),
    grid: layouts.map((layout) => layout.grid),
    xAxis: xAxes,
    yAxis: yAxes,
    series,
    dataZoom: [
      { type: "inside", xAxisIndex: allIndices },
      ...(slideZoom
        ? [{ type: "slider", xAxisIndex: allIndices, height: 16, bottom: 4 }]
        : []),
    ],
  };

  return {
    height: ctx.height,
    panesDegraded,
    option: option as EChartsOption,
  };
}
