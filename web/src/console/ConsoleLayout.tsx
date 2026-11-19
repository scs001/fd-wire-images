// 控制台布局：左侧栏（工作台 / AI 构建 / 产物 / 我的数据 / 实体 / 源包 /
// 我的需求 / 用量 / 数据面 Key）+ 顶栏（品牌字 + 返回门户 + 语言切换 +
// org/plan/身份 + 活跃队徽标 + 登出）+ 内容区。工作台为首位聚合主页
//（wire-personal-workbench 任务 2.2）；默认落地仍是「我的数据」
//（landing_tab 值域新增 workbench 仅作候选）。
// chat tab 显示名「AI 构建」，id 保持 chat 不变（路由兼容，任务 2.2）；
// 页面为壹座试用引导卡（ChatPage）。
// demands tab 已转正（wire-ux-consolidation 任务 4.3）：「我的需求」只读列表
//（MyDemandsPage），不再是「上线中」占位；需求单提交子页
//（#/console/demands/new）折入本壳（extract-console-shell 1.4，页签高亮
// demands）。
// 身份来自登录会话（auth.ts，Authorization: Bearer）；X-Customer-Id 可编辑的
// 开发占位顶栏已退场（wire-self-serve-launch 任务 4.4）。
// 顶栏活跃队徽标（wire-team-spaces-v2 6.4）：多队身份（GET /api/console/teams
// 长度 > 1）才在用户区旁渲染当前队名；单队/拉取失败静默零渲染。
// 摘壳 parity（extract-console-shell 1.2/1.3）：控制台不再被门户顶栏包裹，
// 顶栏自带语言切换（与门户 langButton 同一 i18n 机制与持久化）与返回门户
// 入口（比照 AdminLayout admin-back-portal 模式）。
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { getTeams, putPreferences } from "./api";
import { loadSession } from "./auth";
import { switchLanguage, type Language } from "../i18n";

export type ConsoleTab =
  | "workbench"
  | "chat"
  | "artifacts"
  | "datasets"
  | "entities"
  | "packs"
  | "demands"
  | "usage"
  | "keys"
  | "team"
  | "market";

/**
 * 侧栏分组（wire-console-shell）：十一项按客户的真实心智归四组——
 * - 概览：工作台（单屏聚合）
 * - 数据：自己的（我的数据/实体）、来源（源包）、别人的（数据市场）
 * - 构建：对话入口（AI 构建）与产出（产物）
 * - 运营：需求、用量与凭据、团队
 *
 * 分组只改组织方式，不改任何页签的 id/href/权限/默认落地。
 */
export const CONSOLE_NAV_GROUPS: ReadonlyArray<{
  id: "overview" | "data" | "build" | "ops";
  labelKey: string;
  tabs: ReadonlyArray<ConsoleTab>;
}> = [
  { id: "overview", labelKey: "console.nav.groups.overview", tabs: ["workbench"] },
  {
    id: "data",
    labelKey: "console.nav.groups.data",
    tabs: ["datasets", "entities", "packs", "market"],
  },
  {
    id: "build",
    labelKey: "console.nav.groups.build",
    tabs: ["chat", "artifacts"],
  },
  {
    id: "ops",
    labelKey: "console.nav.groups.ops",
    tabs: ["demands", "usage", "keys", "team"],
  },
];

export const CONSOLE_TABS: ReadonlyArray<{
  id: ConsoleTab;
  href: string;
  labelKey: string;
}> = [
  // 工作台（wire-personal-workbench 任务 2.2）：侧栏首位——聚合各页信息
  // 资产的单屏主页；默认落地不变（仍是「我的数据」，landing_tab 值域新增
  // workbench 仅作为候选）。
  {
    id: "workbench",
    href: "#/console/workbench",
    labelKey: "console.nav.workbench",
  },
  { id: "chat", href: "#/console/chat", labelKey: "console.nav.chat" },
  { id: "artifacts", href: "#/console/artifacts", labelKey: "console.nav.artifacts" },
  {
    id: "datasets",
    href: "#/console/datasets",
    labelKey: "console.nav.datasets",
  },
  // 实体（wire-entity-layer 任务 3.1）：轻量实体语义层入口——声明/关系/
  // 图谱；纯元数据操作零引擎调用，免费档可用。
  {
    id: "entities",
    href: "#/console/entities",
    labelKey: "console.nav.entities",
  },
  // 源包（wire-source-packs 任务 3.1）：主题包订阅入口，免费档可用。
  {
    id: "packs",
    href: "#/console/packs",
    labelKey: "console.nav.packs",
  },
  {
    id: "demands",
    href: "#/console/demands",
    labelKey: "console.nav.demands",
  },
  // 用量页已正式化（usage-transparency 6.2）：不再是「上线中」占位。
  { id: "usage", href: "#/console/usage", labelKey: "console.nav.usage" },
  // 数据面 key（wire-customer-data-engine 6.2）：REST 数据面凭据管理。
  { id: "keys", href: "#/console/keys", labelKey: "console.nav.keys" },
  // 团队（wire-team-spaces 任务 5）：成员/席位/邀请管理——owner 管理面，
  // editor/viewer 只读名单。
  { id: "team", href: "#/console/team", labelKey: "console.nav.team" },
  // 数据市场（wire-dataset-marketplace 任务 6.2）：其他客户开放的数据集
  // 浏览订阅面——列表 SQL 层只查 market 档，私有数据永不出现。
  { id: "market", href: "#/console/market", labelKey: "console.nav.market" },
];

/** 页签 id → 定义（分组渲染按 id 取 href/label，避免两处重复维护）。 */
const TAB_BY_ID = new Map(CONSOLE_TABS.map((tab) => [tab.id, tab]));

/** 分组渲染所需：按 CONSOLE_NAV_GROUPS 的顺序取回页签定义。 */
export function tabsInGroup(
  groupId: (typeof CONSOLE_NAV_GROUPS)[number]["id"],
): ReadonlyArray<NonNullable<ReturnType<typeof TAB_BY_ID.get>>> {
  const group = CONSOLE_NAV_GROUPS.find((item) => item.id === groupId);
  if (group === undefined) return [];
  return group.tabs
    .map((id) => TAB_BY_ID.get(id))
    .filter((tab): tab is NonNullable<typeof tab> => tab !== undefined);
}

export function ConsoleLayout({
  tab,
  customerName,
  planCode,
  identity,
  onLogout,
  children,
}: {
  tab: ConsoleTab;
  /** 登录映射客户名（会话建立响应 customer.name）。 */
  customerName: string;
  /** 当前档位 code（免费档门禁/档位徽标）。 */
  planCode: string;
  /** 登录身份展示（ID token claims 的 email/username）。 */
  identity: string;
  onLogout: () => void;
  children: ReactNode;
}) {
  const { t, i18n } = useTranslation();
  // 活跃队徽标（wire-team-spaces-v2 6.4）：挂载即拉一次 GET /api/console/teams
  // ——多队身份才亮徽标（数据面随活跃队切换，切完由 TeamPage 整页 reload
  // 重建本组件）；单队/失败静默（徽标缺席不阻塞控制台）。
  const [activeTeamName, setActiveTeamName] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    getTeams()
      .then((out) => {
        if (cancelled || out.teams.length <= 1) return;
        const active =
          out.teams.find((m) => m.customerId === out.activeCustomerId) ??
          out.teams.find((m) => m.isActive);
        if (active) setActiveTeamName(active.customerName);
      })
      .catch(() => {
        /* 静默降级：不渲染徽标 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 语言切换（extract-console-shell 1.2）：与门户顶栏 langButton 同一
  // i18n 读写路径与持久化语义——本地 switchLanguage（i18n/changeLanguage +
  // localStorage wire-web-lang）即时生效（侧栏分组标题/页签/内容全量跟随）；
  // 登录态顺带把 ui_locale 即写 server（fire-and-forget，静默失败——偏好
  // 同步绝不打断切换本身）；未登录仅本地，不发任何请求。
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

  // 按钮样式沿门户 langButton 惯例（App.tsx）：激活态 bg-slate-200，
  // 文案沿用门户惯例（中文态显示 EN、英文态显示 中——两种语言并排常驻）。
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

  return (
    <div className="flex min-h-[32rem] flex-col rounded-lg border border-slate-200 bg-white shadow-sm md:flex-row">
      {/* 窄屏侧栏横向化（wire-console-shell 窄屏要求）：md 以下铺成一条横向
          可滚动的分组条，内容区拿到完整宽度；md 以上恢复固定宽侧栏。 */}
      <aside className="shrink-0 border-b border-slate-200 p-4 md:w-52 md:border-b-0 md:border-r">
        <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
          {t("console.title")}
        </div>
        <nav
          className="mt-3 flex gap-4 overflow-x-auto pb-1 md:block md:gap-0 md:overflow-visible"
          data-testid="console-nav"
        >
          {CONSOLE_NAV_GROUPS.map((group) => (
            <div
              key={group.id}
              data-testid={`console-nav-group-${group.id}`}
              className="shrink-0 md:mb-4 md:shrink"
            >
              <div
                className="px-3 pb-1 text-[11px] font-medium uppercase tracking-wide text-slate-400"
                data-testid={`console-nav-group-label-${group.id}`}
              >
                {t(group.labelKey)}
              </div>
              <div className="flex gap-1 md:flex-col">
                {tabsInGroup(group.id).map((item) => {
                  const current = tab === item.id;
                  return (
                    <a
                      key={item.id}
                      href={item.href}
                      data-testid={`console-nav-${item.id}`}
                      aria-current={current ? "page" : undefined}
                      className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors ${
                        current
                          ? "bg-slate-900 text-white"
                          : "text-slate-700 hover:bg-slate-100"
                      }`}
                    >
                      <span>{t(item.labelKey)}</span>
                    </a>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div
          data-testid="console-topbar"
          className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-slate-200 px-6 py-3"
        >
          {/* 品牌字 + 返回门户（extract-console-shell 1.3）：比照 AdminLayout
              AdminBrandBar 的品牌条布局——控制台摘壳后顶栏承载品牌与门户
              出口，门户浏览 → 控制台作业随时可回。 */}
          <a href="#/" className="text-sm font-semibold tracking-wide">
            {t("brand.name")}
          </a>
          <a
            href="#/"
            data-testid="console-back-portal"
            className="text-xs text-slate-500 underline hover:text-slate-800"
          >
            {t("console.topbar.backToPortal")}
          </a>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">
              {t("console.topbar.org")}
            </span>
            <span
              data-testid="console-org"
              className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700"
            >
              {customerName}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">
              {t("console.topbar.plan")}
            </span>
            <span
              data-testid="console-plan"
              className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700"
            >
              {planCode}
            </span>
          </div>
          <span
            data-testid="console-identity"
            className="truncate text-xs text-slate-500"
            title={identity}
          >
            {identity}
          </span>
          {/* 活跃队徽标（v2）：多队身份才渲染（单队零打扰）。 */}
          {activeTeamName != null && (
            <span
              data-testid="active-team-badge"
              title={t("console.topbar.activeTeam")}
              className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs text-indigo-700 ring-1 ring-indigo-100"
            >
              {activeTeamName}
            </span>
          )}
          {/* 顶栏右侧（extract-console-shell 1.2）：语言切换（identity 区旁、
              登出左侧）+ 登出——语言切换沿用门户 langButton 的样式与
              i18n 持久化机制。 */}
          <div className="ml-auto flex items-center gap-2">
            <div className="flex items-center rounded-md border border-slate-200 overflow-hidden">
              {langButton("zh", "中")}
              {langButton("en", "EN")}
            </div>
            <button
              type="button"
              data-testid="console-logout"
              onClick={onLogout}
              className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
            >
              {t("console.topbar.logout")}
            </button>
          </div>
        </div>
        <div className="min-w-0 flex-1 p-6">{children}</div>
      </div>
    </div>
  );
}
