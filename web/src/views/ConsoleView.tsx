// 控制台容器（#/console/*）：登录引导/阻塞态 + 会话建立 + 布局壳 + 按 tab
// 切换子页（我的数据默认、AI 构建引导卡、产物、需求单占位、用量）。
// 身份流（wire-self-serve-launch 任务 4.4）：
//   拉登录配置 →（未配置=阻塞态，绝不假登录）→ 无会话=登录引导 →
//   会话过期先静默续期 → POST /api/console/session 建立映射/触发首登开户 →
//   key_once 明文展示一次（关闭后不可再查）→ 进入控制台。
// X-Customer-Id 开发占位已退场；子页请求一律自动附 Authorization: Bearer。
// 个人偏好（wire-personal-preferences）：会话建立后拉取 GET
// /api/console/me/preferences（失败不阻塞启动）——locale 收敛（server 有值
// 覆盖本地 / 全空首登领养上传）；落地序仅裸 #/console 走偏好（pinned >
// landing_tab > datasets，显式页签直达不劫持）；用户切页签即写
// landing_tab，偏好驱动的程序跳转经 suppress 不回写；置顶看板经
// initialDashboardId 传给产物页挂载即展开。
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConsoleLayout, type ConsoleTab } from "../console/ConsoleLayout";
import { ErrorBoundary } from "../components/ErrorBoundary";
import {
  beginLogin,
  clearSession,
  establishSession,
  fetchAuthConfig,
  hasAdminRole,
  hasAdminSplitDone,
  isExpired,
  loadSession,
  logoutLocal,
  markAdminSplitDone,
  refreshSession,
  type ConsoleAuthConfig,
  type ConsoleAuthError,
  type ConsoleSession,
} from "../console/auth";
import {
  getPreferences,
  putPreferences,
  type ConsolePreferences,
} from "../console/api";
import i18nInstance, { switchLanguage } from "../i18n";
import { ChatPage } from "../console/ChatPage";
import { AssistantPanel } from "../console/assistant/AssistantPanel";
import { WorkbenchPage } from "../console/WorkbenchPage";
import { ArtifactsPage } from "../console/ArtifactsPage";
import { DatasetsPage } from "../console/DatasetsPage";
import { EntitiesPage } from "../console/EntitiesPage";
import { PacksPage } from "../console/PacksPage";
import { MyDemandsPage } from "../console/MyDemandsPage";
import { UsagePage } from "../console/UsagePage";
import { KeysPage } from "../console/KeysPage";
import { TeamPage } from "../console/TeamPage";
import { MarketPage } from "../console/MarketPage";
import { CONTACT_MAILTO } from "../views/HomeView";

type BootstrapPhase =
  | { kind: "loading" }
  | { kind: "blocked" }
  | { kind: "configError" }
  | { kind: "anonymous"; signInError?: string }
  | { kind: "sessionError"; reason?: string }
  | { kind: "ready"; session: ConsoleSession; keyOnce: string | null };

/** 静默续期提前量：距过期不足 5 分钟即尝试 refresh（有 refreshToken 才可能）。 */
const RENEW_AHEAD_MS = 5 * 60_000;

/** 偏好拉取等待上限（ms，wire-personal-preferences）：挂在 loading 门内
 *  先行拉取以避免可见的 datasets 闪现再跳——端点挂起时按无偏好放行，
 *  绝不让控制台启动无限等待。 */
const PREFERENCES_BOOT_TIMEOUT_MS = 2_000;

/** 裸控制台入口（无第二段）：#/console 或 #/console/ ——只有这种入口才走
 *  偏好落地（显式页签直达 #/console/usage 永不劫持）。 */
function isBareConsoleHash(): boolean {
  return /^#\/console\/?$/.test(window.location.hash);
}

/**
 * 裸 #/console 的 wire-admin 角色分流是否已落地过（wire-admin-console-routing
 * 任务 3.2 / design D1：保留分流，补可达性）——标记借 sessionStorage 承载
 * （auth.ts 的 mark/has/clearAdminSplitDone），随登出与新登录清除。
 * 分流本身是既有需求（管理员裸登录落后台，省一次点击），但管理员同时是客户——
 * 若每次裸入口都分流，其从后台（或文档页入口）回到裸 #/console 会被同一条规则
 * 再送回 #/admin，控制台恒不可达。故分流只在本次会话首次生效：二次进入裸入口
 * 按其偏好落地序走，与普通客户一致。要后台则手敲 #/admin——显式直达从不走分流，
 * 不受本标记影响。
 */

/** locale 收敛（wire-personal-preferences）：server ui_locale 有值 → 覆盖
 *  本地 i18n；三键全空（首登）→ 领养：把当前本地语言上传一次，此后换设备
 *  跟随。fire-and-forget，写失败静默（本地语言已可用）。 */
function convergeLocale(prefs: ConsolePreferences): void {
  if (prefs.ui_locale != null) {
    if (i18nInstance.language !== prefs.ui_locale) {
      switchLanguage(prefs.ui_locale);
    }
    return;
  }
  if (prefs.landing_tab == null && prefs.pinned_dashboard_id == null) {
    const lang = i18nInstance.language === "en" ? "en" : "zh";
    void putPreferences({ ui_locale: lang }).catch(() => {
      /* 领养失败静默：下次登录 GET 仍全空会再试 */
    });
  }
}

export function ConsoleView({ tab = "datasets" }: { tab?: ConsoleTab }) {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<BootstrapPhase>({ kind: "loading" });
  // 会话建立只跑一轮；retry 由显式事件触发（bootstrapToken 重新计数）。
  const [bootstrapToken, setBootstrapToken] = useState(0);
  const configRef = useRef<ConsoleAuthConfig | null>(null);
  // 个人偏好（wire-personal-preferences）：null = 未拉到（端点失败/未返回），
  // 拉到后驱动 locale 收敛、落地序与看板置顶徽标。
  const [prefs, setPrefs] = useState<ConsolePreferences | null>(null);
  // 置顶跳转要展开的看板产物 id：仅传给产物页做挂载期初始化；离开
  // artifacts 页签即清（回到该页不重复自动展开——展开只发生在落地那一次）。
  const [initialDashboardId, setInitialDashboardId] = useState<number | null>(
    null,
  );
  // 偏好驱动的程序跳转标记：记录**跳转目标页签**；当前 tab 到达该目标即
  // 清标记且不回写——跳转本身不得覆盖用户存的落地习惯。按「目标页签」而非
  // 「下一次 tab 变化」消费：真实浏览器里 hash 赋值的事件任务先于 React 的
  // ready 提交任务（首帧即已是目标页签），按变化消费会把标记留给用户的第一
  // 次切页签、吞掉那次写回（生产冒烟实弹抓出）。
  const suppressTabWriteRef = useRef<ConsoleTab | null>(null);
  // 上一次生效的 tab（ready 后首帧不算用户导航，变更才写回）。
  const lastTabRef = useRef<ConsoleTab | null>(null);

  const bootstrap = useCallback(async () => {
    setPhase({ kind: "loading" });
    let config: ConsoleAuthConfig;
    try {
      config = await fetchAuthConfig();
    } catch {
      setPhase({ kind: "configError" });
      return;
    }
    configRef.current = config;
    // 配置未下发：阻塞态提示，不发起任何认证跳转或受保护请求。
    if (!config.configured) {
      setPhase({ kind: "blocked" });
      return;
    }
    let session = loadSession();
    if (!session) {
      setPhase({ kind: "anonymous" });
      return;
    }
    // 过期/临期先静默续期；续期失败回落登录引导。
    if (isExpired(session, RENEW_AHEAD_MS)) {
      const renewed = await refreshSession(config, session);
      if (!renewed) {
        clearSession();
        setPhase({ kind: "anonymous" });
        return;
      }
      session = renewed;
    }
    try {
      const establishment = await establishSession(session.idToken);
      const next: ConsoleSession = {
        ...session,
        customer: establishment.customer,
      };
      // 个人偏好（wire-personal-preferences）：挂在 loading 门内先行拉取
      //（常规路径避免可见的 datasets 闪现再跳）；失败/超时绝不阻塞控制台
      // 启动——null 偏好 = 现状 datasets 兜底 + 本地语言，照常进入。
      let loaded: ConsolePreferences | null = null;
      let prefTimeout: ReturnType<typeof setTimeout> | undefined;
      try {
        loaded = await Promise.race([
          getPreferences(),
          new Promise<null>((resolve) => {
            prefTimeout = setTimeout(
              () => resolve(null),
              PREFERENCES_BOOT_TIMEOUT_MS,
            );
          }),
        ]);
      } catch {
        loaded = null; // 端点故障（4xx/5xx/网络）：照常进入控制台
      } finally {
        if (prefTimeout !== undefined) clearTimeout(prefTimeout);
      }
      // 角色分流（wire-console-ux-fixes 任务 5.1）：wire-admin 会话从裸
      // #/console 进入 → 直落内部后台（优先于任何偏好落地——design D6：
      // 管理员即便存过 landing_tab，裸登录也落 admin）。显式子路径
      //（#/console/usage 等）不劫持。hash 赋值后照常 setPhase ready——
      // App 的 hashchange 会切路由、ConsoleView 随之卸载；suppress 挡住
      // 「admin 不在 console tabs 内」可能引发的写回。
      // 可达性补丁（wire-admin-console-routing 任务 3.2 / design D1）：分流
      // 保留，但只在本会话首次生效（adminSplitDone，见其注释）。管理员同时是
      // 客户——若每次裸 #/console 都分流，其从后台（或文档页入口）回到裸入口
      // 会被同一条规则再送回后台，控制台恒不可达。显式的 #/admin / #/console/xxx
      // 直达从不走本分支，不受标记影响。
      if (isBareConsoleHash() && hasAdminRole(next) && !hasAdminSplitDone()) {
        markAdminSplitDone();
        suppressTabWriteRef.current = "datasets";
        window.location.hash = "#/admin";
        setPhase({ kind: "ready", session: next, keyOnce: establishment.keyOnce });
        return;
      }
      if (loaded) {
        setPrefs(loaded);
        convergeLocale(loaded);
        // 落地序：仅裸 #/console 且用户拉取期间未点击（hash 仍裸）才走偏好；
        // pinned（跳 artifacts + 展开该看板）> landing_tab > datasets。
        if (isBareConsoleHash()) {
          if (loaded.pinned_dashboard_id != null) {
            suppressTabWriteRef.current = "artifacts";
            setInitialDashboardId(loaded.pinned_dashboard_id);
            window.location.hash = "#/console/artifacts";
          } else if (
            loaded.landing_tab != null &&
            loaded.landing_tab !== "datasets"
          ) {
            // 落地 datasets = 裸入口现状（tab 不变），无需跳转也无需 suppress。
            suppressTabWriteRef.current = loaded.landing_tab as ConsoleTab;
            window.location.hash = `#/console/${loaded.landing_tab}`;
          }
        }
      }
      setPhase({ kind: "ready", session: next, keyOnce: establishment.keyOnce });
    } catch (err) {
      const authErr = err as ConsoleAuthError;
      if (authErr?.status === 401) {
        // 令牌被服务端拒绝：清会话回登录引导（不静默重放坏令牌）。
        clearSession();
        setPhase({ kind: "anonymous" });
        return;
      }
      setPhase({ kind: "sessionError", reason: authErr?.detail });
    }
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap, bootstrapToken]);

  // 静默续期兜底：控制台挂载期间周期检查临期（有 refreshToken 才会实际发请求）。
  useEffect(() => {
    const handle = window.setInterval(() => {
      const session = loadSession();
      const config = configRef.current;
      if (!session || !config || !session.refreshToken) return;
      if (!isExpired(session, RENEW_AHEAD_MS)) return;
      void refreshSession(config, session);
    }, 60_000);
    return () => window.clearInterval(handle);
  }, []);

  // 页签写回（wire-personal-preferences）：ready 后 tab 变化即写 landing_tab
  // （fire-and-forget，失败静默）；首帧不算导航；偏好驱动的程序跳转
  //（置顶→artifacts / landing_tab 跳转）经 suppressTabWriteRef 不回写——
  // 跳转本身不得覆盖用户存的落地习惯。标记按目标页签消费（见 ref 注释：
  // 真实浏览器里首帧可能已是目标页签，按变化消费会漏清、吞下一次用户写回）。
  useEffect(() => {
    if (phase.kind !== "ready") return;
    if (suppressTabWriteRef.current !== null && tab === suppressTabWriteRef.current) {
      // 到达程序跳转目标：清标记且不回写；此后用户切页签照常即写。
      suppressTabWriteRef.current = null;
      lastTabRef.current = tab;
      return;
    }
    const prev = lastTabRef.current;
    lastTabRef.current = tab;
    if (prev == null || prev === tab) return;
    void putPreferences({ landing_tab: tab }).catch(() => {
      /* 写回失败静默：落地偏好下次仍按旧值 */
    });
  }, [tab, phase.kind]);

  // 离开 artifacts 页签即清落地展开锚（重进该页不再自动展开——展开只属于
  // 置顶落地那一次，用户后续的收起意图优先）。按「从 artifacts 离开」判定：
  // 置顶跳转的 ready 首帧仍是 datasets（App 的 hash 态随 hashchange 事件
  // 滞后一拍），此时不得清掉刚设下的锚。
  const prevTabRef = useRef<ConsoleTab | null>(null);
  useEffect(() => {
    if (prevTabRef.current === "artifacts" && tab !== "artifacts") {
      setInitialDashboardId(null);
    }
    prevTabRef.current = tab;
  }, [tab]);

  // 置顶/取消置顶成功（wire-personal-preferences）：产物页回调更新本地
  // 偏好态，默认徽标即时移动到新行（不重拉 GET）。
  const handlePinnedChange = useCallback((id: number | null) => {
    setPrefs((prev) => ({
      ui_locale: prev?.ui_locale ?? null,
      landing_tab: prev?.landing_tab ?? null,
      pinned_dashboard_id: id,
    }));
  }, []);

  const signIn = async () => {
    const config = configRef.current;
    if (!config?.configured) return;
    try {
      await beginLogin(config, window.location.hash.replace(/^#/, "") || "/console");
    } catch (err) {
      setPhase({ kind: "anonymous", signInError: (err as Error).message });
    }
  };

  if (phase.kind === "loading") {
    // 启动期可见加载指示（wire-console-boot-ux）：居中 spinner + 文案——
    // 静默续期挂起（如 iframe 沙箱）不再表现为整页空白。
    return (
      <div
        data-testid="console-boot-loading"
        className="flex min-h-[50vh] flex-col items-center justify-center gap-3"
      >
        <span
          aria-hidden
          className="h-8 w-8 animate-spin rounded-full border-2 border-slate-200 border-t-slate-600"
        />
        <p className="text-sm text-slate-500">{t("console.boot.loading")}</p>
      </div>
    );
  }

  if (phase.kind === "blocked") {
    return (
      <div
        data-testid="console-blocked"
        className="mx-auto max-w-xl rounded-lg border border-amber-200 bg-amber-50/50 p-8"
      >
        <h1 className="text-xl font-semibold">{t("console.auth.blockedTitle")}</h1>
        <p className="mt-3 text-sm text-slate-600">
          {t("console.auth.blockedBody")}
        </p>
      </div>
    );
  }

  if (phase.kind === "configError") {
    return (
      <div
        data-testid="console-config-error"
        className="mx-auto max-w-xl rounded-lg border border-red-200 bg-red-50 p-8"
      >
        <h1 className="text-xl font-semibold">
          {t("console.auth.configErrorTitle")}
        </h1>
        <button
          type="button"
          data-testid="console-config-retry"
          onClick={() => setBootstrapToken((n) => n + 1)}
          className="mt-4 rounded border border-red-300 px-3 py-1 text-xs text-red-700 hover:bg-red-100"
        >
          {t("console.auth.retry")}
        </button>
      </div>
    );
  }

  if (phase.kind === "anonymous") {
    return (
      <div
        data-testid="console-signin"
        className="mx-auto max-w-xl rounded-lg border border-slate-200 p-8"
      >
        <h1 className="text-xl font-semibold">{t("console.auth.anonymousTitle")}</h1>
        <p className="mt-3 text-sm text-slate-600">
          {t("console.auth.anonymousBody")}
        </p>
        {phase.signInError && (
          <p
            data-testid="console-signin-error"
            className="mt-3 break-all font-mono text-[11px] text-red-500"
          >
            {phase.signInError}
          </p>
        )}
        <div className="mt-5 flex items-center gap-3">
          <button
            type="button"
            data-testid="console-signin-button"
            onClick={() => void signIn()}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-700"
          >
            {t("console.auth.signIn")}
          </button>
          <a
            href="#/login"
            className="text-xs text-slate-500 underline hover:text-slate-900"
          >
            {t("console.auth.loginPageLink")}
          </a>
        </div>
      </div>
    );
  }

  if (phase.kind === "sessionError") {
    return (
      <div
        data-testid="console-session-error"
        className="mx-auto max-w-xl rounded-lg border border-red-200 bg-red-50 p-8"
      >
        <h1 className="text-xl font-semibold">
          {t("console.auth.sessionErrorTitle")}
        </h1>
        {phase.reason && (
          <p
            data-testid="console-session-error-detail"
            className="mt-3 break-all text-sm text-red-600"
          >
            {phase.reason}
          </p>
        )}
        <p className="mt-2 text-xs text-slate-500">{t("console.auth.upgradeHint")}</p>
        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            data-testid="console-session-retry"
            onClick={() => setBootstrapToken((n) => n + 1)}
            className="rounded border border-red-300 px-3 py-1 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.auth.retry")}
          </button>
          <a
            href={CONTACT_MAILTO}
            className="text-xs text-slate-500 underline hover:text-slate-900"
          >
            {t("console.usage.upgradeCta")}
          </a>
        </div>
      </div>
    );
  }

  // ready：布局壳 + 子页。key_once 明文仅此一次展示。
  const session = phase.session;
  const customer = session.customer;
  const keyOnce = phase.keyOnce;
  const logout = () => {
    logoutLocal();
    window.location.hash = "#/";
  };

  return (
    <ConsoleLayout
      tab={tab}
      customerName={customer?.name ?? session.identity}
      planCode={customer?.plan_code ?? ""}
      identity={session.identity}
      onLogout={logout}
    >
      {keyOnce != null && (
        <div
          data-testid="console-key-once"
          className="mb-4 rounded-lg border border-emerald-200 bg-emerald-50/60 p-4"
        >
          <h2 className="text-sm font-semibold text-emerald-800">
            {t("console.keyOnce.title")}
          </h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code className="break-all rounded bg-white px-2 py-1 font-mono text-xs text-slate-800">
              {keyOnce}
            </code>
            <button
              type="button"
              data-testid="console-key-copy"
              onClick={() => {
                if (keyOnce != null) void navigator.clipboard?.writeText(keyOnce);
              }}
              className="rounded border border-emerald-300 bg-white px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-100"
            >
              {t("console.keyOnce.copy")}
            </button>
          </div>
          <p className="mt-2 text-xs text-emerald-700">
            {t("console.keyOnce.hint")}
          </p>
        </div>
      )}
      {tab === "workbench" && <WorkbenchPage />}
      {tab === "chat" && <ChatPage planCode={customer?.plan_code ?? ""} />}
      {tab === "artifacts" && (
        <ArtifactsPage
          // 置顶徽标 + 落地展开锚（wire-personal-preferences）
          pinnedDashboardId={prefs?.pinned_dashboard_id ?? null}
          onPinnedChange={handlePinnedChange}
          initialDashboardId={initialDashboardId}
        />
      )}
      {/* 内容区层错误边界（wire-console-ux-fixes 任务 2.2）：单页异常只降级
          页签内容（amber 错误卡 + 重试），侧栏/顶栏/密钥条仍在。 */}
      <ErrorBoundary>
        {tab === "datasets" && (
          <DatasetsPage planCode={customer?.plan_code ?? ""} />
        )}
        {tab === "entities" && <EntitiesPage />}
        {tab === "packs" && <PacksPage />}
        {tab === "demands" && <MyDemandsPage />}
        {tab === "usage" && <UsagePage />}
        {tab === "keys" && <KeysPage />}
        {tab === "team" && <TeamPage />}
        {tab === "market" && <MarketPage />}
      </ErrorBoundary>
      {/* AI 助手浮层（wire-assistant-panel）：全页签常驻、一处挂载。只在
          ready 态渲染——未登录/阻塞态没有会话，浮层无从发起请求（对应
          spec 场景「公开面不出现浮层」）。档位传入做「问/做」分轨（D3）。
          chat 页签豁免：那页本身就是助手的整页形态，再叠浮层会出现两套
          输入框（同屏两个对话入口会让人不知道往哪写）。 */}
      {tab !== "chat" && (
        <AssistantPanel tab={tab} planCode={customer?.plan_code ?? ""} />
      )}
    </ConsoleLayout>
  );
}
