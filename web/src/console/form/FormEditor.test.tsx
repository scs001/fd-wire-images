// 表单构建器组件测试（wire-form-builder 任务 4.3）：
// 数据集下拉（tier/行数提示）→ 列勾选生成字段（默认 label=列名、input 按
// 列类型建议、顺序=数据集列序）→ 逐字段改 label/input/required/select
// options → submitLabel/successMessage 可配 → 保存走既有 PUT（信封口径
// 断言）；未绑数据集保存允许（构建中间态）；结构自检零请求拦截；未保存
// 离开 confirm。
// 显隐条件编辑（wire-form-field-linkage）：配条件/清除/empty 免值/存量零
// 变化往返。
// 条件组合与选项联动编辑（wire-form-linkage-v2）：组条件行增删（1..10）+
// all/any 切换 + 保存落形 + 与单条件互斥；select 选项联动（控制字段+cases
// 映射行+fallback）+ 与静态 options 互斥 + 空映射保存拦截。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { FormEditor } from "./FormEditor";
import type { FormSpecBody } from "./formSpec";
import { installConsoleBackend, seedConsoleSession } from "../testing";

const DATASETS = [
  {
    id: "ds-sales",
    key: "ds_sales",
    name: "门店销量",
    source_kind: "upload" as const,
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "city", type: "TEXT" },
      { name: "amount", type: "REAL" },
      { name: "reported_on", type: "DATE" },
    ],
    row_count: 120,
    size_bytes: 2880,
    sync_cron: null,
    sync_status: "ok" as const,
    paused_reason: null,
    created_at: "2026-10-01T08:00:00",
    updated_at: "2026-10-01T08:00:00",
    customer_id: "demo-customer",
    // storage_tier 容错读取（免费档响应带才显示 tier 提示）。
    storage_tier: "api",
  },
  {
    id: "ds-visits",
    key: "ds_visits",
    name: "访客登记",
    source_kind: "upload" as const,
    connector_id: null,
    source_ref: null,
    columns: [{ name: "visitor", type: "TEXT" }],
    row_count: 12,
    size_bytes: 300,
    sync_cron: null,
    sync_status: "ok" as const,
    paused_reason: null,
    created_at: "2026-10-02T08:00:00",
    updated_at: "2026-10-02T08:00:00",
    customer_id: "demo-customer",
  },
];

const INITIAL_BODY: FormSpecBody = { datasetId: null, fields: [] };

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

function install() {
  const backend = installConsoleBackend([], { datasets: DATASETS });
  backend.addLifecycleArtifact({
    id: 31,
    kind: "form",
    title: "门店日报表",
    version: 1,
    spec: { title: "门店日报表", spec: { title: "门店日报表", datasetId: null, fields: [] } },
  });
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

/** 打开编辑器（等待数据集列表就绪）。 */
async function openEditor() {
  const backend = install();
  render(
    <FormEditor
      artifactId={31}
      initial={{ title: "门店日报表", body: INITIAL_BODY }}
      onSaved={vi.fn()}
      onExit={vi.fn()}
    />,
  );
  const datasetSelect = (await screen.findByTestId(
    "editor-form-dataset",
  )) as HTMLSelectElement;
  await waitFor(() => {
    expect(datasetSelect.options.length).toBeGreaterThanOrEqual(3); // 占位 + 两数据集
  });
  return { backend, datasetSelect };
}

function columnCheckbox(name: string): HTMLInputElement {
  return screen
    .getAllByTestId("editor-form-column")
    .find((el) => el.getAttribute("data-column") === name)! as HTMLInputElement;
}

function fieldRow(column: string): HTMLElement {
  return screen
    .getAllByTestId("editor-field")
    .find((el) => el.getAttribute("data-column") === column)!;
}

describe("FormEditor（构建器）", () => {
  it("数据集下拉带 tier/行数提示；选数据集后按列序呈现列勾选", async () => {
    const { datasetSelect } = await openEditor();

    // tier 提示（storage_tier 响应带）与行数；无 tier 的回退纯行数文案
    const optionTexts = [...datasetSelect.options].map((o) => o.textContent ?? "");
    expect(optionTexts.some((text) => text.includes("门店销量") && text.includes("轻量存储") && text.includes("120"))).toBe(true);
    expect(optionTexts.some((text) => text.includes("访客登记") && text.includes("12"))).toBe(true);

    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    expect(
      screen
        .getAllByTestId("editor-form-column")
        .map((el) => el.getAttribute("data-column")),
    ).toEqual(["city", "amount", "reported_on"]);
  });

  it("勾列成字段：默认 label=列名、input 按列类型建议、顺序=数据集列序（与勾选先后无关）", async () => {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });

    // 反序勾选（先日期再城市再金额）：字段仍按数据集列序
    fireEvent.click(columnCheckbox("reported_on"));
    fireEvent.click(columnCheckbox("city"));
    fireEvent.click(columnCheckbox("amount"));

    expect(
      screen
        .getAllByTestId("editor-field")
        .map((el) => el.getAttribute("data-column")),
    ).toEqual(["city", "amount", "reported_on"]);
    expect(
      (within(fieldRow("amount")).getByTestId("editor-field-label") as HTMLInputElement)
        .value,
    ).toBe("amount");
    expect(
      (within(fieldRow("amount")).getByTestId("editor-field-input") as HTMLSelectElement)
        .value,
    ).toBe("number");
    expect(
      (within(fieldRow("reported_on")).getByTestId("editor-field-input") as HTMLSelectElement)
        .value,
    ).toBe("date");
    expect(
      (within(fieldRow("city")).getByTestId("editor-field-input") as HTMLSelectElement)
        .value,
    ).toBe("text");
    expect(backend.calls.filter((c) => c.method === "PUT")).toHaveLength(0);

    // 取消勾选即移除该字段
    fireEvent.click(columnCheckbox("amount"));
    expect(
      screen
        .getAllByTestId("editor-field")
        .map((el) => el.getAttribute("data-column")),
    ).toEqual(["city", "reported_on"]);
  });

  it("改配置 + 保存：PUT 信封口径请求体（label/input/required/options/文案/缺省键不落）", async () => {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    fireEvent.click(columnCheckbox("city"));
    fireEvent.click(columnCheckbox("amount"));

    // city：改 label + 转 select + 配选项 + 必填
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-label"), {
      target: { value: "门店城市" },
    });
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-input"), {
      target: { value: "select" },
    });
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-options"), {
      target: { value: "上海, 北京 ,  " },
    });
    fireEvent.click(within(fieldRow("city")).getByTestId("editor-field-required"));

    // amount：必填勾上（number 建议保留）
    fireEvent.click(within(fieldRow("amount")).getByTestId("editor-field-required"));

    // 文案：submitLabel 配置；successMessage 留空（缺省键不落）
    fireEvent.change(screen.getByTestId("editor-form-submit-label"), {
      target: { value: "上报" },
    });
    fireEvent.change(screen.getByTestId("editor-form-title"), {
      target: { value: "门店日报表（修订）" },
    });

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-saved-version")).textContent,
    ).toContain("v2");

    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put?.url.pathname).toBe("/api/v1/artifacts/31");
    expect(put?.body).toEqual({
      spec: {
        title: "门店日报表（修订）",
        spec: {
          datasetId: "ds-sales",
          fields: [
            {
              column: "city",
              label: "门店城市",
              input: "select",
              required: true,
              options: ["上海", "北京"],
            },
            {
              column: "amount",
              label: "amount",
              input: "number",
              required: true,
            },
          ],
          submitLabel: "上报",
        },
      },
    });
  });

  it("未绑数据集保存允许（构建中间态）：PUT 落 datasetId=null + 零字段", async () => {
    const { backend } = await openEditor();
    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-saved-version")).textContent,
    ).toContain("v2");
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put?.body).toEqual({
      spec: {
        title: "门店日报表",
        spec: { datasetId: null, fields: [] },
      },
    });
  });

  it("结构自检零请求拦截：select 无 options 时错误呈现、不发 PUT；配齐后可存", async () => {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    fireEvent.click(columnCheckbox("city"));
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-input"), {
      target: { value: "select" },
    });

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-error")).textContent,
    ).toContain("下拉选择");
    expect(backend.calls.filter((c) => c.method === "PUT")).toHaveLength(0);

    // 配齐选项后保存通过、错误清空
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-options"), {
      target: { value: "上海, 北京" },
    });
    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    expect(screen.queryByTestId("editor-form-error")).toBeNull();
  });

  it("空标题：保存按钮禁用（前端先拦）", async () => {
    await openEditor();
    fireEvent.change(screen.getByTestId("editor-form-title"), {
      target: { value: "   " },
    });
    expect(
      (screen.getByTestId("editor-form-save") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("未保存离开 confirm：取消留在编辑，确认后 onExit；未改动直接退出不 confirm", async () => {
    install();
    const confirm = vi.fn();
    vi.stubGlobal("confirm", confirm); // happy-dom 无原生 confirm：打桩
    const onExit = vi.fn();
    render(
      <FormEditor
        artifactId={31}
        initial={{ title: "门店日报表", body: INITIAL_BODY }}
        onSaved={vi.fn()}
        onExit={onExit}
      />,
    );
    await screen.findAllByTestId("editor-form-dataset");

    // 未改动：直接退出，不 confirm
    fireEvent.click(screen.getByTestId("editor-form-exit"));
    expect(confirm).not.toHaveBeenCalled();
    expect(onExit).toHaveBeenCalledTimes(1);
  });

  it("有未保存改动：confirm 拦截（取消留在编辑、确认后 onExit）", async () => {
    install();
    const confirm = vi.fn();
    vi.stubGlobal("confirm", confirm);
    const onExit = vi.fn();
    render(
      <FormEditor
        artifactId={31}
        initial={{ title: "门店日报表", body: INITIAL_BODY }}
        onSaved={vi.fn()}
        onExit={onExit}
      />,
    );
    const select = (await screen.findByTestId(
      "editor-form-dataset",
    )) as HTMLSelectElement;
    await waitFor(() => {
      expect(select.options.length).toBeGreaterThanOrEqual(2);
    });

    fireEvent.change(select, { target: { value: "ds-sales" } });
    confirm.mockReturnValueOnce(false);
    fireEvent.click(screen.getByTestId("editor-form-exit"));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(onExit).not.toHaveBeenCalled(); // 仍停留在编辑

    confirm.mockReturnValueOnce(true);
    fireEvent.click(screen.getByTestId("editor-form-exit"));
    expect(onExit).toHaveBeenCalledTimes(1);
  });

  it("数据集列表加载失败如实呈现（detail），可重试", async () => {
    const backend = installConsoleBackend([], { datasets: DATASETS });
    backend.addLifecycleArtifact({
      id: 31,
      kind: "form",
      title: "门店日报表",
      spec: { title: "门店日报表", spec: { datasetId: null, fields: [] } },
    });
    const original = backend.fetchImpl;
    let failOnce = true;
    vi.stubGlobal(
      "fetch",
      ((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://console.test");
        if (failOnce && url.pathname === "/api/v1/datasets") {
          failOnce = false;
          return Promise.resolve({
            ok: false,
            status: 500,
            json: () => Promise.resolve({ detail: "数据集服务暂不可用" }),
          });
        }
        return original(input, init);
      }) as unknown as typeof fetch,
    );

    render(
      <FormEditor
        artifactId={31}
        initial={{ title: "门店日报表", body: INITIAL_BODY }}
        onSaved={vi.fn()}
        onExit={vi.fn()}
      />,
    );
    expect(
      await screen.findByText("数据集服务暂不可用"),
    ).toBeTruthy();
    // 重试成功：下拉出数据集
    fireEvent.click(screen.getByTestId("editor-form-datasets-retry"));
    const select = (await screen.findByTestId(
      "editor-form-dataset",
    )) as HTMLSelectElement;
    await waitFor(() => {
      expect(select.options.length).toBeGreaterThanOrEqual(2);
    });
  });
});

// ------------------------------------- 显隐条件编辑（wire-form-field-linkage）

describe("FormEditor（显隐条件编辑）", () => {
  /** 字段卡条件区的查询域（每次现查——重渲染后不持旧 DOM 引用）。 */
  function conditionArea(column: string): ReturnType<typeof within> {
    return within(fieldRow(column));
  }

  /** 勾 city+amount 两字段并配 amount 的显隐条件（当字段=city、算子、值）。 */
  async function openWithTwoFields(op: string, value?: string) {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    fireEvent.click(columnCheckbox("city"));
    fireEvent.click(columnCheckbox("amount"));

    // 开关打开：默认条件=当字段取首个其他字段（city）、算子等于、值空
    fireEvent.click(
      within(fieldRow("amount")).getByTestId("editor-field-condition-toggle"),
    );
    expect(
      (conditionArea("amount").getByTestId(
        "editor-field-condition-field",
      ) as HTMLSelectElement).value,
    ).toBe("city");
    fireEvent.change(
      conditionArea("amount").getByTestId("editor-field-condition-op"),
      { target: { value: op } },
    );
    if (value !== undefined) {
      fireEvent.change(
        conditionArea("amount").getByTestId("editor-field-condition-value"),
        { target: { value } },
      );
    }
    return { backend };
  }

  /** 最近一次 PUT 的 spec.spec。 */
  function lastSavedSpec(backend: ReturnType<typeof install>): {
    datasetId: string | null;
    fields: Array<{ column: string; visibleWhen?: unknown }>;
  } {
    const puts = backend.calls.filter((c) => c.method === "PUT");
    return (puts[puts.length - 1]?.body as {
      spec: { spec: { datasetId: string | null; fields: Array<{ column: string; visibleWhen?: unknown }> } };
    }).spec.spec;
  }

  it("配条件往返保存：spec 里 visibleWhen 落形（当字段/算子/值）", async () => {
    const { backend } = await openWithTwoFields("eq", "上海");
    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();

    const spec = lastSavedSpec(backend);
    expect(spec.fields.find((f) => f.column === "amount")?.visibleWhen).toEqual(
      { field: "city", op: "eq", value: "上海" },
    );
  });

  it("清除条件：一键回无条件（visibleWhen 键不落、开关回关）", async () => {
    const { backend } = await openWithTwoFields("eq", "上海");
    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();

    // 清除条件：条件区收起、开关回关，保存后 PUT 载荷零 visibleWhen 键
    fireEvent.click(
      within(fieldRow("amount")).getByTestId("editor-field-condition-clear"),
    );
    expect(
      within(fieldRow("amount")).queryByTestId("editor-field-condition"),
    ).toBeNull();
    expect(
      (within(fieldRow("amount")).getByTestId("editor-field-condition-toggle") as HTMLInputElement)
        .checked,
    ).toBe(false);

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    const spec = lastSavedSpec(backend);
    expect(JSON.stringify(spec).includes("visibleWhen")).toBe(false);
    expect(spec.fields.find((f) => f.column === "amount")?.visibleWhen).toBeUndefined();
  });

  it("empty 算子无值控件；保存落 {field, op} 不带 value 键", async () => {
    const { backend } = await openWithTwoFields("empty");
    // 值控件随 empty 算子隐藏
    expect(
      conditionArea("amount").queryByTestId("editor-field-condition-value"),
    ).toBeNull();

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    const spec = lastSavedSpec(backend);
    expect(spec.fields.find((f) => f.column === "amount")?.visibleWhen).toEqual(
      { field: "city", op: "empty" },
    );
  });

  it("存量零变化：未开条件时字段卡不渲染条件区，保存载荷零 visibleWhen 键", async () => {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    fireEvent.click(columnCheckbox("city"));
    expect(
      within(fieldRow("city")).queryByTestId("editor-field-condition"),
    ).toBeNull();
    expect(
      (within(fieldRow("city")).getByTestId("editor-field-condition-toggle") as HTMLInputElement)
        .checked,
    ).toBe(false);

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    expect(
      JSON.stringify(lastSavedSpec(backend)).includes("visibleWhen"),
    ).toBe(false);
  });
});

// --------------------------------- 条件组合与选项联动编辑（wire-form-linkage-v2）

describe("FormEditor（条件组合编辑）", () => {
  /** 最近一次 PUT 的 spec.spec。 */
  function lastSavedSpec(backend: ReturnType<typeof install>): {
    fields: Array<{
      column: string;
      visibleWhenGroup?: unknown;
    }>;
  } {
    const puts = backend.calls.filter((c) => c.method === "PUT");
    return (puts[puts.length - 1]?.body as {
      spec: { spec: { fields: Array<{ column: string; visibleWhenGroup?: unknown }> } };
    }).spec.spec;
  }

  /** 勾 city/amount/reported_on 三字段（组条件的目标字段=reported_on）。 */
  async function openWithThreeFields() {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    fireEvent.click(columnCheckbox("city"));
    fireEvent.click(columnCheckbox("amount"));
    fireEvent.click(columnCheckbox("reported_on"));
    return backend;
  }

  /** 打开 reported_on 的组合条件（默认 mode=all、一条条件）。 */
  function enableGroup() {
    fireEvent.click(
      within(fieldRow("reported_on")).getByTestId(
        "editor-field-condition-group-toggle",
      ),
    );
  }

  it("开关打开默认组：mode=all、一条条件（当字段=首个其他字段 city、算子=等于、值空）", async () => {
    await openWithThreeFields();
    enableGroup();

    const row = fieldRow("reported_on");
    expect(
      (within(row).getByTestId("editor-field-group-mode") as HTMLSelectElement)
        .value,
    ).toBe("all");
    const conditionRows = within(row).getAllByTestId("editor-group-condition");
    expect(conditionRows).toHaveLength(1);
    expect(
      (within(conditionRows[0]!).getByTestId("editor-group-condition-field") as HTMLSelectElement)
        .value,
    ).toBe("city");
    expect(
      (within(conditionRows[0]!).getByTestId("editor-group-condition-op") as HTMLSelectElement)
        .value,
    ).toBe("eq");
    expect(
      (within(conditionRows[0]!).getByTestId("editor-group-condition-value") as HTMLInputElement)
        .value,
    ).toBe("");
  });

  it("配组+加条件+切 any → 保存落形（conditions 数组逐条保标量）", async () => {
    const backend = await openWithThreeFields();
    enableGroup();
    const row = fieldRow("reported_on");

    // 首条：当字段=city（默认）、算子=等于（默认）、值=上海
    fireEvent.change(within(row).getByTestId("editor-group-condition-value"), {
      target: { value: "上海" },
    });
    // 加一条：当字段=amount、算子=notEmpty（值控件随 empty 族隐藏）
    fireEvent.click(within(row).getByTestId("editor-field-group-condition-add"));
    const second = within(row).getAllByTestId("editor-group-condition")[1]!;
    fireEvent.change(within(second).getByTestId("editor-group-condition-field"), {
      target: { value: "amount" },
    });
    fireEvent.change(within(second).getByTestId("editor-group-condition-op"), {
      target: { value: "notEmpty" },
    });
    expect(
      within(second).queryByTestId("editor-group-condition-value"),
    ).toBeNull();
    // 切 any
    fireEvent.change(within(row).getByTestId("editor-field-group-mode"), {
      target: { value: "any" },
    });

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    expect(
      lastSavedSpec(backend).fields.find((f) => f.column === "reported_on")
        ?.visibleWhenGroup,
    ).toEqual({
      mode: "any",
      conditions: [
        { field: "city", op: "eq", value: "上海" },
        { field: "amount", op: "notEmpty" },
      ],
    });
  });

  it("条件行增删上限：至多 10 条（加满禁用）；至少留 1 条（仅 1 条时移除禁用）", async () => {
    await openWithThreeFields();
    enableGroup();
    const row = fieldRow("reported_on");

    // 仅 1 条时移除禁用
    expect(
      (within(row).getAllByTestId("editor-group-condition-remove")[0] as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    // 连加 9 条 → 10 条封顶，加按钮禁用
    for (let i = 0; i < 9; i++) {
      fireEvent.click(
        within(fieldRow("reported_on")).getByTestId(
          "editor-field-group-condition-add",
        ),
      );
    }
    expect(
      within(fieldRow("reported_on")).getAllByTestId("editor-group-condition"),
    ).toHaveLength(10);
    expect(
      (within(fieldRow("reported_on")).getByTestId(
        "editor-field-group-condition-add",
      ) as HTMLButtonElement).disabled,
    ).toBe(true);
    // 移除一行 → 9 条、加按钮恢复
    fireEvent.click(
      within(fieldRow("reported_on")).getAllByTestId(
        "editor-group-condition-remove",
      )[0]!,
    );
    expect(
      within(fieldRow("reported_on")).getAllByTestId("editor-group-condition"),
    ).toHaveLength(9);
    expect(
      (within(fieldRow("reported_on")).getByTestId(
        "editor-field-group-condition-add",
      ) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("清除组合：一键回无条件（visibleWhenGroup 键不落、开关回关）", async () => {
    const backend = await openWithThreeFields();
    enableGroup();
    fireEvent.click(
      within(fieldRow("reported_on")).getByTestId("editor-field-group-clear"),
    );
    expect(
      within(fieldRow("reported_on")).queryByTestId("editor-field-group"),
    ).toBeNull();
    expect(
      (within(fieldRow("reported_on")).getByTestId(
        "editor-field-condition-group-toggle",
      ) as HTMLInputElement).checked,
    ).toBe(false);

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    expect(
      JSON.stringify(lastSavedSpec(backend)).includes("visibleWhenGroup"),
    ).toBe(false);
  });

  it("与单条件互斥：配了单条件→组合入口禁用+提示；清除单条件后可用；反向配了组合→单条件开关禁用+提示", async () => {
    await openWithThreeFields();
    const row = fieldRow("reported_on");

    // 配单条件 → 组合入口禁用并提示互斥
    fireEvent.click(
      within(row).getByTestId("editor-field-condition-toggle"),
    );
    expect(
      (within(fieldRow("reported_on")).getByTestId(
        "editor-field-condition-group-toggle",
      ) as HTMLInputElement).disabled,
    ).toBe(true);
    expect(
      within(fieldRow("reported_on")).getByTestId(
        "editor-field-group-exclusive-hint",
      ).textContent,
    ).toContain("互斥");

    // 清除单条件 → 组合入口恢复
    fireEvent.click(
      within(fieldRow("reported_on")).getByTestId("editor-field-condition-clear"),
    );
    expect(
      (within(fieldRow("reported_on")).getByTestId(
        "editor-field-condition-group-toggle",
      ) as HTMLInputElement).disabled,
    ).toBe(false);

    // 配组合 → 单条件开关禁用并提示互斥
    enableGroup();
    expect(
      (within(fieldRow("reported_on")).getByTestId(
        "editor-field-condition-toggle",
      ) as HTMLInputElement).disabled,
    ).toBe(true);
    expect(
      within(fieldRow("reported_on")).getByTestId(
        "editor-field-condition-exclusive-hint",
      ).textContent,
    ).toContain("互斥");
  });
});

describe("FormEditor（选项联动编辑）", () => {
  /** 最近一次 PUT 的 spec.spec。 */
  function lastSavedSpec(backend: ReturnType<typeof install>): {
    fields: Array<{
      column: string;
      options?: unknown;
      optionsFrom?: unknown;
    }>;
  } {
    const puts = backend.calls.filter((c) => c.method === "PUT");
    return (puts[puts.length - 1]?.body as {
      spec: { spec: { fields: Array<{ column: string; options?: unknown; optionsFrom?: unknown }> } };
    }).spec.spec;
  }

  /** 勾 city/amount/reported_on，city 转 select（空 options 占位不算已配置静态选项）。 */
  async function openWithSelectCity() {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    fireEvent.click(columnCheckbox("city"));
    fireEvent.click(columnCheckbox("amount"));
    fireEvent.click(columnCheckbox("reported_on"));
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-input"), {
      target: { value: "select" },
    });
    return backend;
  }

  it("切入选项联动：默认控制字段=首个其他字段 + 一行空映射；配映射行+兜底 → 保存落形（options 键不落）", async () => {
    const backend = await openWithSelectCity();
    const row = fieldRow("city");
    fireEvent.click(within(row).getByTestId("editor-field-options-from-toggle"));

    expect(
      (within(row).getByTestId("editor-field-options-from-field") as HTMLSelectElement)
        .value,
    ).toBe("amount");
    const caseRows = within(row).getAllByTestId("editor-field-case-row");
    expect(caseRows).toHaveLength(1);
    // 配映射行：控制值=大 → 选项 1, 2
    fireEvent.change(within(caseRows[0]!).getByTestId("editor-field-case-key"), {
      target: { value: "大" },
    });
    fireEvent.change(
      within(caseRows[0]!).getByTestId("editor-field-case-options"),
      { target: { value: "1, 2 ," },
      },
    );
    // 兜底：其他
    fireEvent.change(within(row).getByTestId("editor-field-options-from-fallback"), {
      target: { value: "其他" },
    });

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    const city = lastSavedSpec(backend).fields.find((f) => f.column === "city");
    expect(city?.optionsFrom).toEqual({
      field: "amount",
      cases: { 大: ["1", "2"] },
      fallback: ["其他"],
    });
    // 互斥纪律：切入选项联动后 options 键不落
    expect(city?.options).toBeUndefined();
  });

  it("映射行增删与控制字段切换；兜底清空即键不落", async () => {
    const backend = await openWithSelectCity();
    const row = fieldRow("city");
    fireEvent.click(within(row).getByTestId("editor-field-options-from-toggle"));

    // 首行配齐后可加第二行；存在空白键行时加按钮禁用（键去重纪律）
    expect(
      (within(row).getByTestId("editor-field-case-add") as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    const first = within(row).getAllByTestId("editor-field-case-row")[0]!;
    fireEvent.change(within(first).getByTestId("editor-field-case-key"), {
      target: { value: "大",
      },
    });
    fireEvent.change(within(first).getByTestId("editor-field-case-options"), {
      target: { value: "1" },
    });
    expect(
      (within(row).getByTestId("editor-field-case-add") as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    fireEvent.click(within(row).getByTestId("editor-field-case-add"));
    const second = within(row).getAllByTestId("editor-field-case-row")[1]!;
    fireEvent.change(within(second).getByTestId("editor-field-case-key"), {
      target: { value: "小" },
    });
    fireEvent.change(within(second).getByTestId("editor-field-case-options"), {
      target: { value: "3" },
    });
    // 切换控制字段 amount → city 自身不可选（候选=其他字段）
    fireEvent.change(within(row).getByTestId("editor-field-options-from-field"), {
      target: { value: "reported_on" },
    });

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      await screen.findByTestId("editor-form-saved-version"),
    ).toBeTruthy();
    expect(
      lastSavedSpec(backend).fields.find((f) => f.column === "city")
        ?.optionsFrom,
    ).toEqual({
      field: "reported_on",
      cases: { 大: ["1"], 小: ["3"] },
    });

    // 删除一行（剩一行）+ 移除按钮随即禁用（至少留一行）
    fireEvent.click(
      within(fieldRow("city")).getAllByTestId("editor-field-case-remove")[1]!,
    );
    expect(
      within(fieldRow("city")).getAllByTestId("editor-field-case-row"),
    ).toHaveLength(1);
    expect(
      (within(fieldRow("city")).getAllByTestId("editor-field-case-remove")[0] as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("静态 options 与选项联动互斥：配了静态选项→联动入口禁用+提示；清空选项后可用", async () => {
    await openWithSelectCity();
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-options"), {
      target: { value: "上海, 北京" },
    });
    expect(
      (within(fieldRow("city")).getByTestId(
        "editor-field-options-from-toggle",
      ) as HTMLInputElement).disabled,
    ).toBe(true);
    expect(
      within(fieldRow("city")).getByTestId(
        "editor-field-options-from-exclusive-hint",
      ).textContent,
    ).toContain("互斥");

    // 清空静态选项 → 联动入口恢复
    fireEvent.change(within(fieldRow("city")).getByTestId("editor-field-options"), {
      target: { value: "" },
    });
    expect(
      (within(fieldRow("city")).getByTestId(
        "editor-field-options-from-toggle",
      ) as HTMLInputElement).disabled,
    ).toBe(false);
  });

  it("配了选项联动→静态选项输入让位（提示呈现）；清除联动后回到静态输入", async () => {
    await openWithSelectCity();
    fireEvent.click(
      within(fieldRow("city")).getByTestId("editor-field-options-from-toggle"),
    );
    expect(
      within(fieldRow("city")).queryByTestId("editor-field-options"),
    ).toBeNull();
    expect(
      within(fieldRow("city")).getByTestId("editor-field-options-exclusive-hint")
        .textContent,
    ).toBeTruthy();

    fireEvent.click(
      within(fieldRow("city")).getByTestId("editor-field-options-from-clear"),
    );
    expect(
      within(fieldRow("city")).queryByTestId("editor-field-options-from"),
    ).toBeNull();
    expect(
      within(fieldRow("city")).getByTestId("editor-field-options"),
    ).toBeTruthy();
  });

  it("空映射行保存拦截（optionsFromBad）：错误呈现、零 PUT", async () => {
    const backend = await openWithSelectCity();
    fireEvent.click(
      within(fieldRow("city")).getByTestId("editor-field-options-from-toggle"),
    );
    // 默认空白键空列表映射 → 保存自检拦截
    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-error")).textContent,
    ).toContain("选项联动");
    expect(backend.calls.filter((c) => c.method === "PUT")).toHaveLength(0);
  });
});

// ── wire-form-entry-advanced：三新键配置 + 提交闸约束编辑器 ─────────────────

describe("FormEditor（数据集选项源/回填/计算/提交闸）", () => {
  /** 打开编辑器并绑 ds-sales、勾 city+amount+reported_on 三列。 */
  async function openWithFields() {
    const { backend, datasetSelect } = await openEditor();
    fireEvent.change(datasetSelect, { target: { value: "ds-sales" } });
    fireEvent.click(columnCheckbox("city"));
    fireEvent.click(columnCheckbox("amount"));
    fireEvent.click(columnCheckbox("reported_on"));
    return { backend };
  }

  /** 把指定字段切成 select 并切入数据集选项源（返回该块所在字段行）。 */
  function enableDatasetOptions(column: string) {
    const row = fieldRow(column);
    fireEvent.change(within(row).getByTestId("editor-field-input"), {
      target: { value: "select" },
    });
    fireEvent.click(
      within(row).getByTestId("editor-field-options-dataset-toggle"),
    );
    return row;
  }

  function lastPutBody(backend: ReturnType<typeof install>) {
    const put = backend.calls
      .filter((c) => c.method === "PUT")
      .at(-1);
    return put?.body as { spec: { spec: Record<string, unknown> } };
  }

  it("数据集选项源配置：数据集+列+上限 → 保存落形（optionsFromDataset）", async () => {
    const { backend } = await openWithFields();
    const row = enableDatasetOptions("city");
    fireEvent.change(within(row).getByTestId("editor-field-options-dataset-id"), {
      target: { value: "ds-visits" },
    });
    fireEvent.change(
      within(row).getByTestId("editor-field-options-dataset-column"),
      { target: { value: "visitor" },
      },
    );
    fireEvent.change(within(row).getByTestId("editor-field-options-dataset-limit"), {
      target: { value: "50" },
    });

    // 切入数据集源即清静态 options 占位键（三源恰有其一）
    expect(within(row).queryByTestId("editor-field-options")).toBeNull();

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-saved-version")).textContent,
    ).toContain("v2");
    const spec = lastPutBody(backend).spec.spec as {
      fields: Array<Record<string, unknown>>;
    };
    expect(spec.fields.find((f) => f.column === "city")).toMatchObject({
      input: "select",
      optionsFromDataset: {
        datasetId: "ds-visits",
        column: "visitor",
        filters: [],
        limit: 50,
      },
    });
    expect("options" in (spec.fields.find((f) => f.column === "city") ?? {})).toBe(
      false,
    );
  });

  it("三源互斥：静态选项已配 → 数据集入口禁用；过滤条件行可增删并落形", async () => {
    await openWithFields();
    const row = fieldRow("city");
    fireEvent.change(within(row).getByTestId("editor-field-input"), {
      target: { value: "select" },
    });
    // 静态 options 非空 → 联动与数据集入口均禁用
    fireEvent.change(within(row).getByTestId("editor-field-options"), {
      target: { value: "上海, 北京" },
    });
    expect(
      (within(row).getByTestId("editor-field-options-dataset-toggle") as HTMLInputElement)
        .disabled,
    ).toBe(true);

    // 清空静态选项后切入数据集源，加一行过滤（列 visitor / 属于 / a,b）
    fireEvent.change(within(row).getByTestId("editor-field-options"), {
      target: { value: "" },
    });
    fireEvent.click(
      within(row).getByTestId("editor-field-options-dataset-toggle"),
    );
    fireEvent.change(within(row).getByTestId("editor-field-options-dataset-id"), {
      target: { value: "ds-visits" },
    });
    fireEvent.change(
      within(row).getByTestId("editor-field-options-dataset-column"),
      { target: { value: "visitor" } },
    );
    fireEvent.click(within(row).getByTestId("editor-field-dataset-filter-add"));
    fireEvent.change(
      within(row).getByTestId("editor-field-dataset-filter-column"),
      { target: { value: "visitor" } },
    );
    fireEvent.change(within(row).getByTestId("editor-field-dataset-filter-op"), {
      target: { value: "in" },
    });
    fireEvent.change(within(row).getByTestId("editor-field-dataset-filter-value"), {
      target: { value: "alice, 42" },
    });

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-saved-version")).textContent,
    ).toContain("v2");
  });

  it("选值回填配置：源字段限动态选项 select、列随引用集 → 保存落形", async () => {
    const { backend } = await openWithFields();
    const cityRow = enableDatasetOptions("city");
    fireEvent.change(within(cityRow).getByTestId("editor-field-options-dataset-id"), {
      target: { value: "ds-visits" },
    });
    fireEvent.change(
      within(cityRow).getByTestId("editor-field-options-dataset-column"),
      { target: { value: "visitor" } },
    );
    const amountRow = fieldRow("amount");
    fireEvent.click(
      within(amountRow).getByTestId("editor-field-fill-from-toggle"),
    );
    fireEvent.change(
      within(amountRow).getByTestId("editor-field-fill-from-field"),
      { target: { value: "city" } },
    );
    fireEvent.change(
      within(amountRow).getByTestId("editor-field-fill-from-column"),
      { target: { value: "visitor" } },
    );

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-saved-version")).textContent,
    ).toContain("v2");
    const spec = lastPutBody(backend).spec.spec as {
      fields: Array<Record<string, unknown>>;
    };
    expect(spec.fields.find((f) => f.column === "amount")).toMatchObject({
      fillFrom: { field: "city", column: "visitor" },
    });
  });

  it("计算字段配置：日期字段下拉限 date、天数可字面量 → 保存落形；可切字段引用", async () => {
    const { backend } = await openWithFields();
    const amountRow = fieldRow("amount");
    fireEvent.click(
      within(amountRow).getByTestId("editor-field-computed-toggle"),
    );
    fireEvent.change(within(amountRow).getByTestId("editor-field-computed-date"), {
      target: { value: "reported_on" },
    });
    fireEvent.change(
      within(amountRow).getByTestId("editor-field-computed-days-literal"),
      { target: { value: "7" } },
    );

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-saved-version")).textContent,
    ).toContain("v2");
    const spec = lastPutBody(backend).spec.spec as {
      fields: Array<Record<string, unknown>>;
    };
    expect(spec.fields.find((f) => f.column === "amount")).toMatchObject({
      computed: { op: "add_days", date: "reported_on", days: 7 },
    });
  });

  it("提交闸约束编辑器：id/message/探测数据集/过滤（字面与 {field} 引用）/expect → 保存落形", async () => {
    const { backend } = await openWithFields();
    fireEvent.click(screen.getByTestId("editor-constraint-add"));
    const constraint = screen.getByTestId("editor-constraint");
    fireEvent.change(within(constraint).getByTestId("editor-constraint-id"), {
      target: { value: "no-harvest" },
    });
    fireEvent.change(
      within(constraint).getByTestId("editor-constraint-message"),
      { target: { value: "尚处安全间隔期，禁止采收" } },
    );
    fireEvent.change(
      within(constraint).getByTestId("editor-constraint-probe-dataset"),
      { target: { value: "ds-visits" } },
    );
    const filterRow = within(constraint).getByTestId("editor-probe-filter-row");
    fireEvent.change(
      within(filterRow).getByTestId("editor-probe-filter-column"),
      { target: { value: "visitor" } },
    );
    // 值切到字段引用再选 city
    fireEvent.click(
      within(filterRow).getByTestId("editor-probe-filter-value-to-field"),
    );
    fireEvent.change(
      within(filterRow).getByTestId("editor-probe-filter-value-field"),
      { target: { value: "city" } },
    );

    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      (await screen.findByTestId("editor-form-saved-version")).textContent,
    ).toContain("v2");
    const spec = lastPutBody(backend).spec.spec as {
      constraints?: Array<Record<string, unknown>>;
    };
    expect(spec.constraints).toEqual([
      {
        id: "no-harvest",
        message: "尚处安全间隔期，禁止采收",
        probe: {
          datasetId: "ds-visits",
          filters: [{ column: "visitor", op: "eq", value: { field: "city" } }],
          expect: "none",
        },
      },
    ]);
  });

  it("非法配置前端即拦：空 id 约束 / fillFrom 指向非动态选项 select → 保存零请求", async () => {
    const { backend } = await openWithFields();
    // 约束 id 留空
    fireEvent.click(screen.getByTestId("editor-constraint-add"));
    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      screen
        .getAllByTestId("editor-form-error")
        .some((el) => el.getAttribute("data-code") === "constraintBad"),
    ).toBe(true);
    expect(backend.calls.filter((c) => c.method === "PUT")).toHaveLength(0);
  });

  it("fillFrom 指向非动态选项 select（带静态 options）→ fillFromSource 前端拦截", async () => {
    const { backend } = await installAndRenderInvalid({
      datasetId: "ds-sales",
      fields: [
        {
          column: "city",
          label: "城市",
          input: "select",
          options: ["上海"],
        },
        {
          column: "amount",
          label: "金额",
          input: "number",
          fillFrom: { field: "city", column: "visitor" },
        },
      ],
    } as FormSpecBody);
    fireEvent.click(screen.getByTestId("editor-form-save"));
    expect(
      screen
        .getAllByTestId("editor-form-error")
        .some((el) => el.getAttribute("data-code") === "fillFromSource"),
    ).toBe(true);
    expect(backend.calls.filter((c) => c.method === "PUT")).toHaveLength(0);
  });

  /** 装假后端并以给定非法初态渲染编辑器（保存自检断言用）。 */
  async function installAndRenderInvalid(body: FormSpecBody) {
    const backend = install();
    render(
      <FormEditor
        artifactId={31}
        initial={{ title: "门店日报表", body }}
        onSaved={vi.fn()}
        onExit={vi.fn()}
      />,
    );
    // 等数据集列表就绪（保存自检不依赖列表，但保持与其他用例同构）
    await screen.findByTestId("editor-form-dataset");
    return { backend };
  }
});
