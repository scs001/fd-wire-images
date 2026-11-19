// 表单运行时组件测试（wire-form-builder 任务 4.2）：
// console 态提交走 corrections 端点（op=insert 归一后整行 values、带登录
// Bearer、成功显示 successMessage/缺省文案并清空重填）；必填/类型前端先拦
// （逐字段错误、零请求）；未绑数据集诚实态（不可提交）；public 态走公开
// submit 端点（t/v 透传、免登录绝不携带 Authorization、响应 message 呈现、
// 422/429 detail 如实呈现）。
// 显隐联动（wire-form-field-linkage）：条件渲染往返、隐藏即清值、隐藏必填
// 不拦、载荷排除。
// 联动 v1+（wire-form-linkage-v2）：组条件显隐（all/any）+ select 动态选项
// （optionsFrom 级联解析、选项变化清值不进载荷、空选项按隐藏）+ 公开提交
// 防篡改（客户端拦截 + 服务端 422 文案呈现）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { FormRuntime } from "./FormRuntime";
import type { FormSpecBody } from "./formSpec";
import { installConsoleBackend, seedConsoleSession } from "../testing";
import type { ConsoleBackendOptions } from "../testing";

/** 种子数据集（corrections 端点需要数据集在场）。 */
const DATASET = {
  id: "ds-form-1",
  key: "ds_form_1",
  name: "门店日报",
  source_kind: "upload" as const,
  connector_id: null,
  source_ref: null,
  columns: [
    { name: "city", type: "TEXT" },
    { name: "amount", type: "REAL" },
    { name: "reported_on", type: "DATE" },
  ],
  row_count: 3,
  size_bytes: 96,
  sync_cron: null,
  sync_status: "ok" as const,
  paused_reason: null,
  created_at: "2026-10-01T08:00:00",
  updated_at: "2026-10-01T08:00:00",
};

const SPEC: FormSpecBody = {
  datasetId: "ds-form-1",
  fields: [
    { column: "city", label: "城市", input: "text", required: true },
    { column: "amount", label: "金额", input: "number" },
    { column: "reported_on", label: "上报日期", input: "date" },
    {
      column: "level",
      label: "级别",
      input: "select",
      options: ["高", "低"],
    },
  ],
  submitLabel: "上报",
  successMessage: "门店数据已收到",
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

/** 按列名定位字段行。 */
function fieldOf(column: string): HTMLElement {
  return screen
    .getAllByTestId("form-field")
    .find((el) => el.getAttribute("data-column") === column)!;
}

function fill(column: string, value: string) {
  fireEvent.change(within(fieldOf(column)).getByTestId("form-field-input"), {
    target: { value },
  });
}

function install(options: ConsoleBackendOptions = {}) {
  const backend = installConsoleBackend([], { datasets: [DATASET], ...options });
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

/** 找 corrections POST 请求（console 提交载荷断言用）。 */
function findCorrectionPost(backend: ReturnType<typeof install>) {
  return backend.calls.find(
    (c) =>
      c.method === "POST" && c.url.pathname === "/api/v1/datasets/ds-form-1/corrections",
  );
}

type PublicCall = {
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: unknown;
};

/** 打公开端点假 fetch：记录请求（断言无 Authorization），按入参回状态。 */
function stubPublicFetch(status: number, body: unknown): PublicCall[] {
  const calls: PublicCall[] = [];
  vi.stubGlobal(
    "fetch",
    ((input: RequestInfo | URL, init?: RequestInit) => {
      const headers: Record<string, string> = {};
      new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
      calls.push({
        url: String(input),
        method: (init?.method ?? "GET").toUpperCase(),
        headers,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      return Promise.resolve({
        ok: status < 400,
        status,
        json: () => Promise.resolve(body),
      } as unknown as Response);
    }) as unknown as typeof fetch,
  );
  return calls;
}

describe("FormRuntime（console 态：corrections 提交）", () => {
  it("渲染字段（label/必填标/选项）与自定义提交文案", () => {
    install();
    render(<FormRuntime spec={SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />);

    expect(screen.getAllByTestId("form-field").length).toBe(4);
    expect(fieldOf("city").textContent).toContain("城市");
    expect(within(fieldOf("city")).getByTestId("form-field-required")).toBeTruthy();
    expect(screen.getByTestId("form-submit").textContent).toContain("上报");
    const select = within(fieldOf("level")).getByTestId(
      "form-field-input",
    ) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["", "高", "低"]);
  });

  it("提交成功：op=insert + 归一后整行 values + 登录 Bearer；successMessage 呈现并清空重填", async () => {
    const backend = install();
    render(<FormRuntime spec={SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />);

    fill("city", " 上海 ");
    fill("amount", "12.5");
    fill("reported_on", "2026-10-12");
    fireEvent.change(
      within(fieldOf("level")).getByTestId("form-field-input"),
      { target: { value: "高" } },
    );
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(
      (await screen.findByTestId("form-success")).textContent,
    ).toBe("门店数据已收到");

    const post = backend.calls.find(
      (c) =>
        c.method === "POST" &&
        c.url.pathname === "/api/v1/datasets/ds-form-1/corrections",
    );
    expect(post).toBeTruthy();
    expect(post?.headers["authorization"]).toMatch(/^Bearer /);
    // 载荷：op=insert + 归一 values（number 数值化、text trim、未填的缺省键不写）
    expect(post?.body).toEqual({
      op: "insert",
      values: {
        city: "上海",
        amount: 12.5,
        reported_on: "2026-10-12",
        level: "高",
      },
    });
    // 修正层落账（假后端真实信封 {correction_id} 已解包）+ 表单清空可续填
    expect(backend.getCorrections("ds-form-1").length).toBe(1);
    expect(
      (within(fieldOf("city")).getByTestId("form-field-input") as HTMLInputElement)
        .value,
    ).toBe("");
  });

  it("必填缺失前端先拦：逐字段错误、零请求", async () => {
    const backend = install();
    render(<FormRuntime spec={SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />);

    fireEvent.click(screen.getByTestId("form-submit"));
    expect(
      within(fieldOf("city")).getByTestId("form-field-error").textContent,
    ).toContain("必填");
    expect(
      backend.calls.filter(
        (c) => c.method === "POST" && c.url.pathname.includes("corrections"),
      ),
    ).toHaveLength(0);

    // 修正后错误随输入清除，再提交成功
    fill("city", "沪");
    expect(within(fieldOf("city")).queryByTestId("form-field-error")).toBeNull();
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(await screen.findByTestId("form-success")).toBeTruthy();
  });

  it("类型错前端先拦：number 非数字 / date 带时间部分 / select 越选项", () => {
    const backend = install();
    render(<FormRuntime spec={SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />);

    fill("city", "沪");
    fill("amount", "abc");
    fill("reported_on", "2026-10-12T09:00");
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(
      within(fieldOf("amount")).getByTestId("form-field-error").textContent,
    ).toContain("数字");
    expect(
      within(fieldOf("reported_on")).getByTestId("form-field-error").textContent,
    ).toContain("YYYY-MM-DD");
    expect(
      backend.calls.filter(
        (c) => c.method === "POST" && c.url.pathname.includes("corrections"),
      ),
    ).toHaveLength(0);
  });

  it("未绑数据集诚实态：不可提交（无提交按钮、无字段）", () => {
    install();
    render(
      <FormRuntime
        spec={{ ...SPEC, datasetId: null }}
        endpoint={{ kind: "console", datasetId: null }}
      />,
    );
    expect(screen.getByTestId("form-runtime-unbound").textContent).toContain(
      "未绑定数据集",
    );
    expect(screen.queryByTestId("form-submit")).toBeNull();
    expect(screen.queryByTestId("form-field")).toBeNull();
  });

  it("服务端拒绝（429 行数闸等）：detail 如实呈现，表单不清空", async () => {
    // corrections 端点强制失败态：包装假后端拦截 POST corrections。
    const backend = install();
    const original = backend.fetchImpl;
    vi.stubGlobal(
      "fetch",
      ((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://console.test");
        if (
          url.pathname === "/api/v1/datasets/ds-form-1/corrections" &&
          (init?.method ?? "GET").toUpperCase() === "POST"
        ) {
          return Promise.resolve({
            ok: false,
            status: 429,
            json: () =>
              Promise.resolve({
                detail: "免费档单数据集行数已达上限（500 行）",
              }),
          });
        }
        return original(input, init);
      }) as unknown as typeof fetch,
    );

    render(<FormRuntime spec={SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />);
    fill("city", "沪");
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(
      (await screen.findByTestId("form-submit-error")).textContent,
    ).toContain("行数已达上限");
    expect(
      (within(fieldOf("city")).getByTestId("form-field-input") as HTMLInputElement)
        .value,
    ).toBe("沪");
  });
});

describe("FormRuntime（public 态：公开 submit 端点）", () => {
  const SUBMIT_URL = "/api/v1/share/form/9/submit?t=tok-abc&v=2";

  it("公开提交成功：POST 分享端点（t/v 透传、免登录）+ 响应 message 呈现", async () => {
    const calls = stubPublicFetch(201, {
      ok: true,
      message: "感谢上报，门店数据已收到",
    });
    render(<FormRuntime spec={SPEC} endpoint={{ kind: "public", url: SUBMIT_URL }} />);

    fill("city", "京");
    fill("amount", "8");
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(
      (await screen.findByTestId("form-success")).textContent,
    ).toBe("感谢上报，门店数据已收到");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(SUBMIT_URL);
    expect(calls[0]?.method).toBe("POST");
    // 公开红线：绝不携带登录头
    expect(calls[0]?.headers["authorization"]).toBeUndefined();
    expect(calls[0]?.body).toEqual({
      values: { city: "京", amount: 8 },
    });
    // 提交后清空，可继续填写（周期性上报）
    expect(
      (within(fieldOf("city")).getByTestId("form-field-input") as HTMLInputElement)
        .value,
    ).toBe("");
  });

  it("公开提交 422（字段级原因）：detail 如实呈现、零清空", async () => {
    stubPublicFetch(422, { detail: "字段 amount：请输入数字" });
    render(<FormRuntime spec={SPEC} endpoint={{ kind: "public", url: SUBMIT_URL }} />);

    fill("city", "京");
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(
      (await screen.findByTestId("form-submit-error")).textContent,
    ).toContain("字段 amount");
    expect(
      (within(fieldOf("city")).getByTestId("form-field-input") as HTMLInputElement)
        .value,
    ).toBe("京");
  });

  it("公开提交缺省文案：spec.successMessage 兜底（响应无 message 时）", async () => {
    stubPublicFetch(201, { ok: true });
    render(
      <FormRuntime
        spec={{ ...SPEC, submitLabel: undefined, successMessage: undefined }}
        endpoint={{ kind: "public", url: SUBMIT_URL }}
      />,
    );
    // 自定义 submitLabel 缺省回落「提交」
    expect(screen.getByTestId("form-submit").textContent).toContain("提交");
    fill("city", "京");
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(
      (await screen.findByTestId("form-success")).textContent,
    ).toContain("已提交");
  });

  it("未绑数据集在 public 态同样落诚实态（不可提交）", () => {
    stubPublicFetch(200, {});
    render(
      <FormRuntime
        spec={{ ...SPEC, datasetId: null }}
        endpoint={{ kind: "public", url: SUBMIT_URL }}
      />,
    );
    expect(screen.getByTestId("form-runtime-unbound")).toBeTruthy();
    expect(screen.queryByTestId("form-submit")).toBeNull();
  });

  it("公开态显隐联动（wire-form-field-linkage）：隐藏字段不渲染、载荷排除", async () => {
    const calls = stubPublicFetch(201, { ok: true, message: "已收到" });
    render(
      <FormRuntime
        spec={{
          datasetId: "ds-form-1",
          fields: [
            { column: "city", label: "城市", input: "text", required: true },
            {
              column: "detail",
              label: "明细",
              input: "text",
              visibleWhen: { field: "city", op: "contains", value: "京" },
            },
          ],
        }}
        endpoint={{ kind: "public", url: SUBMIT_URL }}
      />,
    );

    // 控制字段空 → contains 条件假 → 明细隐藏（连 label 都不出）
    expect(
      screen
        .getAllByTestId("form-field")
        .some((el) => el.getAttribute("data-column") === "detail"),
    ).toBe(false);

    fill("city", "京");
    expect(
      screen
        .getAllByTestId("form-field")
        .some((el) => el.getAttribute("data-column") === "detail"),
    ).toBe(true);

    fireEvent.click(screen.getByTestId("form-submit"));
    expect(await screen.findByTestId("form-success")).toBeTruthy();
    // 载荷不含隐藏字段键（此处 detail 可见未填 → 非必填缺省不写键）
    expect(calls[0]?.body).toEqual({ values: { city: "京" } });
  });
});

// ------------------------------------- 显隐联动（wire-form-field-linkage）

describe("FormRuntime（显隐联动：条件渲染+隐藏即清+载荷排除）", () => {
  const LINKAGE_SPEC: FormSpecBody = {
    datasetId: "ds-form-1",
    fields: [
      { column: "city", label: "城市", input: "text", required: true },
      {
        column: "type",
        label: "类型",
        input: "select",
        options: ["其他", "标准"],
      },
      {
        column: "detail",
        label: "明细",
        input: "text",
        required: true, // 隐藏必填不拦——只在可见时强制
        visibleWhen: { field: "type", op: "eq", value: "其他" },
      },
    ],
  };

  function selectType(value: string) {
    fireEvent.change(within(fieldOf("type")).getByTestId("form-field-input"), {
      target: { value },
    });
  }

  function detailRow(): HTMLElement | undefined {
    return screen
      .getAllByTestId("form-field")
      .find((el) => el.getAttribute("data-column") === "detail");
  }

  it("显隐往返：选「其他」→明细出现；改选「标准」→消失且值被清；再选回从空开始", () => {
    install();
    render(
      <FormRuntime
        spec={LINKAGE_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    // 初态：控制字段空值 → 带值算子条件假 → 明细隐藏（连 label 都不出）
    expect(screen.getAllByTestId("form-field").length).toBe(2);
    expect(detailRow()).toBeUndefined();

    // 选「其他」→ 条件命中 → 明细出现（必填标随行）并可填
    selectType("其他");
    const detail = detailRow()!;
    expect(detail.textContent).toContain("明细");
    expect(within(detail).getByTestId("form-field-required")).toBeTruthy();
    const input = within(detail).getByTestId("form-field-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "备注X" } });
    expect(input.value).toBe("备注X");

    // 改选「标准」→ 明细消失且 state 值被清（隐藏即清）
    selectType("标准");
    expect(detailRow()).toBeUndefined();

    // 再选回「其他」→ 重新可见从空开始
    selectType("其他");
    expect(
      (within(detailRow()!).getByTestId("form-field-input") as HTMLInputElement)
        .value,
    ).toBe("");
  });

  it("隐藏必填不拦提交；提交载荷不含隐藏字段键（断言 fetch body）", async () => {
    const backend = install();
    render(
      <FormRuntime
        spec={LINKAGE_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    fill("city", "沪");
    selectType("标准"); // 明细隐藏（必填不查、键不落）
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(await screen.findByTestId("form-success")).toBeTruthy();
    const post = backend.calls.find(
      (c) =>
        c.method === "POST" &&
        c.url.pathname === "/api/v1/datasets/ds-form-1/corrections",
    );
    expect(post?.body).toEqual({
      op: "insert",
      values: { city: "沪", type: "标准" }, // detail 键不落
    });
    expect(JSON.stringify(post?.body).includes("detail")).toBe(false);
  });
});

// --------------------------------- 条件组合与选项联动（wire-form-linkage-v2）

describe("FormRuntime（条件组合显隐：all/any 级联+清值+载荷排除）", () => {
  const GROUP_SPEC: FormSpecBody = {
    datasetId: "ds-form-1",
    fields: [
      { column: "city", label: "城市", input: "text", required: true },
      { column: "kind", label: "类型", input: "select", options: ["VIP", "普通"] },
      { column: "amount", label: "金额", input: "number" },
      {
        column: "vip_note",
        label: "VIP 备注",
        input: "text",
        required: true, // 隐藏必填不拦——只在可见时强制
        visibleWhenGroup: {
          mode: "all",
          conditions: [
            { field: "kind", op: "eq", value: "VIP" },
            { field: "amount", op: "notEmpty" },
          ],
        },
      },
      {
        column: "any_note",
        label: "任一备注",
        input: "text",
        visibleWhenGroup: {
          mode: "any",
          conditions: [
            { field: "kind", op: "eq", value: "VIP" },
            { field: "amount", op: "gt", value: "100" },
          ],
        },
      },
    ],
  };

  function rowOf(column: string): HTMLElement | undefined {
    return screen
      .getAllByTestId("form-field")
      .find((el) => el.getAttribute("data-column") === column);
  }

  function selectKind(value: string) {
    fireEvent.change(within(fieldOf("kind")).getByTestId("form-field-input"), {
      target: { value },
    });
  }

  it("all 全真才出现、缺一即隐藏且清值；any 一真即出现、全假隐藏", () => {
    install();
    render(
      <FormRuntime
        spec={GROUP_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    // 初态：kind 空（eq 假）+ amount 空（notEmpty 假）→ 两者皆隐藏
    expect(rowOf("vip_note")).toBeUndefined();
    expect(rowOf("any_note")).toBeUndefined();

    // 选 VIP（amount 仍空）：any 出现（一真即可见）、all 仍隐藏
    selectKind("VIP");
    expect(rowOf("any_note")).toBeDefined();
    expect(rowOf("vip_note")).toBeUndefined();

    // 填金额 → all 出现（必填标随行）可填值
    fill("amount", "50");
    const vipNote = rowOf("vip_note")!;
    expect(within(vipNote).getByTestId("form-field-required")).toBeTruthy();
    fireEvent.change(within(vipNote).getByTestId("form-field-input"), {
      target: { value: "备注X" },
    });

    // 清空金额 → all 隐藏且 state 值被清（隐藏即清，同现行纪律）
    fill("amount", "");
    expect(rowOf("vip_note")).toBeUndefined();

    // 切「普通」：any 的 eq 假 + amount 空 → gt 假 → 隐藏
    selectKind("普通");
    expect(rowOf("any_note")).toBeUndefined();
  });

  it("all 组合隐藏时必填不拦提交；载荷不含隐藏字段键", async () => {
    const backend = install();
    render(
      <FormRuntime
        spec={GROUP_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    fill("city", "沪");
    selectKind("普通"); // vip_note（必填）与 any_note 均隐藏
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(await screen.findByTestId("form-success")).toBeTruthy();
    const post = findCorrectionPost(backend);
    expect(post?.body).toEqual({
      op: "insert",
      values: { city: "沪", kind: "普通" },
    });
    expect(JSON.stringify(post?.body).includes("note")).toBe(false);
  });
});

describe("FormRuntime（选项联动：动态选项+清值+空选项隐藏+防篡改）", () => {
  const SUBMIT_URL = "/api/v1/share/form/9/submit?t=tok-abc&v=2";

  const LINKED_SPEC: FormSpecBody = {
    datasetId: "ds-form-1",
    fields: [
      { column: "city", label: "城市", input: "text", required: true },
      { column: "region", label: "大区", input: "select", options: ["华东", "华南"] },
      {
        column: "province_city",
        label: "所在城市",
        input: "select",
        optionsFrom: {
          field: "region",
          cases: { 华东: ["上海", "杭州"], 华南: ["广州", "深圳"] },
          fallback: ["其他地区"],
        },
      },
    ],
  };

  function citySelect(): HTMLSelectElement {
    return within(fieldOf("province_city")).getByTestId(
      "form-field-input",
    ) as HTMLSelectElement;
  }

  function cityOptions(): string[] {
    return [...citySelect().options].map((o) => o.value);
  }

  function selectRegion(value: string) {
    fireEvent.change(within(fieldOf("region")).getByTestId("form-field-input"), {
      target: { value },
    });
  }

  function selectProvinceCity(value: string) {
    fireEvent.change(citySelect(), { target: { value } });
  }

  it("控制值未选 → fallback；选华东 → 华东列表；切华南 → 选项切换且现值立即被清", () => {
    install();
    render(
      <FormRuntime
        spec={LINKED_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    // 初态控制值空 → 解析 fallback
    expect(cityOptions()).toEqual(["", "其他地区"]);
    selectRegion("华东");
    expect(cityOptions()).toEqual(["", "上海", "杭州"]);
    selectProvinceCity("上海");

    // 切华南：上海不在新选项集 → 立即清值（绝不进提交载荷）
    selectRegion("华南");
    expect(cityOptions()).toEqual(["", "广州", "深圳"]);
    expect(citySelect().value).toBe("");
  });

  it("级联提交：载荷的联动值 ∈ 当前解析选项", async () => {
    const backend = install();
    render(
      <FormRuntime
        spec={LINKED_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    fill("city", "沪");
    selectRegion("华东");
    selectProvinceCity("杭州");
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(await screen.findByTestId("form-success")).toBeTruthy();
    expect(findCorrectionPost(backend)?.body).toEqual({
      op: "insert",
      values: { city: "沪", region: "华东", province_city: "杭州" },
    });
  });

  it("空选项按隐藏：无 fallback + 控制值未命中 → 字段不渲染（连 label 都不出）、必填不拦、载荷不含键", async () => {
    const backend = install();
    render(
      <FormRuntime
        spec={{
          datasetId: "ds-form-1",
          fields: [
            { column: "city", label: "城市", input: "text", required: true },
            {
              column: "region",
              label: "大区",
              input: "select",
              options: ["华东", "华南"],
            },
            {
              column: "province_city",
              label: "所在城市",
              input: "select",
              required: true, // 空选项按隐藏 → required 不查
              optionsFrom: { field: "region", cases: { 华东: ["上海"] } }, // 无 fallback
            },
          ],
        }}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    // 初态控制值空 → 解析 [] → 视同隐藏（不渲染）
    expect(screen.getAllByTestId("form-field").length).toBe(2);
    expect(
      screen
        .getAllByTestId("form-field")
        .some((el) => el.getAttribute("data-column") === "province_city"),
    ).toBe(false);

    fill("city", "沪");
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(await screen.findByTestId("form-success")).toBeTruthy();
    const post = findCorrectionPost(backend);
    expect(post?.body).toEqual({ op: "insert", values: { city: "沪" } });
    expect(JSON.stringify(post?.body).includes("province_city")).toBe(false);
  });

  it("防篡改（客户端拦截）：注入 DOM 伪造选项值 → select 错误呈现、零请求", () => {
    const backend = install();
    render(
      <FormRuntime
        spec={LINKED_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );

    selectRegion("华东");
    // 模拟 devtools 篡改：向 select 注入伪造 <option> 再改值——state 持有
    // 不 ∈ 解析后选项的值，提交走 validateFormValues 先拦。
    const bogus = document.createElement("option");
    bogus.value = "伪造城市";
    citySelect().appendChild(bogus);
    fireEvent.change(citySelect(), { target: { value: "伪造城市" } });
    bogus.remove();

    fill("city", "沪");
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(
      within(fieldOf("province_city")).getByTestId("form-field-error")
        .textContent,
    ).toContain("选项");
    expect(
      backend.calls.filter(
        (c) => c.method === "POST" && c.url.pathname.includes("corrections"),
      ),
    ).toHaveLength(0);
  });

  it("公开提交端到端：级联正例载荷 + 服务端 422（动态成员裁决）detail 如实呈现、零清空", async () => {
    const calls = stubPublicFetch(422, {
      detail:
        "字段 所在城市（province_city）必须是选项之一 ['上海', '杭州']（收到 '伪造城市'）",
    });
    render(
      <FormRuntime spec={LINKED_SPEC} endpoint={{ kind: "public", url: SUBMIT_URL }} />,
    );

    // 客户端解析口径下合法（杭州 ∈ 华东列表）→ 请求发出，服务端按 raw
    // values 重解析裁决 422——detail 如实呈现，表单不清空。
    fill("city", "京");
    selectRegion("华东");
    selectProvinceCity("杭州");
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(
      (await screen.findByTestId("form-submit-error")).textContent,
    ).toContain("必须是选项之一");
    expect(calls[0]?.body).toEqual({
      values: { city: "京", region: "华东", province_city: "杭州" },
    });
    expect(
      (within(fieldOf("city")).getByTestId("form-field-input") as HTMLInputElement)
        .value,
    ).toBe("京");
  });
});

// --------------------------------- 高级录入（wire-form-entry-advanced）

describe("FormRuntime（动态选项+选值回填+计算字段：console 全链路）", () => {
  const CATALOG = {
    id: "ds-catalog",
    key: "ds_catalog",
    name: "投入品档案",
    source_kind: "upload" as const,
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "pesticide", type: "TEXT" },
      { name: "interval_days", type: "INTEGER" },
    ],
    row_count: 3,
    size_bytes: 120,
    sync_cron: null,
    sync_status: "ok" as const,
    paused_reason: null,
    created_at: "2026-10-01T08:00:00",
    updated_at: "2026-10-01T08:00:00",
  };

  const ADV_SPEC: FormSpecBody = {
    datasetId: "ds-form-1",
    fields: [
      {
        column: "pesticide",
        label: "农药",
        input: "select",
        optionsFromDataset: { datasetId: "ds-catalog", column: "pesticide" },
      },
      {
        column: "interval_days",
        label: "安全间隔期",
        input: "number",
        fillFrom: { field: "pesticide", column: "interval_days" },
      },
      { column: "record_on", label: "记录日期", input: "date" },
      {
        column: "deadline",
        label: "截止日期",
        input: "date",
        computed: { op: "add_days", date: "record_on", days: "interval_days" },
      },
    ],
  };

  function installAdv() {
    const backend = installConsoleBackend([], {
      datasets: [DATASET, CATALOG],
      datasetRows: {
        "ds-catalog": {
          columns: CATALOG.columns,
          rows: [
            { pesticide: "chlorpyrifos", interval_days: 7 },
            { pesticide: "abamectin", interval_days: 3 },
            // 同名第二行：回填按 limit=1 取首行（3），不受此行影响
            { pesticide: "abamectin", interval_days: 99 },
          ],
        },
      },
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    return backend;
  }

  function pesticideSelect(): HTMLSelectElement {
    return within(fieldOf("pesticide")).getByTestId(
      "form-field-input",
    ) as HTMLSelectElement;
  }

  function inputOf(column: string): HTMLInputElement {
    return within(fieldOf(column)).getByTestId(
      "form-field-input",
    ) as HTMLInputElement;
  }

  function catalogQueryPosts(backend: ReturnType<typeof installAdv>) {
    return backend.calls.filter(
      (c) =>
        c.method === "POST" && c.url.pathname === "/api/v1/datasets/ds-catalog/query",
    );
  }

  it("动态选项解析：会话身份 distinct 查询 → 去重升序选项进下拉", async () => {
    const backend = installAdv();
    render(
      <FormRuntime spec={ADV_SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />,
    );

    await waitFor(() => {
      expect([...pesticideSelect().options].map((o) => o.value)).toEqual([
        "",
        "abamectin",
        "chlorpyrifos",
      ]);
    });
    // 请求形状：distinct_column + limit 缺省 200，会话 Bearer
    const distinct = catalogQueryPosts(backend).find(
      (c) => (c.body as { distinct_column?: string }).distinct_column === "pesticide",
    );
    expect(distinct).toBeTruthy();
    expect(distinct?.headers["authorization"]).toMatch(/^Bearer /);
    expect((distinct?.body as { limit?: number }).limit).toBe(200);
  });

  it("选值回填：选中→等值查 limit=1 行回填；手改保留；改选以新回填覆盖", async () => {
    const backend = installAdv();
    render(
      <FormRuntime spec={ADV_SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />,
    );
    await waitFor(() => {
      expect(pesticideSelect().options.length).toBeGreaterThan(1);
    });
    expect(inputOf("interval_days").value).toBe("");

    fireEvent.change(pesticideSelect(), { target: { value: "abamectin" } });
    await waitFor(() => {
      expect(inputOf("interval_days").value).toBe("3");
    });
    // 回填查询：filters=[选项列 eq 选中值]，limit=1
    const fillQuery = catalogQueryPosts(backend).find(
      (c) =>
        (c.body as { distinct_column?: string }).distinct_column === undefined &&
        (
          (c.body as { filters?: Array<{ value?: unknown }> }).filters ?? []
        )[0]?.value === "abamectin",
    );
    expect(fillQuery).toBeTruthy();
    expect((fillQuery?.body as { limit?: number }).limit).toBe(1);

    // 手改间隔期：源值未变 → 手改保留（不触发回填覆盖）
    fireEvent.change(inputOf("interval_days"), { target: { value: "5" } });
    expect(inputOf("interval_days").value).toBe("5");

    // 改选 chlorpyrifos：以新回填覆盖（=7）
    fireEvent.change(pesticideSelect(), { target: { value: "chlorpyrifos" } });
    await waitFor(() => {
      expect(inputOf("interval_days").value).toBe("7");
    });
  });

  it("计算字段：日期+天数齐 → 自动算出（闰年口径）；手改保留；参与值再变 → 重算覆盖", async () => {
    installAdv();
    render(
      <FormRuntime spec={ADV_SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />,
    );
    await waitFor(() => {
      expect(pesticideSelect().options.length).toBeGreaterThan(1);
    });

    fireEvent.change(pesticideSelect(), { target: { value: "abamectin" } });
    await waitFor(() => {
      expect(inputOf("interval_days").value).toBe("3");
    });
    // 参与值缺一（日期未填）→ 计算字段留空
    expect(inputOf("deadline").value).toBe("");

    // 2024 闰年：02-28 + 3 天 = 03-02
    fireEvent.change(inputOf("record_on"), { target: { value: "2024-02-28" } });
    await waitFor(() => {
      expect(inputOf("deadline").value).toBe("2024-03-02");
    });

    // 手改截止日期：参与值不变 → 手改保留
    fireEvent.change(inputOf("deadline"), { target: { value: "2024-03-10" } });
    expect(inputOf("deadline").value).toBe("2024-03-10");

    // 参与值再变（改选 chlorpyrifos → 间隔 7）：重算覆盖手改（02-28 + 7 = 03-06）
    fireEvent.change(pesticideSelect(), { target: { value: "chlorpyrifos" } });
    await waitFor(() => {
      expect(inputOf("interval_days").value).toBe("7");
    });
    await waitFor(() => {
      expect(inputOf("deadline").value).toBe("2024-03-06");
    });
  });

  it("console 提交携带 artifact_id：endpoint.artifactId → corrections 载荷带字符串 id", async () => {
    const backend = installAdv();
    render(
      <FormRuntime
        spec={ADV_SPEC}
        endpoint={{ kind: "console", datasetId: "ds-form-1", artifactId: 31 }}
      />,
    );
    await waitFor(() => {
      expect(pesticideSelect().options.length).toBeGreaterThan(1);
    });

    fireEvent.change(pesticideSelect(), { target: { value: "abamectin" } });
    await waitFor(() => {
      expect(inputOf("interval_days").value).toBe("3");
    });
    fireEvent.change(inputOf("record_on"), { target: { value: "2026-10-15" } });
    await waitFor(() => {
      expect(inputOf("deadline").value).toBe("2026-10-18");
    });
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(await screen.findByTestId("form-success")).toBeTruthy();
    const post = findCorrectionPost(backend);
    expect(post?.body).toEqual({
      op: "insert",
      values: {
        pesticide: "abamectin",
        interval_days: 3,
        record_on: "2026-10-15",
        deadline: "2026-10-18",
      },
      artifact_id: "31",
    });
  });

  it("截断如实标注：limit=1 → 仅首项 + truncated 提示", async () => {
    installAdv();
    render(
      <FormRuntime
        spec={{
          ...ADV_SPEC,
          fields: ADV_SPEC.fields.map((f) =>
            f.column === "pesticide"
              ? {
                  ...f,
                  optionsFromDataset: {
                    ...f.optionsFromDataset!,
                    limit: 1,
                  },
                }
              : f,
          ),
        }}
        endpoint={{ kind: "console", datasetId: "ds-form-1" }}
      />,
    );
    await waitFor(() => {
      expect([...pesticideSelect().options].map((o) => o.value)).toEqual([
        "",
        "abamectin",
      ]);
    });
    expect(
      within(fieldOf("pesticide")).getByTestId("form-field-options-truncated")
        .textContent,
    ).toContain("1");
  });

  it("引用失效如实：distinct 查询 4xx → 选项区可读错误（不静默空白）", async () => {
    const backend = installAdv();
    const original = backend.fetchImpl;
    vi.stubGlobal(
      "fetch",
      ((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://console.test");
        if (
          url.pathname === "/api/v1/datasets/ds-catalog/query" &&
          (init?.body as string | undefined)?.includes("distinct_column")
        ) {
          return Promise.resolve({
            ok: false,
            status: 400,
            json: () =>
              Promise.resolve({ detail: "列 name 不在数据集列快照中" }),
          });
        }
        return original(input, init);
      }) as unknown as typeof fetch,
    );
    render(
      <FormRuntime spec={ADV_SPEC} endpoint={{ kind: "console", datasetId: "ds-form-1" }} />,
    );
    expect(
      (await screen.findByTestId("form-field-options-error")).textContent,
    ).toContain("列 name 不在数据集列快照中");
  });
});

describe("FormRuntime（公开选项端点：动态选项+fills 回填+免登录）", () => {
  const OPTIONS_URL = "/api/v1/share/form/9/options?t=tok-abc&v=2";
  const SUBMIT_URL = "/api/v1/share/form/9/submit?t=tok-abc&v=2";

  const OPTIONS_PAYLOAD = {
    fields: {
      pesticide: {
        options: ["abamectin", "chlorpyrifos"],
        truncated: false,
        fills: {
          interval_days: { abamectin: "3", chlorpyrifos: "7" },
        },
      },
    },
  };

  const PUBLIC_SPEC: FormSpecBody = {
    datasetId: "ds-form-1",
    fields: [
      {
        column: "pesticide",
        label: "农药",
        input: "select",
        optionsFromDataset: { datasetId: "ds-catalog", column: "pesticide" },
      },
      {
        column: "interval_days",
        label: "安全间隔期",
        input: "number",
        fillFrom: { field: "pesticide", column: "interval_days" },
      },
    ],
  };

  /** 分 URL 打公开端点假 fetch：GET options / POST submit 各自状态。 */
  function stubShareFetch(options: {
    optionsStatus?: number;
    optionsBody?: unknown;
    submitStatus?: number;
    submitBody?: unknown;
  }): PublicCall[] {
    const calls: PublicCall[] = [];
    vi.stubGlobal(
      "fetch",
      ((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://share.test");
        const headers: Record<string, string> = {};
        new Headers(init?.headers as HeadersInit | undefined).forEach((v, k) => {
          headers[k.toLowerCase()] = v;
        });
        calls.push({
          url: String(input),
          method: (init?.method ?? "GET").toUpperCase(),
          headers,
          body:
            typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
        });
        if (url.pathname === "/api/v1/share/form/9/options") {
          const status = options.optionsStatus ?? 200;
          return Promise.resolve({
            ok: status < 400,
            status,
            json: () =>
              Promise.resolve(options.optionsBody ?? OPTIONS_PAYLOAD),
          } as unknown as Response);
        }
        const status = options.submitStatus ?? 201;
        return Promise.resolve({
          ok: status < 400,
          status,
          json: () =>
            Promise.resolve(
              options.submitBody ?? { ok: true, message: "已收到" },
            ),
        } as unknown as Response);
      }) as unknown as typeof fetch,
    );
    return calls;
  }

  function pesticideSelect(): HTMLSelectElement {
    return within(fieldOf("pesticide")).getByTestId(
      "form-field-input",
    ) as HTMLSelectElement;
  }

  it("选项端点解析：GET 免登录（无 Authorization）→ 选项进下拉；fills 回填免请求", async () => {
    const calls = stubShareFetch({});
    render(
      <FormRuntime
        spec={PUBLIC_SPEC}
        endpoint={{ kind: "public", url: SUBMIT_URL, optionsUrl: OPTIONS_URL }}
      />,
    );

    await waitFor(() => {
      expect([...pesticideSelect().options].map((o) => o.value)).toEqual([
        "",
        "abamectin",
        "chlorpyrifos",
      ]);
    });
    // 选项端点只 GET 一次、绝不携带登录头
    const optionCalls = calls.filter((c) => c.url.includes("/options"));
    expect(optionCalls).toHaveLength(1);
    expect(optionCalls[0]?.method).toBe("GET");
    expect(optionCalls[0]?.headers["authorization"]).toBeUndefined();

    // 选值回填走 fills 映射（零额外请求）
    fireEvent.change(pesticideSelect(), { target: { value: "chlorpyrifos" } });
    await waitFor(() => {
      expect(
        (within(fieldOf("interval_days")).getByTestId("form-field-input") as HTMLInputElement)
          .value,
      ).toBe("7");
    });
    expect(
      calls.filter((c) => c.url.includes("/options")).length,
    ).toBe(1);
  });

  it("提交闸 422：constraint message 逐字呈现、零清空", async () => {
    stubShareFetch({
      submitStatus: 422,
      submitBody: {
        detail: "提交闸 no-harvest：尚处安全间隔期，禁止采收",
      },
    });
    render(
      <FormRuntime
        spec={PUBLIC_SPEC}
        endpoint={{ kind: "public", url: SUBMIT_URL, optionsUrl: OPTIONS_URL }}
      />,
    );
    await waitFor(() => {
      expect(pesticideSelect().options.length).toBeGreaterThan(1);
    });

    fireEvent.change(pesticideSelect(), { target: { value: "abamectin" } });
    fireEvent.click(screen.getByTestId("form-submit"));

    expect(
      (await screen.findByTestId("form-submit-error")).textContent,
    ).toContain("提交闸 no-harvest：尚处安全间隔期，禁止采收");
    expect(pesticideSelect().value).toBe("abamectin");
  });

  it("选项端点失效错误如实呈现（error 字段 → 可读标注，不静默空白）", async () => {
    stubShareFetch({
      optionsBody: {
        fields: {
          pesticide: {
            options: [],
            truncated: false,
            error: "引用数据集已删除",
          },
        },
      },
    });
    render(
      <FormRuntime
        spec={PUBLIC_SPEC}
        endpoint={{ kind: "public", url: SUBMIT_URL, optionsUrl: OPTIONS_URL }}
      />,
    );
    expect(
      (await screen.findByTestId("form-field-options-error")).textContent,
    ).toContain("引用数据集已删除");
  });
});
