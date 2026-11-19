import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "../i18n";
import i18n, { switchLanguage } from "../i18n";
import { CatalogView } from "./CatalogView";
import { resetDomainsCacheForTests } from "../catalog/useDomains";
import {
  failingBackend,
  installFakeBackend,
  makeListItems,
  type FetchCall,
} from "../catalog/testing";
import type { IndicatorListItem } from "../catalog/api";

const THREE: IndicatorListItem[] = [
  {
    indicator_id: "ind-cpi",
    source_db: "gta",
    code: "CN_CPI_YOY",
    name_zh: "居民消费价格指数同比",
    name_en: "CPI YoY",
    domain: "macro",
    frequency: "月度",
    unit: null,
    status: "verified",
  },
  {
    indicator_id: "ind-pmi",
    source_db: "gta",
    code: "CN_PMI",
    name_zh: "制造业PMI",
    name_en: "Manufacturing PMI",
    domain: "macro",
    frequency: "月度",
    unit: null,
    status: "registered",
  },
  {
    indicator_id: "ind-coal",
    source_db: "world_bank",
    code: "CN_COAL",
    name_zh: null,
    name_en: "Coal Output",
    domain: "energy",
    frequency: "周度",
    unit: null,
    status: "registered",
  },
];

let calls: FetchCall[];

function install(items: IndicatorListItem[]): void {
  const backend = installFakeBackend(items);
  calls = backend.calls;
  vi.stubGlobal("fetch", backend.fetchImpl);
}

beforeEach(() => {
  resetDomainsCacheForTests();
  i18n.changeLanguage("zh");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("CatalogView（公开目录列表）", () => {
  it("渲染列表：中文优先/英文兜底、code、域徽标、状态徽标语义分级", async () => {
    install(THREE);
    render(<CatalogView />);

    await screen.findByText("居民消费价格指数同比");
    // name_zh 缺失 → name_en 兜底
    expect(screen.getByText("Coal Output")).toBeTruthy();
    expect(screen.getByText("CN_CPI_YOY")).toBeTruthy();
    // 状态徽标：verified=绿「可直读」×1，registered=琥珀「按需开通」×2
    const statusTexts = () =>
      screen.getAllByTestId("status-badge").map((el) => el.textContent);
    expect(statusTexts().filter((s) => s?.includes("可直读")).length).toBe(1);
    expect(statusTexts().filter((s) => s?.includes("按需开通")).length).toBe(2);
    // 域徽标
    const domainTexts = screen
      .getAllByTestId("domain-badge")
      .map((el) => el.textContent);
    expect(domainTexts.length).toBe(3);
    expect(domainTexts.filter((s) => s === "energy").length).toBe(1);
    // 行点击 → 详情路由（indicator_id）
    expect(
      screen.getByRole("link", { name: /居民消费价格指数同比/ }).getAttribute("href"),
    ).toBe("#/catalog/ind-cpi");
  });

  it("关键词搜索防抖后带 q 参数；无结果显示空态", async () => {
    install(THREE);
    render(<CatalogView />);
    await screen.findByText("Coal Output");

    fireEvent.change(screen.getByLabelText("搜索"), { target: { value: "cpi" } });
    await waitFor(
      () => expect(calls.some((c) => c.url.searchParams.get("q") === "cpi")).toBe(true),
      { timeout: 2000 },
    );
    await waitFor(() => expect(screen.queryByText("Coal Output")).toBeNull());
    expect(screen.getByText("居民消费价格指数同比")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜索"), { target: { value: "zzz" } });
    const empty = await screen.findByTestId("catalog-empty", {}, { timeout: 2000 });
    expect(empty.textContent).toContain("没有匹配的指标");
  });

  it("status 与 domain 筛选下发参数并收窄结果", async () => {
    install(THREE);
    render(<CatalogView />);
    await screen.findByText("居民消费价格指数同比");

    fireEvent.change(screen.getByLabelText("状态"), { target: { value: "registered" } });
    await waitFor(() =>
      expect(calls.some((c) => c.url.searchParams.get("status") === "registered")).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText("居民消费价格指数同比")).toBeNull());
    expect(screen.getByText("制造业PMI")).toBeTruthy();

    // 域下拉选项由后台数据推导，出现后可选
    await screen.findByRole("option", { name: "energy" }, { timeout: 2000 });
    fireEvent.change(screen.getByLabelText("所属域"), { target: { value: "energy" } });
    await waitFor(() =>
      expect(calls.some((c) => c.url.searchParams.get("domain") === "energy")).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText("制造业PMI")).toBeNull());
    expect(screen.getByText("Coal Output")).toBeTruthy();
  });

  it("分页：下一页/上一页携带 page 参数", async () => {
    install(makeListItems(25));
    const { container } = render(<CatalogView />);

    await screen.findByText("共 25 条 · 第 1 / 2 页");
    expect(container.querySelectorAll("tbody tr").length).toBe(20);

    fireEvent.click(screen.getByText("下一页"));
    await waitFor(() =>
      expect(calls.some((c) => c.url.searchParams.get("page") === "2")).toBe(true),
    );
    await screen.findByText("共 25 条 · 第 2 / 2 页");
    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(5));
    expect(screen.getByText("下一页").hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("上一页").hasAttribute("disabled")).toBe(false);

    fireEvent.click(screen.getByText("上一页"));
    await screen.findByText("共 25 条 · 第 1 / 2 页");
  });

  it("API 不可达显示错误态，重试后恢复", async () => {
    vi.stubGlobal("fetch", failingBackend(503));
    render(<CatalogView />);
    const error = await screen.findByTestId("catalog-error");
    expect(error.textContent).toContain("目录服务暂时不可用");

    install(THREE); // 换成健康后端后重试
    fireEvent.click(screen.getByText("重试"));
    await screen.findByText("居民消费价格指数同比");
    expect(screen.queryByTestId("catalog-error")).toBeNull();
  });

  it("语言切换：状态徽标文案随 i18n 切换", async () => {
    install(THREE);
    render(<CatalogView />);
    await screen.findByText("居民消费价格指数同比");

    const badgeCount = (substr: string) =>
      screen
        .getAllByTestId("status-badge")
        .filter((el) => el.textContent?.includes(substr)).length;

    expect(badgeCount("可直读")).toBe(1);
    act(() => switchLanguage("en"));
    await waitFor(() => expect(badgeCount("Direct read")).toBe(1));
    expect(badgeCount("On demand")).toBe(2);
    expect(screen.getByRole("option", { name: "Direct read" })).toBeTruthy();

    act(() => switchLanguage("zh"));
    await waitFor(() => expect(badgeCount("可直读")).toBe(1));
  });

  it("常驻能力条目：双通道 CTA 新窗口外链；目录数据不可达时照常呈现（非数据驱动）", async () => {
    // 后端 503：指标列表不可达，但能力条目是静态呈现，不依赖目录数据
    vi.stubGlobal("fetch", failingBackend(503));
    render(<CatalogView />);
    await screen.findByTestId("catalog-error");

    const box = screen.getByTestId("daas-analysis-referral");
    expect(box.textContent).toContain("自助分析工作区");
    expect(box.textContent).toContain("取数 → 指标 → 看板 → 研究");

    const market = screen.getByRole("link", { name: "前往壹座市场订阅" });
    expect(market.getAttribute("href")).toBe("https://platform.finddatatech.cloud");
    expect(market.getAttribute("target")).toBe("_blank");
    expect(market.getAttribute("rel")).toContain("noopener");

    const install = screen.getByRole("link", { name: "本地安装 fd-daas-mcp" });
    expect(install.getAttribute("href")).toBe(
      "https://github.com/FindDataTechnology/fd-daas-mcp",
    );
    expect(install.getAttribute("target")).toBe("_blank");
    expect(install.getAttribute("rel")).toContain("noopener");

    // 指标表格未渲染（错误态），能力条目与表格并列而非其数据行
    expect(screen.queryByTestId("catalog-table")).toBeNull();
  });

  it("能力条目不参与搜索/筛选/分页，指标条目行为不变", async () => {
    install(THREE);
    const { container } = render(<CatalogView />);
    await screen.findByText("居民消费价格指数同比");

    const table = screen.getByTestId("catalog-table");
    const box = screen.getByTestId("daas-analysis-referral");
    // 并列而非表格数据行：条目不在指标表格内
    expect(table.contains(box)).toBe(false);
    // 指标表格数据行只有 3 条目录条目
    expect(container.querySelectorAll("tbody tr").length).toBe(3);

    // 搜索收窄到 1 条 → 条目仍在且不受筛选影响
    fireEvent.change(screen.getByLabelText("搜索"), { target: { value: "cpi" } });
    await waitFor(() => expect(screen.queryByText("制造业PMI")).toBeNull(), {
      timeout: 2000,
    });
    expect(screen.getByTestId("daas-analysis-referral")).toBeTruthy();
    expect(container.querySelectorAll("tbody tr").length).toBe(1);

    // 搜索无结果（空态）→ 条目仍在
    fireEvent.change(screen.getByLabelText("搜索"), { target: { value: "zzz" } });
    await screen.findByTestId("catalog-empty", {}, { timeout: 2000 });
    expect(screen.getByTestId("daas-analysis-referral")).toBeTruthy();
  });

  it("双语对等：英文界面标题/说明/两个 CTA 等价呈现", async () => {
    act(() => switchLanguage("en"));
    install(THREE);
    render(<CatalogView />);
    // 指标条目名恒「中文优先英文兜底」（displayName 与语言无关），等待列表就绪
    await screen.findByText("居民消费价格指数同比");

    const box = screen.getByTestId("daas-analysis-referral");
    expect(box.textContent).toContain("Self-service analysis workspace");
    expect(box.textContent).toContain("data access, indicators, dashboards and research");
    expect(
      screen.getByRole("link", { name: "Subscribe on the Base market" }).getAttribute("href"),
    ).toBe("https://platform.finddatatech.cloud");
    expect(
      screen.getByRole("link", { name: "Install fd-daas-mcp locally" }).getAttribute("href"),
    ).toBe("https://github.com/FindDataTechnology/fd-daas-mcp");

    // 切回中文：标题/说明/按钮同步换文案（无未翻译裸段）
    act(() => switchLanguage("zh"));
    await waitFor(() =>
      expect(screen.getByTestId("daas-analysis-referral").textContent).toContain(
        "自助分析工作区",
      ),
    );
    expect(screen.getByRole("link", { name: "前往壹座市场订阅" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "本地安装 fd-daas-mcp" })).toBeTruthy();
  });
});
