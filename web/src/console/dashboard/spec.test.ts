// 仪表盘 spec v1 契约测试（wire-dashboard-renderer 任务 1.1/1.2）：
// 宽容校验三态（合法 v1 / 含非法面板的 v1 / 旧自由 JSON legacy）+
// id 生成器 / 移序 / 面板模板 / 信封组装。
// wire-dashboard-query-contract 任务 2.1/2.4：面板 query 契约——宽容归一
// （合法/非法/limit 回退/列去重保序）、parse 挂载与非法丢弃、编辑 helpers
// （duplicate/convert）保留 query。
import { describe, expect, it } from "vitest";
import {
  buildArtifactEnvelope,
  convertPanelType,
  duplicatePanel,
  generatePanelId,
  isV2Panel,
  moveItem,
  normalizeEncoding,
  normalizeListEncoding,
  normalizeOverlays,
  normalizePanelQuery,
  normalizePanes,
  normalizeRawOption,
  parseDashboardSpec,
  panelTemplate,
  DASHBOARD_ADVANCED_PANEL_TYPES,
  DASHBOARD_BASE_PANEL_TYPES,
  DASHBOARD_PANEL_TYPES,
  type DashboardPanel,
  type DashboardPanelQuery,
} from "./spec";

/** v1 本体（编辑器预览 / 内层直给形态）。 */
const V1_BODY = {
  title: "宏观仪表盘",
  panels: [
    {
      id: "k1",
      type: "kpi",
      title: "CPI 同比",
      span: 1,
      source: "CN_CPI_YOY",
      snapshot: { columns: ["value"], rows: [[2.1]] },
      config: { unit: "%", subtitle: "2026-09" },
    },
    {
      id: "l1",
      type: "line",
      title: "PMI 走势",
      span: 2,
      snapshot: {
        columns: ["月份", "PMI"],
        rows: [
          ["7月", 49.4],
          ["8月", 49.1],
        ],
      },
    },
  ],
};

describe("parseDashboardSpec（宽容校验三态）", () => {
  it("合法 v1：信封与本体两种输入都解析为 v1 态", () => {
    // 信封（产物详情 spec 原样）
    const fromEnvelope = parseDashboardSpec({
      title: "宏观仪表盘",
      spec: V1_BODY,
    });
    expect(fromEnvelope.kind).toBe("v1");
    if (fromEnvelope.kind !== "v1") return;
    expect(fromEnvelope.title).toBe("宏观仪表盘");
    expect(fromEnvelope.panels).toHaveLength(2);
    expect(fromEnvelope.badPanels).toEqual([]);
    expect(fromEnvelope.panels[0]).toEqual(V1_BODY.panels[0]);

    // 本体直给（编辑器实时预览）
    const fromBody = parseDashboardSpec(V1_BODY);
    expect(fromBody.kind).toBe("v1");
    if (fromBody.kind !== "v1") return;
    expect(fromBody.panels).toHaveLength(2);
    expect(fromBody.title).toBe("宏观仪表盘");
  });

  it("非法面板收集跳过、合法面板不受牵连（不整板失败）", () => {
    const parsed = parseDashboardSpec({
      spec: {
        panels: [
          V1_BODY.panels[0],
          { type: "waterfall", title: "坏类型" }, // type 不在枚举
          "只是一个字符串", // 非 JSON 对象
          null,
        ],
      },
    });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    expect(parsed.panels).toHaveLength(1);
    expect(parsed.panels[0]?.id).toBe("k1");
    expect(parsed.badPanels.map((b) => b.reason)).toEqual([
      "bad_type",
      "not_object",
      "not_object",
    ]);
    expect(parsed.badPanels[0]?.index).toBe(1);
    expect(parsed.badPanels[0]?.raw).toEqual({ type: "waterfall", title: "坏类型" });
  });

  it("宽容归一：缺 id 自动补、span 非法回退 1、snapshot 列行数不齐截断补 null", () => {
    const parsed = parseDashboardSpec({
      spec: {
        panels: [
          {
            // 缺 id / snapshot 行比列长 / 格子类型越界（bool、对象）
            type: "table",
            title: "明细",
            span: 9,
            snapshot: {
              columns: ["月份", 42, "值"],
              rows: [
                ["7月", true, 1, "多余列"],
                ["8月"],
                "不是数组行",
                [null, 2, { x: 1 }],
              ],
            },
            config: { unit: "%", yName: 123, subtitle: "注" },
          },
        ],
      },
    });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    const panel = parsed.panels[0] as DashboardPanel;
    expect(panel.id).toBe("p1"); // 自动补 id
    expect(panel.span).toBe(1); // 非法 span 回退
    expect(panel.snapshot?.columns).toEqual(["月份", "值"]); // 非字符串列过滤
    expect(panel.snapshot?.rows).toEqual([
      ["7月", null], // 截断到列数 + 坏格子置 null
      ["8月", null], // 短行补 null
      [null, 2], // 非数组行整行跳过；对象格子置 null
    ]);
    expect(panel.config).toEqual({ unit: "%", subtitle: "注" }); // 非字符串字段丢弃
  });

  it("旧自由 JSON（无 panels）与非对象输入 → legacy 态", () => {
    // 产物页历史种子形状：信封内层是自由 JSON
    const legacy = parseDashboardSpec({
      title: "宏观仪表盘",
      spec: { indicators: ["CN_CPI_YOY"], layout: "2x2" },
    });
    expect(legacy.kind).toBe("legacy");
    if (legacy.kind !== "legacy") return;
    expect(legacy.raw).toEqual({
      title: "宏观仪表盘",
      spec: { indicators: ["CN_CPI_YOY"], layout: "2x2" },
    });

    // panels 非数组 / 输入非对象：同样 legacy（不崩不拒）
    expect(parseDashboardSpec({ spec: { panels: "oops" } }).kind).toBe("legacy");
    expect(parseDashboardSpec(null).kind).toBe("legacy");
    expect(parseDashboardSpec([1, 2]).kind).toBe("legacy");
    expect(parseDashboardSpec("text").kind).toBe("legacy");
  });

  it("v1 标题回退：内层缺 title 时取信封 title，再缺为空串", () => {
    const parsed = parseDashboardSpec({
      title: "信封标题",
      spec: { panels: [] },
    });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    expect(parsed.title).toBe("信封标题");
    expect(parseDashboardSpec({ panels: [] })).toMatchObject({
      kind: "v1",
      title: "",
    });
  });
});

describe("编辑器工具（任务 1.2）", () => {
  it("generatePanelId：p1/p2 递增并跳过已占用 id", () => {
    expect(generatePanelId([])).toBe("p1");
    expect(generatePanelId(["p1"])).toBe("p2");
    expect(generatePanelId(["p1", "p2", "p4"])).toBe("p3");
  });

  it("moveItem：不可变移序 + 越界回弹原地", () => {
    const items = ["a", "b", "c"];
    expect(moveItem(items, 1, 0)).toEqual(["b", "a", "c"]);
    expect(moveItem(items, 0, 2)).toEqual(["b", "c", "a"]);
    expect(moveItem(items, 2, 2)).toEqual(items);
    expect(moveItem(items, 0, 9)).toEqual(items);
    expect(moveItem(items, -1, 0)).toEqual(items);
    expect(items).toEqual(["a", "b", "c"]); // 原数组不动
  });

  it("panelTemplate：各类型起始模板（kpi 值锚在 rows[0][0]）", () => {
    const existing = ["p1", "p2"];
    const kpi = panelTemplate("kpi", existing);
    expect(kpi.id).toBe("p3");
    expect(kpi.type).toBe("kpi");
    expect(kpi.span).toBe(1);
    expect(kpi.snapshot).toEqual({ columns: ["value"], rows: [[0]] });
    expect(kpi.config).toEqual({ unit: "", subtitle: "" });

    const line = panelTemplate("line", existing);
    expect(line.snapshot?.columns).toEqual(["x", "y"]);
    expect(line.snapshot?.rows).toEqual([]);

    expect(panelTemplate("bar", [])?.snapshot?.columns).toEqual(["x", "y"]);
    expect(panelTemplate("table", [])?.snapshot?.columns).toEqual(["列一", "列二"]);
  });

  it("buildArtifactEnvelope：v1 本体放回产物信封（服务端最小校验口径）", () => {
    const spec = {
      title: "宏观仪表盘",
      panels: [V1_BODY.panels[0] as DashboardPanel],
    };
    expect(buildArtifactEnvelope(spec)).toEqual({
      title: "宏观仪表盘",
      spec: { title: "宏观仪表盘", panels: spec.panels },
    });
  });
});

describe("编辑器 v2 工具（wire-dashboard-editor-v2 任务 1.1）", () => {
  it("convertPanelType：line↔bar↔table 互转保 snapshot/config 零损失、needConfirm=false", () => {
    const line = V1_BODY.panels[1] as DashboardPanel;
    const result = convertPanelType(line, "table");
    expect(result.needConfirm).toBe(false);
    expect(result.panel).toEqual({ ...line, type: "table" }); // 仅换 type
    expect(result.panel.snapshot).toEqual(line.snapshot); // 数据原样
    // 不可变：原面板不动
    expect(line.type).toBe("line");

    const withConfig = { ...line, config: { xName: "月份", yName: "PMI" } };
    expect(convertPanelType(withConfig, "bar").panel).toEqual({
      ...withConfig,
      type: "bar",
    });
  });

  it("convertPanelType：kpi↔图表类互转 needConfirm=true（两个方向）", () => {
    const kpi = V1_BODY.panels[0] as DashboardPanel;
    expect(convertPanelType(kpi, "line").needConfirm).toBe(true);
    expect(convertPanelType(kpi, "table").needConfirm).toBe(true);

    const chart = V1_BODY.panels[1] as DashboardPanel;
    const intoKpi = convertPanelType(chart, "kpi");
    expect(intoKpi.needConfirm).toBe(true);
    // snapshot 仍原样保留（渲染层 kpi 只读首值——取舍由确认对话框交代）
    expect(intoKpi.panel.snapshot).toEqual(chart.snapshot);
  });

  it("convertPanelType：同类型转换为幂等原样返回（needConfirm=false）", () => {
    const kpi = V1_BODY.panels[0] as DashboardPanel;
    const same = convertPanelType(kpi, "kpi");
    expect(same.needConfirm).toBe(false);
    expect(same.panel).toBe(kpi); // 原对象原样返回
  });

  it("duplicatePanel：新 id（避开占用）+ 副本标题 + snapshot/config 深拷贝", () => {
    const kpi = V1_BODY.panels[0] as DashboardPanel;
    const copy = duplicatePanel(kpi);
    expect(copy.id).not.toBe(kpi.id); // 缺省避开原面板自身 id
    expect(copy.id).toBe("p1"); // generatePanelId 口径
    expect(copy.title).toBe("CPI 同比副本"); // 默认「副本」后缀
    expect(copy.type).toBe("kpi");
    expect(copy.snapshot).toEqual(kpi.snapshot);
    expect(copy.config).toEqual(kpi.config);

    // 深拷贝：改副本数据不波及原件
    copy.snapshot?.rows.push([9.9]);
    if (copy.config) copy.config.unit = "‰";
    expect(kpi.snapshot?.rows).toEqual([[2.1]]);
    expect(kpi.config?.unit).toBe("%");

    // 传入既有 id 集合时避开占用；后缀可指定（i18n）
    expect(duplicatePanel(kpi, ["p1"]).id).toBe("p2");
    expect(duplicatePanel(kpi, ["p1", "p2"], " (copy)").title).toBe(
      "CPI 同比 (copy)",
    );
  });
});

describe("面板 query 契约（wire-dashboard-query-contract 任务 2.1）", () => {
  /** 合法全字段 query（kpi 单值约定：sort 到最近一行 + limit 1 + 投影值列）。 */
  const VALID_QUERY: DashboardPanelQuery = {
    dataset_id: "ds_coal_price",
    filters: [{ column: "city", op: "eq", value: "秦皇岛" }],
    sort: [{ column: "date", dir: "desc" }],
    limit: 1,
    columns: ["price"],
  };

  it("合法 query 原样归一（全字段；value 原样透传不猜类型）", () => {
    expect(normalizePanelQuery(VALID_QUERY)).toEqual(VALID_QUERY);
    // 只带 dataset_id：可选字段缺省不落
    expect(normalizePanelQuery({ dataset_id: "ds_x" })).toEqual({ dataset_id: "ds_x" });
    // value 为任意 JSON 值（数组/对象/布尔）均透传
    expect(
      normalizePanelQuery({
        dataset_id: "ds_x",
        filters: [
          { column: "tags", op: "in", value: ["a", "b"] },
          { column: "ok", op: "eq", value: true },
        ],
      }),
    ).toEqual({
      dataset_id: "ds_x",
      filters: [
        { column: "tags", op: "in", value: ["a", "b"] },
        { column: "ok", op: "eq", value: true },
      ],
    });
  });

  it("顶层非对象（含数组/null）或 dataset_id 缺失/非字符串/空串 → undefined", () => {
    expect(normalizePanelQuery(undefined)).toBeUndefined();
    expect(normalizePanelQuery(null)).toBeUndefined();
    expect(normalizePanelQuery("query")).toBeUndefined();
    expect(normalizePanelQuery(42)).toBeUndefined();
    expect(normalizePanelQuery([])).toBeUndefined();
    expect(normalizePanelQuery({ filters: [] })).toBeUndefined(); // 缺 dataset_id
    expect(normalizePanelQuery({ dataset_id: "" })).toBeUndefined(); // 空串
    expect(normalizePanelQuery({ dataset_id: 123 })).toBeUndefined(); // 非字符串
    expect(normalizePanelQuery({ dataset_id: "  " })).toEqual({ dataset_id: "  " }); // 仅要求非空，不 trim
  });

  it("filters 宽容归一：非法项丢弃（算子白名单/空 column/非对象项），全不合法不落字段", () => {
    expect(
      normalizePanelQuery({
        dataset_id: "ds_x",
        filters: [
          { column: "city", op: "between", value: [1, 2] }, // 算子不在白名单 → 丢弃
          { column: "", op: "eq", value: 1 }, // 空 column → 丢弃
          { column: "date", op: "regex", value: ".*" }, // 丢弃
          "只是一个字符串", // 非对象项 → 丢弃
          null,
          { column: "city", op: "gte", value: 500 }, // 合法 → 保留
          { column: "name", op: "contains", value: "煤" }, // 合法 → 保留
        ],
      }),
    ).toEqual({
      dataset_id: "ds_x",
      filters: [
        { column: "city", op: "gte", value: 500 },
        { column: "name", op: "contains", value: "煤" },
      ],
    });
    // 全不合法 → 字段不落（query 本体仍保留 dataset_id）
    expect(
      normalizePanelQuery({ dataset_id: "ds_x", filters: [{ column: "c", op: "bad" }] }),
    ).toEqual({ dataset_id: "ds_x" });
    // 非数组 / 空数组 → 同样不落字段
    expect(normalizePanelQuery({ dataset_id: "ds_x", filters: "oops" })).toEqual({
      dataset_id: "ds_x",
    });
    expect(normalizePanelQuery({ dataset_id: "ds_x", filters: [] })).toEqual({
      dataset_id: "ds_x",
    });
  });

  it("sort 宽容归一：dir 不在 asc/desc 或空 column 丢弃；全不合法/非数组不落字段", () => {
    expect(
      normalizePanelQuery({
        dataset_id: "ds_x",
        sort: [
          { column: "date", dir: "DESC" }, // 方向白名单外 → 丢弃
          { column: "", dir: "asc" }, // 空 column → 丢弃
          { column: "price", dir: "desc" }, // 合法 → 保留
          "只是一个字符串",
        ],
      }),
    ).toEqual({ dataset_id: "ds_x", sort: [{ column: "price", dir: "desc" }] });
    expect(
      normalizePanelQuery({ dataset_id: "ds_x", sort: [{ column: "date", dir: "up" }] }),
    ).toEqual({ dataset_id: "ds_x" });
    expect(normalizePanelQuery({ dataset_id: "ds_x", sort: "oops" })).toEqual({
      dataset_id: "ds_x",
    });
    expect(normalizePanelQuery({ dataset_id: "ds_x", sort: [] })).toEqual({
      dataset_id: "ds_x",
    });
  });

  it("limit：非法（非数字/非整数/越界 1..1000）回退 100；合法保留；缺省不落", () => {
    for (const bad of [0, -5, 1001, 2000, 2.5, "50", null, NaN]) {
      expect(normalizePanelQuery({ dataset_id: "ds_x", limit: bad })).toEqual({
        dataset_id: "ds_x",
        limit: 100,
      });
    }
    expect(normalizePanelQuery({ dataset_id: "ds_x", limit: 1 })).toEqual({
      dataset_id: "ds_x",
      limit: 1,
    });
    expect(normalizePanelQuery({ dataset_id: "ds_x", limit: 1000 })).toEqual({
      dataset_id: "ds_x",
      limit: 1000,
    });
    expect(normalizePanelQuery({ dataset_id: "ds_x" })).toEqual({ dataset_id: "ds_x" }); // 未给出不落
  });

  it("columns：只留字符串、去重保序；非字符串丢弃；全非法/非数组/空不落字段", () => {
    expect(
      normalizePanelQuery({
        dataset_id: "ds_x",
        columns: ["价格", "日期", "价格", 42, null, "城市", true, "日期"],
      }),
    ).toEqual({ dataset_id: "ds_x", columns: ["价格", "日期", "城市"] });
    expect(
      normalizePanelQuery({ dataset_id: "ds_x", columns: [42, null, {}] }),
    ).toEqual({ dataset_id: "ds_x" }); // 全非法 → 不落
    expect(normalizePanelQuery({ dataset_id: "ds_x", columns: "oops" })).toEqual({
      dataset_id: "ds_x",
    });
    expect(normalizePanelQuery({ dataset_id: "ds_x", columns: [] })).toEqual({
      dataset_id: "ds_x",
    });
  });

  it("parseDashboardSpec：合法 query 挂载到面板；非法 query 整体丢弃（面板照常收下）", () => {
    const parsed = parseDashboardSpec({
      spec: {
        panels: [
          {
            id: "q1",
            type: "table",
            title: "契约面板",
            snapshot: { columns: ["日期", "价格"], rows: [["7月", 850]] },
            query: {
              dataset_id: "ds_coal",
              filters: [{ column: "c", op: "bad" }, { column: "city", op: "eq", value: 1 }],
              limit: 5000,
              columns: ["价格", "价格", 9],
            },
          },
          {
            id: "q2",
            type: "kpi",
            title: "坏契约面板",
            snapshot: { columns: ["value"], rows: [[1]] },
            query: { filters: [{ column: "c", op: "eq", value: 1 }] }, // 缺 dataset_id
          },
        ],
      },
    });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    // 合法 query：归一后挂载（非法 filters 项丢弃、limit 越界回退 100、列去重保序）
    expect(parsed.panels[0]?.query).toEqual({
      dataset_id: "ds_coal",
      filters: [{ column: "city", op: "eq", value: 1 }],
      limit: 100,
      columns: ["价格"],
    });
    // 非法 query：整体丢弃，面板本体不受影响（snapshot 原样，渲染读快照）
    expect(parsed.panels[1]?.query).toBeUndefined();
    expect(parsed.panels[1]?.snapshot).toEqual({ columns: ["value"], rows: [[1]] });
    expect(parsed.badPanels).toEqual([]); // 非法 query 不计坏面板
  });

  it("duplicatePanel 保留 query（structuredClone 深拷贝，副本改 query 不波及原件）", () => {
    const panel: DashboardPanel = {
      id: "q1",
      type: "table",
      title: "契约面板",
      span: 2,
      snapshot: { columns: ["日期"], rows: [["7月"]] },
      query: { ...VALID_QUERY, filters: [{ column: "city", op: "eq", value: "秦皇岛" }] },
    };
    const copy = duplicatePanel(panel, ["q1"]);
    expect(copy.query).toEqual(panel.query); // 原样保留
    // 深拷贝独立：改副本 query 字段不波及原件
    copy.query!.dataset_id = "ds_other";
    copy.query!.filters![0]!.column = "mutated";
    expect(panel.query!.dataset_id).toBe("ds_coal_price");
    expect(panel.query!.filters![0]!.column).toBe("city");
  });

  it("convertPanelType 保留 query（对象展开——仅换 type，kpi↔图表互转同样保留）", () => {
    const kpi: DashboardPanel = {
      id: "q1",
      type: "kpi",
      title: "契约指标",
      span: 1,
      snapshot: { columns: ["value"], rows: [[850]] },
      query: VALID_QUERY,
    };
    const toLine = convertPanelType(kpi, "line");
    expect(toLine.needConfirm).toBe(true);
    expect(toLine.panel.type).toBe("line");
    expect(toLine.panel.query).toEqual(VALID_QUERY); // 原样保留
    expect(kpi.query).toEqual(VALID_QUERY); // 不可变：原面板不动

    const toKpi = convertPanelType(
      { ...kpi, type: "line", query: { dataset_id: "ds_y", limit: 7 } },
      "kpi",
    );
    expect(toKpi.panel.query).toEqual({ dataset_id: "ds_y", limit: 7 });
  });
});

describe("v2 类型谱（wire-chart-contract-v2 任务 1.1）", () => {
  it("两档类型完整且互不重叠，并集等于完整枚举", () => {
    const base = new Set(DASHBOARD_BASE_PANEL_TYPES as readonly string[]);
    const advanced = new Set(DASHBOARD_ADVANCED_PANEL_TYPES as readonly string[]);
    expect(base.size).toBe(5); // kpi/line/bar/table + list（wire-list-panel）
    expect(advanced.size).toBe(10);
    // 不重叠
    for (const t of advanced) expect(base.has(t)).toBe(false);
    // 并集 == 完整枚举（漏一个就会让该类型既不可选也解析为坏面板）
    expect([...base, ...advanced].sort()).toEqual(
      [...DASHBOARD_PANEL_TYPES].sort(),
    );
  });

  it("高级档类型可被解析（不再判坏面板）", () => {
    const parsed = parseDashboardSpec({
      spec: {
        panels: DASHBOARD_ADVANCED_PANEL_TYPES.map((type, i) => ({
          id: `a${i}`,
          type,
          title: String(type),
        })),
      },
    });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    expect(parsed.panels.map((p) => p.type)).toEqual([
      ...DASHBOARD_ADVANCED_PANEL_TYPES,
    ]);
    expect(parsed.badPanels).toEqual([]);
  });
});

describe("encoding 编码通道（任务 1.2）", () => {
  it("合法全字段原样归一", () => {
    expect(
      normalizeEncoding({
        x: "日期",
        y: ["CPI", "PPI"],
        series: "地区",
        size: "成交额",
        xType: "time",
        yType: "value",
        xName: "月份",
        yName: "同比",
      }),
    ).toEqual({
      x: "日期",
      y: ["CPI", "PPI"],
      series: "地区",
      size: "成交额",
      xType: "time",
      yType: "value",
      xName: "月份",
      yName: "同比",
    });
  });

  it("轴类型非法回退默认（x→category、y→value），不丢其余字段", () => {
    expect(normalizeEncoding({ x: "a", xType: "log2", yType: "ordinal" })).toEqual({
      x: "a",
      xType: "category",
      yType: "value",
    });
    // 四种合法轴类型都收
    for (const t of ["category", "value", "time", "log"] as const) {
      expect(normalizeEncoding({ xType: t })?.xType).toBe(t);
    }
  });

  it("列名非法丢弃：空串/非字符串不收；y 去重保序", () => {
    expect(normalizeEncoding({ x: "", y: ["a", "a", 42, null, "b", ""] })).toEqual({
      y: ["a", "b"],
    });
    expect(normalizeEncoding({ x: 42, series: null, size: { a: 1 } })).toBeUndefined();
  });

  it("无任何合法字段 → undefined（面板走 v1 回退分支）", () => {
    expect(normalizeEncoding(undefined)).toBeUndefined();
    expect(normalizeEncoding(null)).toBeUndefined();
    expect(normalizeEncoding([])).toBeUndefined();
    expect(normalizeEncoding({})).toBeUndefined();
    expect(normalizeEncoding({ y: [] })).toBeUndefined();
    expect(normalizeEncoding("encoding")).toBeUndefined();
  });
});

describe("panes 多窗格（任务 1.3）", () => {
  it("两个以上合法窗格才落字段；单窗格归一为 undefined", () => {
    const two = normalizePanes([
      { title: "价格", encoding: { x: "日期", xType: "time" } },
      { title: "成交量", height: 1 },
    ]);
    expect(two).toHaveLength(2);
    expect(two?.[0]?.encoding?.xType).toBe("time");
    expect(two?.[1]?.height).toBe(1);

    // 单窗格 = 无多窗格语义
    expect(normalizePanes([{ title: "只有一格" }])).toBeUndefined();
    expect(normalizePanes([])).toBeUndefined();
    expect(normalizePanes("panes")).toBeUndefined();
  });

  it("坏窗格逐项丢弃；丢弃后不足两个同样不落字段", () => {
    expect(
      normalizePanes(["字符串", null, { title: "唯一合法格" }, 42]),
    ).toBeUndefined();
    const mixed = normalizePanes([
      { title: "主" },
      "坏项",
      { title: "副" },
    ]);
    expect(mixed).toHaveLength(2);
    expect(mixed?.map((p) => p.title)).toEqual(["主", "副"]);
  });

  it("窗格 height 只收正整数；非法值不落字段", () => {
    const panes = normalizePanes([
      { height: 0 },
      { height: -3 },
      { height: 2.5 },
      { height: "3" },
      { height: 4 },
    ]);
    // 前四项 height 均被丢弃，第五项保留 → 5 个窗格全合法（校验只对 height）
    expect(panes).toHaveLength(5);
    expect(panes?.map((p) => p.height)).toEqual([
      undefined,
      undefined,
      undefined,
      undefined,
      4,
    ]);
  });
});

describe("overlays 叠加层（任务 1.4）", () => {
  it("三类各自归一并保留标签与线型", () => {
    expect(
      normalizeOverlays({
        markLine: [
          { value: 2.1, label: "更替水平", lineStyle: "dashed" },
          { value: 50, axis: "y", lineStyle: "solid" },
          { value: "2020-01", axis: "x" },
        ],
        markArea: [{ from: 2008, to: 2009, label: "危机" }],
        markPoint: [{ x: "2020", y: 4.8, label: "峰值" }],
      }),
    ).toEqual({
      markLine: [
        { value: 2.1, label: "更替水平", lineStyle: "dashed" },
        { value: 50, axis: "y", lineStyle: "solid" },
        { value: "2020-01", axis: "x" },
      ],
      markArea: [{ from: 2008, to: 2009, label: "危机" }],
      markPoint: [{ x: "2020", y: 4.8, label: "峰值" }],
    });
  });

  it("非法项逐项丢弃：缺坐标/坐标类型越界/非对象项", () => {
    expect(
      normalizeOverlays({
        markLine: [
          { label: "无值" }, // 缺 value
          { value: { a: 1 } }, // 坐标非数字非字符串
          "字符串",
          { value: 7, label: "合法" },
        ],
      }),
    ).toEqual({ markLine: [{ value: 7, label: "合法" }] });
    // 全非法 → 不落字段
    expect(normalizeOverlays({ markLine: [{ value: null }] })).toBeUndefined();
  });

  it("线型白名单外丢弃该字段但保留线；axis 非 x/y 丢弃", () => {
    expect(
      normalizeOverlays({ markLine: [{ value: 1, lineStyle: "wavy", axis: "z" }] }),
    ).toEqual({ markLine: [{ value: 1 }] });
  });

  it("无叠加层 → undefined（v1 面板零漂移）", () => {
    expect(normalizeOverlays(undefined)).toBeUndefined();
    expect(normalizeOverlays({})).toBeUndefined();
    expect(normalizeOverlays([])).toBeUndefined();
    expect(normalizeOverlays({ markLine: [] })).toBeUndefined();
  });
});

describe("raw_option 逃生舱（任务 1.5）", () => {
  it("纯 JSON 载荷原样收下", () => {
    const raw = {
      xAxis: { type: "time" },
      series: [{ type: "line", data: [1, 2, 3] }],
      markLine: { data: [{ yAxis: 2.1 }] },
      tooltip: { valueFormatter: "(v) => v" }, // 字符串载荷合法（成品看板实态）
    };
    expect(normalizeRawOption(raw)).toEqual(raw);
  });

  it("拒绝函数、正则、Date 与类实例；数组正常递归", () => {
    expect(normalizeRawOption({ f: () => 1 })).toBeNull();
    expect(normalizeRawOption({ r: /x/ })).toBeNull();
    expect(normalizeRawOption({ d: new Date() })).toBeNull();
    expect(normalizeRawOption({ s: new Set([1]) })).toBeNull();
    expect(normalizeRawOption({ nested: { deep: [{ bad: () => 1 }] } })).toBeNull();
    expect(normalizeRawOption([{ a: 1 }])).toBeNull(); // 顶层数组不是 option 对象
    expect(normalizeRawOption("raw")).toBeNull();
    expect(normalizeRawOption(null)).toBeNull();
  });

  it("parse：含函数值的 raw_option 整面板记 bad_raw_option，其余面板不受影响", () => {
    const parsed = parseDashboardSpec({
      spec: {
        panels: [
          { id: "ok1", type: "line", title: "正常", snapshot: { columns: ["x", "y"], rows: [["a", 1]] } },
          { id: "bad", type: "line", title: "坏 raw", raw_option: { series: [{ formatter: () => 1 }] } },
          { id: "ok2", type: "candlestick", title: "K线", raw_option: { xAxis: { type: "time" } } },
        ],
      },
    });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    expect(parsed.panels.map((p) => p.id)).toEqual(["ok1", "ok2"]);
    expect(parsed.badPanels).toHaveLength(1);
    expect(parsed.badPanels[0]?.reason).toBe("bad_raw_option");
    expect(parsed.badPanels[0]?.index).toBe(1);
    expect(parsed.panels[1]?.raw_option).toEqual({ xAxis: { type: "time" } });
  });
});

describe("v2 面板形态判定与 v1 零漂移（任务 1.6）", () => {
  it("v1 面板解析结果与升级前逐字段一致（不含任何 v2 字段）", () => {
    const parsed = parseDashboardSpec({ spec: V1_BODY });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    // 关键：对象形状完全相同——多一个 encoding:undefined 都会破坏快照断言
    expect(parsed.panels[0]).toEqual(V1_BODY.panels[0]);
    expect(parsed.panels[1]).toEqual(V1_BODY.panels[1]);
    expect(Object.keys(parsed.panels[0]!).sort()).toEqual(
      Object.keys(V1_BODY.panels[0]!).sort(),
    );
    // isV2Panel 对 v1 面板恒 false
    for (const panel of parsed.panels) expect(isV2Panel(panel)).toBe(false);
  });

  it("isV2Panel：任一 v2 字段在场即 true", () => {
    const base: DashboardPanel = {
      id: "p1",
      type: "line",
      title: "",
      span: 1,
    };
    expect(isV2Panel(base)).toBe(false);
    expect(isV2Panel({ ...base, encoding: { x: "a" } })).toBe(true);
    expect(isV2Panel({ ...base, panes: [{}, {}] })).toBe(true);
    expect(isV2Panel({ ...base, overlays: { markLine: [{ value: 1 }] } })).toBe(true);
    expect(isV2Panel({ ...base, height: 300 })).toBe(true);
    expect(isV2Panel({ ...base, raw_option: {} })).toBe(true);
  });
});

describe("v2 字段在编辑 helpers 上的保留（任务 4.6）", () => {
  /** 携带全部 v2 字段的面板。 */
  const V2_PANEL: DashboardPanel = {
    id: "v2",
    type: "line",
    title: "带副图的行情",
    span: 3,
    source: "ds_prices",
    height: 420,
    snapshot: {
      columns: ["日期", "收盘", "量"],
      rows: [["2026-01", 10, 100]],
    },
    encoding: { x: "日期", xType: "time", y: ["收盘"] },
    panes: [
      { title: "价格", encoding: { x: "日期", xType: "time", y: ["收盘"] } },
      { title: "成交量", encoding: { y: ["量"] }, height: 1 },
    ],
    overlays: { markLine: [{ value: 2.1, label: "阈值", lineStyle: "dashed" }] },
  };

  it("convertPanelType：高级档互转保留 encoding/panes/overlays/height", () => {
    const toBar = convertPanelType(V2_PANEL, "bar");
    expect(toBar.needConfirm).toBe(false); // 非 kpi ↔ 图表
    expect(toBar.panel.type).toBe("bar");
    expect(toBar.panel.encoding).toEqual(V2_PANEL.encoding);
    expect(toBar.panel.panes).toEqual(V2_PANEL.panes);
    expect(toBar.panel.overlays).toEqual(V2_PANEL.overlays);
    expect(toBar.panel.height).toBe(420);
    // 不可变：原面板不动
    expect(V2_PANEL.type).toBe("line");
  });

  it("convertPanelType：高级档 → kpi 需确认，且 v2 字段仍原样保留", () => {
    const toKpi = convertPanelType(V2_PANEL, "kpi");
    expect(toKpi.needConfirm).toBe(true);
    expect(toKpi.panel.encoding).toEqual(V2_PANEL.encoding);
    expect(toKpi.panel.panes).toEqual(V2_PANEL.panes);
  });

  it("duplicatePanel：深拷贝 panes/overlays，改副本不波及原件", () => {
    const copy = duplicatePanel(V2_PANEL, ["v2"]);
    expect(copy.id).not.toBe("v2");
    expect(copy.panes).toEqual(V2_PANEL.panes);
    expect(copy.overlays).toEqual(V2_PANEL.overlays);
    expect(copy.encoding).toEqual(V2_PANEL.encoding);
    expect(copy.height).toBe(420);

    // 深拷贝独立性
    copy.panes![1]!.title = "被改的副图";
    copy.overlays!.markLine![0]!.value = 999;
    copy.encoding!.y = ["被改"];
    expect(V2_PANEL.panes![1]!.title).toBe("成交量");
    expect(V2_PANEL.overlays!.markLine![0]!.value).toBe(2.1);
    expect(V2_PANEL.encoding!.y).toEqual(["收盘"]);
  });

  it("duplicatePanel：raw_option 一并深拷贝（改副本不波及原件）", () => {
    const rawPanel: DashboardPanel = {
      id: "r",
      type: "line",
      title: "成品",
      span: 1,
      raw_option: { series: [{ name: "a", data: [1, 2] }] },
    };
    const copy = duplicatePanel(rawPanel, ["r"]);
    expect(copy.raw_option).toEqual(rawPanel.raw_option);
    (copy.raw_option!.series as Array<Record<string, unknown>>)[0]!.name = "mutated";
    expect(
      (rawPanel.raw_option!.series as Array<Record<string, unknown>>)[0]!.name,
    ).toBe("a");
  });
});

describe("列表面板契约（wire-list-panel）", () => {
  const LIST_COLUMNS = ["title", "summary", "image_url", "published_at", "source", "url"];
  const listPanel = (overrides: Record<string, unknown> = {}) => ({
    id: "l1",
    type: "list",
    title: "资讯流",
    span: 3,
    encoding: {
      title: "title",
      summary: "summary",
      image: "image_url",
      time: "published_at",
      source: "source",
      link: "url",
    },
    snapshot: { columns: LIST_COLUMNS, rows: [] },
    ...overrides,
  });

  it("五角色全部命中：encoding 原样保留", () => {
    const parsed = parseDashboardSpec({ spec: { panels: [listPanel()] } });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    expect(parsed.badPanels).toEqual([]);
    expect(parsed.panels[0]!.encoding).toEqual({
      title: "title",
      summary: "summary",
      image: "image_url",
      time: "published_at",
      source: "source",
      link: "url",
    });
  });

  it("title 缺失或不在快照列内：整面板判坏（bad_list_encoding）", () => {
    for (const encoding of [{}, { title: "missing_col" }, { summary: "summary" }]) {
      const parsed = parseDashboardSpec({
        spec: { panels: [listPanel({ encoding })] },
      });
      if (parsed.kind !== "v1") throw new Error("expected v1");
      expect(parsed.panels).toEqual([]);
      expect(parsed.badPanels).toEqual([
        { index: 0, reason: "bad_list_encoding", raw: expect.anything() },
      ]);
    }
  });

  it("快照缺失：判坏（没有列就无从校验角色）", () => {
    const parsed = parseDashboardSpec({
      spec: { panels: [listPanel({ snapshot: undefined })] },
    });
    if (parsed.kind !== "v1") throw new Error("expected v1");
    expect(parsed.badPanels[0]!.reason).toBe("bad_list_encoding");
  });

  it("可选角色列名不命中：只丢该角色，面板照常解析", () => {
    const parsed = parseDashboardSpec({
      spec: {
        panels: [
          listPanel({
            encoding: { title: "title", image: "no_such_col", link: "url" },
          }),
        ],
      },
    });
    if (parsed.kind !== "v1") throw new Error("expected v1");
    expect(parsed.badPanels).toEqual([]);
    expect(parsed.panels[0]!.encoding).toEqual({ title: "title", link: "url" });
  });

  it("config.limit 钳制到 1..20，非数回退 20", () => {
    const limits = [0, 21, "abc", 7];
    const expected = [1, 20, 20, 7];
    limits.forEach((limit, i) => {
      const parsed = parseDashboardSpec({
        spec: { panels: [listPanel({ config: { limit } })] },
      });
      if (parsed.kind !== "v1") throw new Error("expected v1");
      expect(parsed.panels[0]!.config?.limit).toBe(expected[i]);
    });
  });

  it("normalizeListEncoding：快照缺失返回 null，可选角色逐项过滤", () => {
    expect(normalizeListEncoding({ title: "title" }, undefined)).toBeNull();
    expect(
      normalizeListEncoding(
        { title: "title", summary: "summary", image: "nope" },
        { columns: ["title", "summary"], rows: [] },
      ),
    ).toEqual({ title: "title", summary: "summary" });
  });

  it("panelTemplate('list')：骨架合法（title 命中快照列）", () => {
    const template = panelTemplate("list", []);
    expect(template.type).toBe("list");
    expect(template.encoding?.title).toBe("title");
    expect(template.config?.limit).toBe(20);
    const parsed = parseDashboardSpec({ spec: { panels: [template] } });
    if (parsed.kind !== "v1") throw new Error("expected v1");
    expect(parsed.badPanels).toEqual([]);
    expect(parsed.panels).toHaveLength(1);
  });

  it("类型互转：list↔table 免确认且快照零损失；list↔图表需确认", () => {
    const source: DashboardPanel = {
      id: "p1",
      type: "table",
      title: "表",
      span: 1,
      snapshot: { columns: ["标题", "摘要"], rows: [["a", "b"]] },
    };
    const toList = convertPanelType(source, "list");
    expect(toList.needConfirm).toBe(false);
    expect(toList.panel.snapshot).toEqual(source.snapshot);
    expect(toList.panel.encoding).toEqual({ title: "标题", summary: "摘要" });

    const backToTable = convertPanelType(toList.panel, "table");
    expect(backToTable.needConfirm).toBe(false);
    expect(backToTable.panel.snapshot).toEqual(source.snapshot);
    expect(backToTable.panel.encoding).toBeUndefined();
    expect(backToTable.panel.config).toBeUndefined();

    const chartSource: DashboardPanel = {
      id: "p2",
      type: "line",
      title: "线",
      span: 1,
      snapshot: { columns: ["x", "y"], rows: [] },
      encoding: { x: "x", y: ["y"] },
    };
    const toListFromChart = convertPanelType(chartSource, "list");
    expect(toListFromChart.needConfirm).toBe(true);
    expect(toListFromChart.panel.encoding).toEqual({
      x: "x",
      y: ["y"],
      title: "x",
      summary: "y",
    });

    const fromList = convertPanelType(toList.panel, "line");
    expect(fromList.needConfirm).toBe(true);
    expect(fromList.panel.encoding).toBeUndefined();
  });
});
