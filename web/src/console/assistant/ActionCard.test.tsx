// 动作提议卡组件测试（wire-copilot-action-gate 任务 3.2/3.3）：两面渲染、
// 合规门留白（缺字段不请求）、失败重试（同参数重新 propose）、历史回放
// （终态入参直接渲染 resolved 面）、草稿预览内联（dashboard 完整渲染 /
// form 字段清单 / record-view 键列）。
//
// 网络一律 stub 全局 fetch（仓内惯例：不 mock 模块，走真实 api client——
// 顺带断言请求 URL 与载荷形状）。DashboardRenderer 用 SVG 渲染器，happy-dom
// 下 echarts 可直接跑（见 DashboardRenderer.test.tsx 先例），无需 mock。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { seedConsoleSession } from "../testing";
import { ActionCard, type ActionCardProposal } from "./ActionCard";

interface Recorded {
  url: string;
  method: string;
  body: unknown;
}

/** 动作闸假后端：propose/approve/reject 三端点按契约形状响应。 */
function installGate(approveResult?: Partial<{
  state: "executed" | "failed";
  result_summary: string;
  error: string;
}>): { calls: Recorded[]; fetchImpl: typeof fetch } {
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
    if (url.includes("/v1/ai/actions/propose") && method === "POST") {
      return json(
        {
          proposal_id: 99,
          action_id: (body as { action_id?: string })?.action_id ?? "",
          state: "pending_confirm",
          tier: "inline",
          impact: "新建一份产物草稿",
        },
        201,
      );
    }
    const approve = url.match(/\/v1\/ai\/actions\/(\d+)\/approve$/);
    if (approve && method === "POST") {
      return json({
        proposal_id: Number(approve[1]),
        action_id: "wire.artifact.publish",
        state: approveResult?.state ?? "executed",
        ...(approveResult?.result_summary != null
          ? { result_summary: approveResult.result_summary }
          : {}),
        ...(approveResult?.error != null ? { error: approveResult.error } : {}),
      });
    }
    const reject = url.match(/\/v1\/ai\/actions\/(\d+)\/reject$/);
    if (reject && method === "POST") {
      return json({ proposal_id: Number(reject[1]), state: "rejected" });
    }
    return json([], 200);
  }) as typeof fetch;
  return { calls, fetchImpl };
}

const BASE_PROPOSAL: ActionCardProposal = {
  proposalId: 7,
  actionId: "wire.artifact.publish",
  args: { artifact_id: 12, title: "宏观仪表盘" },
  state: "pending_confirm",
  tier: "inline",
  impact: "产物对队内可见",
};

beforeEach(() => {
  i18n.changeLanguage("zh");
  sessionStorage.clear();
  // api.ts 的 bearerHeaders 在无会话时直接抛 401（真实行为）。
  seedConsoleSession({ sub: "demo-customer", planCode: "basic" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("ActionCard（待确认面）", () => {
  it("渲染动作 id、目标、影响面、tier 徽章与确认/取消按钮", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(<ActionCard proposal={BASE_PROPOSAL} conversationId={42} />);

    const card = screen.getByTestId("action-card");
    expect(card.getAttribute("data-state")).toBe("pending_confirm");
    expect(screen.getByTestId("action-id").textContent).toBe(
      "wire.artifact.publish",
    );
    expect(screen.getByTestId("action-target").textContent).toBe("宏观仪表盘");
    expect(screen.getByTestId("action-impact").textContent).toContain(
      "产物对队内可见",
    );
    expect(screen.getByTestId("action-tier").textContent).toBe("行内确认");
    expect(screen.getByTestId("action-confirm")).toBeTruthy();
    expect(screen.getByTestId("action-cancel")).toBeTruthy();
  });

  it("确认走 approve（无合规键），成功翻已执行面并显示结果摘要", async () => {
    const gate = installGate({ result_summary: "产物 #12 已发布" });
    vi.stubGlobal("fetch", gate.fetchImpl);

    const events: Array<{ resources?: string[] }> = [];
    const onEvent = (event: Event) => {
      events.push((event as CustomEvent<{ resources?: string[] }>).detail);
    };
    window.addEventListener("wire:action-executed", onEvent);
    render(<ActionCard proposal={BASE_PROPOSAL} conversationId={42} />);
    fireEvent.click(screen.getByTestId("action-confirm"));

    await waitFor(() => {
      expect(screen.getByTestId("action-card").getAttribute("data-state")).toBe(
        "executed",
      );
    });
    window.removeEventListener("wire:action-executed", onEvent);
    expect(screen.getByTestId("action-result").textContent).toContain(
      "产物 #12 已发布",
    );
    expect(screen.queryByTestId("action-confirm")).toBeNull();

    const approve = gate.calls.find((call) => call.url.endsWith("/approve"));
    expect(approve?.url).toBe("/api/v1/ai/actions/7/approve");
    expect(approve?.body).toEqual({});
    // 执行成功事件（任务 4.1）：wire.artifact.* → resources=["artifacts"]。
    expect(events).toEqual([{ resources: ["artifacts"] }]);
  });

  it("取消走 reject，翻已取消面", async () => {
    const gate = installGate();
    vi.stubGlobal("fetch", gate.fetchImpl);
    render(<ActionCard proposal={BASE_PROPOSAL} conversationId={42} />);

    fireEvent.click(screen.getByTestId("action-cancel"));
    await waitFor(() => {
      expect(screen.getByTestId("action-card").getAttribute("data-state")).toBe(
        "rejected",
      );
    });
    expect(screen.getByTestId("action-rejected-note")).toBeTruthy();
    const reject = gate.calls.find((call) => call.url.endsWith("/reject"));
    expect(reject?.url).toBe("/api/v1/ai/actions/7/reject");
  });

  it("pending_approval 态显示等待管理员批准，无确认按钮", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(
      <ActionCard
        proposal={{
          ...BASE_PROPOSAL,
          state: "pending_approval",
          tier: "async",
          actionId: "wire.artifact.share.create",
        }}
        conversationId={42}
      />,
    );

    expect(screen.getByTestId("action-waiting").textContent).toBe(
      "等待本队管理员批准",
    );
    expect(screen.queryByTestId("action-confirm")).toBeNull();
    expect(screen.queryByTestId("action-cancel")).toBeNull();
  });
});

describe("ActionCard（需求单合规门，design D8）", () => {
  const DEMAND_PROPOSAL: ActionCardProposal = {
    proposalId: 8,
    actionId: "wire.demand.submit",
    args: { description: "希望补一个港口吞吐量指标" },
    state: "pending_confirm",
    tier: "inline",
    impact: "提交一张需求单",
  };

  it("合规字段未填齐：确认按钮 disabled，点击不发请求", async () => {
    const gate = installGate();
    vi.stubGlobal("fetch", gate.fetchImpl);
    render(<ActionCard proposal={DEMAND_PROPOSAL} conversationId={42} />);

    const confirm = screen.getByTestId("action-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.click(confirm);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(
      gate.calls.filter((call) => call.url.includes("/approve")),
    ).toHaveLength(0);

    // 只填 URL 未勾选：仍禁用。
    fireEvent.change(screen.getByTestId("action-compliance-url"), {
      target: { value: "https://example.com/data" },
    });
    expect(
      (screen.getByTestId("action-confirm") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("填齐两字段后确认：approve 载荷携带 compliance（手填值）", async () => {
    const gate = installGate({ result_summary: "需求单已提交" });
    vi.stubGlobal("fetch", gate.fetchImpl);
    render(<ActionCard proposal={DEMAND_PROPOSAL} conversationId={42} />);

    fireEvent.change(screen.getByTestId("action-compliance-url"), {
      target: { value: "https://example.com/data" },
    });
    fireEvent.click(screen.getByTestId("action-compliance-claim"));
    fireEvent.click(screen.getByTestId("action-confirm"));

    await waitFor(() => {
      expect(screen.getByTestId("action-card").getAttribute("data-state")).toBe(
        "executed",
      );
    });
    const approve = gate.calls.find((call) => call.url.endsWith("/approve"));
    expect(approve?.body).toEqual({
      compliance: {
        source_url: "https://example.com/data",
        public_claim: true,
      },
    });
  });
});

describe("ActionCard（草稿预览内联，任务 3.3）", () => {
  it("dashboard kind：卡内用 DashboardRenderer 渲染完整看板", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(
      <ActionCard
        proposal={{
          proposalId: 9,
          actionId: "wire.artifact.draft.create",
          args: {
            kind: "dashboard",
            spec: {
              title: "经营速览",
              spec: {
                title: "经营速览",
                panels: [
                  {
                    id: "k1",
                    type: "kpi",
                    title: "月销",
                    span: 1,
                    snapshot: { columns: ["value"], rows: [[128]] },
                  },
                ],
              },
            },
          },
          state: "pending_confirm",
          tier: "inline",
          impact: "暂存一份仪表盘草稿",
        }}
        conversationId={42}
      />,
    );

    const preview = screen.getByTestId("action-preview");
    expect(preview.className).toContain("h-64");
    // 复用既有看板渲染器（客户确认的是「看到的样子」，不是文字描述）。
    expect(preview.querySelector('[data-testid="dashboard-renderer"]')).toBeTruthy();
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("128");
  });

  it("form kind：渲染字段清单（结构摘要）", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(
      <ActionCard
        proposal={{
          proposalId: 10,
          actionId: "wire.artifact.draft.create",
          args: {
            kind: "form",
            spec: {
              title: "客户登记",
              spec: {
                datasetId: null,
                fields: [
                  { column: "name", label: "客户名", input: "text" },
                  { column: "amount", label: "金额", input: "number", required: true },
                ],
              },
            },
          },
          state: "pending_confirm",
          tier: "inline",
        }}
        conversationId={42}
      />,
    );

    const fields = screen.getAllByTestId("action-preview-field");
    expect(fields.map((el) => el.textContent)).toEqual([
      "客户名text",
      "金额number",
    ]);
  });

  it("record-view kind：渲染键列与表头字段清单", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(
      <ActionCard
        proposal={{
          proposalId: 11,
          actionId: "wire.artifact.draft.create",
          args: {
            kind: "record-view",
            spec: {
              title: "资产标签",
              spec: {
                datasetId: "ds-1",
                keyColumn: "asset_code",
                headerFields: [
                  { column: "asset_code", label: "资产码" },
                  { column: "owner", label: "负责人" },
                ],
                sections: [],
              },
            },
          },
          state: "pending_confirm",
          tier: "inline",
        }}
        conversationId={42}
      />,
    );

    expect(screen.getByTestId("action-preview").textContent).toContain(
      "asset_code",
    );
    expect(
      screen.getAllByTestId("action-preview-field").map((el) => el.textContent),
    ).toEqual(["资产码", "负责人"]);
  });

  it("结构未知的 kind：渲染 spec 键名清单，绝不渲染占位文案", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(
      <ActionCard
        proposal={{
          proposalId: 12,
          actionId: "wire.artifact.draft.create",
          args: {
            kind: "alert_rule",
            spec: { title: "煤价告警", spec: { indicator: "CN_COAL", threshold: 0.05 } },
          },
          state: "pending_confirm",
          tier: "inline",
        }}
        conversationId={42}
      />,
    );

    const keys = screen.getAllByTestId("action-preview-key");
    expect(keys.map((el) => el.textContent)).toEqual(["indicator", "threshold"]);
    expect(screen.getByTestId("action-preview").textContent).not.toContain(
      "暂不可用",
    );
  });
});

describe("ActionCard（resolved 面与历史回放）", () => {
  it("failed 态：显示失败原因并提供重试（同参数重新 propose）", async () => {
    const gate = installGate();
    vi.stubGlobal("fetch", gate.fetchImpl);
    render(
      <ActionCard
        proposal={{
          ...BASE_PROPOSAL,
          state: "failed",
          error: "引擎调用超时",
        }}
        conversationId={42}
      />,
    );

    expect(screen.getByTestId("action-error-line").textContent).toBe(
      "引擎调用超时",
    );
    fireEvent.click(screen.getByTestId("action-retry"));

    // 重试产生新提议 id，卡回到待确认面。
    await waitFor(() => {
      expect(screen.getByTestId("action-card").getAttribute("data-proposal-id")).toBe(
        "99",
      );
    });
    expect(screen.getByTestId("action-card").getAttribute("data-state")).toBe(
      "pending_confirm",
    );
    const propose = gate.calls.find((call) =>
      call.url.includes("/v1/ai/actions/propose"),
    );
    expect(propose?.body).toEqual({
      action_id: "wire.artifact.publish",
      args: BASE_PROPOSAL.args,
      conversation_id: 42,
    });
  });

  it("executed 态：显示结果摘要（会话重载后的历史回放面）", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(
      <ActionCard
        proposal={{
          ...BASE_PROPOSAL,
          state: "executed",
          resultSummary: "产物 #12 已发布",
        }}
        conversationId={42}
      />,
    );

    expect(screen.getByTestId("action-card").getAttribute("data-state")).toBe(
      "executed",
    );
    expect(screen.getByTestId("action-result").textContent).toContain(
      "产物 #12 已发布",
    );
    expect(screen.queryByTestId("action-confirm")).toBeNull();
  });

  it("expired 态：显示过期说明", () => {
    vi.stubGlobal("fetch", installGate().fetchImpl);
    render(
      <ActionCard
        proposal={{ ...BASE_PROPOSAL, state: "expired" }}
        conversationId={42}
      />,
    );

    expect(screen.getByTestId("action-expired-note")).toBeTruthy();
  });
});
