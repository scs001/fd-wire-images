// 官网首页测试：hero/价值主张照旧；「联系开通」CTA 改站内表单卡
// （add-contact-intake）——提交成功态如实反馈、honeypot 字段隐藏在 DOM
// 但提交带空值、400 逐字段回显、成功后清表单。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { parseHash } from "../App";
import { HomeView } from "./HomeView";
import { installFakeBackend } from "../catalog/testing";
import { resetCatalogStatsCacheForTests } from "../catalog/useCatalogStats";

const STATS = {
  registered: 57553,
  verified: 3421,
  by_source: [
    { source_db: "world_bank", registered: 12000, verified: 800 },
    { source_db: "yearbook_catalog", registered: 45553, verified: 2621 },
  ],
};

interface ContactCall {
  url: URL;
  body: Record<string, unknown>;
}

/** 目录 stats 假后端 + /api/v1/contact 可编程端点；返回 contact 调用记录。 */
function installBackendWithContact(
  contact: (body: Record<string, unknown>) => {
    status: number;
    json: unknown;
  },
): ContactCall[] {
  const backend = installFakeBackend([], {}, STATS);
  const calls: ContactCall[] = [];
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://catalog.test");
    if (url.pathname === "/api/v1/contact") {
      calls.push({
        url,
        body: init?.body ? JSON.parse(String(init.body)) : {},
      });
      const spec = contact(calls[calls.length - 1]!.body);
      return Promise.resolve({
        ok: spec.status < 400,
        status: spec.status,
        json: () => Promise.resolve(spec.json),
      });
    }
    return (backend.fetchImpl as typeof fetch)(input, init);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", fetchImpl);
  return calls;
}

const VALID_FORM = {
  name: "张三",
  email: "zhangsan@example.com",
  company: "示例科技",
  message: "想开通全国光伏装机月度数据",
};

/** 打开表单卡并填入合法字段（honeypot 保持空值——真人对它不可见）。 */
async function openAndFillForm() {
  fireEvent.click(screen.getByTestId("home-cta-contact"));
  expect(screen.getByTestId("home-contact-card")).toBeTruthy();
  fireEvent.change(screen.getByTestId("contact-name"), {
    target: { value: VALID_FORM.name },
  });
  fireEvent.change(screen.getByTestId("contact-email"), {
    target: { value: VALID_FORM.email },
  });
  fireEvent.change(screen.getByTestId("contact-company"), {
    target: { value: VALID_FORM.company },
  });
  fireEvent.change(screen.getByTestId("contact-message"), {
    target: { value: VALID_FORM.message },
  });
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  resetCatalogStatsCacheForTests();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HomeView（官网首页 · 柏讯产品叙事）", () => {
  it("hero 与三条价值主张；目录规模数字活取 stats（57,553 → 「57,000+ 指标目录」）", async () => {
    installFakeBackend([], {}, STATS) && vi.stubGlobal("fetch", installFakeBackend([], {}, STATS).fetchImpl);
    render(<HomeView />);

    expect(screen.getByText("柏讯 Wire · B2B 数据平台")).toBeTruthy();
    // 千位向下取整：57,553 → 57,000（数字来自 /v1/catalog/stats，非手写）
    expect(await screen.findByText("57,000+ 指标目录")).toBeTruthy();
    expect(screen.getByText("MCP 直连数据")).toBeTruthy();
    // 价值主张三覆盖已上线服务：AI 对话构建 × 我的数据（上传物化/产物）
    expect(screen.getByText("AI 对话构建 × 我的数据")).toBeTruthy();
    const value3Desc = screen.getByText(/AI 对话构建分析产物并即时预览/);
    expect(value3Desc.textContent).toContain("上传");
    expect(value3Desc.textContent).toContain("物化");
    // 控制台已上线的口径（不再有「控制台上线中」过时占位口径）
    expect(value3Desc.textContent).toContain("控制台已上线");
    expect(screen.queryByText(/控制台上线中/)).toBeNull();
    // 五线站位一句（不展开叙事）
    expect(screen.getByText(/壹座之上、萬星闪耀/)).toBeTruthy();
  });

  it("stats 不可用 → 回退不带数字的文案，不显示写死的旧数字", async () => {
    vi.stubGlobal("fetch", installFakeBackend([], {}, null).fetchImpl); // stats 端点 503
    render(<HomeView />);

    expect(await screen.findByText("指标目录")).toBeTruthy();
    expect(screen.queryByText(/57,000/)).toBeNull();
    expect(screen.queryByText(/\d+\+ 指标目录/)).toBeNull();
    // 其余两条价值主张不受影响
    expect(screen.getByText("MCP 直连数据")).toBeTruthy();
  });

  it("CTA：浏览指标目录 → #/catalog；联系开通 → 原位展开站内表单卡（非 mailto）", () => {
    vi.stubGlobal("fetch", installFakeBackend([], {}, null).fetchImpl);
    render(<HomeView />);

    expect(
      screen.getByTestId("home-cta-catalog").getAttribute("href"),
    ).toBe("#/catalog");
    // 联系开通是按钮（不再是 mailto 链接），点击展开表单卡
    const contactCta = screen.getByTestId("home-cta-contact");
    expect(contactCta.tagName).toBe("BUTTON");
    expect(screen.queryByTestId("home-contact-card")).toBeNull();
    fireEvent.click(contactCta);
    expect(screen.getByTestId("home-contact-card")).toBeTruthy();
    expect(screen.getByTestId("contact-name")).toBeTruthy();
    expect(screen.getByTestId("contact-email")).toBeTruthy();
    expect(screen.getByTestId("contact-company")).toBeTruthy();
    expect(screen.getByTestId("contact-message")).toBeTruthy();
  });

  it("合法提交 → POST /api/v1/contact 202 → 成功态如实反馈 + 表单清空", async () => {
    const calls = installBackendWithContact(() => ({
      status: 202,
      json: { received: true },
    }));
    render(<HomeView />);
    await openAndFillForm();

    // honeypot 字段在 DOM 中（隐藏呈现）且值为空
    const honeypot = document.querySelector(
      '[data-testid="contact-website"]',
    ) as HTMLInputElement;
    expect(honeypot).toBeTruthy();
    expect(honeypot.value).toBe("");

    fireEvent.click(screen.getByTestId("contact-submit"));
    expect(await screen.findByTestId("contact-success")).toBeTruthy();
    expect(screen.getByTestId("contact-success").textContent).toContain(
      "已收到您的提交",
    );

    // 请求形状：四字段 + honeypot 空值（自动化提交天然带空值）
    expect(calls.length).toBe(1);
    expect(calls[0]!.url.pathname).toBe("/api/v1/contact");
    expect(calls[0]!.body).toEqual({ ...VALID_FORM, website: "" });

    // 成功后清表单：四个输入全部为空，可继续提第二份
    expect(
      (screen.getByTestId("contact-name") as HTMLInputElement).value,
    ).toBe("");
    expect(
      (screen.getByTestId("contact-email") as HTMLInputElement).value,
    ).toBe("");
    expect(
      (screen.getByTestId("contact-company") as HTMLInputElement).value,
    ).toBe("");
    expect(
      (screen.getByTestId("contact-message") as HTMLInputElement).value,
    ).toBe("");
  });

  it("400 逐字段回显：服务端 detail 数组按字段定位（loc+code 本地化）", async () => {
    installBackendWithContact(() => ({
      status: 400,
      json: {
        detail: [
          { loc: ["body", "email"], msg: "invalid", type: "value_error" },
          { loc: ["body", "message"], msg: "too_long", type: "value_error" },
        ],
      },
    }));
    render(<HomeView />);
    await openAndFillForm();
    fireEvent.click(screen.getByTestId("contact-submit"));

    expect(await screen.findByTestId("contact-email-error")).toBeTruthy();
    expect(screen.getByTestId("contact-email-error").textContent).toContain(
      "邮箱格式不正确",
    );
    expect(screen.getByTestId("contact-message-error").textContent).toContain(
      "需求描述需在 2000 字符以内",
    );
    // 无成功态
    expect(screen.queryByTestId("contact-success")).toBeNull();
  });

  it("429 如实呈现限流口径（24h 窗口原文，不冒充其他错误）", async () => {
    installBackendWithContact(() => ({
      status: 429,
      json: { detail: "rate_limited" },
    }));
    render(<HomeView />);
    await openAndFillForm();
    fireEvent.click(screen.getByTestId("contact-submit"));

    const errorBox = await screen.findByTestId("contact-submit-error");
    expect(errorBox.textContent).toContain("24 小时内最多 5 条");
    expect(errorBox.textContent).toContain("同一邮箱最多 3 条");
  });

  it("honeypot 命中形态演练：提交携带非空 website → 服务端 400 通用拒绝文案", async () => {
    installBackendWithContact(() => ({
      status: 400,
      json: { detail: "rejected" },
    }));
    render(<HomeView />);
    await openAndFillForm();
    // 模拟机器人填了 honeypot（真人对该字段不可见）
    fireEvent.change(screen.getByTestId("contact-website"), {
      target: { value: "http://spam.example/" },
    });
    fireEvent.click(screen.getByTestId("contact-submit"));

    const errorBox = await screen.findByTestId("contact-submit-error");
    // 不提示是 honeypot：文案与普通校验失败同口径
    expect(errorBox.textContent).toContain("提交未通过校验");
    expect(errorBox.textContent.toLowerCase()).not.toContain("honeypot");
  });

  it("路由：#/ 归首页，首页无需登录即可渲染", () => {
    expect(parseHash("#/")).toEqual({ name: "home" });
    // 未登录环境（无任何凭据存储）直接渲染成功即代表可访问
    vi.stubGlobal("fetch", installFakeBackend([], {}, null).fetchImpl);
    render(<HomeView />);
    expect(screen.getByTestId("home-cta-catalog")).toBeTruthy();
  });
});
