// 橱窗页组件测试（wire-showcase-market 任务 4.1–4.4）：
// 列表渲染 / 详情出图 / 只读红线（无工具栏、无导出、无编辑）/ 不注入 noindex /
// 零租户标识。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { ShowcaseView } from "./ShowcaseView";

const CARD = {
  id: 7,
  kind: "dashboard",
  title: "中国宏观景气",
  version: 3,
  published_at: "2026-10-20T01:00:00Z",
  updated_at: "2026-10-20T01:00:00Z",
  panel_count: 5,
};

const DETAIL = {
  ...CARD,
  panels: [{ id: "p1", type: "kpi", title: "CPI" }],
  spec: {
    title: "中国宏观景气",
    spec: {
      panels: [
        {
          id: "p1",
          type: "kpi",
          title: "CPI 同比",
          span: 1,
          snapshot: { columns: ["value"], rows: [[2.1]] },
          config: { unit: "%" },
        },
        {
          id: "p2",
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
    },
  },
  refreshed_at: "2026-10-20T00:30:00Z",
};

function stubFetch(payload: unknown, status = 200) {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  i18n.changeLanguage("zh");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // 清掉用例可能注入的 robots 标签
  document.head.querySelectorAll('meta[name="robots"]').forEach((el) => el.remove());
});

describe("橱窗列表（任务 4.1/4.2）", () => {
  it("渲染条目（标题 + 面板数），链接指向详情", async () => {
    const fetchMock = stubFetch({ items: [CARD], total: 1 });
    render(<ShowcaseView />);

    const item = await screen.findByTestId("showcase-item");
    expect(item.textContent).toContain("中国宏观景气");
    expect(item.textContent).toContain("5 个面板");
    expect(item.getAttribute("href")).toBe("#/showcase/7");

    // 匿名请求：不带任何 Authorization 头
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init?.headers as Record<string, string>)?.authorization).toBeUndefined();
  });

  it("空橱窗如实提示（不报错、不显示假条目）", async () => {
    stubFetch({ items: [], total: 0 });
    render(<ShowcaseView />);
    expect(await screen.findByTestId("showcase-empty")).toBeTruthy();
    expect(screen.queryByTestId("showcase-item")).toBeNull();
  });

  it("加载失败如实呈现并给重试", async () => {
    stubFetch({ detail: "boom" }, 500);
    render(<ShowcaseView />);
    expect(await screen.findByTestId("showcase-error")).toBeTruthy();
    expect(screen.getByText("重试")).toBeTruthy();
  });
});

describe("橱窗详情（任务 4.1/4.3）", () => {
  it("渲染看板（复用渲染器）且含数据", async () => {
    stubFetch(DETAIL);
    render(<ShowcaseView detailId={7} />);

    expect(await screen.findByTestId("showcase-detail-title")).toHaveProperty(
      "textContent",
      "中国宏观景气",
    );
    expect(await screen.findByTestId("dashboard-renderer")).toBeTruthy();
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("2.1");
  });

  it("list 面板在橱窗匿名面正常出卡片（图文信息流可对外展示）", async () => {
    stubFetch({
      ...DETAIL,
      panels: [{ id: "f1", type: "list", title: "最新动态" }],
      spec: {
        title: "行业资讯流",
        spec: {
          title: "行业资讯流",
          panels: [
            {
              id: "f1",
              type: "list",
              title: "最新动态",
              span: 3,
              encoding: { title: "title", image: "image_url", time: "date" },
              snapshot: {
                columns: ["title", "image_url", "date"],
                rows: [
                  ["标题甲", "https://example.com/a.jpg", "2026-10-20"],
                  ["标题乙", "", "2026-10-19"],
                ],
              },
            },
          ],
        },
      },
    });
    render(<ShowcaseView detailId={7} />);

    expect(await screen.findByTestId("dashboard-list")).toBeTruthy();
    expect(
      document.querySelectorAll('[data-testid="dashboard-list-item"]'),
    ).toHaveLength(2);
    // 只读红线不变
    expect(screen.queryByTestId("dashboard-panel-toolbar")).toBeNull();
    expect(screen.queryByTestId("panel-tool-export")).toBeNull();
  });

  it("只读红线：无工具栏 / 无导出 / 无编辑入口", async () => {
    stubFetch(DETAIL);
    render(<ShowcaseView detailId={7} />);
    await screen.findByTestId("dashboard-renderer");

    // 渲染器以 toolbar=off 工作
    expect(
      screen.getByTestId("dashboard-renderer").getAttribute("data-toolbar"),
    ).toBe("off");
    // 工具栏四件与整板导出全部缺席
    expect(screen.queryByTestId("dashboard-panel-toolbar")).toBeNull();
    expect(screen.queryByTestId("panel-tool-fullscreen")).toBeNull();
    expect(screen.queryByTestId("panel-tool-export")).toBeNull();
    expect(screen.queryByTestId("panel-tool-table")).toBeNull();
    expect(screen.queryByTestId("panel-tool-copy")).toBeNull();
    expect(screen.queryByTestId("dashboard-export-csv")).toBeNull();
    // 无编辑/保存
    expect(screen.queryByTestId("dashboard-edit")).toBeNull();
    expect(screen.queryByTestId("editor-save")).toBeNull();
  });

  it("不出现任何租户标识", async () => {
    stubFetch(DETAIL);
    render(<ShowcaseView detailId={7} />);
    const title = await screen.findByTestId("showcase-detail-title");
    const page = title.closest("[data-testid='showcase-detail']");
    const text = page?.textContent ?? "";
    expect(text).not.toContain("finddata-official");
    expect(text).not.toContain("publisher_customer_id");
    expect(text).not.toContain("customer");
  });

  it("数据截至时间如实呈现", async () => {
    stubFetch(DETAIL);
    render(<ShowcaseView detailId={7} />);
    await screen.findByTestId("showcase-detail-title");
    expect(screen.getByText(/数据截至/)).toBeTruthy();
  });
});

describe("可索引（任务 4.4）", () => {
  it("橱窗页不注入 robots=noindex（与登录市场面相反）", async () => {
    stubFetch({ items: [CARD], total: 1 });
    render(<ShowcaseView />);
    await screen.findByTestId("showcase-item");
    const robots = document.head.querySelector('meta[name="robots"]');
    expect(robots).toBeNull();
  });
});
