import { useEffect, useState } from "react";
import { fetchIndicators, type IndicatorListItem } from "./api";

// 域下拉选项：目录 API 无 facets 端点，这里以分页扫描收集 distinct domain
// （page_size=100，封顶 10 页防失控），模块级缓存——一次会话只扫一次。
// 扫描失败不影响主流程：下拉退化为仅「全部」，搜索/筛选仍可用。
const PAGE_SIZE = 100;
const MAX_SCAN_PAGES = 10;

let domainsCache: string[] | null = null;
let inflight: Promise<string[]> | null = null;

function absorbItems(items: IndicatorListItem[], into: Set<string>): void {
  for (const item of items) {
    if (item.domain && item.domain.trim()) into.add(item.domain.trim());
  }
}

function collectDomains(): Promise<string[]> {
  if (domainsCache) return Promise.resolve(domainsCache);
  if (!inflight) {
    inflight = (async () => {
      try {
        const first = await fetchIndicators({ page: 1, page_size: PAGE_SIZE });
        const domains = new Set<string>();
        absorbItems(first.items, domains);
        const totalPages = Math.ceil(first.total / Math.max(1, first.page_size));
        const restPages = Array.from(
          { length: Math.max(0, Math.min(totalPages, MAX_SCAN_PAGES) - 1) },
          (_, k) => k + 2,
        );
        if (restPages.length > 0) {
          const pages = await Promise.all(
            restPages.map((page) =>
              fetchIndicators({ page, page_size: PAGE_SIZE }).catch(() => null),
            ),
          );
          for (const page of pages) {
            if (page) absorbItems(page.items, domains);
          }
        }
        domainsCache = [...domains].sort((a, b) => a.localeCompare(b));
        return domainsCache;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

/** 目录页域下拉选项（""/全部 之外的动态域列表）。 */
export function useDomains(): string[] {
  const [domains, setDomains] = useState<string[]>(domainsCache ?? []);

  useEffect(() => {
    if (domainsCache) {
      setDomains(domainsCache);
      return;
    }
    let alive = true;
    collectDomains()
      .then((ds) => {
        if (alive) setDomains(ds);
      })
      .catch(() => {
        /* 域选项拉不到不阻塞目录主流程 */
      });
    return () => {
      alive = false;
    };
  }, []);

  return domains;
}

/** 仅测试用：清除域选项模块级缓存（保持用例间独立）。 */
export function resetDomainsCacheForTests(): void {
  domainsCache = null;
  inflight = null;
}
