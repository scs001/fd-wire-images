// 管线工作台组件测试（wire-transform-pipelines 任务 5.1-5.5）：
// 列表渲染（版本徽标/步骤数/调度态/最近运行状态章+触发方式）/ 编辑器
// 快乐路径（新建三类型步骤、参数形状与原子操作一致）+ 步骤增删改序 +
// 新版本明示文案（改步骤 vN+1 / 仅改描述不动版本）/ 运行交互（in-flight
// 禁用、成功步骤、失败定位与「后续步骤未执行」、配额与引擎不支持如实态、
// 不伪造失败后的步骤）/ 输出槽位稳定（同版本重跑同 dataset id）/ 历史
// 分页与展开 / 调度交互（挂接/暂停/恢复/删除 + 输出受管提示）/ viewer 403
// 只读降级 / 列表错误态重试。fetch 走 testing.ts 内存假后端（零网络）。
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
import { PipelineView } from "./PipelineView";
import {
  installConsoleBackend,
  seedConsoleSession,
  type FakePipelineSeed,
} from "../testing";
import type { Dataset, PipelineStepDef } from "../api";

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

/** 三步管线种子（v1；runs/failAt 按测试注入）。 */
function seedPipeline(partial: Partial<FakePipelineSeed> & { id: number }): FakePipelineSeed {
  return {
    name: "营收日报",
    description: "每日营收加工链",
    base_dataset_id: "ds-1",
    versions: [
      { steps: [DERIVE_STEP, JOIN_STEP, LINK_STEP], created_at: "2026-10-12T10:00:00" },
    ],
    ...partial,
  };
}

function install(options?: Parameters<typeof installConsoleBackend>[1]) {
  const backend = installConsoleBackend([], options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

function callsOf(backend: ReturnType<typeof install>, filter: {
  method?: string;
  path: string;
}) {
  return backend.calls.filter(
    (c) =>
      c.url.pathname === filter.path &&
      (filter.method == null || c.method === filter.method),
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

describe("管线工作台 · 列表（5.1）", () => {
  it("列表渲染：名称/版本徽标/步骤数/调度 cron 与启停态/最近运行状态章+触发方式", async () => {
    install({
      datasets: BASE_DATASETS,
      pipelines: [
        seedPipeline({
          id: 1,
          schedule: {
            cron: "0 6 * * *",
            enabled: true,
            last_run_at: "2026-10-13T06:00:00",
            last_status: "success",
          },
          runs: [
            {
              id: 11,
              status: "failed",
              version: 1,
              trigger: "schedule",
              started_at: "2026-10-13T06:00:00",
            },
          ],
        }),
        seedPipeline({
          id: 2,
          name: "一次性加工",
          schedule: {
            cron: "0 9 * * 1",
            enabled: false,
            last_run_at: null,
            last_status: null,
          },
        }),
      ],
    });
    render(<PipelineView onOpenDataset={() => {}} />);

    const rows = await screen.findAllByTestId("pipeline-row");
    expect(rows.map((el) => el.getAttribute("data-pipeline-id"))).toEqual(["2", "1"]);

    // 管线 1：v1 徽标、3 步、调度 cron 启用态、最近运行失败章 + 调度触发
    const row1 = within(rows[1]!);
    expect(row1.getByTestId("pipeline-row-name").textContent).toBe("营收日报");
    expect(row1.getByTestId("pipeline-row-version").textContent).toBe("v1");
    expect(row1.getByTestId("pipeline-row-version").getAttribute("data-version")).toBe("1");
    expect(row1.getByTestId("pipeline-row-steps").textContent).toBe("3 步");
    const schedule = row1.getByTestId("pipeline-row-schedule");
    expect(schedule.getAttribute("data-cron")).toBe("0 6 * * *");
    expect(schedule.getAttribute("data-enabled")).toBe("true");
    expect(schedule.textContent).toContain("0 6 * * *");
    const lastRun = row1.getByTestId("pipeline-row-lastrun");
    expect(lastRun.getAttribute("data-status")).toBe("failed");
    expect(lastRun.getAttribute("data-trigger")).toBe("schedule");
    expect(lastRun.textContent).toContain("失败");
    expect(lastRun.textContent).toContain("调度");

    // 管线 2：调度已暂停（cron 带「已暂停」）、尚未运行
    const row2 = within(rows[0]!);
    const paused = row2.getByTestId("pipeline-row-schedule");
    expect(paused.getAttribute("data-enabled")).toBe("false");
    expect(paused.textContent).toContain("已暂停");
    expect(row2.getByTestId("pipeline-row-norun").textContent).toBe("尚未运行");
  });

  it("空列表引导 + 错误态如实（detail 呈现）与重试重发", async () => {
    const backend = install({ failPipelinesStatus: 503 });
    render(<PipelineView onOpenDataset={() => {}} />);

    const error = await screen.findByTestId("pipeline-list-error");
    expect(error.textContent).toContain("管线图加载失败");
    expect(screen.queryByTestId("pipeline-list")).toBeNull();

    fireEvent.click(screen.getByTestId("pipeline-list-retry"));
    await waitFor(() =>
      expect(callsOf(backend, { method: "GET", path: "/api/v1/pipelines" })).toHaveLength(2),
    );

    // 无失败注入的空态：引导新建
    cleanup();
    install({ pipelines: [] });
    render(<PipelineView onOpenDataset={() => {}} />);
    expect(await screen.findByTestId("pipeline-list-empty")).toBeTruthy();
    expect(screen.getByTestId("pipeline-list-empty").textContent).toContain("还没有管线");
  });
});

describe("管线工作台 · 编辑器（5.2）", () => {
  it("新建快乐路径：三类型步骤参数形状与原子操作一致（derive 结构化 / join JSON / link 右表+键列）", async () => {
    const backend = install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);

    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");

    fireEvent.change(screen.getByTestId("pipeline-editor-name"), {
      target: { value: "新建加工链" },
    });
    fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
      target: { value: "ds-1" },
    });

    // 步骤 1：derive（默认）——名称 + limit + filters JSON（新建从零步起步）
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    fireEvent.change(screen.getByTestId("pipeline-step-name"), {
      target: { value: "过滤上海" },
    });
    fireEvent.change(screen.getByTestId("pipeline-step-limit"), {
      target: { value: "100" },
    });
    fireEvent.change(screen.getByTestId("pipeline-step-filters"), {
      target: { value: '[{"column":"city","op":"=","value":"上海"}]' },
    });

    // 步骤 2：join——JSON 整体
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps = screen.getAllByTestId("pipeline-step");
    fireEvent.change(within(steps[1]!).getByTestId("pipeline-step-name"), {
      target: { value: "挂 CPI" },
    });
    fireEvent.change(within(steps[1]!).getByTestId("pipeline-step-op"), {
      target: { value: "join" },
    });
    fireEvent.change(within(steps[1]!).getByTestId("pipeline-step-json"), {
      target: {
        value:
          '{"indicators":[{"code":"CPI_YOY","as":"cpi_yoy"}],"date_column":"date"}',
      },
    });

    // 步骤 3：link——右表 picker + 左右键列
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps3 = screen.getAllByTestId("pipeline-step");
    fireEvent.change(within(steps3[2]!).getByTestId("pipeline-step-name"), {
      target: { value: "关联客户表" },
    });
    fireEvent.change(within(steps3[2]!).getByTestId("pipeline-step-op"), {
      target: { value: "link" },
    });
    fireEvent.change(within(steps3[2]!).getByTestId("pipeline-step-right"), {
      target: { value: "ds-2" },
    });
    fireEvent.change(within(steps3[2]!).getByTestId("pipeline-step-left-keys"), {
      target: { value: "cust_id" },
    });
    fireEvent.change(within(steps3[2]!).getByTestId("pipeline-step-right-keys"), {
      target: { value: "id" },
    });

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");

    const created = callsOf(backend, { method: "POST", path: "/api/v1/pipelines" });
    expect(created).toHaveLength(1);
    expect(created[0]!.body).toEqual({
      name: "新建加工链",
      description: undefined,
      base_dataset_id: "ds-1",
      steps: [
        {
          name: "过滤上海",
          op: "derive",
          params: {
            filters: [{ column: "city", op: "=", value: "上海" }],
            limit: 100,
          },
        },
        {
          name: "挂 CPI",
          op: "join",
          params: {
            indicators: [{ code: "CPI_YOY", as: "cpi_yoy" }],
            date_column: "date",
          },
        },
        {
          name: "关联客户表",
          op: "link",
          params: {
            right_dataset_id: "ds-2",
            left_keys: ["cust_id"],
            right_keys: ["id"],
          },
        },
      ],
    });
    // 建成即 v1，详情头部版本徽标在场
    expect(screen.getByTestId("pipeline-detail-version").textContent).toBe("v1");
  });

  it("步骤增删改序：上移/下移/移除；非法 JSON 行内回显且不发请求", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-edit"));
    await screen.findByTestId("pipeline-editor");

    // 上移第 2 步（join）到第 1：derive 的上移在顶部应禁用
    const stepsBefore = screen.getAllByTestId("pipeline-step");
    expect(
      (within(stepsBefore[0]!).getByTestId("pipeline-step-up") as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.click(
      within(screen.getAllByTestId("pipeline-step")[1]!).getByTestId(
        "pipeline-step-up",
      ),
    );
    await waitFor(() =>
      expect(
        screen
          .getAllByTestId("pipeline-step")
          .map((el) => el.getAttribute("data-op")),
      ).toEqual(["join", "derive", "link"]),
    );
    // 下移禁用边界（末步）
    expect(
      (within(screen.getAllByTestId("pipeline-step")[2]!).getByTestId(
        "pipeline-step-down",
      ) as HTMLButtonElement).disabled,
    ).toBe(true);

    // 移除第 3 步（link）
    fireEvent.click(
      within(screen.getAllByTestId("pipeline-step")[2]!).getByTestId(
        "pipeline-step-remove",
      ),
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("pipeline-step")).toHaveLength(2),
    );

    // derive filters 非法 JSON：保存行内回显、不发 PUT
    fireEvent.change(
      within(screen.getAllByTestId("pipeline-step")[1]!).getByTestId(
        "pipeline-step-filters",
      ),
      { target: { value: "[not json" } },
    );
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-step-json-error");
    expect(screen.getByTestId("pipeline-step-json-error").textContent).toContain(
      "参数 JSON 不合法",
    );
    expect(callsOf(backend, { method: "PUT", path: "/api/v1/pipelines/1" })).toEqual([]);
  });

  it("版本语义明示：改步骤出「步骤变更将创建新版本 v2」，保存后徽标升 v2、v1 原样可查", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    expect(screen.getByTestId("pipeline-detail-version").textContent).toBe("v1");
    fireEvent.click(screen.getByTestId("pipeline-edit"));
    await screen.findByTestId("pipeline-editor");

    // 未改动：无新版本明示
    expect(screen.queryByTestId("pipeline-version-confirm")).toBeNull();

    // 改第 1 步名称 → 明示 v2；保存 PUT 带 steps
    fireEvent.change(
      within(screen.getAllByTestId("pipeline-step")[0]!).getByTestId(
        "pipeline-step-name",
      ),
      { target: { value: "过滤北京" } },
    );
    expect(screen.getByTestId("pipeline-version-confirm").textContent).toBe(
      "步骤变更将创建新版本 v2",
    );
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    const put = callsOf(backend, { method: "PUT", path: "/api/v1/pipelines/1" });
    expect(put).toHaveLength(1);
    expect(
      (put[0]!.body as { steps: unknown[] }).steps,
    ).toHaveLength(3);
    // 详情徽标升 v2；版本历史两版（v2 当前、v1 原样）
    expect(screen.getByTestId("pipeline-detail-version").textContent).toBe("v2");
    const versions = screen.getAllByTestId("pipeline-version");
    expect(versions.map((el) => el.getAttribute("data-version"))).toEqual(["2", "1"]);
    expect(
      screen.getByTestId("pipeline-version-current-2").textContent,
    ).toBe("当前版本");
    // 假后端断言面：版本 append-only（v1 步骤原样）
    expect(backend.getPipelines()[0]!.versions).toHaveLength(2);
    expect(backend.getPipelines()[0]!.versions[0]!.steps[0]!.name).toBe("过滤上海");
  });

  it("仅改描述不动版本：无新版本明示，保存后仍 v1", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-edit"));
    await screen.findByTestId("pipeline-editor");

    fireEvent.change(screen.getByTestId("pipeline-editor-desc"), {
      target: { value: "改个描述" },
    });
    expect(screen.queryByTestId("pipeline-version-confirm")).toBeNull();
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    expect(screen.getByTestId("pipeline-detail-version").textContent).toBe("v1");
    expect(backend.getPipelines()[0]!.versions).toHaveLength(1);
  });

  it("viewer 403：编辑器切只读态（字段禁用、保存收起、提示如实）", async () => {
    install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
      teamMembers: [{ logtoSub: "demo-customer", role: "viewer" }],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-edit"));
    await screen.findByTestId("pipeline-editor");

    fireEvent.change(screen.getByTestId("pipeline-editor-name"), {
      target: { value: "想改个名" },
    });
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));

    const editor = await screen.findByTestId("pipeline-editor");
    expect(editor.getAttribute("data-readonly")).toBe("true");
    expect(screen.getByTestId("pipeline-editor-readonly").textContent).toContain(
      "viewer",
    );
    expect(screen.queryByTestId("pipeline-editor-save")).toBeNull();
    expect(
      (screen.getByTestId("pipeline-editor-name") as HTMLInputElement).disabled,
    ).toBe(true);
  });
});

describe("管线工作台 · 运行与历史（5.3）", () => {
  it("立即运行：in-flight 禁用 + 运行中文案；成功结果逐步骤卡（行数/耗时/输出跳转）；同版本重跑输出槽位稳定", async () => {
    const onOpen = vi.fn();
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
      pipelineRunDelayMs: 30,
    });
    render(<PipelineView onOpenDataset={onOpen} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");

    const runButton = screen.getByTestId("pipeline-run-button") as HTMLButtonElement;
    expect(runButton.textContent).toBe("立即运行");
    fireEvent.click(runButton);
    // in-flight：禁用 + 运行中…
    expect(runButton.disabled).toBe(true);
    expect(runButton.textContent).toBe("运行中…");

    const result = await screen.findByTestId("pipeline-run-result");
    expect(result.getAttribute("data-status")).toBe("success");
    expect(result.getAttribute("data-version")).toBe("1");
    expect(result.getAttribute("data-trigger")).toBe("manual");

    // 三步按序成功：op 标签、行数、耗时、输出数据集
    const steps = screen.getAllByTestId("pipeline-run-step");
    expect(steps.map((el) => el.getAttribute("data-seq"))).toEqual(["1", "2", "3"]);
    expect(steps.map((el) => el.getAttribute("data-op"))).toEqual([
      "derive",
      "join",
      "link",
    ]);
    expect(steps.map((el) => el.getAttribute("data-status"))).toEqual([
      "success",
      "success",
      "success",
    ]);
    expect(
      within(steps[0]!).getByTestId("pipeline-run-step-op").textContent,
    ).toBe("派生");
    expect(
      within(steps[1]!).getByTestId("pipeline-run-step-op").textContent,
    ).toBe("挂接指标");
    expect(
      within(steps[2]!).getByTestId("pipeline-run-step-op").textContent,
    ).toBe("关联数据集");
    expect(within(steps[0]!).getByTestId("pipeline-run-step-rows").textContent).toBe(
      "120 行",
    );
    expect(
      within(steps[0]!).getByTestId("pipeline-run-step-duration").textContent,
    ).toBe("120 ms");

    // 输出数据集跳转（onOpenDataset 携带输出 dataset id）
    const output = within(steps[0]!).getByTestId("pipeline-run-step-output");
    expect(output.getAttribute("data-dataset-id")).toBe("pl-1-v1-s1");
    fireEvent.click(output);
    expect(onOpen).toHaveBeenCalledWith("pl-1-v1-s1");

    // 运行后按钮复原；历史新增一条（手动）
    await waitFor(() => expect(runButton.disabled).toBe(false));
    const historyRows = screen.getAllByTestId("pipeline-history-row");
    expect(historyRows).toHaveLength(1);
    expect(historyRows[0]!.getAttribute("data-trigger")).toBe("manual");

    // 同版本重跑：输出槽位稳定（同 dataset id = 原地刷新语义）
    fireEvent.click(runButton);
    await waitFor(() =>
      expect(screen.getAllByTestId("pipeline-history-row")).toHaveLength(2),
    );
    const second = screen.getAllByTestId("pipeline-run-step");
    expect(
      within(second[0]!).getByTestId("pipeline-run-step-output").getAttribute(
        "data-dataset-id",
      ),
    ).toBe("pl-1-v1-s1");
    expect(backend.getPipelineRuns(1)).toHaveLength(2);
  });

  it("失败隔离：第二步失败如实定位（错误摘要 + 后续步骤未执行注记），第三步无记录不伪造", async () => {
    install({
      datasets: BASE_DATASETS,
      pipelines: [
        seedPipeline({
          id: 1,
          failAt: { seq: 2, kind: "failed", error: "join 失败：指标 code 不存在" },
        }),
      ],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-run-button"));

    const result = await screen.findByTestId("pipeline-run-result");
    expect(result.getAttribute("data-status")).toBe("failed");
    const steps = screen.getAllByTestId("pipeline-run-step");
    expect(steps.map((el) => el.getAttribute("data-status"))).toEqual([
      "success",
      "failed",
    ]);
    // 失败步骤：错误摘要 + 「后续步骤未执行」注记
    const failed = within(steps[1]!);
    expect(failed.getByTestId("pipeline-run-step-error").textContent).toContain(
      "join 失败：指标 code 不存在",
    );
    expect(screen.getByTestId("pipeline-run-aborted-note").textContent).toBe(
      "后续步骤未执行（该步骤失败后管线中止）。",
    );
    // 第三步无 step 记录（绝不伪造 skipped）
    expect(
      screen
        .getAllByTestId("pipeline-run-step")
        .filter((el) => el.getAttribute("data-seq") === "3"),
    ).toHaveLength(0);
    // 第一步成功输出保留（可跳转）
    expect(
      within(steps[0]!).getByTestId("pipeline-run-step-output").getAttribute(
        "data-dataset-id",
      ),
    ).toBe("pl-1-v1-s1");
    // 历史如实记 failed
    expect(
      screen.getByTestId("pipeline-history-row").getAttribute("data-status"),
    ).toBe("failed");
  });

  it("配额硬停与引擎不支持如实态：quota_exhausted / 引擎暂不支持 link", async () => {
    // 配额硬停在第二步：状态章 quota_exhausted（rose 调、与失败区分）
    install({
      datasets: BASE_DATASETS,
      pipelines: [
        seedPipeline({ id: 1, failAt: { seq: 2, kind: "quota_exhausted" } }),
      ],
    });
    const { unmount } = render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-run-button"));
    const quotaStep = (await screen.findAllByTestId("pipeline-run-step"))[1]!;
    expect(quotaStep.getAttribute("data-status")).toBe("quota_exhausted");
    expect(
      within(quotaStep).getByTestId("pipeline-run-step-status").textContent,
    ).toBe("配额耗尽");
    // 色调与普通失败区分（rose vs red）
    expect(
      within(quotaStep).getByTestId("pipeline-run-step-status").className,
    ).toContain("rose");
    expect(
      within(quotaStep).getByTestId("pipeline-run-step-error").textContent,
    ).toContain("配额");
    unmount();

    // 旧引擎无 link 工具：运行失败于该步骤、错误明示引擎不支持
    install({
      datasets: BASE_DATASETS,
      pipelines: [
        seedPipeline({ id: 1, failAt: { seq: 3, kind: "unsupported" } }),
      ],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-run-button"));
    const steps = await screen.findAllByTestId("pipeline-run-step");
    const unsupported = steps[2]!;
    expect(unsupported.getAttribute("data-status")).toBe("failed");
    expect(
      within(unsupported).getByTestId("pipeline-run-step-error").textContent,
    ).toContain("引擎暂不支持 link");
    // 前两步输出保留
    expect(steps).toHaveLength(3);
    expect(steps[0]!.getAttribute("data-status")).toBe("success");
  });

  it("运行历史分页：12 条两页，下一页/上一页；点击历史行展开该次 run 步骤明细", async () => {
    const runs = Array.from({ length: 12 }, (_, i) => ({
      id: i + 1,
      status: (i % 3 === 0 ? "failed" : "success") as "failed" | "success",
      version: 1,
      trigger: (i % 4 === 0 ? "schedule" : "manual") as "schedule" | "manual",
      started_at: `2026-10-13T0${i % 10}:00:00`,
    }));
    install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1, runs })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");

    // 第 1 页：10 条（倒序 id 12..3）+ 分页信息
    await screen.findAllByTestId("pipeline-history-row");
    expect(screen.getAllByTestId("pipeline-history-row")).toHaveLength(10);
    expect(screen.getByTestId("pipeline-history-info").textContent).toContain("共 12 次");
    expect(screen.getByTestId("pipeline-history-info").textContent).toContain("1 / 2");
    expect(
      (screen.getByTestId("pipeline-history-prev") as HTMLButtonElement).disabled,
    ).toBe(true);

    fireEvent.click(screen.getByTestId("pipeline-history-next"));
    await waitFor(() =>
      expect(screen.getAllByTestId("pipeline-history-row")).toHaveLength(2),
    );
    expect(
      (screen.getByTestId("pipeline-history-next") as HTMLButtonElement).disabled,
    ).toBe(true);

    fireEvent.click(screen.getByTestId("pipeline-history-prev"));
    await waitFor(() =>
      expect(screen.getAllByTestId("pipeline-history-row")).toHaveLength(10),
    );

    // 点击某条历史：结果面板展开该 run 的步骤明细
    const target = screen.getAllByTestId("pipeline-history-row")[0]!;
    fireEvent.click(target);
    const result = await screen.findByTestId("pipeline-run-result");
    expect(result.getAttribute("data-run-id")).toBe(target.getAttribute("data-run-id"));
    expect(screen.getAllByTestId("pipeline-run-step")).toHaveLength(3);
  });

  it("viewer 运行被拒：403 detail 如实呈现，不产生 run 记录", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
      teamMembers: [{ logtoSub: "demo-customer", role: "viewer" }],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");
    fireEvent.click(screen.getByTestId("pipeline-run-button"));

    const error = await screen.findByTestId("pipeline-run-error");
    expect(error.textContent).toContain("viewer 只读");
    expect(backend.getPipelineRuns(1)).toHaveLength(0);
    expect(screen.queryByTestId("pipeline-run-result")).toBeNull();
  });
});

describe("管线工作台 · 调度（5.4）", () => {
  it("挂接 → 暂停 → 恢复 → 删除：状态如实流转；挂接后详情显示「输出已由管线调度管理」", async () => {
    const backend = install({
      datasets: BASE_DATASETS,
      pipelines: [seedPipeline({ id: 1 })],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");

    // 未挂接：无受管提示；保存按钮文案「挂接调度」（等调度态加载完成）
    expect(screen.queryByTestId("pipeline-schedule-managed-note")).toBeNull();
    expect(
      (await screen.findByTestId("pipeline-schedule-save")).textContent,
    ).toBe("挂接调度");
    // UTC 五字段注记在场
    expect(screen.getByTestId("pipeline-schedule-utc-note").textContent).toContain(
      "五字段",
    );

    // 挂接：PUT {cron}；状态章启用；受管提示出现
    fireEvent.change(screen.getByTestId("pipeline-cron-input"), {
      target: { value: "0 6 * * *" },
    });
    fireEvent.click(screen.getByTestId("pipeline-schedule-save"));
    const state = await screen.findByTestId("pipeline-schedule-state");
    expect(state.getAttribute("data-cron")).toBe("0 6 * * *");
    expect(state.getAttribute("data-enabled")).toBe("true");
    const puts = callsOf(backend, { method: "PUT", path: "/api/v1/pipelines/1/schedule" });
    expect(puts).toHaveLength(1);
    expect(puts[0]!.body).toEqual({ cron: "0 6 * * *" });
    expect(
      (await screen.findByTestId("pipeline-schedule-managed-note")).textContent,
    ).toContain("输出已由管线调度管理");

    // 暂停：enabled=false，恢复按钮出现
    fireEvent.click(screen.getByTestId("pipeline-schedule-pause"));
    await waitFor(() =>
      expect(
        screen.getByTestId("pipeline-schedule-state").getAttribute("data-enabled"),
      ).toBe("false"),
    );
    expect(screen.getByTestId("pipeline-schedule-resume")).toBeTruthy();
    expect(callsOf(backend, { path: "/api/v1/pipelines/1/schedule/pause" })).toHaveLength(1);

    // 恢复：enabled=true
    fireEvent.click(screen.getByTestId("pipeline-schedule-resume"));
    await waitFor(() =>
      expect(
        screen.getByTestId("pipeline-schedule-state").getAttribute("data-enabled"),
      ).toBe("true"),
    );

    // 删除：回到未挂接（登记面复现、受管提示消失）
    fireEvent.click(screen.getByTestId("pipeline-schedule-delete"));
    await waitFor(() =>
      expect(screen.queryByTestId("pipeline-schedule-state")).toBeNull(),
    );
    expect(screen.queryByTestId("pipeline-schedule-managed-note")).toBeNull();
    expect(screen.getByTestId("pipeline-schedule-save").textContent).toBe("挂接调度");
    expect(backend.getPipelines()[0]!.schedule).toBeNull();
  });

  it("非法 cron（非五字段）422 如实回显；已挂接种子呈暂停态与最近调度运行", async () => {
    install({
      datasets: BASE_DATASETS,
      pipelines: [
        seedPipeline({
          id: 1,
          schedule: {
            cron: "0 9 * * 1",
            enabled: false,
            last_run_at: "2026-10-13T09:00:00",
            last_status: "failed",
          },
        }),
      ],
    });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-row"));
    await screen.findByTestId("pipeline-detail");

    // 种子暂停态如实（恢复按钮在场、最近调度运行与状态）
    const state = await screen.findByTestId("pipeline-schedule-state");
    expect(state.getAttribute("data-enabled")).toBe("false");
    expect(screen.getByTestId("pipeline-schedule-resume").textContent).toBe("恢复");
    expect(screen.getByTestId("pipeline-schedule-last-run").textContent).toContain(
      "最近调度运行",
    );
    expect(screen.getByTestId("pipeline-schedule-last-status").textContent).toContain(
      "failed",
    );
    // 已挂接 → 受管提示
    expect(screen.getByTestId("pipeline-schedule-managed-note").textContent).toContain(
      "输出已由管线调度管理",
    );

    // 非五字段 cron：服务端 422 detail 原样回显
    fireEvent.change(screen.getByTestId("pipeline-cron-input"), {
      target: { value: "0 6 *" },
    });
    fireEvent.click(screen.getByTestId("pipeline-schedule-save"));
    const error = await screen.findByTestId("pipeline-schedule-error");
    expect(error.textContent).toContain("五字段");
  });
});

// ── 确定性算子 + SQL 步骤（wire-transform-operators 任务 3.1/3.2）────
// 四个算子的参数构建器与 SQL 编辑器（占位声明 + 参数声明 + 试运行预览）。
describe("管线工作台 · 算子步骤与 SQL 编辑器（3.1/3.2）", () => {
  it("步骤类型下拉含九种 op（含 window，wire-window-operator）", async () => {
    install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const select = screen.getByTestId("pipeline-step-op");
    const values = Array.from(
      (select as HTMLSelectElement).querySelectorAll("option"),
    ).map((o) => (o as HTMLOptionElement).value);
    expect(values).toEqual([
      "derive",
      "join",
      "link",
      "filter",
      "project",
      "aggregate",
      "union",
      "window",
      "sql",
    ]);
  });

  it("project 构建器：源列 + 新列名 → 载荷 columns[{source, as}]", async () => {
    const backend = install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.change(screen.getByTestId("pipeline-editor-name"), {
      target: { value: "投影链" },
    });
    fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps = screen.getAllByTestId("pipeline-step");
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-name"), {
      target: { value: "投影" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-op"), {
      target: { value: "project" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-project-source-0"), {
      target: { value: "city" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-project-as-0"), {
      target: { value: "城市" },
    });
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    const created = callsOf(backend, { method: "POST", path: "/api/v1/pipelines" });
    expect(created[0]!.body).toEqual({
      name: "投影链",
      description: undefined,
      base_dataset_id: "ds-1",
      steps: [
        {
          name: "投影",
          op: "project",
          params: { columns: [{ source: "city", as: "城市" }] },
        },
      ],
    });
  });

  it("aggregate 构建器：分组列 + 度量行 → 载荷 group_by/measures", async () => {
    const backend = install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.change(screen.getByTestId("pipeline-editor-name"), {
      target: { value: "汇总链" },
    });
    fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps = screen.getAllByTestId("pipeline-step");
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-name"), {
      target: { value: "汇总" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-op"), {
      target: { value: "aggregate" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-group-by"), {
      target: { value: "city, quarter" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-measure-func-0"), {
      target: { value: "sum" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-measure-column-0"), {
      target: { value: "amount" },
    });
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    const created = callsOf(backend, { method: "POST", path: "/api/v1/pipelines" });
    expect(created[0]!.body).toEqual({
      name: "汇总链",
      description: undefined,
      base_dataset_id: "ds-1",
      steps: [
        {
          name: "汇总",
          op: "aggregate",
          params: {
            group_by: ["city", "quarter"],
            measures: [{ func: "sum", column: "amount" }],
          },
        },
      ],
    });
  });

  it("union 构建器：右表选择 → 载荷 right_dataset_id + on=by_name", async () => {
    const backend = install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.change(screen.getByTestId("pipeline-editor-name"), {
      target: { value: "合并链" },
    });
    fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps = screen.getAllByTestId("pipeline-step");
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-name"), {
      target: { value: "合并" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-op"), {
      target: { value: "union" },
    });
    fireEvent.change(within(steps[0]!).getByTestId("pipeline-step-union-right"), {
      target: { value: "ds-2" },
    });
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    const created = callsOf(backend, { method: "POST", path: "/api/v1/pipelines" });
    expect(created[0]!.body).toEqual({
      name: "合并链",
      description: undefined,
      base_dataset_id: "ds-1",
      steps: [
        {
          name: "合并",
          op: "union",
          params: { right_dataset_id: "ds-2", on: "by_name" },
        },
      ],
    });
  });

  it("SQL 编辑器：占位声明 + 参数声明 → 载荷 sql/tables/params；试运行预览不落库", async () => {
    const backend = install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.change(screen.getByTestId("pipeline-editor-name"), {
      target: { value: "SQL 链" },
    });
    fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps = screen.getAllByTestId("pipeline-step");
    const step = within(steps[0]!);
    fireEvent.change(step.getByTestId("pipeline-step-name"), {
      target: { value: "SQL 汇总" },
    });
    fireEvent.change(step.getByTestId("pipeline-step-op"), {
      target: { value: "sql" },
    });
    fireEvent.change(step.getByTestId("pipeline-step-sql-text"), {
      target: {
        value: "SELECT city, count(*) AS n FROM {orders} WHERE city = :city GROUP BY city",
      },
    });
    fireEvent.change(step.getByTestId("pipeline-step-sql-table-name-0"), {
      target: { value: "orders" },
    });
    fireEvent.change(step.getByTestId("pipeline-step-sql-table-dataset-0"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(step.getByTestId("pipeline-step-sql-param-add"));
    const step2 = within(screen.getAllByTestId("pipeline-step")[0]!);
    fireEvent.change(step2.getByTestId("pipeline-step-sql-param-name-0"), {
      target: { value: "city" },
    });
    fireEvent.change(step2.getByTestId("pipeline-step-sql-param-value-0"), {
      target: { value: "上海" },
    });

    // 试运行：走预览端点，标注「不落库」
    fireEvent.click(step2.getByTestId("pipeline-step-sql-preview"));
    const preview = await screen.findByTestId("pipeline-step-sql-preview-result");
    expect(preview.textContent).toContain("region");
    expect(preview.textContent).toContain("华东");
    expect(step2.getByTestId("pipeline-step-sql").textContent).toContain("不落库");
    // 预览端点被调用，且不是建集请求
    const previews = callsOf(backend, {
      method: "POST",
      path: "/api/v1/datasets/ds-1/sql/preview",
    });
    expect(previews).toHaveLength(1);
    expect(previews[0]!.body).toEqual({
      sql: "SELECT city, count(*) AS n FROM {orders} WHERE city = :city GROUP BY city",
      tables: [{ name: "orders", dataset_key: "ds-1" }],
      params: { city: "上海" },
    });

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");
    const created = callsOf(backend, { method: "POST", path: "/api/v1/pipelines" });
    expect(created[0]!.body).toEqual({
      name: "SQL 链",
      description: undefined,
      base_dataset_id: "ds-1",
      steps: [
        {
          name: "SQL 汇总",
          op: "sql",
          params: {
            sql: "SELECT city, count(*) AS n FROM {orders} WHERE city = :city GROUP BY city",
            tables: [{ name: "orders", dataset_key: "ds-1" }],
            params: { city: "上海" },
          },
        },
      ],
    });
  });

  it("SQL 试运行失败：服务端 422 文案如实回显（不吞错）", async () => {
    install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps = screen.getAllByTestId("pipeline-step");
    const step = within(steps[0]!);
    fireEvent.change(step.getByTestId("pipeline-step-op"), {
      target: { value: "sql" },
    });
    // 声明了 orders，但 SQL 里引用了另一个未声明的占位 → 服务端 422
    fireEvent.change(step.getByTestId("pipeline-step-sql-text"), {
      target: { value: "SELECT * FROM {undeclared}" },
    });
    fireEvent.change(step.getByTestId("pipeline-step-sql-table-name-0"), {
      target: { value: "orders" },
    });
    fireEvent.change(step.getByTestId("pipeline-step-sql-table-dataset-0"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(step.getByTestId("pipeline-step-sql-preview"));
    const error = await screen.findByTestId("pipeline-step-sql-preview-error");
    expect(error.textContent).toContain("未声明");
  });

  it("SQL 试运行前置校验：SQL 与占位声明都没填 → 本地提示，不发请求", async () => {
    const backend = install({ datasets: BASE_DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
      target: { value: "ds-1" },
    });
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const steps = screen.getAllByTestId("pipeline-step");
    const step = within(steps[0]!);
    fireEvent.change(step.getByTestId("pipeline-step-op"), {
      target: { value: "sql" },
    });
    fireEvent.click(step.getByTestId("pipeline-step-sql-preview"));
    const error = await screen.findByTestId("pipeline-step-sql-preview-error");
    expect(error.textContent).toContain("占位声明");
    expect(
      callsOf(backend, {
        method: "POST",
        path: "/api/v1/datasets/ds-1/sql/preview",
      }),
    ).toHaveLength(0);
  });
});
