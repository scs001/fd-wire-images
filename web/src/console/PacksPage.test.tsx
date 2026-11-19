// 「源包」页测试（wire-source-packs 任务 3.3）：
// - 卡片墙：双语名（按当前语言）/描述/指标数徽标/已订阅徽标；
// - 订阅切换：按钮即时切换并刷新徽标（POST subscribe / DELETE unsubscribe，
//   假后端幂等；断言 Bearer 与路径）；
// - 「仅看已订阅」过滤开关；
// - 详情展开：分页指标清单（翻页/页码/共 N 项）+ 每行链 #/catalog/{code}；
// - 未登录态：公开浏览（列表/详情无 Authorization 也能读），订阅按钮引导登录；
// - 加载失败：错误如实呈现 + 重试。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { PacksPage } from "./PacksPage";
import { ConsoleLayout } from "./ConsoleLayout";
import { parseHash } from "../App";
import {
  installConsoleBackend,
  seedConsoleSession,
  type FakePackSeed,
} from "./testing";
import type { PackIndicator } from "./api";

/** 造 n 个指标（code 形如 PREFIX-001；domain 交替 领域/null）。 */
function indicators(count: number, prefix: string): PackIndicator[] {
  return Array.from({ length: count }, (_, i) => ({
    code: `${prefix}-${String(i + 1).padStart(3, "0")}`,
    name_zh: `${prefix} 指标 ${i + 1}`,
    domain: i % 2 === 0 ? "宏观" : null,
  }));
}

function seedPacks(): FakePackSeed[] {
  return [
    {
      id: "macro-prices",
      name_zh: "宏观价格",
      name_en: "Macro Prices",
      desc: "CPI/PPI 等价格类指标的主题打包",
      indicators: indicators(3, "CPI"),
    },
    {
      id: "population",
      name_zh: "人口与就业",
      name_en: "Population & Jobs",
      desc: "人口与就业类指标的主题打包",
      indicators: indicators(25, "POP"),
    },
  ];
}

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

describe("PacksPage 导航接线（ConsoleLayout tab + hash 路由）", () => {
  it("左侧栏含「源包」tab（顺序：我的数据之后），href 指向 #/console/packs", () => {
    render(
      <ConsoleLayout
        tab="packs"
        customerName="演示客户"
        planCode="free"
        identity="demo@test"
        onLogout={() => {}}
      >
        <PacksPage />
      </ConsoleLayout>,
    );
    const nav = screen.getByTestId("console-nav");
    const ids = [...nav.querySelectorAll("a")].map((a) =>
      a.getAttribute("data-testid"),
    );
    // 顺序断言：datasets 之后、demands 之前
    expect(ids.indexOf("console-nav-datasets")).toBeLessThan(
      ids.indexOf("console-nav-packs"),
    );
    expect(ids.indexOf("console-nav-packs")).toBeLessThan(
      ids.indexOf("console-nav-demands"),
    );
    expect(screen.getByTestId("console-nav-packs").getAttribute("href")).toBe(
      "#/console/packs",
    );
    expect(screen.getByTestId("console-nav-packs").textContent).toContain(
      "源包",
    );
  });

  it("parseHash：#/console/packs 解析为 packs tab", () => {
    expect(parseHash("#/console/packs")).toEqual({
      name: "console",
      tab: "packs",
    });
  });
});

describe("PacksPage（源包 · 卡片墙 + 订阅）", () => {
  it("卡片墙渲染：双语名（zh）/描述/指标数徽标/已订阅徽标", async () => {
    const backend = installConsoleBackend([], {
      packs: seedPacks(),
      packSubscriptions: ["macro-prices"],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    const cards = await screen.findAllByTestId("pack-card");
    expect(cards.length).toBe(2);
    // 中文会话取 name_zh
    expect(screen.getByText("宏观价格")).toBeTruthy();
    expect(screen.getByText("人口与就业")).toBeTruthy();
    // 指标数徽标（实时计数）
    expect(screen.getByText("3 项指标")).toBeTruthy();
    expect(screen.getByText("25 项指标")).toBeTruthy();
    // 已订阅徽标只出现在订阅中的包
    const badges = screen.getAllByTestId("pack-subscribed-badge");
    expect(badges.length).toBe(1);
    expect(cards[0]!.getAttribute("data-subscribed")).toBe("true");
    expect(cards[1]!.getAttribute("data-subscribed")).toBe("false");
    // 描述在场
    expect(screen.getAllByTestId("pack-desc").length).toBe(2);
  });

  it("双语名切换：英文会话取 name_en", async () => {
    await i18n.changeLanguage("en");
    const backend = installConsoleBackend([], { packs: seedPacks() });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    expect(await screen.findByText("Macro Prices")).toBeTruthy();
    expect(screen.getByText("Population & Jobs")).toBeTruthy();
    expect(screen.getByText("25 indicators")).toBeTruthy();
    await i18n.changeLanguage("zh");
  });

  it("订阅切换：按钮即时刷新徽标（POST subscribe → DELETE unsubscribe）", async () => {
    const backend = installConsoleBackend([], { packs: seedPacks() });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    await screen.findAllByTestId("pack-card");
    const macroCard = screen
      .getAllByTestId("pack-card")
      .find((el) => el.getAttribute("data-pack-id") === "macro-prices")!;
    // 未订阅：无徽标，按钮文案「订阅」
    expect(screen.queryByTestId("pack-subscribed-badge")).toBeNull();

    const toggleOf = (packId: string) =>
      screen
        .getAllByTestId("pack-toggle-subscribe")
        .find((el) => el.getAttribute("data-pack-id") === packId)!;
    fireEvent.click(toggleOf("macro-prices"));

    // 即时切换：徽标出现、卡片态翻转、假后端订阅落位
    await waitFor(() => {
      expect(macroCard.getAttribute("data-subscribed")).toBe("true");
    });
    expect(screen.getAllByTestId("pack-subscribed-badge").length).toBe(1);
    expect(backend.getPackSubscriptions()).toContain("macro-prices");
    const subCall = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/packs/macro-prices/subscribe",
    )!;
    expect(subCall.method).toBe("POST");
    expect(subCall.headers["authorization"]).toMatch(/^Bearer /);

    // 再点一次 = 退订：徽标消失、假后端清空
    fireEvent.click(toggleOf("macro-prices"));
    await waitFor(() => {
      expect(macroCard.getAttribute("data-subscribed")).toBe("false");
    });
    expect(screen.queryByTestId("pack-subscribed-badge")).toBeNull();
    expect(backend.getPackSubscriptions()).toEqual([]);
    const unsubCall = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/packs/macro-prices/unsubscribe",
    )!;
    expect(unsubCall.method).toBe("DELETE");
  });

  it("「仅看已订阅」过滤：开关只保留订阅中的包", async () => {
    const backend = installConsoleBackend([], {
      packs: seedPacks(),
      packSubscriptions: ["macro-prices"],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    await screen.findAllByTestId("pack-card");
    expect(screen.getAllByTestId("pack-card").length).toBe(2);

    fireEvent.click(screen.getByTestId("packs-only-subscribed"));
    expect(screen.getAllByTestId("pack-card").length).toBe(1);
    expect(
      screen.getAllByTestId("pack-card")[0]!.getAttribute("data-pack-id"),
    ).toBe("macro-prices");

    // 关闭过滤恢复全量
    fireEvent.click(screen.getByTestId("packs-only-subscribed"));
    expect(screen.getAllByTestId("pack-card").length).toBe(2);
  });

  it("详情展开：分页指标清单（翻页/页码/共 N 项）+ 行链 #/catalog/{code}", async () => {
    const backend = installConsoleBackend([], { packs: seedPacks() });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    await screen.findAllByTestId("pack-card");
    // 展开人口与就业（25 项 > 默认每页 20 → 两页）
    fireEvent.click(
      screen
        .getAllByTestId("pack-expand")
        .find((el) => el.getAttribute("data-pack-id") === "population")!,
    );

    const detail = await screen.findByTestId("pack-detail");
    expect(detail.getAttribute("data-pack-id")).toBe("population");
    let rows = screen.getAllByTestId("pack-indicator-row");
    expect(rows.length).toBe(20);
    const pageLabel = screen.getByTestId("pack-detail-page");
    expect(pageLabel.textContent).toContain("第 1 / 2 页");
    expect(pageLabel.textContent).toContain("共 25 项");
    // 第一页首行链到目录详情页
    const firstLink = screen.getAllByTestId("pack-indicator-link")[0]!;
    expect(firstLink.getAttribute("href")).toBe("#/catalog/POP-001");
    // 首页禁用上一页、放行下一页
    expect(
      (screen.getByTestId("pack-detail-prev") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByTestId("pack-detail-next") as HTMLButtonElement).disabled,
    ).toBe(false);

    fireEvent.click(screen.getByTestId("pack-detail-next"));
    await waitFor(() => {
      expect(screen.getAllByTestId("pack-indicator-row").length).toBe(5);
    });
    expect(screen.getByTestId("pack-detail-page").textContent).toContain(
      "第 2 / 2 页",
    );
    // 详情分页请求带页码（公开端点，无需 Authorization 之外参数）
    const pagedCall = backend.calls
      .filter((c) => c.url.pathname === "/api/v1/packs/population")
      .find((c) => c.url.searchParams.get("page") === "2")!;
    expect(pagedCall).toBeTruthy();
    // 尾页禁用下一页
    expect(
      (screen.getByTestId("pack-detail-next") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("未登录态：公开浏览不受影响（无 Authorization 也能读），订阅引导登录", async () => {
    // 无登录会话：列表/详情为公开端点照常；订阅按钮引导登录。
    sessionStorage.clear();
    const backend = installConsoleBackend([], { packs: seedPacks() });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    const cards = await screen.findAllByTestId("pack-card");
    expect(cards.length).toBe(2);
    // 公开列表请求不携带 Authorization
    const listCall = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/packs",
    )!;
    expect(listCall.headers["authorization"]).toBeUndefined();
    // 订阅按钮点击 → 引导登录（不发起订阅请求）
    expect(screen.queryByTestId("packs-login-hint")).toBeNull();
    fireEvent.click(screen.getAllByTestId("pack-toggle-subscribe")[0]!);
    expect(await screen.findByTestId("packs-login-hint")).toBeTruthy();
    expect(
      screen.getByTestId("packs-login-cta").getAttribute("href"),
    ).toBe("#/login");
    expect(
      backend.calls.find((c) => c.url.pathname.endsWith("/subscribe")),
    ).toBeUndefined();
    // 详情（公开）照常展开
    fireEvent.click(screen.getAllByTestId("pack-expand")[0]!);
    expect(await screen.findByTestId("pack-detail")).toBeTruthy();
    const detailCall = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/packs/macro-prices",
    )!;
    expect(detailCall.headers["authorization"]).toBeUndefined();
  });

  it("会话中途失效（订阅端点 401）：转引导登录，浏览不受影响", async () => {
    const backend = installConsoleBackend([], {
      packs: seedPacks(),
      failPackSubscribeStatus: 401,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    await screen.findAllByTestId("pack-card");
    fireEvent.click(screen.getAllByTestId("pack-toggle-subscribe")[0]!);
    expect(await screen.findByTestId("packs-login-hint")).toBeTruthy();
    // 公开浏览面照常（无错误条）
    expect(screen.queryByTestId("packs-error")).toBeNull();
    expect(screen.getAllByTestId("pack-card").length).toBe(2);
  });

  it("加载失败：错误如实呈现 + 重试", async () => {
    const backend = installConsoleBackend([], {
      packs: seedPacks(),
      failPacksStatus: 503,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<PacksPage />);

    const error = await screen.findByTestId("packs-error");
    expect(error.textContent).toContain("源包加载失败");
    // 重试：换一个健康的后端后恢复
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { packs: seedPacks() }).fetchImpl,
    );
    fireEvent.click(screen.getByTestId("packs-retry"));
    expect(await screen.findAllByTestId("pack-card")).toHaveLength(2);
  });
});
