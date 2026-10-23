// 信息流来源契约与滞后标注（wire-analysis-console 4.5/4.6）
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { DashboardRenderer } from "./DashboardRenderer";
import { normalizeInfoQuery, parseDashboardSpec } from "./spec";

beforeEach(() => i18n.changeLanguage("zh"));
afterEach(() => cleanup());

const PANEL = {
  id: "l1",
  type: "list",
  title: "公告流",
  span: 3,
  encoding: { title: "标题", summary: "摘要", time: "时间", source: "来源", link: "链接" },
  snapshot: { columns: ["标题", "摘要", "时间", "来源", "链接"], rows: [["公告一", "摘要", "2026-10-20", "cninfo", "https://x/1"]] },
  info_query: { sources: ["cninfo"], source_types: ["announcement"], published_after: "2026-10-01", limit: 50 },
};

describe("info_query 归一", () => {
  it("保留合法字段并钳制 limit", () => {
    expect(normalizeInfoQuery({ sources: ["cninfo"], limit: 999 })).toEqual({
      sources: ["cninfo"],
      limit: 20,
    });
    expect(normalizeInfoQuery({ published_after: "2026/10/01" })?.published_after).toBeUndefined();
    expect(normalizeInfoQuery("x")).toBeUndefined();
  });

  it("解析面板时保留 info_query 与 info_lag", () => {
    const parsed = parseDashboardSpec({
      title: "板",
      spec: { title: "板", panels: [{ ...PANEL, info_lag: { at: "2026-10-23", reason: "来源不可用" } }] },
    });
    expect(parsed.kind).toBe("v1");
    if (parsed.kind !== "v1") return;
    expect(parsed.panels[0].info_query).toEqual(PANEL.info_query);
    expect(parsed.panels[0].info_lag).toEqual({ at: "2026-10-23", reason: "来源不可用" });
  });
});

describe("滞后标注渲染", () => {
  it("保留快照并显示滞后原因", () => {
    const parsed = parseDashboardSpec({
      title: "板",
      spec: {
        title: "板",
        panels: [{ ...PANEL, info_lag: { at: "2026-10-23T00:00:00Z", reason: "fd-info 不可用" } }],
      },
    });
    if (parsed.kind !== "v1") throw new Error("expected v1");
    render(<DashboardRenderer spec={{ title: "板", panels: parsed.panels }} />);
    expect(screen.getByTestId("dashboard-list-lag").textContent).toContain("fd-info 不可用");
    expect(screen.getAllByTestId("dashboard-list-item").length).toBe(1);
  });
});
