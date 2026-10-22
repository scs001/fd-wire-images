// 需求单派发台组件测试（wire-ux-consolidation 任务 3.5 三态化）：全客户列表 +
// 状态筛选查询形状、派发操作调用形状（POST transition）、非法迁移项禁用、
// rejected 必填理由、终态不可派发、409 冲突如实回显；末尾钉死三态机镜像与
// 后端白名单（api 仓 fd_wire_api/demands/statemachine.py）一致。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { DemandsPage } from "./DemandsPage";
import { installAdminBackend, seedAdminSession } from "./testing";
import type { AdminDemand } from "./api";
import { ALL_DEMAND_STATUSES, ALLOWED_TRANSITIONS } from "./demandsMachine";

const TOKEN = "test-admin-token";

function demand(partial: Partial<AdminDemand> & { id: number; status: AdminDemand["status"] }): AdminDemand {
  return {
    code: `DE-20261004-0${partial.id}`,
    customer_id: `cust-${partial.id}`,
    title: `需求 ${partial.id}`,
    intent: "意图",
    status_history: [
      { status: "submitted", at: "2026-10-04T09:00:00+00:00", by: `customer:cust-${partial.id}`, note: "需求单提交" },
    ],
    evaluation_summary: null,
    rejection_reason: null,
    quote_amount: null,
    quote_note: null,
    pipeline_events: [],
    delivery_events: [],
    gate: null,
    scope_status: null,
    scope_name: null,
    created_at: "2026-10-04T09:00:00",
    updated_at: "2026-10-04T09:00:00",
    ...partial,
  };
}

const DEMANDS: AdminDemand[] = [
  demand({ id: 1, status: "submitted", customer_id: "cust-001", title: "煤价日度" }),
  demand({ id: 2, status: "in_progress", customer_id: "cust-002", title: "航运运价" }),
  demand({ id: 3, status: "delivered", customer_id: "cust-002", title: "光伏装机" }),
];

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // admin 双闸（web 半边）：登录会话持 wire-admin 角色，idToken 与假后端
  // 的期望 Bearer 一致（X-Admin-Token 由 nginx 服务端注入，前端不持）。
  seedAdminSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

function openPanel(itemId: number) {
  const item = screen
    .getAllByTestId("admin-demand-item")
    .find((el) => el.textContent!.includes(`DE-20261004-0${itemId}`))!;
  fireEvent.click(
    screen.getAllByTestId("admin-demand-dispatch-toggle")[
      screen.getAllByTestId("admin-demand-item").indexOf(item)
    ]!,
  );
  return item;
}

/** 展开某单的编排进度面（wire-demand-visibility 任务 2.2）。 */
function expandProgress(itemId: number) {
  const items = screen.getAllByTestId("admin-demand-item");
  const item = items.find((el) =>
    el.textContent!.includes(`DE-20261004-0${itemId}`),
  )!;
  fireEvent.click(
    screen.getAllByTestId("admin-demand-progress-toggle")[
      items.indexOf(item)
    ]!,
  );
  return item;
}

describe("DemandsPage（需求单派发台）", () => {
  it("全客户列表渲染 + 状态筛选的查询形状（?status=）", async () => {
    const backend = installAdminBackend({ demands: DEMANDS, bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<DemandsPage />);

    const items = await screen.findAllByTestId("admin-demand-item");
    expect(items.length).toBe(3);
    // 两家客户的单都在（全客户视图）
    const allText = items.map((el) => el.textContent!).join("\n");
    expect(allText).toContain("煤价日度");
    expect(allText).toContain("航运运价");
    expect(allText).toContain("光伏装机");
    expect(allText).toContain("cust-001");
    expect(allText).toContain("cust-002");

    fireEvent.change(screen.getByTestId("demand-status-filter"), {
      target: { value: "in_progress" },
    });
    await waitFor(() => {
      expect(screen.getAllByTestId("admin-demand-item").length).toBe(1);
    });
    const filterCall = backend.calls.filter((c) => c.url.pathname === "/internal/admin/demands").pop()!;
    expect(filterCall.url.searchParams.get("status")).toBe("in_progress");
  });

  it("派发操作：非法迁移项禁用，合法迁移的调用形状（target/by/note）与刷新", async () => {
    const backend = installAdminBackend({ demands: [DEMANDS[0]!], bearerToken: TOKEN });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(1);
    const select = screen.getByTestId("admin-dispatch-target") as HTMLSelectElement;
    // submitted 只允许 in_progress / rejected：其余终态禁用
    const options = Array.from(select.options);
    expect(options.length).toBe(4);
    expect(options.find((o) => o.value === "in_progress")!.disabled).toBe(false);
    expect(options.find((o) => o.value === "rejected")!.disabled).toBe(false);
    for (const illegal of ["submitted", "delivered"]) {
      expect(options.find((o) => o.value === illegal)!.disabled).toBe(true);
    }

    fireEvent.change(screen.getByTestId("admin-dispatch-note"), {
      target: { value: "派发台接单" },
    });
    fireEvent.click(screen.getByTestId("admin-dispatch-submit"));

    await waitFor(() => {
      expect(
        screen.getByTestId("admin-demand-status").getAttribute("data-status"),
      ).toBe("in_progress");
    });

    const call = backend.calls.find((c) => c.method === "POST")!;
    expect(call.url.pathname).toBe("/internal/admin/demands/1/transition");
    expect(call.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
    expect(call.body).toEqual({
      target_status: "in_progress",
      by: "admin-console",
      note: "派发台接单",
    });
    // 时间线追加 admin-console 的派发记录
    expect(screen.getByTestId("admin-demand-last-event").textContent).toContain(
      "admin-console",
    );
  });

  it("rejected 必填理由：缺理由禁用提交，携带 reason 的调用形状", async () => {
    const backend = installAdminBackend({
      demands: [demand({ id: 2, status: "in_progress" })],
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2);
    fireEvent.change(screen.getByTestId("admin-dispatch-target"), {
      target: { value: "rejected" },
    });
    // 理由输入出现且提交禁用
    const submit = screen.getByTestId("admin-dispatch-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("admin-dispatch-reason"), {
      target: { value: "数据源不可达" },
    });
    expect((screen.getByTestId("admin-dispatch-submit") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId("admin-dispatch-submit"));

    await waitFor(() => {
      expect(
        screen.getByTestId("admin-demand-status").getAttribute("data-status"),
      ).toBe("rejected");
    });
    const call = backend.calls.find((c) => c.method === "POST")!;
    expect(call.body).toEqual({
      target_status: "rejected",
      by: "admin-console",
      reason: "数据源不可达",
    });
  });

  it("终态（delivered）不可派发：按钮禁用且无面板", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({ demands: [DEMANDS[2]!], bearerToken: TOKEN }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");
    const toggle = screen.getByTestId("admin-demand-dispatch-toggle") as HTMLButtonElement;
    expect(toggle.disabled).toBe(true);
    expect(screen.queryByTestId("admin-dispatch-panel")).toBeNull();
  });

  it("409 冲突如实回显（服务端是唯一裁决面）", async () => {
    // 假后端对迁移强制 409——面板提交后应出现冲突提示并保留原状态
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [DEMANDS[0]!],
        bearerToken: TOKEN,
        failTransitionStatus: 409,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(1);
    fireEvent.click(screen.getByTestId("admin-dispatch-submit"));
    const conflict = await screen.findByTestId("admin-dispatch-conflict");
    expect(conflict.textContent).toContain("不允许");
  });

  it("派发台文案不出现八个旧态名（三态 + rejected 为全集）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({ demands: DEMANDS, bearerToken: TOKEN }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");
    openPanel(1);

    const pageText = document.body.textContent ?? "";
    for (const legacy of [
      "evaluating",
      "quoted",
      "awaiting_confirm",
      "building",
      "delivery_acceptance",
      "评估中",
      "已报价",
      "待确认",
      "构建中",
      "交付验收",
    ]) {
      expect(pageText).not.toContain(legacy);
    }
  });

  it("转生成工单：仅 in_progress 单渲染出单区（submitted 面板无）", async () => {
    // 模块级 DEMANDS 会被前序用例的 transition 就地改状态（假后端共享
    // 引用）——这里自造新鲜数据，状态断言才可靠。
    vi.stubGlobal(
      "fetch",
      installAdminBackend(
        {
          demands: [
            demand({ id: 1, status: "submitted" }),
            demand({ id: 2, status: "in_progress" }),
          ],
          bearerToken: TOKEN,
        },
      ).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2); // in_progress：出单区在场
    expect(screen.getByTestId("admin-workorder-section")).toBeDefined();
    expect(screen.getByTestId("admin-workorder-submit")).toBeDefined();

    openPanel(1); // submitted：派发面板在场但无出单区
    expect(screen.getByTestId("admin-dispatch-panel")).toBeDefined();
    expect(screen.queryByTestId("admin-workorder-section")).toBeNull();
  });

  it("出单成功：调用形状 + 路径/全文/未投递文案，无投递成功假象", async () => {
    const backend = installAdminBackend({
      demands: [DEMANDS[1]!],
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2);
    fireEvent.click(screen.getByTestId("admin-workorder-submit"));

    const result = await screen.findByTestId("admin-workorder-result");
    expect(result.textContent).toContain(
      "reports/health-tickets/20261004-wire-DE-20261004-02.yaml",
    );
    // 工单全文在场（人工推送降级红线）且形态正确
    expect(screen.getByTestId("admin-workorder-yaml").textContent).toContain(
      "kind: generate",
    );
    // 诚实降级：明示未投递，页面无任何投递成功字样
    expect(screen.getByTestId("admin-workorder-undelivered").textContent).toContain(
      "未投递",
    );
    expect(document.body.textContent ?? "").not.toContain("投递成功");

    const call = backend.calls
      .filter((c) => c.method === "POST")
      .find((c) => c.url.pathname === "/internal/admin/demands/2/workorder")!;
    expect(call).toBeDefined();
    expect(call.body).toBeUndefined(); // 无 slug 覆写：不带请求体
    expect(call.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  it("出单失败（422 无源地址）：琥珀框如实回显服务端 detail", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [DEMANDS[1]!],
        bearerToken: TOKEN,
        failWorkorderStatus: 422,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2);
    fireEvent.click(screen.getByTestId("admin-workorder-submit"));

    const conflict = await screen.findByTestId("admin-dispatch-conflict");
    expect(conflict.textContent).toContain("source_urls");
    // 失败不渲染任何成功面
    expect(screen.queryByTestId("admin-workorder-result")).toBeNull();
  });

  it("slug 探测 unverified：如实放行释义在场（free 文案不出现）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [DEMANDS[1]!],
        bearerToken: TOKEN,
        workorderResult: {
          ticket_yaml: "kind: generate\nunit: spiders/example-com/\n",
          ticket_path: "reports/health-tickets/20261004-wire-DE-20261004-02.yaml",
          delivered: false,
          queued_for_pull: true,
          slug_check: "unverified",
        },
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2);
    fireEvent.click(screen.getByTestId("admin-workorder-submit"));

    const slug = await screen.findByTestId("admin-workorder-slug");
    expect(slug.textContent).toContain("探测通道不可达");
    expect(slug.textContent).not.toContain("已确证空闲");
  });
});

describe("DemandsPage 萬星直连（wire-wanxing-direct-dispatch 任务 3）", () => {
  it("in_progress 单：点击直连 → POST wanxing-dispatch + 回执面（reply/message_id/幂等键/slug）", async () => {
    const backend = installAdminBackend({
      demands: [demand({ id: 2, status: "in_progress" })],
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2);
    // 直连按钮与转生成工单并列在场（仅 in_progress 单渲染该区）
    fireEvent.click(screen.getByTestId("admin-demand-wanxing-dispatch"));

    // 成功回执面：萬星回执摘要在场（同步 A2A 回执）
    const result = await screen.findByTestId("admin-wanxing-result");
    expect(result.textContent).toContain("收到工单");
    expect(screen.getByTestId("admin-wanxing-message-id").textContent).toContain(
      "9f2c7e41-a7b3-4c8d-9e55-1f0a2b3c4d5e",
    );
    expect(
      screen.getByTestId("admin-wanxing-idempotency-key").textContent,
    ).toContain("wire-DE-20261004-02-9af3c1d2e4b56708");
    expect(screen.getByTestId("admin-wanxing-slug").textContent).toContain(
      "guangfu-zhuangji",
    );
    // 成功即无失败面
    expect(screen.queryByTestId("admin-wanxing-error")).toBeNull();

    // 调用形状：POST 管理端点 + 登录 Bearer（双闸 web 半边）
    const call = backend.calls
      .filter((c) => c.method === "POST")
      .find(
        (c) => c.url.pathname === "/internal/admin/demands/2/wanxing-dispatch",
      )!;
    expect(call).toBeDefined();
    expect(call.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  it("502 投递失败（detail 对象）：错误面显示 message + 工单全文 + 降级指引，无成功面", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [demand({ id: 2, status: "in_progress" })],
        bearerToken: TOKEN,
        failWanxingStatus: 502,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2);
    fireEvent.click(screen.getByTestId("admin-demand-wanxing-dispatch"));

    const error = await screen.findByTestId("admin-wanxing-error");
    expect(error.textContent).toContain("投递失败");
    // 工单全文随响应在场（人工推送降级红线，折叠 pre 形态照既有 yaml 展示）
    expect(screen.getByTestId("admin-wanxing-error-yaml").textContent).toContain(
      "kind: generate",
    );
    // 降级指引：人工推送或改走舰队拉取通道
    expect(error.textContent).toContain("舰队拉取");
    expect(screen.queryByTestId("admin-wanxing-result")).toBeNull();
  });

  it("非 in_progress 单不渲染直连按钮（submitted 面板展开亦无）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [demand({ id: 1, status: "submitted" })],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(1);
    expect(screen.getByTestId("admin-dispatch-panel")).toBeDefined();
    expect(screen.queryByTestId("admin-demand-wanxing-dispatch")).toBeNull();
  });

  it("直连留痕：pipeline_events 带已投递/失败事件 → 通道事实行在场（刷新后仍可见）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              {
                type: "wanxing_dispatched",
                reply_excerpt: "收到，开始处理",
                at: "2026-10-12T08:00:00+00:00",
              },
            ],
          }),
          demand({
            id: 4,
            status: "in_progress",
            pipeline_events: [
              {
                type: "wanxing_dispatch_failed",
                error: "403: CALLER_NOT_AUTHORIZED",
                at: "2026-10-12T08:10:00+00:00",
              },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    openPanel(2);
    const fact = screen.getByTestId("admin-wanxing-fact");
    expect(fact.textContent).toContain("已投递");
    expect(fact.textContent).toContain("收到，开始处理");

    openPanel(4);
    const failedFact = screen.getByTestId("admin-wanxing-fact-failed");
    expect(failedFact.textContent).toContain("CALLER_NOT_AUTHORIZED");
  });
});

describe("DemandsPage 编排进度面（wire-demand-visibility 任务 2.2）", () => {
  it("在途单：各阶段按序渲染 + 时间 + ticket_path + PR 链接", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              {
                at: "2026-10-04T10:00:00+00:00",
                type: "workorder_exported",
                ticket_path: "reports/health-tickets/20261004-wire-DE-20261004-02.yaml",
                ticket_yaml: "kind: generate\n",
                sha256: "abc123",
              },
              {
                at: "2026-10-04T10:30:00+00:00",
                type: "pipeline_run",
                pipeline_run_id: "20261004-wire-DE-20261004-02.yaml",
                ticket_path: "reports/health-tickets/20261004-wire-DE-20261004-02.yaml",
                pr_url: null,
              },
              { at: "2026-10-04T11:00:00+00:00", type: "workorder_submitted" },
              {
                at: "2026-10-04T12:00:00+00:00",
                type: "pr_open",
                pr_url: "https://github.com/finddata/industry/pull/42",
              },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(2);
    const entries = screen.getAllByTestId("admin-demand-progress-entry");
    expect(entries.map((el) => el.getAttribute("data-stage"))).toEqual([
      "workorder_exported",
      "pipeline_run",
      "workorder_submitted",
      "pr_open",
    ]);
    // 阶段文案 + 时间戳在场（顺序即事件顺序）
    const text = entries.map((el) => el.textContent!).join("\n");
    expect(text).toContain("已出单");
    expect(text).toContain("已投递（拉取器已落仓）");
    expect(text).toContain("已进入生成流水线");
    expect(text).toContain("爬虫已生成·待人工审核");
    expect(text).toContain(
      "reports/health-tickets/20261004-wire-DE-20261004-02.yaml",
    );
    expect(text).toContain("20261004-wire-DE-20261004-02.yaml");
    // 时间戳：本地化日期时间在场（格式断言避免时区脆弱）
    expect(text).toMatch(/\d{4}\/\d{2}\/\d{2}\s+\d{2}:\d{2}/);
    // PR 外链
    const pr = screen.getByTestId("admin-demand-progress-pr") as HTMLAnchorElement;
    expect(pr.getAttribute("href")).toBe(
      "https://github.com/finddata/industry/pull/42",
    );
    // 后续阶段未发生即不渲染
    expect(screen.queryByTestId("admin-demand-progress-empty")).toBeNull();
  });

  it("未出单：如实占位「尚未出单」，零阶段条目（不虚构进度）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [demand({ id: 1, status: "submitted" })],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(1);
    expect(
      screen.getByTestId("admin-demand-progress-empty").textContent,
    ).toContain("尚未出单");
    expect(screen.queryAllByTestId("admin-demand-progress-entry").length).toBe(0);
  });

  it("merged + scope ok：点亮阶段与「私有数据面已就绪」+ scope 名", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 3,
            status: "delivered",
            pipeline_events: [
              { at: "2026-10-04T10:00:00+00:00", type: "workorder_exported" },
              { at: "2026-10-04T12:00:00+00:00", type: "workorder_submitted" },
              { at: "2026-10-04T13:00:00+00:00", type: "pr_open" },
              {
                at: "2026-10-04T14:00:00+00:00",
                type: "merged",
                pr_url: "https://github.com/finddata/industry/pull/42",
              },
            ],
            scope_status: "ok",
            scope_name: "demand-DE-20261004-03",
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(3);
    const merged = screen
      .getAllByTestId("admin-demand-progress-entry")
      .find((el) => el.getAttribute("data-stage") === "merged")!;
    expect(merged.textContent).toContain("审核通过·部署点亮");
    // merged 绿点（客户面同款配色）
    expect(merged.querySelector(".bg-emerald-500")).not.toBeNull();
    const scope = screen.getByTestId("admin-demand-scope");
    expect(scope.getAttribute("data-scope-status")).toBe("ok");
    expect(scope.textContent).toContain("私有数据面已就绪");
    expect(scope.textContent).toContain("demand-DE-20261004-03");
  });

  it("scope pending：交付挂起·重试中", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 3,
            status: "delivered",
            pipeline_events: [
              { at: "2026-10-04T10:00:00+00:00", type: "workorder_exported" },
            ],
            scope_status: "pending",
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(3);
    const scope = screen.getByTestId("admin-demand-scope");
    expect(scope.getAttribute("data-scope-status")).toBe("pending");
    expect(scope.textContent).toContain("交付挂起·重试中");
    expect(scope.textContent).not.toContain("已就绪");
  });

  it("blocked：红点受阻阶段渲染（运营跟进语义）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              { at: "2026-10-04T10:00:00+00:00", type: "workorder_exported" },
              {
                at: "2026-10-04T11:00:00+00:00",
                type: "blocked",
                note: "工单提交连续 3 次未成功",
              },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(2);
    const blocked = screen
      .getAllByTestId("admin-demand-progress-entry")
      .find((el) => el.getAttribute("data-stage") === "blocked")!;
    expect(blocked.textContent).toContain("生成受阻·运营跟进中");
    expect(blocked.querySelector(".bg-rose-500")).not.toBeNull();
    // note 等内部字段不倾倒进进度面
    expect(blocked.textContent).not.toContain("工单提交连续 3 次未成功");
  });

  it("未知事件类型静默跳过：其余事件照常渲染、页面不崩", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              { at: "2026-10-04T10:00:00+00:00", type: "workorder_exported" },
              { at: "2026-10-04T10:15:00+00:00", type: "some_future_event", foo: 1 },
              { at: "2026-10-04T10:30:00+00:00", type: "pipeline_run" },
              { at: "2026-10-04T10:45:00+00:00", type: "gate_result", passed: true },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(2);
    const stages = screen
      .getAllByTestId("admin-demand-progress-entry")
      .map((el) => el.getAttribute("data-stage"));
    // 未知类型与 gate_result 均不进时间线；已知两类照常
    expect(stages).toEqual(["workorder_exported", "pipeline_run"]);
    expect(screen.getByTestId("admin-demand-progress")).toBeDefined();
  });

  it("gate 摘要：通过/未通过 + 尝试/流水线运行数（无 gate 则省略）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              { at: "2026-10-04T10:00:00+00:00", type: "pipeline_run" },
            ],
            gate: {
              passed: false,
              attempts: 2,
              pipeline_runs: 1,
              latest: { passed: false },
            },
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(2);
    const gate = screen.getByTestId("admin-demand-gate");
    expect(gate.getAttribute("data-passed")).toBe("false");
    expect(gate.textContent).toContain("门禁未通过");
    expect(gate.textContent).toContain("尝试 2 次");
    expect(gate.textContent).toContain("流水线运行 1 次");
  });

  // 自动出单进度面（wire-demand-auto-export 任务 2.3）：失败阶段+原因、
  // 自动徽标、占位语义。
  it("自动出单被拒：失败阶段+原因渲染、红点，且不再显示「尚未出单」占位", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              {
                at: "2026-10-04T10:00:00+00:00",
                type: "auto_export_failed",
                reason: "需求单 DE-20261004-02 没有 source_urls（存量单无源地址）",
                status_code: 422,
                trigger: "auto",
              },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(2);
    const failed = screen
      .getAllByTestId("admin-demand-progress-entry")
      .find((el) => el.getAttribute("data-stage") === "auto_export_failed")!;
    expect(failed.textContent).toContain("自动出单被拒·需人工处置");
    expect(failed.querySelector(".bg-rose-500")).not.toBeNull();
    // 失败原因以次要文本如实呈现
    const reason = screen.getByTestId("admin-demand-progress-reason");
    expect(reason.textContent).toContain("原因：");
    expect(reason.textContent).toContain("没有 source_urls");
    // 有失败留痕即不显示「尚未出单」占位
    expect(screen.queryByTestId("admin-demand-progress-empty")).toBeNull();
  });

  it("auto_export_failed 的 reason 非字符串：不猜不渲染原因", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              {
                at: "2026-10-04T10:00:00+00:00",
                type: "auto_export_failed",
                reason: 422,
                status_code: 422,
              },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(2);
    expect(
      screen
        .getAllByTestId("admin-demand-progress-entry")
        .find((el) => el.getAttribute("data-stage") === "auto_export_failed"),
    ).toBeDefined();
    expect(screen.queryByTestId("admin-demand-progress-reason")).toBeNull();
  });

  it("自动出单成功：trigger=auto 带自动徽标；人工出单（无 trigger）不带", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              {
                at: "2026-10-04T10:00:00+00:00",
                type: "workorder_exported",
                ticket_path:
                  "reports/health-tickets/20261004-wire-DE-20261004-02.yaml",
                trigger: "auto",
              },
            ],
          }),
          demand({
            id: 3,
            status: "delivered",
            pipeline_events: [
              { at: "2026-10-04T10:00:00+00:00", type: "workorder_exported" },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(2);
    const badge = screen.getByTestId("admin-demand-progress-auto");
    expect(badge.textContent).toBe("自动");
    // 徽标挂在「已出单」阶段行上
    expect(
      badge
        .closest('[data-testid="admin-demand-progress-entry"]')!
        .getAttribute("data-stage"),
    ).toBe("workorder_exported");

    // 人工出单（事件不带 trigger 键）：同一时间线零自动徽标
    expandProgress(3);
    expect(screen.queryAllByTestId("admin-demand-progress-auto").length).toBe(0);
  });

  it("占位语义：仅 auto_export_failed 留痕时不显示「尚未出单」", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({ id: 1, status: "submitted" }),
          demand({
            id: 2,
            status: "in_progress",
            pipeline_events: [
              {
                at: "2026-10-04T10:00:00+00:00",
                type: "auto_export_failed",
                reason: "slug 已被占用",
                status_code: 409,
                trigger: "auto",
              },
            ],
          }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    // 无任何尝试留档：占位在场、零阶段条目
    expandProgress(1);
    expect(
      screen.getByTestId("admin-demand-progress-empty").textContent,
    ).toContain("尚未出单");
    expect(screen.queryAllByTestId("admin-demand-progress-entry").length).toBe(0);

    // 仅失败留痕：占位退场，失败阶段在场
    expandProgress(2);
    expect(screen.queryByTestId("admin-demand-progress-empty")).toBeNull();
    expect(screen.getAllByTestId("admin-demand-progress-entry").length).toBe(1);
  });
});

describe("DemandsPage scope 重试（wire-scope-delivery-live 任务 2.1）", () => {
  it("按钮仅 delivered+pending 渲染：delivered+ok / in_progress / 未发起均无", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 3,
            status: "delivered",
            scope_status: "pending",
            delivery_events: [
              { type: "scope_create_failed", error: "scope API 未配置（SCOPE_API_BASE 为空）" },
            ],
          }),
          demand({
            id: 4,
            status: "delivered",
            scope_status: "ok",
            scope_name: "demand-DE-20261004-04",
          }),
          demand({ id: 2, status: "in_progress" }),
        ],
        bearerToken: TOKEN,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    // 进度面逐单展开（单开互斥）：仅 delivered+pending 的第 3 单出现重试按钮
    expandProgress(3);
    const buttons = screen.getAllByTestId("admin-demand-scope-retry");
    expect(buttons.length).toBe(1);
    const item = screen
      .getAllByTestId("admin-demand-item")
      .find((el) => el.textContent!.includes("DE-20261004-03"))!;
    expect(item.contains(buttons[0]!)).toBe(true);
    expect(screen.queryByTestId("admin-demand-scope-retry-error")).toBeNull();

    expandProgress(3); // 收起，换下一单
    expandProgress(4);
    // delivered+ok：scope 名照常呈现（无按钮、无重试错误位）
    expect(screen.queryByTestId("admin-demand-scope-retry")).toBeNull();
    expect(screen.getAllByTestId("admin-demand-scope-name").length).toBe(1);

    expandProgress(4);
    expandProgress(2);
    // in_progress（scope 未发起）：无按钮
    expect(screen.queryByTestId("admin-demand-scope-retry")).toBeNull();
  });

  it("点击成功：POST 形状 + scope 名出现 + scope 行转 ok + 按钮退场", async () => {
    const backend = installAdminBackend({
      demands: [
        demand({
          id: 3,
          status: "delivered",
          scope_status: "pending",
        }),
      ],
      bearerToken: TOKEN,
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(3);
    expect(
      screen.getByTestId("admin-demand-scope").getAttribute("data-scope-status"),
    ).toBe("pending");
    fireEvent.click(screen.getByTestId("admin-demand-scope-retry"));

    // 成功：scope 名行内呈现，scope 行如实转 ok
    const name = await screen.findByTestId("admin-demand-scope-name");
    expect(name.textContent).toContain("demand-DE-20261004-03");
    expect(
      screen.getByTestId("admin-demand-scope").getAttribute("data-scope-status"),
    ).toBe("ok");
    expect(screen.getByTestId("admin-demand-scope").textContent).toContain(
      "私有数据面已就绪",
    );
    // 不再 pending：按钮退场，无错误文案
    expect(screen.queryByTestId("admin-demand-scope-retry")).toBeNull();
    expect(screen.queryByTestId("admin-demand-scope-retry-error")).toBeNull();

    // 调用形状：POST 管理端点、无请求体、登录 Bearer
    const call = backend.calls
      .filter((c) => c.method === "POST")
      .find((c) => c.url.pathname === "/internal/admin/demands/3/scope-retry")!;
    expect(call).toBeDefined();
    expect(call.body).toBeUndefined();
    expect(call.headers["authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  it("点击失败（200 仍 pending）：最新 scope_create_failed 文案出现，按钮仍在可再试", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 3,
            status: "delivered",
            scope_status: "pending",
          }),
        ],
        bearerToken: TOKEN,
        scopeRetryOutcome: "fail",
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(3);
    fireEvent.click(screen.getByTestId("admin-demand-scope-retry"));

    const error = await screen.findByTestId("admin-demand-scope-retry-error");
    expect(error.textContent).toContain("scope API 未配置");
    // 如实失败：scope 行仍 pending、按钮仍在（可再次重试）、无 scope 名假象
    expect(
      screen.getByTestId("admin-demand-scope").getAttribute("data-scope-status"),
    ).toBe("pending");
    expect(screen.getByTestId("admin-demand-scope-retry")).toBeDefined();
    expect(screen.queryByTestId("admin-demand-scope-name")).toBeNull();
  });

  it("点击 409：服务端 detail 如实呈现（按钮保留）", async () => {
    vi.stubGlobal(
      "fetch",
      installAdminBackend({
        demands: [
          demand({
            id: 3,
            status: "delivered",
            scope_status: "pending",
          }),
        ],
        bearerToken: TOKEN,
        failScopeRetryStatus: 409,
      }).fetchImpl,
    );
    render(<DemandsPage />);
    await screen.findAllByTestId("admin-demand-item");

    expandProgress(3);
    fireEvent.click(screen.getByTestId("admin-demand-scope-retry"));

    const error = await screen.findByTestId("admin-demand-scope-retry-error");
    expect(error.textContent).toContain("scope 已交付或无 pending 留档");
    expect(screen.getByTestId("admin-demand-scope-retry")).toBeDefined();
  });
});

describe("demandsMachine 镜像（wire-ux-consolidation 任务 3.5）", () => {
  it("状态全集 = 三态 + rejected（旧五态不在全集）", () => {
    expect(ALL_DEMAND_STATUSES).toEqual([
      "submitted",
      "in_progress",
      "delivered",
      "rejected",
    ]);
  });

  it("白名单与后端 statemachine.py 一致：rejected 自 submitted/in_progress 均可达", () => {
    expect(ALLOWED_TRANSITIONS).toEqual({
      submitted: ["in_progress", "rejected"],
      in_progress: ["delivered", "rejected"],
      delivered: [],
      rejected: [],
    });
  });
});
