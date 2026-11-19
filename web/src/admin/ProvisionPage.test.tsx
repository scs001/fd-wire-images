// cell 配给状态页组件测试（add-self-serve-cell-provision 任务 1.3 web 面）：
// 列表新→旧与状态徽标、失败原因可见、空态、鉴权失败如实回显。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { ProvisionPage } from "./ProvisionPage";
import { installAdminBackend, seedAdminSession } from "./testing";
import type { AdminProvisionRequest } from "./api";

const REQUESTS: AdminProvisionRequest[] = [
  {
    id: 3,
    customer_id: "cust-beta",
    state: "failed",
    steps: [
      { step: "provision", ok: true },
      { step: "error", error: "RegistryError: HTTP 401" },
    ],
    error: "RegistryError: registry register -> HTTP 401",
    created_at: "2026-11-06T10:00:00",
  },
  {
    id: 2,
    customer_id: "cust-alpha",
    state: "running",
    steps: [{ step: "provision", ok: true }],
    error: null,
    created_at: "2026-11-06T09:00:00",
  },
  {
    id: 1,
    customer_id: "cust-gamma",
    state: "done",
    steps: [{ step: "registry" }],
    error: null,
    created_at: "2026-11-06T08:00:00",
  },
];

beforeEach(() => {
  i18n.changeLanguage("zh");
  seedAdminSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProvisionPage（后台 cell 配给状态页）", () => {
  it("列表新→旧、状态徽标四态、失败原因可见、请求形状正确", async () => {
    const { fetchImpl, calls } = installAdminBackend({
      provisionRequests: REQUESTS,
    });
    vi.stubGlobal("fetch", fetchImpl);
    render(<ProvisionPage />);

    expect(await screen.findByTestId("admin-provisions-table")).toBeTruthy();
    // 请求形状：limit/offset 分页参数
    const listCall = calls.find(
      (c) => c.url.pathname === "/internal/admin/cell-provision-requests",
    )!;
    expect(listCall.url.searchParams.get("limit")).toBe("50");

    // 新→旧：id=3 在首行；客户名列齐备
    const rows = screen.getAllByTestId("admin-provisions-row");
    expect(rows.length).toBe(3);
    const customers = screen
      .getAllByTestId("admin-provisions-customer")
      .map((n) => n.textContent);
    expect(customers).toEqual(["cust-beta", "cust-alpha", "cust-gamma"]);
    // 状态徽标（四态文案）
    expect(screen.getByTestId("admin-provisions-state-3").textContent).toBe("失败");
    expect(screen.getByTestId("admin-provisions-state-2").textContent).toBe("执行中");
    expect(screen.getByTestId("admin-provisions-state-1").textContent).toBe("已完成");
    // 失败原因可见（首行），成功行呈现 —
    const errors = screen
      .getAllByTestId("admin-provisions-error")
      .map((n) => n.textContent);
    expect(errors[0]).toContain("HTTP 401");
    expect(errors[2]).toBe("—");
  });

  it("空列表 → 空态呈现（无表格）", async () => {
    const { fetchImpl } = installAdminBackend({ provisionRequests: [] });
    vi.stubGlobal("fetch", fetchImpl);
    render(<ProvisionPage />);
    expect(await screen.findByTestId("admin-provisions-empty")).toBeTruthy();
    expect(screen.queryByTestId("admin-provisions-table")).toBeNull();
  });

  it("入队时间呈现（YYYY-MM-DD HH:mm 截断）", async () => {
    const { fetchImpl } = installAdminBackend({ provisionRequests: REQUESTS });
    vi.stubGlobal("fetch", fetchImpl);
    render(<ProvisionPage />);
    await screen.findByTestId("admin-provisions-table");
    const rows = screen.getAllByTestId("admin-provisions-row");
    expect(rows[0]!.textContent).toContain("2026-11-06 10:00");
  });

  it("401 鉴权失败 → AdminErrorBox 如实回显（重试出口在）", async () => {
    const { fetchImpl } = installAdminBackend({ forceStatus: 401 });
    vi.stubGlobal("fetch", fetchImpl);
    render(<ProvisionPage />);
    expect(await screen.findByTestId("admin-auth-error")).toBeTruthy();
    expect(screen.queryByTestId("admin-provisions-table")).toBeNull();
  });
});
