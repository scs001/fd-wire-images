// 公开分享页组件测试（wire-dashboard-share 任务 2.1/2.3 + wire-share-branding
// 品牌面 1.1-1.3）：
// 三态——正常渲染（复用 DashboardRenderer + 页脚署名）/ 令牌坏 404 落
// 「链接无效或已过期」/ 503 未配置 secret 落「分享功能未开通」；
// 品牌面——四态同一双语品牌锁定顶栏（寻数·柏讯 / Wire，不随语言切换）、
// 页脚就绪态三段署名/非就绪态产品署名、og:title/og:description/og:site_name
// 注入与卸载清理；
// 红线——noindex meta 注入与卸载移除、document.title 前缀「分享 · 」、
// 公开端点请求绝不携带 Authorization（免登录接收者直接打开）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { ShareView } from "./ShareView";
import { parseHash } from "../App";

/** v1 信封 spec（产物详情原样形状）：单 kpi 面板。 */
const DASH_SPEC = {
  title: "宏观仪表盘",
  spec: {
    title: "宏观仪表盘",
    panels: [
      {
        id: "p1",
        type: "kpi",
        title: "CPI 同比",
        span: 1,
        snapshot: { columns: ["value"], rows: [[2.3]] },
        config: { unit: "%" },
      },
    ],
  },
};

/** 公开端点成功响应（proposal 冻结形状 {title, spec, version, shared_at}）。 */
const SHARE_BODY = {
  title: "宏观仪表盘",
  spec: DASH_SPEC,
  version: 3,
  shared_at: "2026-10-11T08:00:00",
};

type FetchRecord = { url: string; headers: Record<string, string> }[];

/** 打公开端点假 fetch：记录请求（断言无 Authorization），按入参回状态。 */
function stubShareFetch(status: number, body: unknown): FetchRecord {
  const calls: FetchRecord = [];
  vi.stubGlobal(
    "fetch",
    ((input: RequestInfo | URL, init?: RequestInit) => {
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach(
        (v, k) => {
          headers[k.toLowerCase()] = v;
        },
      );
      calls.push({ url: String(input), headers });
      return Promise.resolve({
        ok: status < 400,
        status,
        json: () => Promise.resolve(body),
      } as unknown as Response);
    }) as unknown as typeof fetch,
  );
  return calls;
}

function robotsMeta(): HTMLMetaElement | null {
  return document.querySelector('meta[name="robots"]');
}

/** 就绪态注入的 og meta（data-share-og 标记定位）。 */
function ogMeta(property: string): HTMLMetaElement | null {
  return document.querySelector(`meta[property="${property}"]`);
}

/** 品牌锁定断言：顶栏 寻数·柏讯 + Wire 双语并呈，链接产品首页 #/。 */
function expectShareBrand() {
  const brand = screen.getByTestId("share-brand");
  expect(brand.textContent).toContain("寻数·柏讯");
  expect(brand.textContent).toContain("Wire");
  expect(brand.getAttribute("href")).toBe("#/");
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  document.title = "柏讯 Wire — B2B 数据平台";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("ShareView（公开分享页三态 + noindex 红线）", () => {
  it("正常渲染：复用 DashboardRenderer 只读、页脚三段署名、og 注入、标题前缀、noindex meta、请求无鉴权头", async () => {
    const calls = stubShareFetch(200, SHARE_BODY);

    const { unmount } = render(
      <ShareView artifactId="11" token="tok-abc" version="3" />,
    );

    // 公开端点：路径 + t/v 查询参数齐整，且绝不携带 Authorization
    await waitFor(() => {
      expect(calls[0]?.url).toBe(
        "/api/v1/share/dashboard/11?t=tok-abc&v=3",
      );
    });
    expect(calls[0]?.headers["authorization"]).toBeUndefined();

    // 复用渲染器：v1 面板正常画出（kpi 大数），无编辑入口
    expect(await screen.findByTestId("dashboard-renderer")).toBeTruthy();
    expect(
      (await screen.findByTestId("dashboard-kpi-value")).textContent,
    ).toBe("2.3");
    expect(screen.getByTestId("share-dashboard-title").textContent).toBe(
      "宏观仪表盘",
    );
    // 导出红线（wire-dashboard-export）：分享页零导出入口（渲染器不携带
    // 导出 UI，导出只在控制台查看态 DashboardPane 工具区）
    expect(screen.queryByTestId("dashboard-export-csv")).toBeNull();
    // 面板工具栏红线（wire-chart-contract-v2 任务 3.5）：分享页不传 toolbar，
    // 全屏/导出图/图数切换/复制数据一律不出现。
    expect(screen.queryByTestId("dashboard-panel-toolbar")).toBeNull();
    expect(screen.queryByTestId("panel-tool-fullscreen")).toBeNull();
    expect(screen.queryByTestId("panel-tool-export")).toBeNull();
    expect(screen.queryByTestId("panel-tool-table")).toBeNull();
    expect(screen.queryByTestId("panel-tool-copy")).toBeNull();
    expect(
      screen.getByTestId("dashboard-renderer").getAttribute("data-toolbar"),
    ).toBe("off");

    // 品牌顶栏：双语锁定 + 产品首页链接
    expectShareBrand();

    // 页脚署名（就绪态三段）：{看板名} · 由 寻数·柏讯 提供 · 版本 v{n}
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "宏观仪表盘 · 由 寻数·柏讯 提供 · 版本 v3",
    );

    // og 链接预览元信息（就绪态注入）：title=看板名、site_name=品牌锁定、
    // description 双语一句话；noindex 红线仍在。
    expect(ogMeta("og:title")?.getAttribute("content")).toBe("宏观仪表盘");
    expect(ogMeta("og:site_name")?.getAttribute("content")).toBe(
      "寻数·柏讯 Wire",
    );
    expect(ogMeta("og:description")?.getAttribute("content")).toBe(
      "柏讯数据看板——企业数据 × 宏观指标",
    );

    // 红线：document.title 前缀「分享 · 」+ robots noindex meta 已注入
    expect(document.title).toBe("分享 · 宏观仪表盘");
    expect(robotsMeta()?.getAttribute("content")).toBe("noindex");

    // 卸载清理：og 与 noindex 一并移除（SPA 其他视图不受影响）
    unmount();
    expect(ogMeta("og:title")).toBeNull();
    expect(ogMeta("og:description")).toBeNull();
    expect(ogMeta("og:site_name")).toBeNull();
    expect(robotsMeta()).toBeNull();
  });

  it("令牌无效/过期（404）：落「链接无效或已过期」静态页——无渲染器、无重试泄露", async () => {
    stubShareFetch(404, { detail: "not found" });

    const { unmount } = render(
      <ShareView artifactId="11" token="tampered-token" version="1" />,
    );

    expect(await screen.findByTestId("share-invalid")).toBeTruthy();
    expect(screen.getByTestId("share-invalid").textContent).toContain(
      "链接无效或已过期",
    );
    // 无效页同样带品牌顶栏（四态品牌一致）；页脚落非就绪态产品署名
    expectShareBrand();
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "由 寻数·柏讯 提供",
    );
    // 不渲染看板本体，也不注入 og；标题仍带「分享 · 」前缀
    expect(screen.queryByTestId("dashboard-renderer")).toBeNull();
    expect(ogMeta("og:title")).toBeNull();
    expect(ogMeta("og:site_name")).toBeNull();
    expect(document.title).toContain("分享 · ");
    // 卸载移除 noindex meta（SPA 其他视图不受影响）
    unmount();
    expect(robotsMeta()).toBeNull();
  });

  it("服务端未配置 secret（503）：落「分享功能未开通」页", async () => {
    stubShareFetch(503, { detail: "FD_WIRE_SHARE_SECRET 未配置" });

    render(<ShareView artifactId="11" token="tok-abc" version="2" />);

    expect(await screen.findByTestId("share-disabled")).toBeTruthy();
    expect(screen.getByTestId("share-disabled").textContent).toContain(
      "分享功能未开通",
    );
    // 未开通页同样带品牌顶栏（四态品牌一致）+ 非就绪态产品署名，无 og
    expectShareBrand();
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "由 寻数·柏讯 提供",
    );
    expect(screen.queryByTestId("dashboard-renderer")).toBeNull();
    expect(screen.queryByTestId("share-invalid")).toBeNull();
    expect(ogMeta("og:title")).toBeNull();
  });

  it("缺版本参数：请求省略 v（仅 t）；品牌锁定顶栏常在", async () => {
    const calls = stubShareFetch(200, SHARE_BODY);

    render(<ShareView artifactId="11" token="tok-only" version={null} />);

    expect(await screen.findByTestId("dashboard-renderer")).toBeTruthy();
    expect(calls[0]?.url).toBe("/api/v1/share/dashboard/11?t=tok-only");
    expectShareBrand();
  });

  it("list 面板在分享页只读呈现：卡片可见、无工具栏/导出入口", async () => {
    stubShareFetch(200, {
      ...SHARE_BODY,
      spec: {
        title: "资讯流",
        spec: {
          title: "资讯流",
          panels: [
            {
              id: "f1",
              type: "list",
              title: "最新动态",
              span: 3,
              encoding: { title: "title", summary: "summary", link: "url" },
              snapshot: {
                columns: ["title", "summary", "url"],
                rows: [
                  ["标题甲", "摘要甲", "https://example.com/a"],
                  ["标题乙", "摘要乙", ""],
                ],
              },
            },
          ],
        },
      },
    });

    render(<ShareView artifactId="11" token="tok-abc" version="3" />);

    expect(await screen.findByTestId("dashboard-list")).toBeTruthy();
    expect(
      document.querySelectorAll('[data-testid="dashboard-list-item"]'),
    ).toHaveLength(2);
    // 公开只读红线：分享页不传 toolbar → 无任何工具栏/导出入口
    expect(screen.queryByTestId("dashboard-panel-toolbar")).toBeNull();
    expect(document.querySelector('[data-testid="panel-tool-export"]')).toBeNull();
  });

  it("四态品牌一致：加载中即有品牌顶栏与产品署名，且不注入 og", async () => {
    // 永不 resolve 的 fetch：停在 loading 态检查品牌面
    vi.stubGlobal(
      "fetch",
      (() => new Promise<Response>(() => {})) as unknown as typeof fetch,
    );

    render(<ShareView artifactId="11" token="tok-abc" version="3" />);

    expect(screen.getByTestId("share-loading")).toBeTruthy();
    expectShareBrand();
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "由 寻数·柏讯 提供",
    );
    expect(ogMeta("og:title")).toBeNull();
  });

  it("品牌锁定不随界面语言切换：en 下顶栏仍为 寻数·柏讯 + Wire，页脚/og 走英文文案", async () => {
    i18n.changeLanguage("en");
    stubShareFetch(200, SHARE_BODY);

    render(<ShareView artifactId="11" token="tok-abc" version="3" />);

    expect(await screen.findByTestId("dashboard-renderer")).toBeTruthy();
    // 双语锁定：品牌名两语言下相同（不翻译）
    expectShareBrand();
    // 页脚三段署名与 og description/site_name 随语言（site_name 为锁定值）
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "宏观仪表盘 · Provided by 寻数·柏讯 · Version v3",
    );
    expect(ogMeta("og:title")?.getAttribute("content")).toBe("宏观仪表盘");
    expect(ogMeta("og:site_name")?.getAttribute("content")).toBe(
      "寻数·柏讯 Wire",
    );
    expect(ogMeta("og:description")?.getAttribute("content")).toBe(
      "Baixun data dashboards — enterprise data × macro indicators",
    );
  });
});

// ── 表单分享态（wire-form-builder 任务 4.5）──────────────────────────────
// 契约：GET /api/v1/share/form/{id}?t&v → {title, spec(钉死版本信封原样),
// version, expires_at}；公开提交 POST /api/v1/share/form/{id}/submit?t&v
// body {values} → 201 {ok:true, message}；失败统一 404 / 503。四态框架、
// 品牌面、noindex 与看板分享页同款（mode 分派）。

/** 表单产物信封 spec（钉死版本原样）：城市必填 + 金额数字 + 下拉级别。 */
const FORM_SHARE_SPEC = {
  title: "门店日报表",
  spec: {
    title: "门店日报表",
    datasetId: "ds-store-1",
    fields: [
      { column: "city", label: "城市", input: "text", required: true },
      { column: "amount", label: "金额", input: "number" },
      {
        column: "level",
        label: "级别",
        input: "select",
        options: ["高", "低"],
      },
    ],
    submitLabel: "上报",
    successMessage: "门店数据已收到",
  },
};

const FORM_SHARE_BODY = {
  title: "门店日报表",
  spec: FORM_SHARE_SPEC,
  version: 2,
  expires_at: "2026-11-11T08:00:00",
};

type FormFetchRecord = Array<{
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: unknown;
}>;

/** 打公开表单端点假 fetch：GET 回表单载荷；POST /submit 回提交结果；
 *  记录全部请求（断言无 Authorization 与 t/v 透传）。 */
function stubFormShareFetch(): FormFetchRecord {
  const calls: FormFetchRecord = [];
  vi.stubGlobal(
    "fetch",
    ((input: RequestInfo | URL, init?: RequestInit) => {
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach(
        (v, k) => {
          headers[k.toLowerCase()] = v;
        },
      );
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({
        url,
        method,
        headers,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      if (method === "POST") {
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () =>
            Promise.resolve({ ok: true, message: "感谢上报，数据已收到" }),
        } as unknown as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(FORM_SHARE_BODY),
      } as unknown as Response);
    }) as unknown as typeof fetch,
  );
  return calls;
}

describe("ShareView 表单态（wire-form-builder 4.5：渲染 + 公开提交）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    document.title = "柏讯 Wire — B2B 数据平台";
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("渲染：GET 公开表单端点（t/v 透传、无鉴权头）→ FormRuntime 字段 + 页脚版本 + noindex/og", async () => {
    const calls = stubFormShareFetch();

    render(
      <ShareView artifactId="9" token="tok-form" version="2" mode="form" />,
    );

    // 公开 GET：路径 + t/v 查询参数齐整，绝不携带 Authorization
    await waitFor(() => {
      expect(calls[0]?.url).toBe("/api/v1/share/form/9?t=tok-form&v=2");
    });
    expect(calls[0]?.headers["authorization"]).toBeUndefined();

    // 标题 + FormRuntime（钉死版本字段：三字段 + 自定义提交文案）
    expect(
      (await screen.findByTestId("share-form-title")).textContent,
    ).toBe("门店日报表");
    expect(screen.getAllByTestId("form-field").length).toBe(3);
    expect(screen.getByTestId("form-submit").textContent).toContain("上报");

    // 品牌顶栏 + 页脚三段署名（版本 v2）
    expectShareBrand();
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "门店日报表 · 由 寻数·柏讯 提供 · 版本 v2",
    );

    // 红线：noindex + og:title/og:site_name 注入（form og:description）
    expect(robotsMeta()?.getAttribute("content")).toBe("noindex");
    expect(ogMeta("og:title")?.getAttribute("content")).toBe("门店日报表");
    expect(ogMeta("og:description")?.getAttribute("content")).toBe(
      "柏讯数据表单——填写提交即安全入账",
    );
  });

  it("公开提交：POST /submit（t/v 透传、无鉴权头、归一 values）→ 成功文案呈现", async () => {
    const calls = stubFormShareFetch();

    render(
      <ShareView artifactId="9" token="tok-form" version="2" mode="form" />,
    );
    await screen.findByTestId("form-runtime");

    // 必填缺失先拦（前端零请求）
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);

    // 填写并提交：公开 submit 端点
    fireEvent.change(
      screen
        .getAllByTestId("form-field")
        .find((el) => el.getAttribute("data-column") === "city")!
        .querySelector('[data-testid="form-field-input"]')!,
      { target: { value: "上海" } },
    );
    fireEvent.change(
      screen
        .getAllByTestId("form-field")
        .find((el) => el.getAttribute("data-column") === "amount")!
        .querySelector('[data-testid="form-field-input"]')!,
      { target: { value: "12.5" } },
    );
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(
      (await screen.findByTestId("form-success")).textContent,
    ).toBe("感谢上报，数据已收到");
    const post = calls.find((c) => c.method === "POST");
    expect(post?.url).toBe("/api/v1/share/form/9/submit?t=tok-form&v=2");
    expect(post?.headers["authorization"]).toBeUndefined();
    expect(post?.body).toEqual({
      values: { city: "上海", amount: 12.5 },
    });
  });

  it("令牌无效（404）：落「链接无效或已过期」页——不渲染表单", async () => {
    vi.stubGlobal(
      "fetch",
      (() =>
        Promise.resolve({
          ok: false,
          status: 404,
          json: () => Promise.resolve({ detail: "not found" }),
        } as unknown as Response)) as unknown as typeof fetch,
    );

    render(
      <ShareView artifactId="9" token="bad-token" version="2" mode="form" />,
    );

    expect(await screen.findByTestId("share-invalid")).toBeTruthy();
    expect(screen.getByTestId("share-invalid").textContent).toContain(
      "链接无效或已过期",
    );
    expect(screen.queryByTestId("form-runtime")).toBeNull();
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "由 寻数·柏讯 提供",
    );
  });

  it("路由分派：#/share/form/{id}?t&v 解析为 shareForm（照 shareDashboard 方式）", () => {
    const route = parseHash("#/share/form/9?t=abc&v=2");
    expect(route).toEqual({
      name: "shareForm",
      id: "9",
      token: "abc",
      version: "2",
    });
    // 看板分派零漂移
    expect(parseHash("#/share/dashboard/11?t=d&v=3")).toEqual({
      name: "shareDashboard",
      id: "11",
      token: "d",
      version: "3",
    });
    // 缺 t：路由层不拦截（交 ShareView 落无效页）
    expect(parseHash("#/share/form/9")).toEqual({
      name: "shareForm",
      id: "9",
      token: null,
      version: null,
    });
  });
});
