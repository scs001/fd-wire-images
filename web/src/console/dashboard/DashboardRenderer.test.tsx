// 仪表盘渲染器组件测试（wire-dashboard-renderer 任务 2.3）：
// 三类面板渲染（kpi/line+bar/table）、空数据占位、坏面板隔离（不整板失败）、
// 旧自由 JSON legacy 态、快照口径标注。
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { DashboardRenderer } from "./DashboardRenderer";

beforeEach(() => {
  i18n.changeLanguage("zh");
});

afterEach(() => {
  cleanup();
});

/** 含三类面板 + 空数据面板 + 两个非法面板的 v1 信封。 */
const MIXED_SPEC = {
  title: "宏观仪表盘",
  spec: {
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
            ["9月", 49.8],
          ],
        },
        config: { yName: "PMI", xName: "月份" },
      },
      {
        id: "b1",
        type: "bar",
        title: "发电量",
        span: 1,
        snapshot: {
          columns: ["月份", "火电", "水电"],
          rows: [
            ["7月", 100, 50],
            ["8月", 110, 60],
          ],
        },
      },
      {
        id: "t1",
        type: "table",
        title: "月度明细",
        span: 3,
        snapshot: {
          columns: ["月份", "值"],
          rows: Array.from({ length: 25 }, (_, i) => [`2026-${i + 1}月`, i]),
        },
      },
      { id: "e1", type: "kpi", title: "空指标", span: 1 },
      // 非法类型：v2 把 pie 纳入类型谱了，故换一个仍在谱外的类型做坏面板样本
      { type: "sunburst", title: "坏类型" },
      "只是一个字符串",
    ],
  },
};

describe("DashboardRenderer（快照渲染）", () => {
  it("三类面板渲染：kpi 大数卡 / line+bar echarts / table 简表", () => {
    render(<DashboardRenderer spec={MIXED_SPEC} />);

    expect(screen.getByTestId("dashboard-title").textContent).toBe("宏观仪表盘");

    // kpi：值 + 单位 + 副标题 + 来源标注
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("2.1");
    expect(screen.getByTestId("dashboard-kpi-unit").textContent).toBe("%");
    expect(screen.getByTestId("dashboard-kpi-subtitle").textContent).toBe("2026-09");
    expect(screen.getByTestId("dashboard-panel-source").textContent).toBe(
      "CN_CPI_YOY",
    );

    // line + bar：echarts 容器（SVG 渲染器在测试环境可跑）
    const charts = screen.getAllByTestId("dashboard-chart");
    expect(charts).toHaveLength(2);
    for (const chart of charts) {
      expect(chart.querySelector("svg")).toBeTruthy();
    }

    // table：25 行全渲染 + 超 20 行启用内部滚动
    const table = screen.getByTestId("dashboard-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(25);
    expect(table.querySelector("tbody tr td")?.textContent).toBe("2026-1月");
    expect((table.parentElement as HTMLElement).className).toContain(
      "overflow-y-auto",
    );

    // 栅格跨度：line 面板占 2 列（Tailwind 静态类映射）
    const linePanel = screen
      .getAllByTestId("dashboard-panel")
      .find((el) => el.getAttribute("data-panel-id") === "l1")!;
    expect(linePanel.className).toContain("md:col-span-2");

    // 快照口径小字
    expect(screen.getByTestId("dashboard-snapshot-note").textContent).toContain(
      "快照数据",
    );
  });

  it("空 snapshot 如实显「无数据」；kpi 值为 null 同样占位", () => {
    render(
      <DashboardRenderer
        spec={{
          title: "空看板",
          panels: [
            { id: "a", type: "line", title: "空折线", span: 1 },
            { id: "b", type: "table", title: "空表", span: 1 },
            {
              id: "c",
              type: "kpi",
              title: "空值",
              span: 1,
              snapshot: { columns: ["value"], rows: [[null]] },
            },
          ],
        }}
      />,
    );
    expect(screen.getAllByTestId("dashboard-no-data")).toHaveLength(3);
  });

  it("坏面板隔离：非法面板单卡标注跳过，合法面板不受牵连", () => {
    render(<DashboardRenderer spec={MIXED_SPEC} />);

    const bads = screen.getAllByTestId("dashboard-bad-panel");
    expect(bads).toHaveLength(2);
    // 序号 1 起 + 原因文案
    expect(bads[0]?.getAttribute("data-index")).toBe("5");
    expect(bads[0]?.textContent).toContain("面板 #6");
    expect(bads[0]?.textContent).toContain("图表类型谱");
    expect(bads[1]?.textContent).toContain("面板 #7");
    expect(bads[1]?.textContent).toContain("JSON 对象");

    // 合法面板照常渲染（不整板失败）
    expect(screen.getAllByTestId("dashboard-panel")).toHaveLength(5);
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("2.1");
  });

  it("旧自由 JSON →「旧版自由格式」+ 只读 JSON 查看", () => {
    render(
      <DashboardRenderer
        spec={{
          title: "宏观仪表盘",
          spec: { indicators: ["CN_CPI_YOY", "CN_PMI"], layout: "2x2" },
        }}
      />,
    );
    expect(screen.getByTestId("dashboard-legacy")).toBeTruthy();
    expect(screen.getByTestId("dashboard-legacy").textContent).toContain(
      "旧版自由格式",
    );
    const json = screen.getByTestId("dashboard-legacy-json").textContent ?? "";
    expect(json).toContain("CN_CPI_YOY");
    expect(json).toContain("2x2");
    // legacy 态不渲染任何面板
    expect(screen.queryByTestId("dashboard-panel")).toBeNull();
  });

  it("空看板（零面板零坏面板）→ 空态提示 + 快照口径仍在", () => {
    render(<DashboardRenderer spec={{ title: "", panels: [] }} />);
    expect(screen.getByTestId("dashboard-empty-panels").textContent).toContain(
      "还没有面板",
    );
    expect(screen.queryByTestId("dashboard-title")).toBeNull();
    expect(screen.getByTestId("dashboard-snapshot-note")).toBeTruthy();
  });
});

describe("v2 面板渲染（wire-chart-contract-v2 任务 2.x）", () => {
  it("高级档类型与 time 轴面板正常渲染（不判坏面板）", () => {
    render(
      <DashboardRenderer
        spec={{
          title: "行情看板",
          panels: [
            {
              id: "k",
              type: "candlestick",
              title: "K 线",
              span: 3,
              snapshot: {
                columns: ["日期", "开盘", "最高", "最低", "收盘"],
                rows: [["2026-01-02", 10, 12, 9, 11]],
              },
              encoding: { x: "日期", xType: "time" },
            },
            {
              id: "p",
              type: "pie",
              title: "占比",
              span: 1,
              snapshot: { columns: ["类目", "值"], rows: [["A", 3]] },
            },
          ],
        }}
      />,
    );
    expect(screen.getAllByTestId("dashboard-panel")).toHaveLength(2);
    expect(screen.queryByTestId("dashboard-bad-panel")).toBeNull();
    expect(screen.getAllByTestId("dashboard-chart")).toHaveLength(2);
  });

  it("raw_option 面板带「高级配置」徽标", () => {
    render(
      <DashboardRenderer
        spec={{
          title: "",
          panels: [
            {
              id: "r",
              type: "line",
              title: "成品迁入",
              span: 2,
              raw_option: {
                xAxis: { type: "time" },
                series: [{ name: "值", type: "line", data: [[Date.UTC(2026, 0, 1), 1]] }],
              },
            },
          ],
        }}
      />,
    );
    expect(screen.getByTestId("dashboard-panel-raw-badge").textContent).toBe("高级配置");
    expect(screen.queryByTestId("dashboard-bad-panel")).toBeNull();
  });

  it("raw_option 含函数值 → 坏面板标注且指明原因", () => {
    render(
      <DashboardRenderer
        spec={{
          title: "",
          panels: [
            {
              id: "bad",
              type: "line",
              title: "坏配置",
              span: 1,
              raw_option: { series: [{ formatter: () => 1 }] },
            },
          ],
        }}
      />,
    );
    const bad = screen.getByTestId("dashboard-bad-panel");
    expect(bad.getAttribute("data-bad-reason")).toBe("bad_raw_option");
    expect(bad.textContent).toContain("不可序列化");
  });

  it("声明高度的面板应用该高度；未声明保持 220", () => {
    render(
      <DashboardRenderer
        spec={{
          title: "",
          panels: [
            {
              id: "tall",
              type: "line",
              title: "高图",
              span: 1,
              height: 400,
              snapshot: { columns: ["x", "y"], rows: [["a", 1]] },
            },
            {
              id: "normal",
              type: "line",
              title: "默认高",
              span: 1,
              snapshot: { columns: ["x", "y"], rows: [["a", 1]] },
            },
          ],
        }}
      />,
    );
    const [tall, normal] = screen.getAllByTestId("dashboard-chart");
    expect((tall as HTMLElement).style.height).toBe("400px");
    expect((normal as HTMLElement).style.height).toBe("220px");
  });

  it("多窗格高度不足 → 降级提示如实呈现", () => {
    render(
      <DashboardRenderer
        spec={{
          title: "",
          panels: [
            {
              id: "panes",
              type: "line",
              title: "价格与量",
              span: 3,
              height: 90,
              snapshot: {
                columns: ["日期", "收盘", "量"],
                rows: [["2026-01", 10, 100]],
              },
              panes: [{}, {}],
            },
          ],
        }}
      />,
    );
    expect(screen.getByTestId("dashboard-panes-degraded").textContent).toContain(
      "只显示主图",
    );
  });
});

describe("工具栏（wire-chart-contract-v2 任务 3.1–3.5）", () => {
  const CHART_SPEC = {
    title: "看板",
    panels: [
      {
        id: "l1",
        type: "line",
        title: "折线",
        span: 2,
        snapshot: { columns: ["月份", "值"], rows: [["7月", 1]] },
      },
      {
        id: "k1",
        type: "kpi",
        title: "指标",
        span: 1,
        snapshot: { columns: ["value"], rows: [[1]] },
      },
    ],
  };

  it("默认不渲染工具栏（分享页红线靠默认值守住）", () => {
    render(<DashboardRenderer spec={CHART_SPEC} />);
    expect(screen.queryByTestId("dashboard-panel-toolbar")).toBeNull();
    expect(screen.queryByTestId("panel-tool-export")).toBeNull();
    expect(screen.getByTestId("dashboard-renderer").getAttribute("data-toolbar")).toBe("off");
  });

  it("toolbar=true 时图表面板出现工具栏，kpi 面板不出现", () => {
    render(<DashboardRenderer spec={CHART_SPEC} toolbar />);
    // 只有一个图表面板 → 一个工具栏
    expect(screen.getAllByTestId("dashboard-panel-toolbar")).toHaveLength(1);
    expect(screen.getByTestId("panel-tool-fullscreen")).toBeTruthy();
    expect(screen.getByTestId("panel-tool-table")).toBeTruthy();
    expect(screen.getByTestId("panel-tool-copy")).toBeTruthy();
    expect(screen.getByTestId("dashboard-renderer").getAttribute("data-toolbar")).toBe("on");
  });

  it("图数切换：切到表格呈现快照同源数据，按钮文案随之切换", () => {
    render(<DashboardRenderer spec={CHART_SPEC} toolbar />);
    const toggle = screen.getByTestId("panel-tool-table");
    expect(toggle.textContent).toBe("看表");

    fireEvent.click(toggle);
    expect(screen.getByTestId("dashboard-table")).toBeTruthy();
    expect(screen.getByTestId("panel-tool-table").textContent).toBe("看图");
    // 图表容器让位给表格
    expect(screen.queryByTestId("dashboard-chart")).toBeNull();

    fireEvent.click(screen.getByTestId("panel-tool-table"));
    expect(screen.getByTestId("dashboard-chart")).toBeTruthy();
  });

  it("全屏：点击后图表容器标记切换，再次点击恢复", () => {
    render(<DashboardRenderer spec={CHART_SPEC} toolbar />);
    const host = screen.getByTestId("dashboard-chart");
    expect(host.getAttribute("data-fullscreen")).toBe("off");

    fireEvent.click(screen.getByTestId("panel-tool-fullscreen"));
    expect(screen.getByTestId("dashboard-chart").getAttribute("data-fullscreen")).toBe("on");

    fireEvent.click(screen.getByTestId("panel-tool-fullscreen"));
    expect(screen.getByTestId("dashboard-chart").getAttribute("data-fullscreen")).toBe("off");
  });

  it("raw_option 面板不可表格化时如实提示", () => {
    render(
      <DashboardRenderer
        toolbar
        spec={{
          title: "",
          panels: [
            {
              id: "r",
              type: "line",
              title: "无法表格化",
              span: 1,
              raw_option: { series: [{ name: "s" }] }, // 无 data
            },
          ],
        }}
      />,
    );
    fireEvent.click(screen.getByTestId("panel-tool-table"));
    expect(screen.getByTestId("dashboard-raw-not-tabular").textContent).toContain(
      "无法转为表格",
    );
  });

  it("raw_option 面板可表格化时按系列抽取", () => {
    render(
      <DashboardRenderer
        toolbar
        spec={{
          title: "",
          panels: [
            {
              id: "r",
              type: "line",
              title: "成品",
              span: 1,
              raw_option: {
                xAxis: { type: "category", data: ["2024", "2025"] },
                series: [{ name: "营收", type: "line", data: [1.2, 1.5] }],
              },
            },
          ],
        }}
      />,
    );
    fireEvent.click(screen.getByTestId("panel-tool-table"));
    const table = screen.getByTestId("dashboard-table");
    expect(table.textContent).toContain("营收");
    expect(table.textContent).toContain("1.2");
    expect(table.textContent).toContain("2024");
  });
});

describe("List panel (list type)", () => {
  it("Three-row snapshot renders three dashboard-list-item cards in snapshot order", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: {
            columns: ["title", "summary", "image", "time", "source", "link"],
            rows: [
              ["Title1", "Summary1", "https://example.com/img1.jpg", "2026-10-21", "Source1", "https://example.com/link1"],
              ["Title2", "Summary2", "data:image/png;base64,...", "2026-10-20", "Source2", "javascript:alert(1)"],
              ["Title3", "Summary3", "https://example.com/img3.jpg", "2026-10-19", "Source3", "https://example.com/link3"],
            ],
          },
          encoding: {
            title: "title",
            summary: "summary",
            image: "image",
            time: "time",
            source: "source",
            link: "link",
          },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    const items = screen.getAllByTestId("dashboard-list-item");
    expect(items).toHaveLength(3);
    expect(items[0].textContent).toContain("Title1");
    expect(items[1].textContent).toContain("Title2");
    expect(items[2].textContent).toContain("Title3");
  });

  it("config.limit: 2 with three rows renders only two cards", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          config: { limit: 2 },
          snapshot: {
            columns: ["title"],
            rows: [["Row1"], ["Row2"], ["Row3"]],
          },
          encoding: { title: "title" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    expect(screen.getAllByTestId("dashboard-list-item")).toHaveLength(2);
  });

  it("Valid https image renders an <img> with loading='lazy' and referrerpolicy='no-referrer'", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: {
            columns: ["title", "image"],
            rows: [["Title", "https://example.com/image.jpg"]],
          },
          encoding: { title: "title", image: "image" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    // alt="" 是装饰性图片（无可访问名），因此按 DOM 查询而非 role。
    const img = document.querySelector("img");
    expect(img).not.toBeNull();
    expect(img!.getAttribute("loading")).toBe("lazy");
    expect(img!.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("data:image/png/base64 value renders no img element", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: {
            columns: ["title", "image"],
            rows: [["Title", "data:image/png;base64,..."]],
          },
          encoding: { title: "title", image: "image" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("Simulate image error with fireEvent.error(img) and assert the image block is gone", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: {
            columns: ["title", "image"],
            rows: [["Title", "https://example.com/image.jpg"]],
          },
          encoding: { title: "title", image: "image" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    const img = document.querySelector("img");
    expect(img).not.toBeNull();
    fireEvent.error(img!);
    expect(document.querySelector("img")).toBeNull();
  });

  it("A javascript:alert(1) link produces no <a> element", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: {
            columns: ["title", "link"],
            rows: [["Title", "javascript:alert(1)"]],
          },
          encoding: { title: "title", link: "link" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("A valid https link produces an anchor with target='_blank' and rel='noopener noreferrer'", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: {
            columns: ["title", "link"],
            rows: [["Title", "https://example.com"]],
          },
          encoding: { title: "title", link: "link" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("A title containing <script>x</script> is rendered as literal text", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: {
            columns: ["title"],
            rows: [["<script>alert(1)</script>"]],
          },
          encoding: { title: "title" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    expect(screen.getByText("<script>alert(1)</script>")).not.toBeNull();
    expect(document.querySelector("script")).toBeNull();
  });

  it("Empty snapshot shows dashboard-no-data", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          // 空快照的合法形态：列声明存在、零行（title 角色仍可校验）。
          snapshot: { columns: ["title"], rows: [] },
          encoding: { title: "title" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} />);
    expect(screen.getByTestId("dashboard-no-data")).not.toBeNull();
  });

  it("A list panel with toolbar prop true must NOT render dashboard-panel-toolbar", () => {
    const spec = {
      panels: [
        {
          id: "list1",
          type: "list",
          snapshot: { columns: ["title"], rows: [["Title"]] },
          encoding: { title: "title" },
        },
      ],
    };
    render(<DashboardRenderer spec={spec} toolbar={true} />);
    expect(screen.queryByTestId("dashboard-panel-toolbar")).toBeNull();
  });
});
