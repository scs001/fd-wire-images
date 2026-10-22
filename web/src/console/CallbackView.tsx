// 登录回调页（/console/callback，wire-self-serve-launch 任务 4.4）。
// Logto 授权码重定向回真实路径（hash 路由壳经 App 的 pathname 检测进入本页）：
// 校验 state → 兑换 code（PKCE verifier）→ 落 sessionStorage 会话 → 清理 URL
// 并跳回 returnTo（默认 #/console）。失败态如实呈现（state 不匹配 / 兑换失败），
// 绝不静默吞掉——由用户主动返回登录重试。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { completeLogin, fetchAuthConfig } from "./auth";

export function CallbackView({
  query,
}: {
  /** 回调查询串（缺省取 window.location.search；测试注入用）。 */
  query?: string;
}) {
  const { t } = useTranslation();
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const config = await fetchAuthConfig();
        if (!config.configured) {
          throw new Error("login not configured on server");
        }
        const { returnTo } = await completeLogin(
          config,
          new URLSearchParams(query ?? window.location.search),
        );
        if (cancelled) return;
        setDone(true);
        // 清掉真实路径（回到 hash 路由），再以 hash 跳转触发壳重渲染。
        try {
          window.history.replaceState(null, "", "/");
        } catch {
          /* replaceState 不可用时直接置 hash */
        }
        window.location.hash = `#${returnTo}`;
      } catch (err) {
        if (!cancelled) {
          setFailed(true);
          setMessage((err as Error).message);
        }
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [query]);

  return (
    <div className="mx-auto max-w-md py-16 text-center">
      {failed ? (
        <div
          data-testid="callback-error"
          className="rounded-lg border border-red-200 bg-red-50 p-8"
        >
          <h1 className="text-lg font-semibold text-red-700">
            {t("console.callback.errorTitle")}
          </h1>
          <p className="mt-2 break-all font-mono text-[11px] text-red-500">
            {message}
          </p>
          <a
            data-testid="callback-back-login"
            href="#/login"
            className="mt-4 inline-block rounded-md border border-red-300 px-3 py-1.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.callback.backToLogin")}
          </a>
        </div>
      ) : (
        <div data-testid="callback-processing" className="text-sm text-slate-500">
          {done
            ? t("console.callback.success")
            : t("console.callback.processing")}
        </div>
      )}
    </div>
  );
}
