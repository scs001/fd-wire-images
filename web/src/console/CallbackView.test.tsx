// 登录回调页测试（wire-self-serve-launch 任务 4.4）：
// 成功流（state 校验 → code 兑换 → 会话落地 → 回 returnTo）与失败态
// （state 不匹配 / IdP error 参数）如实呈现。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { CallbackView } from "./CallbackView";
import { installConsoleBackend, makeIdToken } from "./testing";
import { PKCE_STORAGE_KEY, SESSION_STORAGE_KEY } from "./auth";

const PKCE = {
  state: "st-1",
  verifier: "v".repeat(48),
  nonce: "n-1",
  returnTo: "/console",
};

const TOKEN_RESPONSE = {
  id_token: makeIdToken({ sub: "demo-customer", email: "demo@example.com" }),
  expires_in: 3600,
};

function install(): void {
  const consoleBackend = installConsoleBackend([]);
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    if (url.hostname === "auth.test") {
      // Logto token 端点（POST {issuer}/oidc/token）
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(TOKEN_RESPONSE),
      });
    }
    return consoleBackend.fetchImpl(input, init);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", fetchImpl);
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  sessionStorage.setItem(PKCE_STORAGE_KEY, JSON.stringify(PKCE));
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
  window.location.hash = "";
});

describe("CallbackView（登录回调 /console/callback）", () => {
  it("成功流：state 校验 → code 兑换（带 verifier）→ 会话落地 → 回 returnTo", async () => {
    install();
    render(<CallbackView query="code=abc&state=st-1" />);

    // 等待成功态渲染（processing → success）
    const box = await screen.findByTestId("callback-processing");
    await vi.waitFor(() => {
      expect(box.textContent).toContain("登录成功");
    });
    // 会话已落 sessionStorage（ID token 会话级存储）
    const session = JSON.parse(sessionStorage.getItem(SESSION_STORAGE_KEY)!);
    expect(session.idToken).toBe(TOKEN_RESPONSE.id_token);
    // PKCE 一次性状态已清理
    expect(sessionStorage.getItem(PKCE_STORAGE_KEY)).toBeNull();
    // 回到 hash 路由的 returnTo（真实路径已清理）
    expect(window.location.hash).toBe("#/console");
    expect(window.location.pathname).toBe("/");
  });

  it("state 不匹配 → 失败态如实呈现 + 返回登录入口（不落会话）", async () => {
    install();
    render(<CallbackView query="code=abc&state=wrong" />);

    const error = await screen.findByTestId("callback-error");
    expect(error.textContent).toContain("登录失败");
    expect(screen.getByTestId("callback-back-login").getAttribute("href")).toBe(
      "#/login",
    );
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("IdP error 参数（如 access_denied）→ 失败态如实呈现", async () => {
    install();
    render(<CallbackView query="error=access_denied&error_description=denied" />);

    const error = await screen.findByTestId("callback-error");
    expect(error.textContent).toContain("登录失败");
    expect(error.textContent).toContain("access_denied");
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });
});
