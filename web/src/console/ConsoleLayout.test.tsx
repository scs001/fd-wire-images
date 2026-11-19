// 控制台外壳分组测试（wire-console-shell）：四组渲染、页签齐全且归属正确、
// 当前项 aria-current、分组标题随语言切换、路由不受分组影响。
// 摘壳 parity（extract-console-shell）：顶栏品牌字+返回门户（href=#/）、
// 语言切换点击即切侧栏文案且写持久化键 wire-web-lang、demands/new 场景
//（tab=demands）活跃页签=我的需求。
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import {
  CONSOLE_NAV_GROUPS,
  CONSOLE_TABS,
  ConsoleLayout,
  tabsInGroup,
  type ConsoleTab,
} from "./ConsoleLayout";

function renderShell(tab: ConsoleTab) {
  return render(
    <ConsoleLayout
      tab={tab}
      customerName="演示客户"
      planCode="basic"
      identity="demo@example.com"
      onLogout={() => {}}
    >
      <div data-testid="page-body" />
    </ConsoleLayout>,
  );
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("ConsoleLayout（侧栏分组）", () => {
  it("渲染四个分组标题", () => {
    renderShell("datasets");

    expect(screen.getByTestId("console-nav-group-overview")).toBeTruthy();
    expect(screen.getByTestId("console-nav-group-data")).toBeTruthy();
    expect(screen.getByTestId("console-nav-group-build")).toBeTruthy();
    expect(screen.getByTestId("console-nav-group-ops")).toBeTruthy();

    expect(
      screen.getByTestId("console-nav-group-label-overview").textContent,
    ).toBe("概览");
    expect(
      screen.getByTestId("console-nav-group-label-data").textContent,
    ).toBe("数据");
    expect(
      screen.getByTestId("console-nav-group-label-build").textContent,
    ).toBe("构建");
    expect(
      screen.getByTestId("console-nav-group-label-ops").textContent,
    ).toBe("运营");
  });

  it("十一个页签齐全，且分组归属符合契约", () => {
    renderShell("datasets");

    for (const item of CONSOLE_TABS) {
      expect(screen.getByTestId(`console-nav-${item.id}`)).toBeTruthy();
    }

    // 契约顺序与归属（spec 的四组定义）
    expect(CONSOLE_NAV_GROUPS.map((group) => group.id)).toEqual([
      "overview",
      "data",
      "build",
      "ops",
    ]);
    expect(tabsInGroup("overview").map((tab) => tab.id)).toEqual(["workbench"]);
    expect(tabsInGroup("data").map((tab) => tab.id)).toEqual([
      "datasets",
      "entities",
      "packs",
      "market",
    ]);
    expect(tabsInGroup("build").map((tab) => tab.id)).toEqual([
      "chat",
      "artifacts",
    ]);
    expect(tabsInGroup("ops").map((tab) => tab.id)).toEqual([
      "demands",
      "usage",
      "keys",
      "team",
    ]);
  });

  it("每个页签恰好出现在一个分组里（无遗漏、无重复）", () => {
    const grouped = CONSOLE_NAV_GROUPS.flatMap((group) =>
      tabsInGroup(group.id).map((tab) => tab.id),
    );
    expect(grouped).toHaveLength(CONSOLE_TABS.length);
    expect(new Set(grouped).size).toBe(CONSOLE_TABS.length);
    expect(new Set(grouped)).toEqual(new Set(CONSOLE_TABS.map((t) => t.id)));
  });

  it("当前页签带 aria-current，其余不带", () => {
    renderShell("artifacts");

    expect(
      screen.getByTestId("console-nav-artifacts").getAttribute("aria-current"),
    ).toBe("page");
    for (const item of CONSOLE_TABS) {
      if (item.id === "artifacts") continue;
      expect(
        screen.getByTestId(`console-nav-${item.id}`).getAttribute("aria-current"),
      ).toBeNull();
    }
  });

  it("分组标题不被标为当前项", () => {
    renderShell("artifacts");
    for (const group of CONSOLE_NAV_GROUPS) {
      expect(
        screen
          .getByTestId(`console-nav-group-label-${group.id}`)
          .getAttribute("aria-current"),
      ).toBeNull();
    }
  });

  it("分组标题随语言切换", () => {
    i18n.changeLanguage("en");
    renderShell("datasets");

    expect(
      screen.getByTestId("console-nav-group-label-overview").textContent,
    ).toBe("Overview");
    expect(
      screen.getByTestId("console-nav-group-label-data").textContent,
    ).toBe("Data");
    expect(
      screen.getByTestId("console-nav-group-label-build").textContent,
    ).toBe("Build");
    expect(
      screen.getByTestId("console-nav-group-label-ops").textContent,
    ).toBe("Operations");
  });

  it("路由不受分组影响：href 保持不变", () => {
    renderShell("market");

    expect(
      screen.getByTestId("console-nav-market").getAttribute("href"),
    ).toBe("#/console/market");
    expect(
      screen.getByTestId("console-nav-workbench").getAttribute("href"),
    ).toBe("#/console/workbench");
    expect(
      screen.getByTestId("console-nav-keys").getAttribute("href"),
    ).toBe("#/console/keys");
  });
});

describe("ConsoleLayout（摘壳 parity：extract-console-shell）", () => {
  it("顶栏品牌字与返回门户链接在场（console-back-portal，href=#/）", () => {
    renderShell("datasets");

    expect(screen.getByTestId("console-back-portal").getAttribute("href")).toBe(
      "#/",
    );
    // 品牌字在顶栏（摘壳后控制台自带品牌承载，比照 AdminLayout 品牌条）。
    expect(screen.getByTestId("console-topbar").textContent).toContain("柏讯");
    expect(screen.getByTestId("console-topbar").textContent).toContain(
      "返回门户",
    );
  });

  it("语言切换点击即切侧栏分组/页签文案，持久化键 wire-web-lang 写入", () => {
    renderShell("datasets");

    // 中文基线
    expect(
      screen.getByTestId("console-nav-group-label-overview").textContent,
    ).toBe("概览");
    expect(screen.getByTestId("console-nav-datasets").textContent).toBe(
      "我的数据",
    );
    // 点击 EN → 即时切换（同一次渲染内分组标题与页签全量跟随）
    fireEvent.click(screen.getByText("EN"));
    expect(
      screen.getByTestId("console-nav-group-label-overview").textContent,
    ).toBe("Overview");
    expect(
      screen.getByTestId("console-nav-group-label-data").textContent,
    ).toBe("Data");
    expect(screen.getByTestId("console-nav-datasets").textContent).toBe(
      "My data",
    );
    // 持久化键写入（与门户同一机制：i18n/index.ts LANGUAGE_KEY）
    expect(localStorage.getItem("wire-web-lang")).toBe("en");
    // 激活态沿用门户 langButton 惯例
    expect(screen.getByText("EN").className).toContain("bg-slate-200");
    // 切回中文同样即时生效
    fireEvent.click(screen.getByText("中"));
    expect(
      screen.getByTestId("console-nav-group-label-overview").textContent,
    ).toBe("概览");
    expect(localStorage.getItem("wire-web-lang")).toBe("zh");
  });

  it("demands/new 场景（App 传 tab=demands）：活跃页签=我的需求（aria-current）", () => {
    renderShell("demands");

    expect(
      screen.getByTestId("console-nav-demands").getAttribute("aria-current"),
    ).toBe("page");
    for (const item of CONSOLE_TABS) {
      if (item.id === "demands") continue;
      expect(
        screen.getByTestId(`console-nav-${item.id}`).getAttribute("aria-current"),
      ).toBeNull();
    }
  });
});
