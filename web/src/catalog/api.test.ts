import { describe, expect, it } from "vitest";
import {
  API_BASE,
  buildCatalogStatsUrl,
  buildIndicatorDetailUrl,
  buildIndicatorsUrl,
  demandNewHref,
  displayName,
  displayDescription,
  fetchCatalogStats,
  fetchIndicators,
  fetchIndicator,
  formatCatalogTotal,
  CatalogApiError,
} from "./api";
import { failingBackend } from "./testing";

describe("buildIndicatorsUrl", () => {
  it("默认基址为同源 /api，空查询无参数", () => {
    expect(API_BASE).toBe("/api");
    expect(buildIndicatorsUrl()).toBe("/api/v1/catalog/indicators");
  });

  it("空值参数不下发，取值参数按序拼接", () => {
    expect(
      buildIndicatorsUrl({ q: "  cpi ", domain: "", status: "", page: 0 }),
    ).toBe("/api/v1/catalog/indicators?q=cpi");
    expect(
      buildIndicatorsUrl({ q: "cpi", domain: "macro", status: "registered", page: 2, page_size: 20 }),
    ).toBe("/api/v1/catalog/indicators?q=cpi&domain=macro&status=registered&page=2&page_size=20");
  });
});

describe("buildIndicatorDetailUrl", () => {
  it("id 做 encodeURIComponent", () => {
    expect(buildIndicatorDetailUrl("ind-001")).toBe("/api/v1/catalog/indicators/ind-001");
    expect(buildIndicatorDetailUrl("a b/c")).toBe("/api/v1/catalog/indicators/a%20b%2Fc");
  });
});

describe("displayName / displayDescription", () => {
  it("中文优先，英文兜底，code 兜底", () => {
    expect(displayName({ name_zh: "物价", name_en: "CPI", code: "CN_CPI" })).toBe("物价");
    expect(displayName({ name_zh: "  ", name_en: "CPI", code: "CN_CPI" })).toBe("CPI");
    expect(displayName({ name_zh: null, name_en: null, code: "CN_CPI" })).toBe("CN_CPI");
  });

  it("描述中文优先英文兜底，全空为 null", () => {
    expect(displayDescription({ description_zh: "desc", description_en: "e" })).toBe("desc");
    expect(displayDescription({ description_zh: null, description_en: "e" })).toBe("e");
    expect(displayDescription({ description_zh: null, description_en: null })).toBeNull();
  });
});

describe("demandNewHref", () => {
  it("导流到需求单占位路由并预填 code", () => {
    expect(demandNewHref("CN_COAL")).toBe("#/console/demands/new?indicator=CN_COAL");
    expect(demandNewHref("A B")).toBe("#/console/demands/new?indicator=A%20B");
  });
});

describe("fetch error mapping", () => {
  it("非 2xx → CatalogApiError 带 status", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = failingBackend(503);
    try {
      await expect(fetchIndicators()).rejects.toMatchObject({
        name: "CatalogApiError",
        status: 503,
      });
      await expect(fetchIndicator("nope")).rejects.toBeInstanceOf(CatalogApiError);
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe("buildCatalogStatsUrl / fetchCatalogStats", () => {
  const originalFetch = globalThis.fetch;

  it("stats URL 与解析（公开匿名计数端点）", async () => {
    expect(buildCatalogStatsUrl()).toBe("/api/v1/catalog/stats");
    const fetchImpl = (async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        registered: 57553,
        verified: 3421,
        by_source: [
          { source_db: "world_bank", registered: 12000, verified: 800 },
        ],
      }),
    })) as unknown as typeof fetch;
    globalThis.fetch = fetchImpl;
    try {
      const stats = await fetchCatalogStats();
      expect(stats.registered).toBe(57553);
      expect(stats.verified).toBe(3421);
      expect(stats.by_source[0]?.source_db).toBe("world_bank");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("stats 非法 JSON → CatalogApiError", async () => {
    const fetchImpl = (async () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw new Error("boom");
      },
    })) as unknown as typeof fetch;
    globalThis.fetch = fetchImpl;
    try {
      await expect(fetchCatalogStats()).rejects.toBeInstanceOf(CatalogApiError);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

describe("formatCatalogTotal（首页数字：千位向下取整）", () => {
  it("57,553 → 57,000（千位分隔符展示）", () => {
    expect(formatCatalogTotal(57553)).toBe("57,000");
  });

  it("恰为千整倍数与边界", () => {
    expect(formatCatalogTotal(1000)).toBe("1,000");
    expect(formatCatalogTotal(999999)).toBe("999,000");
  });

  it("不足 1,000 / 非法值 → 空串（调用方回退不带数字文案）", () => {
    expect(formatCatalogTotal(999)).toBe("");
    expect(formatCatalogTotal(1)).toBe("");
    expect(formatCatalogTotal(0)).toBe("");
    expect(formatCatalogTotal(-5)).toBe("");
    expect(formatCatalogTotal(Number.NaN)).toBe("");
  });
});
