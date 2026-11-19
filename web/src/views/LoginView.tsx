// 登录页（#/login，wire-self-serve-launch 任务 4.4）：死按钮激活为真登录。
// - 登录参数经 /api/console/auth/config 运行时下发；未配置（configured=false）
//   呈阻塞态，登录入口不触发认证跳转（console-auth spec 场景「未配置阻塞」）。
// - 登录动作 = Logto SPA 授权码 + PKCE 跳转（beginLogin，回调 /console/callback）。
// - 已有会话：展示登录身份 + 进入控制台 / 登出，不重复发起认证。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  beginLogin,
  fetchAuthConfig,
  isExpired,
  loadSession,
  logoutLocal,
  type ConsoleAuthConfig,
  type ConsoleSession,
} from "../console/auth";

export function LoginView() {
  const { t } = useTranslation();
  const [config, setConfig] = useState<ConsoleAuthConfig | null>(null);
  const [configFailed, setConfigFailed] = useState(false);
  const [session, setSession] = useState<ConsoleSession | null>(() =>
    loadSession(),
  );
  const [beginError, setBeginError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAuthConfig()
      .then((value) => {
        if (!cancelled) setConfig(value);
      })
      .catch(() => {
        if (!cancelled) setConfigFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const signedIn = session != null && !isExpired(session);

  const signIn = async () => {
    if (!config?.configured) return; // 阻塞态：绝不发起假登录
    setBeginError(null);
    try {
      await beginLogin(config, "/console");
    } catch (err) {
      setBeginError((err as Error).message);
    }
  };

  return (
    <div className="max-w-sm mx-auto py-16">
      <div
        data-testid={
          config && !config.configured
            ? "login-blocked"
            : signedIn
              ? "login-signed-in"
              : "login-ready"
        }
        className="rounded-lg border border-slate-200 p-8 text-center"
      >
        <h1 className="text-xl font-semibold">{t("login.title")}</h1>

        {configFailed && (
          <div data-testid="login-config-error" className="mt-4">
            <p className="text-sm text-red-700">{t("login.errorTitle")}</p>
            <p className="mt-1 text-xs text-slate-500">{t("login.errorHint")}</p>
          </div>
        )}

        {!configFailed && config == null && (
          <p className="mt-3 text-sm text-slate-400">{t("login.loading")}</p>
        )}

        {config && !config.configured && (
          <>
            <p className="mt-3 text-sm text-slate-500">{t("login.blockedBody")}</p>
            <p className="mt-1 text-xs text-amber-600">{t("login.blockedHint")}</p>
          </>
        )}

        {config?.configured && !signedIn && (
          <>
            <p className="mt-3 text-sm text-slate-500">{t("login.subtitle")}</p>
            {beginError && (
              <p
                data-testid="login-begin-error"
                className="mt-2 break-all font-mono text-[11px] text-red-500"
              >
                {beginError}
              </p>
            )}
            <button
              type="button"
              data-testid="login-cta"
              onClick={() => void signIn()}
              className="mt-6 w-full rounded-md bg-slate-900 text-white px-4 py-2 text-sm hover:bg-slate-700 transition-colors"
            >
              {t("login.cta")}
            </button>
          </>
        )}

        {signedIn && (
          <>
            <p className="mt-3 text-sm text-slate-500">
              {t("login.signedInAs", { identity: session!.identity })}
            </p>
            <div className="mt-6 flex items-center justify-center gap-3">
              <a
                data-testid="login-go-console"
                href="#/console"
                className="rounded-md bg-slate-900 text-white px-4 py-2 text-sm hover:bg-slate-700 transition-colors"
              >
                {t("login.goConsole")}
              </a>
              <button
                type="button"
                data-testid="login-signout"
                onClick={() => {
                  logoutLocal();
                  setSession(null);
                }}
                className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 transition-colors"
              >
                {t("login.signOut")}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
