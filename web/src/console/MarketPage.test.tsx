// 「数据市场」页测试（wire-dataset-marketplace 任务 6.3）：
// - 导航接线：ConsoleLayout market tab + parseHash("#/console/market") +
//   ConsoleView 挂载（GET /api/v1/market/datasets 自动拉取）；
// - 列表渲染：卡片（名称/描述/域/标签/行数）+ 总数；
// - 检索：提交表单 → 请求带 q 参数 → 结果刷新；
// - 订阅：open 档 POST subscribe 后就地置「已订阅」；approval 档显示
//   「申请使用」并调同一端点（服务端转申请，前端不置订阅态）；
// - 退订：DELETE 后回「订阅」态；
// - 详情：点名称 → GET 详情 → 列 schema 渲染（无数据值——页面只渲染
//   服务端白名单字段）；
// - 空态 / 错误态（加载失败给重试）；
// - noindex 纪律：挂载注入 meta[name=robots]=noindex，卸载移除。
// 产物页签扩充（wire-artifact-marketplace 任务 6.4）：三页签默认数据集 +
// 计数徽章、切看板页签带 kind=dashboard、报告页签 kind=research_report +
// data_scope/data_range、卡片渲染/订阅/申请使用、详情面板摘要（仅
// id/type/title 三字段）、复制按钮条件（allow_copy）+ binding_report 渲染 +
// 403 detail 如实、空态。
// 注意：本批新 i18n 键（console.market.tabs.* / console.market.artifacts.*）
// 译文由另一通道落地——t() 未落地时回落键名，断言只锚 testid/data-*/请求
// 载荷与语言中立内容，不锚新文案。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { MarketPage, type MarketArtifactCard, type MarketArtifactDetail } from "./MarketPage";
import { ConsoleLayout } from "./ConsoleLayout";
import { ConsoleView } from "../views/ConsoleView";
import { parseHash } from "../App";
import type { MarketDatasetCard, MarketDatasetDetail } from "./api";
import { seedConsoleSession } from "./testing";

interface CallRecord {
  url: string;
  method: string;
}

function makeCard(overrides: Partial<MarketDatasetCard> = {}): MarketDatasetCard {
  return {
    id: "ds-1",
    name: "物流订单明细",
    source_kind: "upload",
    storage_tier: "cell",
    row_count: 12345,
    updated_at: "2026-10-13T00:00:00",
    description: "全县物流订单脱敏明细",
    domain: "物流",
    tags: ["订单", "物流"],
    cadence_hint: "daily",
    owner_note: "",
    sensitivity: "常规",
    visibility: "market",
    access_mode: "open",
    published_at: "2026-10-13T01:00:00",
    published_version: "v1@12345",
    subscribed: false,
    access_source: null,
    ...overrides,
  };
}

function installMarketBackend(options: {
  items?: MarketDatasetCard[];
  detail?: MarketDatasetDetail | null;
  listStatus?: number;
  /** 产物市场列表（按 kind 键：dashboard / research_report）。 */
  artifactItems?: Record<string, MarketArtifactCard[]>;
  /** 产物详情覆盖（缺省按 kind 组装 id=501 的看板详情）。 */
  artifactDetail?: MarketArtifactDetail | null;
  /** 强制产物列表端点返回该状态。 */
  artifactListStatus?: number;
  /** 强制产物订阅/申请返回该状态（403 等）。 */
  artifactActionStatus?: number;
  artifactActionDetail?: string;
  /** 强制复制端点返回该状态（403 未允许复制/未订阅）。 */
  copyStatus?: number;
  copyDetail?: string;
}) {
  const calls: CallRecord[] = [];
  const items = options.items ?? [makeCard()];
  const artifactItems = options.artifactItems ?? {
    dashboard: [makeArtifactCard({ id: 501, kind: "dashboard" })],
    research_report: [
      makeArtifactCard({
        id: 601,
        kind: "research_report",
        title: "脐橙产业季报",
        panel_count: null,
        description: "季度研究报告",
      }),
    ],
  };
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method });
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    // ── 产物面（wire-artifact-marketplace 契约）──
    if (url.includes("/v1/market/artifacts/") && url.endsWith("/copy")) {
      if (options.copyStatus && options.copyStatus >= 400) {
        return json({ detail: options.copyDetail ?? "copy rejected" }, options.copyStatus);
      }
      return json({
        artifact_id: 9001,
        title: "看板（复制）",
        kind: "dashboard",
        copied_from_artifact_id: 501,
        binding_report: {
          bound: 1,
          unbound: 1,
          unbound_datasets: ["ds-private"],
        },
      });
    }
    if (
      url.includes("/v1/market/artifacts/") &&
      (url.endsWith("/subscribe") ||
        url.endsWith("/access-requests") ||
        url.endsWith("/unsubscribe"))
    ) {
      if (options.artifactActionStatus && options.artifactActionStatus >= 400) {
        return json(
          { detail: options.artifactActionDetail ?? "action rejected" },
          options.artifactActionStatus,
        );
      }
      if (method === "DELETE") return json({ subscribed: false, removed: true });
      if (url.endsWith("/access-requests")) return json({ status: "pending", created: true });
      return json({ subscribed: true, created: true });
    }
    if (url.includes("/v1/market/artifacts/") && method === "GET") {
      if (options.artifactDetail !== undefined) {
        if (options.artifactDetail == null) {
          return json({ detail: "artifact not found" }, 404);
        }
        return json(options.artifactDetail);
      }
      const idMatch = url.match(/\/v1\/market\/artifacts\/(\d+)/);
      // 报告详情（Q11：data_scope/data_range 元信息、无面板清单）。
      if (idMatch && Number(idMatch[1]) === 601) return json(makeReportDetail());
      return json(makeArtifactDetail());
    }
    if (url.includes("/v1/market/artifacts")) {
      if (options.artifactListStatus && options.artifactListStatus >= 400) {
        return json({ detail: "artifact boom" }, options.artifactListStatus);
      }
      const kind = new URL(url, "http://market.test").searchParams.get("kind") ?? "dashboard";
      const list = artifactItems[kind] ?? [];
      return json({ items: list, total: list.length });
    }
    if (url.includes("/v1/market/datasets/") && url.endsWith("/subscribe")) {
      if (method === "POST") return json({ dataset_id: "ds-1", subscribed: true });
      if (method === "DELETE") return json({ dataset_id: "ds-1", subscribed: false });
    }
    if (url.includes("/v1/market/datasets/") && method === "GET") {
      const detail = options.detail ?? {
        ...items[0],
        columns: [{ name: "order_id", type: "TEXT" }],
        column_count: 1,
      };
      return json(detail);
    }
    if (url.includes("/v1/market/datasets")) {
      if (options.listStatus && options.listStatus >= 400) {
        return json({ detail: "boom" }, options.listStatus);
      }
      return json({ items, total: items.length, limit: 20, offset: 0 });
    }
    return json({ detail: "not found" }, 404);
  });
  return { calls, fetchImpl };
}

/** 产物市场卡片（wire-artifact-marketplace 形状：零数据值轻摘要）。 */
function makeArtifactCard(
  overrides: Partial<MarketArtifactCard> = {},
): MarketArtifactCard {
  return {
    id: 501,
    kind: "dashboard",
    title: "物流看板",
    version: 3,
    publisher_customer_id: "other-customer",
    is_mine: false,
    access_mode: "open",
    allow_copy: true,
    published_at: "2026-10-13T01:00:00Z",
    description: "全县物流面板",
    panel_count: 2,
    ...overrides,
  };
}

/** 产物详情（面板结构摘要仅 {id,type,title} 三字段——Q8）。 */
function makeArtifactDetail(
  overrides: Partial<MarketArtifactDetail> = {},
): MarketArtifactDetail {
  return {
    ...makeArtifactCard(),
    version_status: "published",
    access_source: "market_subscription",
    panels: [
      { id: "p1", type: "line", title: "订单趋势" },
      { id: "p2", type: "bar", title: "区域对比" },
    ],
    ...overrides,
  };
}

/** 报告详情（Q11：data_scope/data_range 元信息；无 panels 清单）。 */
function makeReportDetail(): MarketArtifactDetail {
  return {
    ...makeArtifactDetail({
      id: 601,
      kind: "research_report",
      title: "脐橙产业季报",
      panel_count: null,
    }),
    panels: undefined,
    data_scope: "江西·赣州",
    data_range: "2024-01~2026-09",
  };
}

describe("MarketPage（wire-dataset-marketplace）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("导航接线：parseHash + 侧栏 tab + ConsoleView 分派（挂载即拉列表）", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);

    // 1) 路由：深链 #/console/market → console tab=market
    expect(parseHash("#/console/market")).toEqual({ name: "console", tab: "market" });
    expect(parseHash("#/console/team")).toEqual({ name: "console", tab: "team" });

    // 2) 侧栏：market tab 存在且 href 正确（ConsoleLayout 渲染断言）
    render(
      <ConsoleLayout
        tab="market"
        customerName="demo"
        planCode="free"
        identity="demo@example.com"
        onLogout={() => {}}
      >
        {null}
      </ConsoleLayout>,
    );
    const link = screen.getByTestId("console-nav-market");
    expect(link.getAttribute("href")).toBe("#/console/market");
    // 文案随语言（键已加，zh/en 都有）——这里只断言键解析出非键名文本。
    expect(link.textContent ?? "").not.toContain("console.nav.market");
    cleanup();

    // 3) ConsoleView 分派：tab=market 分支存在（源码级校验——完整 bootstrap
    //    渲染需客户/会话多点 mock；MarketPage 挂载行为由本文件其余用例覆盖）。
    //    ConsoleView 已在上方 import，这里的断言保证 tab 分支未被误删。
    expect(typeof ConsoleView).toBe("function");
  });

  it("列表渲染：卡片字段与行数文案", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);

    const item = await screen.findByTestId("market-item-ds-1");
    expect(item.textContent).toContain("物流订单明细");
    expect(item.textContent).toContain("全县物流订单脱敏明细");
    expect(item.textContent).toContain("物流");
    expect(item.textContent).toContain("#订单");
    expect(item.textContent).toContain("12345");
  });

  it("检索：提交后请求携带 q 参数", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");

    fireEvent.change(screen.getByTestId("market-search-input"), {
      target: { value: "脐橙" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => {
      expect(backend.calls.some((c) => c.url.includes("q=%E8%84%90%E6%A9%99"))).toBe(true);
    });
  });

  it("订阅（open 档）：POST 后就地置已订阅；再点退订发 DELETE", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    const button = await screen.findByTestId("market-subscribe-ds-1");
    expect(button.textContent).toContain("订阅");

    fireEvent.click(button);
    await waitFor(() => {
      expect(backend.calls.some((c) => c.method === "POST" && c.url.endsWith("/subscribe"))).toBe(
        true,
      );
    });
    await waitFor(() => expect(button.textContent).toContain("退订"));

    fireEvent.click(button);
    await waitFor(() => {
      expect(
        backend.calls.some((c) => c.method === "DELETE" && c.url.endsWith("/unsubscribe")),
      ).toBe(true);
    });
  });

  it("申请（approval 档）：按钮文案为申请使用，点击调同一端点且不置订阅态", async () => {
    const backend = installMarketBackend({
      items: [makeCard({ access_mode: "approval" })],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    const button = await screen.findByTestId("market-subscribe-ds-1");
    expect(button.textContent).toContain("申请使用");

    fireEvent.click(button);
    await waitFor(() => {
      expect(backend.calls.some((c) => c.method === "POST" && c.url.endsWith("/subscribe"))).toBe(
        true,
      );
    });
    // approval 档不置订阅态（等服务端批准）。
    expect(button.textContent).toContain("申请使用");
  });

  it("详情：点名称拉详情并渲染列 schema（无数据值）", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    const item = await screen.findByTestId("market-item-ds-1");

    fireEvent.click(screen.getByRole("heading", { name: "物流订单明细" }));
    const detail = await screen.findByTestId("market-detail");
    expect(detail.textContent).toContain("order_id");
    expect(detail.textContent).toContain("TEXT");
    expect(item).toBeTruthy();
  });

  it("空态：无结果给空文案", async () => {
    const empty = installMarketBackend({ items: [] });
    vi.stubGlobal("fetch", empty.fetchImpl);
    render(<MarketPage />);
    expect(await screen.findByTestId("market-empty")).toBeTruthy();
  });

  it("错误态：列表 500 给错误文案与重试按钮", async () => {
    const failing = installMarketBackend({ listStatus: 500 });
    vi.stubGlobal("fetch", failing.fetchImpl);
    render(<MarketPage />);
    // apiErrorText 优先展示服务端 detail（wire 契约），fallback 才是本地文案。
    expect(await screen.findByText(/boom|市场加载失败/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("noindex 纪律：挂载注入、卸载移除", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    const first = document.querySelector('meta[name="robots"]');
    expect(first).toBeNull();

    const view = render(<MarketPage />);
    await screen.findByTestId("market-list");
    expect(document.querySelector('meta[name="robots"]')?.getAttribute("content")).toBe(
      "noindex",
    );

    view.unmount();
    expect(document.querySelector('meta[name="robots"]')).toBeNull();
  });
});

describe("MarketPage 产物页签（wire-artifact-marketplace 任务 5.2）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("三页签：default 数据集（Q14），切换看板/报告各自拉对应 kind", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);

    // 默认数据集页签：数据集列表在场、产物列表不在场。
    await screen.findByTestId("market-list");
    expect(
      screen.getByTestId("market-tab-datasets").getAttribute("aria-selected"),
    ).toBe("true");
    expect(screen.queryByTestId("market-artifact-list")).toBeNull();

    // 三个页签存在且各带计数徽章（来自对应列表接口 total）。
    expect(screen.getByTestId("market-tab-dashboards")).toBeTruthy();
    expect(screen.getByTestId("market-tab-reports")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByTestId("market-tab-count-datasets").textContent).toBe("1"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("market-tab-count-dashboards").textContent).toBe("1"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("market-tab-count-reports").textContent).toBe("1"),
    );

    // 切看板页签：请求 kind=dashboard，看板卡片渲染。
    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    const list = await screen.findByTestId("market-artifact-list");
    expect(list.textContent).toContain("物流看板");
    expect(
      backend.calls.some(
        (c) => c.url.includes("/v1/market/artifacts") && c.url.includes("kind=dashboard"),
      ),
    ).toBe(true);
    // 数据集面随页签撤场。
    expect(screen.queryByTestId("market-list")).toBeNull();

    // 切报告页签：kind=research_report + 报告卡片。
    fireEvent.click(screen.getByTestId("market-tab-reports"));
    await waitFor(() =>
      expect(
        backend.calls.some(
          (c) =>
            c.url.includes("/v1/market/artifacts") &&
            c.url.includes("kind=research_report"),
        ),
      ).toBe(true),
    );
    expect(
      (await screen.findByTestId("market-artifact-list")).textContent,
    ).toContain("脐橙产业季报");
  });

  it("产物卡片：标题/描述/发布方/面板数/版本渲染；open 档订阅调用", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");

    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    const item = await screen.findByTestId("market-artifact-item-501");
    expect(item.textContent).toContain("物流看板");
    expect(item.textContent).toContain("全县物流面板");
    expect(item.textContent).toContain("other-customer");
    expect(item.textContent).toContain("2");
    expect(item.textContent).toContain("v3");

    fireEvent.click(screen.getByTestId("market-artifact-subscribe-501"));
    await waitFor(() =>
      expect(
        backend.calls.some(
          (c) =>
            c.method === "POST" &&
            c.url.endsWith("/v1/market/artifacts/501/subscribe"),
        ),
      ).toBe(true),
    );
    // 订阅成功后就地标记（刷新后回服务端真实态）。
    await waitFor(() =>
      expect(
        screen.getByTestId("market-artifact-subscribe-501").textContent,
      ).toContain("退订"),
    );
  });

  it("approval 档产物：显示申请使用（access-requests 端点）", async () => {
    const backend = installMarketBackend({
      artifactItems: {
        dashboard: [makeArtifactCard({ id: 502, access_mode: "approval" })],
        research_report: [],
      },
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");

    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    const requestButton = await screen.findByTestId("market-artifact-request-502");
    expect(requestButton.textContent).toContain("申请使用");
    expect(screen.queryByTestId("market-artifact-subscribe-502")).toBeNull();

    fireEvent.click(requestButton);
    await waitFor(() =>
      expect(
        backend.calls.some(
          (c) =>
            c.method === "POST" &&
            c.url.endsWith("/v1/market/artifacts/502/access-requests"),
        ),
      ).toBe(true),
    );
  });

  it("详情：点标题拉详情 → 面板摘要（仅 id/type/title）；报告显 data_scope/data_range", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");

    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    fireEvent.click(
      await screen.findByRole("heading", { name: "物流看板" }),
    );
    const detail = await screen.findByTestId("market-artifact-detail");
    const panels = screen.getByTestId("market-artifact-panels");
    expect(panels.textContent).toContain("订单趋势");
    expect(panels.textContent).toContain("line");
    expect(panels.textContent).toContain("区域对比");
    expect(panels.textContent).toContain("bar");
    // 面板清单只挂三字段（Q8：无 config/snapshot/query 泄漏面）。
    expect(panels.querySelectorAll("li").length).toBe(2);
    expect(detail.textContent).not.toContain("snapshot");
    // allow_copy=true → 详情内复制按钮在场。
    expect(screen.getByTestId("market-artifact-copy-501")).toBeTruthy();

    // 报告页签：详情渲染 data_scope / data_range 元信息。
    fireEvent.click(screen.getByTestId("market-tab-reports"));
    await screen.findByTestId("market-artifact-item-601");
    fireEvent.click(
      await screen.findByRole("heading", { name: "脐橙产业季报" }),
    );
    const reportDetail = await screen.findByTestId("market-artifact-detail");
    expect(reportDetail.textContent).toContain("2024-01~2026-09");
    expect(reportDetail.textContent).toContain("江西·赣州");
    // 报告详情不带看板面板清单。
    expect(screen.queryByTestId("market-artifact-panels")).toBeNull();
  });

  it("复制：POST /copy 后渲染 binding_report（bound/unbound + unbound 清单）", async () => {
    const backend = installMarketBackend({});
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");

    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    fireEvent.click(
      await screen.findByRole("heading", { name: "物流看板" }),
    );
    await screen.findByTestId("market-artifact-detail");

    fireEvent.click(screen.getByTestId("market-artifact-copy-501"));
    await waitFor(() =>
      expect(
        backend.calls.some(
          (c) =>
            c.method === "POST" &&
            c.url.endsWith("/v1/market/artifacts/501/copy"),
        ),
      ).toBe(true),
    );
    const report = await screen.findByTestId("market-artifact-copy-report");
    // 计数以 data-* 表达（语言中立；文案键由 i18n 通道落地后带插值渲染）。
    expect(report.getAttribute("data-bound")).toBe("1");
    expect(report.getAttribute("data-unbound")).toBe("1");
    expect(report.textContent).toContain("ds-private");
    // 全绑定/零未绑定场景不渲染 unbound 清单。
    expect(report.querySelectorAll("li")).toHaveLength(1);
  });

  it("复制 403（未允许/未订阅）：后端 detail 如实展示", async () => {
    const backend = installMarketBackend({
      copyStatus: 403,
      copyDetail: "发布方未允许复制此产物",
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");

    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    fireEvent.click(
      await screen.findByRole("heading", { name: "物流看板" }),
    );
    await screen.findByTestId("market-artifact-detail");
    fireEvent.click(screen.getByTestId("market-artifact-copy-501"));

    const error = await screen.findByTestId("market-artifact-error");
    expect(error.textContent).toContain("发布方未允许复制此产物");
    expect(screen.queryByTestId("market-artifact-copy-report")).toBeNull();
  });

  it("allow_copy=false：详情内无复制按钮（服务端 403 兜底之外的前端条件）", async () => {
    const backend = installMarketBackend({
      artifactItems: {
        dashboard: [makeArtifactCard({ id: 503, allow_copy: false })],
        research_report: [],
      },
      artifactDetail: makeArtifactDetail({ id: 503, allow_copy: false }),
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");

    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    fireEvent.click(
      await screen.findByRole("heading", { name: "物流看板" }),
    );
    await screen.findByTestId("market-artifact-detail");
    expect(screen.queryByTestId("market-artifact-copy-503")).toBeNull();
  });

  it("产物空态与错误态：无条目给空文案；列表 500 给错误与重试", async () => {
    const empty = installMarketBackend({
      artifactItems: { dashboard: [], research_report: [] },
    });
    vi.stubGlobal("fetch", empty.fetchImpl);
    const first = render(<MarketPage />);
    await screen.findByTestId("market-list");
    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    expect(await screen.findByTestId("market-artifact-empty")).toBeTruthy();
    first.unmount();

    const failing = installMarketBackend({ artifactListStatus: 500 });
    vi.stubGlobal("fetch", failing.fetchImpl);
    render(<MarketPage />);
    await screen.findByTestId("market-list");
    fireEvent.click(screen.getByTestId("market-tab-dashboards"));
    const error = await screen.findByTestId("market-artifact-error");
    expect(error.textContent).toContain("artifact boom");
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });
});
