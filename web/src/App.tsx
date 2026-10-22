// App shell: top nav (site / pricing / docs / catalog / console, zh–en toggle)
// over the views. Hash routing, deliberately no router dependency — parametrized
// routes are parsed by parseHash below (catalog list, catalog detail, console
// tabs, demand placeholder).
// 页脚分发（wire-ux-consolidation 任务 1.2）：门户视图挂全量页脚，
// #/console/* 挂紧凑页脚，#/admin 不渲染页脚。
// 公开分享页（wire-dashboard-share；form/record-view/doc 后续变更同壳追加）：
// #/share/{dashboard|form|record-view|doc}/{id}?t=&v= 完全独立成壳（极简顶栏
// + 分享页脚，noindex），不进上方任何布局。
// 登录件（wire-self-serve-launch 4.4/5.2）：
// - Logto 回调走真实路径 /console/callback（App 挂载时检测 pathname 进入
//   CallbackView，完成后回 hash 路由）；
// - 「内部后台」导航按登录角色显隐（ID token roles 含 wire-admin 才渲染）。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { HomeView } from "./views/HomeView";
import { ConsoleView } from "./views/ConsoleView";
import { LoginView } from "./views/LoginView";
import { CatalogView } from "./views/CatalogView";
import { CatalogDetailView } from "./views/CatalogDetailView";
import { DemandNewView } from "./views/DemandNewView";
import { ShareView } from "./views/ShareView";
import { PricingView } from "./views/PricingView";
import { DocsView } from "./views/DocsView";
import { ShowcaseView } from "./views/ShowcaseView";
import { AdminView } from "./admin/AdminView";
import { parseAdminSection, type AdminSection } from "./admin/AdminLayout";
import { CallbackView } from "./console/CallbackView";
import { loadSession } from "./console/auth";
import { putPreferences } from "./console/api";
import type { ConsoleTab } from "./console/ConsoleLayout";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { SiteFooter } from "./components/SiteFooter";
import { switchLanguage, type Language } from "./i18n";

export type Route =
  | { name: "home" }
  | { name: "pricing" }
  | { name: "docs" }
  | { name: "catalog" }
  | { name: "catalogDetail"; id: string }
  | { name: "console"; tab: ConsoleTab }
  | { name: "demandNew"; indicator: string | null }
  | { name: "admin"; section: AdminSection }
  | { name: "shareDashboard"; id: string; token: string | null; version: string | null }
  | { name: "shareForm"; id: string; token: string | null; version: string | null }
  | { name: "shareDoc"; id: string; token: string | null; version: string | null }
  | { name: "shareRecordView"; id: string; token: string | null; recordKey: string | null }
  | { name: "showcase" }
  | { name: "showcaseDetail"; id: number }
  | { name: "login" };

/** 控制台二级段 → tab（未知段回落默认 datasets）。 */
function parseConsoleTab(segment: string | undefined): ConsoleTab {
  if (
    segment === "workbench" ||
    segment === "chat" ||
    segment === "artifacts" ||
    segment === "datasets" ||
    segment === "entities" ||
    segment === "packs" ||
    segment === "demands" ||
    segment === "usage" ||
    segment === "keys" ||
    segment === "team" ||
    segment === "market"
  ) {
    return segment;
  }
  return "datasets";
}

/** "#/catalog/abc?indicator=x" → 结构化路由（未知路径回落首页）。 */
export function parseHash(rawHash: string): Route {
  const path = rawHash || "#/";
  const [rawPath, rawQuery = ""] = path.replace(/^#/, "").split("?");
  const segments = rawPath.split("/").filter(Boolean);
  const query = new URLSearchParams(rawQuery);
  if (segments.length === 0) return { name: "home" };
  const [head, second, third] = segments;
  if (head === "pricing") return { name: "pricing" };
  if (head === "docs") return { name: "docs" };
  if (head === "showcase") {
    // 橱窗（wire-showcase-market）：匿名公开只读面。详情 id 非法（非数字）
    // 回落列表——比渲染一个必然 404 的详情页更诚实。
    if (second === undefined) return { name: "showcase" };
    const id = Number(second);
    return Number.isInteger(id) && id > 0
      ? { name: "showcaseDetail", id }
      : { name: "showcase" };
  }
  if (head === "console") {
    if (second === "demands" && third === "new") {
      return { name: "demandNew", indicator: query.get("indicator") };
    }
    // 旧深链 ?c=<id> 仅服务于已退役的对话页：查询参数被忽略，仍落对应 tab
    //（chat 落壹座引导卡），路由兼容。
    return { name: "console", tab: parseConsoleTab(second) };
  }
  if (head === "admin") {
    // #/admin/customers|ledger|demands|catalog|usage（未知段回落客户页）
    return { name: "admin", section: parseAdminSection(second) };
  }
  if (head === "login") return { name: "login" };
  if (head === "share" && second === "dashboard" && third) {
    // 公开分享深链 #/share/dashboard/{id}?t=…&v=…（wire-dashboard-share）：
    // t 缺失/坏由 ShareView 如实落「链接无效或已过期」页，不在路由层拦截。
    return {
      name: "shareDashboard",
      id: third,
      token: query.get("t"),
      version: query.get("v"),
    };
  }
  if (head === "share" && second === "form" && third) {
    // 公开表单深链 #/share/form/{id}?t=…&v=…（wire-form-builder 任务 4.5）：
    // 与看板分享同款分派——四态框架/品牌面/noindex 沿用，运行时换 FormRuntime。
    return {
      name: "shareForm",
      id: third,
      token: query.get("t"),
      version: query.get("v"),
    };
  }
  if (head === "share" && second === "doc" && third) {
    // 公开文档深链 #/share/doc/{id}?t=…&v=…（wire-rich-text-docs 任务 5.2）：
    // 与看板分享同款分派——四态框架/品牌面/noindex 沿用，正文换 MarkdownView；
    // t/v 缺失/坏由 ShareView 如实落「链接无效或已过期」页，不在路由层拦截。
    return {
      name: "shareDoc",
      id: third,
      token: query.get("t"),
      version: query.get("v"),
    };
  }
  if (head === "share" && second === "record-view" && third) {
    // 单记录公开页深链 #/share/record-view/{id}?t=…&k=…（wire-record-view
    // 任务 5.1）：无 v 参数（版本钉死在令牌内），k=键值（扫码/输键取数）；
    // t 缺失/坏由 ShareView 如实落「链接无效或已过期」页，不在路由层拦截。
    return {
      name: "shareRecordView",
      id: third,
      token: query.get("t"),
      recordKey: query.get("k"),
    };
  }
  if (head === "catalog") {
    if (segments.length >= 2) {
      const raw = segments.slice(1).join("/");
      let id = raw;
      try {
        id = decodeURIComponent(raw);
      } catch {
        /* 保留原样，交由 API 404 */
      }
      return { name: "catalogDetail", id };
    }
    return { name: "catalog" };
  }
  return { name: "home" };
}

/** Logto 授权码回调走真实路径（非 hash）：命中即渲染回调页。 */
export function isConsoleCallbackPath(pathname: string): boolean {
  return pathname.replace(/\/+$/, "") === "/console/callback";
}

function renderRoute(route: Route) {
  switch (route.name) {
    case "pricing":
      return <PricingView />;
    case "docs":
      return <DocsView />;
    case "showcase":
      return <ShowcaseView />;
    case "showcaseDetail":
      return <ShowcaseView detailId={route.id} />;
    case "catalog":
      return <CatalogView />;
    case "catalogDetail":
      return <CatalogDetailView id={route.id} />;
    case "console":
      return <ConsoleView tab={route.tab} />;
    case "demandNew":
      return <DemandNewView indicator={route.indicator} />;
    case "admin":
      return <AdminView section={route.section} />;
    case "shareDashboard":
      return (
        <ShareView
          artifactId={route.id}
          token={route.token ?? ""}
          version={route.version}
        />
      );
    case "shareForm":
      return (
        <ShareView
          artifactId={route.id}
          token={route.token ?? ""}
          version={route.version}
          mode="form"
        />
      );
    case "shareDoc":
      return (
        <ShareView
          artifactId={route.id}
          token={route.token ?? ""}
          version={route.version}
          mode="doc"
        />
      );
    case "shareRecordView":
      return (
        <ShareView
          artifactId={route.id}
          token={route.token ?? ""}
          version={null}
          mode="record-view"
          recordKey={route.recordKey ?? ""}
        />
      );
    case "login":
      return <LoginView />;
    case "home":
      return <HomeView />;
  }
}

export function App() {
  const { t, i18n } = useTranslation();
  const [hash, setHash] = useState(window.location.hash || "#/");

  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const route = parseHash(hash);
  // 控制台/内部后台是工作台布局（自带侧栏与宽内容区），主容器放宽。
  const wideMain = route.name === "console" || route.name === "admin";
  // 页脚分发（wire-console-ux-fixes 任务 3.1）：控制台是工作台布局（自带
  // 侧栏与常驻顶栏），页脚从内容流里摘除——admin/console/demandNew 一律
  // 无页脚，其余门户视图（含登录页）保留全量 SiteFooter。
  const footer =
    route.name === "admin" ||
    route.name === "console" ||
    route.name === "demandNew"
      ? null
      : <SiteFooter />;
  // Logto 回调：真实路径 /console/callback 优先于 hash 路由（完成后回 hash）。
  const callbackActive = isConsoleCallbackPath(window.location.pathname);
  // 内部后台入口不再挂在门户顶栏（wire-console-ux-fixes 5.2）：后台独立成壳，
  // 管理员由分流落地直达 #/admin；角色门禁仍在 AdminView 与 admin API 双闸。

  const tab = (h: string, label: string, active: boolean, testid?: string) => (
    <a
      href={h}
      data-testid={testid}
      className={`px-3 py-1.5 rounded-md text-sm transition-colors ${
        active ? "bg-slate-900 text-white" : "hover:bg-slate-200"
      }`}
    >
      {label}
    </a>
  );

  // 语言切换（wire-personal-preferences）：本地照旧先切；登录态下顺带把
  // ui_locale 即写 server（fire-and-forget，静默失败——偏好同步绝不打断
  // 切换本身）；未登录仅本地（localStorage），不发任何请求。
  const handleLanguageSwitch = (lang: Language) => {
    switchLanguage(lang);
    if (!loadSession()?.idToken) return;
    try {
      void putPreferences({ ui_locale: lang }).catch(() => {
        /* 偏好写失败静默：本地语言已生效，下次登录再收敛 */
      });
    } catch {
      /* 会话读取异常等：仅本地语言切换 */
    }
  };

  const langButton = (lang: Language, label: string) => (
    <button
      type="button"
      onClick={() => handleLanguageSwitch(lang)}
      className={`px-2 py-1 rounded text-xs transition-colors ${
        i18n.language === lang
          ? "bg-slate-200 font-medium"
          : "hover:bg-slate-100"
      }`}
    >
      {label}
    </button>
  );

  if (callbackActive) {
    return (
      <div className="min-h-screen">
        <CallbackView />
      </div>
    );
  }

  // 公开分享页（wire-dashboard-share / wire-form-builder / wire-record-view /
  // wire-rich-text-docs）：独立极简壳——自带「Wire」顶栏与「由柏讯分享」
  // 页脚，不进站点导航/控制台布局，无需登录。
  if (
    route.name === "shareDashboard" ||
    route.name === "shareForm" ||
    route.name === "shareDoc" ||
    route.name === "shareRecordView"
  ) {
    return <ErrorBoundary>{renderRoute(route)}</ErrorBoundary>;
  }

  return (
    <div className="min-h-screen">
      {/* 内部后台独立成壳（wire-console-ux-fixes 任务 5.2）：#/admin 不被
          门户站点顶栏包裹——后台自有极简顶条（AdminLayout），门户页签/
          语言切换/登录入口都只在门户与控制台面呈现。 */}
      {route.name !== "admin" && (
        <header className="border-b border-slate-200">
          <div className="max-w-5xl mx-auto px-4 py-4 flex items-center gap-4">
            <a href="#/" className="text-lg font-semibold tracking-wide">
              {t("brand.name")}
              <span className="ml-2 text-sm font-normal text-slate-500">
                {t("brand.tagline")}
              </span>
            </a>
            <nav className="ml-auto flex items-center gap-1">
              {tab("#/", t("nav.home"), route.name === "home")}
              {tab("#/pricing", t("nav.pricing"), route.name === "pricing")}
              {tab("#/docs", t("nav.docs"), route.name === "docs")}
              {tab("#/catalog", t("nav.catalog"), route.name === "catalog" || route.name === "catalogDetail")}
              {tab(
                "#/showcase",
                t("nav.showcase"),
                route.name === "showcase" || route.name === "showcaseDetail",
              )}
              {tab("#/console", t("nav.console"), route.name === "console" || route.name === "demandNew")}

              <a
                href="#/login"
                className="ml-2 px-3 py-1.5 rounded-md text-sm border border-slate-300 hover:bg-slate-100 transition-colors"
              >
                {t("nav.login")}
              </a>
              <div className="ml-2 flex items-center rounded-md border border-slate-200 overflow-hidden">
                {langButton("zh", "中")}
                {langButton("en", "EN")}
              </div>
            </nav>
          </div>
        </header>
      )}
      <main
        className={
          wideMain ? "mx-auto max-w-7xl px-4 py-6" : "mx-auto max-w-5xl px-4 py-8"
        }
      >
        <ErrorBoundary>{renderRoute(route)}</ErrorBoundary>
      </main>
      {footer}
    </div>
  );
}
