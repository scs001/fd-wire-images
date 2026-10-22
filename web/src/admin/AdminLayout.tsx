// 内部后台布局（#/admin/*）：左侧栏（客户 / 账本 / 需求单 / 目录治理 / 用量 /
// 本地数据）+ 顶栏（登录身份 + 返回前台 + 登出）+ 内容区。
// admin 双闸（wire-self-serve-launch 任务 5.2）：X-Admin-Token 输入框与
// dev-admin-token 默认值退场——浏览器只持登录 Bearer，服务端令牌由 nginx
// 注入；本组件只负责按登录角色呈现（入口显隐在 App 导航与 AdminView 门禁）。
import { type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { markAdminSplitDone } from "../console/auth";

export type AdminSection =
  | "customers"
  | "ledger"
  | "demands"
  | "catalog"
  | "usage"
  | "localdata";

export const ADMIN_SECTIONS: ReadonlyArray<{
  id: AdminSection;
  href: string;
  labelKey: string;
}> = [
  { id: "customers", href: "#/admin/customers", labelKey: "admin.nav.customers" },
  { id: "ledger", href: "#/admin/ledger", labelKey: "admin.nav.ledger" },
  { id: "demands", href: "#/admin/demands", labelKey: "admin.nav.demands" },
  { id: "catalog", href: "#/admin/catalog", labelKey: "admin.nav.catalog" },
  { id: "usage", href: "#/admin/usage", labelKey: "admin.nav.usage" },
  {
    id: "localdata",
    href: "#/admin/localdata",
    labelKey: "admin.nav.localData",
  },
];

export function parseAdminSection(segment: string | undefined): AdminSection {
  if (
    segment === "customers" ||
    segment === "ledger" ||
    segment === "demands" ||
    segment === "catalog" ||
    segment === "usage" ||
    segment === "localdata"
  ) {
    return segment;
  }
  return "customers";
}

/** 后台品牌条（wire-console-ux-fixes 任务 5.2）：后台独立成壳后自带的极简
 *  顶条——门户站点顶栏不再包裹 #/admin，本条承载品牌与返回门户出口。既挂在
 *  AdminLayout 顶部（正常后台页），也挂在 AdminView 角色门禁拦截分支（深链
 *  无角色时不悬空）。 */
export function AdminBrandBar() {
  const { t } = useTranslation();
  return (
    <div
      data-testid="admin-brand-bar"
      className="flex items-center gap-3 border-b border-slate-100 px-6 py-2.5"
    >
      <a href="#/" className="text-sm font-semibold tracking-wide">
        {t("brand.name")}
      </a>
      <span className="text-xs text-slate-400">{t("admin.title")}</span>
      <a
        href="#/"
        data-testid="admin-back-portal"
        className="ml-auto text-xs text-slate-500 underline hover:text-slate-800"
      >
        {t("admin.layout.backToPortal")}
      </a>
    </div>
  );
}

export function AdminLayout({
  section,
  identity,
  onLogout,
  children,
}: {
  section: AdminSection;
  /** 登录身份展示（ID token claims 的 email/username）。 */
  identity: string;
  onLogout: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();

  return (
    <div className="flex min-h-[32rem] flex-col rounded-lg border border-slate-200 bg-white shadow-sm">
      <AdminBrandBar />
      <div className="flex min-h-0 flex-1">
        <aside className="w-52 shrink-0 border-r border-slate-200 p-4">
        <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
          {t("admin.title")}
        </div>
        <nav className="mt-3 flex flex-col gap-1" data-testid="admin-nav">
          {ADMIN_SECTIONS.map((item) => (
            <a
              key={item.id}
              href={item.href}
              data-testid={`admin-nav-${item.id}`}
              className={`flex items-center rounded-md px-3 py-2 text-sm transition-colors ${
                section === item.id
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              <span>{t(item.labelKey)}</span>
            </a>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div
          data-testid="admin-topbar"
          className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-slate-200 px-6 py-3"
        >
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">
              {t("admin.topbar.identity")}
            </span>
            <span
              data-testid="admin-identity"
              className="truncate rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700"
              title={identity}
            >
              {identity}
            </span>
          </div>
          <span className="text-[11px] text-slate-400">
            {t("admin.topbar.gateNote")}
          </span>
          <button
            type="button"
            data-testid="admin-logout"
            onClick={onLogout}
            className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            {t("console.topbar.logout")}
          </button>
          {/* 控制台出口（wire-admin-console-routing 任务 3.1 / design D2）：
              后台是「管理者视角」，但管理员同时是客户——其看板/产物/我的数据
              等第一人称资产只在控制台可达。此出口与「返回前台」并列而非合并：
              二者目的不同（回门户浏览 vs 进控制台用资产），且取消分流会让
              管理员每次登录多点一次，故保留分流 + 补出口。
              落点是裸 #/console（而非显式子路径）：只有裸入口才走用户的偏好
              落地序（置顶看板 > landing_tab），符合「管理员同时是客户」语义；
              点击即置分流标记，使这次显式进入不再被角色分流送回后台。 */}
          <a
            href="#/console"
            data-testid="admin-goto-console"
            onClick={markAdminSplitDone}
            className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            {t("nav.console")}
          </a>
          <a
            href="#/"
            data-testid="admin-exit"
            className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            {t("admin.topbar.exit")}
          </a>
        </div>
        <div className="min-w-0 flex-1 p-6">{children}</div>
      </div>
      </div>
    </div>
  );
}

/** 鉴权失败（401/403）与一般错误的如实呈现（含重试）。 */
export function AdminErrorBox({
  error,
  onRetry,
}: {
  error: { status?: number; message: string; detail?: string };
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const auth = error.status === 401 || error.status === 403;
  return (
    <div
      data-testid={auth ? "admin-auth-error" : "admin-error"}
      className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
    >
      <p className="text-sm font-medium text-red-700">
        {auth ? t("admin.error.authTitle") : t("admin.error.title")}
      </p>
      <p className="mt-1 text-xs text-red-600" data-testid="admin-error-detail">
        {error.detail ?? error.message}
      </p>
      {auth && (
        <p className="mt-1 text-xs text-red-500">{t("admin.error.authHint")}</p>
      )}
      <button
        type="button"
        onClick={onRetry}
        className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
      >
        {t("admin.error.retry")}
      </button>
    </div>
  );
}

/** 统一的加载/空态小件（各页复用，状态齐备）。 */
export function AdminStates({
  loading,
  empty,
  emptyTestId = "admin-empty",
  children,
}: {
  loading: boolean;
  empty: boolean;
  emptyTestId?: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  if (loading) {
    return (
      <p className="mt-6 text-xs text-slate-400" data-testid="admin-loading">
        {t("admin.common.loading")}
      </p>
    );
  }
  if (empty) {
    return (
      <div
        data-testid={emptyTestId}
        className="mt-6 rounded-lg border border-dashed border-slate-300 p-8 text-center"
      >
        <p className="text-sm text-slate-600">{t("admin.common.empty")}</p>
      </div>
    );
  }
  return <>{children}</>;
}
