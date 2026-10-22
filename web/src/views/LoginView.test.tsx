// 登录页组件测试（wire-self-serve-launch 任务 4.4，console-auth spec）：
// - 配置未下发（configured=false）→ 阻塞态，登录入口不触发认证跳转（不假登录）；
// - 配置可用 → CTA 发起 Logto SPA+PKCE 跳转（S256、回调 /console/callback）；
// - 已有会话 → 展示身份 + 进入控制台 / 登出；配置端点故障 → 如实呈现。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { LoginView } from "./LoginView";
import { installConsoleBackend, seedConsoleSession } from "../console/testing";
import { PKCE_STORAGE_KEY, SESSION_STORAGE_KEY } from "../console/auth";

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("LoginView（登录页 · console-auth）", () => {
  it("configured=false → 阻塞态：无登录 CTA，不发起认证跳转（不假登录）", async () => {
    const backend = installConsoleBackend([], { authConfig: { configured: false } });
    vi.stubGlobal("fetch", backend.fetchImpl);
    const assign = vi.spyOn(window.location, "assign").mockImplementation(() => {});
    render(<LoginView />);

    expect(await screen.findByTestId("login-blocked")).toBeTruthy();
    expect(screen.getByTestId("login-blocked").textContent).toContain(
      "configured=false",
    );
    expect(screen.queryByTestId("login-cta")).toBeNull();
    expect(assign).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(PKCE_STORAGE_KEY)).toBeNull();
  });

  it("配置可用未登录 → CTA 发起 PKCE 登录（S256 + /console/callback 回调）", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    const assign = vi.spyOn(window.location, "assign").mockImplementation(() => {});
    render(<LoginView />);

    fireEvent.click(await screen.findByTestId("login-cta"));
    await vi.waitFor(() => {
      expect(assign).toHaveBeenCalledTimes(1);
    });
    const url = new URL(String(assign.mock.calls[0]![0]));
    expect(url.href).toContain("/oidc/auth");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("client_id")).toBe("test-client");
    expect(url.searchParams.get("redirect_uri")).toMatch(/\/console\/callback$/);
    // PKCE 参数落 sessionStorage（会话级）
    expect(JSON.parse(sessionStorage.getItem(PKCE_STORAGE_KEY)!)).toHaveProperty(
      "verifier",
    );
  });

  it("已有会话 → 展示身份 + 进入控制台 / 登出（登出清会话）", async () => {
    installConsoleBackend([]);
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([]).fetchImpl,
    );
    seedConsoleSession({ sub: "demo-customer", email: "demo@example.com" });
    render(<LoginView />);

    const box = await screen.findByTestId("login-signed-in");
    expect(box.textContent).toContain("demo@example.com");
    expect(screen.getByTestId("login-go-console").getAttribute("href")).toBe(
      "#/console",
    );
    fireEvent.click(screen.getByTestId("login-signout"));
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("auth/config 端点故障 → 配置错误如实呈现（不发起假登录）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { failAuthConfigStatus: 500 }).fetchImpl,
    );
    render(<LoginView />);

    expect(await screen.findByTestId("login-config-error")).toBeTruthy();
    expect(screen.queryByTestId("login-cta")).toBeNull();
  });
});
