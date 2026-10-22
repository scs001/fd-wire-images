// 测试专用：目录 API 的内存假后端（q/domain/status 过滤 + 分页，
// 语义对齐 api 仓 CatalogQueryService.search）。
import type { CatalogStats, IndicatorDetail, IndicatorListItem } from "./api";

export type FakeResponse = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
};

export type FetchCall = { url: URL };

function jsonResponse(body: unknown, status = 200): FakeResponse {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

export function makeListItems(
  count: number,
  overrides: Array<Partial<IndicatorListItem>> = [],
): IndicatorListItem[] {
  return Array.from({ length: count }, (_, i) => {
    const n = String(i + 1).padStart(2, "0");
    return {
      indicator_id: `ind-${n}`,
      source_db: "world_bank",
      code: `CODE_${n}`,
      name_zh: `指标 ${n}`,
      name_en: `Indicator ${n}`,
      domain: i % 2 === 0 ? "macro" : "energy",
      frequency: "月度",
      unit: null,
      status: (i % 3 === 0 ? "verified" : "registered") as IndicatorListItem["status"],
      ...overrides[i],
    };
  });
}

/** 安装假后端为 global fetch；返回（可断言的）调用记录。 */
export function installFakeBackend(
  items: IndicatorListItem[],
  details: Record<string, IndicatorDetail> = {},
  stats: CatalogStats | null = null,
): { calls: FetchCall[]; fetchImpl: typeof fetch } {
  const calls: FetchCall[] = [];

  const fetchImpl = ((input: RequestInfo | URL): Promise<FakeResponse> => {
    const url = new URL(String(input), "http://catalog.test");
    calls.push({ url });
    const path = url.pathname;

    if (path === "/api/v1/catalog/stats") {
      if (stats) return Promise.resolve(jsonResponse(stats));
      return Promise.resolve(jsonResponse({ detail: "stats unavailable" }, 503));
    }

    if (path === "/api/v1/catalog/indicators") {
      const q = (url.searchParams.get("q") || "").trim().toLowerCase();
      const domain = (url.searchParams.get("domain") || "").trim();
      const status = (url.searchParams.get("status") || "").trim();
      const page = Math.max(1, Number(url.searchParams.get("page") ?? "1") || 1);
      const pageSize = Math.min(
        100,
        Math.max(1, Number(url.searchParams.get("page_size") ?? "20") || 20),
      );

      let rows = [...items];
      if (q) {
        rows = rows.filter((it) =>
          [it.code, it.name_zh, it.name_en]
            .filter(Boolean)
            .some((f) => String(f).toLowerCase().includes(q)),
        );
      }
      if (domain) rows = rows.filter((it) => it.domain === domain);
      if (status) rows = rows.filter((it) => it.status === status);
      rows.sort(
        (a, b) =>
          String(a.domain ?? "").localeCompare(String(b.domain ?? "")) ||
          a.code.localeCompare(b.code),
      );

      const total = rows.length;
      const paged = rows.slice((page - 1) * pageSize, page * pageSize);
      return Promise.resolve(
        jsonResponse({ items: paged, total, page, page_size: pageSize }),
      );
    }

    const detailMatch = path.match(/^\/api\/v1\/catalog\/indicators\/(.+)$/);
    if (detailMatch) {
      const id = decodeURIComponent(detailMatch[1]!);
      const detail = details[id];
      if (detail) return Promise.resolve(jsonResponse(detail));
      return Promise.resolve(jsonResponse({ detail: "indicator not found" }, 404));
    }

    return Promise.resolve(jsonResponse({ detail: "not found" }, 404));
  }) as unknown as typeof fetch;

  return { calls, fetchImpl };
}

export function failingBackend(status = 500): typeof fetch {
  return (() =>
    Promise.resolve(jsonResponse({ detail: "boom" }, status))) as unknown as typeof fetch;
}
