// 单记录页运行时组件测试（wire-record-view 任务 4.2/5.1/5.2）：
// 公开态——取数端点裸 fetch（路径 + t/k 齐整、**绝不携带 Authorization**）；
// 四态——就绪（header 字段区 + 时间轴升序 + 节点点击展开 + truncated/error
// 如实标注）/「无此记录」（200 found=false，与 404 区分）/「链接无效」
// （404 统一口径）/加载中；503「分享功能未开通」与 429 限流态。
// 试预览态——POST /artifacts/{id}/record-view/preview body {key}（console
// 会话身份携带 Authorization），服务端错误 detail 如实呈现。
// 路由分派——#/share/record-view/{id}?t&k 解析（照 shareDashboard 方式）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { RecordViewRuntime } from "./RecordViewRuntime";
import { parseHash } from "../../App";
import { seedConsoleSession } from "../testing";

/** 公开取数成功响应（proposal 冻结形状：header value / rows 单元格可为
 *  数字/字符串，渲染统一 String()）。 */
const READY_BODY = {
  title: "批次溯源页",
  version: 4,
  expires_at: null,
  found: true,
  header: [
    { label: "产品名称", column: "产品名称", value: "云南咖啡豆" },
    { label: "负责人", column: "负责人", value: 13800000000 },
    { label: "产地", column: "产地", value: null },
  ],
  sections: [
    {
      title: "打药记录",
      dateColumn: "施药日期",
      rows: [
        { 施药日期: "2026-03-02", 药剂: "多菌灵", 用量: 1.5 },
        { 施药日期: "2026-03-01", 药剂: "波尔多液", 用量: 2 },
      ],
      truncated: true,
    },
    {
      title: "播种",
      dateColumn: "日期",
      rows: [],
      truncated: false,
      error: "列「日期」已不在数据集列快照中",
    },
  ],
};

type FetchRecord = Array<{
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: unknown;
}>;

/** 假 fetch：记录请求（断言 Authorization 有无），按入参回状态/载荷。 */
function stubFetch(
  status: number,
  body: unknown,
): FetchRecord {
  const calls: FetchRecord = [];
  vi.stubGlobal(
    "fetch",
    ((input: RequestInfo | URL, init?: RequestInit) => {
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
      calls.push({
        url: String(input),
        method: (init?.method ?? "GET").toUpperCase(),
        headers,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      return Promise.resolve({
        ok: status < 400,
        status,
        json: () => Promise.resolve(body),
      } as unknown as Response);
    }) as unknown as typeof fetch,
  );
  return calls;
}

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

describe("RecordViewRuntime（公开态：取数与四态）", () => {
  it("就绪：GET 公开端点（t/k 齐整、无 Authorization）→ header 字段区 + 时间轴升序 + truncated/error 如实", async () => {
    const calls = stubFetch(200, READY_BODY);

    render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tok-abc", recordKey: "B-01" }}
      />,
    );

    // 公开端点：路径 + t/k 查询参数齐整，且绝不携带 Authorization。
    await waitFor(() => {
      expect(calls[0]?.url).toBe("/api/v1/share/record-view/11?t=tok-abc&k=B-01");
    });
    expect(calls[0]?.headers["authorization"]).toBeUndefined();

    // header 字段区：label: 值（数字 String()、null 占位 —）。
    const headerFields = await screen.findAllByTestId("record-view-header-field");
    expect(
      headerFields.map((el) => el.getAttribute("data-column")),
    ).toEqual(["产品名称", "负责人", "产地"]);
    expect(headerFields[0]!.textContent).toContain("云南咖啡豆");
    expect(headerFields[1]!.textContent).toContain("13800000000");
    expect(headerFields[2]!.textContent).toContain("—");

    // 时间轴：按施药日期升序（03-01 在前）+「已截断」徽标 + error 段如实。
    const section = screen
      .getAllByTestId("record-view-section")
      .find((el) => el.getAttribute("data-title") === "打药记录")!;
    const dates = within(section)
      .getAllByTestId("record-view-node-toggle")
      .map((el) => el.textContent);
    expect(dates[0]).toContain("2026-03-01");
    expect(dates[1]).toContain("2026-03-02");
    expect(screen.getByTestId("record-view-truncated").textContent).toBe("已截断");
    expect(screen.getByTestId("record-view-section-error").textContent).toContain(
      "列「日期」已不在数据集列快照中",
    );
  });

  it("节点点击展开该行全部列值（再点收起）", async () => {
    stubFetch(200, READY_BODY);

    render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tok", recordKey: "B-01" }}
      />,
    );

    const nodes = await screen.findAllByTestId("record-view-node-toggle");
    expect(screen.queryByTestId("record-view-node-detail")).toBeNull();

    fireEvent.click(nodes[0]!);
    const detail = screen.getByTestId("record-view-node-detail");
    expect(detail.textContent).toContain("施药日期");
    expect(detail.textContent).toContain("波尔多液");

    fireEvent.click(nodes[0]!);
    expect(screen.queryByTestId("record-view-node-detail")).toBeNull();
  });

  it("「无此记录」态：200 found=false 如实呈现（与 404 令牌态区分，不判错误）", async () => {
    const calls = stubFetch(200, {
      title: "批次溯源页",
      version: 4,
      expires_at: null,
      found: false,
    });

    render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tok", recordKey: "B-404" }}
      />,
    );

    expect(await screen.findByTestId("record-view-notfound")).toBeTruthy();
    expect(screen.getByTestId("record-view-notfound").textContent).toContain(
      "无此记录",
    );
    // 仍是 200 请求（无重试风暴），无就绪渲染。
    expect(calls).toHaveLength(1);
    expect(screen.queryByTestId("record-view-body")).toBeNull();
  });

  it("「链接无效」态：404 统一口径（不区分原因、无重试泄露）", async () => {
    stubFetch(404, { detail: "not found" });

    render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tampered", recordKey: "B-01" }}
      />,
    );

    expect(await screen.findByTestId("record-view-invalid")).toBeTruthy();
    expect(screen.getByTestId("record-view-invalid").textContent).toContain(
      "链接无效或已过期",
    );
    expect(screen.queryByTestId("record-view-body")).toBeNull();
  });

  it("503=分享功能未开通；429=限流可读文案（Retry 语义不白屏）", async () => {
    stubFetch(503, { detail: "FD_WIRE_SHARE_SECRET 未配置" });
    const { unmount } = render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tok", recordKey: "k" }}
      />,
    );
    expect(await screen.findByTestId("record-view-disabled")).toBeTruthy();
    expect(screen.getByTestId("record-view-disabled").textContent).toContain(
      "分享功能未开通",
    );
    unmount();

    stubFetch(429, { detail: "请求过于频繁，请稍后再试" });
    render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tok", recordKey: "k" }}
      />,
    );
    expect(await screen.findByTestId("record-view-rate-limited")).toBeTruthy();
    expect(screen.getByTestId("record-view-rate-limited").textContent).toContain(
      "请求过于频繁",
    );
  });

  it("加载中态：请求未返回即渲染加载文案", () => {
    vi.stubGlobal(
      "fetch",
      (() => new Promise<Response>(() => {})) as unknown as typeof fetch,
    );
    render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tok", recordKey: "k" }}
      />,
    );
    expect(screen.getByTestId("record-view-loading").textContent).toContain(
      "记录查询中",
    );
  });

  it("就绪回调：onLoaded 抬升 title/version（ShareView 页脚/og 消费）", async () => {
    stubFetch(200, READY_BODY);
    const onLoaded = vi.fn();
    render(
      <RecordViewRuntime
        endpoint={{ kind: "public", artifactId: 11, token: "tok", recordKey: "k" }}
        onLoaded={onLoaded}
      />,
    );
    await screen.findByTestId("record-view-body");
    expect(onLoaded).toHaveBeenCalledWith({ title: "批次溯源页", version: 4 });
  });
});

describe("RecordViewRuntime（试预览态：console 身份）", () => {
  it("POST preview 端点 body {key}、携带登录头；就绪同构渲染", async () => {
    const calls = stubFetch(200, READY_BODY);
    // previewRecordView 走 requestJson（会话 Bearer）；种入会话令牌。
    seedConsoleSession({ sub: "demo-customer", idToken: "id-token-xyz" });

    render(
      <RecordViewRuntime
        endpoint={{ kind: "preview", artifactId: 31, recordKey: "B-01" }}
      />,
    );

    await screen.findByTestId("record-view-body");
    const post = calls[0]!;
    expect(post.method).toBe("POST");
    expect(post.url).toBe("/api/v1/artifacts/31/record-view/preview");
    expect(post.body).toEqual({ key: "B-01" });
    // console 会话身份（与公开态的「绝不携带」相对）。
    expect(post.headers["authorization"]).toBe("Bearer id-token-xyz");
    // 同构渲染：时间轴照常。
    expect(screen.getAllByTestId("record-view-node-toggle").length).toBe(2);
  });

  it("preview 失败：服务端 detail 如实呈现（构建工具不适用「不泄露」纪律）", async () => {
    seedConsoleSession();
    stubFetch(422, { detail: "keyColumn 已不在主数据集列快照中" });

    render(
      <RecordViewRuntime
        endpoint={{ kind: "preview", artifactId: 31, recordKey: "B-01" }}
      />,
    );

    expect(await screen.findByTestId("record-view-preview-error")).toBeTruthy();
    expect(screen.getByTestId("record-view-preview-error").textContent).toContain(
      "keyColumn 已不在主数据集列快照中",
    );
  });
});

describe("路由分派（#/share/record-view/{id}?t&k）", () => {
  it("解析为 shareRecordView（t/k 进 route；缺参不拦截交运行时如实呈现）", () => {
    expect(parseHash("#/share/record-view/11?t=abc&k=B-01")).toEqual({
      name: "shareRecordView",
      id: "11",
      token: "abc",
      recordKey: "B-01",
    });
    // 键值带 percent-encoded 字符：query 解析出解码后的键值（原样进端点
    // 时再由 URLSearchParams 编码——往返无损）。
    expect(parseHash("#/share/record-view/12?t=d&k=%E6%89%B9%E6%AC%A1-01")).toMatchObject({
      name: "shareRecordView",
      recordKey: "批次-01",
    });
    // 缺 t：路由层不拦截（交运行时落无效页）；既有看板/表单分派零漂移。
    expect(parseHash("#/share/record-view/11")).toEqual({
      name: "shareRecordView",
      id: "11",
      token: null,
      recordKey: null,
    });
    expect(parseHash("#/share/dashboard/11?t=d&v=3")).toEqual({
      name: "shareDashboard",
      id: "11",
      token: "d",
      version: "3",
    });
  });
});
