// AI 助手浮层测试（wire-assistant-panel → wire-copilot-action-gate 任务 3.1/3.4）：
// 开合与 Esc、统一副驾（问/做合并——全档位走同一条消息循环）、免费档能力
// 徽章、propose_action 留痕渲染为动作卡、会话草稿列表与转正提议、失败保留
// 输入、壹座深链不内嵌。
//
// 网络一律 stub：这里验的是交互契约，不碰真后端。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { seedConsoleSession } from "../testing";
import { AssistantPanel } from "./AssistantPanel";

interface Recorded {
  url: string;
  method: string;
  body: unknown;
}

interface BackendOptions {
  /** POST .../messages 的回复消息序列（默认一条 assistant 回声）。 */
  reply?: Array<{ id: number; role: string; content: string }>;
  /** GET .../artifacts 的草稿列表。 */
  artifacts?: unknown[];
}

/** 统一副驾假后端：会话创建 / 消息循环（含轮询 GET）/ 草稿 / 动作闸。 */
function installBackend(options: BackendOptions = {}): {
  calls: Recorded[];
  fetchImpl: typeof fetch;
} {
  const calls: Recorded[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, method, body });
    const json = (data: unknown, status = 200) =>
      new Response(JSON.stringify(data), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    // 创建会话（统一发送先建会话再发消息）。
    if (url.includes("/v1/ai/conversations") && method === "POST" && !/\/\d+\/messages$/.test(url)) {
      return json(
        {
          id: 42,
          customer_id: "c",
          title: "新对话",
          created_at: "2026-10-20T00:00:00",
          messages: [],
        },
        201,
      );
    }
    // 续聊一轮（统一循环：响应即本轮全部消息）。
    const post = url.match(/\/v1\/ai\/conversations\/(\d+)\/messages$/);
    if (post && method === "POST") {
      const content = (body as { content?: string })?.content ?? "";
      return json([
        {
          id: 101,
          conversation_id: 42,
          role: "user",
          content,
          created_at: "2026-10-20T00:00:00",
        },
        ...(options.reply ?? [
          {
            id: 102,
            conversation_id: 42,
            role: "assistant",
            content: "「我的数据」页可以上传表格。",
            created_at: "2026-10-20T00:00:01",
          },
        ]).map((message) => ({
          conversation_id: 42,
          created_at: "2026-10-20T00:00:01",
          ...message,
        })),
      ]);
    }
    // 轮询增量（默认无新增——主响应已带全量）。
    if (url.includes("/messages?after_id=") && method === "GET") {
      return json([]);
    }
    // 本会话草稿（任务 3.4：会话建立后拉取）。
    if (url.includes("/artifacts") && method === "GET") {
      return json(options.artifacts ?? []);
    }
    // 会话提议列表（历史回放对照，默认空）。
    if (url.includes("/v1/ai/actions?conversation_id=") && method === "GET") {
      return json([]);
    }
    // 动作闸 propose（草稿转正按钮的目标端点）。
    if (url.includes("/v1/ai/actions/propose") && method === "POST") {
      return json(
        {
          proposal_id: 77,
          action_id: (body as { action_id?: string })?.action_id ?? "",
          state: "pending_confirm",
          tier: "inline",
          impact: "草稿转正为正式产物",
        },
        201,
      );
    }
    return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return { calls, fetchImpl };
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  // 会话注入：api.ts 的 bearerHeaders 在无会话时直接抛 401（真实行为），
  // 测试必须先有登录态才能走到网络层。
  sessionStorage.clear();
  seedConsoleSession({ sub: "demo-customer", planCode: "basic" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("AssistantPanel（开合）", () => {
  it("初始收起：入口可见、面板隐藏", () => {
    vi.stubGlobal("fetch", installBackend().fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="basic" />);

    expect(screen.getByTestId("assistant-launcher")).toBeTruthy();
    expect(screen.getByTestId("assistant-panel").getAttribute("data-open")).toBe(
      "off",
    );
  });

  it("点击入口展开面板", () => {
    vi.stubGlobal("fetch", installBackend().fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="basic" />);

    fireEvent.click(screen.getByTestId("assistant-launcher"));
    expect(screen.getByTestId("assistant-panel").getAttribute("data-open")).toBe(
      "on",
    );
  });

  it("Esc 收起，且已输入草稿保留", async () => {
    vi.stubGlobal("fetch", installBackend().fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="basic" />);

    fireEvent.click(screen.getByTestId("assistant-launcher"));
    const input = screen.getByTestId("assistant-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "还没发出去的话" } });

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByTestId("assistant-panel").getAttribute("data-open")).toBe(
      "off",
    );

    // 再展开：草稿仍在（外壳常驻挂载、只切可见性）
    fireEvent.click(screen.getByTestId("assistant-launcher"));
    expect(
      (screen.getByTestId("assistant-input") as HTMLTextAreaElement).value,
    ).toBe("还没发出去的话");
  });
});

describe("AssistantPanel（统一副驾：问/做合并）", () => {
  it("免费档：渲染能力说明徽章，不再有模式切换", () => {
    vi.stubGlobal("fetch", installBackend().fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="free" />);
    fireEvent.click(screen.getByTestId("assistant-launcher"));

    expect(screen.getByTestId("assistant-free-badge")).toBeTruthy();
    expect(screen.queryByTestId("assistant-mode-switch")).toBeNull();
    expect(screen.queryByTestId("assistant-mode-build")).toBeNull();
  });

  it("付费档：同样无模式切换（单一副驾，徽章不渲染）", () => {
    vi.stubGlobal("fetch", installBackend().fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="basic" />);
    fireEvent.click(screen.getByTestId("assistant-launcher"));

    expect(screen.queryByTestId("assistant-mode-switch")).toBeNull();
    expect(screen.queryByTestId("assistant-free-badge")).toBeNull();
  });

  it("任意档位发送统一走消息循环（postMessage），不再走 /ai/answer", async () => {
    const backend = installBackend();
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="free" />);
    fireEvent.click(screen.getByTestId("assistant-launcher"));

    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "这页能干嘛" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    // 轮询节奏（首拍 900ms）之后主响应合入——放宽等待上限。
    await waitFor(
      () => {
        expect(
          screen.getByTestId("assistant-entry-assistant").textContent,
        ).toContain("可以上传表格");
      },
      { timeout: 4000 },
    );

    const messagePost = backend.calls.find(
      (call) =>
        call.method === "POST" &&
        /\/v1\/ai\/conversations\/\d+\/messages$/.test(call.url),
    );
    expect(messagePost?.url).toBe("/api/v1/ai/conversations/42/messages");
    expect(messagePost?.body).toEqual({ content: "这页能干嘛" });
    // 问答轨端点已停用（design D1：前端停用 /answer）。
    expect(
      backend.calls.filter((call) => call.url.includes("/v1/ai/answer")),
    ).toHaveLength(0);
  });

  it("失败如实展示且保留输入以便重试", async () => {
    const fetchImpl = (async () =>
      new Response(
        JSON.stringify({ detail: "AI 服务调用失败，本轮未产生回复。" }),
        { status: 502, headers: { "Content-Type": "application/json" } },
      )) as typeof fetch;
    vi.stubGlobal("fetch", fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="free" />);
    fireEvent.click(screen.getByTestId("assistant-launcher"));

    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "这页能干嘛" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(() => {
      expect(screen.getByTestId("assistant-error")).toBeTruthy();
    });
    // 输入被回填，客户可改后重发
    expect(
      (screen.getByTestId("assistant-input") as HTMLTextAreaElement).value,
    ).toBe("这页能干嘛");
  });
});

describe("AssistantPanel（动作卡与草稿，wire-copilot-action-gate）", () => {
  it("propose_action 留痕渲染为动作卡（含影响面与确认/取消）", async () => {
    const backend = installBackend({
      reply: [
        {
          id: 103,
          role: "tool_trace",
          content: JSON.stringify({
            tool_name: "propose_action",
            arguments: {
              action_id: "wire.artifact.draft.create",
              kind: "dashboard",
              spec: {
                title: "经营速览",
                spec: { title: "经营速览", panels: [] },
              },
            },
            result: {
              proposal_id: 7,
              state: "pending_confirm",
              impact: "暂存一份仪表盘草稿",
            },
          }),
        },
        {
          id: 104,
          role: "assistant",
          content: "我准备了一份仪表盘草稿，请在卡片上确认。",
        },
      ],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="basic" />);
    fireEvent.click(screen.getByTestId("assistant-launcher"));

    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "帮我做一个经营速览看板" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(
      () => {
        expect(screen.getByTestId("action-card")).toBeTruthy();
      },
      { timeout: 4000 },
    );
    expect(screen.getByTestId("action-card").getAttribute("data-state")).toBe(
      "pending_confirm",
    );
    expect(screen.getByTestId("action-id").textContent).toBe(
      "wire.artifact.draft.create",
    );
    expect(screen.getByTestId("action-impact").textContent).toContain(
      "暂存一份仪表盘草稿",
    );
    // dashboard 草稿：卡内联预览（复用看板渲染器）。
    expect(screen.getByTestId("action-preview")).toBeTruthy();
    expect(screen.getByTestId("action-confirm")).toBeTruthy();
    expect(screen.getByTestId("action-cancel")).toBeTruthy();
    // 助手回声仍按普通消息渲染。
    expect(screen.getByTestId("assistant-entry-assistant").textContent).toContain(
      "请在卡片上确认",
    );
  });

  it("会话草稿列表可见，转正按钮发起 wire.artifact.promote 提议", async () => {
    const backend = installBackend({
      artifacts: [
        {
          id: 5,
          conversation_id: 42,
          customer_id: "c",
          kind: "dashboard",
          spec: { title: "经营速览", spec: { title: "经营速览", panels: [] } },
          status: "draft",
          created_at: "2026-10-20T00:00:00",
        },
      ],
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<AssistantPanel tab="datasets" planCode="basic" />);
    fireEvent.click(screen.getByTestId("assistant-launcher"));

    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "看下这轮做了什么" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    // 会话建立后草稿区出现在事务区顶部。
    await waitFor(
      () => {
        expect(screen.getByTestId("assistant-drafts")).toBeTruthy();
      },
      { timeout: 4000 },
    );
    expect(screen.getByTestId("assistant-draft-title").textContent).toBe(
      "经营速览",
    );

    fireEvent.click(screen.getByTestId("assistant-draft-promote"));
    await waitFor(() => {
      expect(screen.getByTestId("action-card")).toBeTruthy();
    });
    const propose = backend.calls.find((call) =>
      call.url.includes("/v1/ai/actions/propose"),
    );
    expect(propose?.body).toEqual({
      action_id: "wire.artifact.promote",
      args: { draft_id: 5 },
      conversation_id: 42,
    });
  });
});

describe("AssistantPanel（壹座深链）", () => {
  it("新窗口打开且不内嵌 iframe", () => {
    vi.stubGlobal("fetch", installBackend().fetchImpl);
    const { container } = render(
      <AssistantPanel tab="datasets" planCode="basic" />,
    );
    fireEvent.click(screen.getByTestId("assistant-launcher"));

    const link = screen.getByTestId("assistant-base-link");
    expect(link.getAttribute("href")).toBe("https://platform.finddatatech.cloud");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(container.querySelector("iframe")).toBeNull();
  });
});
