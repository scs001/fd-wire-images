import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { CatalogDetailView } from "./CatalogDetailView";
import { installFakeBackend } from "../catalog/testing";
import type { IndicatorDetail } from "../catalog/api";

const REGISTERED: IndicatorDetail = {
  indicator_id: "ind-coal",
  source_db: "world_bank",
  code: "CN_COAL",
  name_zh: "煤炭产量",
  name_en: "Coal Output",
  description_zh: "全国原煤产量",
  description_en: "National raw coal output",
  domain: "energy",
  frequency: "周度",
  unit: "万吨",
  time_span_start: "2015-01",
  time_span_end: "2024-06",
  entity_coverage_count: 31,
  sample_value: '{"weekly_tons": 105000}',
  status: "registered",
  synced_at: "2026-10-01T00:10:00Z",
  on_demand: true,
  demand_prefill: { indicator_code: "CN_COAL" },
};

const VERIFIED: IndicatorDetail = {
  ...REGISTERED,
  indicator_id: "ind-cpi",
  code: "CN_CPI_YOY",
  name_zh: "居民消费价格指数同比",
  name_en: "CPI YoY",
  description_zh: "全国居民消费价格同比涨幅",
  description_en: "National CPI year-over-year change",
  domain: "macro",
  frequency: "月度",
  time_span_start: "1990-01",
  time_span_end: "2026-09",
  entity_coverage_count: 420,
  sample_value: '{"yoy_pct": 0.4}',
  status: "verified",
  on_demand: false,
  demand_prefill: null,
};

function installDetails(details: Record<string, IndicatorDetail>) {
  const backend = installFakeBackend([], details);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

beforeEach(() => {
  i18n.changeLanguage("zh");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("CatalogDetailView", () => {
  it("registered：展示全部字段、明确「按需开通」并导流需求单，无任何读取入口", async () => {
    const { calls } = installDetails({ "ind-coal": REGISTERED });
    render(<CatalogDetailView id="ind-coal" />);

    const fields = await screen.findByTestId("detail-fields");
    // 标题中文优先；全部展示字段（字段区整体断言，避免标题/行重复文本歧义）
    expect(screen.getByRole("heading", { name: "煤炭产量" })).toBeTruthy();
    expect(fields.textContent).toContain("全国原煤产量"); // 描述（中文）
    expect(fields.textContent).toContain("Coal Output"); // 名称（英文）
    expect(fields.textContent).toContain("National raw coal output"); // 描述（英文）
    expect(fields.textContent).toContain("周度"); // 频率
    expect(fields.textContent).toContain("2015-01 – 2024-06"); // 时间跨度
    expect(fields.textContent).toContain("31 个实体"); // 实体覆盖
    expect(fields.textContent).toContain("weekly_tons"); // 示例值
    expect(fields.textContent).toContain("同步时间"); // 同步时间
    // 只请求了目录详情端点（页面不请求任何观测数值）
    expect(calls.length).toBe(1);
    expect(calls[0]!.url.pathname).toBe("/api/v1/catalog/indicators/ind-coal");
    // registered 导流
    const cta = screen.getByRole("link", { name: "按需开通（提交需求单）" });
    expect(cta.getAttribute("href")).toBe("#/console/demands/new?indicator=CN_COAL");
    expect(screen.getByTestId("detail-on-demand").textContent).toContain(
      "不提供任何数据读取入口",
    );
    // 不提供读取数据入口
    expect(screen.queryByText("读取数据")).toBeNull();
    expect(screen.queryByTestId("detail-direct-read")).toBeNull();
  });

  it("verified：「读取数据」引导登录，不展示按需开通", async () => {
    installDetails({ "ind-cpi": VERIFIED });
    render(<CatalogDetailView id="ind-cpi" />);

    await screen.findByTestId("detail-direct-read");
    expect(screen.getByRole("heading", { name: "居民消费价格指数同比" })).toBeTruthy();
    const fields = screen.getByTestId("detail-fields");
    expect(fields.textContent).toContain("420 个实体");
    expect(fields.textContent).toContain("1990-01 – 2026-09");
    const read = screen.getByRole("link", { name: "读取数据" });
    expect(read.getAttribute("href")).toBe("#/login");
    expect(screen.queryByTestId("detail-on-demand")).toBeNull();
    expect(screen.queryByText("按需开通（提交需求单）")).toBeNull();
  });

  it("404：指标不存在的明确态", async () => {
    installDetails({});
    render(<CatalogDetailView id="missing" />);
    const notFound = await screen.findByTestId("detail-not-found");
    expect(notFound.textContent).toContain("指标不存在");
  });

  const LEGAL: IndicatorDetail = {
    ...REGISTERED,
    indicator_id: "ind-law",
    code: "law.guide_cases",
    name_zh: "指导性案例库",
    name_en: "Guiding Cases",
    description_zh: "最高法指导案例合集",
    description_en: "SPC guiding cases",
    domain: "法律",
    status: "registered",
    on_demand: true,
    demand_prefill: { indicator_code: "law.guide_cases" },
  };

  it("法律域：保留可见、开通转介识律，不出现需求单/读取入口", async () => {
    installDetails({ "ind-law": LEGAL });
    render(<CatalogDetailView id="ind-law" />);

    const box = await screen.findByTestId("detail-lex-referral");
    const cta = screen.getByRole("link", { name: "前往识律平台开通" });
    expect(cta.getAttribute("href")).toBe("https://lex.finddatatech.cloud");
    expect(cta.getAttribute("target")).toBe("_blank");
    expect(box.textContent).toContain("目录保留可见");
    // 条目本身完整可见（字段区不被转介替换）
    expect(screen.getByTestId("detail-fields").textContent).toContain("指导性案例库");
    // 开通/读取动线均不出现
    expect(screen.queryByTestId("detail-on-demand")).toBeNull();
    expect(screen.queryByTestId("detail-direct-read")).toBeNull();
    expect(screen.queryByText("按需开通（提交需求单）")).toBeNull();
  });

  it("法律域 verified 同样转介（避免「绿色可读」假象）", async () => {
    installDetails({
      "ind-law2": { ...LEGAL, indicator_id: "ind-law2", status: "verified", on_demand: false },
    });
    render(<CatalogDetailView id="ind-law2" />);
    await screen.findByTestId("detail-lex-referral");
    expect(screen.queryByTestId("detail-direct-read")).toBeNull();
  });

  it("双语对等：英文界面呈现等价转介", async () => {
    i18n.changeLanguage("en");
    installDetails({ "ind-law": LEGAL });
    render(<CatalogDetailView id="ind-law" />);
    const box = await screen.findByTestId("detail-lex-referral");
    expect(screen.getByRole("link", { name: "Activate on the Lex platform" })).toBeTruthy();
    expect(box.textContent).toContain("served by the Lex platform");
  });

  it("API 故障：错误态 + 重试恢复", async () => {
    const backend = installFakeBackend([], { "ind-coal": REGISTERED });
    vi.stubGlobal(
      "fetch",
      (() =>
        Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({}),
        })) as unknown as typeof fetch,
    );
    render(<CatalogDetailView id="ind-coal" />);
    expect(await screen.findByTestId("detail-error")).toBeTruthy();

    vi.stubGlobal("fetch", backend.fetchImpl); // 恢复健康后端后重试
    fireEvent.click(screen.getByText("重试"));
    await screen.findAllByText("煤炭产量");
  });
});
