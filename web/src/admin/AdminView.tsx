// 内部后台容器（#/admin/*）：角色门禁 + 布局壳 + 按左侧栏切换六页（客户/
// 账本/需求单/目录治理/用量/本地数据）。
// admin 双闸（wire-self-serve-launch 任务 5.2）：无 wire-admin 角色的登录
// 用户在此被角色门禁拦下（导航入口已在 App 按角色显隐，路由直达也进不来）；
// 子页请求自动附 Authorization: Bearer（见 admin/api.ts），不再持有任何
// 手填令牌——token prop 与 key 重挂载随令牌而变的旧制退场。
import { useTranslation } from "react-i18next";
import {
  AdminBrandBar,
  AdminLayout,
  parseAdminSection,
  type AdminSection,
} from "./AdminLayout";
import { hasAdminRole, loadSession, logoutLocal } from "../console/auth";
import { CustomersPage } from "./CustomersPage";
import { LedgerPage } from "./LedgerPage";
import { DemandsPage } from "./DemandsPage";
import { CatalogGovernancePage } from "./CatalogGovernancePage";
import { UsageOverviewPage } from "./UsageOverviewPage";
import { LocalDataPage } from "./LocalDataPage";

export function AdminView({
  section = "customers",
}: {
  section?: AdminSection;
}) {
  const { t } = useTranslation();
  const session = loadSession();
  const active: AdminSection = parseAdminSection(section);

  // 角色门禁：无 wire-admin 一律拦下（不留任何数据请求面）。品牌条照常
  // 渲染（wire-console-ux-fixes 5.2）：后台深链被拦时仍有品牌与返回门户
  // 出口，页面不悬空。
  if (!hasAdminRole(session)) {
    return (
      <div>
        <AdminBrandBar />
        <div
          data-testid="admin-role-gate"
          className="mx-auto mt-8 max-w-xl rounded-lg border border-amber-200 bg-amber-50/50 p-8"
        >
          <h1 className="text-xl font-semibold">{t("admin.roleGate.title")}</h1>
          <p className="mt-3 text-sm text-slate-600">
            {t("admin.roleGate.body")}
          </p>
        </div>
      </div>
    );
  }

  const logout = () => {
    logoutLocal();
    window.location.hash = "#/";
  };

  return (
    <AdminLayout
      section={active}
      identity={session!.identity}
      onLogout={logout}
    >
      {active === "customers" && <CustomersPage />}
      {active === "ledger" && <LedgerPage />}
      {active === "demands" && <DemandsPage />}
      {active === "catalog" && <CatalogGovernancePage />}
      {active === "usage" && <UsageOverviewPage />}
      {active === "localdata" && <LocalDataPage />}
    </AdminLayout>
  );
}
