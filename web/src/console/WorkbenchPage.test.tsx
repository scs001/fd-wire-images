// 「工作台」页测试（wire-personal-workbench 任务 2.4）：
// - 聚合渲染：六段卡片各段计数与最近条目（计数一律来自响应，不虚构）；
// - 空 payload：各段空态 + 上手 CTA 深链（上传 CSV / 浏览源包 / 去 AI 构建）；
// - 免费档配额条：free_quota 在场渲染（含触限内联升档）、付费档 null 不渲染；
// - 壹座入口紧凑卡：与 ChatPage 引导卡同链接语义，且无任何测试账号明文
//  （console-ai-entry 红线）；
// - 最近数据集行可点且 href 指向 #/console/datasets；
// - 错误态如实呈现 + 重试恢复；
// - 导航接线：workbench 页签侧栏首位、hash 路由解析、landing=workbench
//   程序跳转不回写（沿用既有 suppress 测试范式）。
// fetch 一律走 testing.ts 的内存假后端（零网络）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { WorkbenchPage } from "./WorkbenchPage";
import { ConsoleLayout } from "./ConsoleLayout";
import { ConsoleView } from "../views/ConsoleView";
import { App, parseHash } from "../App";
import {
  installConsoleBackend,
  seedConsoleSession,
} from "./testing";
import type { WorkbenchSummary } from "./api";

/** 有数据的聚合种子：各段计数可逐一断言（避免与空态/其他段混淆的数字）。 */
const SEEDED_WORKBENCH: WorkbenchSummary = {
  datasets: {
    total: 7,
    api_tier: 5,
    cell_tier: 2,
    recent: [
      {
        id: "ds-1",
        name: "门店销量",
        storage_tier: "api",
        row_count: 120,
        size_bytes: 4096,
        updated_at: "2026-10-11T09:00:00",
      },
      {
        id: "ds-2",
        name: "煤价物化",
        storage_tier: "cell",
        row_count: 9000,
        size_bytes: 8192,
        updated_at: "2026-10-10T09:00:00",
      },
    ],
  },
  free_quota: { used_datasets: 2, max_datasets: 3, max_rows_per_dataset: 10000 },
  packs: {
    subscribed: 2,
    recent: [
      {
        pack_id: "macro-prices",
        name_zh: "宏观价格",
        name_en: "Macro Prices",
        subscribed_at: "2026-10-09T08:00:00",
      },
    ],
  },
  artifacts: {
    active_total: 4,
    by_kind: {
      dashboard: 2,
      composite_indicator: 1,
      research_report: 1,
      alert_rule: 0,
      refresh_schedule: 0,
    },
    recent: [
      {
        id: 11,
        kind: "dashboard",
        title: "宏观仪表盘",
        status: "published",
        updated_at: "2026-10-10T09:00:00",
      },
    ],
  },
  alerts: { alert_rules: 3, firing: 1, events_24h: 5 },
  demands: {
    open: 1,
    latest: {
      code: "DE-20261010-01",
      status: "in_progress",
      updated_at: "2026-10-10T10:00:00",
    },
  },
};

/** 档位未知（max_* 为 null 的 fail-closed 口径）——D4 降级态。 */
const UNKNOWN_QUOTA_WORKBENCH: WorkbenchSummary = {
  ...SEEDED_WORKBENCH,
  free_quota: {
    used_datasets: 0,
    max_datasets: null,
    max_rows_per_dataset: null,
  } as unknown as WorkbenchSummary["free_quota"],
};

/** 触限的免费档聚合（used=max）：内联升档提示在场、页面不拦截。 */
const EXHAUSTED_WORKBENCH: WorkbenchSummary = {
  ...SEEDED_WORKBENCH,
  free_quota: { used_datasets: 3, max_datasets: 3, max_rows_per_dataset: 10000 },
};

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  seedConsoleSession({ sub: "demo-customer", planCode: "basic" });
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
  window.location.hash = "";
});

describe("WorkbenchPage（聚合渲染）", () => {
  it("六段卡片：各段计数与最近条目如实渲染（不虚构计数）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { workbench: SEEDED_WORKBENCH }).fetchImpl,
    );
    render(<WorkbenchPage />);

    expect(await screen.findByTestId("workbench-root")).toBeTruthy();
    // 我的数据：总数 + api/cell 分层徽标 + 最近列表（行可点、href 对）
    const datasets = screen.getByTestId("workbench-section-datasets");
    expect(datasets.textContent).toContain("共 7 个数据集");
    expect(screen.getByTestId("workbench-tier-api").textContent).toContain("5");
    expect(screen.getByTestId("workbench-tier-cell").textContent).toContain("2");
    const recentRows = screen.getAllByTestId("workbench-recent-dataset");
    expect(recentRows.length).toBe(2);
    expect(recentRows[0]!.getAttribute("href")).toBe("#/console/datasets");
    expect(recentRows[0]!.textContent).toContain("门店销量");
    const tierBadges = screen.getAllByTestId("workbench-recent-dataset-tier");
    expect(tierBadges[0]!.getAttribute("data-tier")).toBe("api");
    expect(tierBadges[1]!.getAttribute("data-tier")).toBe("cell");
    expect(tierBadges[0]!.textContent).toBe("轻量存储");
    // 我的源包：订阅计数 + 最近订阅名称（zh 会话取 name_zh）
    const packs = screen.getByTestId("workbench-section-packs");
    expect(packs.textContent).toContain("已订阅 2 个源包");
    const packRow = screen.getByTestId("workbench-recent-pack");
    expect(packRow.textContent).toContain("宏观价格");
    expect(packRow.getAttribute("href")).toBe("#/console/packs");
    // 看板与产物：活跃总数 + by_kind 徽标（展示序 = ARTIFACT_KINDS 八类）+ 最近产物
    const artifacts = screen.getByTestId("workbench-section-artifacts");
    expect(artifacts.textContent).toContain("4 个活跃产物");
    const kindBadges = screen.getAllByTestId("workbench-kind-badge");
    // wire-record-view：展示序追加 record-view（api/web 两处一致；旧响应
    // by_kind 缺键时徽标兜底 0）；wire-rich-text-docs 任务 4.4 展示序再追加
    // doc（同「两处一致」纪律）。
    expect(kindBadges.map((b) => b.getAttribute("data-kind"))).toEqual([
      "dashboard",
      "form",
      "composite_indicator",
      "research_report",
      "alert_rule",
      "refresh_schedule",
      "record-view",
      "doc",
    ]);
    expect(kindBadges[0]!.textContent).toContain("2");
    const artifactRow = screen.getByTestId("workbench-recent-artifact");
    expect(artifactRow.textContent).toContain("宏观仪表盘");
    expect(
      screen.getByTestId("workbench-recent-artifact-status").getAttribute(
        "data-status",
      ),
    ).toBe("published");
    expect(artifactRow.getAttribute("href")).toBe("#/console/artifacts");
    // 我的规则：规则数 / 触发中 / 24h 事件三个数字
    expect(screen.getByTestId("workbench-alert-rules").textContent).toContain(
      "3",
    );
    expect(screen.getByTestId("workbench-alert-firing").textContent).toContain(
      "1",
    );
    expect(screen.getByTestId("workbench-alert-events").textContent).toContain(
      "5",
    );
    // 我的需求：进行中计数 + 最新一单（code/status）
    const demands = screen.getByTestId("workbench-section-demands");
    expect(demands.textContent).toContain("进行中 1");
    expect(screen.getByTestId("workbench-demand-latest").textContent).toContain(
      "DE-20261010-01",
    );
    expect(
      screen.getByTestId("workbench-demand-latest-status").textContent,
    ).toContain("处理中");
    // 六段（含壹座卡）都在场
    expect(screen.getByTestId("workbench-section-yizuo")).toBeTruthy();
  });

  it("壹座入口卡：与「AI 构建」引导卡同链接语义，且无任何测试账号明文（console-ai-entry 红线）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { workbench: SEEDED_WORKBENCH }).fetchImpl,
    );
    render(<WorkbenchPage />);
    await screen.findByTestId("workbench-root");

    // 主入口直落 platform.finddatatech.cloud（同 IdP 免中转，同 ChatPage）。
    const demo = screen.getByTestId("workbench-yizuo-demo-cta");
    expect(demo.getAttribute("href")).toBe("https://platform.finddatatech.cloud");
    expect(demo.getAttribute("target")).toBe("_blank");
    expect(demo.getAttribute("rel")).toContain("noopener");
    expect(demo.textContent).toContain("去壹座试用");
    expect(screen.queryByTestId("workbench-yizuo-enter-link")).toBeNull();
    // 红线：卡片文本无测试账号邮箱（platform-test / @）/口令样式字样。
    const text = screen.getByTestId("workbench-section-yizuo").textContent ?? "";
    expect(text).not.toContain("platform-test");
    expect(text).not.toContain("@");
    expect(text).not.toMatch(/密码|口令|password/i);
  });

  it("空 payload：各段空态 + 上手 CTA 深链正确，付费档（free_quota=null）不渲染配额条", async () => {
    // 缺省 workbench = 空聚合（free_quota=null / latest=null / 各段零值）。
    vi.stubGlobal("fetch", installConsoleBackend([]).fetchImpl);
    render(<WorkbenchPage />);

    expect(await screen.findByTestId("workbench-root")).toBeTruthy();
    // 数据集段：空态 + 「上传 CSV」→ #/console/datasets
    const datasetsEmpty = screen.getByTestId("workbench-empty-datasets");
    expect(datasetsEmpty.textContent).toContain("还没有数据集");
    expect(
      screen.getByTestId("workbench-empty-datasets-cta").getAttribute("href"),
    ).toBe("#/console/datasets");
    expect(
      screen.getByTestId("workbench-empty-datasets-cta").textContent,
    ).toContain("上传 CSV");
    // 源包段：空态 + 「浏览源包」→ #/console/packs
    expect(
      screen.getByTestId("workbench-empty-packs-cta").getAttribute("href"),
    ).toBe("#/console/packs");
    expect(
      screen.getByTestId("workbench-empty-packs-cta").textContent,
    ).toContain("浏览源包");
    // 产物段：空态 + 「去 AI 构建」→ #/console/chat
    expect(
      screen.getByTestId("workbench-empty-artifacts-cta").getAttribute("href"),
    ).toBe("#/console/chat");
    expect(
      screen.getByTestId("workbench-empty-artifacts-cta").textContent,
    ).toContain("去 AI 构建");
    // 规则段 / 需求段：空态在场（无专属 CTA，不虚构计数）
    expect(screen.getByTestId("workbench-empty-alerts")).toBeTruthy();
    expect(screen.getByTestId("workbench-empty-demands")).toBeTruthy();
    // 付费档 free_quota=null → 配额条绝不渲染
    expect(screen.queryByTestId("workbench-quota")).toBeNull();
  });

  it("免费档配额条：在场渲染（未触限）；触达上限转内联升档提示而非整页拦截", async () => {
    const backend = installConsoleBackend([], {
      workbench: EXHAUSTED_WORKBENCH,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<WorkbenchPage />);

    // 触限态：amber 内联提示 + mailto 升档 CTA；六段照常渲染（不拦截）。
    const quota = await screen.findByTestId("workbench-quota");
    expect(quota.getAttribute("data-exhausted")).toBe("true");
    expect(quota.textContent).toContain("3/3");
    expect(quota.textContent).toContain("已达免费档上限");
    expect(
      screen.getByTestId("workbench-quota-upgrade").getAttribute("href"),
    ).toMatch(/^mailto:/);
    expect(screen.getByTestId("workbench-section-packs")).toBeTruthy();
    expect(screen.getByTestId("workbench-section-yizuo")).toBeTruthy();
  });

  it("免费档未触限：配额条数值来自响应（已用/上限/单集行数），无升档 CTA", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { workbench: SEEDED_WORKBENCH }).fetchImpl,
    );
    render(<WorkbenchPage />);

    const quota = await screen.findByTestId("workbench-quota");
    expect(quota.getAttribute("data-exhausted")).toBe("false");
    expect(quota.textContent).toContain("2/3");
    expect(quota.textContent).toContain("10,000");
    expect(screen.queryByTestId("workbench-quota-upgrade")).toBeNull();
  });

  it("错误态如实呈现 + 重试恢复（照既有页范式）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { failWorkbenchStatus: 503 }).fetchImpl,
    );
    render(<WorkbenchPage />);

    const error = await screen.findByTestId("workbench-error");
    expect(error.textContent).toContain("工作台加载失败");
    // 重试：换健康后端后六段恢复渲染
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { workbench: SEEDED_WORKBENCH }).fetchImpl,
    );
    fireEvent.click(screen.getByTestId("workbench-retry"));
    expect(await screen.findByTestId("workbench-root")).toBeTruthy();
    expect(screen.getByTestId("workbench-section-datasets").textContent).toContain(
      "共 7 个数据集",
    );
  });
});

describe("WorkbenchPage（导航接线 + 落地偏好）", () => {
  it("侧栏首位为「工作台」页签，href 指向 #/console/workbench", () => {
    render(
      <ConsoleLayout
        tab="workbench"
        customerName="演示客户"
        planCode="basic"
        identity="demo@test"
        onLogout={() => {}}
      >
        <WorkbenchPage />
      </ConsoleLayout>,
    );
    const nav = screen.getByTestId("console-nav");
    const ids = [...nav.querySelectorAll("a")].map((a) =>
      a.getAttribute("data-testid"),
    );
    // 首位断言：workbench 在全部页签之前（其余页签相对序不变）。
    expect(ids[0]).toBe("console-nav-workbench");
    expect(ids.indexOf("console-nav-workbench")).toBeLessThan(
      ids.indexOf("console-nav-chat"),
    );
    expect(
      screen.getByTestId("console-nav-workbench").getAttribute("href"),
    ).toBe("#/console/workbench");
    expect(screen.getByTestId("console-nav-workbench").textContent).toContain(
      "工作台",
    );
  });

  it("parseHash：#/console/workbench 解析为 workbench tab（未知段仍回落 datasets）", () => {
    expect(parseHash("#/console/workbench")).toEqual({
      name: "console",
      tab: "workbench",
    });
    expect(parseHash("#/console/nope")).toEqual({
      name: "console",
      tab: "datasets",
    });
  });

  it("ConsoleView tab=workbench：渲染工作台页，聚合请求带 Bearer", async () => {
    const backend = installConsoleBackend([], {
      workbench: SEEDED_WORKBENCH,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<ConsoleView tab="workbench" />);

    expect(await screen.findByTestId("workbench-root")).toBeTruthy();
    const call = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/workbench",
    );
    expect(call).toBeTruthy();
    expect(call!.method).toBe("GET");
    expect(call!.headers["authorization"]).toMatch(/^Bearer /);
  });

  it("landing_tab=workbench：裸 #/console 程序跳转落工作台，不回写 landing_tab（suppress 范式）", async () => {
    const backend = installConsoleBackend([], {
      preferences: {
        "demo-customer": { ui_locale: "zh", landing_tab: "workbench" },
      },
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession({ sub: "demo-customer" });
    window.location.hash = "#/console";
    render(<App />);

    // 程序跳转：hash 落 #/console/workbench、侧栏高亮 workbench、页面渲染。
    await screen.findByTestId("console-nav");
    await waitFor(() => {
      expect(window.location.hash).toBe("#/console/workbench");
    });
    expect(
      screen
        .getByTestId("console-nav-workbench")
        .className.includes("bg-slate-900"),
    ).toBe(true);
    expect(await screen.findByTestId("workbench-root")).toBeTruthy();
    // 程序跳转不回写：无任何 landing_tab PUT（沿用既有 suppress 断言面）。
    const landingPuts = backend.calls.filter(
      (c) =>
        c.url.pathname === "/api/console/me/preferences" &&
        c.method === "PUT" &&
        (c.body as Record<string, unknown> | undefined)?.landing_tab !==
          undefined,
    );
    expect(landingPuts).toEqual([]);
  });
});

describe("WorkbenchPage 配额条：档位未知 null 降级（D4）", () => {
  it("max_* 为 null → 中性提示行，不崩、不渲染升档引导", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { workbench: UNKNOWN_QUOTA_WORKBENCH })
        .fetchImpl,
    );
    render(<WorkbenchPage />);

    expect(await screen.findByTestId("workbench-root")).toBeTruthy();
    const bar = screen.getByTestId("workbench-quota");
    expect(bar.getAttribute("data-unknown")).toBe("true");
    expect(bar.textContent).toContain("当前档位限额信息不可用");
    expect(screen.queryByTestId("workbench-quota-upgrade")).toBeNull();
  });

  it("限额齐全 → 数值条零漂移（触限 amber + 升档引导在场）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], { workbench: EXHAUSTED_WORKBENCH }).fetchImpl,
    );
    render(<WorkbenchPage />);

    expect(await screen.findByTestId("workbench-root")).toBeTruthy();
    const bar = screen.getByTestId("workbench-quota");
    expect(bar.getAttribute("data-unknown")).toBeNull();
    expect(bar.getAttribute("data-exhausted")).toBe("true");
    expect(bar.textContent).toContain("已用 3/3");
    expect(screen.getByTestId("workbench-quota-upgrade")).toBeTruthy();
  });
});
