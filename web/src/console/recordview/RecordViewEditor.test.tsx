// 单记录页构建器组件测试（wire-record-view 任务 4.1/4.2）：
// 主数据集/键列选择 → headerFields 列勾选与 label 改写 → sections 增删配
// （数据集/日期列/上限/filters 字面与 {fromField} 引用双形态互切）→ 保存
// 走既有 PUT（信封口径断言）；草稿校验镜像零请求拦截；试预览——键值下拉
// （distinct 查询）+ 手输兜底 + POST preview 端点调用（console 身份）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { RecordViewEditor } from "./RecordViewEditor";
import type { RecordViewSpecBody } from "./recordViewSpec";
import { installConsoleBackend, seedConsoleSession } from "../testing";

const DATASETS = [
  {
    id: "ds-batch",
    key: "ds_batch",
    name: "批次集",
    source_kind: "upload" as const,
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "批次号", type: "TEXT" },
      { name: "产品名称", type: "TEXT" },
      { name: "地块编号", type: "TEXT" },
    ],
    row_count: 20,
    size_bytes: 480,
    sync_cron: null,
    sync_status: "ok" as const,
    paused_reason: null,
    created_at: "2026-10-01T08:00:00",
    updated_at: "2026-10-01T08:00:00",
    customer_id: "demo-customer",
  },
  {
    id: "ds-spray",
    key: "ds_spray",
    name: "打药记录",
    source_kind: "upload" as const,
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "施药日期", type: "DATE" },
      { name: "地块编号", type: "TEXT" },
      { name: "药剂", type: "TEXT" },
    ],
    row_count: 200,
    size_bytes: 4800,
    sync_cron: null,
    sync_status: "ok" as const,
    paused_reason: null,
    created_at: "2026-10-02T08:00:00",
    updated_at: "2026-10-02T08:00:00",
    customer_id: "demo-customer",
  },
];

const INITIAL_BODY: RecordViewSpecBody = {
  datasetId: null,
  keyColumn: null,
  headerFields: [],
  sections: [],
};

/** 试预览端点成功载荷（服务端同形组装）。 */
const PREVIEW_BODY = {
  title: "批次溯源页",
  version: 2,
  expires_at: null,
  found: true,
  header: [
    { label: "产品名称", column: "产品名称", value: "云南咖啡豆" },
  ],
  sections: [
    {
      title: "打药记录",
      dateColumn: "施药日期",
      rows: [{ 施药日期: "2026-03-01", 药剂: "波尔多液" }],
      truncated: false,
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

/** 装假后端（数据集 + 产物）并包一层 preview 端点拦截（假后端无该路由）。 */
function install(previewStatus = 200, previewBody: unknown = PREVIEW_BODY) {
  const backend = installConsoleBackend([], { datasets: DATASETS });
  backend.addLifecycleArtifact({
    id: 41,
    kind: "record-view",
    title: "批次溯源页",
    version: 1,
    spec: {
      title: "批次溯源页",
      spec: { datasetId: null, keyColumn: null, headerFields: [], sections: [] },
    },
  });
  const baseFetch = backend.fetchImpl;
  const wrapped = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    if (
      url.pathname === "/api/v1/artifacts/41/record-view/preview" &&
      (init?.method ?? "GET").toUpperCase() === "POST"
    ) {
      return Promise.resolve({
        ok: previewStatus < 400,
        status: previewStatus,
        json: () => Promise.resolve(previewBody),
      } as unknown as Response);
    }
    return baseFetch(input as string, init);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", wrapped);
  return backend;
}

/** 打开编辑器（等待数据集列表就绪）。 */
async function openEditor(previewStatus = 200, previewBody?: unknown) {
  const backend = install(previewStatus, previewBody);
  render(
    <RecordViewEditor
      artifactId={41}
      initial={{ title: "批次溯源页", body: INITIAL_BODY }}
      onSaved={vi.fn()}
      onExit={vi.fn()}
    />,
  );
  const datasetSelect = (await screen.findByTestId(
    "editor-recordview-dataset",
  )) as HTMLSelectElement;
  await waitFor(() => {
    expect(datasetSelect.options.length).toBeGreaterThanOrEqual(3); // 占位 + 两数据集
  });
  return { backend, datasetSelect };
}

function headerCheckbox(name: string): HTMLInputElement {
  return screen
    .getAllByTestId("editor-recordview-header-column")
    .find((el) => el.getAttribute("data-column") === name)! as HTMLInputElement;
}

describe("RecordViewEditor（构建器落形）", () => {
  it("选主数据集 → 键列下拉（列快照）；勾列成头部字段并按列序排列", async () => {
    const { datasetSelect } = await openEditor();

    fireEvent.change(datasetSelect, { target: { value: "ds-batch" } });
    const keySelect = (await screen.findByTestId(
      "editor-recordview-key-column",
    )) as HTMLSelectElement;
    expect(
      [...keySelect.options].map((o) => o.value),
    ).toEqual(["", "批次号", "产品名称", "地块编号"]);

    // 勾两列（反序勾选仍按数据集列序）。
    fireEvent.click(headerCheckbox("地块编号"));
    fireEvent.click(headerCheckbox("产品名称"));
    expect(
      screen
        .getAllByTestId("editor-recordview-field")
        .map((el) => el.getAttribute("data-column")),
    ).toEqual(["产品名称", "地块编号"]);

    // label 改写（缺省回落列名）。
    const labelInput = screen
      .getAllByTestId("editor-recordview-field")
      .find((el) => el.getAttribute("data-column") === "产品名称")!
      .querySelector('[data-testid="editor-recordview-field-label"]')!;
    fireEvent.change(labelInput, { target: { value: "产品" } });
    expect((labelInput as HTMLInputElement).value).toBe("产品");
  });

  it("sections 配置：段卡（数据集/日期列/上限）+ filters 字面与 {fromField} 互切；保存落形信封", async () => {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-batch" } });
    fireEvent.change(await screen.findByTestId("editor-recordview-key-column"), {
      target: { value: "批次号" },
    });
    fireEvent.click(headerCheckbox("产品名称"));

    // 添加一个时间轴段。
    fireEvent.click(screen.getByTestId("editor-recordview-add-section"));
    fireEvent.change(screen.getByTestId("editor-recordview-section-title"), {
      target: { value: "打药记录" },
    });
    fireEvent.change(screen.getByTestId("editor-recordview-section-dataset"), {
      target: { value: "ds-spray" },
    });
    fireEvent.change(
      screen.getByTestId("editor-recordview-section-date-column"),
      { target: { value: "施药日期" },
    });
    // 上限改 100。
    fireEvent.change(screen.getByTestId("editor-recordview-section-limit"), {
      target: { value: "100" },
    });

    // 过滤条件：字面值 → 切 {fromField} 引用主记录列。
    fireEvent.click(screen.getByTestId("editor-recordview-add-filter"));
    fireEvent.change(screen.getByTestId("editor-recordview-filter-column"), {
      target: { value: "地块编号" },
    });
    fireEvent.change(screen.getByTestId("editor-recordview-filter-op"), {
      target: { value: "eq" },
    });
    fireEvent.change(screen.getByTestId("editor-recordview-filter-value"), {
      target: { value: "A-01" },
    });
    fireEvent.click(
      screen.getByTestId("editor-recordview-filter-value-to-field"),
    );
    const fieldSelect = screen.getByTestId(
      "editor-recordview-filter-value-field",
    ) as HTMLSelectElement;
    expect([...fieldSelect.options].map((o) => o.value)).toEqual([
      "",
      "批次号",
      "产品名称",
      "地块编号",
    ]);
    fireEvent.change(fieldSelect, { target: { value: "地块编号" } });

    // 保存：PUT 信封 {title, spec:{datasetId, keyColumn, headerFields, sections}}。
    fireEvent.click(screen.getByTestId("editor-recordview-save"));
    await screen.findByTestId("editor-recordview-saved-version");
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put?.url.pathname).toBe("/api/v1/artifacts/41");
    expect(put?.body).toEqual({
      spec: {
        title: "批次溯源页",
        spec: {
          datasetId: "ds-batch",
          keyColumn: "批次号",
          headerFields: [{ column: "产品名称" }],
          sections: [
            {
              title: "打药记录",
              datasetId: "ds-spray",
              dateColumn: "施药日期",
              filters: [
                { column: "地块编号", op: "eq", value: { fromField: "地块编号" } },
              ],
              limit: 100,
            },
          ],
        },
      },
    });
  });

  it("草稿校验镜像零请求拦截：缺键列 → keyColumnRequired；未绑中间态允许保存", async () => {
    const { backend, datasetSelect } = await openEditor();

    // 未绑数据集的最小空 spec：允许保存（中间态，PUT 发出）。
    fireEvent.click(screen.getByTestId("editor-recordview-save"));
    await screen.findByTestId("editor-recordview-saved-version");
    expect(backend.calls.some((c) => c.method === "PUT")).toBe(true);

    // 绑定后未选键列：零请求拦截。
    fireEvent.change(datasetSelect, { target: { value: "ds-batch" } });
    fireEvent.click(screen.getByTestId("editor-recordview-save"));
    await screen.findByTestId("editor-recordview-error");
    expect(
      screen
        .getAllByTestId("editor-recordview-error")
        .map((el) => el.getAttribute("data-code")),
    ).toContain("keyColumnRequired");
  });

  // wire-console-resilience 任务 4.3：多步连续配置一次走通——真实浏览器/E2E
  // 里事件背靠背派发（不等 React 提交），此前的变更处理函数读渲染闭包里的
  // 旧草稿会把前一步选择清掉（stale closure 复现形态）；修复后每步 onChange
  // 立即写入草稿（函数式更新，草稿态单一来源）。用原生事件直发（不包 act、
  // 事件间不等待）钉死该形态。
  it("多步配置状态保持：事件背靠背派发后主数据集/键列/头部字段/段全保持，保存落形一致", async () => {
    const { backend, datasetSelect } = await openEditor();

    /**
     * 原生 change 直发（E2E/真实浏览器形态——不经 act 包装），随后等本步的
     * **实际效果**出现（真实浏览器里每次交互是独立任务，React 先提交再接受
     * 下一次交互；等根节点等于没等——要钉死的是提交后下一步仍保留前一步）。
     */
    const fireChange = async (
      el: HTMLElement,
      value: string,
      effect: () => void,
    ) => {
      (el as HTMLInputElement).value = value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(effect);
    };

    // 四步背靠背：选主数据集 → 选键列 → 勾头部字段 → 加段选段数据集。
    await fireChange(datasetSelect, "ds-batch", () => {
      expect(
        (screen.getByTestId("editor-recordview-key-column") as HTMLSelectElement)
          .options.length,
      ).toBeGreaterThan(1);
    });
    await fireChange(
      screen.getByTestId("editor-recordview-key-column"),
      "批次号",
      () => {
        expect(
          (screen.getByTestId("editor-recordview-dataset") as HTMLSelectElement)
            .value,
        ).toBe("ds-batch");
      },
    );
    // 勾选是受控 checkbox：用点击（React 的 value tracker 会正确处理）而非
    // 手工置 checked——后者对受控组件不触发 onChange。
    fireEvent.click(
      screen
        .getAllByTestId("editor-recordview-header-column")
        .find((el) => el.getAttribute("data-column") === "产品名称")!,
    );
    await screen.findByTestId("editor-recordview-field");
    screen.getByTestId("editor-recordview-add-section").click();
    await screen.findByTestId("editor-recordview-section-dataset");
    await fireChange(
      screen.getByTestId("editor-recordview-section-dataset"),
      "ds-spray",
      () => {
        expect(
          (screen.getByTestId("editor-recordview-dataset") as HTMLSelectElement)
            .value,
        ).toBe("ds-batch");
      },
    );
    // 段日期列（section dataset 换集即清日期列，本步在其后）。
    await fireChange(
      screen.getByTestId("editor-recordview-section-date-column"),
      "施药日期",
      () => {
        expect(
          (screen.getByTestId(
            "editor-recordview-section-dataset",
          ) as HTMLSelectElement).value,
        ).toBe("ds-spray");
      },
    );

    // 每一步的先前选择全部保持（任一被清即失败）。
    expect(
      (screen.getByTestId("editor-recordview-dataset") as HTMLSelectElement)
        .value,
    ).toBe("ds-batch");
    expect(
      (screen.getByTestId("editor-recordview-key-column") as HTMLSelectElement)
        .value,
    ).toBe("批次号");
    expect(
      screen
        .getAllByTestId("editor-recordview-field")
        .map((el) => el.getAttribute("data-column")),
    ).toEqual(["产品名称"]);
    expect(
      (screen.getByTestId("editor-recordview-section-title") as HTMLInputElement)
        .value,
    ).toBe("");
    expect(
      (screen.getByTestId(
        "editor-recordview-section-dataset",
      ) as HTMLSelectElement).value,
    ).toBe("ds-spray");
    expect(
      (screen.getByTestId(
        "editor-recordview-section-date-column",
      ) as HTMLSelectElement).value,
    ).toBe("施药日期");

    // 段标题补上（背靠背里继续输入不丢段配置）→ 保存。
    // 段标题是受控 input：用 fireEvent.change（React 的 value tracker 会正确
    // 处理；手工置 value 再派 change 对受控组件会被回滚）。
    fireEvent.change(screen.getByTestId("editor-recordview-section-title"), {
      target: { value: "打药记录" },
    });
    await waitFor(() => {
      expect(
        (screen.getByTestId(
          "editor-recordview-section-title",
        ) as HTMLInputElement).value,
      ).toBe("打药记录");
    });
    fireEvent.click(screen.getByTestId("editor-recordview-save"));
    await screen.findByTestId("editor-recordview-saved-version");

    // spec 与界面配置一致（保存后草稿不被归一化清写）。
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put?.url.pathname).toBe("/api/v1/artifacts/41");
    expect(put?.body).toEqual({
      spec: {
        title: "批次溯源页",
        spec: {
          datasetId: "ds-batch",
          keyColumn: "批次号",
          headerFields: [{ column: "产品名称" }],
          sections: [
            {
              title: "打药记录",
              datasetId: "ds-spray",
              dateColumn: "施药日期",
              filters: [],
              limit: 200,
            },
          ],
        },
      },
    });
  });

  it("多步配置状态保持（act 形态回归）：勾列后逐步交互 datasetId 仍在（fireEvent 路径零漂移）", async () => {
    const { datasetSelect } = await openEditor();

    fireEvent.change(datasetSelect, { target: { value: "ds-batch" } });
    fireEvent.change(await screen.findByTestId("editor-recordview-key-column"), {
      target: { value: "批次号" },
    });
    fireEvent.click(headerCheckbox("产品名称"));
    fireEvent.click(screen.getByTestId("editor-recordview-add-section"));
    fireEvent.change(screen.getByTestId("editor-recordview-section-dataset"), {
      target: { value: "ds-spray" },
    });

    // 既有 fireEvent（act 包裹）路径同样全保持——正常路径零漂移。
    expect(
      (screen.getByTestId("editor-recordview-dataset") as HTMLSelectElement)
        .value,
    ).toBe("ds-batch");
    expect(
      (screen.getByTestId("editor-recordview-key-column") as HTMLSelectElement)
        .value,
    ).toBe("批次号");
    expect(
      screen
        .getAllByTestId("editor-recordview-field")
        .map((el) => el.getAttribute("data-column")),
    ).toEqual(["产品名称"]);
  });
});

describe("RecordViewEditor（试预览 4.2）", () => {
  it("键值下拉（distinct 查询升序去重）+ 手输兜底 → POST preview {key} → 同构渲染", async () => {
    // 主数据集 distinct 批次号：两值（服务端升序）。
    const backend = installConsoleBackend([], {
      datasets: DATASETS,
      datasetRows: {
        "ds-batch": {
          columns: DATASETS[0]!.columns,
          rows: [
            { 批次号: "B-02", 产品名称: "a", 地块编号: "x" },
            { 批次号: "B-01", 产品名称: "b", 地块编号: "y" },
          ],
        },
      },
    });
    backend.addLifecycleArtifact({
      id: 41,
      kind: "record-view",
      title: "批次溯源页",
      spec: {
        title: "批次溯源页",
        spec: { datasetId: null, keyColumn: null, headerFields: [], sections: [] },
      },
    });
    let previewCalled: { url: string; body: unknown } | null = null;
    const baseFetch = backend.fetchImpl;
    vi.stubGlobal(
      "fetch",
      ((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://console.test");
        if (
          url.pathname === "/api/v1/artifacts/41/record-view/preview" &&
          (init?.method ?? "GET").toUpperCase() === "POST"
        ) {
          previewCalled = {
            url: url.pathname,
            body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
          };
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(PREVIEW_BODY),
          } as unknown as Response);
        }
        return baseFetch(input as string, init);
      }) as unknown as typeof fetch,
    );

    render(
      <RecordViewEditor
        artifactId={41}
        initial={{ title: "批次溯源页", body: INITIAL_BODY }}
        onSaved={vi.fn()}
        onExit={vi.fn()}
      />,
    );
    const datasetSelect = (await screen.findByTestId(
      "editor-recordview-dataset",
    )) as HTMLSelectElement;
    await waitFor(() => {
      expect(datasetSelect.options.length).toBeGreaterThanOrEqual(3);
    });
    fireEvent.change(datasetSelect, { target: { value: "ds-batch" } });
    fireEvent.change(await screen.findByTestId("editor-recordview-key-column"), {
      target: { value: "批次号" },
    });

    // 键值下拉就绪：distinct 升序两值。
    const keySelect = (await screen.findByTestId(
      "record-view-preview-key-select",
    )) as HTMLSelectElement;
    await waitFor(() => {
      expect([...keySelect.options].map((o) => o.value)).toEqual([
        "",
        "B-01",
        "B-02",
      ]);
    });

    // 下拉选择 → 试预览（POST {key}）。
    fireEvent.change(keySelect, { target: { value: "B-02" } });
    fireEvent.click(screen.getByTestId("record-view-preview-button"));
    await screen.findByTestId("record-view-body");
    expect(previewCalled).toEqual({
      url: "/api/v1/artifacts/41/record-view/preview",
      body: { key: "B-02" },
    });
    // 同构渲染：header 值 + 时间轴节点。
    expect(
      screen.getByTestId("record-view-header-field").textContent,
    ).toContain("云南咖啡豆");
    expect(screen.getAllByTestId("record-view-node-toggle").length).toBe(1);

    // 手输兜底：手输优先于下拉选择。
    fireEvent.change(screen.getByTestId("record-view-preview-key-manual"), {
      target: { value: "B-99" },
    });
    previewCalled = null;
    fireEvent.click(screen.getByTestId("record-view-preview-button"));
    await waitFor(() => {
      expect(previewCalled).toEqual({
        url: "/api/v1/artifacts/41/record-view/preview",
        body: { key: "B-99" },
      });
    });
  });

  it("未绑数据集：试预览区如实提示不可用（无键值查询、无 preview 请求）", async () => {
    await openEditor();
    expect(
      screen.getByTestId("record-view-preview").textContent,
    ).toContain("绑定主数据集并选择键列后可试预览");
    expect(
      screen.queryByTestId("record-view-preview-button"),
    ).toBeNull();
  });

  it("preview 失败（422）：服务端 detail 如实红条呈现", async () => {
    await openEditor(422, { detail: "主数据集已不存在" });
    const datasetSelect = screen.getByTestId(
      "editor-recordview-dataset",
    ) as HTMLSelectElement;
    fireEvent.change(datasetSelect, { target: { value: "ds-batch" } });
    fireEvent.change(await screen.findByTestId("editor-recordview-key-column"), {
      target: { value: "批次号" },
    });
    fireEvent.change(screen.getByTestId("record-view-preview-key-manual"), {
      target: { value: "B-01" },
    });
    fireEvent.click(screen.getByTestId("record-view-preview-button"));
    const errorBox = await screen.findByTestId("record-view-preview-error");
    expect(errorBox.textContent).toContain("主数据集已不存在");
  });
});
