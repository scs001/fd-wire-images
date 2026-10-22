// 「我的需求」只读列表测试（wire-ux-consolidation 任务 4.3；
// wire-demand-generation-orchestration 任务 4.2/4.3 追加编排时间线；
// wire-demand-visibility 任务 3.2 追加交付就绪与内部细节零泄露）：
// - 展示：三态徽章 + rejected「未承接」+ 理由摘要 + 提交时间 + 需求摘要；
//   无任何迁移操作控件；
// - 隔离口径：调用层带 Bearer 且不传任何他企参数（跨客户隔离由服务端
//   /api/v1/demands 列表端点按映射客户保证，本测试 mock 该端点响应）；
// - 编排时间线：有 orchestration 则渲染阶段标签+PR 外链；旧单缺省字段
//   （或空数组）不渲染区块；
// - 交付就绪：delivered 单且 delivery 有 scope 信号才渲染（ok=已就绪/
//   pending=准备中 + 公开授权态）；非 delivered 单与无信号旧单不渲染；
//   delivery 携带内部字段（错误串/事件全文）绝不进页面；
// - 空态/加载失败文案自然。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { MyDemandsPage } from "./MyDemandsPage";
import {
  consoleFailingBackend,
  installConsoleBackend,
  seedConsoleSession,
} from "./testing";
import type { CustomerDemand } from "./api";

function demand(
  partial: Partial<CustomerDemand> & { id: number; status: string },
): CustomerDemand {
  return {
    code: `DE-20261010-0${partial.id}`,
    customer_id: "demo-customer",
    title: `需求摘要 ${partial.id}`,
    intent: "需求全文",
    contact_email: "buyer@acme.com",
    company: null,
    status_history: [],
    rejection_reason: null,
    created_at: "2026-10-10T08:00:00",
    updated_at: "2026-10-10T08:00:00",
    ...partial,
  };
}

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

describe("MyDemandsPage（我的需求 · 只读列表）", () => {
  it("展示：三态徽章 + 未承接（含理由）+ 提交时间 + 摘要，且无迁移操作控件", async () => {
    const backend = installConsoleBackend(
      [],
      {
        demands: [
          demand({ id: 1, status: "submitted", title: "煤价日度指标" }),
          demand({ id: 2, status: "in_progress", title: "航运运价指标" }),
          demand({ id: 3, status: "delivered", title: "光伏装机指标" }),
          demand({
            id: 4,
            status: "rejected",
            title: "海关明细数据",
            rejection_reason: "数据源不公开，无法承接",
          }),
        ],
      },
    );
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MyDemandsPage />);

    const items = await screen.findAllByTestId("my-demand-item");
    expect(items.length).toBe(4);

    const statuses = screen
      .getAllByTestId("my-demand-status")
      .map((el) => el.getAttribute("data-status"));
    expect(statuses).toEqual(["rejected", "delivered", "in_progress", "submitted"]);
    expect(screen.getAllByText("已收到").length).toBe(1);
    expect(screen.getAllByText("处理中").length).toBe(1);
    expect(screen.getAllByText("已交付").length).toBe(1);
    expect(screen.getAllByText("未承接").length).toBe(1);

    // rejected 展示理由摘要
    expect(
      screen.getByTestId("my-demand-rejection-reason").textContent,
    ).toContain("数据源不公开");

    // 摘要与提交时间
    expect(screen.getByText("煤价日度指标")).toBeTruthy();
    expect(screen.getAllByTestId("my-demand-time").length).toBe(4);
    expect(screen.getAllByTestId("my-demand-time")[0]!.textContent).toContain(
      "提交于",
    );

    // 只读：无任何迁移/派发操作控件
    expect(screen.queryByTestId("admin-demand-dispatch-toggle")).toBeNull();
    expect(screen.queryByTestId("admin-dispatch-panel")).toBeNull();
    expect(screen.queryByRole("button", { name: /派发|迁移|执行/ })).toBeNull();
  });

  it("跨客户隔离口径：调用层带 Bearer、不传任何他企参数（列表端点响应即全部）", async () => {
    const backend = installConsoleBackend(
      [],
      {
        demands: [
          demand({ id: 1, status: "submitted" }),
          // 他企的单：服务端（假后端按 customer_id 过滤）不会下发
          demand({ id: 2, status: "submitted", customer_id: "other-corp" }),
        ],
      },
    );
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MyDemandsPage />);

    await screen.findAllByTestId("my-demand-item");
    const call = backend.calls.find((c) => c.url.pathname === "/api/v1/demands")!;
    expect(call.method).toBe("GET");
    expect(call.headers["authorization"]).toMatch(/^Bearer /);
    // 不携带任何客户过滤参数——归属过滤只在服务端
    expect(call.url.search).toBe("");
    // 只渲染本企业（服务端下发）的单
    expect(screen.getAllByTestId("my-demand-item").length).toBe(1);
    expect(screen.getAllByTestId("my-demand-item")[0]!.textContent).toContain(
      "DE-20261010-01",
    );
  });

  it("空态文案自然 + 提交入口", async () => {
    const backend = installConsoleBackend([], { demands: [] });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<MyDemandsPage />);

    const empty = await screen.findByTestId("my-demands-empty");
    expect(empty.textContent).toContain("还没有需求单");
    expect(screen.getByTestId("my-demands-new").getAttribute("href")).toBe(
      "#/console/demands/new",
    );
  });

  it("加载失败：错误如实呈现 + 重试", async () => {
    vi.stubGlobal("fetch", consoleFailingBackend(500, "demands down"));
    render(<MyDemandsPage />);

    const error = await screen.findByTestId("my-demands-error");
    expect(error.textContent).toContain("需求单加载失败");
    expect(error.textContent).toContain("demands down");
    expect(screen.getByTestId("my-demands-retry")).toBeTruthy();
  });

  // ── 编排时间线（wire-demand-generation-orchestration 任务 4.2 客户面）──────

  it("编排时间线：按升序渲染各阶段中文标签 + 时间 + PR 外链（pr_open/merged）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          demand({
            id: 7,
            status: "in_progress",
            orchestration: [
              {
                stage: "workorder_submitted",
                at: "2026-10-10T09:00:00",
                pr_url: null,
              },
              {
                stage: "pr_open",
                at: "2026-10-10T11:30:00",
                pr_url: "https://github.com/finddata/fd-industry-data/pull/42",
              },
            ],
          }),
          demand({
            id: 8,
            status: "delivered",
            orchestration: [
              {
                stage: "workorder_submitted",
                at: "2026-10-09T02:00:00",
                pr_url: null,
              },
              {
                stage: "pr_open",
                at: "2026-10-09T06:00:00",
                pr_url: "https://github.com/finddata/fd-industry-data/pull/39",
              },
              {
                stage: "merged",
                at: "2026-10-09T10:00:00",
                pr_url: "https://github.com/finddata/fd-industry-data/pull/39",
              },
            ],
          }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const blocks = await screen.findAllByTestId("demand-orchestration");
    expect(blocks.length).toBe(2);
    expect(blocks[0]!.textContent).toContain("生成进度");

    // 各阶段中文标签（冻结文案）；workorder_submitted 无外链
    const stages = screen
      .getAllByTestId("demand-orchestration-entry")
      .map((el) => el.getAttribute("data-stage"));
    expect(stages).toEqual([
      // 列表按创建时间倒序：id 8 在前
      "workorder_submitted",
      "pr_open",
      "merged",
      "workorder_submitted",
      "pr_open",
    ]);
    const labels = screen
      .getAllByTestId("demand-orchestration-stage")
      .map((el) => el.textContent);
    expect(labels).toContain("已进入生成流水线");
    expect(labels).toContain("爬虫已生成·待人工审核");
    expect(labels).toContain("审核通过·部署点亮");

    // PR 外链：pr_open/merged 渲染、指向服务端下发地址、新窗打开
    const prLinks = screen.getAllByTestId("demand-orchestration-pr");
    expect(prLinks.length).toBe(3);
    expect(prLinks[0]!.getAttribute("href")).toBe(
      "https://github.com/finddata/fd-industry-data/pull/39",
    );
    expect(prLinks[0]!.getAttribute("target")).toBe("_blank");
    expect(prLinks[2]!.getAttribute("href")).toBe(
      "https://github.com/finddata/fd-industry-data/pull/42",
    );
  });

  it("编排时间线：blocked 阶段呈现「生成受阻·运营跟进中」", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          demand({
            id: 9,
            status: "in_progress",
            orchestration: [
              {
                stage: "workorder_submitted",
                at: "2026-10-10T09:00:00",
                pr_url: null,
              },
              { stage: "blocked", at: "2026-10-10T15:00:00", pr_url: null },
            ],
          }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    await screen.findAllByTestId("demand-orchestration-entry");
    expect(screen.getAllByTestId("demand-orchestration-stage").map((e) => e.textContent)).toContain(
      "生成受阻·运营跟进中",
    );
    // blocked 无 PR 外链
    expect(screen.queryByTestId("demand-orchestration-pr")).toBeNull();
  });

  it("旧单无 orchestration 字段：不渲染生成进度区块（空数组同样不渲染）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          // 旧单：响应缺省 orchestration 字段
          demand({ id: 1, status: "submitted" }),
          // 边界：字段在但为空数组——同样不渲染
          demand({ id: 2, status: "in_progress", orchestration: [] }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const items = await screen.findAllByTestId("my-demand-item");
    expect(items.length).toBe(2);
    expect(screen.queryByTestId("demand-orchestration")).toBeNull();
    expect(screen.queryByTestId("demand-orchestration-entry")).toBeNull();
  });

  // ── 交付就绪（wire-demand-visibility 任务 3.2 客户面）────────────────────

  it("交付就绪：delivered + scope ok 渲染「私有数据面已就绪」+ scope 名 + 已授权转公开", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          demand({
            id: 11,
            status: "delivered",
            delivery: {
              scope_name: "demand-DE-20261012-01",
              scope_status: "ok",
              publish_authorized: true,
            },
          }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const block = await screen.findByTestId("demand-delivery");
    expect(block.getAttribute("data-status")).toBe("ok");
    expect(block.textContent).toContain("数据交付");
    expect(block.textContent).toContain("私有数据面已就绪");
    expect(block.textContent).toContain("已授权转公开");
    expect(screen.getByTestId("demand-delivery-scope-name").textContent).toBe(
      "demand-DE-20261012-01",
    );
  });

  it("交付就绪：scope pending 渲染准备中 + 重试提示，不渲染 scope 名", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          demand({
            id: 12,
            status: "delivered",
            delivery: {
              scope_name: null,
              scope_status: "pending",
              publish_authorized: null,
            },
          }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const block = await screen.findByTestId("demand-delivery");
    expect(block.getAttribute("data-status")).toBe("pending");
    expect(block.textContent).toContain("数据交付准备中");
    expect(block.textContent).toContain("交付挂起，正在重试");
    expect(screen.queryByTestId("demand-delivery-scope-name")).toBeNull();
    // 未授权（null）：公开授权两态文案都不出现
    expect(block.textContent).not.toContain("已授权转公开");
    expect(block.textContent).not.toContain("公开授权已撤回");
  });

  it("交付就绪：公开授权已撤回呈现撤回次行（非「已授权转公开」）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          demand({
            id: 13,
            status: "delivered",
            delivery: {
              scope_name: "demand-DE-20261012-13",
              scope_status: "ok",
              publish_authorized: false,
            },
          }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const block = await screen.findByTestId("demand-delivery");
    expect(block.textContent).toContain("私有数据面已就绪");
    expect(block.textContent).toContain("公开授权已撤回");
    expect(block.textContent).not.toContain("已授权转公开");
  });

  it("交付就绪：delivered 旧单 delivery 为 null / 缺省字段 → 不渲染交付区块", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          // 旧单：响应 delivery 为 null（交付未发起，无信号可呈）
          demand({ id: 14, status: "delivered", delivery: null }),
          // 更旧的响应形态：字段整体缺省
          demand({ id: 15, status: "delivered" }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const items = await screen.findAllByTestId("my-demand-item");
    expect(items.length).toBe(2);
    expect(screen.queryByTestId("demand-delivery")).toBeNull();
    expect(screen.queryByTestId("demand-delivery-scope-name")).toBeNull();
  });

  it("交付就绪：非 delivered 单即使载荷携带 delivery 也不渲染（submitted/in_progress）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          demand({
            id: 16,
            status: "submitted",
            delivery: {
              scope_name: "demand-DE-20261012-16",
              scope_status: "ok",
              publish_authorized: true,
            },
          }),
          demand({
            id: 17,
            status: "in_progress",
            delivery: {
              scope_name: null,
              scope_status: "pending",
              publish_authorized: null,
            },
          }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const items = await screen.findAllByTestId("my-demand-item");
    expect(items.length).toBe(2);
    expect(screen.queryByTestId("demand-delivery")).toBeNull();
    expect(screen.queryByTestId("demand-delivery-scope-name")).toBeNull();
  });

  it("交付就绪：rejected 单不渲染交付区块（即使载荷带 delivery）", async () => {
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [
          demand({
            id: 18,
            status: "rejected",
            rejection_reason: "数据源不公开",
            delivery: {
              scope_name: "demand-DE-20261012-18",
              scope_status: "ok",
              publish_authorized: null,
            },
          }),
        ],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    await screen.findByTestId("my-demand-item");
    expect(screen.queryByTestId("demand-delivery")).toBeNull();
    expect(screen.queryByTestId("demand-delivery-scope-name")).toBeNull();
  });

  it("泄露防护：delivery 混入内部字段（错误串/事件全文）→ 页面零泄露，仅三个安全子字段呈现", async () => {
    // 后端契约绝不随 delivery 下发这些字段；前端防御性只读三个已知安全
    // 子字段——即便响应被污染，错误串与事件全文也不进任何渲染分支。
    const tainted = {
      scope_status: "ok",
      scope_name: "demand-DE-20261012-19",
      publish_authorized: null,
      error: "scope API 不可达：connection refused",
      delivery_events: [
        {
          type: "scope_create_failed",
          error: "scope API down",
          grant_errors: { "IND-0001": "boom" },
        },
      ],
    } as unknown as CustomerDemand["delivery"];
    vi.stubGlobal(
      "fetch",
      installConsoleBackend([], {
        demands: [demand({ id: 19, status: "delivered", delivery: tainted })],
      }).fetchImpl,
    );
    render(<MyDemandsPage />);

    const block = await screen.findByTestId("demand-delivery");
    expect(block.getAttribute("data-status")).toBe("ok");
    expect(screen.getByTestId("demand-delivery-scope-name").textContent).toBe(
      "demand-DE-20261012-19",
    );
    // 页面全文扫描：三个安全子字段之外的一切（内部字段名/错误串/事件
    // 全文）零出现
    const pageText = screen.getByTestId("my-demands-page").textContent ?? "";
    for (const leak of [
      "connection refused",
      "scope API down",
      "scope_create_failed",
      "grant_errors",
      "delivery_events",
      "IND-0001",
    ]) {
      expect(pageText).not.toContain(leak);
    }
  });
});
