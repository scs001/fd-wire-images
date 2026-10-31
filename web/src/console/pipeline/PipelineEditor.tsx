// 管线编辑器（wire-transform-pipelines 任务 5.2）：名称/描述/基础数据集 +
// 线性步骤列表（增删改序；三类型参数表单与既有原子操作参数形状一致——
// derive=过滤/排序/限行派生 {filters,sort?,limit}；join=挂接指标
// {indicators,date_column,region_column?,default_entity?,filters?}；
// link=关联数据集 {right_dataset_id,left_keys,right_keys}）。
// 版本语义如实：编辑既有管线时改步骤或基础集 → 保存按钮旁明示
// 「步骤变更将创建新版本 vN+1」（保存后按 version_created 回号刷新徽标）；
// 仅名称/描述就地更新不动版本。viewer 403 → 只读态（资产元数据编辑器
// 同款：以服务端 403 为准如实降级，不预判角色）。
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  ConsoleApiError,
  createPipeline,
  PIPELINE_STEP_OPS,
  previewDatasetSql,
  updatePipeline,
  WINDOW_AGG_FUNCS,
  WINDOW_FUNCS,
  WINDOW_RANKING_FUNCS,
  WINDOW_SHIFT_FUNCS,
  windowNeedsOrderBy,
  type DatasetColumn,
  type PipelineDetail,
  type PipelineStepDef,
  type PipelineStepOp,
  type PipelineUpdateResult,
  type WindowFunc,
} from "../api";

/** 编辑器的数据集候选（基础/右表共用一个 picker 源）。key 供 SQL/窗口
 *  试运行的占位声明（`dataset_key`）；columns 供分区列多选（基础集列快照；
 *  后续步骤的真实输入列以服务端校验为准）。 */
export type DatasetOption = {
  id: string;
  name: string;
  key?: string;
  columns?: DatasetColumn[];
};

/** 步骤草稿：结构化小表单（derive/link）+ JSON 文本（derive filters / join
 *  整体）各占草稿字段；params 为原始底座（保存时保留未编辑键如 sort）。 */
interface StepDraft {
  name: string;
  op: PipelineStepOp;
  params: Record<string, unknown>;
  filtersText: string;
  limitText: string;
  joinText: string;
  rightDatasetId: string;
  leftKeysText: string;
  rightKeysText: string;
  /** 投影：源列 → 输出列名（as 缺省=源列名；空串表示沿用源列名）。 */
  projectRows: Array<{ source: string; as: string }>;
  /** 聚合：分组列（逗号分隔）与度量行（func + column + as）。 */
  groupByText: string;
  measureRows: Array<{ func: string; column: string }>;
  /** SQL 步骤：SQL 文本 + 占位声明（名 → 数据集 id）+ 参数声明（名 → 值文本）。 */
  sqlText: string;
  sqlTables: Array<{ name: string; datasetId: string }>;
  sqlParams: Array<{ name: string; value: string }>;
  /** 窗口步骤：分区列（勾选；基础集列快照）+ 排序行（列 + asc/desc）+
   *  函数行（func / column / offset / as）。 */
  partitionColumns: string[];
  orderByRows: Array<{ column: string; dir: "asc" | "desc" }>;
  windowRows: Array<{
    func: string;
    column: string;
    offset: string;
    as: string;
  }>;
  /** 窗口试运行结果（复用 SQL 预览机制：零副作用预览行 + 列 schema）。 */
  windowPreview: {
    columns: Array<{ name: string; type: string }>;
    rows: Array<Record<string, unknown>>;
    row_count: number;
    truncated: boolean;
  } | null;
  windowPreviewError: string | null;
  windowPreviewing: boolean;
  /** SQL 试运行结果（预览行 + 列 schema + 是否截断）。 */
  sqlPreview: {
    columns: Array<{ name: string; type: string }>;
    rows: Array<Record<string, unknown>>;
    row_count: number;
    truncated: boolean;
  } | null;
  sqlPreviewError: string | null;
  sqlPreviewing: boolean;
}

/** 逗号/顿号分隔的键列文本 → 数组（trim + 去空）。 */
function parseKeys(text: string): string[] {
  return text
    .split(/[,，、]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** 投影行 → 载荷（as 空串 = 沿用源列名；去重由服务端 422 兜底）。 */
function projectRowsToParams(
  rows: Array<{ source: string; as: string }>,
): Array<{ source: string; as?: string }> {
  return rows
    .filter((row) => row.source.trim() !== "")
    .map((row) =>
      row.as.trim() === ""
        ? { source: row.source.trim() }
        : { source: row.source.trim(), as: row.as.trim() },
    );
}

/** 度量行 → 载荷（func 非 count 时 column 必填——与服务端同规）。 */
function measureRowsToParams(
  rows: Array<{ func: string; column: string }>,
): Array<{ func: string; column?: string }> {
  return rows
    .filter((row) => row.func.trim() !== "")
    .map((row) =>
      row.column.trim() === ""
        ? { func: row.func.trim() }
        : { func: row.func.trim(), column: row.column.trim() },
    );
}

/** 窗口函数行 → 载荷（闭集 + 参数有意义性，逐条与服务端 D1 同规）：
 *  - 排序类（row_number/rank/dense_rank）**不收 column**（无意义参数显式
 *    拒绝——UI 侧直接不提交）；
 *  - lag/lead：column 必填、offset 可空（缺省 1，范围 1..10000）；
 *  - sum/avg/min/max：column 必填；count 可空（全行计数）；
 *  - `as` 必填且唯一（重名 422 指名，不静默改名）。
 * 非法即抛带错误码的 Error（保存/试运行处按码取本地化文案）。 */
function windowRowsToParams(
  rows: StepDraft["windowRows"],
): Array<{ func: string; column?: string; offset?: number; as: string }> {
  const out: Array<{
    func: string;
    column?: string;
    offset?: number;
    as: string;
  }> = [];
  const seenAs = new Set<string>();
  for (const row of rows) {
    const func = row.func.trim();
    const as = row.as.trim();
    if (as === "") throw new Error("window_as");
    if (seenAs.has(as)) throw new Error("window_as_duplicate");
    seenAs.add(as);
    const column = row.column.trim();
    const entry: { func: string; column?: string; offset?: number; as: string } =
      { func, as };
    if (WINDOW_RANKING_FUNCS.includes(func as WindowFunc)) {
      // 排序类不接受 column（UI 已隐藏；这里兜底丢弃，绝不提交无意义参数）。
    } else if (WINDOW_SHIFT_FUNCS.includes(func as WindowFunc)) {
      if (column === "") throw new Error("window_column");
      entry.column = column;
      const offsetText = row.offset.trim();
      if (offsetText !== "") {
        const offset = Number(offsetText);
        if (
          !Number.isInteger(offset) ||
          offset < 1 ||
          offset > 10000
        ) {
          throw new Error("window_offset");
        }
        entry.offset = offset;
      }
    } else if (WINDOW_AGG_FUNCS.includes(func as WindowFunc)) {
      if (column === "") throw new Error("window_column");
      entry.column = column;
    } else if (func === "count") {
      if (column !== "") entry.column = column;
    } else {
      throw new Error("window");
    }
    out.push(entry);
  }
  return out;
}

/** 标识符引用（双引号；内部双引号翻倍——SQLite/PG 同口径）。 */
function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

/** 窗口步骤参数 → 只读预览 SQL（复用既有 SQL 试运行机制：零副作用）。
 *  语义与引擎 OVER 同形：排序类/位移类带 ORDER BY（并列由引擎内部
 *  `__row_hash` tie-break，预览不追加）；聚合类按缺省 frame=whole_partition
 *  不带 ORDER BY（带上会退化成 running frame）。预览针对编辑器基础数据集
 *  （唯一可用列快照），真实步骤输入列以服务端校验为准。 */
export function windowPreviewSql(params: {
  partition_by: string[];
  order_by: Array<{ column: string; dir: string }>;
  window: Array<{ func: string; column?: string; offset?: number; as: string }>;
}): string {
  const partition =
    params.partition_by.length > 0
      ? `PARTITION BY ${params.partition_by.map(quoteIdent).join(", ")}`
      : "";
  const order =
    params.order_by.length > 0
      ? `ORDER BY ${params.order_by
          .map((row) => `${quoteIdent(row.column)} ${row.dir.toUpperCase()}`)
          .join(", ")}`
      : "";
  const clauses = params.window.map((row) => {
    const func = row.func.toUpperCase();
    if (WINDOW_RANKING_FUNCS.includes(row.func as WindowFunc)) {
      return `${func}() OVER (${[partition, order].filter(Boolean).join(" ")}) AS ${quoteIdent(row.as)}`;
    }
    if (WINDOW_SHIFT_FUNCS.includes(row.func as WindowFunc)) {
      const value = quoteIdent(row.column ?? "");
      const offset = row.offset != null ? `, ${row.offset}` : "";
      return `${func}(${value}${offset}) OVER (${[partition, order]
        .filter(Boolean)
        .join(" ")}) AS ${quoteIdent(row.as)}`;
    }
    const value =
      row.func === "count"
        ? row.column
          ? quoteIdent(row.column)
          : "*"
        : quoteIdent(row.column ?? "");
    return `${func}(${value}) OVER (${partition}) AS ${quoteIdent(row.as)}`;
  });
  return `SELECT *, ${clauses.join(", ")} FROM {src}`;
}

/** 占位声明 → 载荷（name + dataset_key；dataset_id 需查 key，此处传 id）。 */
function sqlTablesToParams(
  rows: Array<{ name: string; datasetId: string }>,
): Array<{ name: string; dataset_key: string }> {
  return rows
    .filter((row) => row.name.trim() !== "")
    .map((row) => ({
      name: row.name.trim(),
      dataset_key: row.datasetId.trim(),
    }));
}

/** 参数声明 → 载荷（值文本按字面量传；空串 = 不声明该参数）。 */
function sqlParamsToParams(
  rows: Array<{ name: string; value: string }>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const name = row.name.trim();
    if (name !== "") out[name] = row.value;
  }
  return out;
}

function draftFromStep(step: PipelineStepDef): StepDraft {
  const params = step.params ?? {};
  const projectRows = Array.isArray(params.columns)
    ? (params.columns as Array<Record<string, unknown>>).map((c) => ({
        source: String(c.source ?? ""),
        as: String(c.as ?? ""),
      }))
    : [];
  const measureRows = Array.isArray(params.measures)
    ? (params.measures as Array<Record<string, unknown>>).map((m) => ({
        func: String(m.func ?? ""),
        column: String(m.column ?? ""),
      }))
    : [];
  const sqlTables = Array.isArray(params.tables)
    ? (params.tables as Array<Record<string, unknown>>).map((t) => ({
        name: String(t.name ?? ""),
        datasetId: String(t.dataset_key ?? ""),
      }))
    : [];
  const sqlParams = params.params && typeof params.params === "object"
    ? Object.entries(params.params as Record<string, unknown>).map(
        ([name, value]) => ({ name, value: String(value ?? "") }),
      )
    : [];
  // 窗口步骤：分区列 / 排序行 / 函数行（旧步骤无该键时给空白默认）。
  const partitionColumns = Array.isArray(params.partition_by)
    ? (params.partition_by as unknown[]).filter(
        (v): v is string => typeof v === "string",
      )
    : [];
  const orderByRows: Array<{ column: string; dir: "asc" | "desc" }> =
    Array.isArray(params.order_by)
      ? (params.order_by as Array<Record<string, unknown>>).map((row) => ({
          column: String(row.column ?? ""),
          dir: row.dir === "desc" ? "desc" : "asc",
        }))
      : [];
  const windowRows = Array.isArray(params.window)
    ? (params.window as Array<Record<string, unknown>>).map((row) => ({
        func: String(row.func ?? ""),
        column: String(row.column ?? ""),
        offset: row.offset != null ? String(row.offset) : "",
        as: String(row.as ?? ""),
      }))
    : [];
  return {
    name: step.name,
    op: step.op,
    params,
    filtersText:
      params.filters != null ? JSON.stringify(params.filters, null, 2) : "",
    limitText: params.limit != null ? String(params.limit) : "",
    joinText: JSON.stringify(params, null, 2),
    rightDatasetId:
      typeof params.right_dataset_id === "string"
        ? params.right_dataset_id
        : "",
    leftKeysText: Array.isArray(params.left_keys)
      ? (params.left_keys as unknown[]).filter((v) => typeof v === "string").join(", ")
      : "",
    rightKeysText: Array.isArray(params.right_keys)
      ? (params.right_keys as unknown[]).filter((v) => typeof v === "string").join(", ")
      : "",
    projectRows,
    measureRows,
    groupByText: Array.isArray(params.group_by)
      ? (params.group_by as unknown[]).filter((v) => typeof v === "string").join(", ")
      : "",
    sqlText: typeof params.sql === "string" ? params.sql : "",
    sqlTables,
    sqlParams,
    partitionColumns,
    orderByRows,
    windowRows,
    windowPreview: null,
    windowPreviewError: null,
    windowPreviewing: false,
    sqlPreview: null,
    sqlPreviewError: null,
    sqlPreviewing: false,
  };
}

function blankDraft(): StepDraft {
  return {
    name: "",
    op: "derive",
    params: {},
    filtersText: "",
    limitText: "",
    joinText: "{}",
    rightDatasetId: "",
    leftKeysText: "",
    rightKeysText: "",
    projectRows: [{ source: "", as: "" }],
    measureRows: [{ func: "count", column: "" }],
    groupByText: "",
    sqlText: "",
    sqlTables: [{ name: "", datasetId: "" }],
    sqlParams: [],
    partitionColumns: [],
    orderByRows: [{ column: "", dir: "asc" }],
    windowRows: [{ func: "row_number", column: "", offset: "", as: "" }],
    windowPreview: null,
    windowPreviewError: null,
    windowPreviewing: false,
    sqlPreview: null,
    sqlPreviewError: null,
    sqlPreviewing: false,
  };
}

// ── 「输入来源」高级区（wire-dag-executor D1/D7）─────────────────────────
// 用户显式声明的输入引用按**步骤下标**保存（-1 = 保留名 base），保存时再
// 解析成步骤名——重命名步骤不会打断已选引用（服务端按名字校验）。缺省
// （未改动）不下发 inputs 键：v1 线性载荷逐字节保持。

/** 保留值：-1 = 基础数据集（base）；其余为步骤下标（0 起）。 */
const INPUT_BASE = -1;

/** 输入校验错误码（i18n `workbench.inputsErrors` 键一一对应）。 */
export type PipelineInputsErrorCode = "inputs_cycle" | "inputs_forward";

export interface PipelineInputsError {
  code: PipelineInputsErrorCode;
  /** 定位细节（环边 / 前向引用目标，步骤显示名）。 */
  detail: string;
}

/** 步骤显示名（空名沿用服务端缺省名 step-<seq>）。 */
function inputStepName(steps: StepDraft[], index: number): string {
  return steps[index]?.name.trim() || `step-${index + 1}`;
}

/** 缺省输入：首步吃基础数据集，其后承接前一步（v1 线性语义）。 */
function defaultInputsFor(index: number): number[] {
  return index === 0 ? [INPUT_BASE] : [index - 1];
}

/** 生效输入（用户改动优先，否则缺省）。 */
function effectiveInputsFor(
  touched: Record<number, number[]>,
  index: number,
): number[] {
  return touched[index] ?? defaultInputsFor(index);
}

/** 仅保留合法引用（步骤下标越界/已删除步骤的残留引用忽略）。 */
function validRefsFor(steps: StepDraft[], refs: number[]): number[] {
  return refs.filter(
    (ref) => ref === INPUT_BASE || (ref >= 0 && ref < steps.length),
  );
}

function sameRefs(a: number[], b: number[]): boolean {
  const norm = (refs: number[]) => [...refs].sort((x, y) => x - y).join(",");
  return norm(a) === norm(b);
}

/** 校验「输入来源」图：先成环（DFS 栈内回边），再前向引用。返回
 *  步骤下标 → 错误（无错为空对象）。 */
export function validatePipelineInputs(
  steps: StepDraft[],
  touched: Record<number, number[]>,
): Record<number, PipelineInputsError> {
  const count = steps.length;
  const inputsFor = (index: number) =>
    validRefsFor(steps, effectiveInputsFor(touched, index));
  const edges = new Map<number, number[]>();
  for (let i = 0; i < count; i += 1) {
    edges.set(
      i,
      inputsFor(i).filter((ref) => ref !== INPUT_BASE),
    );
  }
  // 三色 DFS：visited=已出栈（黑），inStack=栈内（灰）——回灰即成环。
  // 返回构成环的边 [父, 子]（子已是栈内祖先）；无环返回 null。
  const visited = new Set<number>();
  const inStack = new Set<number>();
  const dfs = (
    node: number,
    parent: number | null,
  ): [number, number] | null => {
    if (inStack.has(node)) {
      return parent == null ? [node, node] : [parent, node];
    }
    if (visited.has(node)) return null;
    visited.add(node);
    inStack.add(node);
    for (const next of edges.get(node) ?? []) {
      const found = dfs(next, node);
      if (found) return found;
    }
    inStack.delete(node);
    return null;
  };
  let cycleEdge: [number, number] | null = null;
  for (let i = 0; i < count; i += 1) {
    if (visited.has(i)) continue;
    cycleEdge = dfs(i, null);
    if (cycleEdge) break;
  }
  const errors: Record<number, PipelineInputsError> = {};
  if (cycleEdge) {
    const [from, to] = cycleEdge;
    const detail = `${inputStepName(steps, from)} → ${inputStepName(steps, to)}`;
    // 环边两端都标错（改任一端都可解环）。
    errors[from] = { code: "inputs_cycle", detail };
    errors[to] = { code: "inputs_cycle", detail };
    return errors;
  }
  // 前向引用（含自引用；自引用已由上方成环检测命中）。
  for (let i = 0; i < count; i += 1) {
    const forward = inputsFor(i).find((ref) => ref !== INPUT_BASE && ref >= i);
    if (forward != null) {
      errors[i] = { code: "inputs_forward", detail: inputStepName(steps, forward) };
    }
  }
  return errors;
}

/** 从既有版本步骤还原「输入来源」改动表（显式 inputs → 步骤下标）。
 *  无法解析的引用忽略（服务端按名字 422 拒绝，前端不伪造）。 */
function inputsTouchedFromSteps(
  steps: PipelineStepDef[],
): Record<number, number[]> {
  const touched: Record<number, number[]> = {};
  steps.forEach((step, index) => {
    if (!Array.isArray(step.inputs)) return;
    const refs = step.inputs
      .map((ref) => {
        if (ref === "base") return INPUT_BASE;
        return steps.findIndex((candidate) => candidate.name === ref);
      })
      .filter((ref) => ref === INPUT_BASE || ref >= 0);
    touched[index] = refs;
  });
  return touched;
}

/** 当前版本步骤（新建为空）。 */
function currentStepsOf(pipeline: PipelineDetail | null): PipelineStepDef[] {
  return pipeline
    ? (pipeline.versions.find((v) => v.version === pipeline.current_version)
        ?.steps ?? [])
    : [];
}

/** 仅对用户改动过的步骤下发 inputs（未改动步骤省略键）。 */
function attachInputs(
  step: PipelineStepDef,
  steps: StepDraft[],
  touched: Record<number, number[]>,
  index: number,
): PipelineStepDef {
  const refs = touched[index];
  if (refs == null) return step;
  return {
    ...step,
    inputs: validRefsFor(steps, refs).map((ref) =>
      ref === INPUT_BASE ? "base" : inputStepName(steps, ref),
    ),
  };
}

/** 草稿 → 保存形状（JSON 解析失败抛错并带步骤序号，供行内回显）。 */
function buildStep(draft: StepDraft): PipelineStepDef {
  const params: Record<string, unknown> = { ...draft.params };
  if (draft.op === "derive" || draft.op === "filter") {
    // filter 与 derive 同一套 filters/sort/limit 语义（血缘 kind 不同）
    delete params.filters;
    delete params.limit;
    const filtersText = draft.filtersText.trim();
    if (filtersText) {
      const parsed: unknown = JSON.parse(filtersText);
      if (!Array.isArray(parsed)) {
        throw new Error("filters");
      }
      params.filters = parsed;
    }
    const limitText = draft.limitText.trim();
    if (limitText) {
      const limit = Number(limitText);
      if (!Number.isFinite(limit) || limit <= 0) {
        throw new Error("limit");
      }
      params.limit = limit;
    }
  } else if (draft.op === "join") {
    const parsed: unknown = JSON.parse(draft.joinText.trim() || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("join");
    }
    return { name: draft.name, op: draft.op, params: parsed as Record<string, unknown> };
  } else if (draft.op === "project") {
    delete params.columns;
    const columns = projectRowsToParams(draft.projectRows);
    if (columns.length === 0) {
      throw new Error("project");
    }
    params.columns = columns;
  } else if (draft.op === "aggregate") {
    delete params.group_by;
    delete params.measures;
    const groupBy = parseKeys(draft.groupByText);
    if (groupBy.length === 0) {
      throw new Error("group_by");
    }
    const measures = measureRowsToParams(draft.measureRows);
    if (measures.length === 0) {
      throw new Error("measures");
    }
    params.group_by = groupBy;
    params.measures = measures;
  } else if (draft.op === "union") {
    delete params.right_dataset_id;
    delete params.on;
    if (draft.rightDatasetId.trim() === "") {
      throw new Error("union");
    }
    params.right_dataset_id = draft.rightDatasetId.trim();
    params.on = "by_name";
  } else if (draft.op === "window") {
    delete params.partition_by;
    delete params.order_by;
    delete params.window;
    const partitionBy = draft.partitionColumns
      .map((column) => column.trim())
      .filter(Boolean);
    if (partitionBy.length > 10) throw new Error("window");
    const orderBy = draft.orderByRows
      .filter((row) => row.column.trim() !== "")
      .map((row) => ({ column: row.column.trim(), dir: row.dir }));
    if (orderBy.length > 10) throw new Error("window");
    if (draft.windowRows.length === 0 || draft.windowRows.length > 20) {
      throw new Error("window");
    }
    // 排序类/位移类函数必须有 order_by（服务端同规；前端先行拦截）。
    const needsOrder = draft.windowRows.some((row) =>
      windowNeedsOrderBy(row.func.trim()),
    );
    if (needsOrder && orderBy.length === 0) {
      throw new Error("window_order_by");
    }
    const windowRows = windowRowsToParams(draft.windowRows);
    params.partition_by = partitionBy;
    params.order_by = orderBy;
    params.window = windowRows;
  } else if (draft.op === "sql") {
    delete params.sql;
    delete params.tables;
    delete params.params;
    const sql = draft.sqlText.trim();
    if (!sql) {
      throw new Error("sql");
    }
    const tables = sqlTablesToParams(draft.sqlTables);
    if (tables.length === 0) {
      throw new Error("sql");
    }
    params.sql = sql;
    params.tables = tables;
    params.params = sqlParamsToParams(draft.sqlParams);
  } else {
    delete params.right_dataset_id;
    delete params.left_keys;
    delete params.right_keys;
    params.right_dataset_id = draft.rightDatasetId;
    params.left_keys = parseKeys(draft.leftKeysText);
    params.right_keys = parseKeys(draft.rightKeysText);
  }
  return { name: draft.name, op: draft.op, params };
}

/** 步骤定义规范化比较（与假后端 stepsEqual 同口径：顺序/类型/参数/inputs）。 */
function sameSteps(a: PipelineStepDef[], b: PipelineStepDef[]): boolean {
  const norm = (steps: PipelineStepDef[]) =>
    JSON.stringify(
      steps.map((s) => ({
        name: s.name,
        op: s.op,
        params: s.params ?? {},
        ...(s.inputs != null ? { inputs: s.inputs } : {}),
      })),
    );
  return norm(a) === norm(b);
}

export function PipelineEditor({
  pipeline,
  datasets,
  onSaved,
  onCancel,
}: {
  /** null = 新建；对象 = 编辑既有（步骤草稿取当前版本）。 */
  pipeline: PipelineDetail | null;
  /** 基础数据集 / link 右表候选。 */
  datasets: DatasetOption[];
  onSaved: (result: PipelineUpdateResult) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(pipeline?.name ?? "");
  const [description, setDescription] = useState(pipeline?.description ?? "");
  const [baseId, setBaseId] = useState(pipeline?.base_dataset_id ?? "");
  const [steps, setSteps] = useState<StepDraft[]>(() =>
    pipeline
      ? (pipeline.versions.find(
            (v) => v.version === pipeline.current_version,
          )?.steps ?? []
        ).map(draftFromStep)
      : [],
  );
  const [saving, setSaving] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  /** 行内 JSON/参数错误：步骤序号（1 起）→ 文案。 */
  const [stepErrors, setStepErrors] = useState<Record<number, string>>({});
  /** 高级区「输入来源」：用户改动过的步骤（下标 → 引用下标；-1=base）。
   *  未出现的步骤不下发 inputs（缺省=前一步）。 */
  const [inputsTouched, setInputsTouched] = useState<Record<number, number[]>>(
    () => inputsTouchedFromSteps(currentStepsOf(pipeline)),
  );
  const [inputsOpen, setInputsOpen] = useState(false);
  // viewer 只读：以服务端 403 为准如实降级（资产元数据编辑器同款）。
  const [readOnly, setReadOnly] = useState(false);

  // 切换编辑对象时重置草稿（新建 ↔ 编辑互切）。
  const key = pipeline?.id ?? "new";
  useEffect(() => {
    setName(pipeline?.name ?? "");
    setDescription(pipeline?.description ?? "");
    setBaseId(pipeline?.base_dataset_id ?? "");
    setSteps(
      pipeline
        ? (pipeline.versions.find(
              (v) => v.version === pipeline.current_version,
            )?.steps ?? []
          ).map(draftFromStep)
        : [],
    );
    setOpError(null);
    setStepErrors({});
    setInputsTouched(inputsTouchedFromSteps(currentStepsOf(pipeline)));
    setInputsOpen(false);
    setReadOnly(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const updateStep = (index: number, patch: Partial<StepDraft>) => {
    setSteps((prev) =>
      prev.map((step, i) => (i === index ? { ...step, ...patch } : step)),
    );
  };

  /** 切换某步骤的一个输入引用；改回缺省时从改动表移除（保存载荷省略键）。 */
  const toggleInput = (index: number, ref: number) => {
    setInputsTouched((prev) => {
      const current = effectiveInputsFor(prev, index);
      const next = current.includes(ref)
        ? current.filter((item) => item !== ref)
        : [...current, ref].sort((a, b) => a - b);
      const copy = { ...prev };
      if (sameRefs(next, defaultInputsFor(index))) delete copy[index];
      else copy[index] = next;
      return copy;
    });
  };

  const move = (index: number, delta: -1 | 1) => {
    setSteps((prev) => {
      const target = index + delta;
      if (target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[target]] = [next[target]!, next[index]!];
      return next;
    });
  };

  /** SQL 试运行（零副作用）：预览结果落在该步骤草稿里，不进保存载荷。 */
  const runSqlPreview = async (index: number) => {
    const draft = steps[index];
    if (!draft) return;
    const sql = draft.sqlText.trim();
    const tables = sqlTablesToParams(draft.sqlTables);
    if (!sql || tables.length === 0) {
      updateStep(index, {
        sqlPreview: null,
        sqlPreviewError: t("console.datasets.pipeline.workbench.sqlPreviewNeedsInput"),
      });
      return;
    }
    updateStep(index, { sqlPreviewing: true, sqlPreviewError: null });
    try {
      const result = await previewDatasetSql(baseId, {
        sql,
        tables,
        params: sqlParamsToParams(draft.sqlParams),
      });
      updateStep(index, {
        sqlPreviewing: false,
        sqlPreview: {
          columns: result.columns,
          rows: result.rows,
          row_count: result.row_count,
          truncated: result.truncated,
        },
        sqlPreviewError: null,
      });
    } catch (err) {
      updateStep(index, {
        sqlPreviewing: false,
        sqlPreview: null,
        sqlPreviewError: apiErrorText(
          err,
          t("console.datasets.pipeline.workbench.sqlPreviewFailed"),
        ),
      });
    }
  };

  /** 窗口步骤错误码 → 本地化文案（保存与试运行共用同一张码表）。 */
  const windowErrorText = (code: string): string | null => {
    const known = [
      "window",
      "window_order_by",
      "window_as",
      "window_as_duplicate",
      "window_column",
      "window_offset",
    ];
    if (!known.includes(code)) return null;
    return t(`console.datasets.pipeline.workbench.windowErrors.${code}`);
  };

  /** 输入来源错误码 → 本地化文案（行内回显错误码本身，便于对表排查）。 */
  const inputsErrorText = (code: string, detail: string): string =>
    `${t(`console.datasets.pipeline.workbench.inputsErrors.${code}`, {
      detail,
    })}（${code}）`;

  /** 窗口试运行（复用 SQL 试运行机制：零副作用、不落库）——按当前窗口
   *  参数生成只读 SELECT（`SELECT *, <func> OVER (…) AS <as> FROM {src}`），
   *  走既有 /datasets/{id}/sql/preview 预览表；结果只落草稿，不进保存载荷。 */
  const runWindowPreview = async (index: number) => {
    const draft = steps[index];
    if (!draft) return;
    const source = datasets.find((dataset) => dataset.id === baseId);
    if (baseId === "" || !source) {
      updateStep(index, {
        windowPreview: null,
        windowPreviewError: t(
          "console.datasets.pipeline.workbench.windowPreviewNeedsInput",
        ),
      });
      return;
    }
    let params: {
      partition_by: string[];
      order_by: Array<{ column: string; dir: string }>;
      window: Array<{
        func: string;
        column?: string;
        offset?: number;
        as: string;
      }>;
    };
    try {
      const built = buildStep({ ...draft, name: draft.name || "window" });
      params = {
        partition_by: built.params.partition_by as string[],
        order_by: built.params.order_by as Array<{
          column: string;
          dir: string;
        }>,
        window: built.params.window as Array<{
          func: string;
          column?: string;
          offset?: number;
          as: string;
        }>,
      };
    } catch (err) {
      const code = (err as Error).message;
      updateStep(index, {
        windowPreview: null,
        windowPreviewError:
          windowErrorText(code) ??
          t("console.datasets.pipeline.workbench.windowPreviewFailed"),
      });
      return;
    }
    updateStep(index, { windowPreviewing: true, windowPreviewError: null });
    try {
      const result = await previewDatasetSql(baseId, {
        sql: windowPreviewSql(params),
        tables: [{ name: "src", dataset_key: source.key ?? source.id }],
      });
      updateStep(index, {
        windowPreviewing: false,
        windowPreview: {
          columns: result.columns,
          rows: result.rows,
          row_count: result.row_count,
          truncated: result.truncated,
        },
        windowPreviewError: null,
      });
    } catch (err) {
      updateStep(index, {
        windowPreviewing: false,
        windowPreview: null,
        windowPreviewError: apiErrorText(
          err,
          t("console.datasets.pipeline.workbench.windowPreviewFailed"),
        ),
      });
    }
  };

  // ── 版本语义预判：步骤或基础集变更 → 明示「步骤变更将创建新版本 vN+1」 ──
  const currentSteps = useMemo(() => currentStepsOf(pipeline), [pipeline]);
  const baseChanged =
    pipeline != null && baseId !== pipeline.base_dataset_id;
  const stepsDirty =
    pipeline != null &&
    (steps.length !== currentSteps.length ||
      steps.some((draft, i) => {
        try {
          return !sameSteps(
            [attachInputs(buildStep(draft), steps, inputsTouched, i)],
            [currentSteps[i]!],
          );
        } catch {
          return true;
        }
      }));
  const willCreateVersion = pipeline != null && (baseChanged || stepsDirty);

  // 「输入来源」校验（无环 + 无前向引用）：出错即行内标错误码并阻断保存。
  const inputsErrors = useMemo(
    () => validatePipelineInputs(steps, inputsTouched),
    [steps, inputsTouched],
  );

  const save = async () => {
    if (saving || readOnly) return;
    setOpError(null);
    // 本地校验：名称/基础集/步骤名 + JSON 文本可解析（不合法不发请求）。
    if (!name.trim() || !baseId || steps.length === 0) {
      setOpError(t("console.datasets.pipeline.workbench.editorError"));
      return;
    }
    const errors: Record<number, string> = {};
    // 输入来源错误先落行内（错误码 + 定位细节）；出错时展开高级区。
    const inputErrorIndices = Object.keys(inputsErrors);
    if (inputErrorIndices.length > 0) {
      setInputsOpen(true);
      for (const index of inputErrorIndices) {
        const error = inputsErrors[Number(index)]!;
        errors[Number(index) + 1] = inputsErrorText(error.code, error.detail);
      }
    }
    const built: PipelineStepDef[] = [];
    for (let i = 0; i < steps.length; i += 1) {
      const draft = steps[i]!;
      if (!draft.name.trim()) {
        errors[i + 1] = t("console.datasets.pipeline.workbench.stepNameRequired");
        continue;
      }
      try {
        built.push(attachInputs(buildStep(draft), steps, inputsTouched, i));
      } catch (err) {
        const code = (err as Error).message;
        // 窗口参数错误码走专用文案（其余 JSON/参数错误沿既有拼接）。
        errors[i + 1] =
          windowErrorText(code) ??
          `${t("console.datasets.pipeline.workbench.jsonError")}${
            code ? `（${code}）` : ""
          }`;
      }
    }
    setStepErrors(errors);
    if (Object.keys(errors).length > 0 || built.length !== steps.length) {
      return;
    }
    setSaving(true);
    try {
      const result =
        pipeline == null
          ? await createPipeline({
              name: name.trim(),
              description: description.trim() || undefined,
              base_dataset_id: baseId,
              steps: built,
            })
          : await updatePipeline(pipeline.id, {
              name: name.trim(),
              description: description.trim() || null,
              base_dataset_id: baseId,
              steps: built,
            });
      onSaved(result);
    } catch (err) {
      if (err instanceof ConsoleApiError && err.status === 403) {
        // viewer 只读：切只读态（字段禁用、保存收起），不报错条。
        setReadOnly(true);
        setOpError(null);
      } else {
        setOpError(
          apiErrorText(err, t("console.datasets.pipeline.workbench.editorError")),
        );
      }
    } finally {
      setSaving(false);
    }
  };

  const inputClass =
    "rounded border border-slate-300 px-2 py-1 text-xs disabled:cursor-not-allowed disabled:bg-slate-100";

  /** 分区列多选候选（基础数据集列快照；无快照时如实提示不渲染勾选项）。 */
  const baseColumns =
    datasets.find((dataset) => dataset.id === baseId)?.columns ?? [];

  return (
    <section
      data-testid="pipeline-editor"
      data-readonly={readOnly ? "true" : "false"}
      className="mt-2 rounded-lg border border-slate-200 bg-white p-4"
    >
      <h3
        data-testid="pipeline-editor-title"
        className="text-sm font-semibold text-slate-800"
      >
        {pipeline == null
          ? t("console.datasets.pipeline.workbench.editorTitleNew")
          : t("console.datasets.pipeline.workbench.editorTitleEdit")}
      </h3>

      {opError && (
        <p
          data-testid="pipeline-editor-error"
          className="mt-2 text-xs text-red-600"
        >
          {opError}
        </p>
      )}
      {readOnly && (
        <p
          data-testid="pipeline-editor-readonly"
          className="mt-2 rounded border border-amber-200 bg-amber-50/60 px-2 py-1 text-xs text-amber-700"
        >
          {t("console.datasets.pipeline.workbench.readOnlyNote")}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-start gap-3">
        <label className="flex w-56 flex-col gap-1">
          <span className="text-xs text-slate-500">
            {t("console.datasets.pipeline.workbench.nameLabel")}
          </span>
          <input
            data-testid="pipeline-editor-name"
            value={name}
            disabled={readOnly || saving}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("console.datasets.pipeline.workbench.namePlaceholder")}
            className={inputClass}
          />
        </label>
        <label className="flex w-72 flex-col gap-1">
          <span className="text-xs text-slate-500">
            {t("console.datasets.pipeline.workbench.descLabel")}
          </span>
          <input
            data-testid="pipeline-editor-desc"
            value={description}
            disabled={readOnly || saving}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("console.datasets.pipeline.workbench.descPlaceholder")}
            className={inputClass}
          />
        </label>
        <label className="flex w-60 flex-col gap-1">
          <span className="text-xs text-slate-500">
            {t("console.datasets.pipeline.workbench.baseLabel")}
          </span>
          <select
            data-testid="pipeline-editor-base"
            value={baseId}
            disabled={readOnly || saving}
            onChange={(e) => {
              setBaseId(e.target.value);
            }}
            className={inputClass}
          >
            <option value="">
              {t("console.datasets.pipeline.workbench.basePlaceholder")}
            </option>
            {datasets.map((dataset) => (
              <option key={dataset.id} value={dataset.id}>
                {dataset.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="mt-4 flex items-center justify-between">
        <h4 className="text-xs font-medium text-slate-600">
          {t("console.datasets.pipeline.workbench.stepsTitle")}
        </h4>
        <button
          type="button"
          data-testid="pipeline-add-step"
          disabled={readOnly || saving}
          onClick={() => setSteps((prev) => [...prev, blankDraft()])}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {t("console.datasets.pipeline.workbench.addStep")}
        </button>
      </div>
      <p className="mt-1 text-[11px] text-slate-400">
        {t("console.datasets.pipeline.workbench.stepsHint")}
      </p>

      <ol data-testid="pipeline-steps" className="mt-2 space-y-3">
        {steps.map((step, index) => (
          <li
            key={index}
            data-testid="pipeline-step"
            data-index={index}
            data-op={step.op}
            className="rounded-lg border border-slate-200 bg-slate-50/60 p-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-900 text-[11px] font-medium text-white">
                {index + 1}
              </span>
              <input
                data-testid="pipeline-step-name"
                value={step.name}
                disabled={readOnly || saving}
                onChange={(e) => updateStep(index, { name: e.target.value })}
                placeholder={t(
                  "console.datasets.pipeline.workbench.stepNamePlaceholder",
                )}
                className={`w-44 ${inputClass}`}
              />
              <select
                data-testid="pipeline-step-op"
                value={step.op}
                disabled={readOnly || saving}
                onChange={(e) =>
                  updateStep(index, {
                    op: e.target.value as PipelineStepOp,
                  })
                }
                className={inputClass}
              >
                {PIPELINE_STEP_OPS.map((op) => (
                  <option key={op} value={op}>
                    {t(`console.datasets.pipeline.workbench.opLabels.${op}`)}
                  </option>
                ))}
              </select>
              <span className="ml-auto flex items-center gap-1">
                <button
                  type="button"
                  data-testid="pipeline-step-up"
                  disabled={readOnly || saving || index === 0}
                  onClick={() => move(index, -1)}
                  title={t("console.datasets.pipeline.workbench.moveUp")}
                  className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  ↑
                </button>
                <button
                  type="button"
                  data-testid="pipeline-step-down"
                  disabled={readOnly || saving || index === steps.length - 1}
                  onClick={() => move(index, 1)}
                  title={t("console.datasets.pipeline.workbench.moveDown")}
                  className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  ↓
                </button>
                <button
                  type="button"
                  data-testid="pipeline-step-remove"
                  disabled={readOnly || saving}
                  onClick={() =>
                    setSteps((prev) => prev.filter((_, i) => i !== index))
                  }
                  title={t("console.datasets.pipeline.workbench.removeStep")}
                  className="rounded border border-rose-200 px-1.5 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  ✕
                </button>
              </span>
            </div>

            {/* 参数表单：三类型各按既有原子操作参数形状。 */}
            <div className="mt-2 flex flex-wrap items-start gap-3">
              {step.op === "derive" && (
                <>
                  <label className="flex w-28 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.limitLabel")}
                    </span>
                    <input
                      data-testid="pipeline-step-limit"
                      value={step.limitText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { limitText: e.target.value })
                      }
                      placeholder="1000"
                      inputMode="numeric"
                      className={inputClass}
                    />
                  </label>
                  <label className="flex w-80 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.filtersLabel")}
                    </span>
                    <textarea
                      data-testid="pipeline-step-filters"
                      value={step.filtersText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { filtersText: e.target.value })
                      }
                      placeholder={t(
                        "console.datasets.pipeline.workbench.filtersPlaceholder",
                      )}
                      rows={3}
                      className="rounded border border-slate-300 px-2 py-1 font-mono text-[11px] disabled:cursor-not-allowed disabled:bg-slate-100"
                    />
                    <span className="text-[11px] text-slate-400">
                      {t("console.datasets.pipeline.workbench.filtersHint")}
                    </span>
                  </label>
                </>
              )}
              {step.op === "join" && (
                <label className="flex w-96 flex-col gap-1">
                  <span className="text-[11px] text-slate-500">
                    {t("console.datasets.pipeline.workbench.paramsLabel")}
                  </span>
                  <textarea
                    data-testid="pipeline-step-json"
                    value={step.joinText}
                    disabled={readOnly || saving}
                    onChange={(e) =>
                      updateStep(index, { joinText: e.target.value })
                    }
                    rows={6}
                    className="rounded border border-slate-300 px-2 py-1 font-mono text-[11px] disabled:cursor-not-allowed disabled:bg-slate-100"
                  />
                  <span className="text-[11px] text-slate-400">
                    {t("console.datasets.pipeline.workbench.joinHint")}
                  </span>
                </label>
              )}
              {step.op === "link" && (
                <>
                  <label className="flex w-56 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.linkRightLabel")}
                    </span>
                    <select
                      data-testid="pipeline-step-right"
                      value={step.rightDatasetId}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { rightDatasetId: e.target.value })
                      }
                      className={inputClass}
                    >
                      <option value="">
                        {t("console.datasets.pipeline.workbench.basePlaceholder")}
                      </option>
                      {datasets.map((dataset) => (
                        <option key={dataset.id} value={dataset.id}>
                          {dataset.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="flex w-56 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.linkLeftKeysLabel")}
                    </span>
                    <input
                      data-testid="pipeline-step-left-keys"
                      value={step.leftKeysText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { leftKeysText: e.target.value })
                      }
                      placeholder={t(
                        "console.datasets.pipeline.workbench.keysPlaceholder",
                      )}
                      className={inputClass}
                    />
                  </label>
                  <label className="flex w-56 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.linkRightKeysLabel")}
                    </span>
                    <input
                      data-testid="pipeline-step-right-keys"
                      value={step.rightKeysText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { rightKeysText: e.target.value })
                      }
                      placeholder={t(
                        "console.datasets.pipeline.workbench.keysPlaceholder",
                      )}
                      className={inputClass}
                    />
                  </label>
                  <span className="mt-4 text-[11px] text-slate-400">
                    {t("console.datasets.pipeline.workbench.linkHint")}
                  </span>
                </>
              )}
              {step.op === "project" && (
                <div
                  data-testid="pipeline-step-project"
                  className="w-full rounded border border-slate-200 bg-white p-2"
                >
                  <p className="text-[11px] text-slate-500">
                    {t("console.datasets.pipeline.workbench.projectHint")}
                  </p>
                  <ul className="mt-1 space-y-1">
                    {step.projectRows.map((row, rowIndex) => (
                      <li key={rowIndex} className="flex items-center gap-2">
                        <input
                          data-testid={`pipeline-step-project-source-${rowIndex}`}
                          value={row.source}
                          disabled={readOnly || saving}
                          onChange={(e) => {
                            const rows = step.projectRows.map((r, i) =>
                              i === rowIndex
                                ? { ...r, source: e.target.value }
                                : r,
                            );
                            updateStep(index, { projectRows: rows });
                          }}
                          placeholder={t(
                            "console.datasets.pipeline.workbench.projectSourcePlaceholder",
                          )}
                          className="w-40 rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                        />
                        <span className="text-slate-400">→</span>
                        <input
                          data-testid={`pipeline-step-project-as-${rowIndex}`}
                          value={row.as}
                          disabled={readOnly || saving}
                          onChange={(e) => {
                            const rows = step.projectRows.map((r, i) =>
                              i === rowIndex ? { ...r, as: e.target.value } : r,
                            );
                            updateStep(index, { projectRows: rows });
                          }}
                          placeholder={t(
                            "console.datasets.pipeline.workbench.projectAsPlaceholder",
                          )}
                          className="w-40 rounded border border-slate-300 px-2 py-1 text-[11px]"
                        />
                        <button
                          type="button"
                          data-testid={`pipeline-step-project-remove-${rowIndex}`}
                          disabled={readOnly || saving || step.projectRows.length === 1}
                          onClick={() =>
                            updateStep(index, {
                              projectRows: step.projectRows.filter(
                                (_, i) => i !== rowIndex,
                              ),
                            })
                          }
                          className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                        >
                          ✕
                        </button>
                      </li>
                    ))}
                  </ul>
                  <button
                    type="button"
                    data-testid="pipeline-step-project-add"
                    disabled={readOnly || saving}
                    onClick={() =>
                      updateStep(index, {
                        projectRows: [...step.projectRows, { source: "", as: "" }],
                      })
                    }
                    className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                  >
                    {t("console.datasets.pipeline.workbench.projectAdd")}
                  </button>
                </div>
              )}
              {step.op === "aggregate" && (
                <div
                  data-testid="pipeline-step-aggregate"
                  className="w-full rounded border border-slate-200 bg-white p-2"
                >
                  <label className="flex w-64 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.groupByLabel")}
                    </span>
                    <input
                      data-testid="pipeline-step-group-by"
                      value={step.groupByText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { groupByText: e.target.value })
                      }
                      placeholder={t(
                        "console.datasets.pipeline.workbench.groupByPlaceholder",
                      )}
                      className="rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                    />
                  </label>
                  <ul className="mt-2 space-y-1">
                    {step.measureRows.map((row, rowIndex) => (
                      <li key={rowIndex} className="flex items-center gap-2">
                        <select
                          data-testid={`pipeline-step-measure-func-${rowIndex}`}
                          value={row.func}
                          disabled={readOnly || saving}
                          onChange={(e) => {
                            const rows = step.measureRows.map((r, i) =>
                              i === rowIndex ? { ...r, func: e.target.value } : r,
                            );
                            updateStep(index, { measureRows: rows });
                          }}
                          className="w-28 rounded border border-slate-300 px-2 py-1 text-[11px]"
                        >
                          {["count", "sum", "avg", "min", "max"].map((fn) => (
                            <option key={fn} value={fn}>
                              {fn}
                            </option>
                          ))}
                        </select>
                        <input
                          data-testid={`pipeline-step-measure-column-${rowIndex}`}
                          value={row.column}
                          disabled={readOnly || saving}
                          onChange={(e) => {
                            const rows = step.measureRows.map((r, i) =>
                              i === rowIndex
                                ? { ...r, column: e.target.value }
                                : r,
                            );
                            updateStep(index, { measureRows: rows });
                          }}
                          placeholder={t(
                            "console.datasets.pipeline.workbench.measureColumnPlaceholder",
                          )}
                          className="w-40 rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                        />
                        <button
                          type="button"
                          data-testid={`pipeline-step-measure-remove-${rowIndex}`}
                          disabled={readOnly || saving || step.measureRows.length === 1}
                          onClick={() =>
                            updateStep(index, {
                              measureRows: step.measureRows.filter(
                                (_, i) => i !== rowIndex,
                              ),
                            })
                          }
                          className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                        >
                          ✕
                        </button>
                      </li>
                    ))}
                  </ul>
                  <button
                    type="button"
                    data-testid="pipeline-step-measure-add"
                    disabled={readOnly || saving}
                    onClick={() =>
                      updateStep(index, {
                        measureRows: [
                          ...step.measureRows,
                          { func: "count", column: "" },
                        ],
                      })
                    }
                    className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                  >
                    {t("console.datasets.pipeline.workbench.measureAdd")}
                  </button>
                </div>
              )}
              {step.op === "union" && (
                <label className="flex w-64 flex-col gap-1">
                  <span className="text-[11px] text-slate-500">
                    {t("console.datasets.pipeline.workbench.unionRightLabel")}
                  </span>
                  <select
                    data-testid="pipeline-step-union-right"
                    value={step.rightDatasetId}
                    disabled={readOnly || saving}
                    onChange={(e) =>
                      updateStep(index, { rightDatasetId: e.target.value })
                    }
                    className={inputClass}
                  >
                    <option value="">
                      {t("console.datasets.pipeline.workbench.basePlaceholder")}
                    </option>
                    {datasets.map((dataset) => (
                      <option key={dataset.id} value={dataset.id}>
                        {dataset.name}
                      </option>
                    ))}
                  </select>
                  <span className="text-[11px] text-slate-400">
                    {t("console.datasets.pipeline.workbench.unionHint")}
                  </span>
                </label>
              )}
              {step.op === "window" && (
                <div
                  data-testid="pipeline-step-window"
                  className="w-full rounded border border-slate-200 bg-white p-2"
                >
                  <p className="text-[11px] text-slate-500">
                    {t("console.datasets.pipeline.workbench.windowHint")}
                  </p>

                  {/* 分区列多选（勾选列表；候选 = 基础数据集列快照——步骤真实
                      输入列以服务端列快照校验为准）。 */}
                  <div className="mt-2">
                    <p className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.windowPartitionLabel")}
                    </p>
                    {baseColumns.length === 0 ? (
                      <p
                        data-testid="pipeline-step-window-partition-empty"
                        className="mt-1 text-[11px] text-slate-400"
                      >
                        {t(
                          "console.datasets.pipeline.workbench.windowPartitionEmpty",
                        )}
                      </p>
                    ) : (
                      <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                        {baseColumns.map((column) => {
                          const checked = step.partitionColumns.includes(
                            column.name,
                          );
                          return (
                            <li key={column.name}>
                              <label className="flex items-center gap-1 text-[11px] text-slate-600">
                                <input
                                  type="checkbox"
                                  data-testid={`pipeline-step-window-partition-${column.name}`}
                                  checked={checked}
                                  disabled={readOnly || saving}
                                  onChange={() =>
                                    updateStep(index, {
                                      partitionColumns: checked
                                        ? step.partitionColumns.filter(
                                            (name) => name !== column.name,
                                          )
                                        : [
                                            ...step.partitionColumns,
                                            column.name,
                                          ],
                                    })
                                  }
                                />
                                <span className="font-mono">{column.name}</span>
                              </label>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>

                  {/* 排序行（列 + 方向）：排序类/位移类函数必填。 */}
                  <div className="mt-2">
                    <p className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.windowOrderByLabel")}
                    </p>
                    <ul className="mt-1 space-y-1">
                      {step.orderByRows.map((row, rowIndex) => (
                        <li key={rowIndex} className="flex items-center gap-2">
                          <input
                            data-testid={`pipeline-step-window-order-column-${rowIndex}`}
                            value={row.column}
                            disabled={readOnly || saving}
                            onChange={(e) => {
                              const rows = step.orderByRows.map((r, i) =>
                                i === rowIndex
                                  ? { ...r, column: e.target.value }
                                  : r,
                              );
                              updateStep(index, { orderByRows: rows });
                            }}
                            placeholder={t(
                              "console.datasets.pipeline.workbench.windowColumnPlaceholder",
                            )}
                            className="w-40 rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                          />
                          <select
                            data-testid={`pipeline-step-window-order-dir-${rowIndex}`}
                            value={row.dir}
                            disabled={readOnly || saving}
                            onChange={(e) => {
                              const dir: "asc" | "desc" =
                                e.target.value === "desc" ? "desc" : "asc";
                              const rows = step.orderByRows.map((r, i) =>
                                i === rowIndex ? { ...r, dir } : r,
                              );
                              updateStep(index, { orderByRows: rows });
                            }}
                            className="w-24 rounded border border-slate-300 px-2 py-1 text-[11px]"
                          >
                            <option value="asc">
                              {t(
                                "console.datasets.pipeline.workbench.windowDirAsc",
                              )}
                            </option>
                            <option value="desc">
                              {t(
                                "console.datasets.pipeline.workbench.windowDirDesc",
                              )}
                            </option>
                          </select>
                          <button
                            type="button"
                            data-testid={`pipeline-step-window-order-remove-${rowIndex}`}
                            disabled={
                              readOnly || saving || step.orderByRows.length === 1
                            }
                            onClick={() =>
                              updateStep(index, {
                                orderByRows: step.orderByRows.filter(
                                  (_, i) => i !== rowIndex,
                                ),
                              })
                            }
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                          >
                            ✕
                          </button>
                        </li>
                      ))}
                    </ul>
                    <button
                      type="button"
                      data-testid="pipeline-step-window-order-add"
                      disabled={readOnly || saving}
                      onClick={() =>
                        updateStep(index, {
                          orderByRows: [
                            ...step.orderByRows,
                            { column: "", dir: "asc" },
                          ],
                        })
                      }
                      className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                    >
                      {t("console.datasets.pipeline.workbench.windowOrderAdd")}
                    </button>
                  </div>

                  {/* 函数行（func + column? + offset? + as）。 */}
                  <div className="mt-2">
                    <p className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.windowFuncLabel")}
                    </p>
                    <ul className="mt-1 space-y-1">
                      {step.windowRows.map((row, rowIndex) => {
                        const isRanking = WINDOW_RANKING_FUNCS.includes(
                          row.func as WindowFunc,
                        );
                        const isShift = WINDOW_SHIFT_FUNCS.includes(
                          row.func as WindowFunc,
                        );
                        return (
                          <li
                            key={rowIndex}
                            className="flex flex-wrap items-center gap-2"
                          >
                            <select
                              data-testid={`pipeline-step-window-func-${rowIndex}`}
                              value={row.func}
                              disabled={readOnly || saving}
                              onChange={(e) => {
                                const rows = step.windowRows.map((r, i) =>
                                  i === rowIndex
                                    ? { ...r, func: e.target.value }
                                    : r,
                                );
                                updateStep(index, { windowRows: rows });
                              }}
                              className="w-32 rounded border border-slate-300 px-2 py-1 text-[11px]"
                            >
                              {WINDOW_FUNCS.map((func) => (
                                <option key={func} value={func}>
                                  {func}
                                </option>
                              ))}
                            </select>
                            {!isRanking && (
                              <input
                                data-testid={`pipeline-step-window-column-${rowIndex}`}
                                value={row.column}
                                disabled={readOnly || saving}
                                onChange={(e) => {
                                  const rows = step.windowRows.map((r, i) =>
                                    i === rowIndex
                                      ? { ...r, column: e.target.value }
                                      : r,
                                  );
                                  updateStep(index, { windowRows: rows });
                                }}
                                placeholder={t(
                                  "console.datasets.pipeline.workbench.windowFuncColumnPlaceholder",
                                )}
                                className="w-36 rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                              />
                            )}
                            {isShift && (
                              <input
                                data-testid={`pipeline-step-window-offset-${rowIndex}`}
                                value={row.offset}
                                disabled={readOnly || saving}
                                onChange={(e) => {
                                  const rows = step.windowRows.map((r, i) =>
                                    i === rowIndex
                                      ? { ...r, offset: e.target.value }
                                      : r,
                                  );
                                  updateStep(index, { windowRows: rows });
                                }}
                                placeholder={t(
                                  "console.datasets.pipeline.workbench.windowOffsetPlaceholder",
                                )}
                                inputMode="numeric"
                                className="w-20 rounded border border-slate-300 px-2 py-1 text-[11px]"
                              />
                            )}
                            <span className="text-slate-400">→</span>
                            <input
                              data-testid={`pipeline-step-window-as-${rowIndex}`}
                              value={row.as}
                              disabled={readOnly || saving}
                              onChange={(e) => {
                                const rows = step.windowRows.map((r, i) =>
                                  i === rowIndex
                                    ? { ...r, as: e.target.value }
                                    : r,
                                );
                                updateStep(index, { windowRows: rows });
                              }}
                              placeholder={t(
                                "console.datasets.pipeline.workbench.windowAsPlaceholder",
                              )}
                              className="w-36 rounded border border-slate-300 px-2 py-1 text-[11px]"
                            />
                            <button
                              type="button"
                              data-testid={`pipeline-step-window-func-remove-${rowIndex}`}
                              disabled={
                                readOnly || saving || step.windowRows.length === 1
                              }
                              onClick={() =>
                                updateStep(index, {
                                  windowRows: step.windowRows.filter(
                                    (_, i) => i !== rowIndex,
                                  ),
                                })
                              }
                              className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                            >
                              ✕
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                    <button
                      type="button"
                      data-testid="pipeline-step-window-func-add"
                      disabled={readOnly || saving}
                      onClick={() =>
                        updateStep(index, {
                          windowRows: [
                            ...step.windowRows,
                            { func: "count", column: "", offset: "", as: "" },
                          ],
                        })
                      }
                      className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                    >
                      {t("console.datasets.pipeline.workbench.windowFuncAdd")}
                    </button>
                  </div>

                  {/* 试运行（复用 SQL 预览机制：零副作用、不落库）。 */}
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      type="button"
                      data-testid="pipeline-step-window-preview"
                      disabled={
                        readOnly || saving || step.windowPreviewing
                      }
                      onClick={() => void runWindowPreview(index)}
                      className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {step.windowPreviewing
                        ? t(
                            "console.datasets.pipeline.workbench.windowPreviewing",
                          )
                        : t("console.datasets.pipeline.workbench.windowPreview")}
                    </button>
                    <span className="text-[11px] text-slate-400">
                      {t(
                        "console.datasets.pipeline.workbench.windowPreviewNote",
                      )}
                    </span>
                  </div>
                  {step.windowPreviewError && (
                    <p
                      data-testid="pipeline-step-window-preview-error"
                      className="mt-1 text-[11px] text-red-600"
                    >
                      {step.windowPreviewError}
                    </p>
                  )}
                  {step.windowPreview && (
                    <div
                      data-testid="pipeline-step-window-preview-result"
                      className="mt-2 rounded border border-slate-200 bg-slate-50 p-2"
                    >
                      <p className="text-[11px] text-slate-500">
                        {t(
                          "console.datasets.pipeline.workbench.windowPreviewColumns",
                          {
                            columns: step.windowPreview.columns
                              .map((c) => `${c.name}:${c.type}`)
                              .join(", "),
                          },
                        )}
                      </p>
                      <p className="mt-1 text-[11px] text-slate-500">
                        {t(
                          "console.datasets.pipeline.workbench.windowPreviewRows",
                          { count: step.windowPreview.row_count },
                        )}
                      </p>
                      {step.windowPreview.truncated && (
                        <p className="mt-1 text-[11px] text-amber-700">
                          {t(
                            "console.datasets.pipeline.workbench.windowPreviewTruncated",
                          )}
                        </p>
                      )}
                      <pre
                        data-testid="pipeline-step-window-preview-rows"
                        className="mt-1 overflow-x-auto rounded bg-white p-1.5 font-mono text-[10px] text-slate-700"
                      >
                        {JSON.stringify(step.windowPreview.rows, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              )}
              {step.op === "sql" && (
                <div
                  data-testid="pipeline-step-sql"
                  className="w-full rounded border border-slate-200 bg-white p-2"
                >
                  <label className="flex w-full flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.sqlLabel")}
                    </span>
                    <textarea
                      data-testid="pipeline-step-sql-text"
                      value={step.sqlText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { sqlText: e.target.value })
                      }
                      placeholder="SELECT region, count(*) AS n FROM {orders} GROUP BY region"
                      rows={4}
                      className="rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                    />
                    <span className="text-[11px] text-slate-400">
                      {t("console.datasets.pipeline.workbench.sqlHint")}
                    </span>
                  </label>
                  <div className="mt-2">
                    <p className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.sqlTablesLabel")}
                    </p>
                    <ul className="mt-1 space-y-1">
                      {step.sqlTables.map((row, rowIndex) => (
                        <li key={rowIndex} className="flex items-center gap-2">
                          <input
                            data-testid={`pipeline-step-sql-table-name-${rowIndex}`}
                            value={row.name}
                            disabled={readOnly || saving}
                            onChange={(e) => {
                              const rows = step.sqlTables.map((r, i) =>
                                i === rowIndex
                                  ? { ...r, name: e.target.value }
                                  : r,
                              );
                              updateStep(index, { sqlTables: rows });
                            }}
                            placeholder="orders"
                            className="w-28 rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                          />
                          <select
                            data-testid={`pipeline-step-sql-table-dataset-${rowIndex}`}
                            value={row.datasetId}
                            disabled={readOnly || saving}
                            onChange={(e) => {
                              const rows = step.sqlTables.map((r, i) =>
                                i === rowIndex
                                  ? { ...r, datasetId: e.target.value }
                                  : r,
                              );
                              updateStep(index, { sqlTables: rows });
                            }}
                            className="w-48 rounded border border-slate-300 px-2 py-1 text-[11px]"
                          >
                            <option value="">
                              {t("console.datasets.pipeline.workbench.basePlaceholder")}
                            </option>
                            {datasets.map((dataset) => (
                              <option key={dataset.id} value={dataset.id}>
                                {dataset.name}
                              </option>
                            ))}
                          </select>
                          <button
                            type="button"
                            data-testid={`pipeline-step-sql-table-remove-${rowIndex}`}
                            disabled={readOnly || saving || step.sqlTables.length === 1}
                            onClick={() =>
                              updateStep(index, {
                                sqlTables: step.sqlTables.filter(
                                  (_, i) => i !== rowIndex,
                                ),
                              })
                            }
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                          >
                            ✕
                          </button>
                        </li>
                      ))}
                    </ul>
                    <button
                      type="button"
                      data-testid="pipeline-step-sql-table-add"
                      disabled={readOnly || saving}
                      onClick={() =>
                        updateStep(index, {
                          sqlTables: [...step.sqlTables, { name: "", datasetId: "" }],
                        })
                      }
                      className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                    >
                      {t("console.datasets.pipeline.workbench.sqlTableAdd")}
                    </button>
                  </div>
                  <div className="mt-2">
                    <p className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.sqlParamsLabel")}
                    </p>
                    <ul className="mt-1 space-y-1">
                      {step.sqlParams.map((row, rowIndex) => (
                        <li key={rowIndex} className="flex items-center gap-2">
                          <input
                            data-testid={`pipeline-step-sql-param-name-${rowIndex}`}
                            value={row.name}
                            disabled={readOnly || saving}
                            onChange={(e) => {
                              const rows = step.sqlParams.map((r, i) =>
                                i === rowIndex ? { ...r, name: e.target.value } : r,
                              );
                              updateStep(index, { sqlParams: rows });
                            }}
                            placeholder="r"
                            className="w-28 rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                          />
                          <input
                            data-testid={`pipeline-step-sql-param-value-${rowIndex}`}
                            value={row.value}
                            disabled={readOnly || saving}
                            onChange={(e) => {
                              const rows = step.sqlParams.map((r, i) =>
                                i === rowIndex
                                  ? { ...r, value: e.target.value }
                                  : r,
                              );
                              updateStep(index, { sqlParams: rows });
                            }}
                            placeholder={t(
                              "console.datasets.pipeline.workbench.sqlParamValuePlaceholder",
                            )}
                            className="w-56 rounded border border-slate-300 px-2 py-1 text-[11px]"
                          />
                          <button
                            type="button"
                            data-testid={`pipeline-step-sql-param-remove-${rowIndex}`}
                            disabled={readOnly || saving}
                            onClick={() =>
                              updateStep(index, {
                                sqlParams: step.sqlParams.filter(
                                  (_, i) => i !== rowIndex,
                                ),
                              })
                            }
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                          >
                            ✕
                          </button>
                        </li>
                      ))}
                    </ul>
                    <button
                      type="button"
                      data-testid="pipeline-step-sql-param-add"
                      disabled={readOnly || saving}
                      onClick={() =>
                        updateStep(index, {
                          sqlParams: [...step.sqlParams, { name: "", value: "" }],
                        })
                      }
                      className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                    >
                      {t("console.datasets.pipeline.workbench.sqlParamAdd")}
                    </button>
                  </div>
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      type="button"
                      data-testid="pipeline-step-sql-preview"
                      disabled={readOnly || saving || step.sqlPreviewing}
                      onClick={() => void runSqlPreview(index)}
                      className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {step.sqlPreviewing
                        ? t("console.datasets.pipeline.workbench.sqlPreviewing")
                        : t("console.datasets.pipeline.workbench.sqlPreview")}
                    </button>
                    <span className="text-[11px] text-slate-400">
                      {t("console.datasets.pipeline.workbench.sqlPreviewNote")}
                    </span>
                  </div>
                  {step.sqlPreviewError && (
                    <p
                      data-testid="pipeline-step-sql-preview-error"
                      className="mt-1 text-[11px] text-red-600"
                    >
                      {step.sqlPreviewError}
                    </p>
                  )}
                  {step.sqlPreview && (
                    <div
                      data-testid="pipeline-step-sql-preview-result"
                      className="mt-2 rounded border border-slate-200 bg-slate-50 p-2"
                    >
                      <p className="text-[11px] text-slate-500">
                        {t("console.datasets.pipeline.workbench.sqlPreviewColumns", {
                          columns: step.sqlPreview.columns
                            .map((c) => `${c.name}:${c.type}`)
                            .join(", "),
                        })}
                      </p>
                      <p className="mt-1 text-[11px] text-slate-500">
                        {t("console.datasets.pipeline.workbench.sqlPreviewRows", {
                          count: step.sqlPreview.row_count,
                        })}
                      </p>
                      {step.sqlPreview.truncated && (
                        <p className="mt-1 text-[11px] text-amber-700">
                          {t("console.datasets.pipeline.workbench.sqlPreviewTruncated")}
                        </p>
                      )}
                      <pre
                        data-testid="pipeline-step-sql-preview-rows"
                        className="mt-1 overflow-x-auto rounded bg-white p-1.5 font-mono text-[10px] text-slate-700"
                      >
                        {JSON.stringify(step.sqlPreview.rows, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              )}
            </div>
            {stepErrors[index + 1] && (
              <p
                data-testid="pipeline-step-json-error"
                data-seq={index + 1}
                className="mt-1 text-[11px] text-red-600"
              >
                {stepErrors[index + 1]}
              </p>
            )}
          </li>
        ))}
      </ol>
      {steps.length === 0 && (
        <p data-testid="pipeline-steps-empty" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.pipeline.workbench.stepsEmpty")}
        </p>
      )}

      {/* 高级区「输入来源」（DAG 多输入；缺省收起，不改变简单用户路径）。 */}
      {steps.length > 0 && (
        <div data-testid="pipeline-inputs-advanced" className="mt-3">
          <button
            type="button"
            data-testid="pipeline-inputs-toggle"
            aria-expanded={inputsOpen}
            onClick={() => setInputsOpen((open) => !open)}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {t("console.datasets.pipeline.workbench.inputsAdvanced")}
            {inputsOpen ? " ▾" : " ▸"}
          </button>
          {inputsOpen && (
            <div
              data-testid="pipeline-inputs-section"
              className="mt-2 rounded-lg border border-slate-200 bg-slate-50/60 p-3"
            >
              <p className="text-[11px] text-slate-500">
                {t("console.datasets.pipeline.workbench.inputsHint")}
              </p>
              <ul className="mt-2 space-y-2">
                {steps.map((_step, index) => {
                  const selected = validRefsFor(
                    steps,
                    effectiveInputsFor(inputsTouched, index),
                  );
                  const error = inputsErrors[index];
                  const options: Array<{ ref: number; label: string }> = [
                    {
                      ref: INPUT_BASE,
                      label: t(
                        "console.datasets.pipeline.workbench.inputsBase",
                      ),
                    },
                    ...steps
                      .map((_candidate, j) => j)
                      .filter((j) => j !== index)
                      .map((j) => ({
                        ref: j,
                        label:
                          inputStepName(steps, j) +
                          (j > index
                            ? `（${t(
                                "console.datasets.pipeline.workbench.inputsForwardMark",
                              )}）`
                            : ""),
                      })),
                  ];
                  return (
                    <li
                      key={index}
                      data-testid="pipeline-inputs-step"
                      data-index={index}
                      className="rounded border border-slate-200 bg-white p-2"
                    >
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[11px] font-medium text-slate-600">
                          {t(
                            "console.datasets.pipeline.workbench.inputsStepLabel",
                            {
                              seq: index + 1,
                              name: inputStepName(steps, index),
                            },
                          )}
                        </span>
                        {options.map((option) => {
                          const isSelected = selected.includes(option.ref);
                          return (
                            <button
                              key={option.ref}
                              type="button"
                              data-testid="pipeline-inputs-option"
                              data-index={index}
                              data-ref={option.ref}
                              data-name={
                                option.ref === INPUT_BASE
                                  ? "base"
                                  : inputStepName(steps, option.ref)
                              }
                              data-selected={isSelected ? "true" : "false"}
                              aria-pressed={isSelected}
                              disabled={readOnly || saving}
                              onClick={() => toggleInput(index, option.ref)}
                              className={`rounded border px-1.5 py-0.5 text-[11px] disabled:cursor-not-allowed ${
                                isSelected
                                  ? "border-slate-900 bg-slate-900 text-white"
                                  : "border-slate-300 bg-white text-slate-600 hover:bg-slate-100"
                              }`}
                            >
                              {option.label}
                            </button>
                          );
                        })}
                        {selected.length === 0 && (
                          <span
                            data-testid="pipeline-inputs-none"
                            className="text-[11px] text-slate-400"
                          >
                            {t("console.datasets.pipeline.workbench.inputsNone")}
                          </span>
                        )}
                      </div>
                      {error && (
                        <p
                          data-testid="pipeline-step-inputs-error"
                          data-code={error.code}
                          data-index={index}
                          className="mt-1 text-[11px] text-red-600"
                        >
                          {inputsErrorText(error.code, error.detail)}
                        </p>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* 版本语义明示：改步骤/基础集 → 保存即追加新版本（append-only）。 */}
      {willCreateVersion && !readOnly && (
        <p
          data-testid="pipeline-version-confirm"
          className="mt-3 rounded border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-xs text-amber-700"
        >
          {t("console.datasets.pipeline.workbench.versionConfirm", {
            next: (pipeline?.current_version ?? 0) + 1,
          })}
        </p>
      )}

      <div className="mt-3 flex items-center gap-2">
        {!readOnly && (
          <button
            type="button"
            data-testid="pipeline-editor-save"
            disabled={saving}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {saving
              ? t("console.datasets.pipeline.workbench.saving")
              : t("console.datasets.pipeline.workbench.save")}
          </button>
        )}
        <button
          type="button"
          data-testid="pipeline-editor-cancel"
          disabled={saving}
          onClick={onCancel}
          className="rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {t("console.datasets.pipeline.workbench.cancel")}
        </button>
      </div>
    </section>
  );
}
