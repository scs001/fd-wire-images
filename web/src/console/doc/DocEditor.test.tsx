// 富文本文档分栏编辑器测试（wire-rich-text-docs 任务 4.1 的组件级证据）。
//
// 覆盖六条：读路径（草稿层优先/回退版本 spec.text）、debounce 自动保存落
// 草稿层（PUT /draft，绝不进版本链）、保存状态四态如实呈现、「保存版本」的
// 信封与 onSaved、归档只读、失败（超限 422）如实呈现。
//
// 假后端已具备 doc 草稿层端点（testing.ts 任务 6.2 的镜像）；本文件仅在需要
// 「预置草稿行」/「强制失败」时包一层 fetch 拦截（其余透传），断言以真实
// 内存态（backend.getDocDraft）为准。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { DocEditor } from "./DocEditor";
import { installConsoleBackend, seedConsoleSession } from "../testing";

/** 版本链正文（无草稿行时的回退源）。 */
const VERSION_TEXT = "# 版本正文\n\n来自版本链。";

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

/**
 * 装假后端 + doc 产物种子（id=41）；可选 fetch 拦截：
 * - draftGet：拦截 GET /draft 返回该体（预置「草稿层在场」态）；
 * - draftPutFailure：拦截 PUT /draft 返回该状态（失败/超限如实呈现态）。
 * 拦截的请求仍记入 backend.calls（与透传同断言面）。
 */
function install(
  overrides: {
    draftGet?: unknown;
    draftPutFailure?: { status: number; detail: string };
    status?: "draft" | "published" | "archived";
  } = {},
) {
  const backend = installConsoleBackend([], {});
  backend.addLifecycleArtifact({
    id: 41,
    kind: "doc",
    title: "季度经营说明",
    status: overrides.status ?? "draft",
    version: 1,
    spec: {
      title: "季度经营说明",
      spec: { format: "markdown", text: VERSION_TEXT },
    },
  });
  const baseFetch = backend.fetchImpl;
  const wrapped = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const isDraft = url.pathname === "/api/v1/artifacts/41/draft";
    if (isDraft && method === "GET" && overrides.draftGet !== undefined) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(overrides.draftGet),
      } as unknown as Response);
    }
    if (isDraft && method === "PUT" && overrides.draftPutFailure) {
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
      backend.calls.push({
        method,
        url,
        headers,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      return Promise.resolve({
        ok: false,
        status: overrides.draftPutFailure.status,
        json: () => Promise.resolve({ detail: overrides.draftPutFailure!.detail }),
      } as unknown as Response);
    }
    return baseFetch(input as string, init);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", wrapped);
  return backend;
}

function textarea(): HTMLTextAreaElement {
  return screen.getByTestId("doc-editor-text") as HTMLTextAreaElement;
}

function statusEl(): HTMLElement {
  return screen.getByTestId("doc-editor-status");
}

/** 打开编辑器并等初载完成（textarea 就位）。 */
async function open(overrides: Parameters<typeof install>[0] = {}) {
  const backend = install(overrides);
  const onSaved = vi.fn();
  render(<DocEditor artifactId={41} onSaved={onSaved} startEditing={false} />);
  await screen.findByTestId("doc-editor-text");
  return { backend, onSaved };
}

/** 草稿层 PUT 调用（自动保存的证据）。 */
function draftPuts(backend: ReturnType<typeof install>) {
  return backend.calls.filter(
    (c) =>
      c.method === "PUT" && c.url.pathname === "/api/v1/artifacts/41/draft",
  );
}

/** 版本链 PUT 调用（「保存版本」的证据）。 */
function versionPuts(backend: ReturnType<typeof install>) {
  return backend.calls.filter(
    (c) => c.method === "PUT" && c.url.pathname === "/api/v1/artifacts/41",
  );
}

describe("DocEditor 读路径（草稿层优先 / 回退版本 spec.text）", () => {
  it("草稿层在场：加载草稿正文并呈「已保存」", async () => {
    await open({
      draftGet: { text: "# 草稿正文\n\n未落版的内容。", updated_at: "2026-10-11T09:12:00" },
    });
    await waitFor(() => {
      expect(textarea().value).toBe("# 草稿正文\n\n未落版的内容。");
    });
    expect(statusEl().getAttribute("data-status")).toBe("saved");
    expect(statusEl().textContent).toContain("已保存");
    // 无草稿行才回退版本——此处草稿在场，绝不呈现版本正文。
    expect(textarea().value).not.toContain("来自版本链");
  });

  it("无草稿行：回退当前版本 spec.text，状态「未保存」（尚无自动保存）", async () => {
    await open();
    await waitFor(() => {
      expect(textarea().value).toBe(VERSION_TEXT);
    });
    expect(statusEl().getAttribute("data-status")).toBe("idle");
    expect(statusEl().textContent).toContain("未保存");
  });

  it("预览与源同步：输入 Markdown 后预览区渲染标题/列表/强调", async () => {
    await open();
    await waitFor(() => expect(textarea().value).toBe(VERSION_TEXT));
    fireEvent.change(textarea(), {
      target: { value: "# 一级标题\n\n- 甲\n- 乙\n\n**重点**" },
    });
    const preview = screen.getByTestId("doc-editor-preview");
    expect(preview.querySelector("h1")?.textContent).toBe("一级标题");
    expect(preview.querySelectorAll("li").length).toBe(2);
    expect(preview.querySelector("strong")?.textContent).toBe("重点");
  });
});

describe("DocEditor 自动保存（debounce → 草稿层）", () => {
  it("输入停顿后 PUT /draft 落草稿层；版本链零新增；状态转「已保存」", async () => {
    const { backend } = await open();
    await waitFor(() => expect(textarea().value).toBe(VERSION_TEXT));

    fireEvent.change(textarea(), { target: { value: "# 新正文" } });
    // 输入即「未保存」（尚未落草稿层——如实呈现，不打提前量）。
    expect(statusEl().textContent).toContain("未保存");
    expect(draftPuts(backend)).toHaveLength(0);

    // debounce（~1200ms）后自动保存：正文进草稿层、版本链零新增。
    await waitFor(() => expect(draftPuts(backend)).toHaveLength(1), {
      timeout: 3000,
    });
    expect(draftPuts(backend)[0]!.body).toEqual({ text: "# 新正文" });
    expect(versionPuts(backend)).toHaveLength(0);
    expect(backend.getDocDraft(41)?.text).toBe("# 新正文");
    await waitFor(() => {
      expect(statusEl().getAttribute("data-status")).toBe("saved");
    });
    expect(statusEl().textContent).toContain("已保存");
  }, 10_000);

  it("重挂载读回草稿层：自动保存后的正文不丢（读路径优先草稿）", async () => {
    const { backend } = await open();
    await waitFor(() => expect(textarea().value).toBe(VERSION_TEXT));
    fireEvent.change(textarea(), { target: { value: "刷新后不该丢的正文" } });
    await waitFor(() => expect(draftPuts(backend)).toHaveLength(1), {
      timeout: 3000,
    });

    cleanup();
    render(<DocEditor artifactId={41} onSaved={vi.fn()} />);
    await screen.findByTestId("doc-editor-text");
    await waitFor(() => {
      expect(textarea().value).toBe("刷新后不该丢的正文");
    });
  }, 10_000);

  it("保存失败（超限 422）：状态如实「保存失败」+ 后端 detail 就地呈现，版本链零新增", async () => {
    // 假后端超限口径：UTF-8 字节 > 262144。构造 262145 字节正文。
    const oversize = "a".repeat(262_145);
    const { backend } = await open();
    await waitFor(() => expect(textarea().value).toBe(VERSION_TEXT));

    fireEvent.change(textarea(), { target: { value: oversize } });
    await waitFor(() => expect(draftPuts(backend)).toHaveLength(1), {
      timeout: 5000,
    });
    await waitFor(() => {
      expect(statusEl().getAttribute("data-status")).toBe("failed");
    });
    expect(statusEl().textContent).toContain("保存失败");
    expect(screen.getByTestId("doc-editor-status-error").textContent).toContain(
      "上限",
    );
    // 零半写：草稿行未落、版本链未动。
    expect(backend.getDocDraft(41)).toBeNull();
    expect(versionPuts(backend)).toHaveLength(0);
  }, 15_000);
});

describe("DocEditor 保存版本（显式里程碑，草稿层保留）", () => {
  it("PUT /artifacts/{id} 信封 {title, spec:{format:'markdown', text}} + onSaved；草稿不被清", async () => {
    const { backend, onSaved } = await open();
    await waitFor(() => expect(textarea().value).toBe(VERSION_TEXT));

    // 先让自动保存落一次草稿层（草稿=工作区，保存版本后必须仍在）。
    fireEvent.change(textarea(), { target: { value: "# 里程碑正文" } });
    await waitFor(() => expect(draftPuts(backend)).toHaveLength(1), {
      timeout: 3000,
    });

    fireEvent.click(screen.getByTestId("doc-editor-save"));
    await waitFor(() => expect(versionPuts(backend)).toHaveLength(1), {
      timeout: 3000,
    });
    expect(versionPuts(backend)[0]!.body).toEqual({
      spec: {
        title: "季度经营说明",
        spec: { format: "markdown", text: "# 里程碑正文" },
      },
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onSaved.mock.calls[0]![0]).toMatchObject({
      id: 41,
      kind: "doc",
      version: 2,
      title: "季度经营说明",
    });
    // 保存版本成功后状态即「已保存」（正文已进版本链——持久化落点之一）。
    await waitFor(() => {
      expect(statusEl().getAttribute("data-status")).toBe("saved");
    });
    // 草稿层保留（D3：工作区与里程碑并存，保存版本绝不清工作区）。
    expect(backend.getDocDraft(41)?.text).toBe("# 里程碑正文");
  }, 10_000);

  it("标题随编辑器改写进信封（title 输入框喂 title 字段）", async () => {
    const { backend } = await open();
    await waitFor(() => expect(textarea().value).toBe(VERSION_TEXT));
    fireEvent.change(screen.getByTestId("doc-editor-title"), {
      target: { value: "季度经营说明（修订）" },
    });
    fireEvent.click(screen.getByTestId("doc-editor-save"));
    await waitFor(() => expect(versionPuts(backend)).toHaveLength(1), {
      timeout: 3000,
    });
    expect(versionPuts(backend)[0]!.body).toEqual({
      spec: {
        title: "季度经营说明（修订）",
        spec: { format: "markdown", text: VERSION_TEXT },
      },
    });
  }, 10_000);
});

describe("DocEditor 归档只读", () => {
  it("archived：textarea 禁用、保存按钮禁用、提示「已归档」", async () => {
    const { backend } = await open({ status: "archived" });
    await waitFor(() => expect(textarea().value).toBe(VERSION_TEXT));
    expect(textarea().disabled).toBe(true);
    expect(
      (screen.getByTestId("doc-editor-save") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.getByTestId("doc-editor-archived-notice").textContent).toContain(
      "已归档",
    );

    // 只读面不发写请求（自动保存绝不触发——服务端 409 是兜底而非主路径）。
    expect(draftPuts(backend)).toHaveLength(0);
    expect(versionPuts(backend)).toHaveLength(0);
  });
});
