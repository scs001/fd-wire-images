// 表单 spec 契约与纯函数（wire-form-builder 任务 4.1）。
//
// 存储形状：产物 spec 是信封 `{title, spec:{datasetId, fields[], submitLabel?,
// successMessage?}}`（内层即表单本体）；field = `{column, label,
// input: text|number|date|select, required?, options?, visibleWhen?}`（select
// 须带非空 options；column 表单内唯一；visibleWhen 为可选单显隐条件——
// wire-form-field-linkage）。结构自检（validateFormSpec）与提交前校验
// （validateFormValues）在此集中——FormEditor（保存前）与 FormRuntime
// （提交前）共用同一套规则，与后端 artifacts/form_spec.py 同源：前端先拦
// 只是体验层，后端仍是唯一裁决面（422 如实呈现）。
//
// 显隐联动（wire-form-field-linkage）：字段可带可选单条件 visibleWhen
// （当另一字段的值满足算子时显示）。evaluateFieldVisibility 纯函数求隐藏
// 集（运行时每次 values 变化重求）；validateFormValues 联动感知（隐藏字段
// 带值拒、必填只在可见时强制）。
//
// 联动 v1+（wire-form-linkage-v2，additive）：①多条件组合 visibleWhenGroup
// （{mode: all|any, conditions 1..10}，与 visibleWhen 互斥；求值 all=全真
// 可见/any=一真可见，控制字段值在 raw values 即参与——组内不递归）；②联动
// 改选项 optionsFrom（select 可选键 {field, cases, fallback?}，与静态
// options 互斥恰有其一；按当前值解析候选，空选项字段并入隐藏集——不可填
// 即不可见；select 提交值必须 ∈ 解析后选项，选项变化现值不在新集即清值
// 不进载荷）。契约与 api 侧逐字同规（冻结）。
import type { DatasetColumn } from "../api";

export const FORM_INPUT_TYPES = ["text", "number", "date", "select"] as const;
export type FormInputType = (typeof FORM_INPUT_TYPES)[number];

/** 显隐条件算子枚举（wire-form-field-linkage；与 api 侧同源冻结）。 */
export const FIELD_CONDITION_OPS = [
  "eq",
  "neq",
  "gt",
  "gte",
  "lt",
  "lte",
  "contains",
  "empty",
  "notEmpty",
] as const;
export type FieldConditionOp = (typeof FIELD_CONDITION_OPS)[number];

/** 字段可选单显隐条件：当 `field`（同表单另一字段的 column）满足 `op`
 *  （必要时对照 `value`）时本字段可见。empty/notEmpty 不带 value；其余
 *  算子 value 必为标量且字符串形态 trim 非空。 */
export interface FieldVisibleWhen {
  field: string;
  op: FieldConditionOp;
  value?: string | number | boolean;
}

/** 组合模式（wire-form-linkage-v2）：all=全部条件真才可见、any=至少一真
 *  可见。 */
export type FieldConditionGroupMode = "all" | "any";

/** 多条件组合显隐（wire-form-linkage-v2，与 visibleWhen 互斥）：每条即
 *  单条件显隐的冻结形状与算子枚举，conditions 1..10 条。 */
export interface FieldVisibleWhenGroup {
  mode: FieldConditionGroupMode;
  conditions: FieldVisibleWhen[];
}

/** 选项联动（wire-form-linkage-v2，select 专属、与静态 options 互斥）：
 *  按控制字段（同表单另一字段的 column）当前值解析候选选项——命中 cases
 *  键取其列表，控制值空或 trim 字符串形态未命中 → fallback → 无则 []。
 *  cases 键须非空串、值须非空字符串列表；fallback 同列表规（可选）。 */
export interface FieldOptionsFrom {
  field: string;
  cases: Record<string, string[]>;
  fallback?: string[];
}

// ── 高级录入三新键（wire-form-entry-advanced）──────────────────────────────
// 与 api 侧 artifacts/form_spec.py 同源冻结：optionsFromDataset（跨数据集
// 动态选项）、fillFrom（选值回填）、computed（计算字段）、constraints
//（提交闸）。三新键与既有键的互斥/引用规则在 validateFormSpec 草稿校验
// 镜像后端保存校验（前端先拦，后端仍是唯一裁决面）。

/** 数据集过滤算子白名单（optionsFromDataset.filters 与 probe.filters 共用，
 *  为数据面查询 filters 算子的子集：eq|in|gt|gte|lt|lte）。 */
export const DATASET_FILTER_OPS = [
  "eq",
  "in",
  "gt",
  "gte",
  "lt",
  "lte",
] as const;
export type DatasetFilterOp = (typeof DATASET_FILTER_OPS)[number];

/** 过滤值：标量或标量数组（仅 in 允许数组）。 */
export type DatasetFilterValue = string | number | boolean;

/** optionsFromDataset 的字面量过滤条件（值不引用字段——{field} 引用是
 *  probe.filters 专属）。 */
export interface DatasetFilter {
  column: string;
  op: DatasetFilterOp;
  value: DatasetFilterValue | DatasetFilterValue[];
}

/** 跨数据集动态选项源（select 专属、与静态 options 及 optionsFrom 互斥
 *  恰有其一）：选项=引用数据集有效视图按 filters 过滤后 column 列的去重
 *  非空值，按值升序，至多 limit 条（缺省 200、硬上限 500），超限截断
 *  truncated 如实标注。 */
export interface FieldOptionsFromDataset {
  datasetId: string;
  column: string;
  filters?: DatasetFilter[];
  limit?: number;
}

/** 选值回填：field 必须引用同表单内带 optionsFromDataset 的 select 字段
 *  （不得自引用）；源 select 取值 v 时，从其引用数据集按「选项列=v」取
 *  至多一行，将该行 column 值填入本字段作为可改写的默认值。 */
export interface FieldFillFrom {
  field: string;
  column: string;
}

/** 计算字段（v1 仅 add_days 一 op）：date 引用同表单内另一 input=date
 *  字段；days 引用同表单内 input=number 字段或非负整数字面量；不得自引用。
 *  date/days 任一参与值有效才求值（YYYY-MM-DD + N 天，日历语义含闰年/
 *  月末进位），产出为可改写默认值；失败留空不报错。 */
export interface FieldComputed {
  op: "add_days";
  date: string;
  days: string | number;
}

/** 提交闸 probe 过滤条件：与 DatasetFilter 同形，value 额外允许
 *  `{field: <本表单字段列>}`（以提交 values 按其 input 类型解析代入）。 */
export interface FormProbeFilter {
  column: string;
  op: DatasetFilterOp;
  value: DatasetFilterValue | DatasetFilterValue[] | { field: string };
}

/** 存在性探测：对引用数据集有效视图按 limit=1 探测——expect "none" 探测
 *  到任一行即拦截、"any" 零行即拦截（服务端执法，前端仅 UX 层）。 */
export interface FormConstraintProbe {
  datasetId: string;
  filters: FormProbeFilter[];
  expect: "none" | "any";
}

/** spec 级提交闸约束：when（可选，单条件显隐同形——真才执行探测）以提交
 *  values 求值；命中拦截 422 带 id 与 message（逐字呈现）。 */
export interface FormConstraint {
  id: string;
  message: string;
  when?: FieldVisibleWhen;
  probe: FormConstraintProbe;
}

export interface FormField {
  /** 目标数据集列名（提交 values 的键）。 */
  column: string;
  /** 呈现名（缺省=列名）。 */
  label: string;
  input: FormInputType;
  required?: boolean;
  /** select 专属：候选选项（保存时须非空；与 optionsFrom 互斥恰有其一）。 */
  options?: string[];
  /** 可选显隐条件（缺省无条件=恒可见；与 visibleWhenGroup 互斥）。 */
  visibleWhen?: FieldVisibleWhen;
  /** 可选多条件组合显隐（wire-form-linkage-v2）。 */
  visibleWhenGroup?: FieldVisibleWhenGroup;
  /** select 可选选项联动（wire-form-linkage-v2）。 */
  optionsFrom?: FieldOptionsFrom;
  /** select 可选跨数据集动态选项源（wire-form-entry-advanced）。 */
  optionsFromDataset?: FieldOptionsFromDataset;
  /** 可选选值回填（wire-form-entry-advanced；源须为动态选项 select）。 */
  fillFrom?: FieldFillFrom;
  /** 可选计算字段（wire-form-entry-advanced；v1 仅 add_days）。 */
  computed?: FieldComputed;
}

/** 表单本体（信封的 spec 内层）。datasetId=null 为构建中间态（可保存不可提交）。 */
export interface FormSpecBody {
  datasetId: string | null;
  fields: FormField[];
  submitLabel?: string;
  successMessage?: string;
  /** 可选提交闸约束（wire-form-entry-advanced；≤10 条，服务端执法）。 */
  constraints?: FormConstraint[];
}

/** 文案长度上限（与后端 form_spec 校验同源口径）。 */
export const FORM_TEXT_LIMITS = {
  title: 200,
  label: 100,
  submitLabel: 50,
  successMessage: 200,
} as const;

/** 列类型 → 建议输入类型：数值列 number、日期列 date、其余（含拿不到类型）text。 */
export function suggestInputType(columnType: string): FormInputType {
  if (/int|float|double|decimal|numeric|real|number/i.test(columnType)) {
    return "number";
  }
  if (/date/i.test(columnType)) return "date";
  return "text";
}

/** 列 → 默认字段：label=列名、input 按列类型建议、非必填。 */
export function fieldFromColumn(column: DatasetColumn): FormField {
  return {
    column: column.name,
    label: column.name,
    input: suggestInputType(column.type),
  };
}

/** 列清单 + 勾选 → 字段数组：字段顺序=数据集列序（spec 冻结口径）。 */
export function fieldsFromColumns(
  columns: DatasetColumn[],
  selected: string[],
): FormField[] {
  const keep = new Set(selected);
  return columns.filter((c) => keep.has(c.name)).map(fieldFromColumn);
}

/** 信封组装：编辑器保存时把表单本体放回产物信封（saveArtifactSpec 入参）。
 *  submitLabel/successMessage 仅在非空时写入（缺省不落键）；constraints
 *  非空数组时写入（wire-form-entry-advanced）。 */
export function buildFormEnvelope(
  title: string,
  body: FormSpecBody,
): { title: string; spec: FormSpecBody } {
  const spec: FormSpecBody = {
    datasetId: body.datasetId,
    fields: body.fields,
  };
  const submitLabel = body.submitLabel?.trim();
  const successMessage = body.successMessage?.trim();
  if (submitLabel) spec.submitLabel = submitLabel;
  if (successMessage) spec.successMessage = successMessage;
  if (body.constraints && body.constraints.length > 0) {
    spec.constraints = body.constraints;
  }
  return { title, spec };
}

/** visibleWhen 逐键校验（wire-form-field-linkage）：field 非空 string、
 *  op ∈ 枚举、value 标量且字符串形态 trim 非空（empty 族不得带 value）。
 *  合法则保留（value 保持原标量类型），非法返回 undefined（整键丢弃——
 *  宽容解析绝不整单失败）。自引用/引用不存在属保存校验（编辑器草稿）面，
 *  解析层不裁决（历史已存 spec 照常呈现）。 */
export function normalizeVisibleWhen(raw: unknown): FieldVisibleWhen | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const cond = raw as Record<string, unknown>;
  const field = cond.field;
  const op = cond.op;
  if (typeof field !== "string" || !field.trim()) return undefined;
  if (
    typeof op !== "string" ||
    !FIELD_CONDITION_OPS.includes(op as FieldConditionOp)
  ) {
    return undefined;
  }
  const kind = op as FieldConditionOp;
  const value = cond.value;
  if (kind === "empty" || kind === "notEmpty") {
    if (value !== undefined) return undefined;
    return { field, op: kind };
  }
  if (
    typeof value !== "string" &&
    typeof value !== "number" &&
    typeof value !== "boolean"
  ) {
    return undefined;
  }
  if (String(value).trim() === "") return undefined;
  return { field, op: kind, value };
}

/** visibleWhenGroup 逐键校验归一（wire-form-linkage-v2）：mode ∈ all/any、
 *  conditions 为 1..10 条且逐条过 normalizeVisibleWhen；任一不合法返回
 *  undefined（整键丢弃——与单条件同款宽容纪律，绝不静默改组语义后保留）。 */
export function normalizeVisibleWhenGroup(
  raw: unknown,
): FieldVisibleWhenGroup | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const group = raw as Record<string, unknown>;
  if (group.mode !== "all" && group.mode !== "any") return undefined;
  if (!Array.isArray(group.conditions)) return undefined;
  if (group.conditions.length < 1 || group.conditions.length > 10) {
    return undefined;
  }
  const conditions: FieldVisibleWhen[] = [];
  for (const entry of group.conditions) {
    const cond = normalizeVisibleWhen(entry);
    if (!cond) return undefined;
    conditions.push(cond);
  }
  return { mode: group.mode, conditions };
}

/** 选项列表形状规（与静态 options 同款口径）：非空字符串数组且逐项 trim
 *  非空；非法返回 undefined。 */
function normalizeOptionList(raw: unknown): string[] | undefined {
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const options: string[] = [];
  for (const option of raw) {
    if (typeof option !== "string" || option.trim() === "") return undefined;
    options.push(option);
  }
  return options;
}

/** optionsFrom 逐键校验归一（wire-form-linkage-v2）：field 非空 string、
 *  cases 为对象（键非空串、值过列表规）、fallback 可选（在则过列表规）；
 *  任一不合法返回 undefined（整键丢弃）。引用存在性属保存校验面。 */
export function normalizeOptionsFrom(
  raw: unknown,
): FieldOptionsFrom | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const source = raw as Record<string, unknown>;
  if (typeof source.field !== "string" || !source.field.trim()) return undefined;
  const rawCases = source.cases;
  if (!rawCases || typeof rawCases !== "object" || Array.isArray(rawCases)) {
    return undefined;
  }
  const cases: Record<string, string[]> = {};
  for (const [key, list] of Object.entries(rawCases)) {
    if (!key.trim()) return undefined;
    const options = normalizeOptionList(list);
    if (!options) return undefined;
    cases[key] = options;
  }
  if (source.fallback !== undefined) {
    const fallback = normalizeOptionList(source.fallback);
    if (!fallback) return undefined;
    return { field: source.field, cases, fallback };
  }
  return { field: source.field, cases };
}

/** 过滤值形状收窄（optionsFromDataset 与 probe 共用）：标量或标量数组。 */
function normalizeFilterValue(
  raw: unknown,
): DatasetFilterValue | DatasetFilterValue[] | undefined {
  if (
    typeof raw === "string" ||
    typeof raw === "number" ||
    typeof raw === "boolean"
  ) {
    return raw;
  }
  if (
    Array.isArray(raw) &&
    raw.every(
      (v) =>
        typeof v === "string" ||
        typeof v === "number" ||
        typeof v === "boolean",
    )
  ) {
    return raw as DatasetFilterValue[];
  }
  return undefined;
}

/** 字面量过滤条件归一（optionsFromDataset.filters 用）：column 非空串、
 *  op ∈ 白名单、value 标量或标量数组（仅 in 允许数组——越 op 携带数组的
 *  归一到 undefined 由上层整键丢弃）。 */
function normalizeDatasetFilter(raw: unknown): DatasetFilter | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const f = raw as Record<string, unknown>;
  if (typeof f.column !== "string" || !f.column.trim()) return undefined;
  if (
    typeof f.op !== "string" ||
    !DATASET_FILTER_OPS.includes(f.op as DatasetFilterOp)
  ) {
    return undefined;
  }
  const value = normalizeFilterValue(f.value);
  if (value === undefined) return undefined;
  return { column: f.column, op: f.op as DatasetFilterOp, value };
}

/** optionsFromDataset 逐键校验归一（wire-form-entry-advanced）：datasetId/
 *  column 非空串、filters 逐条过条件规、limit 缺省或 1..500 整数；任一
 *  不合法返回 undefined（整键丢弃）。引用存在性/属主属保存校验面。 */
export function normalizeOptionsFromDataset(
  raw: unknown,
): FieldOptionsFromDataset | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const source = raw as Record<string, unknown>;
  if (typeof source.datasetId !== "string" || !source.datasetId.trim()) {
    return undefined;
  }
  if (typeof source.column !== "string" || !source.column.trim()) {
    return undefined;
  }
  let filters: DatasetFilter[] | undefined;
  if (source.filters !== undefined) {
    if (!Array.isArray(source.filters)) return undefined;
    const list: DatasetFilter[] = [];
    for (const entry of source.filters) {
      const filter = normalizeDatasetFilter(entry);
      if (!filter) return undefined;
      list.push(filter);
    }
    filters = list;
  }
  if (source.limit !== undefined) {
    if (
      typeof source.limit !== "number" ||
      !Number.isInteger(source.limit) ||
      source.limit < 1 ||
      source.limit > 500
    ) {
      return undefined;
    }
  }
  return {
    datasetId: source.datasetId,
    column: source.column,
    ...(filters ? { filters } : {}),
    ...(source.limit !== undefined ? { limit: source.limit } : {}),
  };
}

/** fillFrom 逐键校验归一：field/column 非空串；非法整键丢弃（引用类型
 *  属保存校验面——解析层不裁决，历史已存 spec 照常呈现）。 */
export function normalizeFillFrom(raw: unknown): FieldFillFrom | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const source = raw as Record<string, unknown>;
  if (typeof source.field !== "string" || !source.field.trim()) return undefined;
  if (typeof source.column !== "string" || !source.column.trim()) return undefined;
  return { field: source.field, column: source.column };
}

/** computed 逐键校验归一：op 固定 add_days、date 非空串、days 为非负整数
 *  字面量（number）或字段引用（非空 string）；非法整键丢弃。 */
export function normalizeComputed(
  raw: unknown,
): FieldComputed | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const source = raw as Record<string, unknown>;
  if (source.op !== "add_days") return undefined;
  if (typeof source.date !== "string" || !source.date.trim()) return undefined;
  const days = source.days;
  if (typeof days === "number") {
    if (!Number.isInteger(days) || days < 0) return undefined;
  } else if (typeof days !== "string" || !days.trim()) {
    return undefined;
  }
  return { op: "add_days", date: source.date, days };
}

/** probe 过滤条件归一：value 额外允许 {field: 非空串}（提交 values 代入）。 */
function normalizeProbeFilter(raw: unknown): FormProbeFilter | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const f = raw as Record<string, unknown>;
  if (typeof f.column !== "string" || !f.column.trim()) return undefined;
  if (
    typeof f.op !== "string" ||
    !DATASET_FILTER_OPS.includes(f.op as DatasetFilterOp)
  ) {
    return undefined;
  }
  const value = f.value;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const ref = (value as Record<string, unknown>).field;
    if (typeof ref !== "string" || !ref.trim()) return undefined;
    return { column: f.column, op: f.op as DatasetFilterOp, value: { field: ref } };
  }
  const literal = normalizeFilterValue(value);
  if (literal === undefined) return undefined;
  return { column: f.column, op: f.op as DatasetFilterOp, value: literal };
}

/** constraints 归一（宽容逐键）：id/message 非空、when 可选（过单条件规）、
 *  probe 形状（datasetId/filters 逐条/expect 枚举）；任一不合法整列表
 *  丢弃（回退无 constraints 呈现——运行时零闸，服务端保存校验兜底拦截）。 */
export function normalizeConstraints(
  raw: unknown,
): FormConstraint[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const list: FormConstraint[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") return undefined;
    const c = entry as Record<string, unknown>;
    if (typeof c.id !== "string" || !c.id.trim()) return undefined;
    if (typeof c.message !== "string" || !c.message.trim()) return undefined;
    const probeRaw = c.probe;
    if (!probeRaw || typeof probeRaw !== "object") return undefined;
    const probe = probeRaw as Record<string, unknown>;
    if (
      typeof probe.datasetId !== "string" ||
      !probe.datasetId.trim()
    ) {
      return undefined;
    }
    if (!Array.isArray(probe.filters)) return undefined;
    const filters: FormProbeFilter[] = [];
    for (const f of probe.filters) {
      const filter = normalizeProbeFilter(f);
      if (!filter) return undefined;
      filters.push(filter);
    }
    if (probe.expect !== "none" && probe.expect !== "any") return undefined;
    const when =
      c.when === undefined ? undefined : normalizeVisibleWhen(c.when);
    if (c.when !== undefined && !when) return undefined;
    list.push({
      id: c.id,
      message: c.message,
      ...(when ? { when } : {}),
      probe: {
        datasetId: probe.datasetId,
        filters,
        expect: probe.expect,
      },
    });
  }
  return list;
}

/** 宽容解析产物 spec → 表单本体：同时接受信封（产物详情原样）与本体
 *  （内层直给）两种输入；无 fields 数组视为非表单 spec，返回 null。
 *  字段逐个归一（缺 label 回退列名、非法 input 回退 text），绝不整单失败。 */
export function parseFormSpec(raw: unknown): FormSpecBody | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const inner =
    record.spec && typeof record.spec === "object"
      ? (record.spec as Record<string, unknown>)
      : record;
  if (!Array.isArray(inner.fields)) return null;
  const fields: FormField[] = [];
  for (const entry of inner.fields) {
    if (!entry || typeof entry !== "object") continue;
    const f = entry as Record<string, unknown>;
    if (typeof f.column !== "string" || !f.column) continue;
    const input = FORM_INPUT_TYPES.includes(f.input as FormInputType)
      ? (f.input as FormInputType)
      : "text";
    const options = Array.isArray(f.options)
      ? f.options.filter((o): o is string => typeof o === "string")
      : undefined;
    const visibleWhen = normalizeVisibleWhen(f.visibleWhen);
    const visibleWhenGroup = normalizeVisibleWhenGroup(f.visibleWhenGroup);
    // optionsFrom 为 select 专属键：非 select 字段携带即丢弃（input 回退
    // text 的历史形状照旧呈现，不带联动）。
    const optionsFrom =
      input === "select" ? normalizeOptionsFrom(f.optionsFrom) : undefined;
    // optionsFromDataset 同为 select 专属键（wire-form-entry-advanced）。
    const optionsFromDataset =
      input === "select"
        ? normalizeOptionsFromDataset(f.optionsFromDataset)
        : undefined;
    const fillFrom = normalizeFillFrom(f.fillFrom);
    const computed = normalizeComputed(f.computed);
    fields.push({
      column: f.column,
      label: typeof f.label === "string" && f.label ? f.label : f.column,
      input,
      ...(f.required === true ? { required: true } : {}),
      ...(input === "select" && options ? { options } : {}),
      ...(visibleWhen ? { visibleWhen } : {}),
      ...(visibleWhenGroup ? { visibleWhenGroup } : {}),
      ...(optionsFrom ? { optionsFrom } : {}),
      ...(optionsFromDataset ? { optionsFromDataset } : {}),
      ...(fillFrom ? { fillFrom } : {}),
      ...(computed ? { computed } : {}),
    });
  }
  const constraints = normalizeConstraints(inner.constraints);
  return {
    datasetId: typeof inner.datasetId === "string" ? inner.datasetId : null,
    fields,
    ...(typeof inner.submitLabel === "string" && inner.submitLabel
      ? { submitLabel: inner.submitLabel }
      : {}),
    ...(typeof inner.successMessage === "string" && inner.successMessage
      ? { successMessage: inner.successMessage }
      : {}),
    ...(constraints ? { constraints } : {}),
  };
}

/** 结构自检错误码（编辑器按 console.form.editor.errors.<code> 映射文案）。 */
export type FormSpecErrorCode =
  | "titleRequired"
  | "titleTooLong"
  | "badField"
  | "inputInvalid"
  | "columnDuplicate"
  | "selectOptions"
  | "labelTooLong"
  | "submitLabelTooLong"
  | "successMessageTooLong"
  | "conditionBad"
  | "conditionSelf"
  | "conditionMissing"
  | "conditionGroupBad"
  | "conditionExclusive"
  | "optionsFromBad"
  | "optionsExclusive"
  | "optionsFromSelf"
  | "optionsFromMissing"
  // wire-form-entry-advanced（三新键+constraints）草稿校验错误码。
  | "optionsFromDatasetBad"
  | "fillFromBad"
  | "fillFromSelf"
  | "fillFromSource"
  | "computedBad"
  | "computedSelf"
  | "computedDateRef"
  | "computedDaysRef"
  | "constraintBad"
  | "constraintIdDuplicate";

/** 单条件形状校验（单条件与组内条件共用；不含引用存在性）：field 非空
 *  string、op ∈ 枚举、empty 族不得带 value、其余算子 value 为非空标量。 */
function conditionShapeBad(cond: unknown): boolean {
  if (!cond || typeof cond !== "object") return true;
  const rule = cond as Record<string, unknown>;
  if (typeof rule.field !== "string" || !rule.field.trim()) return true;
  const op = rule.op;
  if (
    typeof op !== "string" ||
    !FIELD_CONDITION_OPS.includes(op as FieldConditionOp)
  ) {
    return true;
  }
  const emptyFamily = op === "empty" || op === "notEmpty";
  const value = rule.value;
  if (emptyFamily) {
    return value !== undefined;
  }
  return (
    (typeof value !== "string" &&
      typeof value !== "number" &&
      typeof value !== "boolean") ||
    String(value).trim() === ""
  );
}

/** 单个 visibleWhen 的草稿校验（编辑器保存前自检用；历史已存 spec 的
 *  解析层不裁决）：形状/op 越枚举/value 非法 → "conditionBad"；
 *  自引用 → "conditionSelf"；引用字段不存在 → "conditionMissing"；
 *  合法 → 无错误。 */
function visibleWhenError(
  field: FormField,
  columns: Set<string>,
): FormSpecErrorCode | null {
  const cond = field.visibleWhen;
  if (!cond || typeof cond !== "object" || conditionShapeBad(cond)) {
    return "conditionBad";
  }
  if (cond.field === field.column) return "conditionSelf";
  if (!columns.has(cond.field)) return "conditionMissing";
  return null;
}

/** visibleWhenGroup 的草稿校验（wire-form-linkage-v2）：mode 越枚举/
 *  conditions 非数组/空/超 10 条/任一条形状非法 → "conditionGroupBad"；
 *  组内条件自引用 → "conditionSelf"、引用不存在 → "conditionMissing"
 *  （沿用单条件错误码——语义一致）。 */
function visibleWhenGroupError(
  field: FormField,
  columns: Set<string>,
): FormSpecErrorCode | null {
  const group = field.visibleWhenGroup;
  if (!group || typeof group !== "object") return "conditionGroupBad";
  if (group.mode !== "all" && group.mode !== "any") return "conditionGroupBad";
  if (
    !Array.isArray(group.conditions) ||
    group.conditions.length < 1 ||
    group.conditions.length > 10
  ) {
    return "conditionGroupBad";
  }
  for (const cond of group.conditions) {
    if (conditionShapeBad(cond)) return "conditionGroupBad";
    if (cond.field === field.column) return "conditionSelf";
    if (!columns.has(cond.field)) return "conditionMissing";
  }
  return null;
}

/** 选项列表形状断言（optionsFrom 校验用，与 normalizeOptionList 同规）。 */
function isOptionList(raw: unknown): raw is string[] {
  if (!Array.isArray(raw) || raw.length === 0) return false;
  return raw.every((o) => typeof o === "string" && o.trim() !== "");
}

/** optionsFrom 的草稿校验（wire-form-linkage-v2）：field 缺/cases 非对象/
 *  键空白/列表规不过/fallback 形状 → "optionsFromBad"；自引用 →
 *  "optionsFromSelf"；引用不存在 → "optionsFromMissing"。 */
function optionsFromError(
  field: FormField,
  columns: Set<string>,
): FormSpecErrorCode | null {
  const source = field.optionsFrom;
  if (!source || typeof source !== "object") return "optionsFromBad";
  if (typeof source.field !== "string" || !source.field.trim()) {
    return "optionsFromBad";
  }
  if (!source.cases || typeof source.cases !== "object") {
    return "optionsFromBad";
  }
  for (const [key, list] of Object.entries(source.cases)) {
    if (!key.trim()) return "optionsFromBad";
    if (!isOptionList(list)) return "optionsFromBad";
  }
  if (source.fallback !== undefined && !isOptionList(source.fallback)) {
    return "optionsFromBad";
  }
  if (source.field === field.column) return "optionsFromSelf";
  if (!columns.has(source.field)) return "optionsFromMissing";
  return null;
}

/** constraints 数量与文案长度上限（与 api 侧保存校验同源口径）。 */
export const FORM_CONSTRAINT_LIMITS = {
  message: 500,
  count: 10,
  filters: 10,
} as const;

/** optionsFromDataset 的草稿校验（wire-form-entry-advanced）：datasetId/
 *  column 非空、filters 逐条形状（op 白名单/标量或数组值）、limit 缺省或
 *  1..500 整数 → "optionsFromDatasetBad"（引用数据集存在性/属主由服务端
 *  保存校验裁决，此处不查）。 */
function optionsFromDatasetError(
  field: FormField,
): FormSpecErrorCode | null {
  const source = field.optionsFromDataset;
  if (!source || typeof source !== "object") return "optionsFromDatasetBad";
  if (typeof source.datasetId !== "string" || !source.datasetId.trim()) {
    return "optionsFromDatasetBad";
  }
  if (typeof source.column !== "string" || !source.column.trim()) {
    return "optionsFromDatasetBad";
  }
  if (source.filters !== undefined) {
    if (
      !Array.isArray(source.filters) ||
      source.filters.length > FORM_CONSTRAINT_LIMITS.filters
    ) {
      return "optionsFromDatasetBad";
    }
    for (const filter of source.filters) {
      if (!filter || filterShapeBad(filter)) return "optionsFromDatasetBad";
    }
  }
  if (source.limit !== undefined) {
    if (
      typeof source.limit !== "number" ||
      !Number.isInteger(source.limit) ||
      source.limit < 1 ||
      source.limit > 500
    ) {
      return "optionsFromDatasetBad";
    }
  }
  return null;
}

/** 字面量过滤条件形状断言（optionsFromDataset.filters 用）。 */
function filterShapeBad(filter: unknown): boolean {
  if (!filter || typeof filter !== "object") return true;
  const f = filter as Record<string, unknown>;
  if (typeof f.column !== "string" || !f.column.trim()) return true;
  if (
    typeof f.op !== "string" ||
    !DATASET_FILTER_OPS.includes(f.op as DatasetFilterOp)
  ) {
    return true;
  }
  return normalizeFilterValue(f.value) === undefined;
}

/** fillFrom 的草稿校验：形状非法 → "fillFromBad"；自引用 → "fillFromSelf"；
 *  源字段不存在或非「带 optionsFromDataset 的 select」→ "fillFromSource"
 *  （同表单引用裁决面——列是否在引用数据集快照中由服务端保存校验裁决）。 */
function fillFromError(
  field: FormField,
  fields: FormField[],
): FormSpecErrorCode | null {
  const source = field.fillFrom;
  if (!source || typeof source !== "object") return "fillFromBad";
  if (
    typeof source.field !== "string" ||
    !source.field.trim() ||
    typeof source.column !== "string" ||
    !source.column.trim()
  ) {
    return "fillFromBad";
  }
  if (source.field === field.column) return "fillFromSelf";
  const target = fields.find((f) => f.column === source.field);
  if (
    !target ||
    target.input !== "select" ||
    target.optionsFromDataset === undefined
  ) {
    return "fillFromSource";
  }
  return null;
}

/** computed 的草稿校验：形状非法（op 越枚举/date 空串/days 非非负整数字面
 *  量或空引用）→ "computedBad"；自引用（date/days 引用自身）→
 *  "computedSelf"；date 引用不存在或非 input=date 字段 → "computedDateRef"；
 *  days 引用不存在或非 input=number 字段 → "computedDaysRef"。 */
function computedFieldError(
  field: FormField,
  fields: FormField[],
): FormSpecErrorCode | null {
  const computed = field.computed;
  if (!computed || typeof computed !== "object") return "computedBad";
  if (computed.op !== "add_days") return "computedBad";
  if (typeof computed.date !== "string" || !computed.date.trim()) {
    return "computedBad";
  }
  if (typeof computed.days === "number") {
    if (!Number.isInteger(computed.days) || computed.days < 0) {
      return "computedBad";
    }
  } else if (typeof computed.days !== "string" || !computed.days.trim()) {
    return "computedBad";
  }
  if (computed.date === field.column) return "computedSelf";
  if (typeof computed.days === "string" && computed.days === field.column) {
    return "computedSelf";
  }
  const dateField = fields.find((f) => f.column === computed.date);
  if (!dateField || dateField.input !== "date") return "computedDateRef";
  if (typeof computed.days === "string") {
    const daysField = fields.find((f) => f.column === computed.days);
    if (!daysField || daysField.input !== "number") return "computedDaysRef";
  }
  return null;
}

/** probe 过滤条件形状断言（value 额外允许 {field: 非空串}）。 */
function probeFilterShapeBad(filter: unknown): boolean {
  if (filterShapeBad(filter)) {
    // 先按字面量规过一遍可排除非对象/列/op 问题；再单看 {field} 引用形态。
    if (!filter || typeof filter !== "object") return true;
    const f = filter as Record<string, unknown>;
    if (typeof f.column !== "string" || !f.column.trim()) return true;
    if (
      typeof f.op !== "string" ||
      !DATASET_FILTER_OPS.includes(f.op as DatasetFilterOp)
    ) {
      return true;
    }
    const value = f.value;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const ref = (value as Record<string, unknown>).field;
      return typeof ref !== "string" || !ref.trim();
    }
    return true;
  }
  return false;
}

/** constraints 的草稿校验（spec 级，wire-form-entry-advanced）：列表 ≤10、
 *  id 非空唯一、message 非空 ≤500、when 可选（单条件同形且引用存在）、
 *  probe 形状（datasetId 非空、filters ≤10 逐条含 {field} 引用形态、
 *  expect ∈ none|any）——任一非法 → "constraintBad"；id 重复 →
 *  "constraintIdDuplicate"。 */
function constraintsErrors(
  constraints: unknown,
  allColumns: Set<string>,
): FormSpecErrorCode[] {
  const errors: FormSpecErrorCode[] = [];
  if (!Array.isArray(constraints) || constraints.length === 0) {
    return errors;
  }
  if (constraints.length > FORM_CONSTRAINT_LIMITS.count) {
    errors.push("constraintBad");
    return errors;
  }
  const seenIds = new Set<string>();
  for (const constraint of constraints) {
    if (!constraint || typeof constraint !== "object") {
      errors.push("constraintBad");
      continue;
    }
    const c = constraint as Record<string, unknown>;
    if (typeof c.id !== "string" || !c.id.trim()) errors.push("constraintBad");
    else if (seenIds.has(c.id)) errors.push("constraintIdDuplicate");
    else seenIds.add(c.id);
    if (
      typeof c.message !== "string" ||
      !c.message.trim() ||
      c.message.length > FORM_CONSTRAINT_LIMITS.message
    ) {
      errors.push("constraintBad");
    }
    if (c.when !== undefined) {
      if (
        conditionShapeBad(c.when) ||
        !(c.when as FieldVisibleWhen).field ||
        !allColumns.has((c.when as FieldVisibleWhen).field)
      ) {
        errors.push("constraintBad");
      }
    }
    const probe = c.probe;
    if (!probe || typeof probe !== "object") {
      errors.push("constraintBad");
      continue;
    }
    const p = probe as Record<string, unknown>;
    if (typeof p.datasetId !== "string" || !p.datasetId.trim()) {
      errors.push("constraintBad");
    }
    if (
      !Array.isArray(p.filters) ||
      p.filters.length === 0 ||
      p.filters.length > FORM_CONSTRAINT_LIMITS.filters
    ) {
      errors.push("constraintBad");
    } else {
      for (const filter of p.filters) {
        if (probeFilterShapeBad(filter)) {
          errors.push("constraintBad");
          break;
        }
      }
    }
    if (p.expect !== "none" && p.expect !== "any") {
      errors.push("constraintBad");
    }
  }
  return errors;
}

/** 结构自检（保存前；与后端 form_spec 规则同源）：title 非空+上限、字段
 *  结构合法、input 枚举、column 唯一、select 非空 options、文案长度上限。
 *  datasetId 非空时的存在性/属主由服务端裁决（422 如实呈现），此处不查。 */
export function validateFormSpec(
  title: string,
  body: FormSpecBody,
): FormSpecErrorCode[] {
  const errors: FormSpecErrorCode[] = [];
  const trimmedTitle = title.trim();
  if (!trimmedTitle) errors.push("titleRequired");
  else if (trimmedTitle.length > FORM_TEXT_LIMITS.title) errors.push("titleTooLong");
  const seen = new Set<string>();
  // 全量列名集（conditionMissing 的引用存在性对照——先收集后校验，字段
  // 顺序不影响结果）。
  const allColumns = new Set(
    body.fields
      .filter(
        (f) =>
          f && typeof f.column === "string" && (f.column as string).length > 0,
      )
      .map((f) => f.column),
  );
  for (const field of body.fields) {
    if (
      !field ||
      typeof field.column !== "string" ||
      !field.column ||
      typeof field.label !== "string"
    ) {
      errors.push("badField");
      continue;
    }
    if (!FORM_INPUT_TYPES.includes(field.input)) errors.push("inputInvalid");
    if (seen.has(field.column)) errors.push("columnDuplicate");
    seen.add(field.column);
    if (field.input === "select") {
      // 选项三源互斥恰有其一（wire-form-entry-advanced）：静态 options /
      // 选项联动 optionsFrom / 数据集源 optionsFromDataset——两源并存即拒
      // （optionsExclusive），零源即 selectOptions；单源时逐源形状校验。
      const sourceCount = [
        field.options,
        field.optionsFrom,
        field.optionsFromDataset,
      ].filter((source) => source !== undefined).length;
      if (sourceCount > 1) errors.push("optionsExclusive");
      else if (sourceCount === 0) errors.push("selectOptions");
      if (field.optionsFrom !== undefined) {
        const sourceError = optionsFromError(field, allColumns);
        if (sourceError) errors.push(sourceError);
      }
      if (field.optionsFromDataset !== undefined) {
        const datasetError = optionsFromDatasetError(field);
        if (datasetError) errors.push(datasetError);
      }
      if (sourceCount === 1 && field.options !== undefined) {
        const options = (field.options ?? []).filter((o) => o.trim());
        if (options.length === 0) errors.push("selectOptions");
      }
    }
    if ((field.label ?? "").length > FORM_TEXT_LIMITS.label) {
      errors.push("labelTooLong");
    }
    if (field.visibleWhen !== undefined) {
      const condError = visibleWhenError(field, allColumns);
      if (condError) errors.push(condError);
    }
    if (field.visibleWhenGroup !== undefined) {
      const groupError = visibleWhenGroupError(field, allColumns);
      if (groupError) errors.push(groupError);
    }
    // 单条件与条件组合互斥（wire-form-linkage-v2）：并存即拒（定位字段
    // 由 api 侧 422 文案承担，web 侧错误码如实呈现）。
    if (field.visibleWhen !== undefined && field.visibleWhenGroup !== undefined) {
      errors.push("conditionExclusive");
    }
    // 三新键（wire-form-entry-advanced）：回填/计算的形状与引用校验。
    if (field.fillFrom !== undefined) {
      const fillError = fillFromError(field, body.fields);
      if (fillError) errors.push(fillError);
    }
    if (field.computed !== undefined) {
      const computedErr = computedFieldError(field, body.fields);
      if (computedErr) errors.push(computedErr);
    }
  }
  // 提交闸 constraints（wire-form-entry-advanced）：形状/数量/引用同规校验。
  if (body.constraints !== undefined) {
    for (const code of constraintsErrors(body.constraints, allColumns)) {
      errors.push(code);
    }
  }
  if ((body.submitLabel?.trim().length ?? 0) > FORM_TEXT_LIMITS.submitLabel) {
    errors.push("submitLabelTooLong");
  }
  if (
    (body.successMessage?.trim().length ?? 0) > FORM_TEXT_LIMITS.successMessage
  ) {
    errors.push("successMessageTooLong");
  }
  return errors;
}

/** 数值串判定（gt/gte/lt/lte 数值比较的前置）：lhs 与 rhs 双双匹配才走
 *  Number() 数值比较，否则字符串字典序（与 api 侧同款冻结口径）。 */
const NUMERIC_RE = /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/;

/** 单条件求值内核（单条件与组内条件共用）：返回条件是否为真。空值 =
 *  缺键 / null / undefined / String(ctrl).trim()===""（数字 0 与 false
 *  不是空值）。empty：ctrl 为空值 → 真；notEmpty 反之。带值算子在 ctrl
 *  为空值时一律假（含 neq）。非空时 lhs=String(ctrl).trim()（bool →
 *  "true"/"false"）、rhs=String(value).trim()；eq/neq 精确串比、contains
 *  子串、比较族走 NUMERIC_RE 双匹配数值比否则字典序。 */
function evalCondition(
  cond: FieldVisibleWhen,
  values: Record<string, unknown>,
): boolean {
  const ctrl = values[cond.field];
  const ctrlEmpty = ctrl == null || String(ctrl).trim() === "";
  if (cond.op === "empty") return ctrlEmpty;
  if (cond.op === "notEmpty") return !ctrlEmpty;
  if (ctrlEmpty) return false;
  const lhs = String(ctrl).trim();
  const rhs = String(cond.value).trim();
  switch (cond.op) {
    case "eq":
      return lhs === rhs;
    case "neq":
      return lhs !== rhs;
    case "contains":
      return lhs.includes(rhs);
    default: {
      // gt/gte/lt/lte：双方皆为数值串才数值比较，否则字典序。
      if (NUMERIC_RE.test(lhs) && NUMERIC_RE.test(rhs)) {
        const a = Number(lhs);
        const b = Number(rhs);
        return cond.op === "gt"
          ? a > b
          : cond.op === "gte"
            ? a >= b
            : cond.op === "lt"
              ? a < b
              : a <= b;
      }
      return cond.op === "gt"
        ? lhs > rhs
        : cond.op === "gte"
          ? lhs >= rhs
          : cond.op === "lt"
            ? lhs < rhs
            : lhs <= rhs;
    }
  }
}

/** select 字段当前候选选项解析（wire-form-linkage-v2，与 api 侧同规纯
 *  函数）：无 optionsFrom → 静态 options 原样（select 之外恒 []）；有则按
 *  raw values 取控制值——为空（缺键/null/trim 空）或 trim 字符串形态不在
 *  cases 键 → fallback → 无 fallback 为 []；命中 cases 键取其列表。 */
export function resolveSelectOptions(
  field: Pick<FormField, "options" | "optionsFrom">,
  values: Record<string, unknown>,
): string[] {
  const source = field.optionsFrom;
  if (!source) return field.options ?? [];
  const ctrl = values[source.field];
  if (ctrl == null || String(ctrl).trim() === "") {
    return source.fallback ?? [];
  }
  return source.cases[String(ctrl).trim()] ?? source.fallback ?? [];
}

/** 显隐条件求值（wire-form-field-linkage + wire-form-linkage-v2，与 api
 *  侧同规纯函数）：返回当前 values 下的**隐藏** column 集（不在集内=可见）。
 *
 * 规则：无 visibleWhen/visibleWhenGroup → 可见（字段同时携带两者时单条件
 * 优先——保存校验已拦并存，此为防御位）。visibleWhen：条件假 → 隐藏。
 * visibleWhenGroup：all=全部条件真才可见、any=至少一真可见（空组防御位：
 *  all 空组恒真、any 空组恒假——保存校验已拦 1..10）。**不递归**：条件只看
 *  values，不看控制字段自身的可见性（控制字段值在 raw values 即参与比较）。
 * 选项联动：select 解析后选项为空（含静态 options 为空的防御位）→ 并入
 *  隐藏集（不可填即不可见，与条件隐藏取并集）。
 *
 * 动态选项源（wire-form-entry-advanced）：optionsFromDataset 字段的选项
 * 由运行时异步解析（console 会话查询/公开选项端点）——经 dynamicOptions
 * 传入「已解析字段的选项表」（键在场=已解析）：已解析且为空 → 并入隐藏
 * 集；未解析（加载中/出错）→ 不隐藏（错误态呈现可读错误，绝不静默空白）。
 * 省略 dynamicOptions（旧行为）时该类字段不参与空选项隐藏裁决。 */
export function evaluateFieldVisibility(
  fields: FormField[],
  values: Record<string, unknown>,
  dynamicOptions?: Record<string, string[]>,
): Set<string> {
  const hidden = new Set<string>();
  for (const field of fields) {
    if (field.visibleWhen) {
      if (!evalCondition(field.visibleWhen, values)) hidden.add(field.column);
    } else if (field.visibleWhenGroup) {
      const results = field.visibleWhenGroup.conditions.map((cond) =>
        evalCondition(cond, values),
      );
      const visible =
        field.visibleWhenGroup.mode === "all"
          ? results.every(Boolean)
          : results.some(Boolean);
      if (!visible) hidden.add(field.column);
    }
  }
  for (const field of fields) {
    if (field.input !== "select") continue;
    if (field.optionsFromDataset) {
      if (dynamicOptions && field.column in dynamicOptions) {
        if ((dynamicOptions[field.column] ?? []).length === 0) {
          hidden.add(field.column);
        }
      }
      continue;
    }
    if (resolveSelectOptions(field, values).length === 0) {
      hidden.add(field.column);
    }
  }
  return hidden;
}

/** 提交前逐字段校验错误码（运行时按 console.form.runtime.errors.<code> 映射）。 */
export type FormValueErrorCode =
  | "required"
  | "number"
  | "date"
  | "select"
  | "text"
  | "hidden";

export interface FormValuesValidation {
  /** 归一后的提交 values（仅通过校验的字段；非必填缺省不写键——不写 null/空串）。 */
  values: Record<string, unknown>;
  /** 逐字段错误：column → 机器码；空对象=全部通过。 */
  errors: Record<string, FormValueErrorCode>;
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** values 提交前校验+归一（与后端公开提交类型校验同规则；显隐联动感知）：
 *  先求 hidden 集——hidden 字段 raw 值非空（trim 后）→ errors="hidden"
 *  （隐藏不可携带值）、值为空→跳过不写键、required 不查；可见字段现有
 *  行为逐字不变。number 收数值或可 parseFloat 的数字串（NaN/Inf 拒）并
 *  归一为数值；date 须 YYYY-MM-DD（regex，拒绝时间部分）；select 须 ∈
 *  options；text 收标量（数字/布尔 stringify，对象/数组拒）；非必填缺省
 *  跳过。
 *
 * 动态选项源（wire-form-entry-advanced）：optionsFromDataset 字段的 select
 *  成员裁决以 dynamicOptions[column]（运行时已解析选项）为准——键不在场
 *  （未解析）按空集裁决（非空值如实报 select，防未解析值溜进载荷）。
 * 服务端提交路径仍以提交时刻实时解析裁决（防篡改），此处仅 UX 先拦。 */
export function validateFormValues(
  fields: FormField[],
  raw: Record<string, unknown>,
  dynamicOptions?: Record<string, string[]>,
): FormValuesValidation {
  const values: Record<string, unknown> = {};
  const errors: Record<string, FormValueErrorCode> = {};
  const hidden = evaluateFieldVisibility(fields, raw, dynamicOptions);
  for (const field of fields) {
    const value = raw[field.column];
    if (hidden.has(field.column)) {
      // 隐藏字段：带值（trim 后非空）即拒；空值跳过（不写键、不查 required）。
      const text = value == null ? "" : String(value).trim();
      if (text !== "") errors[field.column] = "hidden";
      continue;
    }
    if (value !== null && typeof value === "object") {
      errors[field.column] = "text";
      continue;
    }
    const text = value == null ? "" : String(value).trim();
    if (text === "") {
      if (field.required) errors[field.column] = "required";
      continue;
    }
    switch (field.input) {
      case "number": {
        const num = typeof value === "number" ? value : Number(text);
        if (Number.isFinite(num)) values[field.column] = num;
        else errors[field.column] = "number";
        break;
      }
      case "date": {
        const match = DATE_RE.exec(text);
        const month = match ? Number(match[2]) : 0;
        const day = match ? Number(match[3]) : 0;
        if (match && month >= 1 && month <= 12 && day >= 1 && day <= 31) {
          values[field.column] = text;
        } else {
          errors[field.column] = "date";
        }
        break;
      }
      case "select": {
        // 选项联动（wire-form-linkage-v2）：按当前 raw values 解析后的选项
        // 集裁决成员（服务端公开提交同规 422，客户端先拦）；解析为空的
        // 字段已在 hidden 集中走隐藏分支。动态选项源（wire-form-entry-
        // advanced）以运行时已解析选项表裁决。
        const options = field.optionsFromDataset
          ? (dynamicOptions?.[field.column] ?? [])
          : resolveSelectOptions(field, raw);
        if (options.includes(text)) values[field.column] = text;
        else errors[field.column] = "select";
        break;
      }
      default: {
        // text：标量 stringify（运行时输入本就是字符串；数字/布尔转字符串）。
        values[field.column] = text;
      }
    }
  }
  return { values, errors };
}

// ── 运行时求值纯函数（wire-form-entry-advanced，与 api 侧同端口径）──────

/** YYYY-MM-DD + N 天（日历语义：闰年/月末进位正确；UTC 运算避开时区
 *  漂移）。输入非法（形状/月日越界如 2026-02-30/负数或非整天数）返回
 *  null——求值失败留空不报错。 */
export function addDaysToDate(date: string, days: number): string | null {
  if (!Number.isInteger(days) || days < 0) return null;
  const match = DATE_RE.exec(date);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  const base = new Date(Date.UTC(year, month - 1, day));
  // 日历校验：如 2026-02-30 会被 Date 归一到 3 月——如实视为非法。
  if (
    base.getUTCFullYear() !== year ||
    base.getUTCMonth() !== month - 1 ||
    base.getUTCDate() !== day
  ) {
    return null;
  }
  const next = new Date(base.getTime() + days * 86_400_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-${pad(next.getUTCDate())}`;
}

/** computed 字段当前默认值求值（双端同规）：date 引用字段值须为合法
 *  YYYY-MM-DD、days 为非负整数字面量或引用字段值可解析为非负整数——
 *  任一参与值缺失/非法返回 null（留空不报错，required 由必填校验拦截）。 */
export function evaluateComputedField(
  field: Pick<FormField, "computed">,
  values: Record<string, unknown>,
): string | null {
  const computed = field.computed;
  if (!computed || computed.op !== "add_days") return null;
  const dateRaw = values[computed.date];
  const dateText = dateRaw == null ? "" : String(dateRaw).trim();
  if (!DATE_RE.test(dateText)) return null;
  let days: number;
  if (typeof computed.days === "number") {
    days = computed.days;
  } else {
    const daysRaw = values[computed.days];
    const daysText = daysRaw == null ? "" : String(daysRaw).trim();
    if (!/^\d+$/.test(daysText)) return null;
    days = Number(daysText);
  }
  return addDaysToDate(dateText, days);
}

/** 运行时动态选项解析状态（optionsFromDataset 字段逐列）。 */
export interface DynamicOptionsEntry {
  status: "loading" | "ready" | "error";
  options: string[];
  truncated: boolean;
  /** status=error 时的可读错误文案（引用失效不静默空白）。 */
  error?: string;
}

/** distinct 查询响应归一：非空字符串化、去重、按值升序、≤limit 裁剪并
 *  如实标注截断（服务端已排序/去重时幂等；服务端 truncated 并入）。 */
export function normalizeDistinctOptions(
  values: unknown[],
  limit: number,
  responseTruncated = false,
): { options: string[]; truncated: boolean } {
  const seen = new Set<string>();
  const list: string[] = [];
  for (const value of values) {
    if (value == null) continue;
    const text = String(value).trim();
    if (text === "" || seen.has(text)) continue;
    seen.add(text);
    list.push(text);
  }
  list.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const truncated = responseTruncated || list.length > limit;
  return { options: list.slice(0, limit), truncated };
}

/** 选值回填查表（公开页 fills 映射路径）：fields[源字段].fills[声明列]
 *  [选项值]——查不到返回 null（源行缺失留空不报错）。 */
export function lookupSharedFill(
  options: unknown,
  sourceColumn: string,
  declaredColumn: string,
  optionValue: string,
): string | null {
  if (!options || typeof options !== "object") return null;
  const fields = (options as { fields?: unknown }).fields;
  if (!fields || typeof fields !== "object") return null;
  const field = (fields as Record<string, unknown>)[sourceColumn];
  if (!field || typeof field !== "object") return null;
  const fills = (field as { fills?: unknown }).fills;
  if (!fills || typeof fills !== "object") return null;
  const column = (fills as Record<string, unknown>)[declaredColumn];
  if (!column || typeof column !== "object") return null;
  const value = (column as Record<string, unknown>)[optionValue];
  if (typeof value !== "string" || value.trim() === "") return null;
  return value;
}
