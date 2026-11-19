// 产物公开分享选择器测试（wire-artifact-marketplace 任务 6.4 web 面）：
// - 四档渲染与选中态（aria-checked + data-level）、当前态摘要；
// - 保存 PUT 载荷（visibility/access_mode/allow_copy 全量三字段）；
// - 422 / 409 后端 detail 如实回显；
// - 非 published 产物：非 private 档置灰（data-disabled/disabled）+ 发布提示；
// - canEdit=false 只读（无保存按钮、全部控件禁用）；
// - 披露文案（visibilityPublicNote）常驻三态：加载中 / 已加载 / 加载失败；
// - 开放模式选择仅 directed/market 档显示；allow_copy 开关随保存提交；
// - published_at 展示存在性；加载失败态给重试。
// 注意：本批新 i18n 键（console.artifact.visibility.*）译文由另一通道落地——
// t() 在译文未落地时回落键名本身，故断言只锚 testid / data-* / 请求载荷等
// 语言中立内容，不锚具体文案。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { ArtifactVisibilitySection } from "./ArtifactVisibilitySection";
import { seedConsoleSession } from "./testing";

const VISIBILITY_PATH = "/api/v1/artifacts/21/visibility";

interface CallRecord {
  method: string;
  url: string;
  body?: unknown;
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

interface BackendOptions {
  /** GET 初始态（缺省 private + open + allow_copy=false）。 */
  initial?: Partial<{
    visibility: string;
    access_mode: string;
    allow_copy: boolean;
    published_at: string | null;
    published_version: string | number | null;
  }>;
  /** 强制 GET 返回该状态（加载失败态）。 */
  failLoadStatus?: number;
  /** 仅首次 GET 失败（重试恢复路径）。 */
  failFirstLoadOnly?: boolean;
  /** 强制 PUT 返回该状态（422 敏感档闸 / 409 未发布等）。 */
  putStatus?: number;
  putDetail?: string;
}

/** 契约假后端：GET 回当前态、PUT 全量回写并回显（零网络）。 */
function installBackend(options: BackendOptions = {}) {
  const calls: CallRecord[] = [];
  let getCount = 0;
  const state = {
    visibility: "private",
    access_mode: "open",
    allow_copy: false,
    published_at: null as string | null,
    published_version: null as string | number | null,
    ...options.initial,
  };
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({
      method,
      url,
      body:
        typeof init?.body === "string"
          ? (JSON.parse(init.body) as unknown)
          : undefined,
    });
    if (method === "GET") {
      getCount += 1;
      if (options.failLoadStatus || (options.failFirstLoadOnly && getCount === 1)) {
        return jsonResponse({ detail: "visibility boom" }, options.failLoadStatus ?? 500);
      }
      return jsonResponse({ ...state });
    }
    // PUT
    if (options.putStatus !== undefined && options.putStatus >= 400) {
      return jsonResponse({ detail: options.putDetail ?? "rejected" }, options.putStatus);
    }
    const body = (init?.body ? JSON.parse(String(init.body)) : {}) as {
      visibility: string;
      access_mode: string;
      allow_copy: boolean;
    };
    state.visibility = body.visibility;
    state.access_mode = body.access_mode;
    state.allow_copy = body.allow_copy;
    if (body.visibility === "private") {
      state.published_at = null;
      state.published_version = null;
    } else {
      state.published_at = state.published_at ?? "2026-10-13T01:00:00Z";
      state.published_version = state.published_version ?? 2;
    }
    return jsonResponse({ ...state });
  });
  return {
    calls,
    state,
    fetchImpl: fetchImpl as unknown as typeof fetch,
  };
}

function putCalls(calls: CallRecord[]): CallRecord[] {
  return calls.filter((c) => c.method === "PUT");
}

describe("ArtifactVisibilitySection（wire-artifact-marketplace 5.1）", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh");
    localStorage.clear();
    sessionStorage.clear();
    seedConsoleSession();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("四档渲染与选中态：GET 当前态回填（link 选中）、published_at 展示", async () => {
    const backend = installBackend({
      initial: {
        visibility: "link",
        published_at: "2026-10-13T01:00:00Z",
        published_version: 2,
      },
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );

    await screen.findByTestId("artifact-visibility-select");
    // 四档按钮齐备，选中态由 aria-checked 表达（语言中立）。
    const levels = ["private", "link", "directed", "market"].map((level) =>
      screen.getByTestId(`artifact-visibility-option-${level}`),
    );
    expect(levels.map((el) => el.getAttribute("data-level"))).toEqual([
      "private",
      "link",
      "directed",
      "market",
    ]);
    expect(
      levels.map((el) => el.getAttribute("aria-checked")),
    ).toEqual(["false", "true", "false", "false"]);
    // 已开放（link）：当前态徽标 + 开放时间在场
    expect(
      screen.getByTestId("artifact-visibility-current").getAttribute("data-visibility"),
    ).toBe("link");
    expect(screen.getByTestId("artifact-published-at").textContent).toContain("2026");
    // link 档不显示开放模式选择（仅 directed/market）
    expect(screen.queryByTestId("artifact-access-mode")).toBeNull();
    // 请求路径与鉴权通道
    expect(backend.calls[0]!.url).toContain(VISIBILITY_PATH);
  });

  it("保存 PUT 载荷：四档切换 + 开放模式 + allow_copy 全量三字段", async () => {
    const backend = installBackend();
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );
    await screen.findByTestId("artifact-visibility-select");

    // 初始 private：保存按钮禁用（无脏值）。
    expect(
      (screen.getByTestId("artifact-visibility-save") as HTMLButtonElement).disabled,
    ).toBe(true);

    // 选 market → 开放模式选择出现（默认 open）→ 保存
    fireEvent.click(screen.getByTestId("artifact-visibility-option-market"));
    expect(screen.getByTestId("artifact-access-mode")).toBeTruthy();
    fireEvent.click(screen.getByTestId("artifact-visibility-save"));
    await waitFor(() => expect(putCalls(backend.calls)).toHaveLength(1));
    expect(putCalls(backend.calls)[0]!.body).toEqual({
      visibility: "market",
      access_mode: "open",
      allow_copy: false,
    });
    expect(screen.getByTestId("artifact-visibility-saved")).toBeTruthy();
    // 服务端回写后当前态徽标切到 market，open 模式摘要在场
    expect(
      screen.getByTestId("artifact-visibility-current").getAttribute("data-visibility"),
    ).toBe("market");
    expect(screen.getByTestId("artifact-published-at")).toBeTruthy();

    // 切 approval + 打开 allow_copy → 第二次 PUT 携带两个新值
    fireEvent.click(screen.getByTestId("artifact-access-mode-option-approval"));
    fireEvent.click(screen.getByTestId("artifact-allow-copy"));
    expect(
      (screen.getByTestId("artifact-allow-copy") as HTMLInputElement).checked,
    ).toBe(true);
    fireEvent.click(screen.getByTestId("artifact-visibility-save"));
    await waitFor(() => expect(putCalls(backend.calls)).toHaveLength(2));
    expect(putCalls(backend.calls)[1]!.body).toEqual({
      visibility: "market",
      access_mode: "approval",
      allow_copy: true,
    });
  });

  it("422 敏感档闸：后端 detail 如实回显（不吞错）", async () => {
    const backend = installBackend({
      putStatus: 422,
      putDetail: "绑定数据集含敏感档（ds-sensitive）——禁止设为 market（可选 link/directed）",
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );
    await screen.findByTestId("artifact-visibility-select");

    fireEvent.click(screen.getByTestId("artifact-visibility-option-market"));
    fireEvent.click(screen.getByTestId("artifact-visibility-save"));
    const error = await screen.findByTestId("artifact-visibility-error");
    expect(error.textContent).toContain("ds-sensitive");
    // 服务端拒绝后本地草稿保持原状（未被回写覆盖）
    expect(
      screen.getByTestId("artifact-visibility-current").getAttribute("data-visibility"),
    ).toBe("private");
  });

  it("409 未发布：后端 detail 如实回显", async () => {
    const backend = installBackend({
      putStatus: 409,
      putDetail: "仅已发布（published）的产物可设置非私有档位——请先发布",
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );
    await screen.findByTestId("artifact-visibility-select");

    fireEvent.click(screen.getByTestId("artifact-visibility-option-directed"));
    fireEvent.click(screen.getByTestId("artifact-visibility-save"));
    const error = await screen.findByTestId("artifact-visibility-error");
    expect(error.textContent).toContain("请先发布");
  });

  it("非 published 产物：非 private 档置灰 + 发布提示；private 仍可选", async () => {
    const backend = installBackend();
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="draft"
        canEdit={true}
      />,
    );
    await screen.findByTestId("artifact-visibility-select");

    for (const level of ["link", "directed", "market"]) {
      const option = screen.getByTestId(
        `artifact-visibility-option-${level}`,
      ) as HTMLButtonElement;
      expect(option.disabled).toBe(true);
      expect(option.getAttribute("data-disabled")).toBe("true");
    }
    const privateOption = screen.getByTestId(
      "artifact-visibility-option-private",
    ) as HTMLButtonElement;
    expect(privateOption.disabled).toBe(false);
    expect(privateOption.getAttribute("data-disabled")).toBe("false");
    // 非 published 提示在场（publishHint 键）
    expect(screen.getByTestId("artifact-visibility-publish-hint")).toBeTruthy();
  });

  it("canEdit=false 只读：无保存入口、档位与开关全部禁用", async () => {
    const backend = installBackend({
      initial: { visibility: "market", access_mode: "open", allow_copy: true },
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={false}
      />,
    );
    await screen.findByTestId("artifact-visibility-select");

    expect(screen.queryByTestId("artifact-visibility-save")).toBeNull();
    for (const level of ["private", "link", "directed", "market"]) {
      expect(
        (screen.getByTestId(`artifact-visibility-option-${level}`) as HTMLButtonElement)
          .disabled,
      ).toBe(true);
    }
    expect((screen.getByTestId("artifact-allow-copy") as HTMLInputElement).disabled).toBe(
      true,
    );
    // market 档下开放模式按钮同样禁用
    expect(
      (screen.getByTestId("artifact-access-mode-option-approval") as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    // 只读档仍如实展示当前态
    expect(
      screen.getByTestId("artifact-visibility-current").getAttribute("data-visibility"),
    ).toBe("market");
  });

  it("披露文案（visibilityPublicNote）常驻：加载中 / 已加载 / 加载失败三态", async () => {
    const ok = installBackend();
    vi.stubGlobal("fetch", ok.fetchImpl);
    const first = render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );
    // 加载中：loading 态 + 披露已在场
    expect(screen.getByTestId("artifact-visibility-loading")).toBeTruthy();
    expect(screen.getByTestId("artifact-visibility-note").textContent?.trim()).not.toBe("");
    await screen.findByTestId("artifact-visibility-select");
    expect(screen.getByTestId("artifact-visibility-note")).toBeTruthy();
    first.unmount();

    // 加载失败态：披露仍在场
    const failing = installBackend({ failLoadStatus: 500 });
    vi.stubGlobal("fetch", failing.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );
    await screen.findByTestId("artifact-visibility-error");
    expect(screen.getByTestId("artifact-visibility-note")).toBeTruthy();
  });

  it("加载失败：重试恢复（GET 再次请求并进入可编辑态）", async () => {
    const backend = installBackend({ failFirstLoadOnly: true });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );
    await screen.findByTestId("artifact-visibility-error");

    fireEvent.click(screen.getByTestId("artifact-visibility-retry"));
    await screen.findByTestId("artifact-visibility-select");
    expect(backend.calls.filter((c) => c.method === "GET").length).toBeGreaterThanOrEqual(2);
  });

  it("market 档预告敏感档纪律说明（sensitiveNote）；private 档不渲染", async () => {
    const backend = installBackend();
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(
      <ArtifactVisibilitySection
        artifactId={21}
        artifactStatus="published"
        canEdit={true}
      />,
    );
    await screen.findByTestId("artifact-visibility-select");

    expect(screen.queryByTestId("artifact-visibility-sensitive-note")).toBeNull();
    fireEvent.click(screen.getByTestId("artifact-visibility-option-market"));
    expect(screen.getByTestId("artifact-visibility-sensitive-note")).toBeTruthy();
    fireEvent.click(screen.getByTestId("artifact-visibility-option-private"));
    expect(screen.queryByTestId("artifact-visibility-sensitive-note")).toBeNull();
  });
});
