// 产物页组件测试（wire-artifacts-persistence）：
// 列表接 GET /api/v1/artifacts（kind/状态徽标、版本号、来源会话指针、直接创建）、
// kind/status 筛选、spec 编辑保存走 PUT（新版本号提示、非法 JSON 拒存、本地
// 未保存缓冲与丢弃、归档只读）、发布/归档/恢复状态迁移四态、版本历史抽屉
// （列历史 + 查看任一版本 + 可复制）、空态。
// wire-alert-evaluation 任务 5：alert_rule 行状态徽标四态 / 详情区告警事件
// 列表（刷新）/ 试运行（成功与失败如实）/ 非 alert_rule 行零变化 / states
// 拉取失败静默降级 + api client 翻译层三条。
// wire-personal-preferences：dashboard 行置顶/取消置顶（PUT 偏好 + 「默认」
// 徽标即时移动）、422 行内红字（徽标不动）、initialDashboardId 挂载即展开
//（无该 prop 的重挂载不展开）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import "../i18n";
import i18n from "../i18n";
import { ArtifactsPage } from "./ArtifactsPage";
import { installConsoleBackend, seedConsoleSession } from "./testing";
import type { ConsoleBackendOptions, LifecycleArtifactSeed } from "./testing";
import { formatServerDate } from "./util";
import {
  ConsoleApiError,
  getAlertEvents,
  getAlertStates,
  testAlertRule,
  type Conversation,
} from "./api";

const CONVERSATIONS: Conversation[] = [
  {
    id: 1,
    customer_id: "demo-customer",
    title: "宏观仪表盘",
    created_at: "2026-10-04T08:00:00",
  },
  {
    id: 2,
    customer_id: "demo-customer",
    title: "煤价告警",
    created_at: "2026-10-03T08:00:00",
  },
];

const V1_DASH = {
  title: "宏观仪表盘",
  spec: { indicators: ["CN_CPI_YOY", "CN_PMI"], layout: "2x2" },
};
const ALERT_V1 = {
  title: "煤价周环比 >5% 告警",
  spec: { indicator: "CN_COAL_PRICE", threshold: 0.05 },
};
const ALERT_V2 = {
  title: "煤价周环比 >5% 告警（放宽到 8%）",
  spec: { indicator: "CN_COAL_PRICE", threshold: 0.08 },
};

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // 登录会话种子：假后端以 Bearer 的 sub 为客户身份（demo-customer）。
  seedConsoleSession({ sub: "demo-customer" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

function install(options: ConsoleBackendOptions = {}) {
  const backend = installConsoleBackend(CONVERSATIONS, options);
  backend.addLifecycleArtifact({
    id: 11,
    kind: "dashboard",
    title: "宏观仪表盘",
    status: "draft",
    version: 1,
    origin_conversation_id: 1,
    spec: V1_DASH,
    created_at: "2026-10-09T09:00:00",
    updated_at: "2026-10-10T09:00:00",
  });
  backend.addLifecycleArtifact({
    id: 12,
    kind: "alert_rule",
    title: "煤价周环比 >5% 告警（放宽到 8%）",
    status: "published",
    version: 2,
    origin_conversation_id: 2,
    versions: [{ spec: ALERT_V1 }, { spec: ALERT_V2 }],
    created_at: "2026-10-09T10:00:00",
    updated_at: "2026-10-10T10:00:00",
  });
  backend.addLifecycleArtifact({
    id: 13,
    kind: "refresh_schedule",
    title: "每日 08:00 刷新",
    status: "archived",
    version: 1,
    origin_conversation_id: null,
    spec: { title: "每日 08:00 刷新", spec: { cron: "0 8 * * *", timezone: "Asia/Shanghai" } },
    created_at: "2026-10-09T11:00:00",
    updated_at: "2026-10-10T11:00:00",
  });
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

/** 定位指定产物 id 的列表行。 */
function rowOf(id: number): HTMLElement {
  return screen
    .getAllByTestId("artifact-item")
    .find((el) => el.getAttribute("data-artifact-id") === String(id))!;
}

/** 打开指定产物 id 的 spec 编辑器。 */
function openViewer(id: number): void {
  fireEvent.click(within(rowOf(id)).getByTestId("artifact-view"));
}

describe("ArtifactsPage（产物列表：持久化 + 版本 + 状态机）", () => {
  it("三产物渲染：kind/状态徽标、版本号、来源会话指针、直接创建标注", async () => {
    install();
    render(<ArtifactsPage />);

    const items = await screen.findAllByTestId("artifact-item");
    expect(items.length).toBe(3);

    const kindBadges = screen
      .getAllByTestId("artifact-kind-badge")
      .map((el) => el.getAttribute("data-kind"));
    expect(kindBadges.sort()).toEqual(["alert_rule", "dashboard", "refresh_schedule"]);
    // 徽标文案走 i18n（中文名）
    expect(
      screen.getAllByTestId("artifact-kind-badge").map((el) => el.textContent),
    ).toEqual(expect.arrayContaining(["仪表盘", "告警", "定时刷新"]));

    // 状态徽标语义分级：draft/published/archived 三态各一
    const statusBadges = screen
      .getAllByTestId("artifact-status-badge")
      .map((el) => el.getAttribute("data-status"));
    expect(statusBadges.sort()).toEqual(["archived", "draft", "published"]);
    expect(
      screen.getAllByTestId("artifact-status-badge").map((el) => el.textContent),
    ).toEqual(expect.arrayContaining(["草稿", "已发布", "已归档"]));

    // 版本号：published 产物已到 v2，其余 v1
    expect(
      within(rowOf(11)).getByTestId("artifact-version").textContent,
    ).toBe("v1");
    expect(
      within(rowOf(12)).getByTestId("artifact-version").textContent,
    ).toBe("v2");

    // 来源会话指针回跳 chat?c=；直接创建的产物如实标注
    const links = screen
      .getAllByTestId("artifact-source-link")
      .map((el) => el.getAttribute("href"));
    expect(links).toEqual(
      expect.arrayContaining(["#/console/chat?c=1", "#/console/chat?c=2"]),
    );
    expect(
      within(rowOf(13)).getByTestId("artifact-source-none").textContent,
    ).toContain("直接创建");
  });

  it("kind 与 status 筛选收窄列表", async () => {
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    expect(screen.getAllByTestId("artifact-item").length).toBe(3);

    fireEvent.change(screen.getByTestId("artifact-kind-filter"), {
      target: { value: "dashboard" },
    });
    expect(screen.getAllByTestId("artifact-item").length).toBe(1);
    expect(
      screen.getByTestId("artifact-kind-badge").getAttribute("data-kind"),
    ).toBe("dashboard");

    fireEvent.change(screen.getByTestId("artifact-kind-filter"), {
      target: { value: "all" },
    });
    fireEvent.change(screen.getByTestId("artifact-status-filter"), {
      target: { value: "archived" },
    });
    expect(screen.getAllByTestId("artifact-item").length).toBe(1);
    expect(
      screen.getByTestId("artifact-status-badge").getAttribute("data-status"),
    ).toBe("archived");
  });

  it("spec 编辑保存走 PUT：非法 JSON 拒存；合法保存提示新版本号并清缓冲", async () => {
    const backend = install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    openViewer(11);
    const editor = (await screen.findByTestId("spec-editor")) as HTMLTextAreaElement;
    expect(editor.value).toContain('"CN_CPI_YOY"');

    // 非法 JSON：拒绝保存并提示，不发 PUT
    fireEvent.change(editor, { target: { value: "{not json" } });
    fireEvent.click(screen.getByTestId("spec-save-server"));
    expect(screen.getByTestId("spec-invalid-json").textContent).toContain(
      "不是合法 JSON",
    );
    expect(
      backend.calls.filter((c) => c.method === "PUT"),
    ).toHaveLength(0);

    // 合法 JSON：PUT 保存 → 服务端追加 v2，提示新版本号
    const nextSpec = {
      title: "宏观仪表盘（修订）",
      spec: { indicators: ["CN_PMI"], layout: "2x2" },
    };
    fireEvent.change(editor, { target: { value: JSON.stringify(nextSpec) } });
    fireEvent.click(screen.getByTestId("spec-save-server"));
    expect(
      (await screen.findByTestId("spec-saved-version")).textContent,
    ).toContain("v2");
    // 请求体即完整 spec（最小校验口径：title + spec 对象）
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put?.url.pathname).toBe("/api/v1/artifacts/11");
    expect(put?.body).toEqual({ spec: nextSpec });
    // 列表行版本号同步为 v2；本地缓冲已清
    expect(within(rowOf(11)).getByTestId("artifact-version").textContent).toBe("v2");
    expect(localStorage.getItem("wire-web-artifact-buffer-11")).toBeNull();
    // 编辑器回到服务端返回的 spec
    expect(
      ((await screen.findByTestId("spec-editor")) as HTMLTextAreaElement).value,
    ).toContain("CN_PMI");
  });

  it("本地缓冲：编辑自动暂存 localStorage（离开提示缓冲），丢弃后回到服务端 spec", async () => {
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    openViewer(11);
    const editor = (await screen.findByTestId("spec-editor")) as HTMLTextAreaElement;
    fireEvent.change(editor, {
      target: { value: '{"title":"本地改过的仪表盘","spec":{"x":1}}' },
    });
    expect(localStorage.getItem("wire-web-artifact-buffer-11")).toContain(
      "本地改过的仪表盘",
    );
    expect(screen.getByTestId("spec-buffer-badge").textContent).toContain(
      "本地未保存编辑",
    );

    // 重新打开查看器：缓冲自动加载并带徽标（未保存离开提示的兜底）
    fireEvent.click(within(rowOf(11)).getByTestId("artifact-view"));
    openViewer(11);
    const reopened = (await screen.findByTestId("spec-editor")) as HTMLTextAreaElement;
    expect(reopened.value).toContain("本地改过的仪表盘");
    expect(screen.getByTestId("spec-buffer-badge").textContent).toContain(
      "本地未保存编辑",
    );

    // 丢弃本地编辑：清缓冲并回到服务端 spec
    fireEvent.click(screen.getByTestId("spec-discard-buffer"));
    expect(localStorage.getItem("wire-web-artifact-buffer-11")).toBeNull();
    expect(
      (screen.getByTestId("spec-editor") as HTMLTextAreaElement).value,
    ).toContain("CN_CPI_YOY");
    expect(screen.queryByTestId("spec-buffer-badge")).toBeNull();
  });

  it("归档产物 spec 只读：无保存按钮，提示先恢复", async () => {
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    openViewer(13);
    const editor = (await screen.findByTestId("spec-editor")) as HTMLTextAreaElement;
    expect((editor as HTMLTextAreaElement & { readOnly: boolean }).readOnly).toBe(true);
    expect(screen.queryByTestId("spec-save-server")).toBeNull();
    expect(screen.getByTestId("artifact-viewer").textContent).toContain("已归档");
    expect(screen.getByTestId("artifact-viewer").textContent).toContain("恢复");
  });

  it("状态迁移四态：草稿发布 → 归档 → 恢复回草稿；按钮随态收窄", async () => {
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    const row = rowOf(11);
    expect(within(row).getByTestId("artifact-publish")).toBeTruthy();
    expect(within(row).getByTestId("artifact-archive")).toBeTruthy();
    expect(within(row).queryByTestId("artifact-restore")).toBeNull();

    // 发布：draft → published
    fireEvent.click(within(row).getByTestId("artifact-publish"));
    await waitFor(() => {
      expect(
        within(rowOf(11)).getByTestId("artifact-status-badge").getAttribute("data-status"),
      ).toBe("published");
    });
    const badge = within(rowOf(11)).getByTestId("artifact-status-badge");
    expect(badge.textContent).toContain("已发布");
    // published 态：发布按钮退位，归档仍在
    expect(within(rowOf(11)).queryByTestId("artifact-publish")).toBeNull();
    expect(within(rowOf(11)).getByTestId("artifact-archive")).toBeTruthy();

    // 归档：published → archived
    fireEvent.click(within(rowOf(11)).getByTestId("artifact-archive"));
    await waitFor(() => {
      expect(
        within(rowOf(11)).getByTestId("artifact-status-badge").getAttribute("data-status"),
      ).toBe("archived");
    });
    // archived 态：只剩恢复
    expect(within(rowOf(11)).queryByTestId("artifact-archive")).toBeNull();
    expect(within(rowOf(11)).getByTestId("artifact-restore")).toBeTruthy();

    // 恢复：archived → draft
    fireEvent.click(within(rowOf(11)).getByTestId("artifact-restore"));
    await waitFor(() => {
      expect(
        within(rowOf(11)).getByTestId("artifact-status-badge").getAttribute("data-status"),
      ).toBe("draft");
    });
    expect(within(rowOf(11)).getByTestId("artifact-publish")).toBeTruthy();
  });

  it("版本历史抽屉：列历史（新在前）、查看任一版本 spec、可复制", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(within(rowOf(12)).getByTestId("artifact-history"));
    expect(await screen.findByTestId("version-drawer")).toBeTruthy();
    const entries = (await screen.findAllByTestId("version-item")).map((el) =>
      el.getAttribute("data-version"),
    );
    expect(entries).toEqual(["2", "1"]); // 新版本在前

    // 查看任一历史版本：v1 spec 快照如实呈现
    fireEvent.click(
      screen.getAllByTestId("version-item").find((el) => el.getAttribute("data-version") === "1")!,
    );
    const specText = (await screen.findByTestId("version-spec")).textContent ?? "";
    expect(specText).toContain("0.05");
    expect(specText).toContain("煤价周环比 >5% 告警");

    // 复制：整份 spec JSON 进剪贴板
    fireEvent.click(screen.getByTestId("version-copy"));
    expect(await screen.findByTestId("version-copied")).toBeTruthy();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(String(writeText.mock.calls[0]?.[0])).toContain("CN_COAL_PRICE");

    // 关闭抽屉
    fireEvent.click(screen.getByTestId("version-drawer-close"));
    expect(screen.queryByTestId("version-drawer")).toBeNull();
  });

  it("无产物显示空态", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend(CONVERSATIONS).fetchImpl,
    );
    render(<ArtifactsPage />);
    expect(await screen.findByTestId("artifacts-empty")).toBeTruthy();
  });
});

// ── 分享入口与弹层（wire-dashboard-share 任务 2.2/2.3）──────────────────────
// 端点 POST /api/v1/artifacts/{id}/share 未进通用假后端——本文件内联拦截
// （语义对齐 api：仅 published dashboard 发 {url, expires_at, version}，其余
// 403；可选强制状态注入测失败态）。展示链接须从响应自构：
// origin + /#/share/dashboard/{id}?t=…&v={version}（t 从数据端点 url 解出）。

/** 在 install() 三产物（11 dashboard draft / 12 alert published / 13 refresh
 *  archived）之上补两条：14 dashboard published（唯一合法分享对象）、
 *  15 dashboard archived——覆盖入口分发的全部组合。 */
function installShareRows() {
  const backend = install();
  backend.addLifecycleArtifact({
    id: 14,
    kind: "dashboard",
    title: "已发布看板",
    status: "published",
    version: 2,
    origin_conversation_id: 1,
    spec: V1_DASH,
    created_at: "2026-10-09T12:00:00",
    updated_at: "2026-10-10T12:00:00",
  });
  backend.addLifecycleArtifact({
    id: 15,
    kind: "dashboard",
    title: "已归档看板",
    status: "archived",
    version: 1,
    origin_conversation_id: null,
    spec: V1_DASH,
    created_at: "2026-10-09T13:00:00",
    updated_at: "2026-10-10T13:00:00",
  });
  return backend;
}

function localJson(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  };
}

/** 包装假后端：拦截 share POST（Bearer 断言 + 成功/403/强制态 + body 记录
 *  ——wire-share-ttl-options 的 ttl_days 断言依赖），其余透传。 */
function withShareEndpoint(
  backend: ReturnType<typeof installConsoleBackend>,
  force?: { status: number; detail: string },
): typeof fetch {
  const original = backend.fetchImpl;
  return ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const match = url.pathname.match(/^\/api\/v1\/artifacts\/(\d+)\/share$/);
    if (match && (init?.method ?? "GET").toUpperCase() === "POST") {
      const id = Number(match[1]);
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
      backend.calls.push({
        method: "POST",
        url,
        headers,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      if (force) {
        return Promise.resolve(localJson({ detail: force.detail }, force.status));
      }
      // 语义对齐 api：仅已发布仪表盘可分享（本种子集中即 14），其余 403。
      if (id !== 14) {
        return Promise.resolve(
          localJson(
            { detail: `产物 ${id} 不允许生成分享链接（仅已发布仪表盘可分享）` },
            403,
          ),
        );
      }
      return Promise.resolve(
        localJson({
          url: `/api/v1/share/dashboard/14?t=share-token-14&v=2`,
          expires_at: "2026-11-10T08:00:00",
          version: 2,
        }),
      );
    }
    return original(input, init);
  }) as unknown as typeof fetch;
}

describe("ArtifactsPage 分享入口（wire-dashboard-share 2.2/2.3）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession({ sub: "demo-customer" });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("入口按 kind+status 分发：仅 published dashboard 出「生成分享链接」", async () => {
    const backend = installShareRows();
    vi.stubGlobal("fetch", withShareEndpoint(backend));

    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // 唯一合法对象：14（dashboard × published）
    expect(
      within(rowOf(14)).getByTestId("artifact-share").textContent,
    ).toContain("生成分享链接");
    // draft dashboard（11）/ published 非 dashboard（12）/ archived 非
    // dashboard（13）/ archived dashboard（15）一律无入口
    expect(within(rowOf(11)).queryByTestId("artifact-share")).toBeNull();
    expect(within(rowOf(12)).queryByTestId("artifact-share")).toBeNull();
    expect(within(rowOf(13)).queryByTestId("artifact-share")).toBeNull();
    expect(within(rowOf(15)).queryByTestId("artifact-share")).toBeNull();
  });

  it("弹层：POST share → 展示自构公开页链接（origin+hash 路由+t/v）+ 到期时间 + 复制", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    const backend = installShareRows();
    vi.stubGlobal("fetch", withShareEndpoint(backend));

    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(within(rowOf(14)).getByTestId("artifact-share"));
    expect(await screen.findByTestId("share-dialog")).toBeTruthy();

    // 请求：POST /api/v1/artifacts/14/share，带登录 Bearer
    const post = backend.calls.find(
      (c) =>
        c.method === "POST" &&
        /^\/api\/v1\/artifacts\/14\/share$/.test(c.url.pathname),
    );
    expect(post).toBeTruthy();
    expect(post?.headers["authorization"]).toMatch(/^Bearer /);

    // 展示链接 = origin + #/share/dashboard/14?t=<响应 url 解出的 t>&v=<响应 version>
    const expectedUrl = `${window.location.origin}/#/share/dashboard/14?t=share-token-14&v=2`;
    expect(
      (await screen.findByTestId("share-url") as HTMLInputElement).value,
    ).toBe(expectedUrl);
    // 到期时间如实呈现（同 formatServerDate 口径）
    expect(screen.getByTestId("share-expires").textContent).toContain(
      formatServerDate("2026-11-10T08:00:00"),
    );
    expect(screen.getByTestId("share-expires").textContent).toContain("到期时间");

    // 复制按钮：完整公开页链接进剪贴板
    fireEvent.click(screen.getByTestId("share-copy"));
    expect(await screen.findByTestId("share-copied")).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith(expectedUrl);

    // 关闭弹层
    fireEvent.click(screen.getByTestId("share-dialog-close"));
    expect(screen.queryByTestId("share-dialog")).toBeNull();
  });

  it("失败态：服务端拒绝（如 503 未配置 secret）时后端 detail 如实呈现", async () => {
    const backend = installShareRows();
    vi.stubGlobal(
      "fetch",
      withShareEndpoint(backend, {
        status: 503,
        detail: "分享功能未配置（FD_WIRE_SHARE_SECRET 为空）",
      }),
    );

    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(within(rowOf(14)).getByTestId("artifact-share"));
    expect(await screen.findByTestId("share-error")).toBeTruthy();
    expect(screen.getByTestId("share-error").textContent).toContain(
      "分享功能未配置",
    );
    expect(screen.queryByTestId("share-url")).toBeNull();
  });

  // wire-share-ttl-options 任务 2.1：有效期三选一（7/30/90，缺省 30）——
  // 默认 POST body 带 ttl_days=30；改选 90 天即重新生成，body 带 ttl_days=90；
  // 到期时间生成中为本地预览、响应到达后以 expires_at 覆盖。
  it("有效期三选一：默认 30 天 body；改选 90 天重新生成，body 带 ttl_days=90", async () => {
    const backend = installShareRows();
    vi.stubGlobal("fetch", withShareEndpoint(backend));

    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    fireEvent.click(within(rowOf(14)).getByTestId("artifact-share"));
    expect(await screen.findByTestId("share-url")).toBeTruthy();

    // 三档单选（7/30/90）且默认选中 30
    const options = screen.getAllByTestId("share-ttl-option");
    expect(options.map((el) => el.getAttribute("data-days"))).toEqual([
      "7",
      "30",
      "90",
    ]);
    expect(options.map((el) => (el as HTMLInputElement).checked)).toEqual([
      false,
      true,
      false,
    ]);

    // 默认创建请求：POST body 携带 ttl_days=30
    const sharePosts = () =>
      backend.calls.filter(
        (c) =>
          c.method === "POST" && /\/artifacts\/14\/share$/.test(c.url.pathname),
      );
    expect(sharePosts()).toHaveLength(1);
    expect(sharePosts()[0]!.body).toEqual({ ttl_days: 30 });

    // 改选 90 天：触发重新生成（第二个 POST），body 带 ttl_days=90
    fireEvent.click(
      options.find((el) => el.getAttribute("data-days") === "90")!,
    );
    await waitFor(() => expect(sharePosts()).toHaveLength(2));
    expect(sharePosts()[1]!.body).toEqual({ ttl_days: 90 });
    // 到期时间以响应 expires_at 覆盖本地预览；链接来自新响应
    expect(
      (await screen.findByTestId("share-url") as HTMLInputElement).value,
    ).toContain("share-token-14");
    expect(screen.getByTestId("share-expires").textContent).toContain(
      formatServerDate("2026-11-10T08:00:00"),
    );
  });
});

// ── 新建看板（wire-dashboard-blank-create 任务 1.2）──────────────────────
// 契约：POST /api/v1/artifacts body {kind:"dashboard", title} → 201
// ArtifactDetail（draft v1，最小空板 spec）；成功后新行置顶并走既有行展开
// 直接进编辑器（startEditing）；422 detail 如实呈现；空标题前端先拦不发；
// 创建中禁用防重复提交。

describe("ArtifactsPage 新建看板（wire-dashboard-blank-create 1.2）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession({ sub: "demo-customer" });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  /** 打开新建表单（列表就绪后）。 */
  async function openCreateForm() {
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    fireEvent.click(screen.getByTestId("create-dashboard-open"));
    expect(screen.getByTestId("create-dashboard-form")).toBeTruthy();
  }

  it("创建流：标题输入 → POST {kind:'dashboard', title} → 新行置顶 + 直接进编辑器", async () => {
    const backend = install();
    await openCreateForm();

    fireEvent.change(screen.getByTestId("create-dashboard-title"), {
      target: { value: "免费档第一块板" },
    });
    fireEvent.click(screen.getByTestId("create-dashboard-submit"));

    // 创建即跳编辑器：既有行展开路径（dashboardFor）+ startEditing——
    // 编辑器直接可见，标题回填，空板提示在。
    expect(await screen.findByTestId("dashboard-editor")).toBeTruthy();
    expect(
      (screen.getByTestId("editor-dashboard-title") as HTMLInputElement).value,
    ).toBe("免费档第一块板");
    expect(screen.getByTestId("editor-empty-panels").textContent).toContain(
      "还没有面板",
    );

    // POST 载荷断言：kind 白名单 + 标题 + 登录 Bearer
    const post = backend.calls.find(
      (c) => c.method === "POST" && c.url.pathname === "/api/v1/artifacts",
    );
    expect(post).toBeTruthy();
    expect(post?.body).toEqual({ kind: "dashboard", title: "免费档第一块板" });
    expect(post?.headers["authorization"]).toMatch(/^Bearer /);

    // 新行置顶：draft 徽标 + v1 + 直接创建（无来源会话指针）；表单收起
    const items = screen.getAllByTestId("artifact-item");
    expect(items.length).toBe(4);
    const newRow = items[0]!;
    expect(newRow.getAttribute("data-artifact-id")).toBe("14");
    expect(
      within(newRow).getByTestId("artifact-status-badge").getAttribute("data-status"),
    ).toBe("draft");
    expect(within(newRow).getByTestId("artifact-version").textContent).toBe("v1");
    expect(
      within(newRow).getByTestId("artifact-source-none").textContent,
    ).toContain("直接创建");
    expect(screen.queryByTestId("create-dashboard-form")).toBeNull();
  });

  it("422 呈现：后端 detail 如实显示，表单保留可重试", async () => {
    install({
      failArtifactCreateStatus: 422,
      failArtifactCreateDetail: "产物数量已达上限（10 个）",
    });
    await openCreateForm();

    fireEvent.change(screen.getByTestId("create-dashboard-title"), {
      target: { value: "超限板" },
    });
    fireEvent.click(screen.getByTestId("create-dashboard-submit"));

    expect(
      (await screen.findByTestId("create-dashboard-error")).textContent,
    ).toContain("产物数量已达上限");
    // 表单不关、无新行、按钮恢复可重试
    expect(screen.getByTestId("create-dashboard-form")).toBeTruthy();
    expect(screen.getAllByTestId("artifact-item").length).toBe(3);
    expect(
      (screen.getByTestId("create-dashboard-submit") as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("空标题前端校验：空/纯空白不发 POST，提示后可修正再创建", async () => {
    const backend = install();
    await openCreateForm();

    // 空标题：前端先拦
    fireEvent.click(screen.getByTestId("create-dashboard-submit"));
    expect(screen.getByTestId("create-dashboard-error").textContent).toContain(
      "请输入看板标题",
    );
    // 纯空白标题：同样不发
    fireEvent.change(screen.getByTestId("create-dashboard-title"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByTestId("create-dashboard-submit"));
    expect(screen.getByTestId("create-dashboard-error").textContent).toContain(
      "请输入看板标题",
    );
    expect(
      backend.calls.filter(
        (c) => c.method === "POST" && c.url.pathname === "/api/v1/artifacts",
      ),
    ).toHaveLength(0);
    expect(screen.getAllByTestId("artifact-item").length).toBe(3);

    // 修正为合法标题后可正常创建（前端校验不阻塞后续流程）
    fireEvent.change(screen.getByTestId("create-dashboard-title"), {
      target: { value: "修正后的板" },
    });
    fireEvent.click(screen.getByTestId("create-dashboard-submit"));
    expect(await screen.findByTestId("dashboard-editor")).toBeTruthy();
  });

  it("防重复提交：创建中按钮禁用 + 仅一发；成功后表单收起进编辑器", async () => {
    const backend = install();
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const posts: string[] = [];
    const wrapped = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.pathname === "/api/v1/artifacts" && method === "POST") {
        posts.push(`${method} ${url.pathname}`);
        return gate.then(() => backend.fetchImpl(input, init));
      }
      return backend.fetchImpl(input, init);
    }) as unknown as typeof fetch;
    vi.stubGlobal("fetch", wrapped);

    await openCreateForm();
    fireEvent.change(screen.getByTestId("create-dashboard-title"), {
      target: { value: "防重复板" },
    });
    fireEvent.click(screen.getByTestId("create-dashboard-submit"));

    // 创建中：loading 文案 + 禁用，再点不产生第二个 POST
    expect(posts).toEqual(["POST /api/v1/artifacts"]);
    const submit = screen.getByTestId("create-dashboard-submit");
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    expect(submit.textContent).toContain("创建中");
    fireEvent.click(submit);
    expect(posts).toEqual(["POST /api/v1/artifacts"]);

    // 放行后：单次创建成功 → 表单收起、直接进编辑器
    release();
    expect(await screen.findByTestId("dashboard-editor")).toBeTruthy();
    expect(screen.queryByTestId("create-dashboard-form")).toBeNull();
  });
});

// ── 表单产物（wire-form-builder 任务 4.4）──────────────────────────────────
// 契约：POST /api/v1/artifacts {kind:"form", title} → 201 draft v1（最小空
// 表单信封）；kind=form 行展开 = FormRuntime（当前版本）+ 编辑器入口；
// published form 分享按钮照 dashboard 范式（url 直接用后端返回的
// /api/v1/share/form/…，展示链接构造 /#/share/form/{id}?t&v）。

/** 表单 spec 信封（绑定态：两字段 + 文案）。 */
const FORM_SPEC_BOUND = {
  title: "门店日报表",
  spec: {
    title: "门店日报表",
    datasetId: "ds-form-1",
    fields: [
      { column: "city", label: "城市", input: "text", required: true },
      { column: "amount", label: "金额", input: "number" },
    ],
    submitLabel: "上报",
    successMessage: "门店数据已收到",
  },
};

/** 供 corrections 提交落账的种子数据集。 */
const FORM_DATASET = {
  id: "ds-form-1",
  key: "ds_form_1",
  name: "门店日报",
  source_kind: "upload" as const,
  connector_id: null,
  source_ref: null,
  columns: [
    { name: "city", type: "TEXT" },
    { name: "amount", type: "REAL" },
  ],
  row_count: 5,
  size_bytes: 120,
  sync_cron: null,
  sync_status: "ok" as const,
  paused_reason: null,
  created_at: "2026-10-01T08:00:00",
  updated_at: "2026-10-01T08:00:00",
};

/** 在 install() 三产物之上补 form 行：16 draft（绑定态）、17 published。 */
function installFormRows() {
  const backend = installConsoleBackend(CONVERSATIONS, {
    datasets: [FORM_DATASET],
  });
  const seeds: LifecycleArtifactSeed[] = [
    { id: 11, kind: "dashboard", title: "宏观仪表盘", status: "draft", version: 1, origin_conversation_id: 1, spec: V1_DASH, created_at: "2026-10-09T09:00:00", updated_at: "2026-10-10T09:00:00" },
    { id: 12, kind: "alert_rule", title: "煤价周环比 >5% 告警（放宽到 8%）", status: "published", version: 2, origin_conversation_id: 2, versions: [{ spec: ALERT_V1 }, { spec: ALERT_V2 }], created_at: "2026-10-09T10:00:00", updated_at: "2026-10-10T10:00:00" },
    { id: 13, kind: "refresh_schedule", title: "每日 08:00 刷新", status: "archived", version: 1, origin_conversation_id: null, spec: { title: "每日 08:00 刷新", spec: { cron: "0 8 * * *", timezone: "Asia/Shanghai" } }, created_at: "2026-10-09T11:00:00", updated_at: "2026-10-10T11:00:00" },
  ];
  for (const seed of seeds) {
    backend.addLifecycleArtifact(seed);
  }
  backend.addLifecycleArtifact({
    id: 16,
    kind: "form",
    title: "门店日报表",
    status: "draft",
    version: 1,
    origin_conversation_id: null,
    spec: FORM_SPEC_BOUND,
    created_at: "2026-10-11T09:00:00",
    updated_at: "2026-10-11T09:30:00",
  });
  backend.addLifecycleArtifact({
    id: 17,
    kind: "form",
    title: "访客登记表",
    status: "published",
    version: 1,
    origin_conversation_id: null,
    spec: FORM_SPEC_BOUND,
    created_at: "2026-10-11T10:00:00",
    updated_at: "2026-10-11T10:00:00",
  });
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

/** 包装假后端：拦截 form share POST（published form 17 合法 →
 *  {url:"/api/v1/share/form/…", expires_at, version}，其余 403），透传其余。 */
function withFormShareEndpoint(
  backend: ReturnType<typeof installConsoleBackend>,
): typeof fetch {
  const original = backend.fetchImpl;
  return ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const match = url.pathname.match(/^\/api\/v1\/artifacts\/(\d+)\/share$/);
    if (match && (init?.method ?? "GET").toUpperCase() === "POST") {
      const id = Number(match[1]);
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
      backend.calls.push({
        method: "POST",
        url,
        headers,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      if (id !== 17) {
        return Promise.resolve(
          localJson(
            { detail: `产物 ${id} 不允许生成分享链接（仅已发布表单可分享）` },
            403,
          ),
        );
      }
      return Promise.resolve(
        localJson({
          url: `/api/v1/share/form/17?t=form-token-17&v=1`,
          expires_at: "2026-11-10T08:00:00",
          version: 1,
        }),
      );
    }
    return original(input, init);
  }) as unknown as typeof fetch;
}

describe("ArtifactsPage 表单产物（wire-form-builder 4.4）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession({ sub: "demo-customer" });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("kind 徽标与筛选：form 行呈中文徽标，kind=form 筛选收窄到两行", async () => {
    installFormRows();
    render(<ArtifactsPage />);

    const row16 = await waitFor(() => rowOf(16));
    expect(
      within(row16).getByTestId("artifact-kind-badge").textContent,
    ).toContain("表单");
    expect(
      within(row16).getByTestId("artifact-kind-badge").getAttribute("data-kind"),
    ).toBe("form");

    fireEvent.change(screen.getByTestId("artifact-kind-filter"), {
      target: { value: "form" },
    });
    expect(screen.getAllByTestId("artifact-item").length).toBe(2);
  });

  it("新建表单入口：与新建看板并列；标题 → POST {kind:'form', title} → 置顶 + 直接进构建器（免费档路径：零 cell 零 AI 出生）", async () => {
    const backend = installFormRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // 入口与新建看板并列
    fireEvent.click(screen.getByTestId("create-form-open"));
    expect(screen.getByTestId("create-form-form")).toBeTruthy();

    fireEvent.change(screen.getByTestId("create-form-title"), {
      target: { value: "门店巡检表" },
    });
    fireEvent.click(screen.getByTestId("create-form-submit"));

    // 创建即跳构建器：FormPane 展开且直接进 FormEditor
    expect(await screen.findByTestId("form-editor")).toBeTruthy();

    const post = backend.calls.find(
      (c) => c.method === "POST" && c.url.pathname === "/api/v1/artifacts",
    );
    expect(post).toBeTruthy();
    expect(post?.body).toEqual({ kind: "form", title: "门店巡检表" });
    expect(post?.headers["authorization"]).toMatch(/^Bearer /);

    // 新行置顶：form 徽标 + draft v1 + 直接创建；表单收起
    const items = screen.getAllByTestId("artifact-item");
    expect(items.length).toBe(6);
    const newRow = items[0]!;
    expect(newRow.getAttribute("data-artifact-id")).toBe("18");
    expect(
      within(newRow).getByTestId("artifact-kind-badge").getAttribute("data-kind"),
    ).toBe("form");
    expect(within(newRow).getByTestId("artifact-version").textContent).toBe("v1");
    expect(
      within(newRow).getByTestId("artifact-source-none").textContent,
    ).toContain("直接创建");
    expect(screen.queryByTestId("create-form-form")).toBeNull();
  });

  it("新建告警规则入口：标题 → POST {kind:'alert_rule'} → 置顶 + 直接进 spec 编辑器（模板壳）", async () => {
    const backend = installFormRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(screen.getByTestId("create-alert-open"));
    expect(screen.getByTestId("create-alert-form")).toBeTruthy();

    fireEvent.change(screen.getByTestId("create-alert-title"), {
      target: { value: "行数监控" },
    });
    fireEvent.click(screen.getByTestId("create-alert-submit"));

    // 创建即跳 spec 编辑器（SpecViewer 展开显示模板壳）
    expect(await screen.findByTestId("spec-editor")).toBeTruthy();

    const post = backend.calls.find(
      (c) => c.method === "POST" && c.url.pathname === "/api/v1/artifacts",
    );
    expect(post?.body).toEqual({ kind: "alert_rule", title: "行数监控" });

    // 新行置顶：alert_rule 徽标 + draft v1；表单收起
    const items = screen.getAllByTestId("artifact-item");
    const newRow = items[0]!;
    expect(
      within(newRow).getByTestId("artifact-kind-badge").getAttribute("data-kind"),
    ).toBe("alert_rule");
    expect(within(newRow).getByTestId("artifact-version").textContent).toBe("v1");
    expect(screen.queryByTestId("create-alert-form")).toBeNull();
  });

  it("新建表单空标题前端先拦：不发 POST、提示后可修正再创建", async () => {
    const backend = installFormRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(screen.getByTestId("create-form-open"));
    fireEvent.click(screen.getByTestId("create-form-submit"));
    expect(screen.getByTestId("create-form-error").textContent).toContain(
      "请输入表单标题",
    );
    expect(
      backend.calls.filter(
        (c) => c.method === "POST" && c.url.pathname === "/api/v1/artifacts",
      ),
    ).toHaveLength(0);

    fireEvent.change(screen.getByTestId("create-form-title"), {
      target: { value: "修正后的表" },
    });
    fireEvent.click(screen.getByTestId("create-form-submit"));
    expect(await screen.findByTestId("form-editor")).toBeTruthy();
  });

  it("form 行展开 = FormRuntime（当前版本字段）+ 编辑入口；console 提交落 corrections", async () => {
    const backend = installFormRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(within(rowOf(16)).getByTestId("artifact-form-view"));
    const pane = await screen.findByTestId("form-pane");
    // 运行时按当前版本 spec 渲染（字段/自定义提交文案）
    expect(within(pane).getAllByTestId("form-field").length).toBe(2);
    expect(within(pane).getByTestId("form-submit").textContent).toContain("上报");

    // console 试填提交：corrections insert 落修正层（op=insert + 归一 values）
    fireEvent.change(
      within(pane).getAllByTestId("form-field-input")[0] as HTMLInputElement,
      { target: { value: "上海" } },
    );
    fireEvent.change(
      within(pane).getAllByTestId("form-field-input")[1] as HTMLInputElement,
      { target: { value: "12.5" } },
    );
    fireEvent.click(within(pane).getByTestId("form-submit"));
    expect(await within(pane).findByTestId("form-success")).toBeTruthy();
    const correction = backend.getCorrections("ds-form-1")[0];
    expect(correction?.op).toBe("insert");
    expect(correction?.values).toEqual({ city: "上海", amount: 12.5 });

    // 编辑入口：进 FormEditor（标题回填产物标题）
    fireEvent.click(within(pane).getByTestId("form-edit"));
    expect(await screen.findByTestId("form-editor")).toBeTruthy();
    expect(
      (screen.getByTestId("editor-form-title") as HTMLInputElement).value,
    ).toBe("门店日报表");
  });

  it("published form 分享按钮照 dashboard 范式：url 用后端 /api/v1/share/form/…，展示链接 /#/share/form/{id}?t&v", async () => {
    const backend = installFormRows();
    vi.stubGlobal("fetch", withFormShareEndpoint(backend));
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // 入口分发：published form（17）有、draft form（16）无
    expect(within(rowOf(17)).getByTestId("artifact-share")).toBeTruthy();
    expect(within(rowOf(16)).queryByTestId("artifact-share")).toBeNull();

    fireEvent.click(within(rowOf(17)).getByTestId("artifact-share"));
    expect(await screen.findByTestId("share-dialog")).toBeTruthy();

    // 请求带登录 Bearer；展示链接 = origin + #/share/form/17?t=<响应 url 解出的 t>&v=<响应 version>
    const post = backend.calls.find(
      (c) =>
        c.method === "POST" && /\/artifacts\/17\/share$/.test(c.url.pathname),
    );
    expect(post).toBeTruthy();
    expect(post?.headers["authorization"]).toMatch(/^Bearer /);
    expect(
      (await screen.findByTestId("share-url") as HTMLInputElement).value,
    ).toBe(
      `${window.location.origin}/#/share/form/17?t=form-token-17&v=1`,
    );
    expect(screen.getByTestId("share-expires").textContent).toContain(
      formatServerDate("2026-11-10T08:00:00"),
    );
  });
});

// ── 告警执行面（wire-alert-evaluation 任务 5）──────────────────────────────
// 契约：GET /api/v1/alerts/states 一次性 map 驱动 alert_rule 行徽标四态
// （firing 红 / ok 绿 / ok+最近错误琥珀 / 无键灰「未评估」）+ 最近评估时间；
// 行展开附「告警事件」小节（最近 10 条 + 手动刷新）；「试运行」POST
// /artifacts/{id}/alert/test 零副作用——成功展示归约值与触发判定，失败
// 后端 detail 如实红条。非 alert_rule 行零变化；states 拉取失败静默降级。

/** 在 install() 三产物（11 dashboard / 12 alert_rule / 13 refresh）之上补
 *  四条 alert_rule 行：21 firing / 22 ok / 23 ok+最近错误 / 24 无键（未评估）。 */
function installAlertRows(options: ConsoleBackendOptions = {}) {
  const backend = install(options);
  for (const id of [21, 22, 23, 24]) {
    backend.addLifecycleArtifact({
      id,
      kind: "alert_rule",
      title: `告警规则 ${id}`,
      status: "published",
      version: 1,
      origin_conversation_id: null,
      spec: { title: `告警规则 ${id}`, spec: { indicator: "CN_COAL_PRICE", threshold: 0.05 } },
      created_at: "2026-10-11T09:00:00",
      updated_at: "2026-10-11T09:00:00",
    });
  }
  return backend;
}

/** 覆盖徽标全态的 states 种子：12/21 firing、22 ok、23 ok+最近错误、24 无键。 */
const ALERT_STATES_SEED: NonNullable<ConsoleBackendOptions["alertStates"]> = {
  "12": { state: "firing", last_value: 0.09, last_evaluated_at: "2026-10-12T08:00:00", last_error_at: null },
  "21": { state: "firing", last_value: 3.2, last_evaluated_at: "2026-10-12T08:05:00", last_error_at: null },
  "22": { state: "ok", last_value: 0.01, last_evaluated_at: "2026-10-12T08:10:00", last_error_at: null },
  "23": { state: "ok", last_value: 0.02, last_evaluated_at: "2026-10-12T08:15:00", last_error_at: "2026-10-11T22:00:00" },
};

describe("ArtifactsPage 告警执行面（wire-alert-evaluation 5）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession({ sub: "demo-customer" });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("状态徽标四态：firing 红「已触发」/ ok 绿「正常」/ 错误琥珀「错误」/ 无键灰「未评估」+ 最近评估时间；非 alert_rule 行无徽标", async () => {
    installAlertRows({ alertStates: ALERT_STATES_SEED });
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    const badgeOf = async (id: number) =>
      within(rowOf(id)).findByTestId("alert-state-badge");

    // firing：红「已触发」
    const firing = await badgeOf(21);
    expect(firing.getAttribute("data-state")).toBe("firing");
    expect(firing.textContent).toContain("已触发");
    expect(firing.className).toContain("text-red-700");

    // ok：绿「正常」
    const ok = await badgeOf(22);
    expect(ok.getAttribute("data-state")).toBe("ok");
    expect(ok.textContent).toContain("正常");
    expect(ok.className).toContain("text-emerald-700");

    // ok + 最近错误：琥珀「错误」（title 带最近出错时间）
    const errored = await badgeOf(23);
    expect(errored.getAttribute("data-state")).toBe("error");
    expect(errored.textContent).toBe("错误");
    expect(errored.className).toContain("text-amber-700");
    expect(errored.getAttribute("title")).toContain(
      `错误（最近 ${formatServerDate("2026-10-11T22:00:00")}）`,
    );

    // 无键（从未评估）：灰「未评估」
    const unevaluated = await badgeOf(24);
    expect(unevaluated.getAttribute("data-state")).toBe("unevaluated");
    expect(unevaluated.textContent).toContain("未评估");
    expect(unevaluated.className).toContain("text-slate-600");

    // 最近评估时间：照页内 formatServerDate 口径
    const evaluated = await within(rowOf(21)).findByTestId("alert-last-evaluated");
    expect(evaluated.textContent).toContain(
      formatServerDate("2026-10-12T08:05:00"),
    );
    // 未评估行无最近评估时间
    expect(within(rowOf(24)).queryByTestId("alert-last-evaluated")).toBeNull();

    // 非 alert_rule 行（11 dashboard / 13 refresh）：零变化——无徽标无评估时间
    expect(within(rowOf(11)).queryByTestId("alert-state-badge")).toBeNull();
    expect(within(rowOf(13)).queryByTestId("alert-state-badge")).toBeNull();
    expect(within(rowOf(11)).queryByTestId("alert-last-evaluated")).toBeNull();
  });

  it("详情区告警事件：展开 alert_rule 行呈现最近 10 条（kind 徽标/值/阈值/时间/detail）；手动刷新重拉", async () => {
    const backend = installAlertRows({
      alertStates: ALERT_STATES_SEED,
      alertEvents: [
        { id: 101, artifact_id: 12, kind: "firing", value: 0.09, threshold: 0.08, detail: "周环比 9.0% ≥ 8.0%", created_at: "2026-10-12T08:00:00" },
        { id: 100, artifact_id: 12, kind: "resolved", value: 0.07, threshold: 0.08, detail: null, created_at: "2026-10-11T08:00:00" },
        { id: 99, artifact_id: 22, kind: "error", value: null, threshold: null, detail: "查询失败", created_at: "2026-10-10T08:00:00" },
      ],
    });
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    openViewer(12);
    const pane = await screen.findByTestId("alert-pane");

    // 仅本产物事件、id 倒序；请求带 artifact_id=12 & limit=10
    const eventsCalls = () =>
      backend.calls.filter((c) => c.url.pathname === "/api/v1/alerts/events");
    expect(eventsCalls()).toHaveLength(1);
    expect(eventsCalls()[0]!.url.searchParams.get("artifact_id")).toBe("12");
    expect(eventsCalls()[0]!.url.searchParams.get("limit")).toBe("10");
    const items = await within(pane).findAllByTestId("alert-event-item");
    expect(items.map((el) => el.getAttribute("data-event-id"))).toEqual([
      "101",
      "100",
    ]);

    // 行内容：kind 徽标（中文）+ 值/阈值 + 时间 + detail 摘要
    const firingRow = items[0]!;
    expect(
      within(firingRow).getByTestId("alert-event-kind").textContent,
    ).toContain("已触发");
    expect(
      within(firingRow).getByTestId("alert-event-kind").getAttribute("data-kind"),
    ).toBe("firing");
    expect(firingRow.textContent).toContain("0.09");
    expect(firingRow.textContent).toContain("0.08");
    expect(firingRow.textContent).toContain(formatServerDate("2026-10-12T08:00:00"));
    expect(
      within(firingRow).getByTestId("alert-event-detail").textContent,
    ).toContain("周环比 9.0% ≥ 8.0%");
    expect(within(items[1]!).getByTestId("alert-event-kind").textContent).toContain(
      "已恢复",
    );

    // 手动刷新：追加事件后重拉可见（notify_failed 灰徽标）
    backend.addAlertEvent({
      id: 102,
      artifact_id: 12,
      kind: "notify_failed",
      value: 0.09,
      threshold: 0.08,
      detail: "webhook 通知失败",
    });
    fireEvent.click(within(pane).getByTestId("alert-events-refresh"));
    const refreshed = await waitFor(() => {
      const rows = within(pane).getAllByTestId("alert-event-item");
      expect(rows.map((el) => el.getAttribute("data-event-id"))).toEqual([
        "102",
        "101",
        "100",
      ]);
      return rows;
    });
    expect(
      within(refreshed[0]!).getByTestId("alert-event-kind").textContent,
    ).toContain("通知失败");
    expect(eventsCalls()).toHaveLength(2);
  });

  it("试运行：成功展示「归约值 X · 将触发」；请求 POST /artifacts/{id}/alert/test 带登录 Bearer", async () => {
    const backend = installAlertRows({
      alertStates: ALERT_STATES_SEED,
      alertTest: { value: 0.09, would_fire: true },
    });
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    openViewer(12);
    fireEvent.click(await screen.findByTestId("alert-test-button"));

    expect(await screen.findByTestId("alert-test-result")).toBeTruthy();
    expect(screen.getByTestId("alert-test-result").textContent).toContain(
      "归约值 0.09",
    );
    expect(screen.getByTestId("alert-test-result").textContent).toContain(
      "将触发",
    );
    expect(screen.queryByTestId("alert-test-error")).toBeNull();

    const post = backend.calls.find(
      (c) =>
        c.method === "POST" &&
        c.url.pathname === "/api/v1/artifacts/12/alert/test",
    );
    expect(post).toBeTruthy();
    expect(post?.headers["authorization"]).toMatch(/^Bearer /);
    // 试运行零副作用：不写 states / 不落事件
    expect(Object.keys(backend.getAlertStates())).toEqual(
      Object.keys(ALERT_STATES_SEED),
    );
    expect(backend.getAlertEvents()).toHaveLength(0);
  });

  it("试运行失败：后端 detail（如 400 查询失败）如实红条呈现", async () => {
    installAlertRows({
      alertStates: ALERT_STATES_SEED,
      failAlertTestStatus: 400,
      failAlertTestDetail: "查询失败：数据集 ds-x 不存在",
    });
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    openViewer(12);
    fireEvent.click(await screen.findByTestId("alert-test-button"));

    expect(await screen.findByTestId("alert-test-error")).toBeTruthy();
    expect(screen.getByTestId("alert-test-error").textContent).toContain(
      "查询失败：数据集 ds-x 不存在",
    );
    expect(screen.queryByTestId("alert-test-result")).toBeNull();
  });

  it("非 alert_rule 列表：不发 states 请求、行内无告警面；展开也不渲染事件小节", async () => {
    const backend = installConsoleBackend(CONVERSATIONS);
    backend.addLifecycleArtifact({
      id: 11,
      kind: "dashboard",
      title: "宏观仪表盘",
      status: "draft",
      version: 1,
      origin_conversation_id: 1,
      spec: V1_DASH,
      created_at: "2026-10-09T09:00:00",
      updated_at: "2026-10-10T09:00:00",
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // 一条 alert_rule 都没有 → 不请求 GET /api/v1/alerts/states
    expect(
      backend.calls.some((c) => c.url.pathname === "/api/v1/alerts/states"),
    ).toBe(false);
    expect(within(rowOf(11)).queryByTestId("alert-state-badge")).toBeNull();

    // 展开非 alert_rule 行：无告警事件小节
    openViewer(11);
    expect(await screen.findByTestId("artifact-viewer")).toBeTruthy();
    expect(screen.queryByTestId("alert-pane")).toBeNull();
  });

  it("states 拉取失败：静默降级为全「未评估」，页面不阻塞（无错误条、列表照常）", async () => {
    installAlertRows({ failAlertStatesStatus: 500 });
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // 降级：所有 alert_rule 行（含 states 有种的 12）一律「未评估」
    const badge = await within(rowOf(12)).findByTestId("alert-state-badge");
    expect(badge.getAttribute("data-state")).toBe("unevaluated");
    expect(badge.textContent).toContain("未评估");
    expect(
      (await within(rowOf(21)).findByTestId("alert-state-badge"))
        .getAttribute("data-state"),
    ).toBe("unevaluated");

    // 页面照常：无全局错误条、三行俱在、评估时间不渲染
    expect(screen.queryByTestId("artifacts-error")).toBeNull();
    expect(screen.getAllByTestId("artifact-item").length).toBe(7);
    expect(within(rowOf(12)).queryByTestId("alert-last-evaluated")).toBeNull();
  });
});

describe("api client 翻译层（wire-alert-evaluation：snake→camel + 信封形状）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession({ sub: "demo-customer" });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  /** 单条 alert_rule 种子（12）：states/events/test 断言的对象产物。 */
  function seedAlertArtifact(
    backend: ReturnType<typeof installConsoleBackend>,
  ) {
    backend.addLifecycleArtifact({
      id: 12,
      kind: "alert_rule",
      title: "煤价周环比 >5% 告警（放宽到 8%）",
      status: "published",
      version: 2,
      origin_conversation_id: 2,
      versions: [{ spec: ALERT_V1 }, { spec: ALERT_V2 }],
      created_at: "2026-10-09T10:00:00",
      updated_at: "2026-10-10T10:00:00",
    });
  }

  it("getAlertStates：states 键保持 artifact_id 字符串、值译 {state,lastValue,lastEvaluatedAt,lastErrorAt}", async () => {
    const backend = installConsoleBackend(CONVERSATIONS, {
      alertStates: {
        "12": {
          state: "ok",
          last_value: 0.03,
          last_evaluated_at: "2026-10-12T08:00:00",
          last_error_at: "2026-10-11T22:00:00",
        },
        "13": { state: "firing", last_value: 9.9, last_evaluated_at: "2026-10-12T07:00:00" },
      },
    });
    seedAlertArtifact(backend);
    vi.stubGlobal("fetch", backend.fetchImpl);

    const states = await getAlertStates();
    // 键 13 无对应 alert_rule 产物 → 服务端不放行（真实语义：只回规则行）
    expect(Object.keys(states)).toEqual(["12"]);
    expect(states["12"]).toEqual({
      state: "ok",
      lastValue: 0.03,
      lastEvaluatedAt: "2026-10-12T08:00:00",
      lastErrorAt: "2026-10-11T22:00:00",
    });

    const call = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/alerts/states",
    )!;
    expect(call.method).toBe("GET");
    expect(call.headers["authorization"]).toMatch(/^Bearer /);
  });

  it("getAlertEvents：artifact_id/limit 进 query；{events:[…]} 信封解包并译 camelCase（kind 四枚举/null 如实）", async () => {
    const backend = installConsoleBackend(CONVERSATIONS, {
      alertEvents: [
        { id: 103, artifact_id: 12, kind: "notify_failed", value: 0.09, threshold: 0.08, detail: "webhook 通知失败", created_at: "2026-10-12T09:00:00" },
        { id: 102, artifact_id: 12, kind: "error", value: null, threshold: null, detail: "查询失败：列不存在", created_at: "2026-10-12T08:00:00" },
        { id: 101, artifact_id: 12, kind: "resolved", value: 0.07, threshold: 0.08, detail: null, created_at: "2026-10-11T08:00:00" },
        { id: 99, artifact_id: 12, kind: "firing", value: 0.09, threshold: 0.08, detail: null, created_at: "2026-10-10T08:00:00" },
        { id: 98, artifact_id: 22, kind: "firing", value: 1.0, threshold: 0.5, created_at: "2026-10-09T08:00:00" },
      ],
    });
    seedAlertArtifact(backend);
    vi.stubGlobal("fetch", backend.fetchImpl);

    const events = await getAlertEvents(12, 10);
    // 仅 artifact_id=12、id 倒序（服务端口径）、camelCase 译名
    expect(events.map((e) => e.id)).toEqual([103, 102, 101, 99]);
    expect(events[0]).toEqual({
      id: 103,
      artifactId: 12,
      kind: "notify_failed",
      value: 0.09,
      threshold: 0.08,
      detail: "webhook 通知失败",
      createdAt: "2026-10-12T09:00:00",
    });
    // null 字段如实保留为 null
    expect(events[1]).toMatchObject({ kind: "error", value: null, threshold: null });
    expect(events[2]).toMatchObject({ kind: "resolved", detail: null });

    const call = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/alerts/events",
    )!;
    expect(call.method).toBe("GET");
    expect(call.url.searchParams.get("artifact_id")).toBe("12");
    expect(call.url.searchParams.get("limit")).toBe("10");
    expect(call.headers["authorization"]).toMatch(/^Bearer /);
  });

  it("testAlertRule：POST /artifacts/{id}/alert/test，would_fire 译 wouldFire；422 detail 上抛 ConsoleApiError", async () => {
    const backend = installConsoleBackend(CONVERSATIONS, {
      alertTest: { value: 0.1234, would_fire: true },
    });
    seedAlertArtifact(backend);
    vi.stubGlobal("fetch", backend.fetchImpl);

    const result = await testAlertRule(12);
    expect(result).toEqual({ value: 0.1234, wouldFire: true });
    const post = backend.calls.find(
      (c) =>
        c.method === "POST" &&
        c.url.pathname === "/api/v1/artifacts/12/alert/test",
    )!;
    expect(post).toBeTruthy();
    expect(post.headers["authorization"]).toMatch(/^Bearer /);

    // 失败语义走既有 ConsoleApiError（422 spec 非法 detail 如实）
    const failing = installConsoleBackend(CONVERSATIONS, {
      failAlertTestStatus: 422,
      failAlertTestDetail: "spec 非法：threshold 必须 ≥ 0",
    });
    seedAlertArtifact(failing);
    vi.stubGlobal("fetch", failing.fetchImpl);
    const err = await testAlertRule(12).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ConsoleApiError);
    expect((err as ConsoleApiError).status).toBe(422);
    expect((err as ConsoleApiError).detail).toContain("threshold 必须 ≥ 0");
  });
});

describe("ArtifactsPage（默认看板置顶：wire-personal-preferences）", () => {
  /** 容器替身：持有置顶态、onPinnedChange 即更新（模拟 ConsoleView 的
   *  偏好态回写——徽标随 PUT 成功即时移动）。 */
  function PinHarness(props: { initialDashboardId?: number | null }) {
    const [pinned, setPinned] = useState<number | null>(null);
    return (
      <ArtifactsPage
        pinnedDashboardId={pinned}
        onPinnedChange={setPinned}
        initialDashboardId={props.initialDashboardId ?? null}
      />
    );
  }

  /** 在既有种子（11 dashboard / 12 alert / 13 refresh）上追加：14 已发布
   *  看板（可置顶）+ 15 已归档看板（422 用例）。 */
  const installPinnable = () => {
    const backend = install();
    backend.addLifecycleArtifact({
      id: 14,
      kind: "dashboard",
      title: "煤价看板",
      status: "published",
      version: 2,
      origin_conversation_id: null,
      spec: { title: "煤价看板", spec: { title: "煤价看板", panels: [] } },
      created_at: "2026-10-09T12:00:00",
      updated_at: "2026-10-10T12:00:00",
    });
    backend.addLifecycleArtifact({
      id: 15,
      kind: "dashboard",
      title: "旧看板",
      status: "archived",
      version: 1,
      origin_conversation_id: null,
      spec: { title: "旧看板", spec: { title: "旧看板", panels: [] } },
      created_at: "2026-10-09T13:00:00",
      updated_at: "2026-10-10T13:00:00",
    });
    return backend;
  };

  const preferencePuts = (
    backend: ReturnType<typeof installConsoleBackend>,
  ) =>
    backend.calls.filter(
      (c) =>
        c.url.pathname === "/api/console/me/preferences" && c.method === "PUT",
    );

  it("置顶/改置/取消：PUT pinned_dashboard_id 逐次写入，「默认」徽标即时移动", async () => {
    const backend = installPinnable();
    render(<PinHarness />);
    await screen.findAllByTestId("artifact-item");

    // 初始未置顶：无徽标，三块看板行各带「设为默认看板」。
    expect(screen.queryByTestId("artifact-dashboard-default")).toBeNull();
    expect(within(rowOf(11)).getByTestId("artifact-dashboard-pin")).toBeTruthy();

    // 置顶 11：PUT {pinned_dashboard_id: 11}，徽标移入该行（unpin 入场）。
    fireEvent.click(within(rowOf(11)).getByTestId("artifact-dashboard-pin"));
    await waitFor(() => {
      expect(preferencePuts(backend)).toEqual([
        expect.objectContaining({ body: { pinned_dashboard_id: 11 } }),
      ]);
    });
    expect(backend.getPreferences("demo-customer").pinned_dashboard_id).toBe(11);
    expect(
      within(rowOf(11)).getByTestId("artifact-dashboard-default").textContent,
    ).toBe("默认");
    expect(
      within(rowOf(11)).getByTestId("artifact-dashboard-unpin").textContent,
    ).toBe("取消默认看板");

    // 改置 14：徽标移动到新行。
    fireEvent.click(within(rowOf(14)).getByTestId("artifact-dashboard-pin"));
    await waitFor(() =>
      expect(
        within(rowOf(14)).getByTestId("artifact-dashboard-default"),
      ).toBeTruthy(),
    );
    expect(
      within(rowOf(11)).queryByTestId("artifact-dashboard-default"),
    ).toBeNull();

    // 取消置顶：PUT null，徽标退场、pin 按钮回来。
    fireEvent.click(within(rowOf(14)).getByTestId("artifact-dashboard-unpin"));
    await waitFor(() =>
      expect(screen.queryByTestId("artifact-dashboard-default")).toBeNull(),
    );
    expect(backend.getPreferences("demo-customer").pinned_dashboard_id).toBeNull();
    expect(preferencePuts(backend).map((c) => c.body)).toEqual([
      { pinned_dashboard_id: 11 },
      { pinned_dashboard_id: 14 },
      { pinned_dashboard_id: null },
    ]);
  });

  it("422（置顶已归档看板）：行内红字如实呈现，徽标不动、假后端态不污染", async () => {
    const backend = installPinnable();
    render(<PinHarness />);
    await screen.findAllByTestId("artifact-item");

    // 先成功置顶 11（徽标在位）。
    fireEvent.click(within(rowOf(11)).getByTestId("artifact-dashboard-pin"));
    await waitFor(() =>
      expect(
        within(rowOf(11)).getByTestId("artifact-dashboard-default"),
      ).toBeTruthy(),
    );

    // 置顶已归档看板 15 → 假后端 422（归档/失存/跨客户同文案）。
    fireEvent.click(within(rowOf(15)).getByTestId("artifact-dashboard-pin"));
    const error = await screen.findByTestId("artifact-pin-error");
    expect(error.textContent).toContain("置顶看板不存在或不可用");

    // 徽标不动（仍 11）+ 失败写不落库（PUT 三键语义：仅成功写生效）。
    expect(
      within(rowOf(11)).getByTestId("artifact-dashboard-default"),
    ).toBeTruthy();
    expect(
      within(rowOf(15)).queryByTestId("artifact-dashboard-default"),
    ).toBeNull();
    expect(backend.getPreferences("demo-customer").pinned_dashboard_id).toBe(11);
  });

  it("initialDashboardId：挂载即展开该看板；无该 prop 的重挂载不自动展开", async () => {
    installPinnable();
    const { unmount } = render(<PinHarness initialDashboardId={11} />);
    // 置顶落地跳转：不点「查看看板」即直接展开。
    expect(await screen.findByTestId("dashboard-pane")).toBeTruthy();
    unmount();
    cleanup();

    // 普通重挂载（无 initialDashboardId）：不重复自动展开。
    installPinnable();
    render(<PinHarness />);
    await screen.findAllByTestId("artifact-item");
    expect(screen.queryByTestId("dashboard-pane")).toBeNull();
  });
});

// ── 富文本文档（wire-rich-text-docs 任务 4.2/4.3/4.5）──────────────────────
// 契约：doc 行徽标（琥珀）+ 「打开文档」行展开挂 DocEditor（分栏编辑器）；
// published doc 行出分享入口（doc 专属极简弹层）；「新建文档」入口 → POST
// {kind:"doc", title} → 置顶 + 直接进编辑器；published doc **不渲染**可见性
// 设置区（design D5：doc 不进市场——服务端 422 是兜底，UI 不给假入口）。

const DOC_V1 = {
  title: "季度经营说明",
  spec: { format: "markdown", text: "# 正文\n\n第一版。" },
};

/** 在 install() 三产物之上补 doc 行：21 draft、22 published。 */
function installDocRows() {
  const backend = installConsoleBackend(CONVERSATIONS, {});
  const seeds: LifecycleArtifactSeed[] = [
    { id: 11, kind: "dashboard", title: "宏观仪表盘", status: "draft", version: 1, origin_conversation_id: 1, spec: V1_DASH, created_at: "2026-10-09T09:00:00", updated_at: "2026-10-10T09:00:00" },
    { id: 12, kind: "alert_rule", title: "煤价周环比 >5% 告警（放宽到 8%）", status: "published", version: 2, origin_conversation_id: 2, versions: [{ spec: ALERT_V1 }, { spec: ALERT_V2 }], created_at: "2026-10-09T10:00:00", updated_at: "2026-10-10T10:00:00" },
    { id: 13, kind: "refresh_schedule", title: "每日 08:00 刷新", status: "archived", version: 1, origin_conversation_id: null, spec: { title: "每日 08:00 刷新", spec: { cron: "0 8 * * *", timezone: "Asia/Shanghai" } }, created_at: "2026-10-09T11:00:00", updated_at: "2026-10-10T11:00:00" },
  ];
  for (const seed of seeds) backend.addLifecycleArtifact(seed);
  backend.addLifecycleArtifact({
    id: 21,
    kind: "doc",
    title: "季度经营说明",
    status: "draft",
    version: 1,
    origin_conversation_id: null,
    spec: DOC_V1,
    created_at: "2026-10-11T09:00:00",
    updated_at: "2026-10-11T09:30:00",
  });
  // published 文档：可见性区若误挂载会发 GET /artifacts/22/visibility——
  // 页面测试钉死「不发该请求」（D5 隐藏证据）。
  backend.addLifecycleArtifact({
    id: 22,
    kind: "doc",
    title: "已发布文档",
    status: "published",
    version: 1,
    origin_conversation_id: null,
    spec: DOC_V1,
    created_at: "2026-10-11T10:00:00",
    updated_at: "2026-10-11T10:00:00",
  });
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

/** 包装假后端：拦截 doc share POST（22 合法 → {url:"/api/v1/share/doc/…",
 *  expires_at, version}，其余 403），透传其余（照 form 分享测试范式）。 */
function withDocShareEndpoint(
  backend: ReturnType<typeof installConsoleBackend>,
): typeof fetch {
  const original = backend.fetchImpl;
  return ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const match = url.pathname.match(/^\/api\/v1\/artifacts\/(\d+)\/share$/);
    if (match && (init?.method ?? "GET").toUpperCase() === "POST") {
      const id = Number(match[1]);
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
      backend.calls.push({
        method: "POST",
        url,
        headers,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      if (id !== 22) {
        return Promise.resolve(
          localJson(
            { detail: `产物 ${id} 不允许生成分享链接（仅已发布产物可分享）` },
            403,
          ),
        );
      }
      return Promise.resolve(
        localJson({
          url: `/api/v1/share/doc/22?t=doc-token-22&v=1`,
          expires_at: "2026-11-10T08:00:00",
          version: 1,
        }),
      );
    }
    return original(input, init);
  }) as unknown as typeof fetch;
}

describe("ArtifactsPage 富文本文档（wire-rich-text-docs 4.2/4.3/4.5）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession({ sub: "demo-customer" });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("doc 行徽标与筛选：呈「文档」徽标（data-kind=doc），kind=doc 筛选收窄到两行", async () => {
    installDocRows();
    render(<ArtifactsPage />);

    const row21 = await waitFor(() => rowOf(21));
    expect(
      within(row21).getByTestId("artifact-kind-badge").textContent,
    ).toContain("文档");
    expect(
      within(row21).getByTestId("artifact-kind-badge").getAttribute("data-kind"),
    ).toBe("doc");

    fireEvent.change(screen.getByTestId("artifact-kind-filter"), {
      target: { value: "doc" },
    });
    expect(screen.getAllByTestId("artifact-item").length).toBe(2);
  });

  it("doc 行展开 = DocEditor 分栏编辑器（文本区 + 预览区 + 保存版本）；非 doc 行无该按钮", async () => {
    installDocRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    expect(within(rowOf(11)).queryByTestId("artifact-doc-view")).toBeNull();
    fireEvent.click(within(rowOf(21)).getByTestId("artifact-doc-view"));
    expect(await screen.findByTestId("doc-editor")).toBeTruthy();
    expect(screen.getByTestId("doc-editor-text")).toBeTruthy();
    expect(screen.getByTestId("doc-editor-preview")).toBeTruthy();
    expect(screen.getByTestId("doc-editor-save")).toBeTruthy();
    // 版本正文已回填（草稿层无行 → 回退当前版本 spec.text）。
    await waitFor(() => {
      expect(
        (screen.getByTestId("doc-editor-text") as HTMLTextAreaElement).value,
      ).toContain("第一版。");
    });

    // 再点收起（toggle 同框）。
    fireEvent.click(within(rowOf(21)).getByTestId("artifact-doc-view"));
    expect(screen.queryByTestId("doc-editor")).toBeNull();
  });

  it("published doc 分享入口：POST /share + 展示自构链接 /#/share/doc/{id}?t&v + TTL 档可改选", async () => {
    const backend = installDocRows();
    vi.stubGlobal("fetch", withDocShareEndpoint(backend));
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // 入口分发：仅 published doc 行；draft doc 与 published 非 doc 行不出。
    expect(within(rowOf(22)).getByTestId("artifact-doc-share")).toBeTruthy();
    expect(within(rowOf(21)).queryByTestId("artifact-doc-share")).toBeNull();

    fireEvent.click(within(rowOf(22)).getByTestId("artifact-doc-share"));
    expect(await screen.findByTestId("doc-share-dialog")).toBeTruthy();

    // 请求：POST /api/v1/artifacts/22/share，body ttl_days=30（缺省档）。
    const posts = () =>
      backend.calls.filter(
        (c) =>
          c.method === "POST" &&
          /^\/api\/v1\/artifacts\/22\/share$/.test(c.url.pathname),
      );
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0]!.body).toEqual({ ttl_days: 30 });

    // 展示链接自构：origin + #/share/doc/22?t=<响应 url 解出的 t>&v=<version>。
    expect(
      (await screen.findByTestId("doc-share-url") as HTMLInputElement).value,
    ).toBe(`${window.location.origin}/#/share/doc/22?t=doc-token-22&v=1`);
    expect(screen.getByTestId("doc-share-expires").textContent).toContain(
      "到期时间",
    );

    // 复制（剪贴板可用时提示已复制）。
    fireEvent.click(screen.getByTestId("doc-share-copy"));
    expect(await screen.findByTestId("doc-share-copied")).toBeTruthy();

    // TTL 四档（0|7|30|90，与 record-view 同源）：改选 90 重新生成。
    const options = screen.getAllByTestId("doc-share-ttl-option");
    expect(options.map((el) => el.getAttribute("data-days"))).toEqual([
      "0",
      "7",
      "30",
      "90",
    ]);
    // 档位文案落成中文（label 文案；不露出裸键名/空档位）。
    expect(options[2]!.parentElement!.textContent).toContain("30 天");
    expect(options[0]!.parentElement!.textContent).toContain("不设期");
    fireEvent.click(options.find((el) => el.getAttribute("data-days") === "90")!);
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1]!.body).toEqual({ ttl_days: 90 });
  });

  it("published doc 不渲染可见性设置区（D5 隐藏），且不发其读端点请求", async () => {
    const backend = installDocRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // published doc（22）行内无可见性区、也无发起读请求。
    expect(
      within(rowOf(22)).queryByTestId("artifact-visibility-section"),
    ).toBeNull();
    expect(
      backend.calls.some((c) =>
        /^\/api\/v1\/artifacts\/22\/visibility$/.test(c.url.pathname),
      ),
    ).toBe(false);
  });

  it("新建文档入口：标题 → POST {kind:'doc', title} → 置顶 + 直接进分栏编辑器", async () => {
    const backend = installDocRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(screen.getByTestId("create-doc-open"));
    expect(screen.getByTestId("create-doc-form")).toBeTruthy();

    fireEvent.change(screen.getByTestId("create-doc-title"), {
      target: { value: "年度报告" },
    });
    fireEvent.click(screen.getByTestId("create-doc-submit"));

    // 创建即跳编辑器（空白文档的空正文——出生即可写作）。
    expect(await screen.findByTestId("doc-editor-text")).toBeTruthy();
    await waitFor(() => {
      expect(
        (screen.getByTestId("doc-editor-title") as HTMLInputElement).value,
      ).toBe("年度报告");
    });

    const post = backend.calls.find(
      (c) => c.method === "POST" && c.url.pathname === "/api/v1/artifacts",
    );
    expect(post).toBeTruthy();
    expect(post?.body).toEqual({ kind: "doc", title: "年度报告" });
    expect(post?.headers["authorization"]).toMatch(/^Bearer /);

    // 新行置顶 + 表单收起（3 基础行 + 21/22 两条 doc + 新建 = 6）。
    const items = screen.getAllByTestId("artifact-item");
    expect(items.length).toBe(6);
    expect(items[0]!.getAttribute("data-artifact-id")).toBe("23");
    expect(screen.queryByTestId("create-doc-form")).toBeNull();
  });

  it("新建文档空标题前端先拦：不发 POST、提示后可修正再创建", async () => {
    const backend = installDocRows();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(screen.getByTestId("create-doc-open"));
    fireEvent.click(screen.getByTestId("create-doc-submit"));
    expect(screen.getByTestId("create-doc-error").textContent).toContain(
      "请输入文档标题",
    );
    expect(
      backend.calls.some(
        (c) => c.method === "POST" && c.url.pathname === "/api/v1/artifacts",
      ),
    ).toBe(false);

    fireEvent.change(screen.getByTestId("create-doc-title"), {
      target: { value: "修正后的标题" },
    });
    fireEvent.click(screen.getByTestId("create-doc-submit"));
    await screen.findByTestId("doc-editor-text");
  });
});

// ── 执行成功事件刷新（wire-copilot-action-gate 任务 4.1，design D9）────────
// 闸执行成功后 api.ts 广播 wire:action-executed；产物页命中 resources 才重取
// 列表——只重取数据（筛选/展开态不动），无需手动刷新整页。
describe("ArtifactsPage（执行成功事件刷新，wire-copilot-action-gate 4.1）", () => {
  it("wire:action-executed 命中 artifacts 即重取列表（无需手动刷新）", async () => {
    const backend = install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    const listCalls = () =>
      backend.calls.filter(
        (c) => c.method === "GET" && c.url.pathname === "/api/v1/artifacts",
      ).length;
    const before = listCalls();

    window.dispatchEvent(
      new CustomEvent("wire:action-executed", {
        detail: { resources: ["artifacts"] },
      }),
    );

    await waitFor(() => expect(listCalls()).toBeGreaterThan(before));
  });

  it("resources 不含 artifacts：不重取（别页的事不惊动产物页）", async () => {
    const backend = install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    const before = backend.calls.filter(
      (c) => c.method === "GET" && c.url.pathname === "/api/v1/artifacts",
    ).length;

    window.dispatchEvent(
      new CustomEvent("wire:action-executed", {
        detail: { resources: ["datasets"] },
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(
      backend.calls.filter(
        (c) => c.method === "GET" && c.url.pathname === "/api/v1/artifacts",
      ).length,
    ).toBe(before);
  });
});
