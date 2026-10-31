// 管线编辑器 · window 步骤构建器测试（wire-window-operator D5 web 面）：
// - 构建器渲染：分区列多选（基础集列快照）、排序行、函数行、试运行按钮；
// - 必填校验：排序类函数缺 order_by、`as` 缺失（行内回显且不发请求）；
// - 保存载荷形状：{partition_by, order_by, window:[{func, column?, offset?, as}]}
//   （排序类不提交 column；lag/lead 的 offset 转数字；count 可省 column）；
// - 试运行：复用既有 SQL 预览机制（零副作用），断言生成的只读 SELECT 与
//   占位声明，以及预览表渲染；
// - i18n zh/en 同批键位存在。
// fetch 一律走 testing.ts 内存假后端（零网络）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import zh from "../../i18n/zh";
import en from "../../i18n/en";
import { PipelineView } from "./PipelineView";
import {
  installConsoleBackend,
  seedConsoleSession,
  type FetchCall,
} from "../testing";
import type { Dataset } from "../api";

const NOW = "2026-10-31T10:00:00";

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
      { name: "amount", type: "REAL" },
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

const DATASETS = [
  makeDataset({ id: "ds-1", name: "订单明细" }),
  makeDataset({ id: "ds-2", name: "客户表" }),
];

function install(options?: Parameters<typeof installConsoleBackend>[1]) {
  const backend = installConsoleBackend([], options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

function callsOf(
  backend: ReturnType<typeof install>,
  filter: { method?: string; path: string },
): FetchCall[] {
  return backend.calls.filter(
    (call) =>
      call.url.pathname === filter.path &&
      (filter.method == null || call.method === filter.method),
  );
}

/** 打开新建管线编辑器并选中 window 步骤（返回该步骤容器元素）。 */
async function openWindowStep(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByTestId("pipeline-create"));
  await screen.findByTestId("pipeline-editor");
  fireEvent.change(screen.getByTestId("pipeline-editor-name"), {
    target: { value: "窗口加工链" },
  });
  fireEvent.change(screen.getByTestId("pipeline-editor-base"), {
    target: { value: "ds-1" },
  });
  fireEvent.click(screen.getByTestId("pipeline-add-step"));
  const step = screen.getByTestId("pipeline-step");
  fireEvent.change(within(step).getByTestId("pipeline-step-name"), {
    target: { value: "窗口排名" },
  });
  fireEvent.change(within(step).getByTestId("pipeline-step-op"), {
    target: { value: "window" },
  });
  return screen.getByTestId("pipeline-step");
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

describe("window 步骤构建器渲染", () => {
  it("分区列多选（基础集列快照）+ 排序行 + 函数行 + 试运行按钮", async () => {
    install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    expect(within(step).getByTestId("pipeline-step-window")).toBeTruthy();
    // 基础集 ds-1 的列快照 → 三个分区勾选项。
    for (const column of ["id", "city", "amount"]) {
      expect(
        within(step).getByTestId(`pipeline-step-window-partition-${column}`),
      ).toBeTruthy();
    }
    expect(
      within(step).getByTestId("pipeline-step-window-order-column-0"),
    ).toBeTruthy();
    expect(
      within(step).getByTestId("pipeline-step-window-order-dir-0"),
    ).toBeTruthy();
    expect(
      within(step).getByTestId("pipeline-step-window-func-0"),
    ).toBeTruthy();
    expect(
      within(step).getByTestId("pipeline-step-window-as-0"),
    ).toBeTruthy();
    // 默认函数 row_number 为排序类：不渲染函数列输入。
    expect(
      within(step).queryByTestId("pipeline-step-window-column-0"),
    ).toBeNull();
    expect(within(step).getByTestId("pipeline-step-window-preview")).toBeTruthy();
  });

  it("切到 lag：出现函数列与偏移输入；切到 count：只出现函数列", async () => {
    install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-func-0"),
      { target: { value: "lag" } },
    );
    expect(
      within(step).getByTestId("pipeline-step-window-column-0"),
    ).toBeTruthy();
    expect(
      within(step).getByTestId("pipeline-step-window-offset-0"),
    ).toBeTruthy();

    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-func-0"),
      { target: { value: "count" } },
    );
    expect(
      within(step).getByTestId("pipeline-step-window-column-0"),
    ).toBeTruthy();
    expect(
      within(step).queryByTestId("pipeline-step-window-offset-0"),
    ).toBeNull();
  });
});

describe("window 步骤校验", () => {
  it("排序类函数缺 order_by：行内回显且不发保存请求", async () => {
    const backend = install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-as-0"),
      { target: { value: "rn" } },
    );
    // order_by 行留空 → 排序类函数缺排序键。
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));

    const error = await screen.findByTestId("pipeline-step-json-error");
    expect(error.textContent).toContain("排序类");
    expect(
      callsOf(backend, { method: "POST", path: "/api/v1/pipelines" }),
    ).toHaveLength(0);
  });

  it("as 缺失：行内回显必填提示且不发保存请求", async () => {
    const backend = install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-order-column-0"),
      { target: { value: "amount" } },
    );
    // as 留空。
    fireEvent.click(screen.getByTestId("pipeline-editor-save"));

    const error = await screen.findByTestId("pipeline-step-json-error");
    expect(error.textContent).toContain("输出列名");
    expect(
      callsOf(backend, { method: "POST", path: "/api/v1/pipelines" }),
    ).toHaveLength(0);
  });

  it("as 重复：行内回显且不发请求", async () => {
    const backend = install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-order-column-0"),
      { target: { value: "amount" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-as-0"),
      { target: { value: "rn" } },
    );
    fireEvent.click(within(step).getByTestId("pipeline-step-window-func-add"));
    const rows = within(step).getAllByTestId(/pipeline-step-window-as-/);
    fireEvent.change(rows[1]!, { target: { value: "rn" } });

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    const error = await screen.findByTestId("pipeline-step-json-error");
    expect(error.textContent).toContain("不能重复");
    expect(
      callsOf(backend, { method: "POST", path: "/api/v1/pipelines" }),
    ).toHaveLength(0);
  });
});

describe("window 步骤保存载荷", () => {
  it("分区/排序/函数行 → {partition_by, order_by, window}（排序类不提交 column）", async () => {
    const backend = install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    // 分区：city。
    fireEvent.click(
      within(step).getByTestId("pipeline-step-window-partition-city"),
    );
    // 排序：amount 降序。
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-order-column-0"),
      { target: { value: "amount" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-order-dir-0"),
      { target: { value: "desc" } },
    );
    // 函数 1：row_number as rn（排序类不提交 column）。
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-as-0"),
      { target: { value: "rn" } },
    );
    // 函数 2：lag(amount, 2) as prev。
    fireEvent.click(within(step).getByTestId("pipeline-step-window-func-add"));
    const funcSelects = within(step).getAllByTestId(
      /pipeline-step-window-func-\d+/,
    );
    fireEvent.change(funcSelects[1]!, { target: { value: "lag" } });
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-column-1"),
      { target: { value: "amount" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-offset-1"),
      { target: { value: "2" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-as-1"),
      { target: { value: "prev" } },
    );
    // 函数 3：count（无 column）as cnt。
    fireEvent.click(within(step).getByTestId("pipeline-step-window-func-add"));
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-as-2"),
      { target: { value: "cnt" } },
    );

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    await screen.findByTestId("pipeline-detail");

    const created = callsOf(backend, {
      method: "POST",
      path: "/api/v1/pipelines",
    });
    expect(created).toHaveLength(1);
    expect(created[0]!.body).toEqual({
      name: "窗口加工链",
      description: undefined,
      base_dataset_id: "ds-1",
      steps: [
        {
          name: "窗口排名",
          op: "window",
          params: {
            partition_by: ["city"],
            order_by: [{ column: "amount", dir: "desc" }],
            window: [
              { func: "row_number", as: "rn" },
              { func: "lag", column: "amount", offset: 2, as: "prev" },
              { func: "count", as: "cnt" },
            ],
          },
        },
      ],
    });
  });

  it("lag/lead 偏移越界：行内回显（1..10000）", async () => {
    const backend = install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-order-column-0"),
      { target: { value: "amount" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-func-0"),
      { target: { value: "lead" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-column-0"),
      { target: { value: "amount" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-offset-0"),
      { target: { value: "0" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-as-0"),
      { target: { value: "nxt" } },
    );

    fireEvent.click(screen.getByTestId("pipeline-editor-save"));
    const error = await screen.findByTestId("pipeline-step-json-error");
    expect(error.textContent).toContain("10000");
    expect(
      callsOf(backend, { method: "POST", path: "/api/v1/pipelines" }),
    ).toHaveLength(0);
  });
});

describe("window 步骤试运行（复用既有预览机制）", () => {
  it("生成只读 SELECT + 占位声明，渲染预览表（零副作用）", async () => {
    const backend = install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    const step = await openWindowStep();

    fireEvent.click(
      within(step).getByTestId("pipeline-step-window-partition-city"),
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-order-column-0"),
      { target: { value: "amount" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-order-dir-0"),
      { target: { value: "desc" } },
    );
    fireEvent.change(
      within(step).getByTestId("pipeline-step-window-as-0"),
      { target: { value: "rn" } },
    );

    fireEvent.click(within(step).getByTestId("pipeline-step-window-preview"));

    const preview = await screen.findByTestId(
      "pipeline-step-window-preview-result",
    );
    expect(
      within(preview).getByTestId("pipeline-step-window-preview-rows"),
    ).toBeTruthy();

    const calls = callsOf(backend, {
      method: "POST",
      path: "/api/v1/datasets/ds-1/sql/preview",
    });
    expect(calls).toHaveLength(1);
    const body = calls[0]!.body as {
      sql?: string;
      tables?: Array<{ name: string; dataset_key: string }>;
    };
    expect(body.tables).toEqual([{ name: "src", dataset_key: "ds_1" }]);
    expect(body.sql).toContain('SELECT *, ROW_NUMBER() OVER (PARTITION BY "city"');
    expect(body.sql).toContain('ORDER BY "amount" DESC) AS "rn"');
    expect(body.sql).toContain("FROM {src}");
    // 试运行零副作用：不创建任何管线/数据集。
    expect(
      callsOf(backend, { method: "POST", path: "/api/v1/pipelines" }),
    ).toHaveLength(0);
  });

  it("缺基础数据集：试运行提示先选择基础数据集", async () => {
    install({ datasets: DATASETS });
    render(<PipelineView onOpenDataset={() => {}} />);
    // 不选基础数据集，直接建 window 步骤。
    fireEvent.click(await screen.findByTestId("pipeline-create"));
    await screen.findByTestId("pipeline-editor");
    fireEvent.click(screen.getByTestId("pipeline-add-step"));
    const step = screen.getByTestId("pipeline-step");
    fireEvent.change(within(step).getByTestId("pipeline-step-op"), {
      target: { value: "window" },
    });
    fireEvent.click(
      within(step).getByTestId("pipeline-step-window-preview"),
    );
    const error = await screen.findByTestId(
      "pipeline-step-window-preview-error",
    );
    expect(error.textContent).toContain("基础数据集");
  });
});

describe("window i18n 键位（zh/en 同批）", () => {
  const keys = [
    "console.datasets.pipeline.workbench.opLabels.window",
    "console.datasets.pipeline.workbench.windowHint",
    "console.datasets.pipeline.workbench.windowPartitionLabel",
    "console.datasets.pipeline.workbench.windowPartitionEmpty",
    "console.datasets.pipeline.workbench.windowOrderByLabel",
    "console.datasets.pipeline.workbench.windowOrderAdd",
    "console.datasets.pipeline.workbench.windowDirAsc",
    "console.datasets.pipeline.workbench.windowDirDesc",
    "console.datasets.pipeline.workbench.windowColumnPlaceholder",
    "console.datasets.pipeline.workbench.windowFuncLabel",
    "console.datasets.pipeline.workbench.windowFuncAdd",
    "console.datasets.pipeline.workbench.windowFuncColumnPlaceholder",
    "console.datasets.pipeline.workbench.windowOffsetPlaceholder",
    "console.datasets.pipeline.workbench.windowAsPlaceholder",
    "console.datasets.pipeline.workbench.windowPreview",
    "console.datasets.pipeline.workbench.windowPreviewing",
    "console.datasets.pipeline.workbench.windowPreviewNote",
    "console.datasets.pipeline.workbench.windowPreviewFailed",
    "console.datasets.pipeline.workbench.windowPreviewNeedsInput",
    "console.datasets.pipeline.workbench.windowPreviewColumns",
    "console.datasets.pipeline.workbench.windowPreviewRows",
    "console.datasets.pipeline.workbench.windowPreviewTruncated",
    "console.datasets.pipeline.workbench.windowErrors.window",
    "console.datasets.pipeline.workbench.windowErrors.window_order_by",
    "console.datasets.pipeline.workbench.windowErrors.window_as",
    "console.datasets.pipeline.workbench.windowErrors.window_as_duplicate",
    "console.datasets.pipeline.workbench.windowErrors.window_column",
    "console.datasets.pipeline.workbench.windowErrors.window_offset",
  ];

  function read(locale: unknown, path: string): unknown {
    return path
      .split(".")
      .reduce<unknown>(
        (node, part) =>
          node && typeof node === "object"
            ? (node as Record<string, unknown>)[part]
            : undefined,
        locale,
      );
  }

  it("两语言均含全部窗口步骤键位且为非空字符串", () => {
    for (const key of keys) {
      expect(typeof read(zh, key), `zh 缺 ${key}`).toBe("string");
      expect(typeof read(en, key), `en 缺 ${key}`).toBe("string");
    }
  });
});
