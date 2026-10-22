// 控制台布局：左侧栏（工作台 / AI 构建 / 产物 / 我的数据 / 实体 / 源包 /
// 我的需求 / 用量 / 数据面 Key）+ 顶栏（登录客户标识 + 当前档位 + 登出）+
// 内容区。工作台为首位聚合主页（wire-personal-workbench 任务 2.2）；默认
// 落地仍是「我的数据」（landing_tab 值域新增 workbench 仅作候选）。
// chat tab 显示名「AI 构建」，id 保持 chat 不变（路由兼容，任务 2.2）；
// 页面为壹座试用引导卡（ChatPage）。
// demands tab 已转正（wire-ux-consolidation 任务 4.3）：「我的需求」只读列表
//（MyDemandsPage），不再是「上线中」占位。
// 身份来自登录会话（auth.ts，Authorization: Bearer）；X-Customer-Id 可编辑的
// 开发占位顶栏已退场（wire-self-serve-launch 任务 4.4）。
// 顶栏活跃队徽标（wire-team-spaces-v2 6.4）：多队身份（GET /api/console/teams
// 长度 > 1）才在用户区旁渲染当前队名；单队/拉取失败静默零渲染。
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { getTeams } from "./api";

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
  placeholder?: boolean;
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

export function ConsolePlaceholder({
  titleKey,
  bodyKey,
}: {
  titleKey: string;
  bodyKey: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="console-placeholder"
      className="mx-auto max-w-xl rounded-lg border border-amber-200 bg-amber-50/50 p-8"
    >
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">{t(titleKey)}</h1>
        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-700">
          {t("badge.comingSoon")}
        </span>
      </div>
      <p className="mt-4 text-sm text-slate-600">{t(bodyKey)}</p>
    </div>
  );
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
  const { t } = useTranslation();
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
                      {item.placeholder && (
                        <span
                          className={`ml-auto rounded px-1 py-0.5 text-[10px] ${
                            current
                              ? "bg-white/20 text-white"
                              : "bg-amber-100 text-amber-700"
                          }`}
                        >
                          {t("badge.comingSoonShort")}
                        </span>
                      )}
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
          <button
            type="button"
            data-testid="console-logout"
            onClick={onLogout}
            className="ml-auto rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            {t("console.topbar.logout")}
          </button>
        </div>
        <div className="min-w-0 flex-1 p-6">{children}</div>
      </div>
    </div>
  );
}
