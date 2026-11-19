// 用量页组件测试（wire-self-serve-launch 任务 6.2，usage-transparency spec）：
// - 真实数据渲染：GET /api/console/usage 的已用/含额/余量/档位/账期；
// - 余量 <20% warning；耗尽态明示「额度用完、次月恢复」+ 升档引导
//   （与「余额不足」语义区分）；「上线中」占位不再出现。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { UsagePage } from "./UsagePage";
import { installConsoleBackend, seedConsoleSession } from "./testing";

type UsageFixture = {
  period: string;
  plan_code: string;
  included_lines: number;
  used_lines: number;
  remaining_lines: number;
};

function install(usage?: UsageFixture, failUsageStatus?: number) {
  const backend = installConsoleBackend([], { usage, failUsageStatus });
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  sessionStorage.clear();
  // 用量端点是登录受保护面：种子登录会话（Bearer 由 api 客户端自动附）
  seedConsoleSession({ sub: "demo-customer" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("UsagePage（用量页正式化 · usage-transparency）", () => {
  it("真实数据渲染：账期/档位/已用/含额/余量（千分位），无警告无耗尽", async () => {
    install({
      period: "2026-10",
      plan_code: "free",
      included_lines: 1000,
      used_lines: 123,
      remaining_lines: 877,
    });
    render(<UsagePage />);

    expect(await screen.findByTestId("usage-page")).toBeTruthy();
    expect(screen.getByTestId("usage-card-period").textContent).toContain("2026-10");
    // 档位名按 code 取 i18n（free → 免费版）
    expect(screen.getByTestId("usage-card-plan").textContent).toContain("免费版");
    expect(screen.getByTestId("usage-card-used").textContent).toContain("123");
    expect(screen.getByTestId("usage-card-included").textContent).toContain("1,000");
    expect(screen.getByTestId("usage-card-remaining").textContent).toContain("877");
    expect(screen.queryByTestId("usage-low-banner")).toBeNull();
    expect(screen.queryByTestId("usage-exhausted-banner")).toBeNull();
  });

  it("余量 <20% → warning 提示（未耗尽）", async () => {
    install({
      period: "2026-10",
      plan_code: "basic",
      included_lines: 100000,
      used_lines: 99_950,
      remaining_lines: 50,
    });
    render(<UsagePage />);

    expect(await screen.findByTestId("usage-low-banner")).toBeTruthy();
    expect(screen.getByTestId("usage-low-banner").textContent).toContain("20%");
    expect(screen.queryByTestId("usage-exhausted-banner")).toBeNull();
  });

  it("耗尽态：明示「额度用完、次月恢复」+ 升档引导（mailto），与余额不足语义区分", async () => {
    install({
      period: "2026-10",
      plan_code: "free",
      included_lines: 1000,
      used_lines: 1000,
      remaining_lines: 0,
    });
    render(<UsagePage />);

    const banner = await screen.findByTestId("usage-exhausted-banner");
    // 耗尽语义 ≠ 余额不足：明示月度额度恢复（次月），不出现「余额」字样口径
    expect(banner.textContent).toContain("额度已用完");
    expect(banner.textContent).toContain("次月");
    expect(banner.textContent).not.toContain("余额");
    // 升档引导 CTA = 联系开通（mailto）
    const cta = screen.getByTestId("usage-upgrade-cta");
    expect(cta.getAttribute("href")).toMatch(/^mailto:/);
    expect(screen.queryByTestId("usage-low-banner")).toBeNull();
  });

  it("端点失败 → 错误态如实呈现 + 重试按钮（不伪造数字）", async () => {
    const backend = install(undefined, 500);
    render(<UsagePage />);

    expect(await screen.findByTestId("usage-error")).toBeTruthy();
    expect(screen.queryByTestId("usage-card-remaining")).toBeNull();
    expect(screen.getByTestId("usage-retry")).toBeTruthy();
    expect(backend.calls.some((c) => c.url.pathname === "/api/console/usage")).toBe(
      true,
    );
  });
});
