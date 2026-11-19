import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { parseHash, isConsoleCallbackPath, App } from "./App";
import "./i18n";
import i18n from "./i18n";
import { installFakeBackend } from "./catalog/testing";
import { resetCatalogStatsCacheForTests } from "./catalog/useCatalogStats";
import { installConsoleBackend, seedConsoleSession } from "./console/testing";

describe("parseHash（hash 路由解析）", () => {
  it("空 / 首页 / 未知路径 → home", () => {
    expect(parseHash("")).toEqual({ name: "home" });
    expect(parseHash("#/")).toEqual({ name: "home" });
    expect(parseHash("#/unknown/path")).toEqual({ name: "home" });
  });

  it("目录列表与详情", () => {
    expect(parseHash("#/catalog")).toEqual({ name: "catalog" });
    expect(parseHash("#/catalog/ind-001")).toEqual({
      name: "catalogDetail",
      id: "ind-001",
    });
    expect(parseHash("#/catalog/a%2Fb")).toEqual({ name: "catalogDetail", id: "a/b" });
  });

  it("控制台：默认 datasets、六 tab、未知段回落 datasets（wire-ux-consolidation 2.2）", () => {
    expect(parseHash("#/console")).toEqual({
      name: "console",
      tab: "datasets",
    });
    expect(parseHash("#/console/chat")).toEqual({
      name: "console",
      tab: "chat",
    });
    // 旧深链 ?c=<id> 被忽略（对话页退役），仍落 chat tab（壹座引导卡）。
    expect(parseHash("#/console/chat?c=12")).toEqual({
      name: "console",
      tab: "chat",
    });
    expect(parseHash("#/console/artifacts")).toEqual({
      name: "console",
      tab: "artifacts",
    });
    expect(parseHash("#/console/datasets")).toEqual({
      name: "console",
      tab: "datasets",
    });
    expect(parseHash("#/console/demands")).toEqual({
      name: "console",
      tab: "demands",
    });
    expect(parseHash("#/console/usage")).toEqual({
      name: "console",
      tab: "usage",
    });
    // 未知二级段回落默认 datasets
    expect(parseHash("#/console/whatever")).toEqual({
      name: "console",
      tab: "datasets",
    });
  });

  it("控制台 / 需求单占位（预填指标）/ 登录", () => {
    expect(parseHash("#/console/demands/new?indicator=CN_COAL")).toEqual({
      name: "demandNew",
      indicator: "CN_COAL",
    });
    expect(parseHash("#/console/demands/new")).toEqual({
      name: "demandNew",
      indicator: null,
    });
    expect(parseHash("#/login")).toEqual({ name: "login" });
  });

  it("公开分享深链（wire-dashboard-share）：id/t/v 齐全解析；t/v 缺失为 null", () => {
    expect(parseHash("#/share/dashboard/11?t=tok-abc&v=3")).toEqual({
      name: "shareDashboard",
      id: "11",
      token: "tok-abc",
      version: "3",
    });
    // 缺 t/v（接收者手改链接）：token/version 如实为 null，ShareView 落无效页
    expect(parseHash("#/share/dashboard/11")).toEqual({
      name: "shareDashboard",
      id: "11",
      token: null,
      version: null,
    });
    // 缺 id 段不构成分享路由，回落首页
    expect(parseHash("#/share/dashboard")).toEqual({ name: "home" });
    expect(parseHash("#/share/other/11?t=x")).toEqual({ name: "home" });
  });
});

describe("App shell（admin 导航按角色显隐 · Logto 回调真实路径）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    resetCatalogStatsCacheForTests();
    sessionStorage.clear();
    window.location.hash = "";
    // App 渲染含首页（活取 stats）——打上 stats 不可用的假后端（零网络）
    vi.stubGlobal("fetch", installFakeBackend([], {}, null).fetchImpl);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    sessionStorage.clear();
    window.location.hash = "";
  });

  it("isConsoleCallbackPath：/console/callback 命中（容忍尾斜杠），其余真实路径不命中", () => {
    expect(isConsoleCallbackPath("/console/callback")).toBe(true);
    expect(isConsoleCallbackPath("/console/callback/")).toBe(true);
    expect(isConsoleCallbackPath("/")).toBe(false);
    expect(isConsoleCallbackPath("/console")).toBe(false);
  });

  it("无会话/普通客户：门户顶栏无「内部后台」入口（wire-console-ux-fixes 5.2 摘壳后入口整体退役，管理员由分流落地直达）", () => {
    // 无会话
    const { unmount } = render(<App />);
    expect(screen.queryByTestId("nav-admin")).toBeNull();
    unmount();
    cleanup();
    sessionStorage.clear();
    // 普通客户（无 wire-admin 角色）
    seedConsoleSession({ sub: "plain-customer", roles: [] });
    const { unmount: unmount2 } = render(<App />);
    expect(screen.queryByTestId("nav-admin")).toBeNull();
    unmount2();
    cleanup();
    sessionStorage.clear();
    // wire-admin 会话：门户顶栏也不再有后台入口（摘壳裁决）
    seedConsoleSession({ sub: "admin", roles: ["wire-admin"] });
    render(<App />);
    expect(screen.queryByTestId("nav-admin")).toBeNull();
  });

  it("#/admin 独立成壳：不渲染门户站点顶栏（门户页签/登录/语言切换），后台品牌条在场", () => {
    seedConsoleSession({ sub: "admin", roles: ["wire-admin"] });
    window.location.hash = "#/admin";
    render(<App />);
    // 门户顶栏整体缺席（品牌导航与页签不再包裹后台）
    expect(screen.queryByTestId("nav-admin")).toBeNull();
    expect(screen.queryAllByTestId(/^footer-service-/)).toEqual([]);
    expect(screen.queryByText("nav-login", { selector: "a" })).toBeNull();
    // 后台自有品牌条：品牌字 + 「内部后台」小标 + 返回门户
    expect(screen.getByTestId("admin-brand-bar")).toBeTruthy();
    expect(
      screen.getByTestId("admin-back-portal").getAttribute("href"),
    ).toBe("#/");
  });

  it("门户头部会话条件（close-wire-implementation-gaps 4.1）：未登录「登录」在场、「进入控制台」缺席", () => {
    window.location.hash = "#/pricing";
    render(<App />);
    const login = screen.getByText("登录", { selector: "a" });
    expect(login.getAttribute("href")).toBe("#/login");
    expect(screen.queryByTestId("nav-console-enter")).toBeNull();
  });

  it("门户头部会话条件：已登录「登录」换「进入控制台」（门户页）；console 页摘壳无门户顶栏", async () => {
    // 门户页（#/pricing）：同步读 sessionStorage 的会话，头部换控制台直达。
    seedConsoleSession({ sub: "demo-customer" });
    window.location.hash = "#/pricing";
    const { unmount } = render(<App />);
    const enter = screen.getByTestId("nav-console-enter");
    expect(enter.getAttribute("href")).toBe("#/console");
    expect(enter.textContent).toBe("进入控制台");
    expect(screen.queryByText("登录", { selector: "a" })).toBeNull();
    unmount();
    cleanup();
    sessionStorage.clear();
    // console 页（已登录）：摘壳（extract-console-shell 1.1）后门户顶栏整体
    // 缺席——「进入控制台」钮不再出现在控制台页（控制台自有顶栏在场）。
    vi.unstubAllGlobals();
    vi.stubGlobal("fetch", installConsoleBackend([]).fetchImpl);
    seedConsoleSession({ sub: "demo-customer" });
    window.location.hash = "#/console";
    render(<App />);
    expect(await screen.findByTestId("console-topbar")).toBeTruthy();
    expect(screen.queryByTestId("nav-console-enter")).toBeNull();
    expect(screen.queryByText("登录", { selector: "a" })).toBeNull();
  });
});

describe("App 控制台摘壳独立成壳（extract-console-shell）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    resetCatalogStatsCacheForTests();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "#/console";
    vi.stubGlobal("fetch", installConsoleBackend([]).fetchImpl);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "";
  });

  /** 门户顶栏缺席的结构断言：登录钮/进入控制台钮（testid）+ 门户页签文案
   *  （官网——门户 nav 独有，控制台侧栏无此文案）。 */
  const expectPortalTopbarAbsent = () => {
    expect(screen.queryByTestId("nav-login")).toBeNull();
    expect(screen.queryByTestId("nav-console-enter")).toBeNull();
    expect(screen.queryByText("官网")).toBeNull();
    expect(screen.queryByText("进入控制台")).toBeNull();
  };

  it("已登录 console 页：门户顶栏缺席，控制台自有顶栏与返回门户在场", async () => {
    seedConsoleSession({ sub: "demo-customer" });
    render(<App />);
    expect(await screen.findByTestId("console-topbar")).toBeTruthy();
    expectPortalTopbarAbsent();
    // parity（extract-console-shell 1.3）：返回门户入口 href=#/
    expect(
      screen.getByTestId("console-back-portal").getAttribute("href"),
    ).toBe("#/");
  });

  it("已登录 demands/new：同壳承载（侧栏/顶栏/返回门户在场，我的需求页签高亮，表单照常）", async () => {
    seedConsoleSession({ sub: "demo-customer" });
    window.location.hash = "#/console/demands/new?indicator=CN_COAL";
    render(<App />);
    expect(await screen.findByTestId("console-topbar")).toBeTruthy();
    expectPortalTopbarAbsent();
    // 活跃页签=我的需求（aria-current 落 demands）
    expect(
      screen.getByTestId("console-nav-demands").getAttribute("aria-current"),
    ).toBe("page");
    // 表单在壳内照常可达：锚点预填与描述区（深链 ?indicator= 行为不变）
    expect(screen.getByTestId("demand-prefilled-code").textContent).toBe(
      "CN_COAL",
    );
    expect(screen.getByTestId("demand-description")).toBeTruthy();
    expect(
      screen.getByTestId("console-back-portal").getAttribute("href"),
    ).toBe("#/");
    // 页脚分发不变：控制台任一页无页脚
    expect(screen.queryByTestId("site-footer")).toBeNull();
    expect(screen.queryByTestId("console-footer")).toBeNull();
  });

  it("未登录 console 页：门户顶栏缺席，登录引导卡在场（ConsoleView 登录门，壳外）", async () => {
    render(<App />);
    expect(await screen.findByTestId("console-signin")).toBeTruthy();
    expectPortalTopbarAbsent();
    // 现状壳结构：登录引导卡渲染在 ConsoleLayout 之外——无顶栏即无
    // console-back-portal（保持现状行为，摘壳不以引导态为界）。
    expect(screen.queryByTestId("console-back-portal")).toBeNull();
    expect(screen.queryByTestId("console-topbar")).toBeNull();
  });

  it("未登录 demands/new：ConsoleView 登录门在场（表单待登录后呈现），门户顶栏缺席", async () => {
    window.location.hash = "#/console/demands/new?indicator=CN_COAL";
    render(<App />);
    expect(await screen.findByTestId("console-signin")).toBeTruthy();
    expectPortalTopbarAbsent();
    expect(screen.queryByTestId("demand-description")).toBeNull();
    expect(screen.queryByTestId("console-back-portal")).toBeNull();
  });
});

describe("App 页脚分发（wire-ux-consolidation 任务 1.2）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    resetCatalogStatsCacheForTests();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "";
    // 门户页含目录/定价活取——打上不可用假后端（零网络，回退态不碍页脚断言）。
    vi.stubGlobal("fetch", installFakeBackend([], {}, null).fetchImpl);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "";
  });

  it("四门户视图（首页/定价/文档/指标目录）挂全量页脚", () => {
    for (const hash of ["#/", "#/pricing", "#/docs", "#/catalog"]) {
      window.location.hash = hash;
      const { unmount } = render(<App />);
      expect(screen.getByTestId("site-footer")).toBeTruthy();
      expect(screen.getByTestId("footer-service-official")).toBeTruthy();
      expect(screen.queryByTestId("console-footer")).toBeNull();
      unmount();
      cleanup();
      resetCatalogStatsCacheForTests();
    }
  });

  it("#/console 无页脚（wire-console-ux-fixes 任务 3.1：工作台布局摘页脚）", async () => {
    vi.unstubAllGlobals();
    vi.stubGlobal("fetch", installConsoleBackend([]).fetchImpl);
    seedConsoleSession({ sub: "demo-customer" });
    window.location.hash = "#/console";
    render(<App />);
    // 控制台壳渲染（侧栏/顶栏在场），但页脚整体摘除。
    expect(await screen.findByTestId("console-topbar")).toBeTruthy();
    expect(screen.queryByTestId("console-footer")).toBeNull();
    expect(screen.queryByTestId("site-footer")).toBeNull();
    expect(screen.queryByTestId("footer-icp")).toBeNull();
    expect(screen.queryAllByTestId(/^footer-service-/)).toEqual([]);
    expect(screen.queryByTestId("footer-github")).toBeNull();
  });

  it("#/admin 不渲染页脚", () => {
    window.location.hash = "#/admin";
    render(<App />);
    expect(screen.getByTestId("admin-role-gate")).toBeTruthy();
    // 门禁拦截态品牌条仍在（wire-console-ux-fixes 5.2：深链被拦不悬空）
    expect(screen.getByTestId("admin-brand-bar")).toBeTruthy();
    expect(
      screen.getByTestId("admin-back-portal").getAttribute("href"),
    ).toBe("#/");
    expect(screen.queryByTestId("site-footer")).toBeNull();
    expect(screen.queryByTestId("console-footer")).toBeNull();
  });

  it("#/console/demands/new（提交子页折入控制台壳）同样无页脚", async () => {
    // 提交子页折入控制台壳（extract-console-shell 1.4）后走 ConsoleView
    // 登录门：表单在登录会话内呈现（匿名深链落登录引导卡，现状壳结构）。
    vi.unstubAllGlobals();
    vi.stubGlobal("fetch", installConsoleBackend([]).fetchImpl);
    seedConsoleSession({ sub: "demo-customer" });
    window.location.hash = "#/console/demands/new?indicator=CN_COAL";
    render(<App />);
    expect(await screen.findByTestId("demand-description")).toBeTruthy();
    expect(screen.queryByTestId("console-footer")).toBeNull();
    expect(screen.queryByTestId("site-footer")).toBeNull();
  });
});

describe("App 顶栏语言切换（wire-personal-preferences）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    resetCatalogStatsCacheForTests();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "#/console";
    // 控制台壳（顶栏常驻）：console 假后端承接 auth/session/preferences。
    vi.stubGlobal("fetch", installConsoleBackend([]).fetchImpl);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "";
  });

  it("登录态在控制台顶栏切语言 → PUT {ui_locale} 即写 server（fire-and-forget）", async () => {
    // 摘壳后（extract-console-shell 1.2）控制台顶栏自带语言切换，与门户
    // langButton 同一 i18n 机制与持久化语义（登录态顺带写 server）。
    // 预置 ui_locale=zh（避免首登领养 PUT 混入断言）。
    const backend = installConsoleBackend([], {
      preferences: { "demo-customer": { ui_locale: "zh" } },
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession({ sub: "demo-customer" });
    render(<App />);
    await screen.findByTestId("console-topbar");

    fireEvent.click(screen.getByText("EN"));
    await waitFor(() => {
      expect(i18n.language).toBe("en");
    });
    expect(localStorage.getItem("wire-web-lang")).toBe("en");
    await waitFor(() => {
      expect(
        backend.calls.find(
          (c) =>
            c.url.pathname === "/api/console/me/preferences" &&
            c.method === "PUT" &&
            (c.body as { ui_locale?: string })?.ui_locale === "en",
        ),
      ).toBeTruthy();
    });
    // 假后端状态确实写入（跨端下次登录收敛到 en）。
    expect(backend.getPreferences("demo-customer").ui_locale).toBe("en");
  });

  it("未登录在门户页切语言 → 仅本地（不携带任何偏好写请求）", async () => {
    // 匿名控制台是登录引导态（无顶栏无语言钮）——「未登录仅本地」的切换
    // 入口在门户顶栏（摘壳后语言切换按壳归属），落 #/docs（静态门户页）。
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    window.location.hash = "#/docs";
    render(<App />);
    expect(await screen.findByTestId("mcp-endpoint")).toBeTruthy();

    fireEvent.click(screen.getByText("EN"));
    await waitFor(() => {
      expect(i18n.language).toBe("en");
    });
    expect(localStorage.getItem("wire-web-lang")).toBe("en");
    expect(
      backend.calls.filter(
        (c) => c.url.pathname === "/api/console/me/preferences",
      ),
    ).toEqual([]);
  });
});

describe("App 公开分享壳（wire-dashboard-share）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    resetCatalogStatsCacheForTests();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "";
    document.title = "柏讯 Wire — B2B 数据平台";
    // 分享页调公开端点（无鉴权）——404 即「链接无效」态，零依赖。
    vi.stubGlobal(
      "fetch",
      (() =>
        Promise.resolve({
          ok: false,
          status: 404,
          json: () => Promise.resolve({ detail: "not found" }),
        } as unknown as Response)) as unknown as typeof fetch,
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
    window.location.hash = "";
  });

  it("#/share/dashboard/* 渲染独立极简壳：品牌锁定顶栏在，站点导航/页脚不渲染", async () => {
    window.location.hash = "#/share/dashboard/9?t=tok&v=2";
    // 无登录会话（分享页免登录红线）
    render(<App />);

    expect(await screen.findByTestId("share-invalid")).toBeTruthy();
    // 品牌锁定（wire-share-branding）：寻数·柏讯 + Wire 双语并呈
    expect(screen.getByTestId("share-topbar").textContent).toContain(
      "寻数·柏讯",
    );
    expect(screen.getByTestId("share-topbar").textContent).toContain("Wire");
    // 独立成壳：不进站点 header/footer，也无控制台布局
    expect(screen.queryByTestId("site-footer")).toBeNull();
    expect(screen.queryByTestId("console-footer")).toBeNull();
    // 标题前缀与 noindex 红线在 App 壳层同样生效
    expect(document.title).toContain("分享 · ");
    expect(
      document
        .querySelector('meta[name="robots"]')
        ?.getAttribute("content"),
    ).toBe("noindex");
  });
});
