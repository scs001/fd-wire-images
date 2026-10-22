import { useEffect, useState } from "react";
import { fetchCatalogStats, type CatalogStats } from "./api";

// 目录计数（/v1/catalog/stats）：首页 banner 与定价页的指标数字活取源。
// 模块级缓存——一次会话只取一次；取不到（网络/服务失败）返回 null，
// 调用方回退到不带数字的文案，绝不显示写死的旧数字。
let statsCache: CatalogStats | null = null;
let inflight: Promise<CatalogStats | null> | null = null;

function loadStats(): Promise<CatalogStats | null> {
  if (statsCache) return Promise.resolve(statsCache);
  if (!inflight) {
    inflight = fetchCatalogStats()
      .then((stats) => {
        statsCache = stats;
        return stats;
      })
      .catch(() => null)
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

/** 目录计数（registered＝登记总量）；不可用时为 null（渲染层如实降级）。 */
export function useCatalogStats(): CatalogStats | null {
  const [stats, setStats] = useState<CatalogStats | null>(statsCache);

  useEffect(() => {
    if (statsCache) {
      setStats(statsCache);
      return;
    }
    let alive = true;
    loadStats().then((s) => {
      if (alive) setStats(s);
    });
    return () => {
      alive = false;
    };
  }, []);

  return stats;
}

/** 仅测试用：清除计数模块级缓存（保持用例间独立）。 */
export function resetCatalogStatsCacheForTests(): void {
  statsCache = null;
  inflight = null;
}
