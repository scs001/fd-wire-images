// 仪表盘 spec 契约与宽容校验（wire-dashboard-renderer 任务 1；
// wire-chart-contract-v2 扩展为 v2）。
//
// 存储形状：产物 spec 是信封 `{ title, spec: {…} }`（服务端最小校验口径，
// 见 api 仓 artifacts/service.validate_spec——title 非空 + spec 非空对象）；
// 仪表盘本体 = 信封内层 `{ title, panels: […] }`。parseDashboardSpec
// 同时接受信封（产物详情原样）与本体（编辑器预览）两种输入。
//
// 校验口径（宽容）：panels 里的非法面板逐个收集原因并跳过，不整板失败；
// snapshot 列行数不齐截断/补 null 标注不拒收；缺 id 的面板自动补 id；
// span 非法回退 1。旧自由 JSON（内层无 panels 数组）返回 legacy 态。
// wire-dashboard-query-contract 任务 2.1：面板新增可选 query 引用（刷新
// 契约，非渲染输入）——normalizePanelQuery 宽容归一，非法整体丢弃。
//
// v2（wire-chart-contract-v2）：面板在四类基础形态之上扩展类型谱，并新增
// 可选 encoding（显式编码通道+轴类型）/ panes（共享时间轴的副图）/
// overlays（阈值线与区间）/ height / raw_option（完整图表配置直通）。
// **v1 是 v2 的子集**：无 v2 字段的面板解析后逐字段与升级前一致，渲染走
// 既有「第一列 x、其余列系列、类目轴」回退分支——存量产物零迁移零漂移。
export const DASHBOARD_PANEL_TYPES = [
  // 基础档（v1 既有四类 + wire-list-panel 的图文信息流；编辑器保留类型
  // 互转与专属表单）
  "kpi",
  "line",
  "bar",
  "table",
  "list",
  // 高级档（v2 类型谱；表达力靠 encoding/overlays/raw_option 支撑）
  "candlestick",
  "scatter",
  "pie",
  "heatmap",
  "gauge",
  "radar",
  "treemap",
  "sankey",
  "funnel",
  "boxplot",
] as const;
export type DashboardPanelType = (typeof DASHBOARD_PANEL_TYPES)[number];

/** 基础档类型（v1 语义：第一列 x / 其余列系列 / kpi 取首值 / table 整表；
 *  list = 按快照行序的图文卡片流，见 wire-list-panel）。 */
export const DASHBOARD_BASE_PANEL_TYPES = [
  "kpi",
  "line",
  "bar",
  "table",
  "list",
] as const satisfies ReadonlyArray<DashboardPanelType>;

/** 高级档类型（v2 新增十类）。 */
export const DASHBOARD_ADVANCED_PANEL_TYPES = [
  "candlestick",
  "scatter",
  "pie",
  "heatmap",
  "gauge",
  "radar",
  "treemap",
  "sankey",
  "funnel",
  "boxplot",
] as const satisfies ReadonlyArray<DashboardPanelType>;

export const DASHBOARD_PANEL_SPANS = [1, 2, 3] as const;
export type DashboardPanelSpan = (typeof DASHBOARD_PANEL_SPANS)[number];

/** 快照数据（AI 生成仪表盘时内嵌的已取数结果；实时取数是后续物化 change）。 */
export interface DashboardSnapshot {
  columns: string[];
  rows: Array<Array<string | number | null>>;
}

/** 各面板类型的少量展示选项。 */
export interface DashboardPanelConfig {
  /** kpi：数值右侧的单位（如 %、亿元）。 */
  unit?: string;
  /** kpi：大数下方的副标题。 */
  subtitle?: string;
  /** line/bar：y 轴名。 */
  yName?: string;
  /** line/bar：x 轴名。 */
  xName?: string;
  /** list：渲染行数上限（1..20，缺省 20；wire-list-panel）。 */
  limit?: number;
}

/** 轴类型白名单（v2 编码通道）。category=类目（v1 行为）、value=数值、
 *  time=时间（快照首列按日期解析）、log=对数。 */
export const DASHBOARD_AXIS_TYPES = [
  "category",
  "value",
  "time",
  "log",
] as const;
export type DashboardAxisType = (typeof DASHBOARD_AXIS_TYPES)[number];

/**
 * 显式编码通道（v2）：声明数据列 → 图形属性的映射，取代 v1「第一列当 x
 * 轴、其余列当系列」的位置约定。全部字段可选——缺省的部分回退到 v1 约定。
 */
export interface DashboardPanelEncoding {
  /** x 轴列名（缺省取快照首列）。 */
  x?: string;
  /** y 轴列名（每列一个系列；缺省取首列之外的全部列）。 */
  y?: string[];
  /** 系列分组列名（同列不同值拆成多个系列，用于长表形态）。 */
  series?: string;
  /** 大小通道列名（scatter 的 symbolSize / treemap 的取值）。 */
  size?: string;
  /** x 轴类型（缺省 category，与 v1 行为一致）。 */
  xType?: DashboardAxisType;
  /** y 轴类型（缺省 value）。 */
  yType?: DashboardAxisType;
  /** x 轴名（覆盖 config.xName）。 */
  xName?: string;
  /** y 轴名（覆盖 config.yName）。 */
  yName?: string;
  // ── list 角色通道（wire-list-panel；图表类型忽略这些字段）────────────
  /** 标题列（list 必填角色）。 */
  title?: string;
  /** 摘要列。 */
  summary?: string;
  /** 图片列（值须为 http(s) URL）。 */
  image?: string;
  /** 时间列（仅展示，不参与排序——顺序由快照决定）。 */
  time?: string;
  /** 来源列（卡片底部来源标注，与面板级 source 不同）。 */
  source?: string;
  /** 外链列（值须为 http(s) URL）。 */
  link?: string;
}

/** 叠加层：阈值线 / 区间高亮 / 事件点（v2）。 */
export interface DashboardOverlayLine {
  /** 参考线所在轴值（y 轴默认；axis 指定为 x 时按 x 轴值）。 */
  value: string | number;
  /** 线标签（缺省不显示标签）。 */
  label?: string;
  /** 线型。 */
  lineStyle?: "solid" | "dashed" | "dotted";
  /** 参考线方向：y=水平线（默认）、x=垂直线。 */
  axis?: "x" | "y";
}

export interface DashboardOverlayArea {
  /** 区间起点（含）。 */
  from: string | number;
  /** 区间终点（含）。 */
  to: string | number;
  /** 区间标签（缺省不显示）。 */
  label?: string;
}

export interface DashboardOverlayPoint {
  /** 点的 x 值（类目名或时间字符串）。 */
  x: string | number;
  /** 点的 y 值。 */
  y: string | number;
  /** 点标签（缺省取 y 值）。 */
  label?: string;
}

export interface DashboardPanelOverlays {
  markLine?: DashboardOverlayLine[];
  markArea?: DashboardOverlayArea[];
  markPoint?: DashboardOverlayPoint[];
}

/**
 * 面板窗格（v2）：首个为主窗格，其余为副窗格，垂直堆叠并共享 x 轴刻度与
 * 十字光标（主图+成交量+技术指标的既有行业范式）。窗格可各自声明编码与
 * 叠加层；height 为相对权重（缺省主窗格 3、副窗格 1）。
 */
export interface DashboardPanelPane {
  title?: string;
  encoding?: DashboardPanelEncoding;
  overlays?: DashboardPanelOverlays;
  /** 相对高度权重（正整数，缺省 1）。 */
  height?: number;
}

export interface DashboardPanel {
  id: string;
  type: DashboardPanelType;
  title: string;
  /** 栅格宽度（3 列栅格中的 1/2/3 列）。 */
  span: DashboardPanelSpan;
  /** 来源标注（指标 code / 数据集名，展示用）。 */
  source?: string;
  snapshot?: DashboardSnapshot;
  config?: DashboardPanelConfig;
  /** 查询契约引用（wire-dashboard-query-contract）：刷新按此确定性取数，
   * 非渲染输入——渲染永远读 snapshot；非法 query 在 parse 时整体丢弃。 */
  query?: DashboardPanelQuery;
  /** 信息流来源契约（wire-analysis-console）：list 面板可按 info_query 从
   * fd-info 确定性重取；渲染仍只读 snapshot。 */
  info_query?: DashboardInfoQuery;
  /** 刷新时来源不可用留下的滞后标注（有值表示快照不是本轮取的）。 */
  info_lag?: DashboardInfoLag;
  // ── v2 字段（全部可选；缺省即 v1 语义）──────────────────────────────
  /** 显式编码通道（缺省走「首列 x、其余列系列」回退）。 */
  encoding?: DashboardPanelEncoding;
  /** 多窗格（缺省单窗格；单元素数组归一为不落字段）。 */
  panes?: DashboardPanelPane[];
  /** 面板级叠加层（单窗格时生效；多窗格时各 pane 自带）。 */
  overlays?: DashboardPanelOverlays;
  /** 声明高度（px）；缺省沿用渲染器的默认固定高度（v1 视觉零漂移）。 */
  height?: number;
  /**
   * 完整图表配置直通（v2 逃生舱）：承载类型化字段覆盖不到的图形能力，
   * 也是存量成品看板零转换迁入的通道。必须可序列化（不含函数/正则/类
   * 实例）——解析层校验不过则该面板整体跳过并标注。
   * 与类型化字段共存时：类型化字段优先、raw_option 只补齐未覆盖部分。
   */
  raw_option?: Record<string, unknown>;
}

/** 仪表盘 v1 本体（信封的 spec 内层）。 */
export interface DashboardSpecV1 {
  title: string;
  panels: DashboardPanel[];
}

/** 被跳过的非法面板（原样保留 raw 便于标注与排查）。 */
export interface DashboardBadPanel {
  /** 在原 panels 数组里的序号（0 起）。 */
  index: number;
  /** 跳过原因（i18n 键之外的机器码，渲染层映射文案）。 */
  reason: "not_object" | "bad_type" | "bad_raw_option" | "bad_list_encoding";
  raw: unknown;
}

export type DashboardParseResult =
  | {
      /** v1 = 内层有 panels 数组（面板可全部非法——badPanels 如实呈现）。 */
      kind: "v1";
      title: string;
      panels: DashboardPanel[];
      badPanels: DashboardBadPanel[];
    }
  | {
      /** legacy = 旧自由 JSON（无 panels 结构）：整体只读查看，不拒收。 */
      kind: "legacy";
      raw: Record<string, unknown>;
    };

function isPanelType(value: unknown): value is DashboardPanelType {
  return (
    typeof value === "string" &&
    (DASHBOARD_PANEL_TYPES as readonly string[]).includes(value)
  );
}

/** snapshot 宽容归一：columns 只留字符串；行截断到列数、坏格子置 null。 */
function normalizeSnapshot(raw: unknown): DashboardSnapshot | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const record = raw as { columns?: unknown; rows?: unknown };
  if (!Array.isArray(record.columns) && !Array.isArray(record.rows)) {
    return undefined;
  }
  const columns = (Array.isArray(record.columns) ? record.columns : [])
    .filter((c): c is string => typeof c === "string");
  const rawRows = Array.isArray(record.rows) ? record.rows : [];
  // 行数列数不齐：截断到 columns 长度（不齐处补 null），非数组行整行跳过。
  const rows = rawRows
    .filter((row): row is unknown[] => Array.isArray(row))
    .map((row) =>
      columns.map(
        (_, i): string | number | null => {
          const cell = row[i];
          return typeof cell === "string" || typeof cell === "number"
            ? cell
            : null;
        },
      ),
    );
  return { columns, rows };
}

/** 面板 config 归一：kpi/图表字段原样收字符串；list 另收 limit（1..20
 *  钳制，非数回退 20——wire-list-panel）。 */
function normalizeConfig(
  raw: unknown,
  type: DashboardPanelType,
): DashboardPanelConfig | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const record = raw as Record<string, unknown>;
  const config: DashboardPanelConfig = {};
  for (const key of ["unit", "subtitle", "yName", "xName"] as const) {
    if (typeof record[key] === "string") config[key] = record[key] as string;
  }
  if (type === "list" && record.limit !== undefined) {
    config.limit =
      typeof record.limit === "number" && Number.isFinite(record.limit)
        ? Math.min(20, Math.max(1, Math.round(record.limit)))
        : 20;
  }
  return Object.keys(config).length > 0 ? config : undefined;
}

// ── v2 归一（wire-chart-contract-v2）：全部宽容，非法项逐字段丢弃 ────────

function isAxisType(value: unknown): value is DashboardAxisType {
  return (
    typeof value === "string" &&
    (DASHBOARD_AXIS_TYPES as readonly string[]).includes(value)
  );
}

/** 非空字符串才收（列名不允许空串——空串既不能定位列也无法表达意图）。 */
function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

/** 字符串数组归一：只留非空字符串、去重保序；空则不落字段。 */
function normalizeStringList(raw: unknown): string[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: string[] = [];
  for (const entry of raw) {
    const name = nonEmptyString(entry);
    if (name !== undefined && !out.includes(name)) out.push(name);
  }
  return out.length > 0 ? out : undefined;
}

/**
 * 编码通道宽容归一：逐字段校验，非法字段丢弃；轴类型不在白名单回退
 * （xType 回 category、yType 回 value——与 v1 隐含行为一致）。
 * 归一后无任何字段则不落 encoding（面板照 v1 回退分支渲染）。
 */
export function normalizeEncoding(
  raw: unknown,
): DashboardPanelEncoding | undefined {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return undefined;
  }
  const record = raw as Record<string, unknown>;
  const encoding: DashboardPanelEncoding = {};
  const x = nonEmptyString(record.x);
  if (x !== undefined) encoding.x = x;
  const y = normalizeStringList(record.y);
  if (y !== undefined) encoding.y = y;
  const series = nonEmptyString(record.series);
  if (series !== undefined) encoding.series = series;
  const size = nonEmptyString(record.size);
  if (size !== undefined) encoding.size = size;
  // 轴类型：给值但不合法 → 回退默认（不丢编码通道本身）。
  if (record.xType !== undefined) {
    encoding.xType = isAxisType(record.xType) ? record.xType : "category";
  }
  if (record.yType !== undefined) {
    encoding.yType = isAxisType(record.yType) ? record.yType : "value";
  }
  const xName = nonEmptyString(record.xName);
  if (xName !== undefined) encoding.xName = xName;
  const yName = nonEmptyString(record.yName);
  if (yName !== undefined) encoding.yName = yName;
  // list 角色通道（wire-list-panel）：与图表通道同层归一，图表类型忽略。
  for (const role of [
    "title",
    "summary",
    "image",
    "time",
    "source",
    "link",
  ] as const) {
    const column = nonEmptyString(record[role]);
    if (column !== undefined) encoding[role] = column;
  }
  return Object.keys(encoding).length > 0 ? encoding : undefined;
}

/**
 * 列表面板角色归一（wire-list-panel）：title 必填且必须命中快照列，否则
 * 面板判坏（返回 null——没有标题列的卡片流没有意义）；可选角色列名不命中
 * 只丢该角色。快照缺失同样判坏：没有列就无从校验角色。
 */
export function normalizeListEncoding(
  encoding: DashboardPanelEncoding | undefined,
  snapshot: DashboardSnapshot | undefined,
): DashboardPanelEncoding | null {
  if (snapshot === undefined) return null;
  const title = encoding?.title;
  if (title === undefined || !snapshot.columns.includes(title)) return null;
  const next: DashboardPanelEncoding = { title };
  for (const role of ["summary", "image", "time", "source", "link"] as const) {
    const column = encoding?.[role];
    if (column !== undefined && snapshot.columns.includes(column)) {
      next[role] = column;
    }
  }
  return next;
}

const OVERLAY_LINE_STYLES = ["solid", "dashed", "dotted"] as const;

/** 坐标值：数字或非空字符串（类目名 / 时间字符串）。 */
function overlayCoord(value: unknown): string | number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return nonEmptyString(value);
}

/** 叠加层宽容归一：三类逐项校验，非法项丢弃；全空则不落字段。 */
export function normalizeOverlays(
  raw: unknown,
): DashboardPanelOverlays | undefined {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return undefined;
  }
  const record = raw as Record<string, unknown>;
  const overlays: DashboardPanelOverlays = {};

  if (Array.isArray(record.markLine)) {
    const lines: DashboardOverlayLine[] = [];
    for (const entry of record.markLine) {
      if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
        continue;
      }
      const item = entry as Record<string, unknown>;
      const value = overlayCoord(item.value);
      if (value === undefined) continue;
      const line: DashboardOverlayLine = { value };
      const label = nonEmptyString(item.label);
      if (label !== undefined) line.label = label;
      if (
        typeof item.lineStyle === "string" &&
        (OVERLAY_LINE_STYLES as readonly string[]).includes(item.lineStyle)
      ) {
        line.lineStyle = item.lineStyle as DashboardOverlayLine["lineStyle"];
      }
      if (item.axis === "x" || item.axis === "y") line.axis = item.axis;
      lines.push(line);
    }
    if (lines.length > 0) overlays.markLine = lines;
  }

  if (Array.isArray(record.markArea)) {
    const areas: DashboardOverlayArea[] = [];
    for (const entry of record.markArea) {
      if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
        continue;
      }
      const item = entry as Record<string, unknown>;
      const from = overlayCoord(item.from);
      const to = overlayCoord(item.to);
      if (from === undefined || to === undefined) continue;
      const area: DashboardOverlayArea = { from, to };
      const label = nonEmptyString(item.label);
      if (label !== undefined) area.label = label;
      areas.push(area);
    }
    if (areas.length > 0) overlays.markArea = areas;
  }

  if (Array.isArray(record.markPoint)) {
    const points: DashboardOverlayPoint[] = [];
    for (const entry of record.markPoint) {
      if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
        continue;
      }
      const item = entry as Record<string, unknown>;
      const x = overlayCoord(item.x);
      const value = overlayCoord(item.y);
      if (x === undefined || value === undefined) continue;
      const point: DashboardOverlayPoint = { x, y: value };
      const label = nonEmptyString(item.label);
      if (label !== undefined) point.label = label;
      points.push(point);
    }
    if (points.length > 0) overlays.markPoint = points;
  }

  return Object.keys(overlays).length > 0 ? overlays : undefined;
}

/**
 * 窗格宽容归一：逐项校验（非对象丢弃；encoding/overlays 各自归一；
 * height 只收正整数）。不足两个合法窗格即视为无多窗格语义（不落字段）——
 * 单窗格与不收 panes 在渲染上完全等价，落字段反而扩大存储面。
 */
export function normalizePanes(raw: unknown): DashboardPanelPane[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const panes: DashboardPanelPane[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      continue;
    }
    const record = entry as Record<string, unknown>;
    const pane: DashboardPanelPane = {};
    const title = nonEmptyString(record.title);
    if (title !== undefined) pane.title = title;
    const encoding = normalizeEncoding(record.encoding);
    if (encoding !== undefined) pane.encoding = encoding;
    const overlays = normalizeOverlays(record.overlays);
    if (overlays !== undefined) pane.overlays = overlays;
    if (
      typeof record.height === "number" &&
      Number.isInteger(record.height) &&
      record.height > 0
    ) {
      pane.height = record.height;
    }
    panes.push(pane);
  }
  return panes.length >= 2 ? panes : undefined;
}

/**
 * raw_option 可序列化校验（深度遍历）：检出函数、正则、类实例（非纯对象/
 * 数组/原始值）即判不可序列化。返回首个问题的路径描述，null 表示通过。
 *
 * 为什么要查而不是直接 JSON.stringify：`JSON.stringify` 会把函数静默丢成
 * undefined、把正则变成 `{}`——用户面对的是一张空白图而非可诊断的错误。
 * 成品看板里确有 `"(v) => v"` 这类字符串（JSON 解析出来是字符串，不是
 * 函数），它们是合法载荷；真正不可接受的只有解析后仍是非原始值的东西。
 */
function findUnserializable(value: unknown, path: string): string | null {
  if (value === null) return null;
  const kind = typeof value;
  if (kind === "string" || kind === "number" || kind === "boolean") return null;
  if (kind === "undefined") return null;
  if (kind === "function") return `${path} 是函数`;
  if (kind === "symbol" || kind === "bigint") return `${path} 是 ${kind}`;
  if (value instanceof RegExp) return `${path} 是正则`;
  if (value instanceof Date) return `${path} 是 Date`;
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      const problem = findUnserializable(value[i], `${path}[${i}]`);
      if (problem) return problem;
    }
    return null;
  }
  if (kind === "object") {
    const proto = Object.getPrototypeOf(value);
    // 只接受纯对象与 null 原型；类实例（Map/Set/自定义类）一律拒绝。
    if (proto !== Object.prototype && proto !== null) {
      return `${path} 不是纯对象`;
    }
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      const problem = findUnserializable(item, `${path}.${key}`);
      if (problem) return problem;
    }
    return null;
  }
  return `${path} 类型不受支持`;
}

/**
 * raw_option 归一：非对象不落；含不可序列化值返回 null 由调用方判坏面板；
 * 通过则原样收下（内容的语义正确性归渲染层，解析层只保证可安全持有）。
 */
export function normalizeRawOption(raw: unknown): Record<string, unknown> | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  if (findUnserializable(raw, "raw_option") !== null) return null;
  return raw as Record<string, unknown>;
}

/** 查询过滤算子白名单（与数据面 query 端点入参同界）。 */
const DASHBOARD_QUERY_FILTER_OPS = [
  "eq",
  "in",
  "gt",
  "gte",
  "lt",
  "lte",
  "contains",
  "startswith",
] as const;
export type DashboardQueryFilterOp = (typeof DASHBOARD_QUERY_FILTER_OPS)[number];

/** 契约过滤项：column 非空 + 白名单算子；value 原样透传（不猜类型）。 */
export interface DashboardQueryFilter {
  column: string;
  op: DashboardQueryFilterOp;
  value: unknown;
}

/** 契约排序项：column 非空 + 方向白名单 asc/desc。 */
export interface DashboardQuerySort {
  column: string;
  dir: "asc" | "desc";
}

/**
 * 信息流来源契约引用（wire-analysis-console）：list 面板刷新时按
 * `(published_at desc, item_id desc)` 从 fd-info 重取，映射进 snapshot。
 * 与数据集 query 相互独立：面板可以只带其一，也可以都不带。
 */
export interface DashboardInfoQuery {
  /** 来源过滤（如 cninfo / cn-gov / sse-interaction）。 */
  sources?: string[];
  /** 来源类型过滤（announcement | ministry | interaction）。 */
  source_types?: string[];
  /** 发布时间下界（YYYY-MM-DD）。 */
  published_after?: string;
  /** 行数上限 1..200；非法回退 20。 */
  limit?: number;
}

/** 来源不可用时的滞后标注（服务端刷新写入）。 */
export interface DashboardInfoLag {
  at: string;
  reason: string;
}

/**
 * 面板查询契约引用（wire-dashboard-query-contract）：面板刷新的确定性取数
 * 契约（与服务端 parse_panel_query 同形同界）——渲染数据源仍是 snapshot，
 * query 不参与渲染。携带 query 的面板刷新走契约路径（零 LLM）。
 */
export interface DashboardPanelQuery {
  /** 目标数据集 id（非空——缺失/空则整个 query 丢弃）。 */
  dataset_id: string;
  filters?: DashboardQueryFilter[];
  sort?: DashboardQuerySort[];
  /** 行数上限 1..1000；非法（非整数/越界）回退 100。 */
  limit?: number;
  /** 投影列序（即刷新后快照列序）；字符串过滤、去重保序。 */
  columns?: string[];
}

/** filters 宽容归一：非数组不落字段；逐项校验（column 非空 + 算子白名单），
 * 非法项丢弃；归一后为空（全不合法/空数组）同样不落字段。 */
function normalizeQueryFilters(raw: unknown): DashboardQueryFilter[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const filters: DashboardQueryFilter[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) continue;
    const record = entry as Record<string, unknown>;
    if (typeof record.column !== "string" || !record.column) continue;
    if (
      typeof record.op !== "string" ||
      !(DASHBOARD_QUERY_FILTER_OPS as readonly string[]).includes(record.op)
    ) {
      continue;
    }
    filters.push({
      column: record.column,
      op: record.op as DashboardQueryFilterOp,
      value: record.value,
    });
  }
  return filters.length > 0 ? filters : undefined;
}

/** sort 宽容归一：与 filters 同界（column 非空 + dir ∈ asc/desc，逐项丢弃）。 */
function normalizeQuerySort(raw: unknown): DashboardQuerySort[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const sort: DashboardQuerySort[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) continue;
    const record = entry as Record<string, unknown>;
    if (typeof record.column !== "string" || !record.column) continue;
    if (record.dir !== "asc" && record.dir !== "desc") continue;
    sort.push({ column: record.column, dir: record.dir });
  }
  return sort.length > 0 ? sort : undefined;
}

/** columns 宽容归一：非数组不落字段；只留字符串、去重保序；空则不落字段。 */
function normalizeQueryColumns(raw: unknown): string[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const seen = new Set<string>();
  for (const entry of raw) {
    if (typeof entry === "string") seen.add(entry);
  }
  return seen.size > 0 ? [...seen] : undefined;
}

/**
 * query 宽容归一（与服务端同界）：顶层非对象（含数组/null）或 dataset_id
 * 缺失/非字符串/空串 → undefined（整体丢弃，面板照常按 snapshot 渲染）；
 * filters/sort 逐项校验丢弃（全不合法则字段不落）；limit 给出但非法（非
 * 整数或越界 1..1000）回退 100、未给出不落；columns 只留字符串去重保序。
 */
export function normalizePanelQuery(raw: unknown): DashboardPanelQuery | undefined {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return undefined;
  const record = raw as Record<string, unknown>;
  if (typeof record.dataset_id !== "string" || !record.dataset_id) return undefined;

  const filters = normalizeQueryFilters(record.filters);
  const sort = normalizeQuerySort(record.sort);
  const columns = normalizeQueryColumns(record.columns);
  // limit：未给出不落字段；给出但非法（非数字/非整数/越界）回退 100。
  let limit: number | undefined;
  if (record.limit !== undefined) {
    limit =
      typeof record.limit === "number" &&
      Number.isInteger(record.limit) &&
      record.limit >= 1 &&
      record.limit <= 1000
        ? record.limit
        : 100;
  }

  return {
    dataset_id: record.dataset_id,
    ...(filters !== undefined ? { filters } : {}),
    ...(sort !== undefined ? { sort } : {}),
    ...(limit !== undefined ? { limit } : {}),
    ...(columns !== undefined ? { columns } : {}),
  };
}

/** info_query 宽容归一：非对象整体丢弃；sources/source_types 只留非空串；
 * published_after 必须是 YYYY-MM-DD；limit 钳到 1..200，非法回退 20。 */
export function normalizeInfoQuery(raw: unknown): DashboardInfoQuery | undefined {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return undefined;
  const record = raw as Record<string, unknown>;
  const strings = (value: unknown): string[] | undefined => {
    if (!Array.isArray(value)) return undefined;
    const kept = value.filter((v): v is string => typeof v === "string" && v.length > 0);
    return kept.length > 0 ? [...new Set(kept)] : undefined;
  };
  const sources = strings(record.sources);
  const sourceTypes = strings(record.source_types);
  const publishedAfter =
    typeof record.published_after === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(record.published_after)
      ? record.published_after
      : undefined;
  const limit =
    typeof record.limit === "number" &&
    Number.isInteger(record.limit) &&
    record.limit >= 1 &&
    record.limit <= 200
      ? record.limit
      : 20;
  return {
    ...(sources !== undefined ? { sources } : {}),
    ...(sourceTypes !== undefined ? { source_types: sourceTypes } : {}),
    ...(publishedAfter !== undefined ? { published_after: publishedAfter } : {}),
    limit,
  };
}

/** info_lag 宽容归一：at/reason 都必须是字符串，否则不落字段。 */
export function normalizeInfoLag(raw: unknown): DashboardInfoLag | undefined {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return undefined;
  const record = raw as Record<string, unknown>;
  if (typeof record.at !== "string" || typeof record.reason !== "string") return undefined;
  return { at: record.at, reason: record.reason };
}

/**
 * 宽容解析仪表盘 spec：接受产物信封（`{title, spec:{title, panels}}`）或
 * 本体（`{title, panels}`）。内层无 panels 数组 → legacy 态（旧自由
 * JSON，调用方整体只读呈现）；有 panels 数组 → v1 态，逐面板校验：
 * 非 JSON 对象 / type 不在枚举 → 收集进 badPanels 跳过；id 缺失自动补、
 * span 非法回退 1、snapshot/config 归一（不拒收）、query 宽容归一（非法
 * 整体丢弃——面板仍按 snapshot 渲染，不受影响）。
 *
 * v2（wire-chart-contract-v2）：encoding/panes/overlays/height/raw_option
 * 逐项宽容归一，非法整体或逐项丢弃但不拒收面板；唯 raw_option 含不可
 * 序列化值时**整面板**记 bad_raw_option 跳过（该配置无法安全持有，且
 * 静默降级会让用户面对空白图而无从诊断）。
 */
export function parseDashboardSpec(raw: unknown): DashboardParseResult {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { kind: "legacy", raw: {} };
  }
  const envelope = raw as { title?: unknown; spec?: unknown };
  // 信封解包：spec 字段是对象则视作本体所在；否则把整体当本体尝试。
  const body: Record<string, unknown> =
    typeof envelope.spec === "object" && envelope.spec !== null
      ? (envelope.spec as Record<string, unknown>)
      : (raw as Record<string, unknown>);

  if (!Array.isArray(body.panels)) {
    return { kind: "legacy", raw: raw as Record<string, unknown> };
  }

  const usedIds = new Set<string>();
  const panels: DashboardPanel[] = [];
  const badPanels: DashboardBadPanel[] = [];
  body.panels.forEach((entry, index) => {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      badPanels.push({ index, reason: "not_object", raw: entry });
      return;
    }
    const record = entry as Record<string, unknown>;
    if (!isPanelType(record.type)) {
      badPanels.push({ index, reason: "bad_type", raw: entry });
      return;
    }
    // raw_option 先校验：给出但不可序列化 → 整面板跳过（不静默降级）。
    let rawOption: Record<string, unknown> | undefined;
    if (record.raw_option !== undefined) {
      const normalized = normalizeRawOption(record.raw_option);
      if (normalized === null) {
        badPanels.push({ index, reason: "bad_raw_option", raw: entry });
        return;
      }
      rawOption = normalized;
    }
    const title = typeof record.title === "string" ? record.title : "";
    // id 缺失/非字符串：自动补稳定 id（不拒收——AI 草稿常缺 id）。
    let id = typeof record.id === "string" && record.id ? record.id : "";
    if (!id) id = generatePanelId(usedIds);
    usedIds.add(id);
    const span = (
      DASHBOARD_PANEL_SPANS as readonly number[]
    ).includes(record.span as number)
      ? (record.span as DashboardPanelSpan)
      : 1;
    const snapshot = normalizeSnapshot(record.snapshot);
    const config = normalizeConfig(record.config, record.type);
    const source = typeof record.source === "string" ? record.source : undefined;
    const query = normalizePanelQuery(record.query);
    const infoQuery =
      record.type === "list" ? normalizeInfoQuery(record.info_query) : undefined;
    const infoLag =
      record.type === "list" ? normalizeInfoLag(record.info_lag) : undefined;
    // v2 字段（缺省不落字段——v1 面板解析后的对象形状与升级前逐字段一致）
    let encoding = normalizeEncoding(record.encoding);
    // list（wire-list-panel）：title 必填且须命中快照列，否则整面板判坏
    // （与 raw_option 同理——没有标题列的卡片流无从渲染，静默降级更糟）。
    if (record.type === "list") {
      const listEncoding = normalizeListEncoding(encoding, snapshot);
      if (listEncoding === null) {
        badPanels.push({ index, reason: "bad_list_encoding", raw: entry });
        return;
      }
      encoding = listEncoding;
    }
    const panes = normalizePanes(record.panes);
    const overlays = normalizeOverlays(record.overlays);
    const height =
      typeof record.height === "number" &&
      Number.isFinite(record.height) &&
      record.height > 0
        ? Math.round(record.height)
        : undefined;
    panels.push({
      id,
      type: record.type,
      title,
      span,
      ...(source !== undefined ? { source } : {}),
      ...(snapshot !== undefined ? { snapshot } : {}),
      ...(config !== undefined ? { config } : {}),
      ...(query !== undefined ? { query } : {}),
      ...(infoQuery !== undefined ? { info_query: infoQuery } : {}),
      ...(infoLag !== undefined ? { info_lag: infoLag } : {}),
      ...(encoding !== undefined ? { encoding } : {}),
      ...(panes !== undefined ? { panes } : {}),
      ...(overlays !== undefined ? { overlays } : {}),
      ...(height !== undefined ? { height } : {}),
      ...(rawOption !== undefined ? { raw_option: rawOption } : {}),
    });
  });

  const title =
    typeof body.title === "string"
      ? body.title
      : typeof envelope.title === "string"
        ? envelope.title
        : "";
  return { kind: "v1", title, panels, badPanels };
}

/** 面板是否为 v2 形态（携带任一 v2 专有字段）。渲染层据此判定是否走
 *  v1 回退分支；编辑器的 raw_option 只读分支同样以此为准。 */
export function isV2Panel(panel: DashboardPanel): boolean {
  return (
    panel.encoding !== undefined ||
    panel.panes !== undefined ||
    panel.overlays !== undefined ||
    panel.height !== undefined ||
    panel.raw_option !== undefined
  );
}


/** 面板 id 生成器：`p1`、`p2`… 跳过已占用值（编辑器增删移序用）。 */
export function generatePanelId(existing: Iterable<string>): string {
  const used = new Set(existing);
  let seq = 1;
  while (used.has(`p${seq}`)) seq += 1;
  return `p${seq}`;
}

/** 不可变移序（编辑器左移/右移）：把 from 项移动到 to（越界回弹原地）。 */
export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (
    from < 0 ||
    from >= items.length ||
    to < 0 ||
    to >= items.length ||
    from === to
  ) {
    return items;
  }
  const next = [...items];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved!);
  return next;
}

/**
 * 各类型的新面板起始模板（编辑器「添加面板」用；数据留空由用户/预览如实
 * 呈现）。
 *
 * 基础档沿用 v1 模板（形状与升级前逐字段一致）。高级档（v2）给最小可渲染
 * 骨架：带 encoding 的图表类给类目轴 + 单系列两列快照；饼/仪表/漏斗等单维
 * 类型给类目+数值；K 线给日期+四价空表（用户/绑定后填数）。
 */
export function panelTemplate(
  type: DashboardPanelType,
  existingIds: Iterable<string>,
): DashboardPanel {
  const base = {
    id: generatePanelId(existingIds),
    type,
    title: "",
    span: 1 as DashboardPanelSpan,
  };
  switch (type) {
    case "kpi":
      // kpi 值存 snapshot.rows[0][0]（编辑表单的读写锚点）。
      return {
        ...base,
        snapshot: { columns: ["value"], rows: [[0]] },
        config: { unit: "", subtitle: "" },
      };
    case "line":
    case "bar":
      // 第一列当 x 轴、其余列为系列；空行 → 预览「无数据」如实呈现。
      return {
        ...base,
        snapshot: { columns: ["x", "y"], rows: [] },
      };
    case "table":
      return {
        ...base,
        snapshot: { columns: ["列一", "列二"], rows: [] },
      };
    case "list":
      // 图文信息流骨架（wire-list-panel）：标题 + 摘要两列空表；title 角色
      // 必填（解析层据此判合法），用户绑定数据后按列改名。
      return {
        ...base,
        snapshot: { columns: ["title", "summary"], rows: [] },
        encoding: { title: "title", summary: "summary" },
        config: { limit: 20 },
      };
    case "candlestick":
      // 日期 + 开高低收（K 线标准四价；渲染层按此顺序取）。
      return {
        ...base,
        snapshot: {
          columns: ["日期", "开盘", "最高", "最低", "收盘"],
          rows: [],
        },
        encoding: { x: "日期", xType: "time", yType: "value" },
      };
    case "scatter":
      return {
        ...base,
        snapshot: { columns: ["x", "y"], rows: [] },
        encoding: { x: "x", xType: "value", y: ["y"], yType: "value" },
      };
    case "pie":
    case "funnel":
      // 单维占比：类目 + 数值。
      return {
        ...base,
        snapshot: { columns: ["类目", "数值"], rows: [] },
        encoding: { x: "类目", y: ["数值"] },
      };
    case "gauge":
      // 仪表盘只有一个值；快照仍两列以复用统一形状。
      return {
        ...base,
        snapshot: { columns: ["指标", "数值"], rows: [] },
        encoding: { x: "指标", y: ["数值"] },
      };
    case "heatmap":
      return {
        ...base,
        snapshot: { columns: ["x", "y", "值"], rows: [] },
        encoding: { x: "x", y: ["值"], series: "y" },
      };
    case "radar":
      return {
        ...base,
        snapshot: { columns: ["维度", "数值"], rows: [] },
        encoding: { x: "维度", y: ["数值"] },
      };
    case "treemap":
      return {
        ...base,
        snapshot: { columns: ["节点", "数值"], rows: [] },
        encoding: { x: "节点", y: ["数值"], size: "数值" },
      };
    case "sankey":
      // 两列即两端节点 + 流量（源→目标）。
      return {
        ...base,
        snapshot: { columns: ["源", "目标", "流量"], rows: [] },
        encoding: { x: "源", series: "目标", size: "流量" },
      };
    case "boxplot":
      // 箱线图五数概括：类目 + 最小值/下四分位/中位/上四分位/最大值。
      return {
        ...base,
        snapshot: {
          columns: ["类目", "min", "q1", "median", "q3", "max"],
          rows: [],
        },
        encoding: { x: "类目" },
      };
  }
}

/** 信封组装：编辑器保存时把 v1 本体放回产物信封（服务端最小校验口径）。 */
export function buildArtifactEnvelope(spec: DashboardSpecV1): {
  title: string;
  spec: DashboardSpecV1;
} {
  return { title: spec.title, spec: { title: spec.title, panels: spec.panels } };
}

/** convertPanelType 结果：转换后的面板 + kpi↔图表互转的确认标记。 */
export interface ConvertPanelResult {
  panel: DashboardPanel;
  /**
   * kpi 与图表类互转时为 true——kpi 只读 snapshot 首值、图表读整表，
   * 两个方向都有信息取舍，调用方须先让用户确认再落地。
   */
  needConfirm: boolean;
}

/**
 * 面板类型转换（编辑器 v2）：snapshot 原样保留（面板共用同一 snapshot
 * 形状，line↔bar↔table↔list 互转零数据损失），仅换 type；同类型转换为
 * 幂等原样返回。kpi↔图表类互转、以及 list↔图表类互转带 needConfirm 标记
 * （kpi 只读首值、list 按角色取列，两个方向都有信息取舍；list↔table 是
 * 同源表格语义，无需确认）。不可变：原面板不动，返回新对象。
 */
export function convertPanelType(
  panel: DashboardPanel,
  target: DashboardPanelType,
): ConvertPanelResult {
  if (panel.type === target) return { panel, needConfirm: false };
  const needConfirm =
    isKpi(panel.type) !== isKpi(target) ||
    (isList(panel.type) && isChartType(target)) ||
    (isList(target) && isChartType(panel.type));
  const next: DashboardPanel = { ...panel, type: target };
  if (isList(target)) {
    // 转入 list：title 角色取快照首列（无快照给占位名），摘要取次列；
    // 其余列表角色留空由用户在编辑器补齐。
    const columns = panel.snapshot?.columns ?? [];
    next.encoding = {
      ...(stripListRoles(panel.encoding) ?? {}),
      title: columns[0] ?? "title",
      ...(columns[1] !== undefined ? { summary: columns[1] } : {}),
    };
    next.config = { ...panel.config, limit: panel.config?.limit ?? 20 };
  } else if (isList(panel.type)) {
    // 转出 list：剥离列表角色与 limit，图表/表格字段原样保留。
    const stripped = stripListRoles(panel.encoding);
    if (stripped === undefined) delete next.encoding;
    else next.encoding = stripped;
    if (next.config !== undefined) {
      const config = { ...next.config };
      delete config.limit;
      if (Object.keys(config).length > 0) next.config = config;
      else delete next.config;
    }
  }
  return { panel: next, needConfirm };
}

function isKpi(type: DashboardPanelType): boolean {
  return type === "kpi";
}

function isList(type: DashboardPanelType): boolean {
  return type === "list";
}

/** 走 echarts 的类型（kpi/table/list 之外的都算图表类）。 */
function isChartType(type: DashboardPanelType): boolean {
  return type !== "kpi" && type !== "table" && type !== "list";
}

const LIST_ROLES = ["title", "summary", "image", "time", "source", "link"] as const;

/** 剥离 list 角色通道，返回剩余图表编码（空则 undefined）。 */
function stripListRoles(
  encoding: DashboardPanelEncoding | undefined,
): DashboardPanelEncoding | undefined {
  if (encoding === undefined) return undefined;
  const next = { ...encoding };
  for (const role of LIST_ROLES) delete next[role];
  return Object.keys(next).length > 0 ? next : undefined;
}

/**
 * 面板复制（编辑器 v2）：新 id（generatePanelId 避开既有占用）、标题加
 * 副本后缀、snapshot/config/query 深拷贝（structuredClone——副本的后续编辑
 * 绝不波及原件；query 引用同样完整保留，wire-dashboard-query-contract）。
 * existingIds 缺省只避开原面板自身 id；编辑器调用时应传入
 * 当前全部面板 id 以杜绝碰撞。
 */
export function duplicatePanel(
  panel: DashboardPanel,
  existingIds: Iterable<string> = [],
  titleSuffix = "副本",
): DashboardPanel {
  const used = new Set([...existingIds]);
  used.add(panel.id);
  const copy = structuredClone(panel);
  return {
    ...copy,
    id: generatePanelId(used),
    title: `${panel.title}${titleSuffix}`,
  };
}
