// 客户管理页组件测试（任务 7.1）：列表渲染、开户调用形状（X-Admin-Token
// 头 + 请求体）、鉴权失败态（403 fail-closed / 401）、空态。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { CustomersPage } from "./CustomersPage";
import { installAdminBackend, seedAdminSession, DEFAULT_PLANS } from "./testing";
import type { AdminCustomer } from "./api";

const TOKEN = "test-admin-token";

const CUSTOMERS: AdminCustomer[] = [
  {
    id: 1,
    name: "上海壹座",
    sub2api_user_id: "acme-caller",
    plan_id: 1,
    plan_code: "starter",
    plan_name: "起步版",
    status: "active",
    created_at: "2026-10-01T08:00:00",
  },
  {
    id: 2,
    name: "萬星數據",
    sub2api_user_id: null,
    plan_id: 3,
    plan_code: "enterprise",
    plan_name: "旗舰版",
    status: "suspended",
    created_at: "2026-10-02T08:00:00",
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

describe("CustomersPage（客户管理）", () => {
  it("列表渲染：名称/计量身份/档位/状态，sub2api 后续任务如实标注", async () => {
    const backend = installAdminBackend({ customers: CUSTOMERS, bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CustomersPage />);

    const rows = await screen.findAllByTestId("admin-customer-row");
    expect(rows.length).toBe(2);
    expect(rows[0]!.textContent).toContain("上海壹座");
    expect(rows[0]!.textContent).toContain("acme-caller");
    expect(rows[1]!.textContent).toContain("萬星數據");
    // 档位下拉取自 /internal/admin/plans（三档 seed）
    const planSelect = screen.getAllByTestId("admin-customer-plan-select")[0] as HTMLSelectElement;
    expect(Array.from(planSelect.options).map((o) => o.value)).toEqual([
      "starter",
      "professional",
      "enterprise",
    ]);
    // 开户仅本地关联的如实标注
    expect(screen.getByTestId("admin-customers-sub2api-note").textContent).toContain(
      "sub2api",
    );
    // 请求带鉴权头
    expect(backend.calls.length).toBeGreaterThanOrEqual(2);
    expect(backend.calls[0]!.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  it("开户：调用形状（POST /internal/admin/customers，名称+档位+计量身份）并刷新列表", async () => {
    const backend = installAdminBackend({ customers: [], bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CustomersPage />);
    await screen.findByTestId("admin-customers-empty");

    fireEvent.change(screen.getByTestId("new-customer-name"), {
      target: { value: "新客户" },
    });
    fireEvent.change(screen.getByTestId("new-customer-plan"), {
      target: { value: "professional" },
    });
    fireEvent.change(screen.getByTestId("new-customer-caller"), {
      target: { value: "new-caller" },
    });
    fireEvent.click(screen.getByTestId("new-customer-submit"));

    const rows = await screen.findAllByTestId("admin-customer-row");
    expect(rows.length).toBe(1);
    expect(rows[0]!.textContent).toContain("新客户");

    const createCall = backend.calls.find((c) => c.method === "POST")!;
    expect(createCall.url.pathname).toBe("/internal/admin/customers");
    expect(createCall.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
    expect(createCall.body).toEqual({
      name: "新客户",
      plan_code: "professional",
      sub2api_user_id: "new-caller",
    });
  });

  it("鉴权失败态：403（服务端未配置 fail-closed）与 401 都如实呈现并支持重试", async () => {
    const forbidden = installAdminBackend({ forceStatus: 403 });
    vi.stubGlobal("fetch", forbidden.fetchImpl);
    render(<CustomersPage />);
    const box = await screen.findByTestId("admin-auth-error");
    expect(box.textContent).toContain("鉴权失败");
    expect(box.textContent).toContain("forced 403");
    expect(screen.queryByTestId("admin-customer-table")).toBeNull();

    cleanup();
    const unauthorized = installAdminBackend({ forceStatus: 401 });
    vi.stubGlobal("fetch", unauthorized.fetchImpl);
    render(<CustomersPage />);
    expect(await screen.findByTestId("admin-auth-error")).toBeTruthy();
  });

  it("网络错误走通用错误态（非鉴权）", async () => {
    const broken = installAdminBackend({ forceStatus: 500 });
    vi.stubGlobal("fetch", broken.fetchImpl);
    render(<CustomersPage />);
    expect(await screen.findByTestId("admin-error")).toBeTruthy();
  });

  // ── wire-console-ux-fixes 任务 4.1：行内档位下拉「活跃档 ∪ 当前档」+ 停售档置灰 ──
  const RETIRED = "legacy";
  const PLANS_WITH_RETIRED = [
    ...DEFAULT_PLANS,
    {
      id: 9,
      code: RETIRED,
      name: "创始档",
      monthly_price_micro: 99_000_000,
      monthly_price_yuan: "¥99",
      included_lines: 50_000,
      overage_unit_price_micro: 8_000,
      is_active: false,
    },
  ];
  /** 当前档=停售旧档的客户（下拉必须保留它的当前档）。 */
  const LEGACY_CUSTOMER: AdminCustomer = {
    id: 3,
    name: "创始客户",
    sub2api_user_id: "legacy-caller",
    plan_id: 9,
    plan_code: RETIRED,
    plan_name: "创始档",
    status: "active",
    created_at: "2026-10-03T08:00:00",
  };

  it("停售档在下拉中置灰（disabled + 「（已停用）」后缀），活跃档原样", async () => {
    installAdminBackendAndMount({
      customers: [LEGACY_CUSTOMER],
      plans: PLANS_WITH_RETIRED,
      bearerToken: TOKEN,
    });

    const select = (await screen.findByTestId(
      "admin-customer-plan-select",
    )) as HTMLSelectElement;
    const byValue = Object.fromEntries(
      Array.from(select.options).map((o) => [o.value, o]),
    );
    const legacy = byValue[RETIRED]!;
    expect(legacy.disabled).toBe(true);
    expect(legacy.textContent).toContain("创始档");
    expect(legacy.textContent).toContain("已停用");
    // 活跃三档不置灰、不带后缀
    for (const code of ["starter", "professional", "enterprise"]) {
      expect(byValue[code]!.disabled).toBe(false);
      expect(byValue[code]!.textContent).not.toContain("已停用");
    }
  });

  it("当前档=停售旧档的客户：其当前档仍在下拉中（不过滤丢失、选中值不变）", async () => {
    installAdminBackendAndMount({
      customers: [LEGACY_CUSTOMER],
      plans: PLANS_WITH_RETIRED,
      bearerToken: TOKEN,
    });

    const select = (await screen.findByTestId(
      "admin-customer-plan-select",
    )) as HTMLSelectElement;
    // 停售当前档被并进选项集（活跃档 3 + 当前档 1 = 4 项，含 legacy）
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toContain(RETIRED);
    expect(values).toEqual([
      "starter",
      "professional",
      "enterprise",
      RETIRED,
    ]);
    // 选中值恒为 customer.plan_code（不因过滤/重排漂移）
    expect(select.value).toBe(RETIRED);
  });

  it("切到活跃档：PATCH 带新 code（对停售持有者改档照常工作）", async () => {
    const backend = installAdminBackend({
      customers: [LEGACY_CUSTOMER],
      plans: PLANS_WITH_RETIRED,
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CustomersPage />);

    const select = (await screen.findByTestId(
      "admin-customer-plan-select",
    )) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "professional" } });

    // PATCH 载荷带新档位（改档请求形状是本次任务的验收点；happy-dom 对
    // 「初始选中项为 disabled option」的受控 select 有 selected 属性渲染
    // 缺陷，DOM value 回读不可靠，故断言网络载荷与刷新请求）。
    await waitFor(() => {
      const patch = backend.calls.find(
        (c) =>
          c.method === "PATCH" &&
          c.url.pathname === "/internal/admin/customers/3",
      );
      expect(patch).toBeTruthy();
      expect(patch!.body).toEqual({ plan_code: "professional" });
    });
    // 改档后照常重拉列表（customers + plans）
    await waitFor(() => {
      const gets = backend.calls.filter(
        (c) => c.method === "GET" && c.url.pathname === "/internal/admin/customers",
      );
      expect(gets.length).toBeGreaterThanOrEqual(2);
    });
  });

  // ── wire-console-ux-fixes 任务 4.2：新建表单取消写死默认档 ──
  it("新建表单默认不选档：占位项在场、提交按钮禁用（防手滑按默认档开户）", async () => {
    const backend = installAdminBackend({ customers: [], bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CustomersPage />);
    await screen.findByTestId("admin-customers-empty");

    const planSelect = screen.getByTestId("new-customer-plan") as HTMLSelectElement;
    expect(planSelect.value).toBe("");
    // 占位项（value="" + disabled，选中态不可再选回）
    const placeholder = planSelect.options[0]!;
    expect(placeholder.value).toBe("");
    expect(placeholder.disabled).toBe(true);
    expect(placeholder.textContent).toBe("请选择档位");
    // 新建下拉只列活跃档（停售档不进新单）
    expect(Array.from(planSelect.options).map((o) => o.value)).toEqual([
      "",
      "starter",
      "professional",
      "enterprise",
    ]);

    // 名称已填但未选档 → 提交按钮仍禁用
    fireEvent.change(screen.getByTestId("new-customer-name"), {
      target: { value: "新客户" },
    });
    expect((screen.getByTestId("new-customer-submit") as HTMLButtonElement).disabled).toBe(true);
    // 防线兜底：即便强行点击也不发任何 POST
    fireEvent.click(screen.getByTestId("new-customer-submit"));
    expect(backend.calls.filter((c) => c.method === "POST")).toEqual([]);
  });

  it("新建表单选择档位后可提交（选档解锁提交按钮）", async () => {
    const backend = installAdminBackend({ customers: [], bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<CustomersPage />);
    await screen.findByTestId("admin-customers-empty");

    fireEvent.change(screen.getByTestId("new-customer-name"), {
      target: { value: "新客户" },
    });
    fireEvent.change(screen.getByTestId("new-customer-plan"), {
      target: { value: "starter" },
    });
    expect((screen.getByTestId("new-customer-submit") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId("new-customer-submit"));

    const createCall = await waitFor(() =>
      backend.calls.find((c) => c.method === "POST"),
    );
    expect((createCall!.body as { plan_code?: string }).plan_code).toBe("starter");
  });
});

/** 便捷装具：装假后端 + 打桩 + 挂载（断言面统一从组件查）。 */
function installAdminBackendAndMount(
  options: Parameters<typeof installAdminBackend>[0],
): ReturnType<typeof installAdminBackend> {
  const backend = installAdminBackend(options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  render(<CustomersPage />);
  return backend;
}
