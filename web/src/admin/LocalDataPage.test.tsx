// 客户本地数据只读页测试（wire-customer-local-data 任务 5.1）：
// per-cell 渲染（数据集数/存储用量/连接器健康）、鉴权头（X-Admin-Token +
// X-Internal-Token 同值双带）、spec 红线断言——正常态零按钮（无任何管理操作
// 入口）、403/500 如实呈现、空态。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { LocalDataPage } from "./LocalDataPage";
import { installAdminBackend, seedAdminSession } from "./testing";
import type { CustomerLocalDataOverview } from "./api";

const TOKEN = "test-admin-token";

const OVERVIEW: CustomerLocalDataOverview[] = [
  {
    customer_id: "cust-alpha",
    datasets: 3,
    storage_bytes: 2_147_483_648,
    connectors: [
      {
        id: "conn-alpha-1",
        status: "online",
        last_heartbeat_at: "2026-10-06T09:58:00+00:00",
      },
      {
        id: "conn-alpha-2",
        status: "offline",
        last_heartbeat_at: "2026-10-05T09:58:00+00:00",
      },
    ],
  },
  {
    customer_id: "cust-beta",
    datasets: 0,
    storage_bytes: 0,
    connectors: [],
  },
];

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // admin 双闸（web 半边）：登录会话持 wire-admin 角色，idToken 与假后端
  // 的期望 Bearer 一致（X-Admin-Token 由 nginx 服务端注入，前端不持）。
  seedAdminSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("LocalDataPage（客户本地数据只读视图）", () => {
  it("per-cell 渲染：数据集数/存储用量/连接器状态与最后心跳，请求仅带登录 Bearer（双闸的令牌半边由网关注入）", async () => {
    const backend = installAdminBackend({
      datasetOverviews: OVERVIEW,
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    const { container } = render(<LocalDataPage />);

    const rows = await screen.findAllByTestId("admin-localdata-row");
    expect(rows.length).toBe(2);

    // cell 一：数据集数与存储用量（2147483648 → 2.0 GB）
    expect(
      rows[0]!.querySelector('[data-testid="admin-localdata-customer"]')
        ?.textContent,
    ).toBe("cust-alpha");
    expect(
      rows[0]!.querySelector('[data-testid="admin-localdata-datasets"]')
        ?.textContent,
    ).toBe("3");
    expect(
      rows[0]!.querySelector('[data-testid="admin-localdata-storage"]')
        ?.textContent,
    ).toBe("2.0 GB");
    // 连接器健康：在线 + 离线，最后心跳原样呈现
    const connectors = rows[0]!.querySelectorAll(
      '[data-testid="admin-localdata-connector"]',
    );
    expect(connectors.length).toBe(2);
    const statuses = [...connectors].map(
      (c) =>
        c.querySelector('[data-testid="admin-localdata-connector-status"]')
          ?.textContent,
    );
    expect(statuses).toEqual(["在线", "离线"]);
    expect(
      connectors[0]!.querySelector(
        '[data-testid="admin-localdata-connector-heartbeat"]',
      )?.textContent,
    ).toBe("2026-10-06T09:58:00+00:00");

    // cell 二：零数据集、零存储、无连接器
    expect(
      rows[1]!.querySelector('[data-testid="admin-localdata-storage"]')
        ?.textContent,
    ).toBe("0 B");
    expect(
      rows[1]!.querySelector('[data-testid="admin-localdata-no-connectors"]'),
    ).toBeTruthy();

    // 请求形状：GET /internal/datasets/overview + 同值双鉴权头
    const call = backend.calls[0]!;
    expect(call.method).toBe("GET");
    expect(call.url.pathname).toBe("/internal/datasets/overview");
    // admin 双闸后的 web 侧口径：仅登录 Bearer（X-Admin-Token 由 nginx 注入，
    // 浏览器不再携带 X-Internal-Token）。
    expect(call.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
    expect(call.headers["x-internal-token"]).toBeUndefined();
    expect(call.headers["x-admin-token"]).toBeUndefined();

    // 只读视图归入用量职能：整页无任何按钮（含顶栏外管理入口）
    expect(container.querySelectorAll("button").length).toBe(0);
  });

  it("spec 红线：不存在吊销/删除/白名单修改等任何管理操作入口", async () => {
    const backend = installAdminBackend({
      datasetOverviews: OVERVIEW,
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    const { container } = render(<LocalDataPage />);
    await screen.findAllByTestId("admin-localdata-row");

    // 只读标注在场
    expect(
      screen.getByTestId("admin-localdata-readonly-note").textContent,
    ).toContain("只读");
    // 零按钮、零可点击动作元素
    expect(container.querySelectorAll("button, [role='button']").length).toBe(
      0,
    );
    // 常见管理动作 testid 一律不存在
    for (const forbidden of [
      "revoke",
      "delete",
      "remove",
      "whitelist",
      "connector-revoke",
      "dataset-delete",
      "resume",
      "pairing",
    ]) {
      expect(
        container.querySelector(`[data-testid*="${forbidden}"]`),
      ).toBeNull();
    }
    // 无表单控件（管理操作常借表单承载）
    expect(
      container.querySelectorAll("form, input, select, textarea").length,
    ).toBe(0);
  });

  it("403（fail-closed）/ 500 如实呈现：鉴权失败态带 detail，非鉴权走通用错误态", async () => {
    const forbidden = installAdminBackend({ forceStatus: 403 });
    vi.stubGlobal("fetch", forbidden.fetchImpl);
    const first = render(<LocalDataPage />);
    const box = await screen.findByTestId("admin-auth-error");
    expect(box.textContent).toContain("鉴权失败");
    expect(box.textContent).toContain("forced 403");
    expect(screen.queryByTestId("admin-localdata-table")).toBeNull();
    first.unmount();

    const broken = installAdminBackend({ forceStatus: 500 });
    vi.stubGlobal("fetch", broken.fetchImpl);
    render(<LocalDataPage />);
    expect(await screen.findByTestId("admin-error")).toBeTruthy();
  });

  it("空数据：api 返回空数组时如实呈现空态（不假装有数据）", async () => {
    const backend = installAdminBackend({
      datasetOverviews: [],
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<LocalDataPage />);
    expect(await screen.findByTestId("admin-localdata-empty")).toBeTruthy();
    expect(screen.queryByTestId("admin-localdata-table")).toBeNull();
  });
});
