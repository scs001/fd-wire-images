// 需求单提交表单测试（wire-ux-consolidation 任务 4.1/4.2，自占位转正；
// wire-demand-generation-orchestration 任务 4.1/4.3 追加源地址+公开数据声明）：
// - 空必填逐字段提示（不发请求）：描述/邮箱/源地址/声明勾选；
// - 锚点预填：描述区以「指标 {code}：」开头、客户可编辑、indicator_code 入参；
// - 提交走 POST /api/v1/demands 六入参 + Bearer；成功确认态（1 个工作日承诺
//   + 返回目录/查看我的需求）；
// - 源地址本地预校验仅 ≥1（空行忽略）；URL 形态由服务端 422 兜底；
// - 422 FastAPI 标准错误数组逐条映射中文文案（不透传英文 msg 原文）；
// - 未登录（无会话）提交 → 401 如实回显并引导登录。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { DemandNewView } from "./DemandNewView";
import { installConsoleBackend, seedConsoleSession } from "../console/testing";

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

function fillForm(
  opts: {
    description?: string;
    email?: string;
    company?: string;
    sourceUrls?: string;
    assert?: boolean;
  } = {},
) {
  fireEvent.change(screen.getByTestId("demand-description"), {
    target: { value: opts.description ?? "需要煤炭日度价格指标" },
  });
  fireEvent.change(screen.getByTestId("demand-email"), {
    target: { value: opts.email ?? "buyer@acme.com" },
  });
  if (opts.company !== undefined) {
    fireEvent.change(screen.getByTestId("demand-company"), {
      target: { value: opts.company },
    });
  }
  fireEvent.change(screen.getByTestId("demand-source-urls"), {
    target: { value: opts.sourceUrls ?? "https://data.example.com/prices" },
  });
  // 勾选框默认未勾：单次 click 即勾选（jsdom 激活行为）；assert=false 保持不勾。
  if (opts.assert !== false) {
    fireEvent.click(screen.getByTestId("demand-public-assert"));
  }
}

describe("DemandNewView（需求单提交表单）", () => {
  it("空必填逐字段提示：不发请求，四个错误各自可见", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    fireEvent.click(screen.getByTestId("demand-submit"));
    expect(await screen.findByTestId("demand-description-error")).toBeTruthy();
    expect(screen.getByTestId("demand-email-error")).toBeTruthy();
    expect(screen.getByTestId("demand-source-urls-error")).toBeTruthy();
    expect(screen.getByTestId("demand-public-assert-error")).toBeTruthy();
    // 校验失败不发任何请求
    expect(backend.calls.length).toBe(0);
  });

  it("邮箱格式校验：非空但形态不合法仍拒绝", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    fillForm({ email: "not-an-email" });
    fireEvent.click(screen.getByTestId("demand-submit"));
    expect(await screen.findByTestId("demand-email-error")).toBeTruthy();
    expect(backend.calls.length).toBe(0);
  });

  it("提交走真实端点：六入参形状（源地址多行+空行忽略、声明=true）+ Bearer，成功确认态含 1 个工作日承诺与两入口", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    const session = seedConsoleSession();
    render(<DemandNewView indicator="CN_COAL" />);

    // 锚点预填在描述区开头（任务 4.2），客户在预填后继续补充
    const textarea = screen.getByTestId("demand-description") as HTMLTextAreaElement;
    expect(textarea.value).toBe("指标 CN_COAL：");
    expect(screen.getByTestId("demand-prefilled-code").textContent).toBe("CN_COAL");

    fireEvent.change(textarea, {
      target: { value: `${textarea.value}需要煤炭日度价格指标` },
    });
    fireEvent.change(screen.getByTestId("demand-email"), {
      target: { value: "buyer@acme.com" },
    });
    fireEvent.change(screen.getByTestId("demand-company"), {
      target: { value: "上海壹座" },
    });
    // 多行源地址：两条 URL 中夹空行——空行忽略、逐行 trim
    fireEvent.change(screen.getByTestId("demand-source-urls"), {
      target: {
        value: "https://data.example.com/prices\n\n  https://example.org/reports  \n",
      },
    });
    fireEvent.click(screen.getByTestId("demand-public-assert"));
    fireEvent.click(screen.getByTestId("demand-submit"));

    const success = await screen.findByTestId("demand-success");
    expect(success.textContent).toContain("已收到你的需求单");
    expect(success.textContent).toContain("1 个工作日");
    expect(
      screen.getByTestId("demand-success-view-mine").getAttribute("href"),
    ).toBe("#/console/demands");

    const call = backend.calls.find((c) => c.method === "POST")!;
    expect(call.url.pathname).toBe("/api/v1/demands");
    expect(call.headers["authorization"]).toBe(`Bearer ${session.idToken}`);
    expect(call.body).toEqual({
      description: "指标 CN_COAL：需要煤炭日度价格指标",
      contact_email: "buyer@acme.com",
      source_urls: ["https://data.example.com/prices", "https://example.org/reports"],
      public_data_asserted: true,
      company: "上海壹座",
      indicator_code: "CN_COAL",
    });
  });

  it("无锚点入口（定价页）：不带 indicator_code 入参，描述区不预填", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    expect(screen.queryByTestId("demand-prefilled-code")).toBeNull();
    fillForm();
    fireEvent.click(screen.getByTestId("demand-submit"));
    await screen.findByTestId("demand-success");

    const call = backend.calls.find((c) => c.method === "POST")!;
    expect(call.body).toEqual({
      description: "需要煤炭日度价格指标",
      contact_email: "buyer@acme.com",
      source_urls: ["https://data.example.com/prices"],
      public_data_asserted: true,
    });
  });

  it("未登录提交：401 如实回显并引导登录（不静默假登录）", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    // 不 seed 会话：请求层直接以 401 抛出
    render(<DemandNewView indicator={null} />);

    fillForm();
    fireEvent.click(screen.getByTestId("demand-submit"));

    const gate = await screen.findByTestId("demand-signin-required");
    expect(gate.textContent).toContain("登录");
    expect(screen.queryByTestId("demand-success")).toBeNull();
    expect(screen.getByText("前往登录").getAttribute("href")).toBe("#/login");
  });

  it("服务端 422 校验回显：错误文案如实呈现、可重试", async () => {
    const backend = installConsoleBackend([], { failDemandCreateStatus: 422 });
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    fillForm();
    fireEvent.click(screen.getByTestId("demand-submit"));

    const error = await screen.findByTestId("demand-submit-error");
    expect(error.textContent).toContain("422");
    await waitFor(() => {
      expect((screen.getByTestId("demand-submit") as HTMLButtonElement).disabled).toBe(
        false,
      );
    });
  });

  it("源地址与声明本地预校验：全空行拒、未勾选拒（不发请求）；补齐后通过", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    // 描述/邮箱合法，源地址仅空白行、声明未勾选 → 两处错误，其余不报
    fillForm({ sourceUrls: "\n   \n", assert: false });
    fireEvent.click(screen.getByTestId("demand-submit"));
    expect(await screen.findByTestId("demand-source-urls-error")).toBeTruthy();
    expect(screen.getByTestId("demand-public-assert-error")).toBeTruthy();
    expect(screen.queryByTestId("demand-description-error")).toBeNull();
    expect(screen.queryByTestId("demand-email-error")).toBeNull();
    expect(backend.calls.length).toBe(0);

    // 补一条 URL 并勾选声明 → 提交通过
    fireEvent.change(screen.getByTestId("demand-source-urls"), {
      target: { value: "https://data.example.com/prices" },
    });
    fireEvent.click(screen.getByTestId("demand-public-assert"));
    fireEvent.click(screen.getByTestId("demand-submit"));
    await screen.findByTestId("demand-success");
  });

  it("422 FastAPI 错误数组（冻结形状）：逐条映射中文文案，不透传英文 msg 原文", async () => {
    const backend = installConsoleBackend([], {
      failDemandCreateStatus: 422,
      failDemandCreateDetail: [
        {
          type: "url_scheme",
          loc: ["body", "source_urls", 0],
          msg: "Input should be a valid http(s) URL without embedded credentials",
        },
        {
          type: "greater_than",
          loc: ["body", "public_data_asserted"],
          msg: "Input should be true",
        },
      ],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    fillForm();
    fireEvent.click(screen.getByTestId("demand-submit"));

    const error = await screen.findByTestId("demand-submit-error");
    const items = await screen.findAllByTestId("demand-submit-error-item");
    expect(items.length).toBe(2);
    expect(items[0]!.textContent).toContain("第 1 条源地址不合法");
    expect(items[1]!.textContent).toContain("请勾选公开数据声明");
    // 英文 detail 原文绝不照抄
    expect(error.textContent).not.toContain("Input should be");
    await waitFor(() => {
      expect((screen.getByTestId("demand-submit") as HTMLButtonElement).disabled).toBe(
        false,
      );
    });
  });

  it("服务端逐条兜底：本地只查 ≥1，非法 URL 形态由 422 按索引回显（多条含未知条目收敛）", async () => {
    const backend = installConsoleBackend([], {
      failDemandCreateStatus: 422,
      failDemandCreateDetail: [
        {
          type: "url_scheme",
          loc: ["body", "source_urls", 2],
          msg: "Input should be a valid http(s) URL without embedded credentials",
        },
        {
          type: "url_scheme",
          loc: ["body", "source_urls", 4],
          msg: "Input should be a valid http(s) URL without embedded credentials",
        },
        { type: "string_type", loc: ["body", "description"], msg: "Unexpected" },
      ],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    // 非法 URL 通过本地 ≥1 预校验（形态合法性交给服务端兜底）
    fillForm({ sourceUrls: "https://ok.example.com/a\nftp://bad.example.com/b\njavascript:alert(1)" });
    fireEvent.click(screen.getByTestId("demand-submit"));

    const items = await screen.findAllByTestId("demand-submit-error-item");
    expect(items.length).toBe(3);
    expect(items[0]!.textContent).toContain("第 3 条源地址不合法");
    expect(items[1]!.textContent).toContain("第 5 条源地址不合法");
    expect(items[2]!.textContent).toContain("未通过服务端校验");
  });

  it("服务端镜像硬校验（假后端冻结契约）：ftp 协议 URL 被逐条 422 回显", async () => {
    const backend = installConsoleBackend([]);
    vi.stubGlobal("fetch", backend.fetchImpl);
    seedConsoleSession();
    render(<DemandNewView indicator={null} />);

    fillForm({ sourceUrls: "https://ok.example.com/a\nftp://bad.example.com/b" });
    fireEvent.click(screen.getByTestId("demand-submit"));

    const items = await screen.findAllByTestId("demand-submit-error-item");
    expect(items.length).toBe(1);
    expect(items[0]!.textContent).toContain("第 2 条源地址不合法");
    expect(screen.queryByTestId("demand-success")).toBeNull();
  });
});
