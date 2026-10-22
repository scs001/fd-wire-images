// PricingView 四卡测试（wire-self-serve-launch 任务 7.1，portal-pricing spec）：
// 三张公开卡活取 GET /api/v1/plans（价格/行数为后端数据，源码零档位面量）+
// 一张静态「自定义」询价卡；副标题为「用完即停、次月恢复」口径；
// 活取失败回退无数字卡片；目录规模数字活取 stats。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { parseHash } from "../App";
import { PricingView } from "./PricingView";
import { installFakeBackend } from "../catalog/testing";
import { resetCatalogStatsCacheForTests } from "../catalog/useCatalogStats";

const STATS = {
  registered: 57553,
  verified: 3421,
  by_source: [{ source_db: "world_bank", registered: 12000, verified: 800 }],
};

/** 契约钉死的三档公开档位（api 侧按此实现，前端写死对接）。 */
const PLANS = {
  plans: [
    { code: "free", monthly_price_yuan: 0, included_lines: 1000, sort: 1 },
    { code: "basic", monthly_price_yuan: 10, included_lines: 100000, sort: 2 },
    { code: "standard", monthly_price_yuan: 50, included_lines: 1000000, sort: 3 },
  ],
};

/** 同时应答 /api/v1/plans 与 /api/v1/catalog/stats 的组合假后端。 */
function installPricingBackend(options: {
  plans?: typeof PLANS.plans | null;
  plansStatus?: number;
  stats?: typeof STATS | null;
}): { calls: Array<{ url: URL }>; fetchImpl: typeof fetch } {
  const calls: Array<{ url: URL }> = [];
  const catalog = installFakeBackend([], {}, options.stats ?? null);
  const jsonResponse = (body: unknown, status = 200) => ({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  });
  const fetchImpl = ((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://pricing.test");
    calls.push({ url });
    if (url.pathname === "/api/v1/plans") {
      const status = options.plansStatus ?? 200;
      const body =
        status < 400
          ? { plans: options.plans ?? PLANS.plans }
          : { detail: "plans down" };
      return Promise.resolve(jsonResponse(body, status));
    }
    return catalog.fetchImpl(input);
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  resetCatalogStatsCacheForTests();
  // 缺省打上 plans 活数 + stats 不可用（回退文案路径）；各用例自行重打。
  const backend = installPricingBackend({ stats: null });
  vi.stubGlobal("fetch", backend.fetchImpl);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PricingView（定价四卡 · portal-pricing spec）", () => {
  it("三张公开卡活取 plans：价格/含额行数为端点数据（¥0/¥10/¥50），按 sort 排序", async () => {
    render(<PricingView />);

    const free = await screen.findByTestId("tier-free");
    const basic = screen.getByTestId("tier-basic");
    const standard = screen.getByTestId("tier-standard");
    // 档位名按 code 取 i18n（free/basic/standard/custom）
    expect(within(free).getByText("免费版")).toBeTruthy();
    expect(within(basic).getByText("基础版")).toBeTruthy();
    expect(within(standard).getByText("标准版")).toBeTruthy();
    // 价格与含额行数来自端点（千分位为呈现格式化）
    expect(within(free).getByText("¥0")).toBeTruthy();
    expect(within(basic).getByText("¥10")).toBeTruthy();
    expect(within(standard).getByText("¥50")).toBeTruthy();
    expect(within(free).getByText("含 1,000 行/月")).toBeTruthy();
    expect(within(basic).getByText("含 100,000 行/月")).toBeTruthy();
    expect(within(standard).getByText("含 1,000,000 行/月")).toBeTruthy();
    // 用完即停口径：无超额阶梯单价
    for (const card of [free, basic, standard]) {
      expect(within(card).getByText(/用完即停、次月恢复/)).toBeTruthy();
      expect(within(card).queryByText(/超额 ¥/)).toBeNull();
    }
  });

  it("第四张「自定义」卡：主 CTA 站内跳需求单表单，mailto 降次级（任务 4.4）", async () => {
    render(<PricingView />);

    const custom = await screen.findByTestId("tier-custom");
    expect(within(custom).getByText("自定义")).toBeTruthy();
    expect(within(custom).getByText("询价")).toBeTruthy();
    // 主 CTA：站内路由（不唤起邮件客户端）
    const cta = screen.getByTestId("tier-custom-cta");
    expect(cta.getAttribute("href")).toBe("#/console/demands/new");
    expect(cta.textContent).not.toMatch(/mailto/);
    expect(cta.className).not.toContain("text-xs");
    // mailto 降为次级小链接保留
    const mailto = screen.getByTestId("tier-custom-mailto");
    expect(mailto.getAttribute("href")).toMatch(/^mailto:/);
    // 三张公开卡 CTA 仍为 mailto 联系开通（共 3 处，自定义卡不再计入）
    const ctas = screen.getAllByText("联系开通");
    expect(ctas.length).toBe(3);
    for (const cta of ctas) {
      expect(cta.closest("a")?.getAttribute("href")).toMatch(/^mailto:/);
    }
    // 副标题 1 处 + 三张公开卡的功能行各 1 处（自定义卡无该行）
    const stopAtQuota = screen.getAllByText(/用完即停、次月恢复/);
    expect(stopAtQuota.length).toBe(4);
    expect(screen.queryByText(/阶梯单价/)).toBeNull();
  });

  it("活取失败 → 回退无数字卡片：能力概览仍在、绝不出现写死价格", async () => {
    const backend = installPricingBackend({ plans: null, plansStatus: 500, stats: null });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PricingView />);

    // 失败标注 + 仅剩自定义询价卡（公开卡退场，不伪造数字）
    expect(await screen.findByTestId("pricing-load-failed")).toBeTruthy();
    expect(screen.getByTestId("tier-custom")).toBeTruthy();
    expect(screen.queryByTestId("tier-free")).toBeNull();
    expect(screen.queryByText(/¥\d/)).toBeNull();
    expect(screen.queryByText(/行\/月/)).toBeNull();
  });

  it("目录规模数字活取 stats（57,553 → 「57,000+ 指标目录全量浏览」×四卡）", async () => {
    const backend = installPricingBackend({ stats: STATS });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PricingView />);

    await screen.findByTestId("tier-custom");
    // 功能列表随四张卡（三公开 + 自定义）各渲染一次
    const live = await screen.findAllByText("57,000+ 指标目录全量浏览");
    expect(live.length).toBe(4);
    expect(screen.getAllByText("MCP 直连数据读取").length).toBe(4);
  });

  it("stats 不可用 → 功能列表回退不带数字文案", async () => {
    render(<PricingView />);

    await screen.findByTestId("tier-custom");
    const fallback = await screen.findAllByText("指标目录全量浏览");
    expect(fallback.length).toBe(4);
    expect(screen.queryByText(/57,000/)).toBeNull();
  });

  it("EN 语言：档位名取英文键，价格符号仍为 ¥（双币统一口径）", async () => {
    i18n.changeLanguage("en");
    render(<PricingView />);

    const free = await screen.findByTestId("tier-free");
    expect(within(free).getByText("Free")).toBeTruthy();
    expect(within(free).getByText("¥0")).toBeTruthy();
    expect(within(free).getByText("/mo")).toBeTruthy();
  });

  it("脚注：需求单独立报价、法律域默认询价", async () => {
    render(<PricingView />);

    await screen.findByTestId("tier-custom");
    expect(screen.getByText(/数据需求单独立报价/)).toBeTruthy();
    expect(screen.getByText(/法律域数据默认询价开通/)).toBeTruthy();
  });

  it("路由：#/pricing 解析为 pricing（未登录可访问）", () => {
    expect(parseHash("#/pricing")).toEqual({ name: "pricing" });
  });
});
