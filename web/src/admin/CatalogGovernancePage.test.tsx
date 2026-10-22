// 目录治理分页测试（wire-admin-console-routing 任务 2.3）：镜像规模实测
// verified 3,421 条 / registered 54,132 条——全量渲染即生产下滑白屏的直接
// 成因。本文件钉住「只渲染本页」与翻页契约，以及空态/错误态不被分页打破。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { CatalogGovernancePage } from "./CatalogGovernancePage";
import { installAdminBackend, seedAdminSession } from "./testing";
import type { IndicatorPendingItem } from "./api";

const TOKEN = "test-admin-token";

/** 造 n 条某状态的镜像条目（indicator_id 按字典序，与后端排序一致）。 */
function snapshots(status: string, n: number): IndicatorPendingItem[] {
  return Array.from({ length: n }, (_, i) => ({
    indicator_id: `${status}-${String(i + 1).padStart(4, "0")}`,
    code: `CODE_${status}_${i + 1}`,
    name_zh: `指标 ${status} ${i + 1}`,
    name_en: `Indicator ${status} ${i + 1}`,
    domain: "macro",
    frequency: "monthly",
    status,
    synced_at: "2026-10-04T02:00:00+00:00",
  }));
}

/** 取最后一次列表请求（含查询串），用于断言分页参数。 */
function lastListCall(calls: { method: string; url: URL }[]) {
  return calls.filter((c) => c.url.pathname === "/internal/admin/catalog/pending").pop()!;
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  seedAdminSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("CatalogGovernancePage 分页", () => {
  it("首屏只渲染一页条目，不一次渲染全量", async () => {
    const items = [...snapshots("registered", 250), ...snapshots("verified", 3)];
    const backend = installAdminBackend({ bearerToken: TOKEN, catalogItems: items });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CatalogGovernancePage />);

    const rendered = await screen.findAllByTestId("admin-catalog-item");
    // 首屏 100 条（单页上限），而非 250 条全量
    expect(rendered.length).toBe(100);
    expect(screen.queryByTestId("admin-catalog-item-250")).toBeNull();

    // 请求带 limit/offset，total 为状态匹配总数（非本页条数）
    const call = lastListCall(backend.calls);
    expect(call.url.searchParams.get("limit")).toBe("100");
    expect(call.url.searchParams.get("offset")).toBe("0");
    expect(screen.getByTestId("admin-catalog-pagination-info").textContent).toContain(
      "共 250 条",
    );
  });

  it("翻页：下一页取后续切片，上一页回到前一页且首页禁用", async () => {
    const items = snapshots("registered", 250);
    const backend = installAdminBackend({ bearerToken: TOKEN, catalogItems: items });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CatalogGovernancePage />);
    await screen.findAllByTestId("admin-catalog-item");

    const prev = screen.getByTestId("admin-catalog-prev-page") as HTMLButtonElement;
    const next = screen.getByTestId("admin-catalog-next-page") as HTMLButtonElement;
    // 第 1 页：上一页禁用
    expect(prev.disabled).toBe(true);
    expect(next.disabled).toBe(false);

    fireEvent.click(next);
    await waitFor(() => {
      expect(lastListCall(backend.calls).url.searchParams.get("offset")).toBe("100");
    });
    await waitFor(() => {
      expect(
        screen.getByTestId("admin-catalog-pagination-info").textContent,
      ).toContain("第 2 / 3 页");
    });

    fireEvent.click(screen.getByTestId("admin-catalog-prev-page"));
    await waitFor(() => {
      expect(lastListCall(backend.calls).url.searchParams.get("offset")).toBe("0");
    });
  });

  it("切换镜像状态回到第 1 页（不留在越界页码）", async () => {
    const items = [...snapshots("registered", 250), ...snapshots("verified", 5)];
    const backend = installAdminBackend({ bearerToken: TOKEN, catalogItems: items });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CatalogGovernancePage />);
    await screen.findAllByTestId("admin-catalog-item");

    // 翻到第 2 页（registered 有 3 页）
    fireEvent.click(screen.getByTestId("admin-catalog-next-page"));
    await waitFor(() => {
      expect(lastListCall(backend.calls).url.searchParams.get("offset")).toBe("100");
    });

    // 切到 verified（仅 5 条 1 页）：页码必须回到 1，否则是越界空页
    fireEvent.change(screen.getByTestId("catalog-status-filter"), {
      target: { value: "verified" },
    });
    await waitFor(() => {
      const call = lastListCall(backend.calls);
      expect(call.url.searchParams.get("status")).toBe("verified");
      expect(call.url.searchParams.get("offset")).toBe("0");
    });
    await waitFor(() => {
      expect(screen.getAllByTestId("admin-catalog-item").length).toBe(5);
    });
  });

  it("单页即全部时不渲染翻页控件", async () => {
    const items = snapshots("registered", 3);
    const backend = installAdminBackend({ bearerToken: TOKEN, catalogItems: items });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CatalogGovernancePage />);

    await screen.findAllByTestId("admin-catalog-item");
    expect(screen.getAllByTestId("admin-catalog-item").length).toBe(3);
    expect(screen.queryByTestId("admin-catalog-pagination")).toBeNull();
  });

  it("空态：无匹配条目仍走空态，不因分页而改变", async () => {
    const backend = installAdminBackend({
      bearerToken: TOKEN,
      catalogItems: snapshots("verified", 2), // registered 侧为空
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CatalogGovernancePage />);

    await waitFor(() => {
      expect(screen.getByTestId("admin-catalog-empty")).toBeTruthy();
    });
    expect(screen.queryByTestId("admin-catalog-pagination")).toBeNull();
  });

  it("错误态：列表请求失败照常落错误卡（分页不吞错误）", async () => {
    const backend = installAdminBackend({ bearerToken: TOKEN, forceStatus: 500 });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CatalogGovernancePage />);

    await waitFor(() => {
      expect(screen.getByTestId("admin-error")).toBeTruthy();
    });
    expect(screen.queryByTestId("admin-catalog-pagination")).toBeNull();
  });
});
