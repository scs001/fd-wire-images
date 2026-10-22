// 登录件单测（wire-self-serve-launch 任务 4.4，console-auth spec）：
// 会话存取（sessionStorage、不落 localStorage）、过期判定与静默续期窗口、
// admin 角色判定、PKCE 阻塞态（未配置不发起认证）、state 校验。
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ADMIN_ROLE,
  REFRESH_TIMEOUT_MS,
  SESSION_STORAGE_KEY,
  beginLogin,
  clearSession,
  completeLogin,
  decodeJwtClaims,
  hasAdminRole,
  isExpired,
  loadSession,
  refreshSession,
  saveSession,
  type ConsoleSession,
} from "./auth";

function makeSession(overrides: Partial<ConsoleSession> = {}): ConsoleSession {
  return {
    idToken: "header.test-payload.signature",
    expiresAt: Date.now() + 3600_000,
    identity: "demo@example.com",
    roles: [],
    ...overrides,
  };
}

const CONFIG = {
  configured: true,
  issuer: "https://auth.test/oidc",
  client_id: "test-client",
  scopes: ["openid", "profile", "email", "roles", "offline_access"],
  organization_required: false,
};

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

describe("console/auth（会话存取与令牌卫生）", () => {
  it("save/load/clear：会话仅落 sessionStorage，不落 localStorage", () => {
    const session = makeSession({ roles: ["wire-admin"] });
    saveSession(session);
    expect(loadSession()).toEqual(session);
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).not.toBeNull();
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    clearSession();
    expect(loadSession()).toBeNull();
  });

  it("loadSession：损坏或空载荷如实返回 null（不抛错）", () => {
    sessionStorage.setItem(SESSION_STORAGE_KEY, "{not json");
    expect(loadSession()).toBeNull();
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({ foo: 1 }));
    expect(loadSession()).toBeNull();
  });

  it("isExpired：过期与临期（skew）均判过期——静默续期窗口的判定基础", () => {
    expect(isExpired(makeSession({ expiresAt: Date.now() - 1_000 }))).toBe(true);
    expect(isExpired(makeSession({ expiresAt: Date.now() + 30_000 }))).toBe(true); // < 60s skew
    expect(isExpired(makeSession({ expiresAt: Date.now() + 120_000 }))).toBe(false);
  });

  it("hasAdminRole：仅 roles 含 wire-admin 的会话过角色门禁", () => {
    expect(hasAdminRole(makeSession({ roles: [ADMIN_ROLE] }))).toBe(true);
    expect(hasAdminRole(makeSession({ roles: ["customers"] }))).toBe(false);
    expect(hasAdminRole(makeSession({ roles: [] }))).toBe(false);
    expect(hasAdminRole(null)).toBe(false);
  });

  it("decodeJwtClaims：base64url payload 解码（展示用）；坏令牌返回空对象", () => {
    const token = `aaa.${btoa(JSON.stringify({ sub: "s1", roles: ["wire-admin"] }))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "")}.bbb`;
    const claims = decodeJwtClaims(token);
    expect(claims.sub).toBe("s1");
    expect(claims.roles).toEqual(["wire-admin"]);
    expect(decodeJwtClaims("garbage")).toEqual({});
  });
});

describe("console/auth（PKCE 登录流）", () => {
  it("beginLogin：未配置（configured=false）抛错且绝不跳转——阻塞态不假登录", async () => {
    const assign = vi.spyOn(window.location, "assign").mockImplementation(() => {});
    await expect(
      beginLogin({ ...CONFIG, configured: false }),
    ).rejects.toThrow("login not configured");
    expect(assign).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("wire.console.pkce")).toBeNull();
  });

  it("completeLogin：state 不匹配抛错（不兑换 code）", async () => {
    sessionStorage.setItem(
      "wire.console.pkce",
      JSON.stringify({ state: "expected", verifier: "v", nonce: "n", returnTo: "/console" }),
    );
    await expect(
      completeLogin(CONFIG, new URLSearchParams("code=abc&state=other")),
    ).rejects.toThrow("state mismatch");
  });
});

describe("console/auth（静默续期超时兜底，wire-console-boot-ux）", () => {
  it("token 端点挂起 → REFRESH_TIMEOUT_MS 后按续期失败返回 null（不落会话）", async () => {
    vi.useFakeTimers();
    try {
      // fetch 永不 resolve：模拟 iframe 沙箱等静默续期挂起。
      vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
      let settled = false;
      const pending = refreshSession(
        CONFIG,
        makeSession({ refreshToken: "rt-1" }),
      ).then((result) => {
        settled = true;
        return result;
      });

      // 超时窗口内不提前放弃（仍在等待）。
      await vi.advanceTimersByTimeAsync(REFRESH_TIMEOUT_MS - 1);
      expect(settled).toBe(false);

      // 到点：按续期失败处理（null），且不落 sessionStorage。
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toBeNull();
      expect(settled).toBe(true);
      expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it("超时窗口内 token 正常返回 → 续期成功（race 不影响成功路径）", async () => {
    const idToken = `aaa.${btoa(
      JSON.stringify({ sub: "s2", email: "s2@example.com" }),
    )}.bbb`;
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            id_token: idToken,
            refresh_token: "rt-2",
            expires_in: 3600,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const customer = { id: 7, name: "壹座演示", plan_code: "basic" };
    const renewed = await refreshSession(
      CONFIG,
      makeSession({ refreshToken: "rt-1", customer }),
    );

    // 请求形状：refresh_token 授权 + 合并既有客户信息落会话。
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "https://auth.test/oidc/oidc/token",
    );
    expect(String(fetchMock.mock.calls[0][1]?.body)).toContain(
      "grant_type=refresh_token",
    );
    expect(renewed?.refreshToken).toBe("rt-2");
    expect(renewed?.identity).toBe("s2@example.com");
    expect(renewed?.customer).toEqual(customer);
    expect(loadSession()?.refreshToken).toBe("rt-2");
    vi.unstubAllGlobals();
  });

  it("token 端点报错（非超时）→ 仍按失败返回 null", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("invalid_grant", { status: 400 })),
    );
    await expect(
      refreshSession(CONFIG, makeSession({ refreshToken: "rt-1" })),
    ).resolves.toBeNull();
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    vi.unstubAllGlobals();
  });

  it("无 refreshToken → 直接返回 null（不发请求）", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(refreshSession(CONFIG, makeSession())).resolves.toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
