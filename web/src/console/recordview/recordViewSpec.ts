// 单记录公开页 spec 契约与纯函数（wire-record-view 任务 4.1/5.2）。
//
// 存储形状：产物 spec 是信封 `{title, spec:{datasetId, keyColumn,
// headerFields[], sections[]}}`（内层即本体）；headerFields = `[{column,
// label?}]`（label 缺省=列名）；sections = `[{title, datasetId, dateColumn,
// filters[], limit?}]`（每个 section 声明一个时间轴源）；filter value 为
// 字面标量/标量数组（仅 in）或 `{fromField: <主记录列>}`（运行时以主记录行
// 的该列值代入）。与后端 artifacts 侧校验同源：validateRecordViewSpec 是
// 草稿校验的前端镜像（编辑器保存前先拦，后端仍是唯一裁决面 422 如实呈现）；
// 未绑数据集（datasetId=null）的构建中间态可保存、不可分享。
//
// 取数响应（公开端点与 console 试预览同形）的解析归一
// normalizeRecordViewPayload 与时间轴升序排序 sortRowsByDate 亦在此集中
// ——RecordViewRuntime（公开页与试预览共用）消费同一形状，双端同构。
import type { DatasetColumn } from "../api";

/** section 过滤算子白名单（数据面查询 filters 算子的子集，与 api 同源）。 */
export const RECORD_VIEW_FILTER_OPS = [
  "eq",
  "in",
  "gt",
  "gte",
  "lt",
  "lte",
] as const;
export type RecordViewFilterOp = (typeof RECORD_VIEW_FILTER_OPS)[number];

/** 过滤字面值：标量或标量数组（仅 in 允许数组）。 */
export type RecordViewLiteral = string | number | boolean;

/** section 过滤条件：value 为字面量或 {fromField: 主记录列} 引用。 */
export interface RecordViewFilter {
  column: string;
  op: RecordViewFilterOp;
  value: RecordViewLiteral | RecordViewLiteral[] | { fromField: string };
}

/** 头部展示字段：column 属主数据集列快照、表单内唯一；label 缺省=列名。 */
export interface RecordViewHeaderField {
  column: string;
  label?: string;
}

/** 时间轴源声明：dateColumn 按其升序渲染节点。 */
export interface RecordViewSection {
  title: string;
  datasetId: string;
  dateColumn: string;
  filters: RecordViewFilter[];
  /** 1..500，缺省 200。 */
  limit?: number;
}

/** spec 本体（信封的 spec 内层）；datasetId=null 为构建中间态。 */
export interface RecordViewSpecBody {
  datasetId: string | null;
  keyColumn: string | null;
  headerFields: RecordViewHeaderField[];
  sections: RecordViewSection[];
}

/** 数量/长度/上限（与 api 侧保存校验同源口径）。 */
export const RECORD_VIEW_LIMITS = {
  title: 200,
  label: 100,
  sectionTitle: 100,
  headerFields: 50,
  sections: 10,
  filters: 10,
  limitDefault: 200,
  limitMax: 500,
  /** 键值选择器去重下拉上限（spec 冻结值）。 */
  distinctKeys: 500,
} as const;

/** 信封组装：编辑器保存时把本体放回产物信封（saveArtifactSpec 入参）。 */
export function buildRecordViewEnvelope(
  title: string,
  body: RecordViewSpecBody,
): { title: string; spec: RecordViewSpecBody } {
  return { title, spec: body };
}

/** 过滤值形状收窄：字面标量 / 标量数组 / {fromField: 非空串}。 */
function filterValueOf(raw: unknown): RecordViewFilter["value"] | undefined {
  if (
    typeof raw === "string" ||
    typeof raw === "number" ||
    typeof raw === "boolean"
  ) {
    return raw;
  }
  if (Array.isArray(raw)) {
    const literals = raw.filter(
      (v): v is RecordViewLiteral =>
        typeof v === "string" ||
        typeof v === "number" ||
        typeof v === "boolean",
    );
    return literals.length === raw.length ? literals : undefined;
  }
  if (raw && typeof raw === "object") {
    const ref = (raw as Record<string, unknown>).fromField;
    if (typeof ref === "string" && ref.trim()) return { fromField: ref };
  }
  return undefined;
}

/** 宽容解析产物 spec → 本体：同时接受信封（产物详情原样）与本体两种输入；
 *  headerFields 与 sections 均非数组（如其他 kind 的 spec）视为非
 *  record-view 形状返回 null（容器回退诚实空态——空白创建的最小空 spec
 *  两键均为空数组，照常解析）。 */
export function parseRecordViewSpec(raw: unknown): RecordViewSpecBody | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const inner =
    record.spec && typeof record.spec === "object"
      ? (record.spec as Record<string, unknown>)
      : record;
  if (
    !Array.isArray(inner.headerFields) &&
    !Array.isArray(inner.sections)
  ) {
    return null;
  }
  const headerFields: RecordViewHeaderField[] = Array.isArray(
    inner.headerFields,
  )
    ? inner.headerFields.flatMap((entry): RecordViewHeaderField[] => {
        if (!entry || typeof entry !== "object") return [];
        const f = entry as Record<string, unknown>;
        if (typeof f.column !== "string" || !f.column.trim()) return [];
        return [
          {
            column: f.column,
            ...(typeof f.label === "string" && f.label.trim()
              ? { label: f.label }
              : {}),
          },
        ];
      })
    : [];
  const sections: RecordViewSection[] = Array.isArray(inner.sections)
    ? inner.sections.flatMap((entry): RecordViewSection[] => {
        if (!entry || typeof entry !== "object") return [];
        const s = entry as Record<string, unknown>;
        if (typeof s.datasetId !== "string" || !s.datasetId.trim()) return [];
        const filters = Array.isArray(s.filters)
          ? s.filters.flatMap((f): RecordViewFilter[] => {
              if (!f || typeof f !== "object") return [];
              const filter = f as Record<string, unknown>;
              if (typeof filter.column !== "string" || !filter.column.trim()) {
                return [];
              }
              if (
                typeof filter.op !== "string" ||
                !RECORD_VIEW_FILTER_OPS.includes(
                  filter.op as RecordViewFilterOp,
                )
              ) {
                return [];
              }
              const value = filterValueOf(filter.value);
              if (value === undefined) return [];
              return [
                {
                  column: filter.column,
                  op: filter.op as RecordViewFilterOp,
                  value,
                },
              ];
            })
          : [];
        return [
          {
            title: typeof s.title === "string" ? s.title : "",
            datasetId: s.datasetId,
            dateColumn: typeof s.dateColumn === "string" ? s.dateColumn : "",
            filters,
            ...(typeof s.limit === "number" ? { limit: s.limit } : {}),
          },
        ];
      })
    : [];
  return {
    datasetId: typeof inner.datasetId === "string" && inner.datasetId.trim()
      ? inner.datasetId
      : null,
    keyColumn:
      typeof inner.keyColumn === "string" && inner.keyColumn.trim()
        ? inner.keyColumn
        : null,
    headerFields,
    sections,
  };
}

/** 草稿校验错误码（编辑器按 console.recordview.editor.errors.<code> 映射）。 */
export type RecordViewSpecErrorCode =
  | "titleRequired"
  | "titleTooLong"
  | "unbound"
  | "keyColumnRequired"
  | "keyColumnMissing"
  | "headerColumnMissing"
  | "headerColumnDuplicate"
  | "headerLabelTooLong"
  | "headerFieldsTooMany"
  | "sectionBad"
  | "sectionDatasetMissing"
  | "sectionDateColumnMissing"
  | "sectionTooMany"
  | "sectionLimitBad"
  | "filterBad"
  | "filtersTooMany"
  | "fromFieldMissing";

/** 单条 filter 的形状校验（草稿镜像：column 非空、op ∈ 白名单、value 形状
 *  合法；引用存在性由 fromFieldMissing 单独裁决）。 */
function filterShapeBad(filter: RecordViewFilter): boolean {
  if (!filter || typeof filter.column !== "string" || !filter.column.trim()) {
    return true;
  }
  if (
    typeof filter.op !== "string" ||
    !RECORD_VIEW_FILTER_OPS.includes(filter.op as RecordViewFilterOp)
  ) {
    return true;
  }
  return filterValueOf(filter.value) === undefined;
}

/** 是否 {fromField} 引用形态。 */
function isFromFieldRef(value: RecordViewFilter["value"]): value is {
  fromField: string;
} {
  return (
    !!value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    "fromField" in value
  );
}

/** 草稿校验（保存前；与后端保存校验同规的前端镜像）：datasetId 为空时
 *  允许最小空 spec 中间态（携带任何键列/头部/section → "unbound"）；
 *  绑定后逐项校验键列/头部列在列快照内、section 形状与日期列、filters
 *  形状与 {fromField} 引用主记录列、数量与上限。columnsOf 返回 null
 *  （数据集已删/不在列表）时跳过该集的列成员校验（服务端 422 兜底）。 */
export function validateRecordViewSpec(
  title: string,
  body: RecordViewSpecBody,
  columnsOf?: (datasetId: string) => DatasetColumn[] | null,
): RecordViewSpecErrorCode[] {
  const errors: RecordViewSpecErrorCode[] = [];
  const trimmedTitle = title.trim();
  if (!trimmedTitle) errors.push("titleRequired");
  else if (trimmedTitle.length > RECORD_VIEW_LIMITS.title) {
    errors.push("titleTooLong");
  }
  const mainColumns =
    body.datasetId != null && columnsOf
      ? (columnsOf(body.datasetId) ?? null)
      : null;
  const mainColumnNames = new Set(mainColumns?.map((c) => c.name) ?? []);

  if (body.datasetId == null) {
    // 未绑数据集的构建中间态：只允许最小空 spec（可保存不可分享）。
    if (
      (body.keyColumn ?? "") !== "" ||
      body.headerFields.length > 0 ||
      body.sections.length > 0
    ) {
      errors.push("unbound");
    }
    return errors;
  }

  if (!body.keyColumn || !body.keyColumn.trim()) {
    errors.push("keyColumnRequired");
  } else if (
    mainColumns != null &&
    !mainColumnNames.has(body.keyColumn)
  ) {
    errors.push("keyColumnMissing");
  }

  if (body.headerFields.length > RECORD_VIEW_LIMITS.headerFields) {
    errors.push("headerFieldsTooMany");
  }
  const seenColumns = new Set<string>();
  for (const field of body.headerFields) {
    if (!field || typeof field.column !== "string" || !field.column.trim()) {
      errors.push("headerColumnMissing");
      continue;
    }
    if (mainColumns != null && !mainColumnNames.has(field.column)) {
      errors.push("headerColumnMissing");
    }
    if (seenColumns.has(field.column)) errors.push("headerColumnDuplicate");
    seenColumns.add(field.column);
    if ((field.label ?? "").length > RECORD_VIEW_LIMITS.label) {
      errors.push("headerLabelTooLong");
    }
  }

  if (body.sections.length > RECORD_VIEW_LIMITS.sections) {
    errors.push("sectionTooMany");
  }
  for (const section of body.sections) {
    if (
      !section ||
      typeof section.title !== "string" ||
      !section.title.trim() ||
      section.title.length > RECORD_VIEW_LIMITS.sectionTitle
    ) {
      errors.push("sectionBad");
    }
    if (!section.datasetId || !section.datasetId.trim()) {
      errors.push("sectionDatasetMissing");
    }
    if (!section.dateColumn || !section.dateColumn.trim()) {
      errors.push("sectionDateColumnMissing");
    } else if (columnsOf) {
      const sectionColumns = columnsOf(section.datasetId) ?? null;
      if (
        sectionColumns != null &&
        !sectionColumns.some((c) => c.name === section.dateColumn)
      ) {
        errors.push("sectionDateColumnMissing");
      }
    }
    if (section.limit !== undefined) {
      if (
        typeof section.limit !== "number" ||
        !Number.isInteger(section.limit) ||
        section.limit < 1 ||
        section.limit > RECORD_VIEW_LIMITS.limitMax
      ) {
        errors.push("sectionLimitBad");
      }
    }
    if (section.filters.length > RECORD_VIEW_LIMITS.filters) {
      errors.push("filtersTooMany");
    }
    for (const filter of section.filters) {
      if (filterShapeBad(filter)) {
        errors.push("filterBad");
        continue;
      }
      if (isFromFieldRef(filter.value)) {
        if (!mainColumnNames.has(filter.value.fromField)) {
          errors.push("fromFieldMissing");
        }
      }
    }
  }
  return errors;
}

// ── 取数响应解析归一（公开端点与 console 试预览同形）────────────────────

/** 头部字段值（value 原样 JSON——数字/字符串，渲染时 String()）。 */
export interface RecordViewHeaderValue {
  label: string;
  column: string;
  value: unknown;
}

/** 时间轴段载荷（truncated 如实标注；error 为引用失效等可读错误）。 */
export interface RecordViewSectionPayload {
  title: string;
  dateColumn: string;
  rows: Array<Record<string, unknown>>;
  truncated: boolean;
  error?: string;
}

/** 取数响应归一形状（found=false 时 header/sections 为空数组）。 */
export interface RecordViewPayload {
  title: string;
  version: number;
  expiresAt: string | null;
  found: boolean;
  header: RecordViewHeaderValue[];
  sections: RecordViewSectionPayload[];
}

/** 响应归一（宽容但形状冻结）：found 只认真值；header/sections 逐条收窄、
 *  非法条目丢弃不整单失败；expires_at 恒 string|null。 */
export function normalizeRecordViewPayload(raw: unknown): RecordViewPayload {
  const record =
    raw && typeof raw === "object"
      ? (raw as Record<string, unknown>)
      : {};
  const header = Array.isArray(record.header)
    ? record.header.flatMap((entry): RecordViewHeaderValue[] => {
        if (!entry || typeof entry !== "object") return [];
        const h = entry as Record<string, unknown>;
        if (typeof h.column !== "string" || !h.column) return [];
        return [
          {
            column: h.column,
            label:
              typeof h.label === "string" && h.label.trim()
                ? h.label
                : h.column,
            value: h.value,
          },
        ];
      })
    : [];
  const sections = Array.isArray(record.sections)
    ? record.sections.flatMap((entry): RecordViewSectionPayload[] => {
        if (!entry || typeof entry !== "object") return [];
        const s = entry as Record<string, unknown>;
        return [
          {
            title: typeof s.title === "string" ? s.title : "",
            dateColumn: typeof s.dateColumn === "string" ? s.dateColumn : "",
            rows: Array.isArray(s.rows)
              ? s.rows.filter(
                  (row): row is Record<string, unknown> =>
                    !!row && typeof row === "object" && !Array.isArray(row),
                )
              : [],
            truncated: s.truncated === true,
            ...(typeof s.error === "string" && s.error ? { error: s.error } : {}),
          },
        ];
      })
    : [];
  return {
    title: typeof record.title === "string" ? record.title : "",
    version: typeof record.version === "number" ? record.version : 0,
    expiresAt:
      typeof record.expires_at === "string" ? record.expires_at : null,
    found: record.found === true,
    header,
    sections,
  };
}

/** 数值串判定（日期值排序的前置）：双双匹配才走 Number() 数值比较。 */
const NUMERIC_RE = /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/;

/** 时间轴行按日期列升序排序（渲染前的确定性防线——服务端已升序，这里
 *  幂等兜底）：空值（缺键/null/空串）排末尾；双方数值串走数值比较，其余
 *  字符串字典序。返回新数组（入参不动）。 */
export function sortRowsByDate<
  T extends Record<string, unknown>,
>(rows: T[], dateColumn: string): T[] {
  const dateText = (row: T): string | null => {
    const value = row[dateColumn];
    if (value == null) return null;
    const text = String(value).trim();
    return text === "" ? null : text;
  };
  return [...rows].sort((a, b) => {
    const left = dateText(a);
    const right = dateText(b);
    if (left == null && right == null) return 0;
    if (left == null) return 1;
    if (right == null) return -1;
    if (NUMERIC_RE.test(left) && NUMERIC_RE.test(right)) {
      return Number(left) - Number(right);
    }
    return left < right ? -1 : left > right ? 1 : 0;
  });
}

/** 单元格呈现值：null/undefined/空串 → 占位"—"，其余 String()（契约：
 *  无查表、不做严格类型断言）。 */
export function recordCellText(value: unknown): string {
  if (value == null) return "—";
  const text = String(value);
  return text === "" ? "—" : text;
}
