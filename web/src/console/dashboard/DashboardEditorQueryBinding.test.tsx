// DashboardEditor「绑定查询」表单段组件测试（wire-panel-query-binding 任务
// 2.2）：绑定入口/回填、数据集选择（tier 徽标+行数）→ 列勾选/过滤/排序/
// limit 表单交互、试取成功写本地快照（预览即所得，不算保存）、试取 422/400
// 错误如实呈现、解绑清 query、保存 PUT 载荷带 query、kpi 约定提示、数据集
// 列表加载失败呈现。契约试取端点 POST /api/v1/artifacts/query-preview 由
// 本地 fetch 包装拦截 mock（契约：{snapshot,query} / 422 / 404 / 400）。
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
import { DashboardEditor } from "./DashboardEditor";
import {
  installConsoleBackend,
  seedConsoleSession,
  type FetchCall,
  type FakeResponse,
} from "../testing";
import type { Dataset } from "../api";
import type { DashboardSpecV1 } from "./spec";

const NOW = "2026-10-05T10:00:00";

function makeDataset(
  partial: Partial<Dataset> &
    Pick<Dataset, "id" | "name"> & { storage_tier?: string },
): Dataset {
  return {
    key: partial.id.replace(/-/g, "_"),
    source_kind: "upload",
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "date", type: "DATE" },
      { name: "city", type: "VARCHAR" },
      { name: "price", type: "DOUBLE" },
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

/** 试取端点应答器：入参 = 请求体（构建后的契约 query）。 */
type PreviewResponder = (body: unknown) => { status: number; body: unknown };

/**
 * 装假后端 + 种子 dashboard 产物；preview 给出时拦截
 * POST /api/v1/artifacts/query-preview（previewCalls 记录逐次请求体），
 * 其余请求透传假后端。failDatasetsList=true 时 GET /api/v1/datasets → 500。
 */
function install(options?: {
  preview?: PreviewResponder;
  failDatasetsList?: boolean;
  spec?: DashboardSpecV1;
  artifactId?: number;
}) {
  const backend = installConsoleBackend([], {
    datasets: [
      makeDataset({
        id: "ds_coal",
        name: "环渤海煤价",
        row_count: 3650,
        storage_tier: "cell",
      }),
      makeDataset({ id: "ds_macro", name: "宏观指标", row_count: 88 }), // 响应不带 tier
    ],
  });
  const spec = options?.spec ?? INITIAL;
  backend.addLifecycleArtifact({
    id: options?.artifactId ?? 21,
    kind: "dashboard",
    title: spec.title,
    spec: { title: spec.title, spec: JSON.parse(JSON.stringify(spec)) },
  });
  const impl = backend.fetchImpl;
  const previewCalls: Array<{ call: FetchCall; body: unknown }> = [];
  const wrapped = ((
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<FakeResponse> => {
    const url = new URL(String(input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    if (
      options?.failDatasetsList &&
      url.pathname === "/api/v1/datasets" &&
      method === "GET"
    ) {
      return Promise.resolve({
        ok: false,
        status: 500,
        json: () => Promise.resolve({ detail: "后端维护中" }),
      });
    }
    if (options?.preview && url.pathname === "/api/v1/artifacts/query-preview") {
      const body =
        typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
      const call: FetchCall = {
        method,
        url,
        headers: {},
        body,
      };
      previewCalls.push({ call, body });
      const res = options.preview(body);
      return Promise.resolve({
        ok: res.status < 400,
        status: res.status,
        json: () => Promise.resolve(res.body),
      });
    }
    return impl(input, init);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", wrapped);
  return { backend, previewCalls };
}

const INITIAL: DashboardSpecV1 = {
  title: "经营看板",
  panels: [
    {
      id: "t1",
      type: "table",
      title: "煤价明细",
      span: 2,
      snapshot: { columns: ["日期", "价格"], rows: [] },
    },
    {
      id: "k1",
      type: "kpi",
      title: "最新煤价",
      span: 1,
      snapshot: { columns: ["value"], rows: [[0]] },
      config: { unit: "元/吨", subtitle: "" },
    },
  ],
};

/** 已绑定契约看板：q1 带 query（回填用），n1 无 query 对照。 */
const WITH_QUERY: DashboardSpecV1 = {
  title: "契约看板",
  panels: [
    {
      id: "q1",
      type: "table",
      title: "煤价明细",
      span: 2,
      snapshot: { columns: ["日期", "价格"], rows: [["7月", 850]] },
      query: {
        dataset_id: "ds_coal",
        filters: [{ column: "city", op: "eq", value: "秦皇岛" }],
        sort: [{ column: "date", dir: "desc" }],
        limit: 1,
        columns: ["date", "price"],
      },
    },
    {
      id: "n1",
      type: "kpi",
      title: "无契约指标",
      span: 1,
      snapshot: { columns: ["value"], rows: [[1.2]] },
    },
  ],
};

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

function editorPanel(id: string): HTMLElement {
  return screen
    .getAllByTestId("editor-panel")
    .find((el) => el.getAttribute("data-panel-id") === id)!;
}

/** PUT 请求体里指定面板（信封口径内层）。 */
function savedPanel(
  backend: ReturnType<typeof installConsoleBackend>,
  id: string,
) {
  const put = backend.calls.find((c) => c.method === "PUT");
  const body = put?.body as { spec: { spec: { panels: DashboardSpecV1["panels"] } } };
  return body.spec.spec.panels.find((p) => p.id === id);
}

describe("DashboardEditor 绑定查询表单（wire-panel-query-binding 任务 2.1/2.2）", () => {
  it("未绑定：显示「未绑定」+「绑定」入口；绑定后展开表单并懒加载数据集列表", async () => {
    install();
    render(
      <DashboardEditor artifactId={21} initial={INITIAL} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const t1 = editorPanel("t1");
    expect(within(t1).getByTestId("editor-query-unbound").textContent).toBe("未绑定");
    // 未打开：无表单控件
    expect(within(t1).queryByTestId("editor-query-dataset")).toBeNull();

    fireEvent.click(within(t1).getByTestId("editor-query-bind"));
    // 数据集列表懒加载（打开表单段才拉取，未打开前零请求）
    const select = within(t1).getByTestId("editor-query-dataset");
    await waitFor(() => {
      expect(
        (select as HTMLSelectElement).textContent,
      ).toContain("环渤海煤价");
    });
    // 选项文案：名称 + tier 徽标 + 行数；不带 tier 的数据集只显行数
    expect((select as HTMLSelectElement).textContent).toContain("引擎物化");
    expect((select as HTMLSelectElement).textContent).toContain("3650 行");
    expect((select as HTMLSelectElement).textContent).toContain("宏观指标（88 行）");

    // kpi 面板：取值约定提示可见；table 面板无提示
    fireEvent.click(within(editorPanel("k1")).getByTestId("editor-query-bind"));
    expect(
      within(editorPanel("k1")).getByTestId("editor-query-kpi-hint").textContent,
    ).toContain("指标卡约定");
    expect(within(t1).queryByTestId("editor-query-kpi-hint")).toBeNull();
  });

  it("表单交互→保存：选集→勾列→过滤→排序→limit，PUT 载荷带完整 query（投影按数据集列序）", async () => {
    const { backend } = install();
    render(
      <DashboardEditor artifactId={21} initial={INITIAL} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const t1 = editorPanel("t1");
    fireEvent.click(within(t1).getByTestId("editor-query-bind"));
    const select = within(t1).getByTestId("editor-query-dataset");
    await waitFor(() => {
      expect((select as HTMLSelectElement).textContent).toContain("环渤海煤价");
    });

    // 未选数据集直接试取：本地校验提示（不发起请求）
    fireEvent.click(within(t1).getByTestId("editor-query-try"));
    expect(within(t1).getByTestId("editor-query-error").textContent).toContain(
      "请先选择数据集",
    );

    // 选数据集 → 列勾选框按数据集列序出现
    fireEvent.change(select, { target: { value: "ds_coal" } });
    const checkboxes = within(t1).getAllByTestId("editor-query-column");
    expect(checkboxes.map((el) => el.getAttribute("data-column"))).toEqual([
      "date",
      "city",
      "price",
    ]);

    // 勾两列（故意先勾 price 再勾 date——投影输出仍按数据集列序）
    fireEvent.click(
      checkboxes.find((el) => el.getAttribute("data-column") === "price")!,
    );
    fireEvent.click(
      checkboxes.find((el) => el.getAttribute("data-column") === "date")!,
    );

    // 加过滤行：列=city、算子=eq、值=秦皇岛
    fireEvent.click(within(t1).getByTestId("editor-query-filter-add"));
    fireEvent.change(within(t1).getByTestId("editor-query-filter-column"), {
      target: { value: "city" },
    });
    fireEvent.change(within(t1).getByTestId("editor-query-filter-op"), {
      target: { value: "eq" },
    });
    fireEvent.change(within(t1).getByTestId("editor-query-filter-value"), {
      target: { value: "秦皇岛" },
    });

    // 加排序：列=date（默认首列）、方向 desc
    fireEvent.click(within(t1).getByTestId("editor-query-sort-add"));
    fireEvent.change(within(t1).getByTestId("editor-query-sort-dir"), {
      target: { value: "desc" },
    });

    // limit=50
    fireEvent.change(within(t1).getByTestId("editor-query-limit"), {
      target: { value: "50" },
    });

    // 保存：PUT 信封里 t1.query 完整落盘
    fireEvent.click(screen.getByTestId("editor-save"));
    await screen.findByTestId("editor-saved-version");
    expect(savedPanel(backend, "t1")?.query).toEqual({
      dataset_id: "ds_coal",
      filters: [{ column: "city", op: "eq", value: "秦皇岛" }],
      sort: [{ column: "date", dir: "desc" }],
      limit: 50,
      columns: ["date", "price"],
    });
    // 未绑定面板不落 query 字段
    expect(savedPanel(backend, "k1")?.query).toBeUndefined();
  });

  it("试取成功：POST 体=面板 query；snapshot 写进本地编辑态面板（预览即所得，不算保存）", async () => {
    const SNAPSHOT = {
      columns: ["date", "price"],
      rows: [
        ["2026-09-30", 860],
        ["2026-09-29", 855],
      ],
    };
    const { backend, previewCalls } = install({
      spec: WITH_QUERY,
      preview: (body) => ({
        status: 200,
        // 服务端归一后 query（parse_panel_query 口径：全字段展开）
        body: { snapshot: SNAPSHOT, query: { ...(body as object) } },
      }),
    });
    render(
      <DashboardEditor artifactId={21} initial={WITH_QUERY} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const q1 = editorPanel("q1");
    // 已绑定面板初始即开表单（回填原值）
    await waitFor(() => {
      expect(
        (within(q1).getByTestId("editor-query-dataset") as HTMLSelectElement).value,
      ).toBe("ds_coal");
    });

    fireEvent.click(within(q1).getByTestId("editor-query-try"));
    // 成功提示：行数如实
    expect((await within(q1).findByTestId("editor-query-try-ok")).textContent).toContain(
      "已试取 2 行",
    );
    // POST 体 = 面板当前 query（与服务端入参同形）
    expect(previewCalls).toHaveLength(1);
    expect(previewCalls[0]!.body).toEqual(WITH_QUERY.panels[0]!.query);
    // 预览即所得：渲染器快照换成试取结果（2 行）
    await waitFor(() => {
      expect(
        screen.getByTestId("dashboard-table").querySelectorAll("tbody tr"),
      ).toHaveLength(2);
    });

    // 试取只进本地——未保存计数可见（q1 有差异），保存后 PUT 落盘
    fireEvent.click(screen.getByTestId("editor-save"));
    await screen.findByTestId("editor-saved-version");
    const saved = savedPanel(backend, "q1");
    expect(saved?.snapshot).toEqual(SNAPSHOT);
    expect(saved?.query).toEqual(WITH_QUERY.panels[0]!.query);
  });

  it("试取失败如实呈现：422（入参非法）与 400（列不存在）——detail 原文、快照不动", async () => {
    // 422：入参非法
    install({
      spec: WITH_QUERY,
      preview: () => ({
        status: 422,
        body: { detail: "query 无效：dataset_id 缺失" },
      }),
    });
    render(
      <DashboardEditor artifactId={21} initial={WITH_QUERY} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const q1 = editorPanel("q1");
    fireEvent.click(within(q1).getByTestId("editor-query-try"));
    expect(
      (await within(q1).findByTestId("editor-query-error")).textContent,
    ).toContain("query 无效：dataset_id 缺失");
    // 快照不动：预览仍是原快照（1 行）
    expect(
      screen.getByTestId("dashboard-table").querySelectorAll("tbody tr"),
    ).toHaveLength(1);
    cleanup();

    // 400：查询执行失败（列不存在）
    const failed = install({
      artifactId: 22,
      spec: WITH_QUERY,
      preview: () => ({ status: 400, body: { detail: "列不存在: ghost" } }),
    });
    render(
      <DashboardEditor artifactId={22} initial={WITH_QUERY} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const q1b = editorPanel("q1");
    fireEvent.click(within(q1b).getByTestId("editor-query-try"));
    expect(
      (await within(q1b).findByTestId("editor-query-error")).textContent,
    ).toContain("列不存在: ghost");
    expect(failed.previewCalls).toHaveLength(1);
  });

  it("解绑：清除面板 query（本地）、快照保留；保存 PUT 不带 query、段回「未绑定」", async () => {
    const { backend } = install({ spec: WITH_QUERY });
    render(
      <DashboardEditor artifactId={21} initial={WITH_QUERY} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const q1 = editorPanel("q1");
    await waitFor(() => {
      expect(within(q1).getByTestId("editor-query-form")).toBeTruthy();
    });
    fireEvent.click(within(q1).getByTestId("editor-query-unbind"));

    // 段回未绑定入口；快照保留（预览仍是原快照）
    expect(within(q1).getByTestId("editor-query-unbound").textContent).toBe("未绑定");
    expect(
      screen.getByTestId("dashboard-table").querySelectorAll("tbody tr"),
    ).toHaveLength(1);

    fireEvent.click(screen.getByTestId("editor-save"));
    await screen.findByTestId("editor-saved-version");
    const saved = savedPanel(backend, "q1");
    expect(saved?.query).toBeUndefined();
    expect(saved?.snapshot).toEqual(WITH_QUERY.panels[0]!.snapshot);
  });

  it("已有 query 回填：数据集选中、列勾选态、过滤行值、排序方向、limit 原样", async () => {
    install();
    render(
      <DashboardEditor artifactId={21} initial={WITH_QUERY} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const q1 = editorPanel("q1");
    await waitFor(() => {
      expect(
        (within(q1).getByTestId("editor-query-dataset") as HTMLSelectElement).value,
      ).toBe("ds_coal");
    });
    // 列勾选态：date/price 勾、city 不勾（按数据集列序渲染）
    const checked = within(q1)
      .getAllByTestId("editor-query-column")
      .filter((el) => (el as HTMLInputElement).checked)
      .map((el) => el.getAttribute("data-column"));
    expect(checked).toEqual(["date", "price"]);
    // 过滤行回填：列/算子/值
    expect(
      (within(q1).getByTestId("editor-query-filter-column") as HTMLSelectElement).value,
    ).toBe("city");
    expect(
      (within(q1).getByTestId("editor-query-filter-op") as HTMLSelectElement).value,
    ).toBe("eq");
    expect(
      (within(q1).getByTestId("editor-query-filter-value") as HTMLInputElement).value,
    ).toBe("秦皇岛");
    // 排序方向 + limit
    expect(
      (within(q1).getByTestId("editor-query-sort-dir") as HTMLSelectElement).value,
    ).toBe("desc");
    expect(
      (within(q1).getByTestId("editor-query-limit") as HTMLInputElement).value,
    ).toBe("1");
    // 徽标仍在（已绑定查询 + dataset_id）
    expect(within(q1).getByTestId("editor-panel-query").textContent).toContain(
      "ds_coal",
    );
  });

  it("已绑数据集不在列表（已删/他企）：占位选项保留选中值不漂移", async () => {
    const GONE: DashboardSpecV1 = {
      title: "失效绑定",
      panels: [
        {
          id: "g1",
          type: "table",
          title: "失效",
          span: 1,
          snapshot: { columns: ["x"], rows: [] },
          query: { dataset_id: "ds_gone", limit: 10 },
        },
      ],
    };
    install({ spec: GONE });
    render(
      <DashboardEditor artifactId={21} initial={GONE} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const g1 = editorPanel("g1");
    await waitFor(() => {
      const select = within(g1).getByTestId("editor-query-dataset") as HTMLSelectElement;
      expect(select.textContent).toContain("不在列表中");
      expect(select.value).toBe("ds_gone");
    });
  });

  it("数据集列表加载失败如实呈现（可重试）", async () => {
    install({ failDatasetsList: true });
    render(
      <DashboardEditor artifactId={21} initial={INITIAL} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const t1 = editorPanel("t1");
    fireEvent.click(within(t1).getByTestId("editor-query-bind"));
    expect(
      (await within(t1).findByTestId("editor-query-datasets-error")).textContent,
    ).toContain("后端维护中");
  });

  it("过滤行增删：移除行即从 query 消失（保存 PUT 断言）", async () => {
    const { backend } = install();
    render(
      <DashboardEditor artifactId={21} initial={INITIAL} onSaved={vi.fn()} onExit={vi.fn()} />,
    );
    const t1 = editorPanel("t1");
    fireEvent.click(within(t1).getByTestId("editor-query-bind"));
    const select = within(t1).getByTestId("editor-query-dataset");
    await waitFor(() => {
      expect((select as HTMLSelectElement).textContent).toContain("环渤海煤价");
    });
    fireEvent.change(select, { target: { value: "ds_coal" } });

    fireEvent.click(within(t1).getByTestId("editor-query-filter-add"));
    fireEvent.change(within(t1).getByTestId("editor-query-filter-value"), {
      target: { value: "850" },
    });
    // 再加一行后删掉第一行：只剩第二行（price gte 850——数值化）
    fireEvent.click(within(t1).getByTestId("editor-query-filter-add"));
    const rows = within(t1).getAllByTestId("editor-query-filter");
    expect(rows).toHaveLength(2);
    fireEvent.change(within(rows[1]!).getByTestId("editor-query-filter-column"), {
      target: { value: "price" },
    });
    fireEvent.change(within(rows[1]!).getByTestId("editor-query-filter-op"), {
      target: { value: "gte" },
    });
    fireEvent.change(within(rows[1]!).getByTestId("editor-query-filter-value"), {
      target: { value: "850" },
    });
    fireEvent.click(within(rows[0]!).getByTestId("editor-query-filter-remove"));
    expect(within(t1).getAllByTestId("editor-query-filter")).toHaveLength(1);

    fireEvent.click(screen.getByTestId("editor-save"));
    await screen.findByTestId("editor-saved-version");
    expect(savedPanel(backend, "t1")?.query).toEqual({
      dataset_id: "ds_coal",
      filters: [{ column: "price", op: "gte", value: 850 }],
    });
  });
});
