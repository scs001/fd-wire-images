// 账本 / 目录治理 / 用量分轨三页组件测试（任务 7.1）+ #/admin 路由解析。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { parseHash } from "../App";
import { LedgerPage } from "./LedgerPage";
import { CatalogGovernancePage } from "./CatalogGovernancePage";
import { UsageOverviewPage } from "./UsageOverviewPage";
import { installAdminBackend, seedAdminSession } from "./testing";
import { seedConsoleSession } from "../console/testing";
import { AdminView } from "./AdminView";
import type { AdminCustomer, LedgerEntry } from "./api";

const TOKEN = "test-admin-token";

const CUSTOMERS: AdminCustomer[] = [
  {
    id: 1,
    name: "上海壹座",
    sub2api_user_id: "acme-caller",
    plan_id: 1,
    plan_code: "starter",
    plan_name: "起步版",
    status: "active",
    created_at: "2026-10-01T08:00:00",
  },
  {
    id: 2,
    name: "萬星數據",
    sub2api_user_id: "wanxing-caller",
    plan_id: 2,
    plan_code: "professional",
    plan_name: "专业版",
    status: "active",
    created_at: "2026-10-02T08:00:00",
  },
];

const LEDGER: LedgerEntry[] = [
  {
    id: 1,
    customer_id: 1,
    kind: "grant",
    quantity: 500_000,
    amount_micro: 1_999_000_000,
    amount_yuan: "1999.00",
    amount_yuan_str: "¥1,999.00",
    period: "2026-09",
    idempotency_key: "grant:1:2026-09",
    source_ref: null,
    sub2api_applied: false,
    created_at: "2026-09-01T00:05:00",
  },
  {
    id: 2,
    customer_id: 1,
    kind: "overage",
    quantity: 1_200,
    amount_micro: 4_800_000,
    amount_yuan: "4.80",
    amount_yuan_str: "¥4.80",
    period: "2026-10",
    idempotency_key: "settle:2026-10:period:1",
    source_ref: null,
    sub2api_applied: true,
    created_at: "2026-10-01T00:05:00",
  },
  {
    id: 3,
    customer_id: 2,
    kind: "adjustment",
    quantity: 0,
    amount_micro: -500_000,
    amount_yuan: "-0.50",
    amount_yuan_str: "¥-0.50",
    period: "2026-10",
    idempotency_key: "adjust:2:2026-10:ref-77",
    source_ref: "ref-77",
    sub2api_applied: false,
    created_at: "2026-10-02T09:00:00",
  },
];

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // admin 双闸（web 半边）：登录会话持 wire-admin 角色，idToken 与假后端
  // 的期望 Bearer 一致（X-Admin-Token 由 nginx 服务端注入，前端不持）。
  seedAdminSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("LedgerPage（账本流水）", () => {
  it("流水渲染：kind 徽标着色、金额展示、按客户过滤 + 用量摘要", async () => {
    const backend = installAdminBackend({
      customers: CUSTOMERS,
      ledger: LEDGER,
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<LedgerPage />);

    const rows = await screen.findAllByTestId("admin-ledger-row");
    expect(rows.length).toBe(3);
    const kinds = screen
      .getAllByTestId("admin-ledger-kind-badge")
      .map((el) => el.getAttribute("data-kind"));
    expect(kinds).toEqual(["grant", "overage", "adjustment"]);
    // kind 徽标语义着色（不同 kind 不同色类）
    const grantBadge = screen.getAllByTestId("admin-ledger-kind-badge")[0]!;
    const overageBadge = screen.getAllByTestId("admin-ledger-kind-badge")[1]!;
    expect(grantBadge.className).not.toBe(overageBadge.className);
    expect(screen.getAllByTestId("admin-ledger-amount")[0]!.textContent).toBe(
      "¥1,999.00",
    );

    // 按客户过滤：查询形状 + 该客户用量摘要（caller 联查）
    fireEvent.change(screen.getByTestId("ledger-customer-filter"), {
      target: { value: "1" },
    });
    await waitFor(() => {
      expect(screen.getAllByTestId("admin-ledger-row").length).toBe(2);
    });
    const ledgerCall = backend.calls
      .filter((c) => c.url.pathname === "/internal/admin/ledger")
      .pop()!;
    expect(ledgerCall.url.searchParams.get("customer_id")).toBe("1");
    expect(await screen.findByTestId("ledger-usage-summary")).toBeTruthy();
    expect(screen.getByTestId("ledger-usage-summary").textContent).toContain(
      "上海壹座",
    );
    expect(screen.getByTestId("ledger-usage-summary").textContent).toContain(
      "acme-caller",
    );
  });

  it("周期过滤查询形状（?period=）与空态", async () => {
    const backend = installAdminBackend({
      customers: [],
      ledger: [],
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<LedgerPage />);
    expect(await screen.findByTestId("admin-ledger-empty")).toBeTruthy();

    fireEvent.change(screen.getByTestId("ledger-period-filter"), {
      target: { value: "2026-10" },
    });
    await waitFor(() => {
      const call = backend.calls
        .filter((c) => c.url.pathname === "/internal/admin/ledger")
        .pop()!;
      expect(call.url.searchParams.get("period")).toBe("2026-10");
    });
  });
});

describe("CatalogGovernancePage（目录治理）", () => {
  it("registered 列表 + 只读标注 + 审核意见录入调用形状与追加展示", async () => {
    const backend = installAdminBackend({ bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CatalogGovernancePage />);

    const items = await screen.findAllByTestId("admin-catalog-item");
    expect(items.length).toBe(1);
    // 只读纪律如实标注
    expect(screen.getByTestId("admin-catalog-readonly-note").textContent).toContain(
      "权威库",
    );

    fireEvent.click(screen.getByTestId("admin-catalog-notes-toggle"));
    expect(screen.getByTestId("admin-catalog-note-input")).toBeTruthy();

    fireEvent.change(screen.getByTestId("admin-catalog-note-input"), {
      target: { value: "口径核对通过，待权威库复核" },
    });
    fireEvent.click(screen.getByTestId("admin-catalog-note-submit"));

    await waitFor(() => {
      expect(screen.getAllByTestId("admin-catalog-note-item").length).toBe(1);
    });
    expect(screen.getByTestId("admin-catalog-note-item").textContent).toContain(
      "口径核对通过",
    );

    const noteCall = backend.calls.find((c) => c.method === "POST")!;
    expect(noteCall.url.pathname).toBe(
      "/internal/admin/catalog/ind-reg-1/verify-note",
    );
    expect(noteCall.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
    expect(noteCall.body).toEqual({
      note: "口径核对通过，待权威库复核",
      author: "admin-console",
    });
  });
});

describe("UsageOverviewPage（用量分轨）", () => {
  it("两轨数字卡 + 来源分布 + 轨道过滤查询形状", async () => {
    const backend = installAdminBackend({
      bearerToken: TOKEN,
      tracks: [
        { key: "customer", calls: 12, rows: 3400 },
        { key: "platform", calls: 5, rows: 800 },
      ],
      bySource: [
        { key: "yizuo", calls: 8, rows: 2600 },
        { key: "wanxing", calls: 4, rows: 800 },
        { key: "platform-internal", calls: 5, rows: 800 },
      ],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<UsageOverviewPage />);

    expect(await screen.findByTestId("admin-usage-card-customer")).toBeTruthy();
    expect(screen.getByTestId("admin-usage-card-customer-rows").textContent).toBe(
      "3,400",
    );
    expect(screen.getByTestId("admin-usage-card-platform-rows").textContent).toBe(
      "800",
    );
    const sources = screen
      .getAllByTestId("admin-usage-source-row")
      .map((el) => el.textContent);
    expect(sources.length).toBe(3);
    expect(sources[0]).toContain("壹座");
    expect(sources[2]).toContain("平台内部");

    fireEvent.change(screen.getByTestId("usage-track-filter"), {
      target: { value: "customer" },
    });
    await waitFor(() => {
      const call = backend.calls
        .filter((c) => c.url.pathname === "/internal/admin/usage/overview")
        .pop()!;
      expect(call.url.searchParams.get("credential_track")).toBe("customer");
    });
  });

  it("鉴权失败态如实呈现（401）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({ forceStatus: 401 }).fetchImpl,
    );
    render(<UsageOverviewPage />);
    expect(await screen.findByTestId("admin-auth-error")).toBeTruthy();
  });
});

describe("AdminLayout 顶栏出口（wire-admin-console-routing 任务 3.1）", () => {
  it("后台顶栏并列两个出口：进控制台（#/console）与返回前台（#/）", async () => {
    const backend = installAdminBackend({ bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<AdminView section="customers" />);
    await screen.findByTestId("admin-nav");

    // 控制台出口落裸 #/console：只有裸入口走偏好落地序（置顶看板 >
    // landing_tab），符合「管理员同时是客户」语义；点击即置分流标记，
    // 使这次显式进入不再被角色分流送回后台。
    const gotoConsole = screen.getByTestId("admin-goto-console");
    expect(gotoConsole.getAttribute("href")).toBe("#/console");
    // 与既有「返回前台」并列呈现（D2：二者目的不同，不合并）。
    const exit = screen.getByTestId("admin-exit");
    expect(exit.getAttribute("href")).toBe("#/");
    expect(gotoConsole).not.toBe(exit);
    // 样式与既有出口一致（照抄 admin-exit），文案取自既有 nav.console 键
    expect(gotoConsole.className).toBe(exit.className);
    expect(gotoConsole.textContent).toBe("控制台");
    // 控制台出口挂在后台顶栏内（与登出/出口同容器）
    expect(
      screen.getByTestId("admin-topbar").contains(gotoConsole),
    ).toBe(true);
  });

  it("后台六个分区任一页顶栏都带控制台出口（可达性不随分区变化）", async () => {
    const backend = installAdminBackend({ bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    for (const section of [
      "customers",
      "ledger",
      "demands",
      "catalog",
      "usage",
      "localdata",
    ] as const) {
      const { unmount } = render(<AdminView section={section} />);
      await screen.findByTestId("admin-nav");
      expect(
        screen.getByTestId("admin-goto-console").getAttribute("href"),
      ).toBe("#/console");
      unmount();
      cleanup();
    }
  });
});

describe("#/admin 路由解析", () => {
  it("五段解析与未知段回落客户页", () => {
    expect(parseHash("#/admin")).toEqual({ name: "admin", section: "customers" });
    expect(parseHash("#/admin/customers")).toEqual({
      name: "admin",
      section: "customers",
    });
    expect(parseHash("#/admin/ledger")).toEqual({ name: "admin", section: "ledger" });
    expect(parseHash("#/admin/demands")).toEqual({
      name: "admin",
      section: "demands",
    });
    expect(parseHash("#/admin/catalog")).toEqual({
      name: "admin",
      section: "catalog",
    });
    expect(parseHash("#/admin/usage")).toEqual({ name: "admin", section: "usage" });
    expect(parseHash("#/admin/whatever")).toEqual({
      name: "admin",
      section: "customers",
    });
  });
});

describe("AdminView（admin 双闸 · web 半边角色门禁）", () => {
  afterEach(cleanup);

  it("无登录会话 → 角色门禁拦下：不渲染任何页面、不发任何 /internal 请求", () => {
    sessionStorage.clear(); // 文件级 beforeEach 已种 admin 会话——本用例验无会话面
    const backend = installAdminBackend({ bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<AdminView section="customers" />);

    expect(screen.getByTestId("admin-role-gate")).toBeTruthy();
    expect(screen.queryByTestId("admin-nav")).toBeNull();
    expect(
      backend.calls.filter((c) => c.url.pathname.startsWith("/internal")),
    ).toEqual([]);
  });

  it("会话无 wire-admin 角色 → 角色门禁拦下（路由直达也进不来）", () => {
    seedConsoleSession({ sub: "plain-customer", roles: [] });
    const backend = installAdminBackend({ bearerToken: "plain-token" });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<AdminView section="customers" />);

    expect(screen.getByTestId("admin-role-gate")).toBeTruthy();
    expect(
      backend.calls.filter((c) => c.url.pathname.startsWith("/internal")),
    ).toEqual([]);
  });

  it("持 wire-admin 角色 → 通过门禁渲染后台（子页请求自动附登录 Bearer）", async () => {
    const backend = installAdminBackend({ bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<AdminView section="customers" />);

    expect(await screen.findByTestId("admin-nav")).toBeTruthy();
    expect(screen.getByTestId("admin-identity").textContent).toContain(
      "admin@finddatatech.cloud",
    );
    await waitFor(() => {
      const call = backend.calls.find(
        (c) => c.url.pathname === "/internal/admin/customers",
      );
      expect(call?.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
    });
  });
});
