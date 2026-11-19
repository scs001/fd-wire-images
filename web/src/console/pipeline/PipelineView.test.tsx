// 管线子视图组件测试（wire-transform-pipeline-view 任务 2.1/2.3；
// wire-transform-pipelines 5.1 起血缘图拆为「图谱」页签——图谱行为直测
// PipelineGraphView，双页签容器另测）：
// 深度分列布局（列内按 name 排序）、节点卡信息与徽标（tier/调度/kind）、
// SVG 父→子连线、空态引导（源照常列出）、truncated/anomalies 如实提示、
// 节点点击跳数据集详情（mock 回调）、窄屏（<768px）退化分组列表、
// 加载失败错误态与重试。fetch 走 testing.ts 内存假后端（零网络）；
// matchMedia 按窄/宽两档 stub（断点 767px）。
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
import { PipelineGraphView } from "./PipelineGraphView";
import { PipelineView } from "./PipelineView";
import { installConsoleBackend, seedConsoleSession } from "../testing";
import type { PipelineGraph, PipelineGraphNode } from "../api";

const NOW = "2026-10-05T10:00:00";

/** 按 data-dataset-id 取节点卡（testid 统一为 pipeline-node）。 */
async function findNode(datasetId: string): Promise<HTMLElement> {
  const node = (await screen.findAllByTestId("pipeline-node")).find(
    (el) => el.getAttribute("data-dataset-id") === datasetId,
  );
  expect(node).toBeTruthy();
  return node!;
}

function makeNode(
  partial: Partial<PipelineGraphNode> & Pick<PipelineGraphNode, "dataset_id" | "name">,
): PipelineGraphNode {
  return {
    key: partial.dataset_id.replace(/-/g, "_"),
    source_kind: "upload",
    storage_tier: null,
    row_count: 100,
    updated_at: NOW,
    depth: 0,
    schedule_cron: null,
    ...partial,
  };
}

/** 三级派生链 + 同深度列内排序夹具（A/B 同为 depth 0，按 name 排序）。 */
const CHAIN: PipelineGraph = {
  nodes: [
    makeNode({ dataset_id: "ds-1", name: "A 上传源", storage_tier: "api", row_count: 120 }),
    makeNode({
      dataset_id: "ds-2",
      name: "派生集",
      source_kind: "derived",
      storage_tier: "cell",
      row_count: 80,
      depth: 1,
      schedule_cron: "0 * * * *",
      updated_at: "2026-10-06T10:00:00",
    }),
    makeNode({
      dataset_id: "ds-3",
      name: "联合集",
      source_kind: "derived",
      storage_tier: "cell",
      row_count: 40,
      depth: 2,
      updated_at: "2026-10-07T10:00:00",
    }),
    makeNode({ dataset_id: "ds-4", name: "B 上传源", row_count: 10 }),
  ],
  edges: [
    { from_dataset_id: "ds-1", to_dataset_id: "ds-2", op: "derived" },
    { from_dataset_id: "ds-2", to_dataset_id: "ds-3", op: "derived" },
  ],
  anomalies: 0,
  truncated: false,
};

/** stub window.matchMedia（窄/宽两档）；afterEach vi.unstubAllGlobals 复原。 */
function stubMatchMedia(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList => ({
      matches: matches && query === "(max-width: 767px)",
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  );
}

function install(options?: Parameters<typeof installConsoleBackend>[1]) {
  const backend = installConsoleBackend([], options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

function graphCalls(backend: ReturnType<typeof installConsoleBackend>) {
  return backend.calls.filter((c) => c.url.pathname === "/api/v1/pipeline/graph");
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  seedConsoleSession({ sub: "demo-customer" });
  stubMatchMedia(false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("PipelineView（分层布局与节点卡，任务 2.1）", () => {
  it("按深度分列：depth 0 最左、派生逐级向右；列内按 name 排序", async () => {
    install({ pipelineGraph: CHAIN });
    render(<PipelineGraphView onOpenDataset={() => {}} />);

    const columns = await screen.findAllByTestId("pipeline-column");
    expect(columns.map((el) => el.getAttribute("data-depth"))).toEqual([
      "0",
      "1",
      "2",
    ]);
    // 深度 0 列内 A 上传源 排在 B 上传源 前（name 排序）
    expect(
      within(columns[0]!)
        .getAllByTestId("pipeline-node-name")
        .map((el) => el.textContent),
    ).toEqual(["A 上传源", "B 上传源"]);
    expect(
      within(columns[1]!).getByTestId("pipeline-node-name").textContent,
    ).toBe("派生集");
    // 列头标注深度
    expect(
      columns.map((el) => within(el).getByTestId("pipeline-column-label").textContent),
    ).toEqual(["深度 0", "深度 1", "深度 2"]);
  });

  it("节点卡信息与徽标：名称/行数/更新时间、kind 标注、tier 徽标、调度徽标", async () => {
    install({ pipelineGraph: CHAIN });
    render(<PipelineGraphView onOpenDataset={() => {}} />);

    const card = await findNode("ds-2");
    const node = within(card);
    expect(node.getByTestId("pipeline-node-name").textContent).toBe("派生集");
    expect(node.getByTestId("pipeline-node-rows").textContent).toBe("80 行");
    expect(node.getByTestId("pipeline-node-updated").textContent).toContain(
      "更新",
    );
    // kind 标注：derived →「派生」；tier：cell →「cell」；调度 cron 非空 →「定时」
    expect(node.getByTestId("pipeline-node-kind").getAttribute("data-kind")).toBe(
      "derived",
    );
    expect(node.getByTestId("pipeline-node-kind").textContent).toBe("派生");
    expect(node.getByTestId("pipeline-node-tier").getAttribute("data-tier")).toBe(
      "cell",
    );
    expect(node.getByTestId("pipeline-node-tier").textContent).toBe("隔离单元");
    expect(node.getByTestId("pipeline-node-scheduled").textContent).toBe("定时");
    expect(
      node.getByTestId("pipeline-node-scheduled").getAttribute("title"),
    ).toBe("0 * * * *");

    // 上传源：upload →「上传」；api tier →「免费档」；无调度徽标
    const source = within(await findNode("ds-1"));
    expect(source.getByTestId("pipeline-node-kind").textContent).toBe("上传");
    expect(source.getByTestId("pipeline-node-tier").textContent).toBe("免费档");
    expect(source.queryByTestId("pipeline-node-scheduled")).toBeNull();
    expect(source.getByTestId("pipeline-node-rows").textContent).toBe("120 行");
    // 未下发 storage_tier 的节点不渲染 tier 徽标（容错）
    expect(
      within(await findNode("ds-4")).queryByTestId("pipeline-node-tier"),
    ).toBeNull();
  });

  it("SVG 连线父→子：两条边、from/to 指向正确（Bearer 登录身份请求）", async () => {
    const backend = install({ pipelineGraph: CHAIN });
    render(<PipelineGraphView onOpenDataset={() => {}} />);

    const edges = await screen.findAllByTestId("pipeline-edge");
    expect(edges).toHaveLength(2);
    expect(
      edges.map((el) => [el.getAttribute("data-from"), el.getAttribute("data-to")]),
    ).toEqual([
      ["ds-1", "ds-2"],
      ["ds-2", "ds-3"],
    ]);
    expect(edges[0]!.getAttribute("data-op")).toBe("derived");
    // 请求走既有 api client：Bearer 登录头
    const call = graphCalls(backend)[0]!;
    expect(call.method).toBe("GET");
    expect(call.headers["authorization"]).toMatch(/^Bearer /);
  });

  it("空态诚实：无派生边显示引导文案，源数据集照常列出", async () => {
    install({
      pipelineGraph: {
        nodes: CHAIN.nodes.filter((n) => n.source_kind === "upload"),
        edges: [],
        anomalies: 0,
        truncated: false,
      },
    });
    render(<PipelineGraphView onOpenDataset={() => {}} />);

    const hint = await screen.findByTestId("pipeline-empty-hint");
    expect(hint.textContent).toContain("尚无派生数据集");
    expect(hint.textContent).toContain("派生或指标联合");
    // 源数据集照常列出（两列合并进 depth 0 一列）
    expect(screen.getAllByTestId("pipeline-node")).toHaveLength(2);
    expect(screen.queryByTestId("pipeline-edge")).toBeNull();
  });

  it("节点点击跳数据集详情（onOpenDataset 携带 dataset_id）", async () => {
    install({ pipelineGraph: CHAIN });
    const onOpen = vi.fn();
    render(<PipelineGraphView onOpenDataset={onOpen} />);

    fireEvent.click(await findNode("ds-2"));
    expect(onOpen).toHaveBeenCalledWith("ds-2");
    fireEvent.click(await findNode("ds-3"));
    expect(onOpen).toHaveBeenCalledWith("ds-3");
  });

  it("truncated / anomalies 如实提示", async () => {
    install({
      pipelineGraph: { ...CHAIN, truncated: true, anomalies: 2 },
    });
    render(<PipelineGraphView onOpenDataset={() => {}} />);

    expect(await screen.findByTestId("pipeline-truncated")).toBeTruthy();
    expect(screen.getByTestId("pipeline-truncated").textContent).toContain("500");
    expect(screen.getByTestId("pipeline-anomalies").textContent).toContain("2");
    expect(screen.getByTestId("pipeline-anomalies").textContent).toContain(
      "成环或父级缺失",
    );
  });

  it("加载失败：错误态如实 + 重试重发请求", async () => {
    const backend = install({
      failPipelineGraphStatus: 503,
      failPipelineGraphDetail: "管线图暂不可用（引擎元数据超时）",
    });
    render(<PipelineGraphView onOpenDataset={() => {}} />);

    const error = await screen.findByTestId("pipeline-error");
    expect(error.textContent).toContain("管线图加载失败");
    expect(error.textContent).toContain("管线图暂不可用");
    expect(screen.queryByTestId("pipeline-graph")).toBeNull();

    fireEvent.click(screen.getByTestId("pipeline-retry"));
    await waitFor(() => expect(graphCalls(backend)).toHaveLength(2));
  });

  it("加载中文案可见（请求未返回前）", () => {
    install({ pipelineGraph: CHAIN });
    render(<PipelineGraphView onOpenDataset={() => {}} />);
    expect(screen.getByTestId("pipeline-loading").textContent).toBe(
      "管线图加载中…",
    );
  });
});

describe("PipelineView 窄屏退化（<768px，任务 2.1/2.3）", () => {
  it("按深度分组的卡片列表：无 SVG 画布与连线，节点照常点击跳转", async () => {
    stubMatchMedia(true);
    install({ pipelineGraph: CHAIN });
    const onOpen = vi.fn();
    render(<PipelineGraphView onOpenDataset={onOpen} />);

    const list = await screen.findByTestId("pipeline-list");
    const groups = within(list).getAllByTestId("pipeline-depth-group");
    expect(groups.map((el) => el.getAttribute("data-depth"))).toEqual([
      "0",
      "1",
      "2",
    ]);
    // 组内按 name 排序、节点照常呈现（含徽标与点击跳转）
    expect(
      within(groups[0]!)
        .getAllByTestId("pipeline-node-name")
        .map((el) => el.textContent),
    ).toEqual(["A 上传源", "B 上传源"]);
    expect(
      within(groups[1]!).getByTestId("pipeline-node-tier").textContent,
    ).toBe("隔离单元");
    fireEvent.click(within(groups[1]!).getByTestId("pipeline-node"));
    expect(onOpen).toHaveBeenCalledWith("ds-2");
    // 无 SVG：不渲染画布/连线/列容器
    expect(screen.queryByTestId("pipeline-graph")).toBeNull();
    expect(screen.queryByTestId("pipeline-edges")).toBeNull();
    expect(screen.queryByTestId("pipeline-column")).toBeNull();
  });
});

// ── 双页签容器（wire-transform-pipelines 任务 5.1）──────────────────────────
// 「管线」工作台为默认页签，「图谱」只读视图保留为第二页签；图谱数据在
// 切到图谱页签时才拉取（工作台不预取）。
describe("PipelineView 双页签容器（wire-transform-pipelines 5.1）", () => {
  it("默认「管线」工作台；图谱未拉取；切「图谱」拉图并渲染，工作台让位", async () => {
    const backend = install({ pipelineGraph: CHAIN });
    render(<PipelineView onOpenDataset={() => {}} />);

    // 默认工作台页签：管线列表空态在场（无种子管线），图谱未请求
    expect(await screen.findByTestId("pipeline-workbench")).toBeTruthy();
    expect(screen.getByTestId("pipeline-tab-workbench").getAttribute("aria-selected")).toBe("true");
    expect(
      backend.calls.filter((c) => c.url.pathname === "/api/v1/pipeline/graph"),
    ).toEqual([]);
    expect(screen.queryByTestId("pipeline-graph-view")).toBeNull();

    // 切「图谱」：拉图（Bearer）并渲染列与节点；工作台让位
    fireEvent.click(screen.getByTestId("pipeline-tab-graph"));
    expect(await screen.findByTestId("pipeline-graph-view")).toBeTruthy();
    const graphCalls = backend.calls.filter(
      (c) => c.url.pathname === "/api/v1/pipeline/graph",
    );
    expect(graphCalls).toHaveLength(1);
    expect(graphCalls[0]!.headers["authorization"]).toMatch(/^Bearer /);
    expect(
      screen
        .getAllByTestId("pipeline-column")
        .map((el) => el.getAttribute("data-depth")),
    ).toEqual(["0", "1", "2"]);
    expect(screen.queryByTestId("pipeline-workbench")).toBeNull();

    // 切回「管线」：工作台复现（图谱请求不重复）
    fireEvent.click(screen.getByTestId("pipeline-tab-workbench"));
    expect(await screen.findByTestId("pipeline-workbench")).toBeTruthy();
    expect(screen.queryByTestId("pipeline-graph-view")).toBeNull();
    expect(
      backend.calls.filter((c) => c.url.pathname === "/api/v1/pipeline/graph"),
    ).toHaveLength(1);
  });
});
