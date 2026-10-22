// 控制台外壳分组测试（wire-console-shell）：四组渲染、页签齐全且归属正确、
// 当前项 aria-current、分组标题随语言切换、路由不受分组影响。
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
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
});

afterEach(() => {
  cleanup();
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
