// buildPanelQuery / formFromPanelQuery 纯函数单测（wire-panel-query-binding
// 任务 2.2）：表单宽进（未完成行/字符串 limit）→ 契约 raw query 窄出，
// 与服务端 parse_panel_query 同形同界；回填（数组值 join、数值 String 化）。
import { describe, expect, it } from "vitest";
import {
  buildPanelQuery,
  emptyQueryBindingForm,
  formFromPanelQuery,
  type QueryBindingFormState,
} from "./queryBinding";
import type { DashboardPanelQuery } from "./spec";

const DATASET_COLUMNS = ["date", "city", "price"];

function form(partial: Partial<QueryBindingFormState>): QueryBindingFormState {
  return { ...emptyQueryBindingForm(), ...partial };
}

describe("buildPanelQuery（表单 → 契约 query）", () => {
  it("未选数据集 → undefined（未绑定语义）", () => {
    expect(buildPanelQuery(form({ datasetId: "" }))).toBeUndefined();
    expect(buildPanelQuery(form({ datasetId: "  " }), DATASET_COLUMNS)).toBeUndefined();
  });

  it("最小合法：仅 dataset_id，其余字段省略", () => {
    expect(buildPanelQuery(form({ datasetId: "ds-1" }), DATASET_COLUMNS)).toEqual({
      dataset_id: "ds-1",
    });
  });

  it("投影列按数据集列序重排（勾选顺序无关）", () => {
    const query = buildPanelQuery(
      form({ datasetId: "ds-1", columns: ["price", "date"] }),
      DATASET_COLUMNS,
    );
    expect(query?.columns).toEqual(["date", "price"]); // date 在前=数据集列序
  });

  it("投影列剔除不在数据集里的勾选（勾选漂移防护）；全不勾=省略字段", () => {
    const query = buildPanelQuery(
      form({ datasetId: "ds-1", columns: ["price", "ghost"] }),
      DATASET_COLUMNS,
    );
    expect(query?.columns).toEqual(["price"]);
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", columns: [] }), DATASET_COLUMNS)
        ?.columns,
    ).toBeUndefined();
  });

  it("无数据集列信息时按勾选原序（回填后未加载列表的中间态）", () => {
    const query = buildPanelQuery(
      form({ datasetId: "ds-1", columns: ["price", "date"] }),
      undefined,
    );
    expect(query?.columns).toEqual(["price", "date"]);
  });

  it("过滤行：空列/空值行静默丢弃；数字样式转 number；值原样透传字符串", () => {
    const query = buildPanelQuery(
      form({
        datasetId: "ds-1",
        filters: [
          { column: "city", op: "eq", value: "秦皇岛" },
          { column: "", op: "eq", value: "x" }, // 未完成：空列 → 丢弃
          { column: "price", op: "eq", value: "" }, // 未完成：空值 → 丢弃
          { column: "price", op: "gte", value: "850" }, // 完整数字 → number
          { column: "city", op: "contains", value: "秦" },
        ],
      }),
      DATASET_COLUMNS,
    );
    expect(query?.filters).toEqual([
      { column: "city", op: "eq", value: "秦皇岛" },
      { column: "price", op: "gte", value: 850 },
      { column: "city", op: "contains", value: "秦" },
    ]);
  });

  it("in 算子：中英文逗号切分多值、跳过空段、逐项标量化", () => {
    const query = buildPanelQuery(
      form({
        datasetId: "ds-1",
        filters: [
          { column: "city", op: "in", value: "秦皇岛, 上海，太原 , " },
        ],
      }),
      DATASET_COLUMNS,
    );
    expect(query?.filters).toEqual([
      { column: "city", op: "in", value: ["秦皇岛", "上海", "太原"] },
    ]);
  });

  it("输入中的未完成数字（3. 尾点）保字符串——不打断受控输入", () => {
    const query = buildPanelQuery(
      form({ datasetId: "ds-1", filters: [{ column: "price", op: "eq", value: "3." }] }),
      DATASET_COLUMNS,
    );
    expect(query?.filters).toEqual([{ column: "price", op: "eq", value: "3." }]);
  });

  it("排序：空列行丢弃、dir 透传 asc/desc；空 → 省略字段", () => {
    const query = buildPanelQuery(
      form({
        datasetId: "ds-1",
        sort: [
          { column: "date", dir: "desc" },
          { column: "", dir: "asc" },
        ],
      }),
      DATASET_COLUMNS,
    );
    expect(query?.sort).toEqual([{ column: "date", dir: "desc" }]);
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", sort: [] }), DATASET_COLUMNS)?.sort,
    ).toBeUndefined();
  });

  it("limit：1..1000 整数才落字段；空/非整数/越界一律省略（服务端缺省 100）", () => {
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", limit: "1" }), DATASET_COLUMNS)?.limit,
    ).toBe(1);
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", limit: "1000" }), DATASET_COLUMNS)?.limit,
    ).toBe(1000);
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", limit: "" }), DATASET_COLUMNS)?.limit,
    ).toBeUndefined();
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", limit: "12.5" }), DATASET_COLUMNS)?.limit,
    ).toBeUndefined();
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", limit: "0" }), DATASET_COLUMNS)?.limit,
    ).toBeUndefined();
    expect(
      buildPanelQuery(form({ datasetId: "ds-1", limit: "1001" }), DATASET_COLUMNS)?.limit,
    ).toBeUndefined();
  });

  it("全件组合：dataset+filters+sort+limit+columns 同落（服务端同形）", () => {
    const query = buildPanelQuery(
      form({
        datasetId: " ds-1 ",
        columns: ["price"],
        filters: [{ column: "city", op: "eq", value: "秦皇岛" }],
        sort: [{ column: "date", dir: "desc" }],
        limit: "50",
      }),
      DATASET_COLUMNS,
    );
    expect(query).toEqual({
      dataset_id: "ds-1",
      filters: [{ column: "city", op: "eq", value: "秦皇岛" }],
      sort: [{ column: "date", dir: "desc" }],
      limit: 50,
      columns: ["price"],
    });
  });

  it("纯函数不可变：入参 form 不被修改", () => {
    const input = form({
      datasetId: "ds-1",
      columns: ["date", "price"],
      filters: [{ column: "city", op: "in", value: "a,b" }],
    });
    const snapshot = JSON.parse(JSON.stringify(input));
    buildPanelQuery(input, DATASET_COLUMNS);
    expect(input).toEqual(snapshot);
  });
});

describe("formFromPanelQuery（query → 表单回填）", () => {
  it("undefined → 空表单", () => {
    expect(formFromPanelQuery(undefined)).toEqual(emptyQueryBindingForm());
  });

  it("全件回填：filters 值字符串化 / in 数组 join / limit String 化", () => {
    const query: DashboardPanelQuery = {
      dataset_id: "ds_coal_price",
      filters: [
        { column: "city", op: "eq", value: "秦皇岛" },
        { column: "price", op: "gte", value: 850 },
        { column: "city", op: "in", value: ["秦皇岛", "上海"] },
        { column: "note", op: "eq", value: null },
      ],
      sort: [{ column: "date", dir: "desc" }],
      limit: 1,
      columns: ["date", "price"],
    };
    expect(formFromPanelQuery(query)).toEqual({
      datasetId: "ds_coal_price",
      columns: ["date", "price"],
      filters: [
        { column: "city", op: "eq", value: "秦皇岛" },
        { column: "price", op: "gte", value: "850" },
        { column: "city", op: "in", value: "秦皇岛,上海" },
        { column: "note", op: "eq", value: "" },
      ],
      sort: [{ column: "date", dir: "desc" }],
      limit: "1",
    });
  });

  it("回填 → build 往返：in 数组 join 后再 build 还原数组（数字样式回 number）", () => {
    const original: DashboardPanelQuery = {
      dataset_id: "ds-1",
      filters: [{ column: "city", op: "in", value: ["秦皇岛", "上海"] }],
      limit: 20,
    };
    const roundtrip = buildPanelQuery(formFromPanelQuery(original), DATASET_COLUMNS);
    expect(roundtrip).toEqual(original);
  });
});
