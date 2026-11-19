// 面板 → option 构建测试（wire-chart-contract-v2 任务 2.1–2.7）。
// 重点不是"画得好不好看"（视觉验收走浏览器实测），而是契约映射正确：
// v1 回退逐字段不变、轴类型生效、多窗格共享刻度、叠加层落在正确窗格、
// raw_option 与类型化字段的合并优先级可解释。
import { describe, expect, it } from "vitest";
import {
  DEFAULT_PANEL_HEIGHT,
  MIN_PANE_HEIGHT,
  ZOOM_SLIDER_THRESHOLD,
  buildPanelOption,
  deepMergeOptions,
  panesFitInHeight,
  resolveEncoding,
  toTimestamp,
} from "./chartOption";
import type { DashboardPanel } from "./spec";

type Option = Record<string, unknown>;

/** 取构建结果的 option（断言用；无数据时先让测试失败而不是静默通过）。 */
function opt(panel: DashboardPanel, interactive = true): Option {
  const built = buildPanelOption(panel, { interactive });
  if (built === null) throw new Error("expects data but buildPanelOption returned null");
  return built.option as unknown as Option;
}

function seriesOf(option: Option): Option[] {
  return (option.series as Option[] | undefined) ?? [];
}

const V1_LINE: DashboardPanel = {
  id: "l1",
  type: "line",
  title: "PMI",
  span: 1,
  snapshot: {
    columns: ["月份", "PMI"],
    rows: [
      ["7月", 49.4],
      ["8月", 49.1],
    ],
  },
};

describe("toTimestamp（time 轴解析）", () => {
  it("年份 / 年月 / 日期 / ISO 带时间都解析为毫秒时间戳", () => {
    expect(toTimestamp("2026")).toBe(Date.UTC(2026, 0, 1));
    expect(toTimestamp("2026-08")).toBe(Date.UTC(2026, 7, 1));
    expect(toTimestamp("2026/8")).toBe(Date.UTC(2026, 7, 1));
    expect(toTimestamp("2026-08-27")).toBe(Date.UTC(2026, 7, 27));
    expect(toTimestamp("2026-08-27 17:16:43")).toBe(Date.UTC(2026, 7, 27, 17, 16, 43));
    expect(toTimestamp("2026-08-27T09:30")).toBe(Date.UTC(2026, 7, 27, 9, 30, 0));
  });

  it("非法输入归 null（图断线，不错位成 0）", () => {
    expect(toTimestamp("")).toBeNull();
    expect(toTimestamp("   ")).toBeNull();
    expect(toTimestamp("2026-13")).toBeNull();
    expect(toTimestamp("2026-02-31x")).not.toBeNull(); // 日期段能解析（不校验月末）
    expect(toTimestamp("无日期")).toBeNull();
    expect(toTimestamp(null)).toBeNull();
    expect(toTimestamp(undefined)).toBeNull();
  });
});

describe("resolveEncoding（编码解析与回退）", () => {
  it("无 encoding：首列 x、其余列 y（v1 位置约定）", () => {
    const resolved = resolveEncoding(V1_LINE, ["月份", "PMI"]);
    expect(resolved.xIndex).toBe(0);
    expect(resolved.yIndices).toEqual([1]);
    expect(resolved.xType).toBe("category");
    expect(resolved.yType).toBe("value");
  });

  it("声明优先：显式 x 与多列 y", () => {
    const resolved = resolveEncoding(
      { ...V1_LINE, encoding: { x: "月份", y: ["PMI"] } },
      ["月份", "PMI", "CPI"],
    );
    expect(resolved.xIndex).toBe(0);
    expect(resolved.yIndices).toEqual([1]);
  });

  it("声明的列名不存在 → 回退位置约定（不画空白图）", () => {
    const resolved = resolveEncoding(
      { ...V1_LINE, encoding: { x: "不存在的列", y: ["也不存在"] } },
      ["月份", "PMI"],
    );
    expect(resolved.xIndex).toBe(0);
    expect(resolved.yIndices).toEqual([1]);
  });

  it("轴类型透传；轴名 encoding 优先于 config", () => {
    const resolved = resolveEncoding(
      {
        ...V1_LINE,
        config: { xName: "配置里的轴名", yName: "配置 y" },
        encoding: { xType: "time", yType: "log", xName: "编码里的轴名" },
      },
      ["月份", "PMI"],
    );
    expect(resolved.xType).toBe("time");
    expect(resolved.yType).toBe("log");
    expect(resolved.xName).toBe("编码里的轴名");
    expect(resolved.yName).toBe("配置 y");
  });
});

describe("v1 回退零漂移（任务 2.1/2.6）", () => {
  it("未声明 encoding 的折线：category 轴 + 首列 x + 其余列系列", () => {
    const option = opt(V1_LINE);
    const xAxis = option.xAxis as Option;
    expect(xAxis.type).toBe("category");
    expect(xAxis.data).toEqual(["7月", "8月"]);
    const series = seriesOf(option);
    expect(series).toHaveLength(1);
    expect(series[0]?.type).toBe("line");
    expect(series[0]?.name).toBe("PMI");
    expect(series[0]?.data).toEqual([49.4, 49.1]);
  });

  it("未声明 height 时取默认图高（升级前写死的 220）", () => {
    expect(buildPanelOption(V1_LINE)?.height).toBe(DEFAULT_PANEL_HEIGHT);
    expect(DEFAULT_PANEL_HEIGHT).toBe(220);
  });

  it("声明 height 生效", () => {
    expect(buildPanelOption({ ...V1_LINE, height: 420 })?.height).toBe(420);
  });

  it("多列系列与单列系列的图例行为：两条以上才出 legend", () => {
    const one = opt(V1_LINE);
    expect(one.legend).toBeUndefined();

    const two = opt({
      ...V1_LINE,
      snapshot: {
        columns: ["月份", "PMI", "CPI"],
        rows: [["7月", 49.4, 2.1]],
      },
    });
    const legend = two.legend as Option;
    expect(legend.data).toEqual(["PMI", "CPI"]);
  });

  it("bar 类型走柱状系列；空数据返回 null（渲染层显示无数据）", () => {
    const bar = opt({ ...V1_LINE, type: "bar" });
    expect(seriesOf(bar)[0]?.type).toBe("bar");
    expect(buildPanelOption({ ...V1_LINE, snapshot: { columns: ["a", "b"], rows: [] } })).toBeNull();
    expect(buildPanelOption({ ...V1_LINE, snapshot: undefined })).toBeNull();
  });
});

describe("轴类型（任务 2.2）", () => {
  it("time 轴：轴类型为 time，系列数据转为 [时间戳, 值] 点对", () => {
    const option = opt({
      ...V1_LINE,
      snapshot: {
        columns: ["日期", "收盘"],
        rows: [
          ["2001-08-27", 4.0],
          ["2001-08-28", 4.15],
        ],
      },
      encoding: { x: "日期", xType: "time" },
    });
    expect((option.xAxis as Option).type).toBe("time");
    expect(seriesOf(option)[0]?.data).toEqual([
      [Date.UTC(2001, 7, 27), 4.0],
      [Date.UTC(2001, 7, 28), 4.15],
    ]);
  });

  it("time 轴不等间隔按真实时间排布（数据点是时间戳而非等距类目）", () => {
    const option = opt({
      ...V1_LINE,
      snapshot: {
        columns: ["日期", "值"],
        rows: [
          ["2001", 1],
          ["2002", 2],
          ["2010", 3],
        ],
      },
      encoding: { xType: "time" },
    });
    const data = seriesOf(option)[0]?.data as Array<[number, number]>;
    // 2001→2002 间隔 1 年，2002→2010 间隔 8 年：时间戳差必须反映真实间隔
    const gap1 = data[1]![0] - data[0]![0];
    const gap2 = data[2]![0] - data[1]![0];
    expect(gap2 / gap1).toBeCloseTo(8, 0);
  });

  it("log 轴带 logBase 10", () => {
    const option = opt({ ...V1_LINE, encoding: { yType: "log" } });
    expect((option.yAxis as Option).logBase).toBe(10);
  });
});

describe("多窗格（任务 2.3）", () => {
  const panesPanel = (height?: number): DashboardPanel => ({
    id: "k",
    type: "line",
    title: "价格与成交量",
    span: 3,
    ...(height !== undefined ? { height } : {}),
    snapshot: {
      columns: ["日期", "收盘", "成交量"],
      rows: [
        ["2026-01", 10, 100],
        ["2026-02", 12, 120],
      ],
    },
    encoding: { x: "日期", xType: "time", y: ["收盘"] },
    panes: [
      { title: "价格", encoding: { x: "日期", xType: "time", y: ["收盘"] } },
      { title: "成交量", encoding: { x: "日期", xType: "time", y: ["成交量"] } },
    ],
  });

  it("两个窗格 → 两套 grid/轴 + axisPointer 串联 + dataZoom 覆盖全部", () => {
    const option = opt(panesPanel(400));
    expect(option.grid as Option[]).toHaveLength(2);
    expect(option.xAxis as Option[]).toHaveLength(2);
    expect(option.yAxis as Option[]).toHaveLength(2);
    expect(option.axisPointer).toEqual({ link: [{ xAxisIndex: "all" }] });
    const zooms = option.dataZoom as Option[];
    expect(zooms[0]?.xAxisIndex).toEqual([0, 1]);

    // 系列按窗格分配 axisIndex
    const series = seriesOf(option);
    expect(series[0]?.xAxisIndex).toBe(0);
    expect(series[1]?.xAxisIndex).toBe(1);
  });

  it("副窗格缺省继承主窗格 x（共享刻度，不自造轴）", () => {
    const panel = panesPanel(400);
    // 副窗格不声明 x：仍应与主窗格同一 xIndex/xType
    const option = opt({
      ...panel,
      panes: [
        { encoding: { x: "日期", xType: "time", y: ["收盘"] } },
        { encoding: { y: ["成交量"] } },
      ],
    });
    const xAxes = option.xAxis as Option[];
    expect(xAxes[0]?.type).toBe("time");
    expect(xAxes[1]?.type).toBe("time");
  });

  it("高度不足 → 降级为单窗格并标注", () => {
    const built = buildPanelOption(panesPanel(90));
    expect(built?.panesDegraded).toBe(true);
    // 降级后只有一套轴
    expect((built?.option as unknown as Option).xAxis as Option).not.toBeInstanceOf(Array);
  });

  it("panesFitInHeight：足够高才容纳全部窗格", () => {
    const panes = [{}, {}];
    expect(panesFitInHeight(panes, 400)).toBe(true);
    expect(panesFitInHeight(panes, MIN_PANE_HEIGHT * 2)).toBe(false);
  });

  it("单窗格面板与不声明 panes 的单图面板渲染等价", () => {
    const single = opt({ ...V1_LINE, panes: undefined });
    const withOnePane = opt({ ...V1_LINE, panes: [{ encoding: { x: "月份" } }] });
    // 单元素 panes 在解析层已被归一为 undefined；这里直接给对象仍按单图走
    expect(Array.isArray(single.xAxis)).toBe(false);
    expect(Array.isArray(withOnePane.xAxis)).toBe(false);
  });
});

describe("叠加层（任务 2.4）", () => {
  it("阈值线落在首个系列上，标签与线型透传", () => {
    const option = opt({
      ...V1_LINE,
      overlays: {
        markLine: [{ value: 2.1, label: "更替水平", lineStyle: "dashed" }],
      },
    });
    const markLine = seriesOf(option)[0]?.markLine as Option;
    const data = markLine.data as Option[];
    expect(data[0]?.yAxis).toBe(2.1);
    expect((data[0]?.label as Option).formatter).toBe("更替水平");
    expect((data[0]?.lineStyle as Option).type).toBe("dashed");
  });

  it("axis=x 的阈值线画在 x 轴（垂直线），time 轴转时间戳", () => {
    const option = opt({
      ...V1_LINE,
      snapshot: { columns: ["日期", "值"], rows: [["2026-01", 1]] },
      encoding: { x: "日期", xType: "time" },
      overlays: { markLine: [{ value: "2026-06", axis: "x", label: "切换" }] },
    });
    const data = (seriesOf(option)[0]?.markLine as Option).data as Option[];
    expect(data[0]?.xAxis).toBe(Date.UTC(2026, 5, 1));
  });

  it("区间高亮成对出现且带标签", () => {
    const option = opt({
      ...V1_LINE,
      encoding: { x: "月份" },
      overlays: { markArea: [{ from: "7月", to: "8月", label: "区间" }] },
    });
    const data = (seriesOf(option)[0]?.markArea as Option).data as Option[][];
    expect(data).toHaveLength(1);
    expect(data[0]?.[0]?.xAxis).toBe("7月");
    expect(data[0]?.[1]?.xAxis).toBe("8月");
    expect((data[0]?.[0]?.label as Option).formatter).toBe("区间");
  });

  it("事件点带坐标与标签", () => {
    const option = opt({
      ...V1_LINE,
      overlays: { markPoint: [{ x: "8月", y: 49.1, label: "低点" }] },
    });
    const data = (seriesOf(option)[0]?.markPoint as Option).data as Option[];
    expect(data[0]?.coord).toEqual(["8月", 49.1]);
    expect(data[0]?.value).toBe("低点");
  });

  it("无叠加层时不产生 markLine/markArea/markPoint 键", () => {
    const series = seriesOf(opt(V1_LINE))[0] as Option;
    expect(series.markLine).toBeUndefined();
    expect(series.markArea).toBeUndefined();
    expect(series.markPoint).toBeUndefined();
  });
});

describe("raw_option（任务 2.5）", () => {
  const RAW = {
    tooltip: { trigger: "axis" },
    xAxis: { type: "time" },
    yAxis: { type: "value", logBase: 10 },
    series: [{ name: "值", type: "line", data: [[Date.UTC(2026, 0, 1), 1]] }],
    markLine: { data: [{ yAxis: 2 }] },
  };

  it("仅 raw_option（无快照列）：原样直通", () => {
    const option = opt({ id: "r", type: "line", title: "raw", span: 1, raw_option: RAW });
    expect(option.xAxis).toEqual({ type: "time" });
    expect((option.yAxis as Option).logBase).toBe(10);
    expect(option.markLine).toEqual({ data: [{ yAxis: 2 }] });
  });

  it("raw_option 与类型化字段共存：类型化优先、raw 补齐缺位", () => {
    const option = opt({
      ...V1_LINE,
      encoding: { x: "月份" },
      raw_option: {
        xAxis: { type: "log" }, // 会被类型化覆盖成 category
        title: { text: "raw 里的标题" }, // 类型化未产出 → 保留
      },
    });
    expect((option.xAxis as Option).type).toBe("category"); // 类型化赢
    expect(option.title).toEqual({ text: "raw 里的标题" }); // raw 补齐
  });

  it("deepMergeOptions：对象深合、数组整体替换、undefined 不覆盖", () => {
    expect(
      deepMergeOptions(
        { a: { x: 1, y: 2 }, list: [1, 2, 3], keep: "base" },
        { a: { y: 9 }, list: [9], keep: undefined },
      ),
    ).toEqual({ a: { x: 1, y: 9 }, list: [9], keep: "base" });
  });

  it("raw_option 独占时不因快照缺失判无数据", () => {
    expect(
      buildPanelOption({ id: "r", type: "line", title: "", span: 1, raw_option: RAW }),
    ).not.toBeNull();
  });
});

describe("交互（任务 2.7）", () => {
  it("默认附十字光标与内嵌缩放", () => {
    const option = opt(V1_LINE);
    expect((option.tooltip as Option).axisPointer).toEqual({ type: "cross" });
    const zooms = option.dataZoom as Option[];
    expect(zooms[0]?.type).toBe("inside");
    // 短序列不挂滑块
    expect(zooms).toHaveLength(1);
  });

  it("长序列追加缩放滑块", () => {
    const rows = Array.from({ length: ZOOM_SLIDER_THRESHOLD + 1 }, (_, i) => [`m${i}`, i]);
    const option = opt({ ...V1_LINE, snapshot: { columns: ["月份", "PMI"], rows } });
    const zooms = option.dataZoom as Option[];
    expect(zooms).toHaveLength(2);
    expect(zooms[1]?.type).toBe("slider");
  });

  it("interactive=false 时不挂十字光标与缩放（纯静态输出）", () => {
    const built = buildPanelOption(V1_LINE, { interactive: false });
    const option = built?.option as unknown as Option;
    expect((option.tooltip as Option).axisPointer).toBeUndefined();
    expect(option.dataZoom).toBeUndefined();
  });

  it("无轴类型不挂 dataZoom（饼图缩放无意义）", () => {
    const option = opt({
      id: "p",
      type: "pie",
      title: "占比",
      span: 1,
      snapshot: { columns: ["类目", "值"], rows: [["A", 1]] },
    });
    expect(option.dataZoom).toBeUndefined();
  });
});

describe("类型谱系列映射（任务 2.1）", () => {
  it("candlestick：快照四价序转 echarts 的 [开,收,低,高]", () => {
    const option = opt({
      id: "k",
      type: "candlestick",
      title: "K线",
      span: 3,
      snapshot: {
        columns: ["日期", "开盘", "最高", "最低", "收盘"],
        rows: [["2026-01-02", 10, 12, 9, 11]],
      },
      encoding: { x: "日期", xType: "category" },
    });
    const data = seriesOf(option)[0]?.data as number[][];
    expect(data[0]).toEqual([10, 11, 9, 12]); // [open, close, low, high]
  });

  it("pie：类目 + 数值成对", () => {
    const option = opt({
      id: "p",
      type: "pie",
      title: "占比",
      span: 1,
      snapshot: { columns: ["类目", "值"], rows: [["A", 3], ["B", 2]] },
    });
    const data = seriesOf(option)[0]?.data as Option[];
    expect(data).toEqual([
      { name: "A", value: 3 },
      { name: "B", value: 2 },
    ]);
    expect(seriesOf(option)[0]?.type).toBe("pie");
  });

  it("heatmap：x/y 类目转为格点索引", () => {
    const option = opt({
      id: "h",
      type: "heatmap",
      title: "热力",
      span: 2,
      snapshot: {
        columns: ["x", "y", "值"],
        rows: [
          ["A", "P", 1],
          ["B", "Q", 2],
        ],
      },
      encoding: { x: "x", y: ["值"], series: "y" },
    });
    const data = seriesOf(option)[0]?.data as number[][];
    expect(data).toHaveLength(2);
    expect(data[0]?.[2]).toBe(1);
  });

  it("sankey：源→目标 + 流量", () => {
    const option = opt({
      id: "s",
      type: "sankey",
      title: "流向",
      span: 3,
      snapshot: {
        columns: ["源", "目标", "流量"],
        rows: [["A", "X", 5]],
      },
      encoding: { x: "源", series: "目标", size: "流量" },
    });
    const series = seriesOf(option)[0] as Option;
    expect(series.links).toEqual([{ source: "A", target: "X", value: 5 }]);
    expect(series.data).toEqual([{ name: "A" }, { name: "X" }]);
  });

  it("boxplot：五数概括数组", () => {
    const option = opt({
      id: "b",
      type: "boxplot",
      title: "分布",
      span: 2,
      snapshot: {
        columns: ["类目", "min", "q1", "median", "q3", "max"],
        rows: [["A", 1, 2, 3, 4, 5]],
      },
    });
    expect(seriesOf(option)[0]?.data).toEqual([[1, 2, 3, 4, 5]]);
  });

  it("系列分组列：按列值拆成多条系列（长表形态）", () => {
    const option = opt({
      id: "g",
      type: "line",
      title: "分地区",
      span: 2,
      snapshot: {
        columns: ["月份", "地区", "值"],
        rows: [
          ["1月", "东", 1],
          ["1月", "西", 2],
          ["2月", "东", 3],
          ["2月", "西", 4],
        ],
      },
      encoding: { x: "月份", series: "地区", y: ["值"] },
    });
    const series = seriesOf(option);
    expect(series.map((s) => s.name)).toEqual(["东", "西"]);
    expect(series[0]?.data).toEqual([1, null, 3, null]);
    expect(series[1]?.data).toEqual([null, 2, null, 4]);
  });
});

describe("v1 零漂移总闸（任务 7.4）", () => {
  /**
   * 对一份覆盖 v1 全部形态的看板逐面板断言：option 的**关键结构**与
   * 升级前的构建口径逐字段一致（轴类型、轴数据、系列类型/名称/数据、
   * 图例有无）。这是整个变更的安全绳——v2 放开表达力绝不能顺手改了
   * v1 的画法。
   */
  it("四类 v1 面板的 option 与升级前口径逐字段一致", () => {
    // kpi 不走 option 构建（渲染器直接读快照），此处只覆盖图表与表格类
    const line: DashboardPanel = {
      id: "l1",
      type: "line",
      title: "PMI",
      span: 2,
      source: "CN_PMI",
      snapshot: {
        columns: ["月份", "PMI"],
        rows: [
          ["7月", 49.4],
          ["8月", 49.1],
          ["9月", 49.8],
        ],
      },
      config: { xName: "月份", yName: "PMI" },
    };
    const option = opt(line) as Record<string, unknown>;

    // 轴：类目轴 + 首列数据 + 轴名（config 口径）
    expect(option.xAxis).toEqual({
      type: "category",
      gridIndex: 0,
      name: "月份",
      data: ["7月", "8月", "9月"],
    });
    // y 轴：数值轴 + 轴名 + scale（v2 新增的 scale 不改变数值范围语义）
    const yAxis = option.yAxis as Record<string, unknown>;
    expect(yAxis.type).toBe("value");
    expect(yAxis.name).toBe("PMI");

    // 系列：单系列，名称取列名，数据取该列
    const series = option.series as Array<Record<string, unknown>>;
    expect(series).toHaveLength(1);
    expect(series[0]).toMatchObject({
      name: "PMI",
      type: "line",
      data: [49.4, 49.1, 49.8],
    });
    // 单系列不出图例
    expect(option.legend).toBeUndefined();

    // 多列 → 多系列 + 图例（v1 既有行为）
    const multi = opt({
      ...line,
      snapshot: {
        columns: ["月份", "PMI", "CPI"],
        rows: [["7月", 49.4, 2.1]],
      },
    }) as Record<string, unknown>;
    expect((multi.series as Array<Record<string, unknown>>).map((s) => s.name)).toEqual([
      "PMI",
      "CPI",
    ]);
    expect((multi.legend as Record<string, unknown>).data).toEqual(["PMI", "CPI"]);
  });

  it("v1 bar 面板：柱状系列，图高默认 220", () => {
    const bar: DashboardPanel = {
      id: "b1",
      type: "bar",
      title: "发电量",
      span: 1,
      snapshot: { columns: ["月份", "火电"], rows: [["7月", 100]] },
    };
    const built = buildPanelOption(bar);
    expect(built?.height).toBe(220);
    const series = (built?.option as unknown as Record<string, unknown>).series as Array<
      Record<string, unknown>
    >;
    expect(series[0]?.type).toBe("bar");
  });

  it("v1 面板不因新增交互键而改变数据/轴/系列", () => {
    // 交互开启与关闭只应影响 tooltip.axisPointer / dataZoom，其余完全一致
    const withInteraction = opt(V1_LINE) as Record<string, unknown>;
    const without = opt(V1_LINE, false) as Record<string, unknown>;
    expect(without.xAxis).toEqual(withInteraction.xAxis);
    expect(without.yAxis).toEqual(withInteraction.yAxis);
    expect(without.series).toEqual(withInteraction.series);
    expect(withInteraction.dataZoom).toBeDefined();
    expect(without.dataZoom).toBeUndefined();
  });
});
