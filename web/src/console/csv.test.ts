// CSV 导出纯函数单测（wire-dashboard-export 任务 1.1/1.3）：
// - csvEscape：RFC4180 转义边界——逗号/双引号/换行（\r、\n）触发包裹与
//   引号翻倍；null → 空串；数值 String() 原样（含 0/负数/小数）；
// - toCsv：列头 + 数据行 CRLF 拼接、单元格转义透传、空表只有列头行；
// - boardToCsv：UTF-8 BOM 前缀、逐面板 section（注释行 + 快照表 + 空行
//   分隔）、无快照面板只有注释行、空板/legacy（null）仅标题行。
import { describe, expect, it } from "vitest";
import { boardToCsv, csvEscape, rawOptionToTable, toCsv } from "./csv";
import type { DashboardPanel } from "./dashboard/spec";

describe("csvEscape（RFC4180 单格转义）", () => {
  it("普通文本不加引号原样返回", () => {
    expect(csvEscape("煤价")).toBe("煤价");
    expect(csvEscape("2026-09")).toBe("2026-09");
    expect(csvEscape("")).toBe("");
  });

  it("null → 空串（快照缺格子如实空值）", () => {
    expect(csvEscape(null)).toBe("");
  });

  it("数值 String() 原样：整数/小数/0/负数不失真不引号", () => {
    expect(csvEscape(850)).toBe("850");
    expect(csvEscape(2.1)).toBe("2.1");
    expect(csvEscape(0)).toBe("0");
    expect(csvEscape(-3.5)).toBe("-3.5");
  });

  it("含逗号 → 双引号包裹", () => {
    expect(csvEscape("煤价, 环比")).toBe('"煤价, 环比"');
  });

  it("含双引号 → 包裹 + 内部引号翻倍", () => {
    expect(csvEscape('他说"涨"了')).toBe('"他说""涨""了"');
  });

  it("含换行（\\n 或 \\r）→ 双引号包裹（CRLF 内容原样保留）", () => {
    expect(csvEscape("第一行\n第二行")).toBe('"第一行\n第二行"');
    expect(csvEscape("第一行\r\n第二行")).toBe('"第一行\r\n第二行"');
    expect(csvEscape(" lone\r")).toBe('" lone\r"');
  });
});

describe("toCsv（列头 + 数据行）", () => {
  it("列头 + 数据行以 CRLF 拼接，末行不带尾换行", () => {
    expect(
      toCsv(["月份", "值"], [
        ["7月", 49.4],
        ["8月", 49.1],
      ]),
    ).toBe("月份,值\r\n7月,49.4\r\n8月,49.1");
  });

  it("单元格转义透传：逗号/引号/换行/null 混合行", () => {
    expect(toCsv(["a,b", 'q"q'], [["x,y", null], ["l\nm", 1]])).toBe(
      '"a,b","q""q"\r\n"x,y",\r\n"l\nm",1',
    );
  });

  it("空表（零行）只有列头行；零列零行为空串", () => {
    expect(toCsv(["月份"], [])).toBe("月份");
    expect(toCsv([], [])).toBe("");
  });
});

/** 最小面板工厂（补齐 spec 契约必填字段）。 */
function panel(partial: Partial<DashboardPanel>): DashboardPanel {
  return { id: "p1", type: "kpi", title: "", span: 1, ...partial };
}

describe("boardToCsv（整板合成）", () => {
  it("UTF-8 BOM 前缀 + 逐面板 section：注释行（标题（type））+ 快照表 + 空行分隔", () => {
    const csv = boardToCsv("宏观仪表盘", [
      panel({
        id: "k1",
        type: "kpi",
        title: "CPI 同比",
        snapshot: { columns: ["value"], rows: [[2.1]] },
      }),
      panel({
        id: "t1",
        type: "table",
        title: "月度明细",
        span: 3,
        snapshot: {
          columns: ["月份", "值"],
          rows: [
            ["7月", 49.4],
            ["8月", 49.1],
          ],
        },
      }),
    ]);
    expect(csv).toBe(
      "\uFEFF" +
        "# CPI 同比（kpi）\r\n" +
        "value\r\n" +
        "2.1" +
        "\r\n\r\n" +
        "# 月度明细（table）\r\n" +
        "月份,值\r\n" +
        "7月,49.4\r\n" +
        "8月,49.1",
    );
    expect(csv.charCodeAt(0)).toBe(0xfeff); // BOM
    expect(csv.includes("\r\n\r\n")).toBe(true); // 空行分隔
  });

  it("无快照面板只有注释行（如实空结构，前后空行分隔照常）", () => {
    const csv = boardToCsv("板", [
      panel({ id: "a", type: "line", title: "空图", snapshot: undefined }),
      panel({
        id: "b",
        type: "bar",
        title: "有数",
        snapshot: { columns: ["x"], rows: [[1]] },
      }),
    ]);
    expect(csv).toBe("\uFEFF# 空图（line）\r\n\r\n# 有数（bar）\r\nx\r\n1");
  });

  it("快照全空（列头零列零行）照常导出：注释行 + 空表体，不报错", () => {
    const csv = boardToCsv("板", [
      panel({
        id: "a",
        type: "table",
        title: "空表",
        snapshot: { columns: [], rows: [] },
      }),
    ]);
    expect(csv).toBe("\uFEFF# 空表（table）\r\n");
  });

  it("panels 空数组 → 仅 `# {title}` 一行（空板如实）", () => {
    expect(boardToCsv("空板", [])).toBe("\uFEFF# 空板");
  });

  it("legacy（panels = null）→ 仅 `# {title}` 一行", () => {
    expect(boardToCsv("旧版看板", null)).toBe("\uFEFF# 旧版看板");
  });

  it("标题原样进内容（不清洗）——文件名清洗是调用方职责", () => {
    expect(boardToCsv("Q3/汇报", null)).toBe("\uFEFF# Q3/汇报");
  });
});

describe("raw_option 面板导出（wire-chart-contract-v2 任务 7.3）", () => {
  it("rawOptionToTable：按系列抽两列 + x 轴类目作首列", () => {
    const table = rawOptionToTable({
      xAxis: { type: "category", data: ["2024", "2025"] },
      series: [
        { name: "营收", type: "line", data: [1.2, 1.5] },
        { name: "净利", type: "line", data: [0.2, 0.3] },
      ],
    });
    expect(table?.columns).toEqual(["x", "营收", "净利"]);
    expect(table?.rows).toEqual([
      ["2024", 1.2, 0.2],
      ["2025", 1.5, 0.3],
    ]);
  });

  it("rawOptionToTable：数据点对 [x, y] 取 y；无 xAxis.data 则不加首列", () => {
    const table = rawOptionToTable({
      series: [{ name: "值", type: "line", data: [[Date.UTC(2026, 0, 1), 7]] }],
    });
    expect(table?.columns).toEqual(["值"]);
    expect(table?.rows).toEqual([[7]]);
  });

  it("rawOptionToTable：抽不出返回 null（无 series / 无 data / 空数组）", () => {
    expect(rawOptionToTable({})).toBeNull();
    expect(rawOptionToTable({ series: [] })).toBeNull();
    expect(rawOptionToTable({ series: [{ name: "s" }] })).toBeNull();
    expect(rawOptionToTable({ series: [{ name: "s", data: [] }] })).toBeNull();
  });

  it("整板导出：raw_option 面板参与导出，普通面板照常", () => {
    const panels: DashboardPanel[] = [
      {
        id: "t1",
        type: "table",
        title: "明细",
        span: 1,
        snapshot: { columns: ["月", "值"], rows: [["7月", 1]] },
      },
      {
        id: "r1",
        type: "line",
        title: "成品图",
        span: 1,
        raw_option: {
          xAxis: { type: "category", data: ["A"] },
          series: [{ name: "s", type: "line", data: [9] }],
        },
      },
    ];
    const csv = boardToCsv("看板", panels);
    expect(csv).toContain("# 明细（table）");
    expect(csv).toContain("月,值");
    expect(csv).toContain("# 成品图（line）");
    expect(csv).toContain("x,s");
    expect(csv).toContain("A,9");
  });

  it("不可表格化的 raw_option 面板：标注未导出且不中断其余面板", () => {
    const panels: DashboardPanel[] = [
      {
        id: "t1",
        type: "table",
        title: "明细",
        span: 1,
        snapshot: { columns: ["月"], rows: [["7月"]] },
      },
      {
        id: "r1",
        type: "line",
        title: "不可表格化",
        span: 1,
        raw_option: { series: [{ name: "无数据" }] },
      },
    ];
    const csv = boardToCsv("看板", panels);
    expect(csv).toContain("# 明细（table）");
    expect(csv).toContain("7月");
    expect(csv).toContain("无法表格化");
  });

  it("list 面板按快照行原样导出（wire-list-panel：与 table 同口径）", () => {
    const panels: DashboardPanel[] = [
      {
        id: "f1",
        type: "list",
        title: "资讯流",
        span: 3,
        encoding: { title: "title", summary: "summary", link: "url" },
        snapshot: {
          columns: ["title", "summary", "url"],
          rows: [["标题甲", "摘要甲", "https://example.com/a"]],
        },
      },
    ];
    const csv = boardToCsv("看板", panels);
    expect(csv).toContain("# 资讯流（list）");
    expect(csv).toContain("title,summary,url");
    expect(csv).toContain("标题甲,摘要甲,https://example.com/a");
  });
});
