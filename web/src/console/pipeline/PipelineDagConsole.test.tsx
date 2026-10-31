// DAG 控制台组件测试（wire-dag-executor D7，batch 3 web）：
// 运行详情节点图（五态配色 queued 灰/running 蓝/succeeded 绿/failed 红/
// skipped 虚 + 点击节点看耗时/行数/错误 + 无 nodes 时步骤表向后兼容）/
// 在飞 run 2s 轮询（仅 queued/running，收敛后停）/ 取消按钮（queued/running
// 显示、POST 运行级取消端点、cancel_requested_at 已置则置灰并如实说明）/
// 版本编辑器「输入来源」高级区（缺省不下发 inputs、改动后下发步骤名、
// 前向引用与成环行内错误码且阻断保存）/ zh+en 键位在场。
// fetch 走 testing.ts 内存假后端（零网络）。
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
import zh from "../../i18n/zh";
import en from "../../i18n/en";
import { PipelineView } from "./PipelineView";
import { PipelineRunPanel } from "./PipelineRunPanel";
import {
  installConsoleBackend,
  seedConsoleSession,
  type FakePipelineRunSeed,
  type FakePipelineSeed,
} from "../testing";
import type {
  Dataset,
  PipelineRunNode,
  PipelineStepDef,
} from "../api";

const NOW = "2026-10-12T10:00:00";

function makeDataset(
  partial: Partial<Dataset> & Pick<Dataset, "id" | "name">,
): Dataset {
  return {
    key: partial.id.replace(/-/g, "_"),
    source_kind: "upload",
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "id", type: "INTEGER" },
      { name: "city", type: "TEXT" },
    ],
    row_count: 120,
    size_bytes: 4096,
    sync_cron: null,
    sync_status: "ok",
    paused_reason: null,
    created_at: NOW,
    updated_at: NOW,
    customer_id: "demo-customer",
    ...partial,
  };
}

const BASE_DATASETS = [
  makeDataset({ id: "ds-1", name: "订单明细" }),
  makeDataset({ id: "ds-2", name: "客户表" }),
];

const DERIVE_STEP: PipelineStepDef = {
  name: "过滤上海",
  op: "derive",
  params: {
    filters: [{ column: "city", op: "=", value: "上海" }],
    limit: 100,
  },
};
const JOIN_STEP: PipelineStepDef = {
  name: "挂 CPI",
  op: "join",
  params: {
    indicators: [{ code: "CPI_YOY", as: "cpi_yoy" }],
    date_column: "date",
  },
};
const LINK_STEP: PipelineStepDef = {
  name: "关联客户表",
  op: "link",
  params: {
    right_dataset_id: "ds-2",
    left_keys: ["cust_id"],
    right_keys: ["id"],
  },
};

const STEPS = [DERIVE_STEP, JOIN_STEP, LINK_STEP];

/** 五态节点（覆盖 D7 全部配色分支）。 */
const ALL_STATUS_NODES: PipelineRunNode[] = [
  {
    node_key: "排队节点",
    op: "derive",
    status: "queued",
    attempts: 0,
    started_at: null,
    finished_at: null,
    row_count: null,
    duration_ms: null,
    error_text: null,
  },
  {
    node_key: "运行节点",
    op: "join",
    status: "running",
    attempts: 1,
    started_at: "2026-10-13T09:00:00",
    finished_at: null,
    row_count: null,
    duration_ms: null,
    error_text: null,
  },
  {
    node_key: "成功节点",
    op: "link",
    status: "succeeded",
    attempts: 2,
    started_at: "2026-10-13T09:00:00",
    finished_at: "2026-10-13T09:01:00",
    row_count: 45,
    duration_ms: 90,
    error_text: null,
  },
  {
    node_key: "失败节点",
    op: "aggregate",
    status: "failed",
    attempts: 1,
    started_at: "2026-10-13T09:00:00",
    finished_at: "2026-10-13T09:01:00",
    row_count: null,
    duration_ms: 120,
    error_text: "boom：指标不可用",
  },
  {
    node_key: "跳过节点",
    op: "window",
    status: "skipped",
    attempts: 0,
    started_at: null,
    finished_at: null,
    row_count: null,
    duration_ms: null,
    error_text: null,
  },
];

function seedPipeline(
  partial: Partial<FakePipelineSeed> & { id: number },
): FakePipelineSeed {
  return {
    name: "营收日报",
    description: "每日营收加工链",
    base_dataset_id: "ds-1",
    versions: [{ steps: STEPS, created_at: "2026-10-12T10:00:00" }],
    ...partial,
  };
}

function seedRun(partial: Partial<FakePipelineRunSeed> & { id: number }) {
  return {
    status: "success" as const,
    version: 1,
    trigger: "manual" as const,
    started_at: "2026-10-13T09:00:00",
    finished_at: "2026-10-13T09:01:00",
    ...partial,
  };
}

function install(options?: Parameters<typeof installConsoleBackend>[1]) {
  const backend = installConsoleBackend([], options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

function callsOf(
  backend: ReturnType<typeof install>,
  filter: { method?: string; path: string },
) {
  return backend.calls.filter(
    (c) =>
      c.url.pathname === filter.path &&
      (filter.method == null || c.method === filter.method),
  );
}

function detailCalls(
  backend: ReturnType<typeof install>,
  runId: number,
  pipelineId = 1,
) {
  return backend.calls.filter(
    (c) =>
      c.method === "GET" &&
      c.url.pathname === `/api/v1/pipelines/${pipelineId}/runs/${runId}`,
  );
}

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

describe("运行详情节点图（D7）", () => {
  it("五态节点渲染 + 状态配色（queued 灰/running 蓝/succeeded 绿/failed 红/skipped 虚）", async () => {
    const backend = install({
      pipelines: [
        seedPipeline({
          id: 1,
          runs: [
            seedRun({
              id: 7,
              status: "running",
              finished_at: null,
              nodes: ALL_STATUS_NODES,
            }),
          ],
        }),
      ],
    });
    render(
      <PipelineRunPanel
        pipelineId={1}
        onOpenDataset={() => {}}
        pollIntervalMs={60000}
      />,
    );
    fireEvent.click(await screen.findByTestId("pipeline-history-row"));
    const result = await screen.findByTestId("pipeline-run-result");

    const graph = within(result).getByTestId("pipeline-run-node-graph");
    const nodes = within(graph).getAllByTestId("pipeline-run-node");
    expect(nodes).toHaveLength(5);
    expect(nodes.map((node) => node.getAttribute("data-status"))).toEqual([
      "queued",
      "running",
      "succeeded",
      "failed",
      "skipped",
    ]);
    expect(nodes[0]!.className).toContain("bg-slate-100");
    expect(nodes[1]!.className).toContain("bg-sky-50");
    expect(nodes[2]!.className).toContain("bg-emerald-50");
    expect(nodes[3]!.className).toContain("bg-red-50");
    expect(nodes[4]!.className).toContain("border-dashed");
    expect(detailCalls(backend, 7)).toHaveLength(1);
  });

  it("点击节点显示耗时/行数/错误（再次点击收起）", async () => {
    install({
      pipelines: [
        seedPipeline({
          id: 1,
          runs: [
            seedRun({
              id: 7,
              status: "running",
              finished_at: null,
              nodes: ALL_STATUS_NODES,
            }),
          ],
        }),
      ],
    });
    render(
      <PipelineRunPanel
        pipelineId={1}
        onOpenDataset={() => {}}
        pollIntervalMs={60000}
      />,
    );
    fireEvent.click(await screen.findByTestId("pipeline-history-row"));
    const result = await screen.findByTestId("pipeline-run-result");

    // 未点击时无明细面板。
    expect(within(result).queryByTestId("pipeline-run-node-detail")).toBeNull();

    const failedNode = within(result)
      .getAllByTestId("pipeline-run-node")
      .find((node) => node.getAttribute("data-node-key") === "失败节点")!;
    fireEvent.click(failedNode);
    const detail = await within(result).findByTestId("pipeline-run-node-detail");
    expect(detail.getAttribute("data-node-key")).toBe("失败节点");
    expect(
      within(detail).getByTestId("pipeline-run-node-duration").textContent,
    ).toBe("120 ms");
    expect(
      within(detail).getByTestId("pipeline-run-node-rows").textContent,
    ).toBe("行数 —");
    expect(
      within(detail).getByTestId("pipeline-run-node-attempts").textContent,
    ).toBe("尝试 1 次");
    expect(
      within(detail).getByTestId("pipeline-run-node-error").textContent,
    ).toContain("boom：指标不可用");

    // 点击成功节点：耗时与行数如实。
    const succeededNode = within(result)
      .getAllByTestId("pipeline-run-node")
      .find((node) => node.getAttribute("data-node-key") === "成功节点")!;
    fireEvent.click(succeededNode);
    await waitFor(() =>
      expect(
        within(result).getByTestId("pipeline-run-node-rows").textContent,
      ).toBe("45 行"),
    );
    expect(
      within(result).getByTestId("pipeline-run-node-duration").textContent,
    ).toBe("90 ms");
    expect(
      within(result).getByTestId("pipeline-run-node-attempts").textContent,
    ).toBe("尝试 2 次");

    // 再次点击同一节点收起明细。
    fireEvent.click(succeededNode);
    await waitFor(() =>
      expect(within(result).queryByTestId("pipeline-run-node-detail")).toBeNull(),
    );
  });

  it("无 nodes 的旧响应：不渲染节点图，步骤表照常工作（向后兼容）", async () => {
    install({
      pipelines: [
        seedPipeline({
          id: 1,
          runs: [seedRun({ id: 7, status: "success" })],
        }),
      ],
    });
    render(
      <PipelineRunPanel
        pipelineId={1}
        onOpenDataset={() => {}}
        pollIntervalMs={60000}
      />,
    );
    fireEvent.click(await screen.findByTestId("pipeline-history-row"));
    const result = await screen.findByTestId("pipeline-run-result");
    expect(within(result).queryByTestId("pipeline-run-node-graph")).toBeNull();
    expect(within(result).getAllByTestId("pipeline-run-step")).toHaveLength(3);
  });
});

describe("在飞 run 轮询（D7）", () => {
  it("queued/running 时按间隔轮询，收敛后停止", async () => {
    const backend = install({
      pipelines: [
        seedPipeline({
          id: 1,
          runs: [
            seedRun({
              id: 3,
              status: "queued",
              finished_at: null,
              nodes: [
                {
                  node_key: "过滤上海",
                  op: "derive",
                  status: "queued",
                  attempts: 0,
                  started_at: null,
                  finished_at: null,
                  row_count: null,
                  duration_ms: null,
                  error_text: null,
                },
              ],
            }),
          ],
        }),
      ],
    });
    render(
      <PipelineRunPanel
        pipelineId={1}
        onOpenDataset={() => {}}
        pollIntervalMs={40}
      />,
    );
    fireEvent.click(await screen.findByTestId("pipeline-history-row"));
    const result = await screen.findByTestId("pipeline-run-result");
    expect(result.getAttribute("data-status")).toBe("queued");
    expect(
      within(result).getByTestId("pipeline-run-polling-note"),
    ).toBeTruthy();

    // 轮询推进：详情 GET 次数增长（至少 3 次）。
    await waitFor(() => expect(detailCalls(backend, 3).length).toBeGreaterThanOrEqual(3));

    // 收敛为 success：轮询拉到新状态后停止。
    backend.updatePipelineRun(1, 3, {
      status: "success",
      finished_at: "2026-10-13T09:05:00",
      nodes: [
        {
          node_key: "过滤上海",
          op: "derive",
          status: "succeeded",
          attempts: 1,
          started_at: "2026-10-13T09:00:00",
          finished_at: "2026-10-13T09:01:00",
          row_count: 45,
          duration_ms: 90,
          error_text: null,
        },
      ],
    });
    await waitFor(() =>
      expect(
        screen.getByTestId("pipeline-run-result").getAttribute("data-status"),
      ).toBe("success"),
    );
    // 收敛后不再轮询（留一段观察窗，详情 GET 次数不增长）。
    await new Promise((resolve) => setTimeout(resolve, 80));
    const settled = detailCalls(backend, 3).length;
    await new Promise((resolve) => setTimeout(resolve, 160));
    expect(detailCalls(backend, 3).length).toBe(settled);
    // 历史摘要如实更新为 success。
    expect(backend.getPipelineRuns(1)[0]!.status).toBe("success");
  });
});

describe("取消运行（D5）", () => {
  it("queued/running 显示取消按钮：POST 运行级端点，置灰并如实说明", async () => {
    const backend = install({
      pipelines: [
        seedPipeline({
          id: 1,
          runs: [seedRun({ id: 5, status: "running", finished_at: null })],
        }),
      ],
    });
    render(
      <PipelineRunPanel
        pipelineId={1}
        onOpenDataset={() => {}}
        pollIntervalMs={60000}
      />,
    );
    fireEvent.click(await screen.findByTestId("pipeline-history-row"));
    const result = await screen.findByTestId("pipeline-run-result");
    const cancel = within(result).getByTestId(
      "pipeline-run-cancel",
    ) as HTMLButtonElement;
    expect(cancel.disabled).toBe(false);
    expect(cancel.textContent).toBe("取消运行");

    fireEvent.click(cancel);
    await waitFor(() =>
      expect(
        callsOf(backend, { method: "POST", path: "/api/v1/pipelines/runs/5/cancel" }),
      ).toHaveLength(1),
    );
    await waitFor(() =>
      expect(
        (
          within(screen.getByTestId("pipeline-run-result")).getByTestId(
            "pipeline-run-cancel",
          ) as HTMLButtonElement
        ).disabled,
      ).toBe(true),
    );
    const after = screen.getByTestId("pipeline-run-result");
    expect(
      within(after).getByTestId("pipeline-run-cancel").textContent,
    ).toBe("已请求取消");
    expect(
      within(after).getByTestId("pipeline-run-cancel-note").textContent,
    ).toContain("已请求取消");
    // 后端如实落 cancel_requested_at（重复点击不再请求）。
    expect(backend.getPipelineRuns(1)[0]!.cancel_requested_at).not.toBeNull();
    fireEvent.click(
      within(after).getByTestId("pipeline-run-cancel") as HTMLButtonElement,
    );
    expect(
      callsOf(backend, { method: "POST", path: "/api/v1/pipelines/runs/5/cancel" }),
    ).toHaveLength(1);
  });

  it("cancel_requested_at 已置：按钮初始置灰，终态 run 不显示取消", async () => {
    install({
      pipelines: [
        seedPipeline({
          id: 1,
          runs: [
            seedRun({
              id: 6,
              status: "running",
              finished_at: null,
              cancel_requested_at: "2026-10-13T09:30:00",
            }),
            seedRun({ id: 2, status: "success" }),
          ],
        }),
      ],
    });
    render(
      <PipelineRunPanel
        pipelineId={1}
        onOpenDataset={() => {}}
        pollIntervalMs={60000}
      />,
    );
    const rows = await screen.findAllByTestId("pipeline-history-row");
    fireEvent.click(rows[0]!);
    const result = await screen.findByTestId("pipeline-run-result");
    expect(
      (
        within(result).getByTestId("pipeline-run-cancel") as HTMLButtonElement
      ).disabled,
    ).toBe(true);

    // 终态（success）不显示取消按钮。
    fireEvent.click(rows[1]!);
    await waitFor(() =>
      expect(
        screen.getByTestId("pipeline-run-result").getAttribute("data-status"),
      ).toBe("success"),
    );
    expect(
      within(screen.getByTestId("pipeline-run-result")).queryByTestId(
        "pipeline-run-cancel",
      ),
    ).toBeNull();
  });
});

describe("版本编辑器「输入来源」高级区（D1/D7）", () => {
  async function openEditor() {
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-edit"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.click(screen.getByTestId("pipeline-inputs-toggle"));
    await screen.findByTestId("pipeline-inputs-section");
  }

  it("高级区缺省收起；未改动则保存载荷不含 inputs（v1 逐字节保持）", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-edit"));
    await screen.findByTestId("pipeline-editor");
    // 缺省收起：未展开前无分区内容。
    expect(screen.getByTestId("pipeline-inputs-toggle")).toBeTruthy();
    expect(screen.queryByTestId("pipeline-inputs-section")).toBeNull();

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    const puts = callsOf(backend, { method: "PUT", path: "/api/v1/pipelines/1" });
    expect(puts).toHaveLength(1);
    const steps = (puts[0]!.body as { steps: PipelineStepDef[] }).steps;
    // 无改动 → 每个步骤都不带 inputs 键。
    for (const step of steps) {
      expect(Object.prototype.hasOwnProperty.call(step, "inputs")).toBe(false);
    }
  });

  it("改动后下发步骤名；默认前一步 + base 显式表达（并产生新版本）", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    await openEditor();

    // 步骤 2 的缺省输入 = 前一步（过滤上海）；额外勾选基础数据集。
    const rows = screen.getAllByTestId("pipeline-inputs-step");
    expect(rows).toHaveLength(3);
    expect(
      within(rows[1]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "过滤上海")!
        .getAttribute("data-selected"),
    ).toBe("true");
    fireEvent.click(
      within(rows[1]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "base")!,
    );

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    const puts = callsOf(backend, { method: "PUT", path: "/api/v1/pipelines/1" });
    expect(puts).toHaveLength(1);
    const steps = (puts[0]!.body as { steps: PipelineStepDef[] }).steps;
    expect(steps[0]!.inputs).toBeUndefined();
    expect(steps[1]!.inputs).toEqual(["base", "过滤上海"]);
    expect(steps[2]!.inputs).toBeUndefined();
    // 版本追加为 v2（fake 后端按 inputs 差异判定变更）。
    expect(screen.getByTestId("pipeline-detail-version").textContent).toBe("v2");
  });

  it("前向引用：行内错误码 inputs_forward，保存被阻断（不发 PUT）", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    await openEditor();

    const rows = screen.getAllByTestId("pipeline-inputs-step");
    // 步骤 2 显式改为 base（断开其指向前一步的边，避免形成环）。
    fireEvent.click(
      within(rows[1]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "过滤上海")!,
    );
    fireEvent.click(
      within(rows[1]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "base")!,
    );
    // 步骤 1：取消 base，改选后序步骤「挂 CPI」（步骤 2）→ 前向引用。
    fireEvent.click(
      within(rows[0]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "base")!,
    );
    fireEvent.click(
      within(rows[0]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "挂 CPI")!,
    );
    const error = (await screen.findByTestId("pipeline-step-inputs-error")) as HTMLElement;
    expect(error.getAttribute("data-code")).toBe("inputs_forward");
    expect(error.textContent).toContain("inputs_forward");

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    expect(
      callsOf(backend, { method: "PUT", path: "/api/v1/pipelines/1" }),
    ).toHaveLength(0);
  });

  it("成环：环边两端行内错误码 inputs_cycle，保存被阻断", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    await openEditor();

    const rows = screen.getAllByTestId("pipeline-inputs-step");
    // 步骤 1 → 步骤 3（前向）。
    fireEvent.click(
      within(rows[0]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "base")!,
    );
    fireEvent.click(
      within(rows[0]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "关联客户表")!,
    );
    // 步骤 3 → 步骤 1（成环）。
    fireEvent.click(
      within(rows[2]!)
        .getAllByTestId("pipeline-inputs-option")
        .find((option) => option.getAttribute("data-name") === "过滤上海")!,
    );

    await waitFor(() => {
      const errors = screen.getAllByTestId("pipeline-step-inputs-error");
      expect(errors.length).toBeGreaterThanOrEqual(2);
      for (const error of errors) {
        expect(error.getAttribute("data-code")).toBe("inputs_cycle");
      }
    });

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    expect(
      callsOf(backend, { method: "PUT", path: "/api/v1/pipelines/1" }),
    ).toHaveLength(0);
  });
});

describe("i18n 键位（zh/en 同时在场）", () => {
  it("新增运行/节点/取消/输入来源键在两语言均为非空字符串", () => {
    const keys = [
      "runStatus.queued",
      "runStatus.running",
      "runStatus.cancelled",
      "nodeStatus.queued",
      "nodeStatus.running",
      "nodeStatus.succeeded",
      "nodeStatus.failed",
      "nodeStatus.skipped",
      "nodeGraphTitle",
      "nodeGraphHint",
      "nodeAttempts",
      "nodeNoDuration",
      "nodeNoRows",
      "nodeStartedAt",
      "nodeFinishedAt",
      "cancelRun",
      "cancelling",
      "cancelRequested",
      "cancelRequestedNote",
      "cancelError",
      "pollingNote",
      "inputsAdvanced",
      "inputsHint",
      "inputsBase",
      "inputsForwardMark",
      "inputsStepLabel",
      "inputsNone",
      "inputsErrors.inputs_cycle",
      "inputsErrors.inputs_forward",
    ];
    const pick = (locale: Record<string, unknown>) =>
      keys.map((key) => {
        let node: unknown = (
          locale.console as Record<string, unknown>
        ).datasets;
        node = (node as Record<string, unknown>).pipeline;
        node = (node as Record<string, unknown>).workbench;
        for (const part of key.split(".")) {
          node = (node as Record<string, unknown>)[part];
        }
        return node;
      });
    for (const [name, locale] of [
      ["zh", zh as Record<string, unknown>],
      ["en", en as Record<string, unknown>],
    ] as const) {
      const values = pick(locale);
      for (let i = 0; i < keys.length; i += 1) {
        expect(
          typeof values[i] === "string" && (values[i] as string).trim().length > 0,
          `${name} 缺少键 ${keys[i]}`,
        ).toBe(true);
      }
    }
  });
});
