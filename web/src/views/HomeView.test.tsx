import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { parseHash } from "../App";
import { HomeView } from "./HomeView";
import { installFakeBackend } from "../catalog/testing";
import { resetCatalogStatsCacheForTests } from "../catalog/useCatalogStats";

const STATS = {
  registered: 57553,
  verified: 3421,
  by_source: [
    { source_db: "world_bank", registered: 12000, verified: 800 },
    { source_db: "yearbook_catalog", registered: 45553, verified: 2621 },
  ],
};

function installStats(stats: typeof STATS | null): void {
  const backend = installFakeBackend([], {}, stats);
  vi.stubGlobal("fetch", backend.fetchImpl);
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  resetCatalogStatsCacheForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HomeView（官网首页 · 柏讯产品叙事）", () => {
  it("hero 与三条价值主张；目录规模数字活取 stats（57,553 → 「57,000+ 指标目录」）", async () => {
    installStats(STATS);
    render(<HomeView />);

    expect(screen.getByText("柏讯 Wire · B2B 数据平台")).toBeTruthy();
    // 千位向下取整：57,553 → 57,000（数字来自 /v1/catalog/stats，非手写）
    expect(await screen.findByText("57,000+ 指标目录")).toBeTruthy();
    expect(screen.getByText("MCP 直连数据")).toBeTruthy();
    // 价值主张三覆盖已上线服务：AI 对话构建 × 我的数据（上传物化/产物）
    expect(screen.getByText("AI 对话构建 × 我的数据")).toBeTruthy();
    const value3Desc = screen.getByText(/AI 对话构建分析产物并即时预览/);
    expect(value3Desc.textContent).toContain("上传");
    expect(value3Desc.textContent).toContain("物化");
    // 控制台已上线的口径（不再有「控制台上线中」过时占位口径）
    expect(value3Desc.textContent).toContain("控制台已上线");
    expect(screen.queryByText(/控制台上线中/)).toBeNull();
    // 五线站位一句（不展开叙事）
    expect(screen.getByText(/壹座之上、萬星闪耀/)).toBeTruthy();
  });

  it("stats 不可用 → 回退不带数字的文案，不显示写死的旧数字", async () => {
    installStats(null); // stats 端点 503
    render(<HomeView />);

    expect(await screen.findByText("指标目录")).toBeTruthy();
    expect(screen.queryByText(/57,000/)).toBeNull();
    expect(screen.queryByText(/\d+\+ 指标目录/)).toBeNull();
    // 其余两条价值主张不受影响
    expect(screen.getByText("MCP 直连数据")).toBeTruthy();
  });

  it("CTA：浏览指标目录 → #/catalog；联系开通 → mailto 占位", () => {
    installStats(null);
    render(<HomeView />);

    expect(
      screen.getByTestId("home-cta-catalog").getAttribute("href"),
    ).toBe("#/catalog");
    expect(
      screen.getByTestId("home-cta-contact").getAttribute("href"),
    ).toMatch(/^mailto:/);
  });

  it("路由：#/ 归首页，首页无需登录即可渲染", () => {
    expect(parseHash("#/")).toEqual({ name: "home" });
    // 未登录环境（无任何凭据存储）直接渲染成功即代表可访问
    installStats(null);
    render(<HomeView />);
    expect(screen.getByTestId("home-cta-catalog")).toBeTruthy();
  });
});
