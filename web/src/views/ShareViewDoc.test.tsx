// 文档分享页测试（wire-rich-text-docs 任务 5.2 / 6.1）：
// 契约——GET /api/v1/share/doc/{id}?t&v → {title, spec(钉死版本信封原样
// {title, spec:{format:"markdown", text}), version, expires_at}；公开端点
// 免登录（绝不携带 Authorization），失败统一 404 / 503。四态框架、品牌面、
// noindex 与看板/表单分享页同款（mode 分派），正文换 MarkdownView——「渲染
// 安全底线」对公开分享页同样适用（正文里的 <script> 只能是文本）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { ShareView } from "./ShareView";
import { parseHash, App } from "../App";
import {
  installConsoleBackend,
  makeIdToken,
  seedConsoleSession,
} from "../console/testing";

/** doc 钉死版本响应（产物信封原样）：正文含标题 + 原始 HTML 注入样例。 */
const DOC_TEXT =
  "# 年度质量报告\n\n第一段正文。\n\n<script>alert(1)</script>\n\n- 指标一\n- 指标二";
const DOC_SHARE_BODY = {
  title: "年度质量报告",
  spec: {
    title: "年度质量报告",
    spec: { format: "markdown", text: DOC_TEXT },
  },
  version: 2,
  expires_at: null,
};

type FetchRecord = { url: string; headers: Record<string, string> }[];

/** 打公开 doc 端点假 fetch：记录请求（断言无 Authorization），回固定状态。 */
function stubDocFetch(status: number, body: unknown): FetchRecord {
  const calls: FetchRecord = [];
  vi.stubGlobal(
    "fetch",
    ((input: RequestInfo | URL, init?: RequestInit) => {
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
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

function ogMeta(property: string): HTMLMetaElement | null {
  return document.querySelector(`meta[property="${property}"]`);
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
  window.location.hash = "";
});

describe("ShareView doc 模式（wire-rich-text-docs 5.2：正文渲染 + 安全底线）", () => {
  it("渲染：GET 公开 doc 端点（t/v 透传、无鉴权头）→ 标题 + MarkdownView 正文渲染 + 页脚版本 + noindex/og", async () => {
    const calls = stubDocFetch(200, DOC_SHARE_BODY);

    const { unmount } = render(
      <ShareView artifactId="7" token="tok-doc" version="2" mode="doc" />,
    );

    // 公开 GET：路径 + t/v 查询参数齐整，绝不携带 Authorization
    await waitFor(() => {
      expect(calls[0]?.url).toBe("/api/v1/share/doc/7?t=tok-doc&v=2");
    });
    expect(calls[0]?.headers["authorization"]).toBeUndefined();

    // 标题 = 钉死版本信封 title；正文经同一渲染器出 h1（# 标题 → h1）
    expect(
      (await screen.findByTestId("share-doc-title")).textContent,
    ).toBe("年度质量报告");
    const body = screen.getByTestId("doc-markdown");
    expect(body.querySelector("h1")?.textContent).toBe("年度质量报告");
    expect(body.querySelectorAll("li").length).toBe(2);

    // 安全底线（spec「渲染安全底线」对分享页同样适用）：原始 <script> 只是
    // 文本，不成为 DOM 元素、不执行
    expect(body.textContent).toContain("<script>alert(1)</script>");
    expect(body.querySelector("script")).toBeNull();

    // 无编辑入口（正文只读页）
    expect(body.querySelector("textarea")).toBeNull();
    expect(screen.queryByText("编辑文档")).toBeNull();

    // 品牌顶栏（双语锁定）+ 页脚三段署名（版本 v2）
    const brand = screen.getByTestId("share-brand");
    expect(brand.textContent).toContain("寻数·柏讯");
    expect(brand.textContent).toContain("Wire");
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "年度质量报告 · 由 寻数·柏讯 提供 · 版本 v2",
    );

    // 红线：noindex + document.title 前缀 + doc og:description
    expect(robotsMeta()?.getAttribute("content")).toBe("noindex");
    expect(document.title).toBe("分享 · 年度质量报告");
    expect(ogMeta("og:title")?.getAttribute("content")).toBe("年度质量报告");
    expect(ogMeta("og:description")?.getAttribute("content")).toBe(
      "柏讯文档——正文内容页",
    );
    // 卸载清理：noindex 与 og 一并移除
    unmount();
    expect(robotsMeta()).toBeNull();
    expect(ogMeta("og:title")).toBeNull();
  });

  it("缺版本参数：请求省略 v（仅 t）——0 档长效链接同款（expires_at=null 不碍渲染）", async () => {
    const calls = stubDocFetch(200, DOC_SHARE_BODY);

    render(<ShareView artifactId="7" token="tok-perpetual" version={null} mode="doc" />);

    expect(await screen.findByTestId("share-doc-title")).toBeTruthy();
    expect(calls[0]?.url).toBe("/api/v1/share/doc/7?t=tok-perpetual");
  });

  it("令牌无效/归档断链（404）：落「链接无效或已过期」页——不渲染正文", async () => {
    stubDocFetch(404, { detail: "not found" });

    render(<ShareView artifactId="7" token="bad-token" version="2" mode="doc" />);

    expect(await screen.findByTestId("share-invalid")).toBeTruthy();
    expect(screen.getByTestId("share-invalid").textContent).toContain(
      "链接无效或已过期",
    );
    expect(screen.queryByTestId("share-doc")).toBeNull();
    expect(screen.queryByTestId("doc-markdown")).toBeNull();
    expect(screen.getByTestId("share-footer").textContent).toBe(
      "由 寻数·柏讯 提供",
    );
  });

  it("服务端未配置 secret（503）：落「分享功能未开通」页", async () => {
    stubDocFetch(503, { detail: "FD_WIRE_SHARE_SECRET 未配置" });

    render(<ShareView artifactId="7" token="tok-doc" version="2" mode="doc" />);

    expect(await screen.findByTestId("share-disabled")).toBeTruthy();
    expect(screen.getByTestId("share-disabled").textContent).toContain(
      "分享功能未开通",
    );
  });

  it("路由分派：#/share/doc/{id}?t&v 解析为 shareDoc（照 shareDashboard 方式）", () => {
    expect(parseHash("#/share/doc/7?t=x&v=2")).toEqual({
      name: "shareDoc",
      id: "7",
      token: "x",
      version: "2",
    });
    // 缺 t/v：路由层不拦截（交 ShareView 落无效页）
    expect(parseHash("#/share/doc/7")).toEqual({
      name: "shareDoc",
      id: "7",
      token: null,
      version: null,
    });
    // 缺 id 段不构成分享路由，回落首页
    expect(parseHash("#/share/doc")).toEqual({ name: "home" });
    // 既有分享路由零漂移
    expect(parseHash("#/share/dashboard/11?t=d&v=3")).toEqual({
      name: "shareDashboard",
      id: "11",
      token: "d",
      version: "3",
    });
  });

  it("App 壳分派：#/share/doc/* 走独立极简壳（品牌顶栏在，站点导航/页脚不渲染）+ noindex", async () => {
    document.title = "柏讯 Wire — B2B 数据平台";
    // 分享页调公开端点（无鉴权）——404 即「链接无效」态，零依赖。
    stubDocFetch(404, { detail: "not found" });
    window.location.hash = "#/share/doc/7?t=tok&v=2";

    render(<App />);

    expect(await screen.findByTestId("share-invalid")).toBeTruthy();
    expect(screen.getByTestId("share-topbar").textContent).toContain("寻数·柏讯");
    // 独立成壳：不进站点 header/footer，也无控制台布局
    expect(screen.queryByTestId("site-footer")).toBeNull();
    expect(screen.queryByTestId("console-footer")).toBeNull();
    // 标题前缀与 noindex 红线在 App 壳层同样生效
    expect(document.title).toContain("分享 · ");
    expect(robotsMeta()?.getAttribute("content")).toBe("noindex");
  });
});

// ── 假后端 doc 支撑（wire-rich-text-docs 任务 6.2）────────────────────────
// 空白创建白名单 +doc 与草稿层 GET/PUT——DocEditor 的 console 测试面依赖
// 这两段接线（编辑器只取 DocEditor 自身测试，本文件锚定 mock 契约本身不漂）。

describe("testing.ts 假后端 doc 支撑（任务 6.2）", () => {
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

  it("空白创建 doc 放行：201 draft v1，spec = {format:'markdown', text:''}", async () => {
    const backend = installConsoleBackend([]);
    seedConsoleSession({ sub: "demo-customer" });
    // 假后端按 Bearer JWT 的 sub 认客户（对齐生产身份语义）。
    const auth = {
      Authorization: `Bearer ${makeIdToken({ sub: "demo-customer" })}`,
      "Content-Type": "application/json",
    };

    const response = await backend.fetchImpl("/api/v1/artifacts", {
      method: "POST",
      headers: auth,
      body: JSON.stringify({ kind: "doc", title: "年度质量报告" }),
    });
    expect(response.status).toBe(201);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.kind).toBe("doc");
    expect(body.status).toBe("draft");
    expect(body.version).toBe(1);
    expect(body.spec).toEqual({
      title: "年度质量报告",
      spec: { format: "markdown", text: "" },
    });
  });

  it("空白创建白名单外 kind 仍 422（文案点名 doc；record-view 行为不变）", async () => {
    const backend = installConsoleBackend([]);
    seedConsoleSession({ sub: "demo-customer" });
    const auth = {
      Authorization: `Bearer ${makeIdToken({ sub: "demo-customer" })}`,
      "Content-Type": "application/json",
    };
    const post = (kind: string) =>
      backend.fetchImpl("/api/v1/artifacts", {
        method: "POST",
        headers: auth,
        body: JSON.stringify({ kind, title: "x" }),
      });

    const unknown = await post("composite_indicator");
    expect(unknown.status).toBe(422);
    expect(
      ((await unknown.json()) as { detail: string }).detail,
    ).toContain("dashboard/form/alert_rule/doc");

    // record-view 不在本 mock 白名单（任务 6.2 明示不改其既有行为）
    const recordView = await post("record-view");
    expect(recordView.status).toBe(422);
  });

  it("草稿层 GET 无行双 null → PUT 覆盖写（响应回显）→ GET 取回最新；版本链零新增", async () => {
    const backend = installConsoleBackend([]);
    seedConsoleSession({ sub: "demo-customer" });
    const auth = {
      Authorization: `Bearer ${makeIdToken({ sub: "demo-customer" })}`,
      "Content-Type": "application/json",
    };
    const created = await backend.fetchImpl("/api/v1/artifacts", {
      method: "POST",
      headers: auth,
      body: JSON.stringify({ kind: "doc", title: "草稿文档" }),
    });
    const artifactId = ((await created.json()) as { id: number }).id;
    const draftUrl = `/api/v1/artifacts/${artifactId}/draft`;

    // 无草稿行：GET {text:null, updated_at:null}（前端据此回退当前版本）
    const initial = await backend.fetchImpl(draftUrl, { headers: auth });
    expect(initial.status).toBe(200);
    expect(await initial.json()).toEqual({ text: null, updated_at: null });

    // PUT 覆盖写：回显正文与时刻
    const put = await backend.fetchImpl(draftUrl, {
      method: "PUT",
      headers: auth,
      body: JSON.stringify({ text: "第一稿\n\n正文。" }),
    });
    expect(put.status).toBe(200);
    const putBody = (await put.json()) as { text: string; updated_at: string };
    expect(putBody.text).toBe("第一稿\n\n正文。");
    expect(typeof putBody.updated_at).toBe("string");
    expect(backend.getDocDraft(artifactId)?.text).toBe("第一稿\n\n正文。");

    // 覆盖写不产生版本行（design D3：草稿层是工作区，版本链是里程碑）
    const detail = await backend.fetchImpl(`/api/v1/artifacts/${artifactId}`, {
      headers: auth,
    });
    expect(((await detail.json()) as { version: number }).version).toBe(1);
  });

  it("草稿层边界：非 doc kind 422、archived 409、非字符串 422", async () => {
    const backend = installConsoleBackend([]);
    seedConsoleSession({ sub: "demo-customer" });
    const auth = {
      Authorization: `Bearer ${makeIdToken({ sub: "demo-customer" })}`,
      "Content-Type": "application/json",
    };
    const post = (kind: string, title: string) =>
      backend.fetchImpl("/api/v1/artifacts", {
        method: "POST",
        headers: auth,
        body: JSON.stringify({ kind, title }),
      });

    // 非 doc 产物：读写两路 422（草稿层只服务正文型产物）
    const board = await post("dashboard", "看板");
    const boardId = ((await board.json()) as { id: number }).id;
    expect(
      (
        await backend.fetchImpl(`/api/v1/artifacts/${boardId}/draft`, {
          headers: auth,
        })
      ).status,
    ).toBe(422);
    expect(
      (
        await backend.fetchImpl(`/api/v1/artifacts/${boardId}/draft`, {
          method: "PUT",
          headers: auth,
          body: JSON.stringify({ text: "看板不能存草稿" }),
        })
      ).status,
    ).toBe(422);

    // 非字符串 422（未归档的 doc 上验证——archived 门在类型校验之前）
    const doc = await post("doc", "归档文档");
    const docId = ((await doc.json()) as { id: number }).id;
    const bad = await backend.fetchImpl(`/api/v1/artifacts/${docId}/draft`, {
      method: "PUT",
      headers: auth,
      body: JSON.stringify({ text: 42 }),
    });
    expect(bad.status).toBe(422);

    // archived 拒写 409，读仍可（只读位不拦读）；零半写
    await backend.fetchImpl(`/api/v1/artifacts/${docId}/archive`, {
      method: "POST",
      headers: auth,
    });
    const archivedPut = await backend.fetchImpl(
      `/api/v1/artifacts/${docId}/draft`,
      {
        method: "PUT",
        headers: auth,
        body: JSON.stringify({ text: "归档后写入" }),
      },
    );
    expect(archivedPut.status).toBe(409);
    expect(backend.getDocDraft(docId)).toBeNull(); // 零半写
    expect(
      (
        await backend.fetchImpl(`/api/v1/artifacts/${docId}/draft`, {
          headers: auth,
        })
      ).status,
    ).toBe(200);
  });
});
