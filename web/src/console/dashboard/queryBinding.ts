// 面板查询绑定表单 ⇄ 契约 query 的纯转换（wire-panel-query-binding 任务 2.1）。
//
// 编辑器「绑定查询」段受控状态是宽进窄出的两段式：
// - 表单态（QueryBindingFormState）：全部字符串输入（受控 input/select 友好），
//   允许中间未完成态（空过滤行、未选数据集）；
// - buildPanelQuery：表单态 → 契约 raw query（DashboardPanelQuery 同形）——
//   未完成行静默丢弃（空列/空值的过滤、空列的排序）、limit 仅 1..1000 整数
//   落字段、投影列按数据集列序重排（=刷新后快照列序）；未选数据集 → undefined
//   （面板无 query，等价未绑定）。
// - formFromPanelQuery：已有 query 回填表单（数组值 join 逗号、数值 String 化）。
// 本模块零 React 依赖（可测纯函数），组件侧见 PanelQueryBinding.tsx。
import type {
  DashboardPanelQuery,
  DashboardQueryFilterOp,
} from "./spec";

/** 绑定表单可用的过滤算子（=契约白名单 spec.ts DASHBOARD_QUERY_FILTER_OPS，
 *  单独导出供下拉渲染；类型层面仍以契约类型为准）。 */
export const QUERY_BINDING_FILTER_OPS: readonly DashboardQueryFilterOp[] = [
  "eq",
  "in",
  "gt",
  "gte",
  "lt",
  "lte",
  "contains",
  "startswith",
];

/** 过滤行表单态：值一律字符串输入（build 时按算子收窄）。 */
export interface QueryFilterFormRow {
  column: string;
  op: DashboardQueryFilterOp;
  value: string;
}

/** 排序行表单态（dir 受 select 约束恒为 asc/desc）。 */
export interface QuerySortFormRow {
  column: string;
  dir: "asc" | "desc";
}

/** 「绑定查询」段受控表单态。 */
export interface QueryBindingFormState {
  /** 目标数据集 id（"" = 未选择 → buildPanelQuery 返回 undefined）。 */
  datasetId: string;
  /** 勾选的投影列（选择集合语义；输出按数据集列序重排）。 */
  columns: string[];
  filters: QueryFilterFormRow[];
  sort: QuerySortFormRow[];
  /** 行数上限（字符串输入；仅 1..1000 整数落字段，否则省略=服务端缺省 100）。 */
  limit: string;
}

/** 空表单（「绑定」入口打开时的起始态）。 */
export function emptyQueryBindingForm(): QueryBindingFormState {
  return { datasetId: "", columns: [], filters: [], sort: [], limit: "" };
}

/** 值输入 → 契约标量：完整数字样式转 number（与编辑器 coerceCell 同口径，
 *  输入中的 "3." 等未完成态保字符串）；空串保空串（由行级过滤丢弃）。 */
function coerceScalar(text: string): string | number {
  const n = Number(text);
  if (text !== "" && Number.isFinite(n) && !text.endsWith(".")) return n;
  return text;
}

/** 值输入 → 契约值：in 算子按中英文逗号切分多值（逐项标量化、跳过空段），
 *  其余算子整值标量化。 */
function coerceFilterValue(text: string, op: DashboardQueryFilterOp): unknown {
  if (op === "in") {
    return text
      .split(/[,，]/)
      .map((part) => part.trim())
      .filter((part) => part !== "")
      .map(coerceScalar);
  }
  return coerceScalar(text);
}

/**
 * 表单态 → 契约 raw query（与服务端 parse_panel_query 同形同界）：
 * - datasetId 空白 → undefined（未绑定）；
 * - 投影列 = 勾选列 ∩ datasetColumns（按数据集列序重排；datasetColumns 缺省
 *   时按勾选原序）；勾选为空 → 省略 columns 字段（缺省=全列）；
 * - 过滤行仅保留列与值均非空白的行（未完成行静默丢弃）；in 多值切分、
 *   完整数字样式转 number；归一后为空 → 省略 filters 字段；
 * - 排序行仅保留列非空白的行；为空 → 省略 sort 字段；
 * - limit 仅 1..1000 整数落字段，其余（空/非整数/越界）省略（服务端缺省 100）。
 * 纯函数不可变：入参 form 不被修改。
 */
export function buildPanelQuery(
  form: QueryBindingFormState,
  datasetColumns?: readonly string[],
): DashboardPanelQuery | undefined {
  const datasetId = form.datasetId.trim();
  if (!datasetId) return undefined;

  let columns: string[] = [];
  if (form.columns.length > 0) {
    columns = datasetColumns
      ? datasetColumns.filter((name) => form.columns.includes(name))
      : form.columns.filter((name) => name !== "");
  }

  const filters = form.filters
    .filter((row) => row.column.trim() !== "" && row.value.trim() !== "")
    .map((row) => ({
      column: row.column,
      op: row.op,
      value: coerceFilterValue(row.value, row.op),
    }));

  const sort = form.sort
    .filter((row) => row.column.trim() !== "")
    .map((row) => ({ column: row.column, dir: row.dir }));

  const limitText = form.limit.trim();
  const limitNumber = Number(limitText);
  const limit =
    limitText !== "" &&
    Number.isInteger(limitNumber) &&
    limitNumber >= 1 &&
    limitNumber <= 1000
      ? limitNumber
      : undefined;

  return {
    dataset_id: datasetId,
    ...(filters.length > 0 ? { filters } : {}),
    ...(sort.length > 0 ? { sort } : {}),
    ...(limit !== undefined ? { limit } : {}),
    ...(columns.length > 0 ? { columns } : {}),
  };
}

/**
 * 已有契约 query → 表单态回填（编辑器打开已绑定面板时）：
 * - filters 值反向展开：数组 join 中英文逗号均可读回（in 算子），null → ""；
 * - limit 数值 String 化（受控数字框仍是字符串态）；
 * - columns 原样进勾选集合（渲染时与数据集列求交决定勾选态）。
 * query 为 undefined → 空表单。
 */
export function formFromPanelQuery(
  query: DashboardPanelQuery | undefined,
): QueryBindingFormState {
  if (!query) return emptyQueryBindingForm();
  return {
    datasetId: query.dataset_id,
    columns: query.columns ? [...query.columns] : [],
    filters: (query.filters ?? []).map((filter) => ({
      column: filter.column,
      op: filter.op,
      value:
        filter.value == null
          ? ""
          : Array.isArray(filter.value)
            ? filter.value.map((item) => String(item)).join(",")
            : String(filter.value),
    })),
    sort: (query.sort ?? []).map((item) => ({
      column: item.column,
      dir: item.dir,
    })),
    limit: query.limit != null ? String(query.limit) : "",
  };
}
